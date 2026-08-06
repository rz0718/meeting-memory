"""Merging two already-canonical knowledge objects into one.

Deterministic reconciliation and human review resolve a *candidate* against
canonical knowledge; neither ever combines two objects that are already
canonical. In practice the same standing fact regularly ends up recorded
twice under different titles (a metric and a policy both stating "the FX P&L
excess threshold is +/-$500K"), and closing that gap has so far meant a
maintainer hand-editing both files, remembering to re-point every review item
and related-object reference that named the object being retired. This module
does that same set of edits atomically, through the repository's normal
commit/precondition machinery, so a merge cannot be half-applied and cannot
silently drop a stale reference the way a manual edit can.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .constants import (
    CONFIDENCES,
    GENERATED_BEGIN,
    GENERATED_END,
    MANUAL_BEGIN,
    MANUAL_END,
    STATUSES,
)
from .errors import MergeError
from .indexes import generate_indexes
from .models import Evidence, KnowledgeObject
from .reconcile import conflicting_measurements
from .repository import KnowledgeRepository, mutation_locked
from .util import iso_z, sha256_file, utc_now


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


def _append_evidence(target: KnowledgeObject, evidence: Iterable[Evidence]) -> int:
    keys = {item.key() for item in target.evidence}
    added = 0
    for item in evidence:
        if item.key() not in keys:
            target.evidence.append(copy.deepcopy(item))
            keys.add(item.key())
            added += 1
    return added


def _latest_date(values: Iterable[Optional[str]]) -> Optional[str]:
    present = sorted(value for value in values if value)
    return present[-1] if present else None


@dataclass(frozen=True)
class MergeResult:
    loser_id: str
    survivor_id: str
    evidence_added: int
    retargeted_review_ids: Tuple[str, ...]
    retargeted_object_ids: Tuple[str, ...]
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]
    changed_paths: Tuple[str, ...]
    index_changed_paths: Tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "loser_id": self.loser_id,
            "survivor_id": self.survivor_id,
            "evidence_added": self.evidence_added,
            "retargeted_review_ids": list(self.retargeted_review_ids),
            "retargeted_object_ids": list(self.retargeted_object_ids),
            "before": self.before,
            "after": self.after,
            "changed_paths": list(self.changed_paths),
            "index_changed_paths": list(self.index_changed_paths),
            "dry_run": self.dry_run,
        }


class KnowledgeMerger:
    """Fold one canonical knowledge object into another, retiring the first."""

    def __init__(self, repository: KnowledgeRepository, now_fn=utc_now):
        self.repository = repository
        self.now_fn = now_fn

    def _now(self) -> dt.datetime:
        value = self.now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

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
            raise MergeError("invalid knowledge status: %s" % status)
        if confidence is not None and confidence not in CONFIDENCES:
            raise MergeError("invalid knowledge confidence: %s" % confidence)
        if clear_owner and owner is not None:
            raise MergeError("--owner and --clear-owner are mutually exclusive")
        if clear_effective_date and effective_date is not None:
            raise MergeError(
                "--effective-date and --clear-effective-date are mutually exclusive"
            )
        if effective_date is not None:
            try:
                dt.date.fromisoformat(effective_date)
            except ValueError as exc:
                raise MergeError("--effective-date must use YYYY-MM-DD") from exc

    @mutation_locked
    def merge(
        self,
        loser_id: str,
        survivor_id: str,
        reviewer: str,
        note: str,
        *,
        statement: Optional[str] = None,
        title: Optional[str] = None,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        clear_owner: bool = False,
        confidence: Optional[str] = None,
        effective_date: Optional[str] = None,
        clear_effective_date: bool = False,
        allow_cross_category: bool = False,
        allow_conflicting_numbers: bool = False,
        dry_run: bool = False,
        refresh_indexes: bool = True,
    ) -> MergeResult:
        reviewer = reviewer.strip()
        note = note.strip()
        if len(note.splitlines()) != 1:
            raise MergeError("merge note must be exactly one line")
        if statement is not None:
            statement = statement.strip()
            if not statement:
                raise MergeError("final statement may not be empty")
            if any(
                marker in statement
                for marker in (
                    GENERATED_BEGIN,
                    GENERATED_END,
                    MANUAL_BEGIN,
                    MANUAL_END,
                )
            ):
                raise MergeError("final statement may not contain a protected Markdown marker")
        if not reviewer:
            raise MergeError("reviewer may not be empty")
        if not note:
            raise MergeError("merge note may not be empty")
        if loser_id == survivor_id:
            raise MergeError("cannot merge an object into itself: %s" % loser_id)
        self._validate_overrides(
            status, confidence, clear_owner, owner, clear_effective_date, effective_date
        )
        self.repository.validate_all()

        objects = self.repository.load_knowledge()
        by_id = {value.id: value for value in objects}
        loser = by_id.get(loser_id)
        if loser is None:
            raise MergeError("knowledge object not found: %s" % loser_id)
        survivor = by_id.get(survivor_id)
        if survivor is None:
            raise MergeError("knowledge object not found: %s" % survivor_id)
        if loser.path is None or survivor.path is None:
            raise MergeError("merge requires both objects to have a repository path")
        if not allow_cross_category and loser.category != survivor.category:
            raise MergeError(
                "%s is %s but %s is %s; pass --allow-cross-category to merge anyway"
                % (loser_id, loser.category, survivor_id, survivor.category)
            )
        if not allow_conflicting_numbers and conflicting_measurements(
            loser.statement, survivor.statement
        ):
            raise MergeError(
                "%s and %s carry different numbers, signs, or units; "
                "pass --allow-conflicting-numbers to merge anyway"
                % (loser_id, survivor_id)
            )

        object_digests = {
            value.id: sha256_file(value.path)
            for value in objects
            if value.path is not None and value.path.is_file()
        }
        all_reviews = self.repository.load_reviews()
        review_digests = {
            value.id: sha256_file(value.path)
            for value in all_reviews
            if value.path is not None and value.path.is_file()
        }

        before = _object_summary(survivor)
        now_text = iso_z(self._now())
        today = now_text[:10]

        added = _append_evidence(survivor, loser.evidence)
        if statement is not None:
            survivor.statement = statement
        if title is not None:
            survivor.title = title
        if status is not None:
            survivor.status = status
        if clear_owner:
            survivor.owner = None
        elif owner is not None:
            survivor.owner = owner
        if confidence is not None:
            survivor.confidence = confidence
        if clear_effective_date:
            survivor.effective_date = None
        elif effective_date is not None:
            survivor.effective_date = effective_date
        survivor.last_confirmed = _latest_date(
            [survivor.last_confirmed, loser.last_confirmed]
        )
        survivor.updated_at = now_text
        survivor.history.append(
            "%s: Merged %s (duplicate object) into this record by %s; %d evidence "
            "entr%s added. %s"
            % (today, loser.id, reviewer, added, "y" if added == 1 else "ies", note)
        )

        changes: Dict[Path, bytes] = {survivor.path: self.repository.render_knowledge(survivor)}
        preconditions: Dict[Path, Optional[str]] = {
            survivor.path: object_digests[survivor.id],
            loser.path: object_digests[loser.id],
        }
        deletes: List[Path] = [loser.path]

        retargeted_objects: List[str] = []
        for value in objects:
            if value.id in (loser.id, survivor.id):
                continue
            if loser.id not in value.related_objects:
                continue
            updated = copy.deepcopy(value)
            updated.related_objects = sorted(
                {
                    (survivor.id if related == loser.id else related)
                    for related in value.related_objects
                }
            )
            updated.updated_at = now_text
            changes[updated.path] = self.repository.render_knowledge(updated)
            preconditions[updated.path] = object_digests[value.id]
            retargeted_objects.append(value.id)

        retargeted_reviews: List[str] = []
        for item in all_reviews:
            if item.path is None:
                continue
            touches_possible = loser.id in item.possible_existing_ids
            touches_affected = loser.id in item.affected_object_ids
            if not touches_possible and not touches_affected:
                continue
            updated_item = copy.deepcopy(item)
            if touches_possible:
                updated_item.possible_existing_ids = [
                    survivor.id if value == loser.id else value
                    for value in item.possible_existing_ids
                ]
            if touches_affected:
                updated_item.affected_object_ids = sorted(
                    {
                        (survivor.id if value == loser.id else value)
                        for value in item.affected_object_ids
                    }
                )
            changes[updated_item.path] = self.repository.render_review(updated_item)
            preconditions[updated_item.path] = review_digests[item.id]
            retargeted_reviews.append(item.id)

        changed_paths = tuple(
            self.repository._relative(path) for path in sorted(changes, key=str)
        ) + tuple(self.repository._relative(path) for path in deletes)
        changed_paths = tuple(sorted(set(changed_paths)))

        if dry_run:
            return MergeResult(
                loser_id=loser.id,
                survivor_id=survivor.id,
                evidence_added=added,
                retargeted_review_ids=tuple(sorted(retargeted_reviews)),
                retargeted_object_ids=tuple(sorted(retargeted_objects)),
                before=before,
                after=_object_summary(survivor),
                changed_paths=changed_paths,
                index_changed_paths=(),
                dry_run=True,
            )

        self.repository.ensure_layout()
        self.repository.commit(changes, deletes=deletes, preconditions=preconditions)
        index_paths: Tuple[str, ...] = ()
        if refresh_indexes:
            index_result = generate_indexes(self.repository)
            index_paths = index_result.changed_paths
        self.repository.validate_all()
        return MergeResult(
            loser_id=loser.id,
            survivor_id=survivor.id,
            evidence_added=added,
            retargeted_review_ids=tuple(sorted(retargeted_reviews)),
            retargeted_object_ids=tuple(sorted(retargeted_objects)),
            before=before,
            after=_object_summary(survivor),
            changed_paths=changed_paths,
            index_changed_paths=index_paths,
            dry_run=False,
        )
