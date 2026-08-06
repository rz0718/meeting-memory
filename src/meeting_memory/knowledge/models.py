"""Validated local models for durable knowledge."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    CATEGORIES,
    CONFIDENCES,
    OUTCOMES,
    REVIEW_ACTIONS,
    REVIEW_STATUSES,
    RESOLUTION_MODES,
    RUN_STATUSES,
    STATUSES,
    SUGGESTION_DISPOSITIONS,
    TOMBSTONE_KINDS,
)
from .errors import SchemaError


_OBJECT_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "title": self.title,
            "statement": self.statement,
            "status": self.status,
            "effective_date": self.effective_date,
            "owner": self.owner,
            "confidence": self.confidence,
            "reason_for_durability": self.reason_for_durability,
            "evidence": [item.to_dict() for item in self.evidence],
            "existing_object_id": self.existing_object_id,
            "relationship": self.relationship,
        }


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
        if not _OBJECT_ID.fullmatch(object_id):
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
    # Set only on ``suppressed``: which retired identity blocked the candidate.
    # Kept separate from existing_id, which always names a live object.
    tombstone_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise SchemaError("invalid reconciliation outcome: %s" % self.outcome)


@dataclass
class Tombstone:
    """A durable record that a canonical ID was retired deliberately.

    Removal and merge both delete a canonical file. Without this record the
    deletion is invisible to reconciliation, which compares candidates only
    against objects on disk and therefore reads a retired fact as new
    knowledge. ``category`` and ``title`` are carried so matching can apply the
    same identity rules used for live objects: an ID alone would miss a
    restatement whose wording drifted enough to slugify differently.
    """

    object_id: str
    kind: str
    category: str
    title: str
    statement: str
    created_at: str
    reviewer: str
    note: str
    redirect_to: Optional[str] = None
    manifest_path: Optional[str] = None
    path: Optional[Path] = None

    @classmethod
    def from_dict(cls, raw: Any, path: Optional[Path] = None) -> "Tombstone":
        if not isinstance(raw, dict):
            raise SchemaError("tombstone must be an object")
        for key in (
            "object_id",
            "kind",
            "category",
            "title",
            "statement",
            "created_at",
            "reviewer",
            "note",
        ):
            if key not in raw:
                raise SchemaError("tombstone is missing %s" % key)
        object_id = _required_string(raw.get("object_id"), "tombstone.object_id")
        if not _OBJECT_ID.fullmatch(object_id):
            raise SchemaError("tombstone.object_id must be lowercase kebab case")
        kind = _required_string(raw.get("kind"), "tombstone.kind")
        if kind not in TOMBSTONE_KINDS:
            raise SchemaError("tombstone.kind is not allowed: %s" % kind)
        category = _required_string(raw.get("category"), "tombstone.category")
        if category not in CATEGORIES:
            raise SchemaError("tombstone.category is not allowed: %s" % category)
        redirect_to = _nullable_string(raw.get("redirect_to"), "tombstone.redirect_to")
        if kind == "merged":
            if redirect_to is None:
                raise SchemaError("a merged tombstone requires redirect_to")
            if not _OBJECT_ID.fullmatch(redirect_to):
                raise SchemaError("tombstone.redirect_to must be lowercase kebab case")
            if redirect_to == object_id:
                raise SchemaError("tombstone.redirect_to may not name itself")
        elif redirect_to is not None:
            raise SchemaError("a removed tombstone may not carry redirect_to")
        return cls(
            object_id=object_id,
            kind=kind,
            category=category,
            title=_required_string(raw.get("title"), "tombstone.title"),
            statement=_required_string(raw.get("statement"), "tombstone.statement"),
            created_at=_timestamp(raw.get("created_at"), "tombstone.created_at"),
            reviewer=_required_string(raw.get("reviewer"), "tombstone.reviewer"),
            note=_required_string(raw.get("note"), "tombstone.note"),
            redirect_to=redirect_to,
            manifest_path=_nullable_string(
                raw.get("manifest_path"), "tombstone.manifest_path"
            ),
            path=path,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "redirect_to": self.redirect_to,
            "category": self.category,
            "title": self.title,
            "statement": self.statement,
            "created_at": self.created_at,
            "reviewer": self.reviewer,
            "note": self.note,
            "manifest_path": self.manifest_path,
        }


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
    candidate: Optional[KnowledgeCandidate] = None
    existing_updated_at: Optional[str] = None
    existing_statement_sha256: Optional[str] = None
    resolved_at: Optional[str] = None
    reviewer: Optional[str] = None
    resolution_action: Optional[str] = None
    resolution_note: Optional[str] = None
    affected_object_ids: List[str] = field(default_factory=list)
    duplicate_of: Optional[str] = None
    allowed_stale_evidence: bool = False
    suggestion_id: Optional[str] = None
    suggested_action: Optional[str] = None
    suggestion_disposition: str = "not_used"
    resolution_mode: str = "human"
    automation_policy: Optional[str] = None
    verifier_suggestion_id: Optional[str] = None
    verifier_model: Optional[str] = None
    path: Optional[Path] = None

    @classmethod
    def from_dict(cls, raw: Any, **body: Any) -> "ReviewItem":
        if not isinstance(raw, dict):
            raise SchemaError("review front matter must be an object")
        status = _required_string(raw.get("status"), "review.status")
        if status not in REVIEW_STATUSES:
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
        candidate = None
        candidate_raw = raw.get("candidate")
        if candidate_raw is not None:
            candidate = KnowledgeCandidate.from_dict(candidate_raw)
            if candidate.category != category:
                raise SchemaError("review candidate category does not match candidate snapshot")

        existing_updated_at = None
        existing_statement_sha256 = None
        existing_snapshot = raw.get("existing_snapshot")
        if existing_snapshot is not None:
            if not isinstance(existing_snapshot, dict):
                raise SchemaError("review.existing_snapshot must be an object")
            existing_updated_at = _timestamp(
                existing_snapshot.get("updated_at"),
                "review.existing_snapshot.updated_at",
            )
            existing_statement_sha256 = _required_string(
                existing_snapshot.get("statement_sha256"),
                "review.existing_snapshot.statement_sha256",
            )
            if not re.fullmatch(r"[0-9a-f]{64}", existing_statement_sha256):
                raise SchemaError(
                    "review.existing_snapshot.statement_sha256 must be a SHA-256 digest"
                )

        resolution = raw.get("resolution")
        resolved_at = None
        reviewer = None
        resolution_action = None
        resolution_note = None
        affected_object_ids: List[str] = []
        duplicate_of = None
        allowed_stale_evidence = False
        suggestion_id = None
        suggested_action = None
        suggestion_disposition = "not_used"
        resolution_mode = "human"
        automation_policy = None
        verifier_suggestion_id = None
        verifier_model = None
        if resolution is not None:
            if not isinstance(resolution, dict):
                raise SchemaError("review.resolution must be an object")
            resolved_at = _timestamp(
                resolution.get("resolved_at"), "review.resolution.resolved_at"
            )
            reviewer = _required_string(
                resolution.get("reviewer"), "review.resolution.reviewer"
            )
            resolution_action = _required_string(
                resolution.get("action"), "review.resolution.action"
            )
            if resolution_action not in REVIEW_ACTIONS:
                raise SchemaError("review.resolution.action is not allowed")
            resolution_note = _required_string(
                resolution.get("note"), "review.resolution.note"
            )
            affected = resolution.get("affected_object_ids", [])
            if not isinstance(affected, list) or not all(
                isinstance(item, str) for item in affected
            ):
                raise SchemaError(
                    "review.resolution.affected_object_ids must be a string list"
                )
            affected_object_ids = list(affected)
            duplicate_of = _nullable_string(
                resolution.get("duplicate_of"), "review.resolution.duplicate_of"
            )
            allowed_stale_evidence = resolution.get(
                "allowed_stale_evidence", False
            )
            if not isinstance(allowed_stale_evidence, bool):
                raise SchemaError(
                    "review.resolution.allowed_stale_evidence must be boolean"
                )
            suggestion_id = _nullable_string(
                resolution.get("suggestion_id"),
                "review.resolution.suggestion_id",
            )
            suggested_action = _nullable_string(
                resolution.get("suggested_action"),
                "review.resolution.suggested_action",
            )
            if suggested_action is not None and suggested_action not in REVIEW_ACTIONS:
                raise SchemaError(
                    "review.resolution.suggested_action is not allowed"
                )
            suggestion_disposition = resolution.get(
                "suggestion_disposition", "not_used"
            )
            if suggestion_disposition not in SUGGESTION_DISPOSITIONS:
                raise SchemaError(
                    "review.resolution.suggestion_disposition is not allowed"
                )
            resolution_mode = resolution.get("resolution_mode", "human")
            if resolution_mode not in RESOLUTION_MODES:
                raise SchemaError("review.resolution.resolution_mode is not allowed")
            automation_policy = _nullable_string(
                resolution.get("automation_policy"),
                "review.resolution.automation_policy",
            )
            verifier_suggestion_id = _nullable_string(
                resolution.get("verifier_suggestion_id"),
                "review.resolution.verifier_suggestion_id",
            )
            verifier_model = _nullable_string(
                resolution.get("verifier_model"),
                "review.resolution.verifier_model",
            )
            if suggestion_id is None:
                # A resolution can be reached by deterministic code rather than
                # by a model, in which case there is no suggestion to disposition
                # yet the decision was still not a human's. "automated" is
                # allowed here so such a record can state that plainly instead of
                # being filed as human judgement.
                if suggestion_disposition != "not_used" or resolution_mode not in (
                    "human",
                    "automated",
                ):
                    raise SchemaError(
                        "resolution without a suggestion must be human or automated, "
                        "with suggestion_disposition not_used"
                    )
                if suggested_action is not None:
                    raise SchemaError(
                        "resolution without a suggestion cannot have suggested_action"
                    )
            else:
                if suggestion_disposition not in ("accepted", "overridden"):
                    raise SchemaError(
                        "resolution with a suggestion must be accepted or overridden"
                    )
                if resolution_mode not in ("hybrid", "automated"):
                    raise SchemaError(
                        "resolution with a suggestion must be hybrid or automated"
                    )
                if (
                    suggestion_disposition == "accepted"
                    and suggested_action != resolution_action
                ):
                    raise SchemaError(
                        "accepted suggestion action must equal the final action"
                    )
            if resolution_mode != "automated" and any(
                value is not None
                for value in (
                    automation_policy,
                    verifier_suggestion_id,
                    verifier_model,
                )
            ):
                raise SchemaError(
                    "automation audit fields require automated resolution mode"
                )
        if status == "pending" and resolution is not None:
            raise SchemaError("pending review item may not contain a resolution")
        if resolution_action in ("keep-existing", "merge-duplicate") and status != "rejected":
            raise SchemaError(
                "keep-existing and merge-duplicate resolutions must be rejected"
            )
        if (
            resolution_action is not None
            and resolution_action not in ("keep-existing", "merge-duplicate")
            and status != "resolved"
        ):
            raise SchemaError("accepted review resolution must have resolved status")

        candidate_statement = _required_string(
            body.get("candidate_statement"), "review.candidate_statement"
        )
        candidate_evidence = list(body.get("candidate_evidence") or [])
        existing_evidence = list(body.get("existing_evidence") or [])
        if candidate is not None:
            if candidate.statement != candidate_statement:
                raise SchemaError(
                    "review candidate snapshot and Markdown statement differ"
                )
            candidate_evidence = list(candidate.evidence)

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
            candidate_statement=candidate_statement,
            explanation=_required_string(body.get("explanation"), "review.explanation"),
            existing_evidence=existing_evidence,
            candidate_evidence=candidate_evidence,
            candidate=candidate,
            existing_updated_at=existing_updated_at,
            existing_statement_sha256=existing_statement_sha256,
            resolved_at=resolved_at,
            reviewer=reviewer,
            resolution_action=resolution_action,
            resolution_note=resolution_note,
            affected_object_ids=affected_object_ids,
            duplicate_of=duplicate_of,
            allowed_stale_evidence=allowed_stale_evidence,
            suggestion_id=suggestion_id,
            suggested_action=suggested_action,
            suggestion_disposition=suggestion_disposition,
            resolution_mode=resolution_mode,
            automation_policy=automation_policy,
            verifier_suggestion_id=verifier_suggestion_id,
            verifier_model=verifier_model,
            path=body.get("path"),
        )

    def to_frontmatter(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "reason": self.reason,
            "candidate_category": self.candidate_category,
            "possible_existing_ids": list(self.possible_existing_ids),
            "sources": list(self.sources),
        }
        if self.candidate is not None:
            result["candidate"] = self.candidate.to_dict()
        if self.existing_updated_at and self.existing_statement_sha256:
            result["existing_snapshot"] = {
                "updated_at": self.existing_updated_at,
                "statement_sha256": self.existing_statement_sha256,
            }
        if self.resolution_action:
            result["resolution"] = {
                "resolved_at": self.resolved_at,
                "reviewer": self.reviewer,
                "action": self.resolution_action,
                "note": self.resolution_note,
                "affected_object_ids": list(self.affected_object_ids),
                "duplicate_of": self.duplicate_of,
                "allowed_stale_evidence": self.allowed_stale_evidence,
                "suggestion_id": self.suggestion_id,
                "suggested_action": self.suggested_action,
                "suggestion_disposition": self.suggestion_disposition,
                "resolution_mode": self.resolution_mode,
                "automation_policy": self.automation_policy,
                "verifier_suggestion_id": self.verifier_suggestion_id,
                "verifier_model": self.verifier_model,
            }
        return result


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
    # Optional rather than required: run manifests written before tombstones
    # existed carry no such bucket and must keep validating, since validate_all
    # reads every manifest ever written.
    if "candidates_suppressed" in raw:
        suppressed = raw["candidates_suppressed"]
        if not isinstance(suppressed, list) or not all(
            isinstance(item, dict) for item in suppressed
        ):
            raise SchemaError("run.candidates_suppressed must contain objects")


def validate_review_run_manifest(raw: Any) -> None:
    """Validate an AI-review run without treating it as an ingestion run."""
    if not isinstance(raw, dict):
        raise SchemaError("review run manifest must be an object")
    required = {
        "schema_version",
        "run_type",
        "run_id",
        "started_at",
        "completed_at",
        "status",
        "model",
        "prompt_version",
        "filters",
        "requested_review_ids",
        "suggestions_created",
        "suggestions_reused",
        "failures",
    }
    if set(raw) != required:
        raise SchemaError("review run manifest has missing or unknown fields")
    if raw["schema_version"] != "1" or raw["run_type"] != "review_suggestions":
        raise SchemaError("review run manifest has an unsupported schema or type")
    _required_string(raw["run_id"], "review_run.run_id")
    _timestamp(raw["started_at"], "review_run.started_at")
    _timestamp(raw["completed_at"], "review_run.completed_at")
    if raw["status"] not in RUN_STATUSES:
        raise SchemaError("review run manifest has invalid status")
    _required_string(raw["model"], "review_run.model")
    _required_string(raw["prompt_version"], "review_run.prompt_version")
    if not isinstance(raw["filters"], dict):
        raise SchemaError("review_run.filters must be an object")
    requested = raw["requested_review_ids"]
    if (
        not isinstance(requested, list)
        or not all(isinstance(item, str) and item for item in requested)
        or len(requested) != len(set(requested))
    ):
        raise SchemaError("review_run.requested_review_ids must be unique strings")
    for key in ("suggestions_created", "suggestions_reused"):
        value = raw[key]
        if not isinstance(value, dict) or not all(
            isinstance(review_id, str)
            and review_id
            and isinstance(suggestion_id, str)
            and suggestion_id
            for review_id, suggestion_id in value.items()
        ):
            raise SchemaError("review_run.%s must map review IDs to suggestion IDs" % key)
    failures = raw["failures"]
    if not isinstance(failures, list):
        raise SchemaError("review_run.failures must be an array")
    failed_ids = []
    for failure in failures:
        if not isinstance(failure, dict) or set(failure) != {
            "review_id",
            "error_type",
            "error",
        }:
            raise SchemaError("review_run.failures entries have invalid fields")
        failed_ids.append(_required_string(failure["review_id"], "failure.review_id"))
        _required_string(failure["error_type"], "failure.error_type")
        _required_string(failure["error"], "failure.error")
    buckets = (
        list(raw["suggestions_created"])
        + list(raw["suggestions_reused"])
        + failed_ids
    )
    if len(buckets) != len(set(buckets)) or set(buckets) != set(requested):
        raise SchemaError(
            "every requested review must appear exactly once in review-run results"
        )
    succeeded = len(raw["suggestions_created"]) + len(raw["suggestions_reused"])
    expected_status = (
        "success"
        if not failures
        else ("partial_failure" if succeeded else "failed")
    )
    if raw["status"] != expected_status:
        raise SchemaError(
            "review run status must be derived from its result buckets"
        )
