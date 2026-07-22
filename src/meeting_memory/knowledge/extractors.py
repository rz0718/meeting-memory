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

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
        version: str = EXTRACTOR_VERSION,
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
    def _decode_model_json(content: Any) -> Dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise TransientExtractionError("empty model response")
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].rstrip()
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ExtractionError("model response was not valid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != {"candidates"}:
            raise ExtractionError("model JSON must contain only a candidates array")
        return raw

    def extract(self, source: MeetingSource) -> List[KnowledgeCandidate]:
        content = self.client.complete(
            [
                {
                    "role": "system",
                    "content": "Return strictly valid JSON. Never add Markdown commentary.",
                },
                {"role": "user", "content": self._prompt(source)},
            ],
            response_format={"type": "json_object"},
        )
        raw = self._decode_model_json(content)
        candidates = raw["candidates"]
        if not isinstance(candidates, list):
            raise ExtractionError("model candidates must be an array")
        return [KnowledgeCandidate.from_dict(item) for item in candidates]
