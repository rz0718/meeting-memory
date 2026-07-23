"""Incremental durable-knowledge processing pipeline."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import CATEGORIES, SCHEMA_VERSION
from .errors import EvidenceError, KnowledgeError, SchemaError, StorageError
from .extractors import KnowledgeExtractor
from .models import Evidence, KnowledgeCandidate, KnowledgeObject, ReviewItem
from .reconcile import KnowledgeReconciler, knowledge_id
from .repository import KnowledgeRepository
from .util import iso_z, json_bytes, local_today, normalize_text, run_id, sha256_file, slugify, utc_now


@dataclass
class PipelineResult:
    manifest: Dict[str, Any]
    manifest_path: Optional[Path]
    wrote_files: List[str]
    dry_run: bool

    @property
    def exit_code(self) -> int:
        return 0 if self.manifest["status"] == "success" else 1


class KnowledgePipeline:
    def __init__(
        self,
        repository: KnowledgeRepository,
        extractor: KnowledgeExtractor,
        reconciler: Optional[KnowledgeReconciler] = None,
        schema_version: str = SCHEMA_VERSION,
        now_fn=utc_now,
    ):
        self.repository = repository
        self.extractor = extractor
        self.reconciler = reconciler or KnowledgeReconciler()
        self.schema_version = schema_version
        self.now_fn = now_fn

    def _now(self) -> dt.datetime:
        value = self.now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def _next_run_identity(self, started: dt.datetime) -> Tuple[str, Path]:
        base = run_id(started)
        candidate = base
        counter = 1
        directory = self.repository.state_dir / "runs"
        while (directory / ("%s.json" % candidate)).exists():
            candidate = "%s-%02d" % (base, counter)
            counter += 1
        return candidate, directory / ("%s.json" % candidate)

    def source_needs_processing(self, source, force: bool = False) -> bool:
        if force:
            return True
        state = self.repository.load_source_state(source.relative_path)
        if state is None:
            return True
        return any(
            (
                state["source_sha256"] != source.sha256,
                state["extractor_version"] != self.extractor.version,
                state["schema_version"] != self.schema_version,
                state["result"] != "success",
            )
        )

    @staticmethod
    def _manifest(run_identity: str, target_dates: Sequence[str], started_at: str) -> Dict[str, Any]:
        return {
            "run_id": run_identity,
            "target_dates": list(target_dates),
            "started_at": started_at,
            "completed_at": started_at,
            "status": "success",
            "sources_examined": [],
            "sources_processed": [],
            "sources_skipped": [],
            "objects_created": [],
            "objects_reconfirmed": [],
            "objects_refined": [],
            "review_items_created": [],
            "candidates_rejected": [],
            "errors": [],
        }

    def _complete_evidence(self, candidate: KnowledgeCandidate, source) -> None:
        seen = set()
        completed = []
        for evidence in candidate.evidence:
            evidence_path = self.repository.evidence_path(evidence.source)
            if not evidence_path.is_file():
                raise EvidenceError("candidate evidence source is missing: %s" % evidence.source)
            evidence.source_sha256 = sha256_file(evidence_path)
            parts = Path(evidence.source).parts
            if len(parts) < 3:
                raise EvidenceError("candidate evidence source has no date directory")
            try:
                dt.date.fromisoformat(parts[1])
            except ValueError as exc:
                raise EvidenceError("candidate evidence source date is invalid") from exc
            evidence.observed_at = parts[1]
            self.repository.validate_evidence(evidence)
            if evidence.key() not in seen:
                seen.add(evidence.key())
                completed.append(evidence)
        candidate.evidence = completed

    @staticmethod
    def _append_evidence(existing: KnowledgeObject, candidate: KnowledgeCandidate) -> int:
        keys = {item.key() for item in existing.evidence}
        added = 0
        for evidence in candidate.evidence:
            if evidence.key() not in keys:
                existing.evidence.append(copy.deepcopy(evidence))
                keys.add(evidence.key())
                added += 1
        return added

    @staticmethod
    def _last_observed(candidate: KnowledgeCandidate) -> Optional[str]:
        values = sorted(item.observed_at for item in candidate.evidence if item.observed_at)
        return values[-1] if values else None

    def _new_object(
        self,
        candidate: KnowledgeCandidate,
        existing: List[KnowledgeObject],
        now_text: str,
    ) -> KnowledgeObject:
        object_id = knowledge_id(candidate, existing)
        observed = self._last_observed(candidate)
        return KnowledgeObject(
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
                "%s: Initially recorded as %s."
                % (observed or now_text[:10], candidate.status)
            ],
            path=self.repository.knowledge_dir / candidate.category / ("%s.md" % object_id),
        )

    def _review_item(
        self,
        candidate: KnowledgeCandidate,
        outcome: str,
        existing: Optional[KnowledgeObject],
        now_text: str,
        source_date: str,
        reason: str,
    ) -> ReviewItem:
        identity = "\0".join(
            (
                candidate.category,
                normalize_text(candidate.title),
                normalize_text(candidate.statement),
                existing.id if existing else "",
                "|".join(sorted(item.source for item in candidate.evidence)),
            )
        )
        suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        item_id = "review-%s-%s-%s" % (slugify(candidate.title, 48), source_date, suffix)
        if outcome == "conflict":
            item_reason = "conflicting_evidence"
            explanation = (
                "The supported candidate conflicts with curated knowledge, but the source "
                "does not safely establish that the existing object was replaced."
            )
            title = "%s conflict" % candidate.title
        else:
            item_reason = "ambiguous_match"
            explanation = (
                "The candidate is supported, but deterministic matching cannot safely "
                "classify it. %s" % reason
            )
            title = "%s review" % candidate.title
        return ReviewItem(
            id=item_id,
            created_at=now_text,
            status="pending",
            reason=item_reason,
            candidate_category=candidate.category,
            possible_existing_ids=[existing.id] if existing else [],
            sources=sorted({item.source for item in candidate.evidence}),
            title=title,
            existing_statement=existing.statement if existing else None,
            candidate_statement=candidate.statement,
            explanation=explanation,
            existing_evidence=copy.deepcopy(existing.evidence if existing else []),
            candidate_evidence=copy.deepcopy(candidate.evidence),
        )

    def _apply_candidate(
        self,
        candidate: KnowledgeCandidate,
        objects: List[KnowledgeObject],
        review_ids: set,
        review_writes: Dict[Path, bytes],
        manifest: Dict[str, Any],
        events_by_date: Dict[str, List[str]],
        changes_by_date: Dict[str, Dict[str, List[str]]],
        source,
        now_text: str,
    ) -> Tuple[List[KnowledgeObject], List[str], List[str], List[str]]:
        decision = self.reconciler.reconcile(candidate, objects)
        changed_object_ids: List[str] = []
        review_item_ids: List[str] = []
        associated_object_ids: List[str] = []
        existing = next(
            (item for item in objects if item.id == decision.existing_id),
            None,
        )
        if decision.outcome == "insufficient_evidence":
            manifest["candidates_rejected"].append(
                {
                    "source": source.relative_path,
                    "title": candidate.title,
                    "reason": decision.reason,
                    "outcome": decision.outcome,
                }
            )
            return objects, changed_object_ids, review_item_ids, associated_object_ids
        if decision.outcome == "new":
            obj = self._new_object(candidate, objects, now_text)
            objects.append(obj)
            changed_object_ids.append(obj.id)
            associated_object_ids.append(obj.id)
            manifest["objects_created"].append(obj.id)
            changes_by_date[source.source_date]["created"].append(obj.id)
            events_by_date[source.source_date].append("Added %s." % obj.title)
            return objects, changed_object_ids, review_item_ids, associated_object_ids
        if decision.outcome == "duplicate":
            if existing is not None:
                associated_object_ids.append(existing.id)
            return objects, changed_object_ids, review_item_ids, associated_object_ids
        if decision.outcome == "reconfirmation":
            if existing is None:
                raise SchemaError("reconfirmation target disappeared")
            added = self._append_evidence(existing, candidate)
            if added:
                observed = self._last_observed(candidate)
                if observed and (existing.last_confirmed is None or observed > existing.last_confirmed):
                    existing.last_confirmed = observed
                existing.updated_at = now_text
                history_date = observed or now_text[:10]
                history_entry = "%s: Reconfirmed by %s." % (
                    history_date,
                    Path(source.relative_path).stem,
                )
                if history_entry not in existing.history:
                    existing.history.append(history_entry)
                changed_object_ids.append(existing.id)
                manifest["objects_reconfirmed"].append(existing.id)
                changes_by_date[source.source_date]["reconfirmed"].append(existing.id)
                events_by_date[source.source_date].append("Reconfirmed %s." % existing.title)
            associated_object_ids.append(existing.id)
            return objects, changed_object_ids, review_item_ids, associated_object_ids
        if decision.outcome == "refinement":
            if existing is None:
                raise SchemaError("refinement target disappeared")
            previous_statement = existing.statement
            self._append_evidence(existing, candidate)
            existing.statement = candidate.statement
            existing.title = candidate.title
            existing.status = candidate.status
            existing.confidence = candidate.confidence
            if candidate.owner is not None:
                existing.owner = candidate.owner
            if candidate.effective_date is not None:
                existing.effective_date = candidate.effective_date
            observed = self._last_observed(candidate)
            if observed and (existing.last_confirmed is None or observed > existing.last_confirmed):
                existing.last_confirmed = observed
            if normalize_text(previous_statement) != normalize_text(existing.statement):
                existing.updated_at = now_text
                history_entry = "%s: Refined by %s." % (
                    observed or now_text[:10],
                    Path(source.relative_path).stem,
                )
                if history_entry not in existing.history:
                    existing.history.append(history_entry)
                changed_object_ids.append(existing.id)
                manifest["objects_refined"].append(existing.id)
                changes_by_date[source.source_date]["refined"].append(existing.id)
                events_by_date[source.source_date].append("Refined %s." % existing.title)
            associated_object_ids.append(existing.id)
            return objects, changed_object_ids, review_item_ids, associated_object_ids
        if decision.outcome in ("conflict", "needs_review"):
            item = self._review_item(
                candidate,
                decision.outcome,
                existing,
                now_text,
                source.source_date,
                decision.reason,
            )
            review_item_ids.append(item.id)
            if existing is not None:
                associated_object_ids.append(existing.id)
            if item.id not in review_ids:
                review_ids.add(item.id)
                path = self.repository.review_dir / "pending" / ("%s.md" % item.id)
                review_writes[path] = self.repository.render_review(item)
                manifest["review_items_created"].append(item.id)
                changes_by_date[source.source_date]["reviews"].append(item.id)
                events_by_date[source.source_date].append("Review required for %s." % candidate.title)
            return objects, changed_object_ids, review_item_ids, associated_object_ids
        raise SchemaError("unsupported reconciliation outcome: %s" % decision.outcome)

    @staticmethod
    def _state(
        source,
        now_text: str,
        extractor_version: str,
        schema_version: str,
        run_identity: str,
        result: str,
        object_ids: Iterable[str],
        review_ids: Iterable[str],
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        raw = {
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_date": source.source_date,
            "processed_at": now_text,
            "extractor_version": extractor_version,
            "schema_version": schema_version,
            "run_id": run_identity,
            "result": result,
            "knowledge_object_ids": sorted(set(object_ids)),
            "review_item_ids": sorted(set(review_ids)),
        }
        if error:
            raw["error"] = error
        return raw

    @staticmethod
    def _report(
        source_date: str,
        manifest: Dict[str, Any],
        events: List[str],
        date_changes: Dict[str, List[str]],
        changed_files: List[str],
    ) -> bytes:
        examined = [item for item in manifest["sources_examined"] if item.startswith("meetings/%s/" % source_date)]
        processed = [item for item in manifest["sources_processed"] if item.startswith("meetings/%s/" % source_date)]
        skipped = [item for item in manifest["sources_skipped"] if item.startswith("meetings/%s/" % source_date)]
        reviews = date_changes["reviews"]
        rejected = [
            item
            for item in manifest["candidates_rejected"]
            if item.get("source", "").startswith("meetings/%s/" % source_date)
        ]
        changed_ids = set(
            date_changes["created"]
            + date_changes["reconfirmed"]
            + date_changes["refined"]
            + date_changes["reviews"]
        )
        knowledge_files = sorted(
            path
            for path in changed_files
            if (path.startswith("knowledge/") or path.startswith("knowledge-review/"))
            and any(item in path for item in changed_ids)
        )
        important = "\n".join("- " + event for event in events) or "- None."
        review_lines = "\n".join("- `%s`" % item for item in reviews) or "- None."
        file_lines = "\n".join("- `%s`" % item for item in knowledge_files) or "- None."
        text = """# Durable Knowledge Update — %s

## Sources

- %d source documents examined
- %d processed
- %d unchanged

## Changes

- %d objects created
- %d objects reconfirmed
- %d objects refined
- %d review items created
- %d candidates rejected

## Important Changes

%s

## Review Required

%s

## Files Changed

%s
""" % (
            source_date,
            len(examined),
            len(processed),
            len(skipped),
            len(date_changes["created"]),
            len(date_changes["reconfirmed"]),
            len(date_changes["refined"]),
            len(reviews),
            len(rejected),
            important,
            review_lines,
            file_lines,
        )
        return text.encode("utf-8")

    def process_dates(
        self,
        target_dates: Sequence[str],
        dry_run: bool = False,
        force: bool = False,
    ) -> PipelineResult:
        dates = sorted(dict.fromkeys(target_dates))
        for value in dates:
            try:
                dt.date.fromisoformat(value)
            except ValueError as exc:
                raise SchemaError("target date must use YYYY-MM-DD: %s" % value) from exc
        started = self._now()
        now_text = iso_z(started)
        identity, manifest_path = self._next_run_identity(started)
        manifest = self._manifest(identity, dates, now_text)
        if not dry_run:
            self.repository.ensure_layout()

        try:
            objects = copy.deepcopy(self.repository.load_knowledge())
            review_ids = self.repository.load_review_ids()
        except Exception as exc:
            manifest["errors"].append({"scope": "repository", "error": str(exc)})
            manifest["status"] = "failed"
            manifest["completed_at"] = iso_z(self._now())
            if not dry_run:
                self.repository.write_manifest(manifest_path, manifest)
            return PipelineResult(manifest, None if dry_run else manifest_path, [], dry_run)

        original_by_id = {item.id: copy.deepcopy(item) for item in objects}
        changed_ids = set()
        review_writes: Dict[Path, bytes] = {}
        state_writes: Dict[Path, bytes] = {}
        successful_sources = 0
        failed_sources = 0
        events_by_date: Dict[str, List[str]] = {value: [] for value in dates}
        changes_by_date: Dict[str, Dict[str, List[str]]] = {
            value: {
                "created": [],
                "reconfirmed": [],
                "refined": [],
                "reviews": [],
            }
            for value in dates
        }
        initial_hashes: Dict[str, str] = {}

        for source_date in dates:
            try:
                sources = self.repository.qualifying_sources(source_date)
            except Exception as exc:
                manifest["errors"].append(
                    {"scope": "discovery", "date": source_date, "error": str(exc)}
                )
                failed_sources += 1
                continue
            for source in sources:
                manifest["sources_examined"].append(source.relative_path)
                initial_hashes[source.relative_path] = source.sha256
                try:
                    needs_processing = self.source_needs_processing(source, force=force)
                except Exception as exc:
                    manifest["errors"].append(
                        {"source": source.relative_path, "error": str(exc)}
                    )
                    failed_sources += 1
                    continue
                if not needs_processing:
                    manifest["sources_skipped"].append(source.relative_path)
                    continue

                source_objects = copy.deepcopy(objects)
                source_review_ids = set(review_ids)
                source_review_writes: Dict[Path, bytes] = {}
                source_changed: List[str] = []
                source_associated: List[str] = []
                source_reviews: List[str] = []
                source_manifest_lengths = {
                    key: len(manifest[key])
                    for key in (
                        "objects_created",
                        "objects_reconfirmed",
                        "objects_refined",
                        "review_items_created",
                        "candidates_rejected",
                    )
                }
                source_event_length = len(events_by_date[source_date])
                source_date_change_lengths = {
                    key: len(value)
                    for key, value in changes_by_date[source_date].items()
                }
                try:
                    candidates = self.extractor.extract(source)
                    if not isinstance(candidates, list) or not all(
                        isinstance(item, KnowledgeCandidate) for item in candidates
                    ):
                        raise SchemaError("extractor returned an invalid candidate collection")
                    for candidate in candidates:
                        if not candidate.evidence:
                            manifest["candidates_rejected"].append(
                                {
                                    "source": source.relative_path,
                                    "title": candidate.title,
                                    "reason": "candidate has no meaningful traceable evidence",
                                    "outcome": "insufficient_evidence",
                                }
                            )
                            continue
                        self._complete_evidence(candidate, source)
                    for candidate in candidates:
                        if not candidate.evidence:
                            continue
                        source_objects, changed, reviews, associated = self._apply_candidate(
                            candidate,
                            source_objects,
                            source_review_ids,
                            source_review_writes,
                            manifest,
                            events_by_date,
                            changes_by_date,
                            source,
                            now_text,
                        )
                        source_changed.extend(changed)
                        source_reviews.extend(reviews)
                        source_associated.extend(associated)
                except Exception as exc:
                    for key, length in source_manifest_lengths.items():
                        del manifest[key][length:]
                    del events_by_date[source_date][source_event_length:]
                    for key, length in source_date_change_lengths.items():
                        del changes_by_date[source_date][key][length:]
                    message = "%s: %s" % (exc.__class__.__name__, exc)
                    manifest["errors"].append(
                        {"source": source.relative_path, "error": message}
                    )
                    failed_sources += 1
                    failed_state = self._state(
                        source,
                        now_text,
                        self.extractor.version,
                        self.schema_version,
                        identity,
                        "failed",
                        [],
                        [],
                        message,
                    )
                    state_writes[self.repository.state_path(source.relative_path)] = json_bytes(failed_state)
                    continue

                objects = source_objects
                review_ids = source_review_ids
                review_writes.update(source_review_writes)
                changed_ids.update(source_changed)
                manifest["sources_processed"].append(source.relative_path)
                successful_sources += 1
                success_state = self._state(
                    source,
                    now_text,
                    self.extractor.version,
                    self.schema_version,
                    identity,
                    "success",
                    source_associated,
                    source_reviews,
                )
                state_writes[self.repository.state_path(source.relative_path)] = json_bytes(success_state)

        canonical_writes: Dict[Path, bytes] = {}
        try:
            ids = [item.id for item in objects]
            if len(ids) != len(set(ids)):
                raise SchemaError("proposed knowledge objects contain duplicate IDs")
            for obj in objects:
                for evidence in obj.evidence:
                    self.repository.validate_evidence(evidence, require_current_hash=False)
                if obj.id in changed_ids:
                    canonical_writes[obj.path or (
                        self.repository.knowledge_dir / obj.category / ("%s.md" % obj.id)
                    )] = self.repository.render_knowledge(obj)
            for relative_path, expected_hash in initial_hashes.items():
                current = sha256_file(self.repository.evidence_path(relative_path))
                if current != expected_hash:
                    raise StorageError("source document changed during processing: %s" % relative_path)
        except Exception as exc:
            manifest["errors"].append({"scope": "change_validation", "error": str(exc)})
            failed_sources = max(failed_sources, 1)
            canonical_writes = {}
            review_writes = {}
            state_writes = {}

        if failed_sources:
            manifest["status"] = "partial_failure" if successful_sources else "failed"
        else:
            manifest["status"] = "success"

        changed_preview = [
            path.resolve().relative_to(self.repository.root).as_posix()
            for path in sorted(
                list(canonical_writes) + list(review_writes),
                key=str,
            )
        ]
        report_writes: Dict[Path, bytes] = {}
        for source_date in dates:
            report_writes[
                self.repository.outputs_dir / ("durable-knowledge-%s.md" % source_date)
            ] = self._report(
                source_date,
                manifest,
                events_by_date[source_date],
                changes_by_date[source_date],
                changed_preview,
            )

        changes: Dict[Path, bytes] = {}
        changes.update(canonical_writes)
        changes.update(review_writes)
        changes.update(state_writes)
        changes.update(report_writes)
        wrote_files: List[str] = []
        if not dry_run:
            try:
                wrote_files = self.repository.commit(changes)
            except Exception as exc:
                manifest["errors"].append({"scope": "atomic_commit", "error": str(exc)})
                manifest["status"] = "failed"
                wrote_files = []

        manifest["completed_at"] = iso_z(self._now())
        if not dry_run:
            self.repository.write_manifest(manifest_path, manifest)
        return PipelineResult(
            manifest=manifest,
            manifest_path=None if dry_run else manifest_path,
            wrote_files=wrote_files,
            dry_run=dry_run,
        )

    def pending_dates(
        self,
        lookback_days: int,
        include_today: bool = False,
        today: Optional[dt.date] = None,
    ) -> List[str]:
        if lookback_days < 1:
            raise SchemaError("lookback-days must be at least 1")
        today = today or local_today()
        earliest = today - dt.timedelta(days=lookback_days)
        result = []
        for value in self.repository.discover_dates():
            date_value = dt.date.fromisoformat(value)
            if date_value > today or (date_value == today and not include_today):
                continue
            if date_value < earliest:
                continue
            sources = self.repository.qualifying_sources(value)
            if any(self.source_needs_processing(source) for source in sources):
                result.append(value)
        return sorted(result)

    def status(
        self,
        lookback_days: int = 30,
        include_today: bool = False,
        today: Optional[dt.date] = None,
    ) -> Dict[str, Any]:
        pending_sources = []
        today = today or local_today()
        earliest = today - dt.timedelta(days=lookback_days)
        for value in self.repository.discover_dates():
            date_value = dt.date.fromisoformat(value)
            if date_value > today or (date_value == today and not include_today) or date_value < earliest:
                continue
            for source in self.repository.qualifying_sources(value):
                if self.source_needs_processing(source):
                    pending_sources.append(source.relative_path)
        failed_sources = [
            raw["source_path"]
            for _, raw in self.repository.iter_source_states()
            if raw["result"] != "success"
        ]
        objects = self.repository.load_knowledge()
        category_counts = {
            category: sum(1 for item in objects if item.category == category)
            for category in CATEGORIES
        }
        pending_review = self.repository.review_dir / "pending"
        return {
            "pending_sources": sorted(pending_sources),
            "failed_sources": sorted(set(failed_sources)),
            "latest_successful_run": self.repository.latest_successful_run(),
            "open_review_item_count": len(list(pending_review.glob("*.md")))
            if pending_review.exists()
            else 0,
            "knowledge_object_count_by_category": category_counts,
        }
