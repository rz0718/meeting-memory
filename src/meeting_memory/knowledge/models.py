"""Validated local models for durable knowledge."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import CATEGORIES, CONFIDENCES, OUTCOMES, RUN_STATUSES, STATUSES
from .errors import SchemaError


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError("%s must be a non-empty string" % field_name)
    return value.strip()


def _nullable_string(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("%s must be a string or null" % field_name)
    return value.strip() or None


def _date(value: Any, field_name: str, nullable: bool = True) -> Optional[str]:
    if isinstance(value, dt.datetime):
        value = value.date().isoformat()
    elif isinstance(value, dt.date):
        value = value.isoformat()
    value = _nullable_string(value, field_name)
    if value is None:
        if nullable:
            return None
        raise SchemaError("%s may not be null" % field_name)
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SchemaError("%s must use YYYY-MM-DD" % field_name) from exc
    return value


def _timestamp(value: Any, field_name: str) -> str:
    if isinstance(value, dt.datetime):
        value = value.isoformat().replace("+00:00", "Z")
    value = _required_string(value, field_name)
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError("%s must be an ISO-8601 timestamp" % field_name) from exc
    return value


@dataclass
class Evidence:
    source: str
    source_sha256: Optional[str]
    anchor: str
    line_start: int
    line_end: int
    observed_at: Optional[str]

    @classmethod
    def from_dict(cls, raw: Any, complete: bool = False) -> "Evidence":
        if not isinstance(raw, dict):
            raise SchemaError("evidence entries must be objects")
        required = ("source", "anchor", "line_start", "line_end")
        if complete:
            required = required + ("source_sha256", "observed_at")
        for key in required:
            if key not in raw:
                raise SchemaError("evidence entry is missing %s" % key)
        source = _required_string(raw.get("source"), "evidence.source")
        source_sha256 = _nullable_string(raw.get("source_sha256"), "evidence.source_sha256")
        anchor = _required_string(raw.get("anchor"), "evidence.anchor")
        line_start = raw.get("line_start")
        line_end = raw.get("line_end")
        if isinstance(line_start, bool) or not isinstance(line_start, int) or line_start < 1:
            raise SchemaError("evidence.line_start must be a positive integer")
        if isinstance(line_end, bool) or not isinstance(line_end, int) or line_end < line_start:
            raise SchemaError("evidence.line_end must be >= line_start")
        observed_at = raw.get("observed_at")
        observed_at = _date(observed_at, "evidence.observed_at") if observed_at is not None else None
        if complete:
            if not source_sha256 or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                raise SchemaError("evidence.source_sha256 must be a SHA-256 hex digest")
            if observed_at is None:
                raise SchemaError("evidence.observed_at is required")
        return cls(source, source_sha256, anchor, line_start, line_end, observed_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_sha256": self.source_sha256,
            "anchor": self.anchor,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "observed_at": self.observed_at,
        }

    def key(self) -> tuple:
        return (
            self.source,
            self.source_sha256,
            self.anchor,
            self.line_start,
            self.line_end,
            self.observed_at,
        )


@dataclass
class MeetingSource:
    path: Path
    relative_path: str
    source_date: str
    sha256: str
    content: str

    @property
    def lines(self) -> List[str]:
        return self.content.splitlines()


@dataclass
class KnowledgeCandidate:
    category: str
    title: str
    statement: str
    status: str
    effective_date: Optional[str]
    owner: Optional[str]
    confidence: str
    reason_for_durability: str
    evidence: List[Evidence]
    existing_object_id: Optional[str] = None
    relationship: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Any) -> "KnowledgeCandidate":
        if not isinstance(raw, dict):
            raise SchemaError("each candidate must be an object")
        for key in (
            "category",
            "title",
            "statement",
            "status",
            "effective_date",
            "owner",
            "confidence",
            "reason_for_durability",
            "evidence",
        ):
            if key not in raw:
                raise SchemaError("candidate is missing %s" % key)
        category = _required_string(raw.get("category"), "candidate.category")
        if category not in CATEGORIES:
            raise SchemaError("candidate.category is not allowed: %s" % category)
        status = _required_string(raw.get("status"), "candidate.status")
        if status not in STATUSES:
            raise SchemaError("candidate.status is not allowed: %s" % status)
        confidence = _required_string(raw.get("confidence"), "candidate.confidence")
        if confidence not in CONFIDENCES:
            raise SchemaError("candidate.confidence is not allowed: %s" % confidence)
        evidence_raw = raw.get("evidence")
        if not isinstance(evidence_raw, list):
            raise SchemaError("candidate.evidence must be a list")
        relationship = _nullable_string(raw.get("relationship"), "candidate.relationship")
        if relationship is not None and relationship not in OUTCOMES:
            raise SchemaError("candidate.relationship is not a reconciliation outcome")
        return cls(
            category=category,
            title=_required_string(raw.get("title"), "candidate.title"),
            statement=_required_string(raw.get("statement"), "candidate.statement"),
            status=status,
            effective_date=_date(raw.get("effective_date"), "candidate.effective_date"),
            owner=_nullable_string(raw.get("owner"), "candidate.owner"),
            confidence=confidence,
            reason_for_durability=_required_string(
                raw.get("reason_for_durability"), "candidate.reason_for_durability"
            ),
            evidence=[Evidence.from_dict(item) for item in evidence_raw],
            existing_object_id=_nullable_string(
                raw.get("existing_object_id"), "candidate.existing_object_id"
            ),
            relationship=relationship,
        )


@dataclass
class KnowledgeObject:
    id: str
    title: str
    category: str
    status: str
    effective_date: Optional[str]
    last_confirmed: Optional[str]
    owner: Optional[str]
    confidence: str
    created_at: str
    updated_at: str
    evidence: List[Evidence]
    related_objects: List[str]
    statement: str
    history: List[str] = field(default_factory=list)
    manual_section: Optional[str] = None
    path: Optional[Path] = None

    @classmethod
    def from_dict(cls, raw: Any, **extra: Any) -> "KnowledgeObject":
        if not isinstance(raw, dict):
            raise SchemaError("knowledge front matter must be an object")
        for key in (
            "id",
            "title",
            "category",
            "status",
            "effective_date",
            "last_confirmed",
            "owner",
            "confidence",
            "created_at",
            "updated_at",
            "evidence",
            "related_objects",
            "statement",
        ):
            if key not in raw:
                raise SchemaError("knowledge object is missing %s" % key)
        category = _required_string(raw.get("category"), "knowledge.category")
        if category not in CATEGORIES:
            raise SchemaError("knowledge.category is not allowed")
        status = _required_string(raw.get("status"), "knowledge.status")
        if status not in STATUSES:
            raise SchemaError("knowledge.status is not allowed")
        confidence = _required_string(raw.get("confidence"), "knowledge.confidence")
        if confidence not in CONFIDENCES:
            raise SchemaError("knowledge.confidence is not allowed")
        evidence_raw = raw.get("evidence")
        if not isinstance(evidence_raw, list) or not evidence_raw:
            raise SchemaError("knowledge.evidence must contain at least one entry")
        related = raw.get("related_objects")
        if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
            raise SchemaError("knowledge.related_objects must be a string list")
        object_id = _required_string(raw.get("id"), "knowledge.id")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", object_id):
            raise SchemaError("knowledge.id must be lowercase kebab case")
        return cls(
            id=object_id,
            title=_required_string(raw.get("title"), "knowledge.title"),
            category=category,
            status=status,
            effective_date=_date(raw.get("effective_date"), "knowledge.effective_date"),
            last_confirmed=_date(raw.get("last_confirmed"), "knowledge.last_confirmed"),
            owner=_nullable_string(raw.get("owner"), "knowledge.owner"),
            confidence=confidence,
            created_at=_timestamp(raw.get("created_at"), "knowledge.created_at"),
            updated_at=_timestamp(raw.get("updated_at"), "knowledge.updated_at"),
            evidence=[Evidence.from_dict(item, complete=True) for item in evidence_raw],
            related_objects=related,
            statement=_required_string(raw.get("statement"), "knowledge.statement"),
            history=list(extra.get("history") or []),
            manual_section=extra.get("manual_section"),
            path=extra.get("path"),
        )

    def to_frontmatter(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "status": self.status,
            "effective_date": self.effective_date,
            "last_confirmed": self.last_confirmed,
            "owner": self.owner,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence": [item.to_dict() for item in self.evidence],
            "related_objects": list(self.related_objects),
            "statement": self.statement,
        }


@dataclass
class ReconciliationDecision:
    outcome: str
    existing_id: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise SchemaError("invalid reconciliation outcome: %s" % self.outcome)


@dataclass
class ReviewItem:
    id: str
    created_at: str
    status: str
    reason: str
    candidate_category: str
    possible_existing_ids: List[str]
    sources: List[str]
    title: str
    existing_statement: Optional[str]
    candidate_statement: str
    explanation: str
    existing_evidence: List[Evidence] = field(default_factory=list)
    candidate_evidence: List[Evidence] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any, **body: Any) -> "ReviewItem":
        if not isinstance(raw, dict):
            raise SchemaError("review front matter must be an object")
        status = _required_string(raw.get("status"), "review.status")
        if status not in ("pending", "resolved", "rejected"):
            raise SchemaError("invalid review status")
        category = _required_string(raw.get("candidate_category"), "review.candidate_category")
        if category not in CATEGORIES:
            raise SchemaError("invalid review candidate category")
        possible = raw.get("possible_existing_ids")
        sources = raw.get("sources")
        if not isinstance(possible, list) or not all(isinstance(item, str) for item in possible):
            raise SchemaError("review.possible_existing_ids must be a string list")
        if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
            raise SchemaError("review.sources must be a non-empty string list")
        return cls(
            id=_required_string(raw.get("id"), "review.id"),
            created_at=_timestamp(raw.get("created_at"), "review.created_at"),
            status=status,
            reason=_required_string(raw.get("reason"), "review.reason"),
            candidate_category=category,
            possible_existing_ids=possible,
            sources=sources,
            title=_required_string(body.get("title"), "review.title"),
            existing_statement=body.get("existing_statement"),
            candidate_statement=_required_string(
                body.get("candidate_statement"), "review.candidate_statement"
            ),
            explanation=_required_string(body.get("explanation"), "review.explanation"),
        )

    def to_frontmatter(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "reason": self.reason,
            "candidate_category": self.candidate_category,
            "possible_existing_ids": list(self.possible_existing_ids),
            "sources": list(self.sources),
        }


def validate_source_state(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise SchemaError("source state must be an object")
    for key in (
        "source_path",
        "source_sha256",
        "source_date",
        "processed_at",
        "extractor_version",
        "schema_version",
        "run_id",
        "result",
        "knowledge_object_ids",
        "review_item_ids",
    ):
        if key not in raw:
            raise SchemaError("source state missing %s" % key)
    _required_string(raw["source_path"], "state.source_path")
    if not re.fullmatch(r"[0-9a-f]{64}", _required_string(raw["source_sha256"], "state.source_sha256")):
        raise SchemaError("state.source_sha256 is invalid")
    _date(raw["source_date"], "state.source_date", nullable=False)
    _timestamp(raw["processed_at"], "state.processed_at")
    _required_string(raw["extractor_version"], "state.extractor_version")
    _required_string(raw["schema_version"], "state.schema_version")
    _required_string(raw["run_id"], "state.run_id")
    if raw["result"] not in ("success", "failed"):
        raise SchemaError("state.result is invalid")
    for key in ("knowledge_object_ids", "review_item_ids"):
        if not isinstance(raw[key], list) or not all(isinstance(item, str) for item in raw[key]):
            raise SchemaError("state.%s must be a string list" % key)


def validate_run_manifest(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise SchemaError("run manifest must be an object")
    required = (
        "run_id",
        "target_dates",
        "started_at",
        "completed_at",
        "status",
        "sources_examined",
        "sources_processed",
        "sources_skipped",
        "objects_created",
        "objects_reconfirmed",
        "objects_refined",
        "review_items_created",
        "candidates_rejected",
        "errors",
    )
    for key in required:
        if key not in raw:
            raise SchemaError("run manifest missing %s" % key)
    if raw["status"] not in RUN_STATUSES:
        raise SchemaError("run manifest has invalid status")
    _required_string(raw["run_id"], "run.run_id")
    _timestamp(raw["started_at"], "run.started_at")
    _timestamp(raw["completed_at"], "run.completed_at")
    for key in required[1:2] + required[5:]:
        if key in ("status",):
            continue
        if not isinstance(raw[key], list):
            raise SchemaError("run.%s must be a list" % key)
    for value in raw["target_dates"]:
        _date(value, "run.target_dates", nullable=False)
    for key in (
        "sources_examined",
        "sources_processed",
        "sources_skipped",
        "objects_created",
        "objects_reconfirmed",
        "objects_refined",
        "review_items_created",
    ):
        if not all(isinstance(item, str) for item in raw[key]):
            raise SchemaError("run.%s must contain strings" % key)
    for key in ("candidates_rejected", "errors"):
        if not all(isinstance(item, dict) for item in raw[key]):
            raise SchemaError("run.%s must contain objects" % key)
