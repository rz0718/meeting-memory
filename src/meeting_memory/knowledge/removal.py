"""Audited, atomic removal of canonical knowledge objects."""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import RemovalError
from .indexes import generate_indexes
from .repository import KnowledgeRepository, mutation_locked
from .util import iso_z, json_bytes, run_id, sha256_file, utc_now


@dataclass(frozen=True)
class RemovalResult:
    object_ids: Tuple[str, ...]
    updated_object_ids: Tuple[str, ...]
    updated_review_ids: Tuple[str, ...]
    updated_source_states: Tuple[str, ...]
    changed_paths: Tuple[str, ...]
    index_changed_paths: Tuple[str, ...]
    manifest_path: Optional[str]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_ids": list(self.object_ids),
            "objects_removed": len(self.object_ids),
            "updated_object_ids": list(self.updated_object_ids),
            "updated_review_ids": list(self.updated_review_ids),
            "updated_source_states": list(self.updated_source_states),
            "changed_paths": list(self.changed_paths),
            "index_changed_paths": list(self.index_changed_paths),
            "manifest_path": self.manifest_path,
            "dry_run": self.dry_run,
        }


class KnowledgeRemover:
    """Remove canonical objects while repairing every validated inbound reference."""

    def __init__(self, repository: KnowledgeRepository, now_fn=utc_now):
        self.repository = repository
        self.now_fn = now_fn

    def _now(self) -> dt.datetime:
        value = self.now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    @mutation_locked
    def remove(
        self,
        object_ids: Iterable[str],
        reviewer: str,
        note: str,
        *,
        inventory_sha256: Optional[str] = None,
        dry_run: bool = False,
        refresh_indexes: bool = True,
    ) -> RemovalResult:
        requested = tuple(sorted(set(object_ids)))
        reviewer = reviewer.strip()
        note = note.strip()
        if not requested:
            raise RemovalError("at least one canonical object ID is required")
        if not reviewer:
            raise RemovalError("reviewer may not be empty")
        if not note:
            raise RemovalError("removal note may not be empty")
        # Preflight, as merge does: the sweep after the commit can only report a
        # repository that is already inconsistent for some unrelated reason, and
        # by then the objects are gone -- the caller sees a failure for work that
        # succeeded and cannot tell the two apart. Validating first means a
        # corrupt repository refuses the removal at preview, before any deletion.
        self.repository.validate_all()

        objects = self.repository.load_knowledge()
        by_id = {item.id: item for item in objects}
        missing = sorted(set(requested) - set(by_id))
        if missing:
            raise RemovalError(
                "knowledge object(s) not found: %s" % ", ".join(missing)
            )
        removed = set(requested)
        object_digests = {
            item.id: sha256_file(item.path)
            for item in objects
            if item.path is not None and item.path.is_file()
        }
        reviews = self.repository.load_reviews()
        review_digests = {
            item.id: sha256_file(item.path)
            for item in reviews
            if item.path is not None and item.path.is_file()
        }

        now_text = iso_z(self._now())
        changes: Dict[Path, bytes] = {}
        preconditions: Dict[Path, Optional[str]] = {}
        deletes: List[Path] = []
        removed_records: List[Dict[str, Any]] = []
        for object_id in requested:
            item = by_id[object_id]
            if item.path is None or not item.path.is_file():
                raise RemovalError(
                    "knowledge object has no canonical file: %s" % object_id
                )
            deletes.append(item.path)
            preconditions[item.path] = object_digests[object_id]
            removed_records.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "status": item.status,
                    "path": self.repository._relative(item.path),
                    "sha256": object_digests[object_id],
                    "evidence_sources": sorted(
                        {evidence.source for evidence in item.evidence}
                    ),
                }
            )

        updated_objects: List[str] = []
        for item in objects:
            if item.id in removed or not (set(item.related_objects) & removed):
                continue
            if item.path is None:
                raise RemovalError(
                    "referencing object has no canonical file: %s" % item.id
                )
            updated = copy.deepcopy(item)
            removed_related = sorted(set(updated.related_objects) & removed)
            updated.related_objects = sorted(set(updated.related_objects) - removed)
            updated.updated_at = now_text
            updated.history.append(
                "%s: Removed retired related object reference%s %s during "
                "permanent cleanup by %s. %s"
                % (
                    now_text[:10],
                    "s" if len(removed_related) != 1 else "",
                    ", ".join(removed_related),
                    reviewer,
                    note,
                )
            )
            changes[updated.path] = self.repository.render_knowledge(updated)
            preconditions[updated.path] = object_digests[item.id]
            updated_objects.append(item.id)

        updated_reviews: List[str] = []
        for item in reviews:
            if item.path is None:
                continue
            touches_possible = bool(set(item.possible_existing_ids) & removed)
            touches_affected = bool(set(item.affected_object_ids) & removed)
            if not touches_possible and not touches_affected:
                continue
            updated = copy.deepcopy(item)
            updated.possible_existing_ids = [
                value for value in updated.possible_existing_ids if value not in removed
            ]
            updated.affected_object_ids = [
                value for value in updated.affected_object_ids if value not in removed
            ]
            if touches_possible and updated.status == "pending":
                updated.existing_statement = None
                updated.existing_evidence = []
                updated.existing_updated_at = None
                updated.existing_statement_sha256 = None
                if updated.candidate is not None:
                    updated.candidate.existing_object_id = None
            changes[updated.path] = self.repository.render_review(updated)
            preconditions[updated.path] = review_digests[item.id]
            updated_reviews.append(item.id)

        updated_states: List[str] = []
        for path, raw in self.repository.iter_source_states():
            associated = set(raw["knowledge_object_ids"])
            if not associated & removed:
                continue
            updated = copy.deepcopy(raw)
            updated["knowledge_object_ids"] = sorted(associated - removed)
            changes[path] = json_bytes(updated)
            preconditions[path] = sha256_file(path)
            updated_states.append(self.repository._relative(path))

        manifest_id = "permanent-removal-%s" % run_id(self._now())
        manifest_path = (
            self.repository.state_dir / "cleanup-runs" / ("%s.json" % manifest_id)
        )
        manifest = {
            "cleanup_id": manifest_id,
            "operation": "permanent_canonical_removal",
            "created_at": now_text,
            "reviewer": reviewer,
            "note": note,
            "inventory_sha256": inventory_sha256,
            "objects_removed": removed_records,
            "updated_object_ids": sorted(updated_objects),
            "updated_review_ids": sorted(updated_reviews),
            "updated_source_states": sorted(updated_states),
        }
        changes[manifest_path] = json_bytes(manifest)
        preconditions[manifest_path] = None

        changed_paths = tuple(
            sorted(
                {
                    self.repository._relative(path)
                    for path in list(changes) + deletes
                }
            )
        )
        relative_manifest = self.repository._relative(manifest_path)
        if dry_run:
            return RemovalResult(
                object_ids=requested,
                updated_object_ids=tuple(sorted(updated_objects)),
                updated_review_ids=tuple(sorted(updated_reviews)),
                updated_source_states=tuple(sorted(updated_states)),
                changed_paths=changed_paths,
                index_changed_paths=(),
                manifest_path=relative_manifest,
                dry_run=True,
            )

        self.repository.ensure_layout()
        self.repository.commit(
            changes,
            deletes=deletes,
            preconditions=preconditions,
        )
        index_paths: Tuple[str, ...] = ()
        if refresh_indexes:
            index_paths = generate_indexes(self.repository).changed_paths
        self.repository.validate_all()
        return RemovalResult(
            object_ids=requested,
            updated_object_ids=tuple(sorted(updated_objects)),
            updated_review_ids=tuple(sorted(updated_reviews)),
            updated_source_states=tuple(sorted(updated_states)),
            changed_paths=changed_paths,
            index_changed_paths=index_paths,
            manifest_path=relative_manifest,
            dry_run=False,
        )
