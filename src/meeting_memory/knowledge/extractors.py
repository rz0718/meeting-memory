"""Replaceable semantic extraction interfaces and adapters."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from .constants import CATEGORIES, CONFIDENCES, EXTRACTOR_VERSION, STATUSES
from .errors import (
    AuthenticationError,
    ConfigurationError,
    ExtractionError,
    TransientExtractionError,
)
from .models import KnowledgeCandidate, MeetingSource
from .openrouter import OpenRouterChatClient


class KnowledgeExtractor(ABC):
    """Provider-neutral candidate extraction boundary."""

    version = EXTRACTOR_VERSION

    @abstractmethod
    def extract(self, source: MeetingSource) -> List[KnowledgeCandidate]:
        raise NotImplementedError


class FakeExtractor(KnowledgeExtractor):
    """Deterministic extractor used by tests and local demonstrations."""

    def __init__(
        self,
        responses: Union[
            Mapping[str, Any],
            Sequence[Any],
            Callable[[MeetingSource], Any],
        ],
        version: str = EXTRACTOR_VERSION,
    ):
        self.responses = responses
        self.version = version
        self.calls: List[str] = []

    def extract(self, source: MeetingSource) -> List[KnowledgeCandidate]:
        self.calls.append(source.relative_path)
        if callable(self.responses):
            raw = self.responses(source)
        elif isinstance(self.responses, Mapping):
            raw = self.responses.get(source.relative_path, [])
        else:
            raw = self.responses
        if isinstance(raw, Exception):
            raise raw
        if not isinstance(raw, list):
            raise ExtractionError("fake extractor response must be a list")
        return [item if isinstance(item, KnowledgeCandidate) else KnowledgeCandidate.from_dict(item) for item in raw]


class OpenRouterExtractor(KnowledgeExtractor):
    """OpenRouter structured-JSON implementation of the extraction boundary."""

    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    response_preview_limit = 200

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
        version: str = EXTRACTOR_VERSION,
        max_attempts: int = 3,
    ):
        self.api_key = (
            api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        self.model = (
            model
            or os.environ.get("DAILY_KNOWLEDGE_MODEL")
            or os.environ.get("OPENROUTER_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
        )
        self.timeout = timeout
        self.version = version
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts
        if not self.api_key:
            raise ConfigurationError(
                "OPENROUTER_API_KEY or ANTHROPIC_AUTH_TOKEN is not configured"
            )
        if not self.model:
            raise ConfigurationError(
                "DAILY_KNOWLEDGE_MODEL or OPENROUTER_MODEL is not configured"
            )
        self.client = OpenRouterChatClient(
            self.api_key, self.model, timeout=self.timeout
        )

    @staticmethod
    def _prompt(source: MeetingSource) -> str:
        return """You extract only durable company knowledge from a dated source document.
The document may contain Google Meet notes or a Slack channel message digest.

Return JSON only, with this exact top-level shape:
{"candidates": [candidate, ...]}

Each candidate must contain:
category, title, statement, status, effective_date, owner, confidence,
reason_for_durability, evidence.

Allowed categories: %s
Allowed statuses: %s
Allowed confidence: %s

Each evidence item must contain source, anchor, line_start, line_end. The source
must be exactly %s. Use 1-based inclusive line numbers from the supplied note.
Use null for unknown dates or owners. For Slack, distinguish the message author
from an explicitly assigned owner and do not treat ordinary conversation as an
approved decision. Do not invent approval, dates, ownership, or evidence.
Exclude ordinary actions, blockers, deadlines, casual discussion, transient
status, unsupported opinion, and speculation. An empty candidate list is valid.
Do not mark anything deprecated.

SOURCE SHA-256: %s
SOURCE DATE: %s
SOURCE WITH LINE NUMBERS:
%s
""" % (
            ", ".join(CATEGORIES),
            ", ".join(STATUSES),
            ", ".join(CONFIDENCES),
            source.relative_path,
            source.sha256,
            source.source_date,
            "\n".join("%d: %s" % (index, line) for index, line in enumerate(source.lines, 1)),
        )

    @staticmethod
    def _unwrap(text: str) -> str:
        """Drop Markdown fences and any prose around the JSON object."""
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].rstrip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
        return text.strip()

    @classmethod
    def _preview(cls, content: str) -> str:
        text = " ".join(content.split())
        if len(text) > cls.response_preview_limit:
            text = "%s..." % text[: cls.response_preview_limit]
        return " (response began: %s)" % text if text else ""

    @classmethod
    def _decode_model_json(cls, content: Any) -> Dict[str, Any]:
        # Every shape failure here is the model deviating from the prompt, not a
        # deterministic defect, so each one stays retryable: identical sources
        # have failed one night and extracted cleanly the next.
        if not isinstance(content, str) or not content.strip():
            raise TransientExtractionError("empty model response")
        text = cls._unwrap(content.strip())
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise TransientExtractionError(
                "model response was not valid JSON%s" % cls._preview(content)
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
            raise TransientExtractionError(
                "model JSON must contain a candidates array%s" % cls._preview(content)
            )
        return raw

    def extract(self, source: MeetingSource) -> List[KnowledgeCandidate]:
        messages = [
            {
                "role": "system",
                "content": "Return strictly valid JSON. Never add Markdown commentary.",
            },
            {"role": "user", "content": self._prompt(source)},
        ]
        attempt = 1
        while True:
            content = self.client.complete(
                messages, response_format={"type": "json_object"}
            )
            try:
                raw = self._decode_model_json(content)
            except TransientExtractionError:
                if attempt >= self.max_attempts:
                    raise
                attempt += 1
                continue
            return [KnowledgeCandidate.from_dict(item) for item in raw["candidates"]]
