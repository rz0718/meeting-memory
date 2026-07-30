"""Human review listing, presentation, and deterministic resolution."""

from __future__ import annotations

import copy
import datetime as dt
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import CONFIDENCES, RESOLUTION_MODES, REVIEW_ACTIONS, STATUSES
from .consumption import EvidenceReference
from .errors import (
    EvidenceError,
    ReviewNotFoundError,
    ReviewResolutionError,
    StaleReviewError,
)
from .indexes import generate_indexes
from .models import Evidence, KnowledgeCandidate, KnowledgeObject, ReviewItem
from .presentation import read_evidence_excerpt
from .reconcile import knowledge_id
from .repository import KnowledgeRepository, mutation_locked
from .util import (
    EVIDENCE_VERIFIED,
    iso_z,
    normalize_text,
    sha256_bytes,
    sha256_file,
    utc_now,
)


def review_priority(item: ReviewItem) -> str:
    if item.reason == "conflicting_evidence":
        return "conflict"
    if item.possible_existing_ids:
        return "linked"
    return "unlinked"


def review_topic(item: ReviewItem) -> str:
    value = item.candidate.title if item.candidate is not None else item.title
    if item.candidate is None:
        for suffix in (" conflict", " review"):
            if value.casefold().endswith(suffix):
                value = value[: -len(suffix)].strip()
                break
    return normalize_text(value)


def source_series(source: str) -> str:
    """The recurring document a dated source file belongs to.

    Sources are stored as `meetings/<date>/<document>.md`, so a synced Slack
    channel produces a new path every day while remaining one continuous
    document. Reducing a source to its series lets restatements of a standing
    fact be recognized across days.
    """
    return Path(source).stem.casefold()


def review_series(item: ReviewItem) -> set:
    return {source_series(value) for value in item.sources}


def reviews_look_duplicate(left: ReviewItem, right: ReviewItem) -> bool:
    """Whether two pending reviews restate the same fact from the same origin.

    Origin is compared by series rather than by exact path. A daily recap
    channel restates a standing threshold under a fresh dated filename every
    day, so requiring an identical source path made the commonest duplicate in
    the queue -- the same threshold reworded tomorrow -- impossible to merge.
    """
    return (
        left.id != right.id
        and left.candidate_category == right.candidate_category
        and set(left.possible_existing_ids) == set(right.possible_existing_ids)
        and bool(review_series(left) & review_series(right))
        and review_topic(left) == review_topic(right)
    )


def possible_duplicate_ids(
    repository: KnowledgeRepository,
    item: ReviewItem,
    values: Optional[Sequence[ReviewItem]] = None,
) -> Tuple[str, ...]:
    reviews = values if values is not None else repository.load_reviews("pending")
    return tuple(
        value.id
        for value in reviews
        if value.status == "pending" and reviews_look_duplicate(item, value)
    )


def _sort_key(item: ReviewItem) -> tuple:
    order = {"conflict": 0, "linked": 1, "unlinked": 2}
    return (
        order[review_priority(item)],
        item.created_at,
        item.title.casefold(),
        item.id,
    )


def exact_review(
    repository: KnowledgeRepository,
    review_id: str,
    values: Optional[Sequence[ReviewItem]] = None,
) -> ReviewItem:
    reviews = values if values is not None else repository.load_reviews()
    matches = [item for item in reviews if item.id == review_id]
    if not matches:
        raise ReviewNotFoundError("review item not found: %s" % review_id)
    return matches[0]


def list_reviews(
    repository: KnowledgeRepository,
    status: Optional[str] = "pending",
    priority: Optional[str] = None,
    reason: Optional[str] = None,
    category: Optional[str] = None,
    existing_id: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[ReviewItem, ...]:
    values = repository.load_reviews(status)
    if priority and priority != "all":
        values = [item for item in values if review_priority(item) == priority]
    if reason:
        values = [item for item in values if item.reason == reason]
    if category:
        values = [item for item in values if item.candidate_category == category]
    if existing_id:
        values = [
            item for item in values if existing_id in item.possible_existing_ids
        ]
    if source:
        folded = source.casefold()
        values = [
            item
            for item in values
            if any(folded in value.casefold() for value in item.sources)
        ]
    values = sorted(values, key=_sort_key)
    if limit is not None:
        values = values[:limit]
    return tuple(values)


def _freshness_label(excerpt: Dict[str, Any]) -> str:
    if excerpt["stale"]:
        return " [DRIFTED]"
    if excerpt.get("freshness") == EVIDENCE_VERIFIED:
        return " [RE-VERIFIED]"
    return ""


def _evidence_payload(
    repository: KnowledgeRepository,
    evidence: Sequence[Evidence],
    include_excerpts: bool,
) -> List[Dict[str, Any]]:
    values = []
    for item in evidence:
        reference = EvidenceReference.from_evidence(item)
        payload = reference.to_dict()
        if include_excerpts:
            payload["excerpt"] = read_evidence_excerpt(
                repository, reference
            ).to_dict()
        values.append(payload)
    return values


def review_payload(
    repository: KnowledgeRepository,
    item: ReviewItem,
    include_excerpts: bool = False,
    review_values: Optional[Sequence[ReviewItem]] = None,
) -> Dict[str, Any]:
    candidate_metadata = None
    if item.candidate is not None:
        candidate_metadata = {
            "title": item.candidate.title,
            "status": item.candidate.status,
            "effective_date": item.candidate.effective_date,
            "owner": item.candidate.owner,
            "confidence": item.candidate.confidence,
            "reason_for_durability": item.candidate.reason_for_durability,
            "existing_object_id": item.candidate.existing_object_id,
            "relationship": item.candidate.relationship,
        }
    return {
        "id": item.id,
        "created_at": item.created_at,
        "status": item.status,
        "priority": review_priority(item),
        "reason": item.reason,
        "category": item.candidate_category,
        "title": item.title,
        "possible_existing_ids": list(item.possible_existing_ids),
        "sources": list(item.sources),
        "existing_statement": item.existing_statement,
        "candidate_statement": item.candidate_statement,
        "explanation": item.explanation,
        "candidate_metadata": candidate_metadata,
        "existing_evidence": _evidence_payload(
            repository, item.existing_evidence, include_excerpts
        ),
        "candidate_evidence": _evidence_payload(
            repository, item.candidate_evidence, include_excerpts
        ),
        "possible_duplicate_ids": list(
            possible_duplicate_ids(repository, item, review_values)
        ),
        "resolution": (
            {
                "resolved_at": item.resolved_at,
                "reviewer": item.reviewer,
                "action": item.resolution_action,
                "note": item.resolution_note,
                "affected_object_ids": list(item.affected_object_ids),
                "duplicate_of": item.duplicate_of,
                "allowed_stale_evidence": item.allowed_stale_evidence,
                "suggestion_id": item.suggestion_id,
                "suggested_action": item.suggested_action,
                "suggestion_disposition": item.suggestion_disposition,
                "resolution_mode": item.resolution_mode,
                "automation_policy": item.automation_policy,
                "verifier_suggestion_id": item.verifier_suggestion_id,
                "verifier_model": item.verifier_model,
            }
            if item.resolution_action
            else None
        ),
        "file_path": (
            repository._relative(item.path) if item.path is not None else None
        ),
    }


def reviews_payload(values: Sequence[ReviewItem]) -> Dict[str, Any]:
    counts = {"conflict": 0, "linked": 0, "unlinked": 0}
    for item in values:
        counts[review_priority(item)] += 1
    return {
        "count": len(values),
        "counts_by_priority": counts,
        "reviews": [
            {
                "id": item.id,
                "created_at": item.created_at,
                "status": item.status,
                "priority": review_priority(item),
                "reason": item.reason,
                "category": item.candidate_category,
                "title": item.title,
                "possible_existing_ids": list(item.possible_existing_ids),
                "sources": list(item.sources),
            }
            for item in values
        ],
    }


def render_review_list(values: Sequence[ReviewItem]) -> str:
    if not values:
        return "No review items matched.\n"
    lines = ["Review items: %d" % len(values), ""]
    for item in values:
        lines.append(
            "[%s] %s — %s"
            % (review_priority(item), item.id, item.title)
        )
        lines.append(
            "  category: %s; existing: %s"
            % (
                item.candidate_category,
                ", ".join(item.possible_existing_ids) or "none",
            )
        )
        lines.append("  sources: %s" % ", ".join(item.sources))
    return "\n".join(lines).rstrip() + "\n"


def _statement_diff(item: ReviewItem) -> List[str]:
    existing = (item.existing_statement or "").splitlines() or [""]
    candidate = item.candidate_statement.splitlines()
    return list(
        difflib.unified_diff(
            existing,
            candidate,
            fromfile="existing",
            tofile="candidate",
            lineterm="",
        )
    )
def render_review_show(
    repository: KnowledgeRepository,
    item: ReviewItem,
    include_excerpts: bool = False,
    review_values: Optional[Sequence[ReviewItem]] = None,
) -> str:
    payload = review_payload(
        repository,
        item,
        include_excerpts,
        review_values=review_values,
    )
    lines = [
        item.title,
        "",
        "Review ID: %s" % item.id,
        "Status: %s" % item.status,
        "Priority: %s" % review_priority(item),
        "Reason: %s" % item.reason,
        "Category: %s" % item.candidate_category,
        "Possible existing objects: %s"
        % (", ".join(item.possible_existing_ids) or "None"),
        "Sources: %s" % ", ".join(item.sources),
        "Possible duplicate reviews: %s"
        % (", ".join(payload["possible_duplicate_ids"]) or "None"),
        "",
        "Existing statement:",
        item.existing_statement or "No single existing statement was identified.",
        "",
        "Candidate statement:",
        item.candidate_statement,
        "",
        "Diff:",
        *(_statement_diff(item) or ["Statements are identical."]),
        "",
        "Why review is required:",
        item.explanation,
    ]
    if item.candidate is not None:
        lines.extend(
            [
                "",
                "Candidate metadata:",
                "  status: %s" % item.candidate.status,
                "  confidence: %s" % item.candidate.confidence,
                "  owner: %s" % (item.candidate.owner or "Unknown"),
                "  effective date: %s"
                % (item.candidate.effective_date or "Unknown"),
            ]
        )
    if include_excerpts:
        lines.extend(["", "Evidence excerpts:"])
        for label in ("existing_evidence", "candidate_evidence"):
            lines.append("  %s:" % label.replace("_", " ").title())
            for value in payload[label]:
                excerpt = value["excerpt"]
                lines.append(
                    "  - %s lines %d-%d%s"
                    % (
                        value["source"],
                        value["line_start"],
                        value["line_end"],
                        _freshness_label(excerpt),
                    )
                )
                if excerpt["error"]:
                    lines.append("    Unavailable: %s" % excerpt["error"])
                else:
                    lines.append(
                        "    " + (excerpt["text"] or "").replace("\n", "\n    ")
                    )
    if item.resolution_action:
        lines.extend(
            [
                "",
                "Resolution:",
                "  action: %s" % item.resolution_action,
                "  reviewer: %s" % item.reviewer,
                "  resolved at: %s" % item.resolved_at,
                "  allowed stale evidence: %s"
                % ("yes" if item.allowed_stale_evidence else "no"),
                "  suggestion ID: %s" % (item.suggestion_id or "None"),
                "  suggested action: %s"
                % (
                    item.suggested_action
                    or ("human_required" if item.suggestion_id else "None")
                ),
                "  suggestion disposition: %s"
                % item.suggestion_disposition,
                "  resolution mode: %s" % item.resolution_mode,
                "  note: %s" % item.resolution_note,
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class ReviewResolutionResult:
    review_id: str
    action: str
    destination_status: str
    affected_object_ids: Tuple[str, ...]
    object_changes: Tuple[Dict[str, Any], ...]
    changed_paths: Tuple[str, ...]
    index_changed_paths: Tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "review_id": self.review_id,
            "action": self.action,
            "destination_status": self.destination_status,
            "affected_object_ids": list(self.affected_object_ids),
            "object_changes": list(self.object_changes),
            "changed_paths": list(self.changed_paths),
            "index_changed_paths": list(self.index_changed_paths),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ReviewRefreshResult:
    review_id: str
    existing_id: str
    previous_statement: Optional[str]
    current_statement: str
    previous_updated_at: Optional[str]
    current_updated_at: str
    suggestions_made_stale: Tuple[str, ...]
    changed_paths: Tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "review_id": self.review_id,
            "existing_id": self.existing_id,
            "previous_statement": self.previous_statement,
            "current_statement": self.current_statement,
            "previous_updated_at": self.previous_updated_at,
            "current_updated_at": self.current_updated_at,
            "suggestions_made_stale": list(self.suggestions_made_stale),
            "changed_paths": list(self.changed_paths),
            "dry_run": self.dry_run,
        }


class ReviewRefresher:
    """Rebase only a pending review's existing-side canonical snapshot."""

    def __init__(self, repository: KnowledgeRepository):
        self.repository = repository

    @mutation_locked
    def refresh(
        self,
        review_id: str,
        *,
        existing_id: Optional[str] = None,
        adopt_existing: bool = False,
        dry_run: bool = False,
    ) -> ReviewRefreshResult:
        reviews = self.repository.load_reviews()
        item = exact_review(self.repository, review_id, reviews)
        if item.status != "pending":
            raise ReviewResolutionError(
                "only pending reviews can be refreshed: %s" % review_id
            )
        if item.path is None:
            raise ReviewResolutionError("review item has no repository path")
        if existing_id is None:
            if len(item.possible_existing_ids) != 1:
                raise ReviewResolutionError(
                    "refresh requires --existing-id unless the review has exactly one possible object"
                )
            existing_id = item.possible_existing_ids[0]
        if existing_id not in item.possible_existing_ids:
            # A case opened with no candidate object shows a reviewer nothing to
            # compare against. Naming its target is new information, not a
            # change of identity, so adoption is allowed only when the review
            # holds no target at all -- an existing linkage is never rewritten.
            if not (adopt_existing and not item.possible_existing_ids):
                raise ReviewResolutionError(
                    "refresh cannot change review identity; existing object is not in the review snapshot"
                )
        target = next(
            (
                value
                for value in self.repository.load_knowledge()
                if value.id == existing_id
            ),
            None,
        )
        if target is None:
            raise ReviewResolutionError(
                "existing knowledge object not found: %s" % existing_id
            )
        if target.category != item.candidate_category:
            raise ReviewResolutionError(
                "existing object category does not match review candidate"
            )

        original_bytes = item.path.read_bytes()
        target_digest = (
            sha256_file(target.path)
            if target.path is not None and target.path.is_file()
            else None
        )
        previous_statement = item.existing_statement
        previous_updated_at = item.existing_updated_at
        refreshed = copy.deepcopy(item)
        if existing_id not in refreshed.possible_existing_ids:
            refreshed.possible_existing_ids = [existing_id]
        refreshed.existing_statement = target.statement
        refreshed.existing_evidence = copy.deepcopy(target.evidence)
        refreshed.existing_updated_at = target.updated_at
        refreshed.existing_statement_sha256 = sha256_bytes(
            target.statement.encode("utf-8")
        )
        rendered = self.repository.render_review(refreshed)
        changed = rendered != original_bytes
        changed_paths = (
            (self.repository._relative(item.path),) if changed else ()
        )
        stale_suggestions = (
            tuple(value.id for value in self.repository.load_suggestions(item.id))
            if changed
            else ()
        )
        result = ReviewRefreshResult(
            review_id=item.id,
            existing_id=target.id,
            previous_statement=previous_statement,
            current_statement=target.statement,
            previous_updated_at=previous_updated_at,
            current_updated_at=target.updated_at,
            suggestions_made_stale=stale_suggestions,
            changed_paths=changed_paths,
            dry_run=dry_run,
        )
        if dry_run or not changed:
            return result
        self.repository.commit(
            {item.path: rendered},
            preconditions={
                item.path: sha256_bytes(original_bytes),
                **({target.path: target_digest} if target.path is not None else {}),
            },
        )
        self.repository.validate_all()
        return result


class ReviewResolver:
    """Apply one explicit human decision without modifying raw evidence."""

    def __init__(self, repository: KnowledgeRepository, now_fn=utc_now):
        self.repository = repository
        self.now_fn = now_fn

    def _now(self) -> dt.datetime:
        value = self.now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    @staticmethod
    def _candidate_title(item: ReviewItem) -> str:
        if item.candidate is not None:
            return item.candidate.title
        for suffix in (" conflict", " review"):
            if item.title.casefold().endswith(suffix):
                return item.title[: -len(suffix)].strip()
        return item.title

    @staticmethod
    def _last_observed(evidence: Sequence[Evidence]) -> Optional[str]:
        values = sorted(item.observed_at for item in evidence if item.observed_at)
        return values[-1] if values else None

    @staticmethod
    def _append_evidence(target: KnowledgeObject, evidence: Iterable[Evidence]) -> int:
        keys = {item.key() for item in target.evidence}
        added = 0
        for item in evidence:
            if item.key() not in keys:
                target.evidence.append(copy.deepcopy(item))
                keys.add(item.key())
                added += 1
        return added

    @staticmethod
    def _object_summary(target: Optional[KnowledgeObject]) -> Optional[Dict[str, Any]]:
        if target is None:
            return None
        return {
            "id": target.id,
            "title": target.title,
            "category": target.category,
            "status": target.status,
            "statement": target.statement,
            "effective_date": target.effective_date,
            "owner": target.owner,
            "confidence": target.confidence,
            "last_confirmed": target.last_confirmed,
            "evidence_count": len(target.evidence),
            "updated_at": target.updated_at,
        }

    def _target_object(
        self,
        item: ReviewItem,
        objects: Sequence[KnowledgeObject],
        explicit_id: Optional[str],
        required: bool,
    ) -> Optional[KnowledgeObject]:
        target_id = explicit_id
        if target_id is None and len(item.possible_existing_ids) == 1:
            target_id = item.possible_existing_ids[0]
        if target_id is None:
            if required:
                raise ReviewResolutionError(
                    "action requires --existing-id because no single existing object is selected"
                )
            return None
        target = next((value for value in objects if value.id == target_id), None)
        if target is None:
            raise ReviewResolutionError(
                "existing knowledge object not found: %s" % target_id
            )
        if target.category != item.candidate_category:
            raise ReviewResolutionError(
                "existing object category does not match review candidate"
            )
        return target

    @staticmethod
    def _validate_canonical_snapshot(
        item: ReviewItem, target: Optional[KnowledgeObject]
    ) -> None:
        if target is None or item.existing_statement is None:
            return
        if target.id not in item.possible_existing_ids:
            # The reviewer explicitly selected a different object for an
            # ambiguous/unlinked case; this review has no snapshot for it.
            return
        if target.statement != item.existing_statement:
            raise StaleReviewError(
                "canonical statement changed after this review was created"
            )
        if (
            item.existing_updated_at is not None
            and target.updated_at != item.existing_updated_at
        ):
            raise StaleReviewError(
                "canonical object changed after this review was created"
            )
        if item.existing_statement_sha256 is not None:
            current = sha256_bytes(target.statement.encode("utf-8"))
            if current != item.existing_statement_sha256:
                raise StaleReviewError(
                    "canonical statement fingerprint no longer matches the review"
                )

    def _validate_candidate_evidence(
        self, item: ReviewItem, allow_stale_evidence: bool
    ) -> None:
        if not item.candidate_evidence:
            raise ReviewResolutionError(
                "candidate has no structured evidence and cannot be promoted"
            )
        for evidence in item.candidate_evidence:
            try:
                self.repository.validate_evidence(
                    evidence, require_current_hash=not allow_stale_evidence
                )
            except EvidenceError as exc:
                raise StaleReviewError(str(exc)) from exc
            if allow_stale_evidence:
                source_path = self.repository.evidence_path(evidence.source)
                try:
                    line_count = len(
                        source_path.read_text(encoding="utf-8").splitlines()
                    )
                except (OSError, UnicodeError) as exc:
                    raise StaleReviewError(
                        "candidate evidence source cannot be read: %s"
                        % evidence.source
                    ) from exc
                if evidence.line_end > line_count:
                    raise StaleReviewError(
                        "candidate evidence lines %d-%d exceed %s (%d lines)"
                        % (
                            evidence.line_start,
                            evidence.line_end,
                            evidence.source,
                            line_count,
                        )
                    )

    @staticmethod
    def _metadata(
        item: ReviewItem,
        existing: Optional[KnowledgeObject],
        title: Optional[str],
        status: Optional[str],
        owner: Optional[str],
        clear_owner: bool,
        confidence: Optional[str],
        effective_date: Optional[str],
        clear_effective_date: bool,
        require_complete: bool,
    ) -> Dict[str, Optional[str]]:
        candidate = item.candidate
        resolved_title = (
            title
            or (candidate.title if candidate is not None else None)
            or (existing.title if existing is not None else None)
        )
        resolved_status = (
            status
            or (candidate.status if candidate is not None else None)
            or (existing.status if existing is not None else None)
        )
        resolved_confidence = (
            confidence
            or (candidate.confidence if candidate is not None else None)
            or (existing.confidence if existing is not None else None)
        )
        if clear_owner:
            resolved_owner = None
        elif owner is not None:
            resolved_owner = owner
        elif candidate is not None and candidate.owner is not None:
            resolved_owner = candidate.owner
        else:
            resolved_owner = existing.owner if existing is not None else None
        if clear_effective_date:
            resolved_effective_date = None
        elif effective_date is not None:
            resolved_effective_date = effective_date
        elif candidate is not None and candidate.effective_date is not None:
            resolved_effective_date = candidate.effective_date
        else:
            resolved_effective_date = (
                existing.effective_date if existing is not None else None
            )
        if require_complete and not resolved_title:
            raise ReviewResolutionError(
                "legacy review requires --title to create a separate object"
            )
        if require_complete and not resolved_status:
            raise ReviewResolutionError(
                "legacy review requires --status to create a separate object"
            )
        if require_complete and not resolved_confidence:
            raise ReviewResolutionError(
                "legacy review requires --confidence to create a separate object"
            )
        return {
            "title": resolved_title,
            "status": resolved_status,
            "owner": resolved_owner,
            "confidence": resolved_confidence,
            "effective_date": resolved_effective_date,
        }

    @staticmethod
    def _validate_overrides(
        status: Optional[str],
        confidence: Optional[str],
        clear_owner: bool,
        owner: Optional[str],
        clear_effective_date: bool,
        effective_date: Optional[str],
    ) -> None:
        if status is not None and status not in STATUSES:
            raise ReviewResolutionError("invalid knowledge status: %s" % status)
        if confidence is not None and confidence not in CONFIDENCES:
            raise ReviewResolutionError(
                "invalid knowledge confidence: %s" % confidence
            )
        if clear_owner and owner is not None:
            raise ReviewResolutionError("--owner and --clear-owner are mutually exclusive")
        if clear_effective_date and effective_date is not None:
            raise ReviewResolutionError(
                "--effective-date and --clear-effective-date are mutually exclusive"
            )
        if effective_date is not None:
            try:
                dt.date.fromisoformat(effective_date)
            except ValueError as exc:
                raise ReviewResolutionError(
                    "--effective-date must use YYYY-MM-DD"
                ) from exc

    @mutation_locked
    def resolve(
        self,
        review_id: str,
        action: Optional[str],
        reviewer: str,
        note: str,
        *,
        suggestion_id: Optional[str] = None,
        accept_suggestion: bool = False,
        existing_id: Optional[str] = None,
        duplicate_of: Optional[str] = None,
        new_id: Optional[str] = None,
        title: Optional[str] = None,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        clear_owner: bool = False,
        confidence: Optional[str] = None,
        effective_date: Optional[str] = None,
        clear_effective_date: bool = False,
        allow_stale_evidence: bool = False,
        resolution_mode: Optional[str] = None,
        dry_run: bool = False,
        refresh_indexes: bool = True,
    ) -> ReviewResolutionResult:
        if accept_suggestion and not suggestion_id:
            raise ReviewResolutionError(
                "--accept-suggestion requires --suggestion-id"
            )
        if accept_suggestion and action is not None:
            raise ReviewResolutionError(
                "--accept-suggestion and an explicit action are mutually exclusive"
            )
        if not accept_suggestion and action is None:
            raise ReviewResolutionError("review resolution requires an action")
        if action is not None and action not in REVIEW_ACTIONS:
            raise ReviewResolutionError("unsupported review action: %s" % action)
        if resolution_mode is not None and resolution_mode not in RESOLUTION_MODES:
            raise ReviewResolutionError(
                "unsupported resolution mode: %s" % resolution_mode
            )
        reviewer = reviewer.strip()
        note = note.strip()
        if not reviewer:
            raise ReviewResolutionError("reviewer may not be empty")
        if not note:
            raise ReviewResolutionError("resolution note may not be empty")
        self._validate_overrides(
            status,
            confidence,
            clear_owner,
            owner,
            clear_effective_date,
            effective_date,
        )
        metadata_overrides = any(
            value is not None
            for value in (title, status, owner, confidence, effective_date, new_id)
        ) or clear_owner or clear_effective_date
        if metadata_overrides and action not in (
            "replace",
            "refine",
            "create-separate",
        ):
            raise ReviewResolutionError(
                "knowledge metadata overrides are not valid for action %s" % action
            )
        if duplicate_of is not None and action != "merge-duplicate":
            raise ReviewResolutionError(
                "--duplicate-of is only valid with merge-duplicate"
            )
        if new_id is not None and action != "create-separate":
            raise ReviewResolutionError(
                "--new-id is only valid with create-separate"
            )
        if action == "merge-duplicate" and existing_id is not None:
            raise ReviewResolutionError(
                "--existing-id is not valid with merge-duplicate"
            )
        if action == "create-separate" and existing_id is not None:
            raise ReviewResolutionError(
                "--existing-id is not valid with create-separate"
            )
        if allow_stale_evidence and action not in (
            "replace",
            "refine",
            "reconfirm",
            "create-separate",
        ):
            raise ReviewResolutionError(
                "--allow-stale-evidence is only valid when promoting candidate evidence"
            )

        all_reviews = self.repository.load_reviews()
        item = exact_review(self.repository, review_id, all_reviews)
        if item.status != "pending":
            raise ReviewResolutionError(
                "review item is already %s: %s" % (item.status, review_id)
            )
        if item.path is None:
            raise ReviewResolutionError("review item has no repository path")

        objects = self.repository.load_knowledge()
        review_digests = {
            value.id: sha256_file(value.path)
            for value in all_reviews
            if value.path is not None and value.path.is_file()
        }
        object_digests = {
            value.id: sha256_file(value.path)
            for value in objects
            if value.path is not None and value.path.is_file()
        }
        evidence_paths: Dict[str, Path] = {}
        evidence_digests: Dict[str, Optional[str]] = {}
        for evidence in item.existing_evidence + item.candidate_evidence:
            if evidence.source in evidence_paths:
                continue
            path = self.repository.evidence_path(evidence.source)
            evidence_paths[evidence.source] = path
            evidence_digests[evidence.source] = (
                sha256_file(path) if path.is_file() else None
            )

        suggestion = None
        suggestion_context = None
        suggestion_digest = None
        suggestion_evidence_preconditions: Dict[Path, Optional[str]] = {}
        if suggestion_id is not None:
            from .review_suggestions import (
                build_suggestion_context,
                resolver_arguments_for_proposed_result,
            )

            suggestion = self.repository.load_suggestion(review_id, suggestion_id)
            suggestion_path = self.repository.suggestion_path(
                review_id, suggestion_id
            )
            suggestion_digest = sha256_file(suggestion_path)
            suggestion_context = build_suggestion_context(
                self.repository,
                item,
                suggestion.model,
                context_lines=int(
                    suggestion.request_parameters["context_lines"]
                ),
                review_values=all_reviews,
                objects=objects,
            )
            if (
                suggestion_context.input_fingerprint
                != suggestion.input_fingerprint
                or suggestion_context.review_sha256 != suggestion.review_sha256
                or suggestion_context.canonical_sha256_by_id
                != suggestion.canonical_sha256_by_id
                or suggestion_context.related_review_sha256_by_id
                != suggestion.related_review_sha256_by_id
                or suggestion_context.evidence_sha256_by_source
                != suggestion.evidence_sha256_by_source
                or suggestion_context.prompt_sha256 != suggestion.prompt_sha256
            ):
                raise StaleReviewError(
                    "suggestion inputs changed; generate or select a current suggestion"
                )
            for source, digest in (
                suggestion_context.evidence_sha256_by_source.items()
            ):
                path = self.repository.evidence_path(source)
                if digest == "missing":
                    suggestion_evidence_preconditions[path] = None
                elif len(digest) == 64 and all(
                    value in "0123456789abcdef" for value in digest
                ):
                    suggestion_evidence_preconditions[path] = digest
            if accept_suggestion:
                supplied_overrides = any(
                    value is not None
                    for value in (
                        existing_id,
                        duplicate_of,
                        new_id,
                        title,
                        status,
                        owner,
                        confidence,
                        effective_date,
                    )
                ) or clear_owner or clear_effective_date or allow_stale_evidence
                if supplied_overrides:
                    raise ReviewResolutionError(
                        "accepted suggestions cannot be combined with resolution overrides"
                    )
                recommendation = suggestion.recommendation
                if recommendation.suggested_action is None:
                    raise ReviewResolutionError(
                        "this suggestion requires a human-authored action"
                    )
                action = recommendation.suggested_action
                mapped = resolver_arguments_for_proposed_result(
                    suggestion_context, recommendation
                )
                existing_id = mapped.get("existing_id")
                duplicate_of = mapped.get("duplicate_of")
                new_id = mapped.get("new_id")
                title = mapped.get("title")
                status = mapped.get("status")
                owner = mapped.get("owner")
                clear_owner = bool(mapped.get("clear_owner", False))
                confidence = mapped.get("confidence")
                effective_date = mapped.get("effective_date")
                clear_effective_date = bool(
                    mapped.get("clear_effective_date", False)
                )

        if action is None:  # Narrow Optional[str] after suggestion mapping.
            raise ReviewResolutionError("review resolution requires an action")
        target_required = action in ("replace", "refine", "reconfirm")
        target = self._target_object(item, objects, existing_id, target_required)
        if action in ("replace", "refine", "reconfirm", "keep-existing"):
            self._validate_canonical_snapshot(item, target)

        now_text = iso_z(self._now())
        affected: List[str] = []
        object_changes: List[Dict[str, Any]] = []
        canonical_writes: Dict[Path, bytes] = {}
        destination_status = "resolved"

        if action in ("replace", "refine", "reconfirm", "create-separate"):
            self._validate_candidate_evidence(item, allow_stale_evidence)

        if action in ("replace", "refine"):
            if target is None:
                raise ReviewResolutionError("resolution target disappeared")
            if target.path is None:
                raise ReviewResolutionError(
                    "resolution target has no canonical repository path"
                )
            before = self._object_summary(target)
            metadata = self._metadata(
                item,
                target,
                title,
                status,
                owner,
                clear_owner,
                confidence,
                effective_date,
                clear_effective_date,
                require_complete=False,
            )
            self._append_evidence(target, item.candidate_evidence)
            target.statement = item.candidate_statement
            target.title = str(metadata["title"])
            target.status = str(metadata["status"])
            target.owner = metadata["owner"]
            target.confidence = str(metadata["confidence"])
            target.effective_date = metadata["effective_date"]
            observed = self._last_observed(item.candidate_evidence)
            if observed and (
                target.last_confirmed is None or observed > target.last_confirmed
            ):
                target.last_confirmed = observed
            target.updated_at = now_text
            target.history.append(
                "%s: Human review %s by %s (%s)."
                % (now_text[:10], action, reviewer, review_id)
            )
            canonical_writes[target.path] = self.repository.render_knowledge(target)
            affected.append(target.id)
            object_changes.append(
                {
                    "change": action,
                    "before": before,
                    "after": self._object_summary(target),
                }
            )
        elif action == "reconfirm":
            if target is None:
                raise ReviewResolutionError("resolution target disappeared")
            if target.path is None:
                raise ReviewResolutionError(
                    "resolution target has no canonical repository path"
                )
            before = self._object_summary(target)
            self._append_evidence(target, item.candidate_evidence)
            observed = self._last_observed(item.candidate_evidence)
            if observed and (
                target.last_confirmed is None or observed > target.last_confirmed
            ):
                target.last_confirmed = observed
            target.updated_at = now_text
            target.history.append(
                "%s: Human review reconfirmed by %s (%s)."
                % (now_text[:10], reviewer, review_id)
            )
            canonical_writes[target.path] = self.repository.render_knowledge(target)
            affected.append(target.id)
            object_changes.append(
                {
                    "change": action,
                    "before": before,
                    "after": self._object_summary(target),
                }
            )
        elif action == "create-separate":
            metadata = self._metadata(
                item,
                None,
                title or self._candidate_title(item),
                status,
                owner,
                clear_owner,
                confidence,
                effective_date,
                clear_effective_date,
                require_complete=True,
            )
            candidate = KnowledgeCandidate(
                category=item.candidate_category,
                title=str(metadata["title"]),
                statement=item.candidate_statement,
                status=str(metadata["status"]),
                effective_date=metadata["effective_date"],
                owner=metadata["owner"],
                confidence=str(metadata["confidence"]),
                reason_for_durability=(
                    item.candidate.reason_for_durability
                    if item.candidate is not None
                    else "Promoted by human review."
                ),
                evidence=copy.deepcopy(item.candidate_evidence),
            )
            object_id = new_id or knowledge_id(candidate, objects)
            if any(value.id == object_id for value in objects):
                raise ReviewResolutionError(
                    "new knowledge object ID already exists: %s" % object_id
                )
            observed = self._last_observed(item.candidate_evidence)
            created = KnowledgeObject(
                id=object_id,
                title=candidate.title,
                category=candidate.category,
                status=candidate.status,
                effective_date=candidate.effective_date,
                last_confirmed=observed,
                owner=candidate.owner,
                confidence=candidate.confidence,
                created_at=now_text,
                updated_at=now_text,
                evidence=copy.deepcopy(candidate.evidence),
                related_objects=[],
                statement=candidate.statement,
                history=[
                    "%s: Created by human review %s by %s."
                    % (now_text[:10], review_id, reviewer)
                ],
                path=(
                    self.repository.knowledge_dir
                    / candidate.category
                    / ("%s.md" % object_id)
                ),
            )
            # Validate ID, metadata, and evidence through the canonical model.
            KnowledgeObject.from_dict(created.to_frontmatter())
            canonical_writes[created.path] = self.repository.render_knowledge(created)
            affected.append(created.id)
            object_changes.append(
                {
                    "change": action,
                    "before": None,
                    "after": self._object_summary(created),
                }
            )
        elif action == "keep-existing":
            destination_status = "rejected"
            if target is not None:
                affected.append(target.id)
        elif action == "merge-duplicate":
            destination_status = "rejected"
            if not duplicate_of:
                raise ReviewResolutionError(
                    "merge-duplicate requires --duplicate-of"
                )
            if duplicate_of == review_id:
                raise ReviewResolutionError("a review item cannot duplicate itself")
            other = exact_review(self.repository, duplicate_of, all_reviews)
            if other.status != "pending":
                raise ReviewResolutionError(
                    "duplicate target must still be pending: %s" % duplicate_of
                )
            if not reviews_look_duplicate(item, other):
                raise ReviewResolutionError(
                    "reviews do not share the same category, target, source series, and topic"
                )

        resolved = copy.deepcopy(item)
        resolved.status = destination_status
        resolved.resolved_at = now_text
        resolved.reviewer = reviewer
        resolved.resolution_action = action
        resolved.resolution_note = note
        resolved.affected_object_ids = sorted(set(affected))
        resolved.duplicate_of = duplicate_of if action == "merge-duplicate" else None
        resolved.allowed_stale_evidence = allow_stale_evidence
        resolved.suggestion_id = suggestion.id if suggestion is not None else None
        resolved.suggested_action = (
            suggestion.recommendation.suggested_action
            if suggestion is not None
            else None
        )
        resolved.suggestion_disposition = (
            "accepted" if accept_suggestion else "overridden"
        ) if suggestion is not None else "not_used"
        # A caller driving resolution programmatically must be able to say so.
        # Deriving the mode from the presence of a suggestion alone recorded
        # every scripted resolution as "human", which misstates the audit trail
        # on exactly the decisions a reviewer most needs to distinguish.
        resolved.resolution_mode = resolution_mode or (
            "hybrid" if suggestion is not None else "human"
        )
        resolved.path = (
            self.repository.review_dir
            / destination_status
            / ("%s.md" % resolved.id)
        )

        changes = dict(canonical_writes)
        changes[resolved.path] = self.repository.render_review(resolved)
        changed_paths = tuple(
            self.repository._relative(path)
            for path in sorted(
                list(changes) + [item.path],
                key=str,
            )
        )
        if dry_run:
            return ReviewResolutionResult(
                review_id=review_id,
                action=action,
                destination_status=destination_status,
                affected_object_ids=tuple(sorted(set(affected))),
                object_changes=tuple(object_changes),
                changed_paths=changed_paths,
                index_changed_paths=(),
                dry_run=True,
            )

        self.repository.ensure_layout()
        preconditions: Dict[Path, Optional[str]] = {
            item.path: review_digests[item.id],
            resolved.path: None,
        }
        for path in canonical_writes:
            matching = next(
                (
                    value
                    for value in objects
                    if value.path is not None and value.path.resolve() == path.resolve()
                ),
                None,
            )
            preconditions[path] = (
                object_digests[matching.id] if matching is not None else None
            )
        if action in ("replace", "refine", "reconfirm", "create-separate"):
            for source, path in evidence_paths.items():
                preconditions[path] = evidence_digests[source]
        if action == "merge-duplicate" and duplicate_of is not None:
            duplicate = exact_review(self.repository, duplicate_of, all_reviews)
            if duplicate.path is not None:
                preconditions[duplicate.path] = review_digests[duplicate.id]
        if suggestion is not None and suggestion_digest is not None:
            preconditions[
                self.repository.suggestion_path(item.id, suggestion.id)
            ] = suggestion_digest
            if suggestion_context is not None:
                for value in suggestion_context.canonical_objects:
                    if value.path is not None:
                        preconditions[value.path] = object_digests[value.id]
                for value in suggestion_context.related_reviews:
                    if value.path is not None:
                        preconditions[value.path] = review_digests[value.id]
                preconditions.update(suggestion_evidence_preconditions)
        self.repository.commit(
            changes,
            deletes=(item.path,),
            preconditions=preconditions,
        )
        index_paths: Tuple[str, ...] = ()
        if refresh_indexes and canonical_writes:
            index_result = generate_indexes(self.repository)
            index_paths = index_result.changed_paths
        self.repository.validate_all()
        return ReviewResolutionResult(
            review_id=review_id,
            action=action,
            destination_status=destination_status,
            affected_object_ids=tuple(sorted(set(affected))),
            object_changes=tuple(object_changes),
            changed_paths=changed_paths,
            index_changed_paths=index_paths,
            dry_run=False,
        )
