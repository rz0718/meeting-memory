"""Read models for the UI, each a thin wrapper over an existing payload builder.

Nothing in this module writes. Where a value cannot be derived honestly from
what the repository stores -- most notably the prior statement of a refined
object -- the payload says so rather than reconstructing something plausible.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..knowledge.consumption import EvidenceReference
from ..knowledge.errors import EvidenceError, ReviewNotFoundError, SchemaError
from ..knowledge.models import Evidence, KnowledgeObject, ReviewItem, validate_run_manifest
from ..knowledge.presentation import ObjectNotFoundError, read_evidence_excerpt
from ..knowledge.repository import KnowledgeRepository
from ..knowledge.review import review_payload, review_priority
from ..knowledge.review_suggestions import (
    latest_current_suggestion,
    suggestion_display_payload,
)
from ..knowledge.util import EVIDENCE_VERIFIED, parse_frontmatter, sha256_bytes


MANIFEST_BUCKETS = (
    "objects_created",
    "objects_refined",
    "objects_reconfirmed",
    "review_items_created",
)
BUCKET_LABELS = {
    "objects_created": "Created",
    "objects_refined": "Refined",
    "objects_reconfirmed": "Reconfirmed",
    "review_items_created": "Sent to review",
}
# "2026-07-31: Refined by slack-c0194tgl94h."
_HISTORY_ENTRY = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}): (?P<text>.+)$")
# Sources are per-day artifacts, so the calendar day is in the path itself.
_SOURCE_DATE = re.compile(r"^meetings/(?P<date>\d{4}-\d{2}-\d{2})/")
# The only shape from which a prior statement can be read back honestly. No
# writer in this repository emits it today; see docs/review-ui-plan.md §6.
_PRIOR_STATEMENT = re.compile(r'previous statement: "(?P<statement>[^"]+)"', re.I)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def _run_counts(manifest: Dict[str, Any]) -> Dict[str, int]:
    return {
        "sources_examined": len(manifest["sources_examined"]),
        "sources_processed": len(manifest["sources_processed"]),
        "sources_skipped": len(manifest["sources_skipped"]),
        "objects_created": len(manifest["objects_created"]),
        "objects_reconfirmed": len(manifest["objects_reconfirmed"]),
        "objects_refined": len(manifest["objects_refined"]),
        "review_items_created": len(manifest["review_items_created"]),
        "candidates_rejected": len(manifest["candidates_rejected"]),
        # Absent from manifests written before tombstones existed.
        "candidates_suppressed": len(manifest.get("candidates_suppressed", [])),
        # Absent from manifests written before duplicate sources were withheld.
        "sources_deduplicated": len(manifest.get("sources_deduplicated", [])),
        "errors": len(manifest["errors"]),
    }


def _run_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": manifest["run_id"],
        "status": manifest["status"],
        "started_at": manifest["started_at"],
        "completed_at": manifest["completed_at"],
        "target_dates": list(manifest["target_dates"]),
        "counts": _run_counts(manifest),
    }


def iter_run_manifests(repository: KnowledgeRepository) -> Iterable[Dict[str, Any]]:
    directory = repository.state_dir / "runs"
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SchemaError("invalid run manifest %s: %s" % (path, exc)) from exc
        validate_run_manifest(raw)
        yield raw


def _run_date(manifest: Dict[str, Any]) -> Optional[str]:
    return manifest["started_at"][:10] if manifest.get("started_at") else None


def runs_payload(
    repository: KnowledgeRepository,
    date: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Run manifests newest first, optionally narrowed to a date or range.

    A run is dated by the day it started, which is the day a reviewer means when
    they ask what last night's run inserted, regardless of the meeting dates it
    happened to target.
    """
    for value, label in ((date, "date"), (start, "start"), (end, "end")):
        if value is not None:
            try:
                dt.date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("%s must use YYYY-MM-DD" % label) from exc
    manifests = list(iter_run_manifests(repository))
    selected = []
    for manifest in manifests:
        run_date = _run_date(manifest)
        if date is not None and run_date != date:
            continue
        if start is not None and (run_date is None or run_date < start):
            continue
        if end is not None and (run_date is None or run_date > end):
            continue
        selected.append(manifest)
    selected.sort(key=lambda value: (value["started_at"], value["run_id"]), reverse=True)
    if limit is not None:
        selected = selected[:limit]
    latest = repository.latest_successful_run()
    return {
        "count": len(selected),
        "latest_successful_run_id": latest["run_id"] if latest else None,
        "runs": [_run_summary(value) for value in selected],
        "dates": sorted(
            {
                value
                for value in (_run_date(manifest) for manifest in manifests)
                if value is not None
            },
            reverse=True,
        ),
    }


def _history_entries(obj: KnowledgeObject) -> List[Dict[str, str]]:
    values = []
    for line in obj.history:
        match = _HISTORY_ENTRY.match(line.strip())
        if match:
            values.append(
                {"date": match.group("date"), "text": match.group("text")}
            )
        else:
            values.append({"date": "", "text": line.strip()})
    return values


def _refinement_diff(
    obj: Optional[KnowledgeObject], run_date: Optional[str]
) -> Dict[str, Any]:
    """Best-effort before/after for a refined object.

    The run manifest records IDs only and the pipeline stores no prior
    statement, so this reads the ``history`` block. When no history entry
    carries the previous text -- the normal case today -- the payload reports
    ``available: false`` and the UI renders "prior text unavailable" instead of
    a fabricated diff.
    """
    if obj is None:
        return {
            "available": False,
            "reason": "the object no longer exists in the repository",
            "entry": None,
            "before": None,
            "after": None,
        }
    entry = None
    for value in reversed(_history_entries(obj)):
        if "refined" not in value["text"].casefold():
            continue
        if run_date is None or value["date"] <= run_date:
            entry = value
            break
    prior = None
    for value in _history_entries(obj):
        match = _PRIOR_STATEMENT.search(value["text"])
        if match:
            prior = match.group("statement")
    if prior is None:
        return {
            "available": False,
            "reason": "the prior statement is not recorded structurally",
            "entry": entry,
            "before": None,
            "after": obj.statement,
        }
    return {
        "available": True,
        "reason": None,
        "entry": entry,
        "before": prior,
        "after": obj.statement,
        "diff": word_diff(prior, obj.statement),
    }


def _describe_source(repository: KnowledgeRepository, source: str) -> Dict[str, Any]:
    """Name a source for display.

    Never raises. A source with no front matter is the ordinary case for meeting
    notes, and a source deleted since the run still has to render -- the same
    posture the reader takes with an unavailable excerpt.
    """
    stem = PurePosixPath(source).stem
    fallback = stem.replace("-", " ").replace("_", " ").strip() or source
    date = _SOURCE_DATE.match(source)
    described = {
        "source": source,
        "label": fallback,
        "date": date.group("date") if date else None,
        "kind": "meeting",
    }
    try:
        text = repository.evidence_path(source).read_text(encoding="utf-8")
        raw, _ = parse_frontmatter(text)
    except (EvidenceError, OSError, UnicodeError, SchemaError):
        return described
    if raw.get("source_type") == "slack":
        described["kind"] = "slack"
        channel = raw.get("slack_channel_id")
        # The collector resolves user names only, so no channel name is stored
        # anywhere in the repository. Showing the ID is honest; inventing a
        # name is not.
        if isinstance(channel, str) and channel.strip():
            described["label"] = "Slack %s" % channel.strip()
        return described
    title = raw.get("title")
    if isinstance(title, str) and title.strip():
        described["label"] = title.strip()
    return described


class _SourceAttribution:
    """Which of a run's processed sources an object came from.

    Every outcome path appends the processing source's evidence before it
    records the bucket (pipeline.py:283,308,338), so an object named by a
    manifest carries evidence from the source that put it there. Evidence is
    appended and never reordered (merge.py:58), so position breaks observed_at
    ties.

    Restricting to the run's own sources matters because a run spans several
    meeting dates: an object touched by an early source may already carry later
    evidence from an earlier run, and unqualified "latest" would file it under a
    source this run never processed.
    """

    def __init__(
        self, repository: KnowledgeRepository, sources_processed: Iterable[str]
    ):
        self._repository = repository
        self._processed = set(sources_processed)
        self._described: Dict[str, Dict[str, Any]] = {}

    def source_for(self, obj: Optional[KnowledgeObject]) -> Optional[str]:
        if obj is None or not obj.evidence:
            return None
        pool = [item for item in obj.evidence if item.source in self._processed]
        if not pool:
            pool = list(obj.evidence)
        latest = max(
            range(len(pool)),
            key=lambda index: (pool[index].observed_at or "", index),
        )
        return pool[latest].source

    def describe(self, source: str) -> Dict[str, Any]:
        if source not in self._described:
            self._described[source] = _describe_source(self._repository, source)
        return self._described[source]


def _object_row(
    obj: Optional[KnowledgeObject],
    object_id: str,
    bucket: str,
    run_date: Optional[str],
    attribution: _SourceAttribution,
) -> Dict[str, Any]:
    row = {
        "id": object_id,
        "bucket": bucket,
        "present": obj is not None,
        "source": attribution.source_for(obj),
        "title": obj.title if obj is not None else None,
        "category": obj.category if obj is not None else None,
        "status": obj.status if obj is not None else None,
        "statement": obj.statement if obj is not None else None,
        "updated_at": obj.updated_at if obj is not None else None,
        "evidence_count": len(obj.evidence) if obj is not None else 0,
    }
    if bucket == "objects_refined":
        row["refinement"] = _refinement_diff(obj, run_date)
    return row


def _review_row(item: Optional[ReviewItem], review_id: str) -> Dict[str, Any]:
    return {
        "id": review_id,
        "bucket": "review_items_created",
        "present": item is not None,
        "title": item.title if item is not None else None,
        "category": item.candidate_category if item is not None else None,
        "status": item.status if item is not None else None,
        "priority": review_priority(item) if item is not None else None,
        "reason": item.reason if item is not None else None,
        "statement": item.candidate_statement if item is not None else None,
    }


def run_detail_payload(
    repository: KnowledgeRepository, run_id: str
) -> Dict[str, Any]:
    manifest = next(
        (
            value
            for value in iter_run_manifests(repository)
            if value["run_id"] == run_id
        ),
        None,
    )
    if manifest is None:
        raise ObjectNotFoundError("run manifest not found: %s" % run_id)
    objects = {value.id: value for value in repository.load_knowledge()}
    reviews = {value.id: value for value in repository.load_reviews()}
    run_date = _run_date(manifest)
    attribution = _SourceAttribution(repository, manifest["sources_processed"])
    groups = []
    for bucket in MANIFEST_BUCKETS:
        if bucket == "review_items_created":
            rows = [
                _review_row(reviews.get(value), value) for value in manifest[bucket]
            ]
        else:
            rows = [
                _object_row(objects.get(value), value, bucket, run_date, attribution)
                for value in manifest[bucket]
            ]
        groups.append(
            {
                "bucket": bucket,
                "label": BUCKET_LABELS[bucket],
                "count": len(rows),
                "rows": rows,
            }
        )
    # Every processed source in manifest order, then any source a fallback
    # attribution reached for. The list pane needs a label for both.
    ordered_sources = list(manifest["sources_processed"])
    extra = {
        row["source"]
        for group in groups
        for row in group["rows"]
        if row.get("source") and row["source"] not in ordered_sources
    }
    ordered_sources.extend(sorted(extra))
    return {
        "summary": _run_summary(manifest),
        "groups": groups,
        "sources": [attribution.describe(value) for value in ordered_sources],
        "sources_processed": list(manifest["sources_processed"]),
        "sources_skipped": list(manifest["sources_skipped"]),
        "sources_deduplicated": list(manifest.get("sources_deduplicated", [])),
        "candidates_rejected": list(manifest["candidates_rejected"]),
        "candidates_suppressed": list(manifest.get("candidates_suppressed", [])),
        "errors": list(manifest["errors"]),
    }


# ---------------------------------------------------------------------------
# Knowledge objects
# ---------------------------------------------------------------------------


def reference_excerpt_payload(
    repository: KnowledgeRepository, reference: EvidenceReference
) -> Dict[str, Any]:
    """One evidence anchor re-read from its source file, with a freshness label.

    Every tab renders evidence through this, so a drifted or unreadable anchor
    looks the same wherever it appears.
    """
    payload = reference.to_dict()
    excerpt = read_evidence_excerpt(repository, reference)
    payload["excerpt"] = excerpt.to_dict()
    payload["freshness_label"] = _freshness_label(excerpt.to_dict())
    return payload


def _excerpt_payload(
    repository: KnowledgeRepository, evidence: Evidence
) -> Dict[str, Any]:
    return reference_excerpt_payload(
        repository, EvidenceReference.from_evidence(evidence)
    )


def _freshness_label(excerpt: Dict[str, Any]) -> Optional[str]:
    if excerpt.get("error"):
        return "unavailable"
    if excerpt.get("stale"):
        return "drifted"
    if excerpt.get("freshness") == EVIDENCE_VERIFIED:
        return "re-verified"
    return "current"


def knowledge_payload(
    repository: KnowledgeRepository, object_id: str
) -> Dict[str, Any]:
    objects = repository.load_knowledge()
    obj = next((value for value in objects if value.id == object_id), None)
    if obj is None:
        raise ObjectNotFoundError("knowledge object not found: %s" % object_id)
    reviews = repository.load_reviews()
    return {
        "id": obj.id,
        "title": obj.title,
        "category": obj.category,
        "status": obj.status,
        "statement": obj.statement,
        "owner": obj.owner,
        "confidence": obj.confidence,
        "effective_date": obj.effective_date,
        "last_confirmed": obj.last_confirmed,
        "created_at": obj.created_at,
        "updated_at": obj.updated_at,
        "related_objects": list(obj.related_objects),
        "history": _history_entries(obj),
        "file_path": (
            repository._relative(obj.path) if obj.path is not None else None
        ),
        "evidence": [_excerpt_payload(repository, value) for value in obj.evidence],
        "pending_review_ids": [
            value.id
            for value in reviews
            if value.status == "pending" and obj.id in value.possible_existing_ids
        ],
    }


def knowledge_index_payload(repository: KnowledgeRepository) -> Dict[str, Any]:
    """Every canonical object as an identity row, for pickers and quick find."""
    return {
        "objects": [
            {
                "id": value.id,
                "title": value.title,
                "category": value.category,
                "status": value.status,
                "statement": value.statement,
                "updated_at": value.updated_at,
            }
            for value in repository.load_knowledge()
        ]
    }


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------


def _words(value: str) -> List[str]:
    return re.findall(r"\s+|\S+", value)


def word_diff(before: Optional[str], after: str) -> Dict[str, List[Dict[str, str]]]:
    """Word-level segments for the existing and candidate columns.

    Returned as two independently renderable sequences so each column can show
    its own text with only its own changes highlighted, rather than a shared
    unified diff a reviewer has to reassemble mentally.
    """
    left_words = _words(before or "")
    right_words = _words(after or "")
    matcher = difflib.SequenceMatcher(None, left_words, right_words, autojunk=False)
    left: List[Dict[str, str]] = []
    right: List[Dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_text = "".join(left_words[i1:i2])
        right_text = "".join(right_words[j1:j2])
        if tag == "equal":
            if left_text:
                left.append({"kind": "same", "text": left_text})
            if right_text:
                right.append({"kind": "same", "text": right_text})
            continue
        if left_text:
            left.append({"kind": "removed", "text": left_text})
        if right_text:
            right.append({"kind": "added", "text": right_text})
    return {"left": left, "right": right}


def unified_statement_diff(before: Optional[str], after: str) -> List[str]:
    return list(
        difflib.unified_diff(
            (before or "").splitlines() or [""],
            (after or "").splitlines(),
            fromfile="existing",
            tofile="candidate",
            lineterm="",
        )
    )


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def canonical_drift(
    item: ReviewItem, objects: Sequence[KnowledgeObject]
) -> Optional[Dict[str, Any]]:
    """Whether the canonical object moved after this review was cut.

    Mirrors ``ReviewResolver._validate_canonical_snapshot`` exactly, read-only,
    so the UI can replace the action panel with the guided refresh instead of
    letting the human fill in a form that the resolver will refuse.
    """
    if item.existing_statement is None:
        return None
    for existing_id in item.possible_existing_ids:
        target = next((value for value in objects if value.id == existing_id), None)
        if target is None:
            continue
        if target.statement != item.existing_statement:
            return {
                "existing_id": existing_id,
                "reason": "canonical statement changed after this review was created",
                "review_statement": item.existing_statement,
                "current_statement": target.statement,
                "diff": word_diff(item.existing_statement, target.statement),
            }
        if (
            item.existing_updated_at is not None
            and target.updated_at != item.existing_updated_at
        ):
            return {
                "existing_id": existing_id,
                "reason": "canonical object changed after this review was created",
                "review_statement": item.existing_statement,
                "current_statement": target.statement,
                "review_updated_at": item.existing_updated_at,
                "current_updated_at": target.updated_at,
                "diff": word_diff(item.existing_statement, target.statement),
            }
        if item.existing_statement_sha256 is not None:
            current = sha256_bytes(target.statement.encode("utf-8"))
            if current != item.existing_statement_sha256:
                return {
                    "existing_id": existing_id,
                    "reason": (
                        "canonical statement fingerprint no longer matches the review"
                    ),
                    "review_statement": item.existing_statement,
                    "current_statement": target.statement,
                    "diff": word_diff(item.existing_statement, target.statement),
                }
    return None


def _target_options(
    item: ReviewItem, objects: Sequence[KnowledgeObject]
) -> List[Dict[str, Any]]:
    by_id = {value.id: value for value in objects}
    return [
        {
            "id": value,
            "title": by_id[value].title if value in by_id else None,
            "category": by_id[value].category if value in by_id else None,
            "statement": by_id[value].statement if value in by_id else None,
            "present": value in by_id,
        }
        for value in item.possible_existing_ids
    ]


def _blocked_states(
    item: ReviewItem,
    payload: Dict[str, Any],
    suggestion: Optional[Dict[str, Any]],
    drift: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    recommendation = suggestion["recommendation"] if suggestion else None
    stale_candidate = any(
        value["excerpt"].get("stale") for value in payload["candidate_evidence"]
    )
    unreadable_candidate = any(
        value["excerpt"].get("error") for value in payload["candidate_evidence"]
    )
    accept_blocked_reason = None
    if suggestion is None:
        accept_blocked_reason = "no current AI suggestion is available"
    elif not suggestion.get("current"):
        accept_blocked_reason = "the suggestion is stale; regenerate it"
    elif recommendation["suggested_action"] is None:
        accept_blocked_reason = "this suggestion requires a human-authored action"
    elif recommendation["requires_human"]:
        accept_blocked_reason = "the suggestion is marked requires_human"
    return {
        "canonical_drift": drift,
        "accept_disabled": accept_blocked_reason is not None,
        "accept_disabled_reason": accept_blocked_reason,
        "requires_human": bool(recommendation["requires_human"])
        if recommendation
        else None,
        "suggested_action_is_null": (
            recommendation["suggested_action"] is None if recommendation else None
        ),
        "candidate_evidence_drifted": stale_candidate,
        "candidate_evidence_unreadable": unreadable_candidate,
        "target_choice_required": len(item.possible_existing_ids) > 1,
        "has_possible_duplicates": bool(payload["possible_duplicate_ids"]),
    }


def review_detail_payload(
    repository: KnowledgeRepository,
    review_id: str,
    review_values: Optional[Sequence[ReviewItem]] = None,
) -> Dict[str, Any]:
    reviews = list(review_values if review_values is not None else repository.load_reviews())
    item = next((value for value in reviews if value.id == review_id), None)
    if item is None:
        raise ReviewNotFoundError("review item not found: %s" % review_id)
    payload = review_payload(
        repository, item, include_excerpts=True, review_values=reviews
    )
    for group in ("existing_evidence", "candidate_evidence"):
        for value in payload[group]:
            value["freshness_label"] = _freshness_label(value["excerpt"])
    objects = repository.load_knowledge()
    suggestion = None
    if item.status == "pending":
        current = latest_current_suggestion(repository, item)
        if current is not None:
            suggestion = suggestion_display_payload(repository, current, item)
    all_suggestions = [
        {
            "id": value.id,
            "generated_at": value.generated_at,
            "model": value.model,
            "suggested_action": value.recommendation.suggested_action,
            "confidence": value.recommendation.confidence,
            "current": suggestion is not None and suggestion["id"] == value.id,
        }
        for value in repository.load_suggestions(item.id)
    ]
    drift = canonical_drift(item, objects) if item.status == "pending" else None
    payload["statement_diff"] = word_diff(
        item.existing_statement, item.candidate_statement
    )
    payload["unified_diff"] = unified_statement_diff(
        item.existing_statement, item.candidate_statement
    )
    payload["suggestion"] = suggestion
    payload["suggestions"] = all_suggestions
    payload["target_options"] = _target_options(item, objects)
    payload["duplicate_previews"] = [
        {
            "id": value.id,
            "title": value.title,
            "priority": review_priority(value),
            "reason": value.reason,
            "category": value.candidate_category,
            "candidate_statement": value.candidate_statement,
            "sources": list(value.sources),
        }
        for value in reviews
        if value.id in payload["possible_duplicate_ids"]
    ]
    payload["blocked"] = _blocked_states(item, payload, suggestion, drift)
    return payload


def review_list_payload(
    repository: KnowledgeRepository, values: Sequence[ReviewItem]
) -> Dict[str, Any]:
    """The queue list, with just enough per-row state to render a badge.

    Suggestion currency is read from artifacts already on disk; it never
    triggers a provider call.
    """
    counts = {"conflict": 0, "linked": 0, "unlinked": 0}
    rows = []
    for item in values:
        priority = review_priority(item)
        if item.status == "pending":
            counts[priority] += 1
        suggestion = (
            latest_current_suggestion(repository, item)
            if item.status == "pending"
            else None
        )
        rows.append(
            {
                "id": item.id,
                "created_at": item.created_at,
                "status": item.status,
                "priority": priority,
                "reason": item.reason,
                "category": item.candidate_category,
                "title": item.title,
                "possible_existing_ids": list(item.possible_existing_ids),
                "sources": list(item.sources),
                "suggestion": (
                    {
                        "id": suggestion.id,
                        "suggested_action": suggestion.recommendation.suggested_action,
                        "confidence": suggestion.recommendation.confidence,
                        "requires_human": suggestion.recommendation.requires_human,
                    }
                    if suggestion is not None
                    else None
                ),
                "resolution": (
                    {
                        "action": item.resolution_action,
                        "reviewer": item.reviewer,
                        "resolved_at": item.resolved_at,
                        "suggestion_disposition": item.suggestion_disposition,
                        "resolution_mode": item.resolution_mode,
                    }
                    if item.resolution_action
                    else None
                ),
            }
        )
    return {
        "count": len(rows),
        "counts_by_priority": counts,
        "reviews": rows,
    }
