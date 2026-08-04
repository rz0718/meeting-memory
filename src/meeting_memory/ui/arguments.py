"""Request bodies and their translation into exact library-call arguments.

Each builder here produces the same keyword arguments the equivalent CLI
invocation produces in ``cli.py``. Keeping the mapping in one place is what
stops the UI and the CLI from drifting apart, and it is what the route tests
assert against.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..knowledge.constants import (
    CATEGORIES,
    CONFIDENCES,
    RESOLUTION_MODES,
    REVIEW_ACTIONS,
    STATUSES,
)


def _iso_date(value: Optional[str], label: str) -> Optional[str]:
    """Validate exactly as ``cli._iso_date`` does, then re-emit ISO form."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("%s must use YYYY-MM-DD" % label) from exc


def _choice(value: Optional[str], allowed, label: str) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value not in allowed:
        raise ValueError(
            "%s must be one of: %s" % (label, ", ".join(sorted(allowed)))
        )
    return value


class ResolveRequest(BaseModel):
    """The form behind Preview / Apply, one field per ``review resolve`` flag."""

    action: Optional[str] = None
    accept_suggestion: bool = False
    suggestion_id: Optional[str] = None
    reviewer: Optional[str] = None
    note: str = ""
    existing_id: Optional[str] = None
    duplicate_of: Optional[str] = None
    new_id: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    clear_owner: bool = False
    confidence: Optional[str] = None
    effective_date: Optional[str] = None
    clear_effective_date: bool = False
    allow_stale_evidence: bool = False
    resolution_mode: Optional[str] = None
    dry_run: bool = True
    no_index: bool = False


def resolver_arguments(
    request: ResolveRequest, reviewer: str
) -> Dict[str, Any]:
    """Map the form onto ``ReviewResolver.resolve`` keyword arguments.

    Empty strings become ``None`` because an untouched text input and an
    omitted CLI flag must mean the same thing; the resolver's own validation
    then handles every combination, so nothing is pre-screened here beyond the
    types argparse would have enforced.
    """
    return {
        "action": _choice(request.action, REVIEW_ACTIONS, "action"),
        "reviewer": (request.reviewer or reviewer or "").strip(),
        "note": request.note,
        "suggestion_id": (request.suggestion_id or None),
        "accept_suggestion": bool(request.accept_suggestion),
        "existing_id": (request.existing_id or None),
        "duplicate_of": (request.duplicate_of or None),
        "new_id": (request.new_id or None),
        "title": (request.title or None),
        "status": _choice(request.status, STATUSES, "status"),
        "owner": (request.owner or None),
        "clear_owner": bool(request.clear_owner),
        "confidence": _choice(request.confidence, CONFIDENCES, "confidence"),
        "effective_date": _iso_date(request.effective_date, "effective_date"),
        "clear_effective_date": bool(request.clear_effective_date),
        "allow_stale_evidence": bool(request.allow_stale_evidence),
        "resolution_mode": _choice(
            request.resolution_mode, RESOLUTION_MODES, "resolution_mode"
        ),
        "dry_run": bool(request.dry_run),
        "refresh_indexes": not bool(request.no_index),
    }


class RefreshRequest(BaseModel):
    existing_id: Optional[str] = None
    adopt_existing: bool = False
    dry_run: bool = True


def refresher_arguments(request: RefreshRequest) -> Dict[str, Any]:
    return {
        "existing_id": (request.existing_id or None),
        "adopt_existing": bool(request.adopt_existing),
        "dry_run": bool(request.dry_run),
    }


class MergeRequest(BaseModel):
    loser_id: str
    survivor_id: str
    reviewer: Optional[str] = None
    note: str = ""
    statement: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    clear_owner: bool = False
    confidence: Optional[str] = None
    effective_date: Optional[str] = None
    clear_effective_date: bool = False
    allow_cross_category: bool = False
    allow_conflicting_numbers: bool = False
    dry_run: bool = True
    no_index: bool = False


def merge_arguments(request: MergeRequest, reviewer: str) -> Dict[str, Any]:
    return {
        "loser_id": request.loser_id,
        "survivor_id": request.survivor_id,
        "reviewer": (request.reviewer or reviewer or "").strip(),
        "note": request.note,
        "statement": (request.statement or None),
        "title": (request.title or None),
        "status": _choice(request.status, STATUSES, "status"),
        "owner": (request.owner or None),
        "clear_owner": bool(request.clear_owner),
        "confidence": _choice(request.confidence, CONFIDENCES, "confidence"),
        "effective_date": _iso_date(request.effective_date, "effective_date"),
        "clear_effective_date": bool(request.clear_effective_date),
        "allow_cross_category": bool(request.allow_cross_category),
        "allow_conflicting_numbers": bool(request.allow_conflicting_numbers),
        "dry_run": bool(request.dry_run),
        "refresh_indexes": not bool(request.no_index),
    }


class RemovalRequest(BaseModel):
    reviewer: Optional[str] = None
    note: str = ""
    confirm_count: Optional[int] = None
    inventory_sha256: Optional[str] = None
    no_index: bool = False


class SuggestRequest(BaseModel):
    """Filters for suggestion generation, mirroring ``review suggest``."""

    review_ids: Optional[list] = None
    priority: str = "all"
    reason: Optional[str] = None
    category: Optional[str] = None
    existing_id: Optional[str] = None
    source: Optional[str] = None
    limit: Optional[int] = None
    context_lines: int = 5
    force: bool = False

    def filters(self) -> Dict[str, Any]:
        """The manifest ``filters`` block the CLI records for the same request."""
        return {
            key: value
            for key, value in {
                "priority": self.priority,
                "reason": self.reason,
                "category": _choice(self.category, CATEGORIES, "category"),
                "existing_id": self.existing_id,
                "source": self.source,
                "limit": self.limit,
            }.items()
            if value is not None and value != "all"
        }


class AskRequest(BaseModel):
    """The Ask form, one field per ``ask`` context option.

    The bounds match ``cli._positive`` and ``cli._nonnegative`` so a request the
    CLI would reject at parse time is rejected here too, rather than reaching
    ``build_context_packet`` and failing later with a less specific message.
    """

    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1)
    max_chars: int = Field(default=30000, ge=1)
    max_evidence_per_object: int = Field(default=3, ge=0)
    include_review_items: bool = True
    include_manual_notes: bool = False
    include_evidence_excerpts: bool = False


def context_arguments(request: AskRequest) -> Dict[str, Any]:
    """Map the Ask form onto ``build_context_packet`` keyword arguments."""
    return {
        "limit": request.limit,
        "max_chars": request.max_chars,
        "max_evidence_per_object": request.max_evidence_per_object,
        "include_review_items": bool(request.include_review_items),
        "include_manual_notes": bool(request.include_manual_notes),
        "include_evidence_excerpts": bool(request.include_evidence_excerpts),
    }


class SaveAnswerRequest(BaseModel):
    """Save an answer already returned in this session, identified by token."""

    answer_token: str = Field(min_length=1)


class ReviewerRequest(BaseModel):
    reviewer: str = Field(min_length=1)


class BasketItemRequest(BaseModel):
    object_id: str = Field(min_length=1)
