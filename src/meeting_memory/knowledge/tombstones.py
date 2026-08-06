"""Durable records of deliberately retired canonical identities.

Removal and merge both end by deleting a canonical file. Reconciliation
compares candidates only against objects on disk, so a deleted object is
indistinguishable from one that never existed, and the next extraction that
reaches it -- a forced re-run, a version bump, an edited note, or simply a
later meeting restating the fact -- recreates it under the same deterministic
ID. A tombstone is the record that makes the *decision* durable rather than
just its filesystem effect.

Tombstones are written inside the same atomic commit as the deletion they
describe, so neither can land without the other.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import RemovalError, SchemaError
from .models import KnowledgeObject, Tombstone
from .util import iso_z, json_bytes, run_id, sha256_file, utc_now


def tombstone_record(
    obj: KnowledgeObject,
    kind: str,
    created_at: str,
    reviewer: str,
    note: str,
    *,
    redirect_to: Optional[str] = None,
    manifest_path: Optional[str] = None,
) -> Tombstone:
    """Build the tombstone for an object that is about to be deleted."""
    return Tombstone.from_dict(
        {
            "object_id": obj.id,
            "kind": kind,
            "redirect_to": redirect_to,
            "category": obj.category,
            "title": obj.title,
            "statement": obj.statement,
            "created_at": created_at,
            "reviewer": reviewer,
            "note": note,
            "manifest_path": manifest_path,
        }
    )


def stage_tombstone(
    repository,
    tombstone: Tombstone,
    changes: Dict[Path, bytes],
    preconditions: Dict[Path, Optional[str]],
) -> str:
    """Add one tombstone write to a pending transaction.

    An ID can legitimately be retired more than once: removing an object, later
    re-recording the same fact, then removing it again. The precondition is
    therefore the existing digest when a tombstone is already on disk and
    "must not exist" only when it is genuinely new, so a second retirement
    overwrites the first instead of failing the transaction.
    """
    path = repository.tombstone_path(tombstone.object_id)
    changes[path] = json_bytes(tombstone.to_dict())
    preconditions[path] = sha256_file(path) if path.is_file() else None
    return repository._relative(path)


def resolve_survivor(
    object_id: str, by_id: Dict[str, Tombstone]
) -> Tuple[Optional[str], str]:
    """Follow a merge chain to the identity that should receive new evidence.

    Returns ``(survivor_id, terminal_kind)``. ``survivor_id`` is the first ID in
    the chain that is not itself tombstoned -- the live object, if it exists.
    ``terminal_kind`` is ``"merged"`` when the chain ended by leaving the
    tombstone set and ``"removed"`` when it ended on a removal, which means the
    fact has no surviving home and must be suppressed.
    """
    seen = set()
    current = object_id
    while True:
        tombstone = by_id.get(current)
        if tombstone is None:
            return current, "merged"
        if tombstone.kind == "removed":
            return None, "removed"
        if current in seen:
            raise SchemaError("tombstone redirect chain cycles at %s" % current)
        seen.add(current)
        current = tombstone.redirect_to


def validate_tombstones(
    tombstones: Sequence[Tombstone], live_ids: Iterable[str]
) -> None:
    """Check the invariants that make tombstones trustworthy."""
    by_id: Dict[str, Tombstone] = {}
    for tombstone in tombstones:
        if tombstone.object_id in by_id:
            raise SchemaError("duplicate tombstone: %s" % tombstone.object_id)
        by_id[tombstone.object_id] = tombstone
    live = set(live_ids)
    for tombstone in sorted(by_id.values(), key=lambda value: value.object_id):
        if tombstone.object_id in live:
            raise SchemaError(
                "tombstoned object is also live: %s" % tombstone.object_id
            )
        if tombstone.kind != "merged":
            continue
        survivor, terminal = resolve_survivor(tombstone.redirect_to, by_id)
        if terminal == "merged" and survivor not in live:
            raise SchemaError(
                "%s redirects to a missing object: %s"
                % (tombstone.object_id, survivor)
            )


@dataclass(frozen=True)
class BackfillResult:
    created: Tuple[str, ...]
    skipped_live: Tuple[str, ...]
    skipped_existing: Tuple[str, ...]
    skipped_lifted: Tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": list(self.created),
            "skipped_live": list(self.skipped_live),
            "skipped_existing": list(self.skipped_existing),
            "skipped_lifted": list(self.skipped_lifted),
            "dry_run": self.dry_run,
        }


def backfill_tombstones(repository, *, dry_run: bool = False) -> BackfillResult:
    """Reconstruct removal tombstones from existing cleanup manifests.

    Removals performed before tombstones existed left a cleanup manifest
    recording each object's ID, title, category, and reviewer, which is enough
    to rebuild the record. Merges left no such artifact -- only prose in the
    survivor's history -- so a merge performed before this change cannot be
    recovered here and must be re-asserted by hand.

    An ID named by an old manifest may since have been legitimately re-created,
    so anything currently live is skipped rather than retired a second time.
    This is why backfill is an explicit operator action and not a migration
    that runs itself.
    """
    with repository.mutation_lock():
        return _backfill_locked(repository, dry_run)


def _backfill_locked(repository, dry_run: bool) -> BackfillResult:
    # Preflight as removal and merge do, so a repository that is already
    # inconsistent refuses at preview rather than failing the sweep after the
    # write and leaving the operator unable to tell the two apart.
    repository.validate_all()
    directory = repository.state_dir / "cleanup-runs"
    manifests = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            manifests.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as exc:
            raise SchemaError("invalid cleanup manifest %s: %s" % (path, exc)) from exc

    # A lift is a decision too. Its manifest sits in the same directory as the
    # removal it reverses, and the removal manifest is never rewritten, so
    # rebuilding from removals alone would quietly reinstate every tombstone an
    # operator deliberately lifted.
    lifted: Dict[str, str] = {}
    for _, raw in manifests:
        if raw.get("operation") != "tombstone_lift":
            continue
        object_id = (raw.get("lifted") or {}).get("object_id")
        created_at = raw.get("created_at")
        if object_id and created_at and created_at > lifted.get(object_id, ""):
            lifted[object_id] = created_at

    live = {item.id for item in repository.load_knowledge()}
    known = {item.object_id for item in repository.load_tombstones()}
    created: Dict[str, Tombstone] = {}
    skipped_live: List[str] = []
    skipped_existing: List[str] = []
    skipped_lifted: List[str] = []
    for path, raw in manifests:
        if raw.get("operation") != "permanent_canonical_removal":
            continue
        for record in raw.get("objects_removed", []):
            object_id = record.get("id")
            if not object_id:
                continue
            if object_id in live:
                skipped_live.append(object_id)
                continue
            if object_id in known or object_id in created:
                skipped_existing.append(object_id)
                continue
            if lifted.get(object_id, "") > (raw.get("created_at") or ""):
                skipped_lifted.append(object_id)
                continue
            created[object_id] = Tombstone.from_dict(
                {
                    "object_id": object_id,
                    "kind": "removed",
                    "redirect_to": None,
                    "category": record.get("category"),
                    "title": record.get("title"),
                    # The statement was not captured by the cleanup manifest and
                    # the canonical file is gone, so identity rests on the title
                    # alone. Said plainly rather than invented.
                    "statement": "Statement not recorded; reconstructed from %s."
                    % repository._relative(path),
                    "created_at": raw.get("created_at"),
                    "reviewer": raw.get("reviewer"),
                    "note": raw.get("note"),
                    "manifest_path": repository._relative(path),
                }
            )

    # One ID can appear in several manifests -- removed, lifted, removed again.
    # Only the outcome is reported, so an ID that was rebuilt is not also listed
    # as skipped by an earlier manifest that the later one superseded.
    result = BackfillResult(
        created=tuple(sorted(created)),
        skipped_live=tuple(sorted(set(skipped_live) - set(created))),
        skipped_existing=tuple(sorted(set(skipped_existing) - set(created))),
        skipped_lifted=tuple(sorted(set(skipped_lifted) - set(created))),
        dry_run=dry_run,
    )
    if dry_run or not created:
        return result

    repository.ensure_layout()
    changes: Dict[Path, bytes] = {}
    preconditions: Dict[Path, Optional[str]] = {}
    for tombstone in created.values():
        stage_tombstone(repository, tombstone, changes, preconditions)
    repository.commit(changes, preconditions=preconditions)
    repository.validate_all()
    return result


@dataclass(frozen=True)
class LiftResult:
    object_id: str
    kind: str
    redirect_to: Optional[str]
    manifest_path: str
    changed_paths: Tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "redirect_to": self.redirect_to,
            "manifest_path": self.manifest_path,
            "changed_paths": list(self.changed_paths),
            "dry_run": self.dry_run,
        }


def list_tombstones(repository, kind: Optional[str] = None) -> List[Tombstone]:
    values = repository.load_tombstones()
    if kind is not None:
        values = [value for value in values if value.kind == kind]
    return values


def lift_tombstone(
    repository,
    object_id: str,
    reviewer: str,
    note: str,
    *,
    dry_run: bool = False,
    now_fn=utc_now,
) -> LiftResult:
    """Stop a tombstone from blocking, without restoring what it recorded.

    The canonical content is gone and its evidence digests were captured
    against sources that may since have changed, so reconstructing the object
    here would be a fabrication. Lifting only removes the block: the next run
    re-extracts the fact from whatever evidence exists now, or does not, which
    is the honest answer either way.
    """
    reviewer = reviewer.strip()
    note = note.strip()
    if not reviewer:
        raise RemovalError("reviewer may not be empty")
    if not note:
        raise RemovalError("lift note may not be empty")
    with repository.mutation_lock():
        return _lift_locked(repository, object_id, reviewer, note, dry_run, now_fn)


def _lift_locked(repository, object_id, reviewer, note, dry_run, now_fn) -> LiftResult:
    repository.validate_all()

    tombstones = repository.load_tombstones()
    by_id = {value.object_id: value for value in tombstones}
    target = by_id.get(object_id)
    if target is None:
        raise RemovalError("no tombstone found for %s" % object_id)
    # Another tombstone routing through this one would be left pointing at an
    # ID that is neither live nor retired, which validate_all rejects. Refusing
    # here names the blocking records instead of failing after the commit.
    blocked = sorted(
        value.object_id
        for value in tombstones
        if value.kind == "merged" and value.redirect_to == object_id
    )
    if blocked:
        raise RemovalError(
            "cannot lift %s while %s redirect%s through it"
            % (object_id, ", ".join(blocked), "s" if len(blocked) == 1 else "")
        )

    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    now_text = iso_z(now)
    manifest_id = "tombstone-lift-%s" % run_id(now)
    manifest_path = (
        repository.state_dir / "cleanup-runs" / ("%s.json" % manifest_id)
    )
    manifest = {
        "cleanup_id": manifest_id,
        "operation": "tombstone_lift",
        "created_at": now_text,
        "reviewer": reviewer,
        "note": note,
        "lifted": target.to_dict(),
    }
    target_path = repository.tombstone_path(object_id)
    changed_paths = tuple(
        sorted(
            {
                repository._relative(target_path),
                repository._relative(manifest_path),
            }
        )
    )
    if dry_run:
        return LiftResult(
            object_id=object_id,
            kind=target.kind,
            redirect_to=target.redirect_to,
            manifest_path=repository._relative(manifest_path),
            changed_paths=changed_paths,
            dry_run=True,
        )

    repository.ensure_layout()
    repository.commit(
        {manifest_path: json_bytes(manifest)},
        deletes=[target_path],
        preconditions={
            manifest_path: None,
            target_path: sha256_file(target_path),
        },
    )
    repository.validate_all()
    return LiftResult(
        object_id=object_id,
        kind=target.kind,
        redirect_to=target.redirect_to,
        manifest_path=repository._relative(manifest_path),
        changed_paths=changed_paths,
        dry_run=False,
    )
