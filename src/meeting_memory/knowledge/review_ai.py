"""Provider-neutral AI advisor interface and OpenRouter implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from .errors import (
    AuthenticationError,
    ConfigurationError,
    ExtractionError,
    TransientExtractionError,
)
from .openrouter import OpenRouterChatClient
from .review_suggestions import (
    PROMPT_VERSION,
    ReviewRecommendation,
    SuggestionContext,
    SuggestionValidationError,
    build_suggestion_context,
    create_suggestion,
    decode_recommendation_json,
    unusable_evidence_recommendation,
    validate_recommendation,
)
from .util import utc_now


class KnowledgeReviewAdvisor(ABC):
    @abstractmethod
    def suggest(self, context: SuggestionContext) -> ReviewRecommendation:
        raise NotImplementedError


class FakeReviewAdvisor(KnowledgeReviewAdvisor):
    """Deterministic advisor for tests and local demonstrations."""

    def __init__(
        self,
        responses: Union[Any, Sequence[Any]],
        repository: Any = None,
    ):
        # Accept the original internal (repository, responses) order as well as
        # the public, response-first test adapter shape.
        if hasattr(responses, "load_reviews") and repository is not None:
            responses, repository = repository, responses
        self.repository = repository
        self.responses = responses
        self.calls: List[SuggestionContext] = []

    def suggest(self, context: SuggestionContext) -> ReviewRecommendation:
        self.calls.append(context)
        if (
            isinstance(self.responses, Sequence)
            and not isinstance(self.responses, (str, bytes, dict))
        ):
            raw = self.responses[len(self.calls) - 1]
        else:
            raw = self.responses
        if isinstance(raw, Exception):
            raise raw
        if isinstance(raw, ReviewRecommendation):
            return raw
        if self.repository is None:
            return raw
        return validate_recommendation(self.repository, context, raw)


class OpenRouterReviewAdvisor(KnowledgeReviewAdvisor):
    """Structured, bounded-retry OpenRouter review adapter."""

    max_attempts = 3

    def __init__(
        self,
        repository: Any,
        client: Optional[OpenRouterChatClient] = None,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        self.repository = repository
        self.client = client
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def suggest(self, context: SuggestionContext) -> ReviewRecommendation:
        if self.client is None:
            self.client = OpenRouterChatClient(
                self.api_key,
                self.model or context.request_payload["model"],
                timeout=self.timeout,
            )
        last_error: Optional[SuggestionValidationError] = None
        for _ in range(self.max_attempts):
            content = self.client.complete(
                context.messages, response_format={"type": "json_object"}
            )
            try:
                return validate_recommendation(
                    self.repository, context, decode_recommendation_json(content)
                )
            except SuggestionValidationError as exc:
                last_error = exc
        raise last_error


@dataclass(frozen=True)
class SuggestionBatchResult:
    manifest: Dict[str, Any]
    manifest_path: str

    @property
    def failed(self) -> bool:
        return bool(self.manifest["failures"])


def _timestamp(now_fn: Any) -> str:
    value = now_fn()
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _failure_type(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return "authentication_error"
    if isinstance(exc, ConfigurationError):
        return "configuration_error"
    if isinstance(exc, TransientExtractionError):
        return "provider_error"
    if isinstance(exc, SuggestionValidationError):
        return "validation_error"
    if isinstance(exc, ExtractionError):
        return "provider_error"
    return "repository_error"


def generate_review_suggestions(
    repository: Any,
    reviews: Sequence[Any],
    advisor: KnowledgeReviewAdvisor,
    model: str,
    *,
    context_lines: int = 5,
    force: bool = False,
    filters: Optional[Dict[str, Any]] = None,
    now_fn: Any = utc_now,
) -> SuggestionBatchResult:
    """Generate each review independently and always persist a run manifest."""
    started_at = _timestamp(now_fn)
    created: Dict[str, str] = {}
    reused: Dict[str, str] = {}
    failures = []
    all_pending = repository.load_reviews("pending")
    objects = repository.load_knowledge()
    for item in reviews:
        try:
            context = build_suggestion_context(
                repository,
                item,
                model,
                context_lines=context_lines,
                review_values=all_pending,
                objects=objects,
            )
            if not force:
                matches = [
                    value
                    for value in repository.load_suggestions(item.id)
                    if value.input_fingerprint == context.input_fingerprint
                ]
                if matches:
                    reused[item.id] = matches[-1].id
                    continue
            recommendation = (
                unusable_evidence_recommendation()
                if context.candidate_evidence_unusable
                else advisor.suggest(context)
            )
            recommendation = validate_recommendation(
                repository,
                context,
                (
                    recommendation.to_dict()
                    if isinstance(recommendation, ReviewRecommendation)
                    else recommendation
                ),
            )
            suggestion = create_suggestion(
                repository, context, recommendation, now_fn=now_fn
            )
            repository.write_suggestion(suggestion)
            created[item.id] = suggestion.id
        except Exception as exc:
            failures.append(
                {
                    "review_id": item.id,
                    "error_type": _failure_type(exc),
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )
    succeeded = len(created) + len(reused)
    status = (
        "success"
        if not failures
        else ("partial_failure" if succeeded else "failed")
    )
    completed_at = _timestamp(now_fn)
    compact = re.sub(r"[^0-9]", "", started_at)[:14]
    base_run_id = "review-run-%s" % compact
    run_id = base_run_id
    suffix = 2
    while repository.review_run_path(run_id).exists():
        run_id = "%s-%d" % (base_run_id, suffix)
        suffix += 1
    manifest = {
        "schema_version": "1",
        "run_type": "review_suggestions",
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "filters": dict(filters or {}),
        "requested_review_ids": [item.id for item in reviews],
        "suggestions_created": created,
        "suggestions_reused": reused,
        "failures": failures,
    }
    manifest_path = repository.write_review_run_manifest(manifest)
    return SuggestionBatchResult(manifest, manifest_path)
