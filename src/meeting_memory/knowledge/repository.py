"""Filesystem repository, discovery, validation, and transaction support."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import (
    CATEGORIES,
    GENERATED_BEGIN,
    GENERATED_END,
    MANUAL_BEGIN,
    MANUAL_END,
)
from .errors import EvidenceError, SchemaError, StorageError
from .models import (
    Evidence,
    KnowledgeObject,
    MeetingSource,
    ReviewItem,
    validate_run_manifest,
    validate_source_state,
)
from .util import (
    atomic_write,
    dump_frontmatter,
    json_bytes,
    parse_frontmatter,
    sha256_file,
    slugify,
)


class KnowledgeRepository:
    """Owns meeting-note input and generated memory data.

    ``root`` is the writable memory-data directory. ``meetings_dir`` may live
    anywhere; evidence keeps the portable logical form ``meetings/...`` rather
    than embedding an absolute machine-specific path.
    """

    def __init__(
        self,
        root: Path,
        meetings_dir: Optional[Path] = None,
        require_evidence_sources: bool = True,
    ):
        self.root = Path(root).resolve()
        self.meetings_dir = (
            Path(meetings_dir).resolve()
            if meetings_dir is not None
            else self.root / "meetings"
        )
        self.knowledge_dir = self.root / "knowledge"
        self.require_evidence_sources = require_evidence_sources
        self.review_dir = self.root / "knowledge-review"
        self.state_dir = self.root / ".knowledge-state"
        self.outputs_dir = self.root / "outputs" / "Durable-Knowledge"
        self.logs_dir = self.root / "logs"

    def ensure_layout(self) -> None:
        for category in CATEGORIES:
            (self.knowledge_dir / category).mkdir(parents=True, exist_ok=True)
        for status in ("pending", "resolved", "rejected"):
            (self.review_dir / status).mkdir(parents=True, exist_ok=True)
        for child in ("sources", "runs"):
            (self.state_dir / child).mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError as exc:
            raise SchemaError("path is outside repository: %s" % path) from exc

    def source_reference(self, path: Path) -> str:
        """Return the stable evidence identifier for an input note."""
        try:
            relative = path.resolve().relative_to(self.meetings_dir.resolve())
        except ValueError as exc:
            raise SchemaError("path is outside meeting logs: %s" % path) from exc
        return (PurePosixPath("meetings") / PurePosixPath(relative.as_posix())).as_posix()

    def evidence_path(self, source: str) -> Path:
        """Resolve a portable ``meetings/...`` evidence identifier safely."""
        logical = PurePosixPath(source)
        if (
            logical.is_absolute()
            or not logical.parts
            or logical.parts[0] != "meetings"
            or any(part in ("", ".", "..") for part in logical.parts[1:])
        ):
            raise EvidenceError("evidence source must be under meetings/: %s" % source)
        resolved = (self.meetings_dir / Path(*logical.parts[1:])).resolve()
        try:
            resolved.relative_to(self.meetings_dir.resolve())
        except ValueError as exc:
            raise EvidenceError("evidence escapes meeting logs: %s" % source) from exc
        return resolved

    def discover_dates(self) -> List[str]:
        if not self.meetings_dir.is_dir():
            return []
        values = []
        for child in self.meetings_dir.iterdir():
            if child.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", child.name):
                values.append(child.name)
        return sorted(values)

    @staticmethod
    def _frontmatter_opt_out(path: Path) -> bool:
        try:
            raw, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SchemaError):
            return False
        value = raw.get("durable_knowledge")
        return value is False or (isinstance(value, str) and value.strip().lower() == "false")

    def qualifying_sources(self, source_date: str) -> List[MeetingSource]:
        directory = self.meetings_dir / source_date
        if not directory.is_dir():
            return []
        result = []
        for path in sorted(directory.glob("*.md"), key=lambda item: item.name.lower()):
            lower = path.name.lower()
            if lower.endswith(".transcript.md") or "standup" in lower or "interview" in lower:
                continue
            if self._frontmatter_opt_out(path):
                continue
            data = path.read_bytes()
            result.append(
                MeetingSource(
                    path=path,
                    relative_path=self.source_reference(path),
                    source_date=source_date,
                    sha256=sha256_file(path),
                    content=data.decode("utf-8"),
                )
            )
        return result

    @staticmethod
    def _only_marker_pair(text: str, begin: str, end: str) -> Tuple[int, int]:
        if text.count(begin) != 1 or text.count(end) != 1:
            raise SchemaError("expected exactly one %s / %s marker pair" % (begin, end))
        start = text.index(begin)
        finish = text.index(end, start) + len(end)
        if finish <= start:
            raise SchemaError("protected Markdown markers are out of order")
        return start, finish

    def load_knowledge_file(self, path: Path) -> KnowledgeObject:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SchemaError("cannot read knowledge file %s: %s" % (path, exc)) from exc
        raw, body = parse_frontmatter(text)
        generated_start, generated_end = self._only_marker_pair(body, GENERATED_BEGIN, GENERATED_END)
        manual_start, manual_end = self._only_marker_pair(body, MANUAL_BEGIN, MANUAL_END)
        if generated_end > manual_start:
            raise SchemaError("generated and manual sections overlap in %s" % path)
        generated = body[generated_start + len(GENERATED_BEGIN) : generated_end - len(GENERATED_END)]
        statement = generated.strip()
        if raw.get("statement") != statement:
            raise SchemaError("front-matter statement and generated statement differ in %s" % path)
        manual_section = body[manual_start:manual_end]
        history = []
        history_match = re.search(r"(?m)^## History\s*$([\s\S]*)$", body[manual_end:])
        if history_match:
            for line in history_match.group(1).splitlines():
                if line.startswith("- "):
                    history.append(line[2:])
                elif line.strip():
                    raise SchemaError("history contains a non-list entry in %s" % path)
        obj = KnowledgeObject.from_dict(
            raw,
            history=history,
            manual_section=manual_section,
            path=path,
        )
        expected = self.knowledge_dir / obj.category
        if path.parent.resolve() != expected.resolve():
            raise SchemaError("knowledge category does not match directory for %s" % path)
        return obj

    def load_knowledge(self) -> List[KnowledgeObject]:
        objects = []
        seen: Dict[str, Path] = {}
        if not self.knowledge_dir.exists():
            return objects
        for category in CATEGORIES:
            for path in sorted((self.knowledge_dir / category).glob("*.md")):
                obj = self.load_knowledge_file(path)
                if obj.id in seen:
                    raise SchemaError(
                        "duplicate knowledge id %s in %s and %s" % (obj.id, seen[obj.id], path)
                    )
                seen[obj.id] = path
                objects.append(obj)
        known = set(seen)
        for obj in objects:
            missing = sorted(set(obj.related_objects) - known)
            if missing:
                raise SchemaError("%s refers to missing objects: %s" % (obj.id, ", ".join(missing)))
        return objects

    @staticmethod
    def render_knowledge(obj: KnowledgeObject) -> bytes:
        manual = obj.manual_section
        if manual is None:
            manual = "%s\n\n%s" % (MANUAL_BEGIN, MANUAL_END)
        history = "\n".join("- " + item for item in obj.history)
        text = (
            "---\n%s\n---\n\n"
            "%s\n\n%s\n\n%s\n\n"
            "%s\n\n"
            "## History\n\n%s\n"
        ) % (
            dump_frontmatter(obj.to_frontmatter()),
            GENERATED_BEGIN,
            obj.statement,
            GENERATED_END,
            manual,
            history,
        )
        return text.encode("utf-8")

    @staticmethod
    def render_review(item: ReviewItem) -> bytes:
        existing = item.existing_statement or "_No single existing statement was identified._"
        existing_evidence = "\n".join(
            "- `%s` (sha256 `%s`, anchor `%s`, lines %d-%d, observed %s)"
            % (
                ev.source,
                ev.source_sha256,
                ev.anchor,
                ev.line_start,
                ev.line_end,
                ev.observed_at,
            )
            for ev in item.existing_evidence
        ) or "- None"
        candidate_evidence = "\n".join(
            "- `%s` (sha256 `%s`, anchor `%s`, lines %d-%d, observed %s)"
            % (
                ev.source,
                ev.source_sha256,
                ev.anchor,
                ev.line_start,
                ev.line_end,
                ev.observed_at,
            )
            for ev in item.candidate_evidence
        ) or "- None"
        text = (
            "---\n%s\n---\n\n"
            "# %s\n\n"
            "## Existing knowledge\n\n%s\n\n"
            "### Existing evidence\n\n%s\n\n"
            "## New candidate\n\n%s\n\n"
            "### Candidate evidence\n\n%s\n\n"
            "## Why review is required\n\n%s\n\n"
            "## Suggested actions\n\n"
            "- confirm the current approved statement\n"
            "- retain the existing knowledge\n"
            "- update the knowledge manually\n"
            "- reject the candidate\n"
        ) % (
            dump_frontmatter(item.to_frontmatter()),
            item.title,
            existing,
            existing_evidence,
            item.candidate_statement,
            candidate_evidence,
            item.explanation,
        )
        return text.encode("utf-8")

    def load_review_file(self, path: Path) -> ReviewItem:
        text = path.read_text(encoding="utf-8")
        raw, body = parse_frontmatter(text)

        def section(name: str, next_name: str) -> str:
            match = re.search(
                r"(?m)^## %s\s*$\n+([\s\S]*?)(?=^## %s\s*$)" % (re.escape(name), re.escape(next_name)),
                body,
            )
            if not match:
                raise SchemaError("review item %s is missing section %s" % (path, name))
            return match.group(1).strip()

        title_match = re.search(r"(?m)^# (.+)$", body)
        if not title_match:
            raise SchemaError("review item %s is missing title" % path)
        existing = section("Existing knowledge", "New candidate")
        existing = existing.split("\n\n### Existing evidence", 1)[0].strip()
        if existing.startswith("_No single"):
            existing = None
        candidate = section("New candidate", "Why review is required")
        candidate = candidate.split("\n\n### Candidate evidence", 1)[0].strip()
        explanation_match = re.search(
            r"(?m)^## Why review is required\s*$\n+([\s\S]*?)(?=^## Suggested actions\s*$)",
            body,
        )
        if not explanation_match:
            raise SchemaError("review item %s is missing explanation" % path)
        item = ReviewItem.from_dict(
            raw,
            title=title_match.group(1).strip(),
            existing_statement=existing,
            candidate_statement=candidate,
            explanation=explanation_match.group(1).strip(),
        )
        if path.parent.name in ("pending", "resolved", "rejected") and item.status != path.parent.name:
            raise SchemaError("review status does not match directory for %s" % path)
        for source in item.sources:
            try:
                source_path = self.evidence_path(source)
            except EvidenceError as exc:
                raise SchemaError(str(exc)) from exc
            if not source_path.is_file():
                raise SchemaError("review source is missing: %s" % source)
        return item

    def load_reviews(self, status: Optional[str] = None) -> List[ReviewItem]:
        if status is not None and status not in ("pending", "resolved", "rejected"):
            raise ValueError("unsupported review status: %s" % status)
        statuses = (status,) if status else ("pending", "resolved", "rejected")
        values = []
        seen = set()
        known_ids = {item.id for item in self.load_knowledge()}
        for review_status in statuses:
            directory = self.review_dir / review_status
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                item = self.load_review_file(path)
                if item.id in seen:
                    raise SchemaError("duplicate review item id: %s" % item.id)
                missing = sorted(set(item.possible_existing_ids) - known_ids)
                if missing:
                    raise SchemaError(
                        "%s refers to missing knowledge objects: %s"
                        % (item.id, ", ".join(missing))
                    )
                seen.add(item.id)
                values.append(item)
        return values

    def load_review_ids(self) -> set:
        return {item.id for item in self.load_reviews()}

    def state_path(self, source_path: str) -> Path:
        import hashlib

        stem = slugify(source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0], 48)
        suffix = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:10]
        return self.state_dir / "sources" / ("%s-%s.json" % (stem, suffix))

    def load_source_state(self, source_path: str) -> Optional[Dict[str, Any]]:
        path = self.state_path(source_path)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SchemaError("invalid source state %s: %s" % (path, exc)) from exc
        validate_source_state(raw)
        if raw["source_path"] != source_path:
            raise SchemaError("source state path collision for %s" % source_path)
        return raw

    def iter_source_states(self) -> Iterable[Tuple[Path, Dict[str, Any]]]:
        directory = self.state_dir / "sources"
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SchemaError("invalid source state %s: %s" % (path, exc)) from exc
            validate_source_state(raw)
            yield path, raw

    def latest_successful_run(self) -> Optional[Dict[str, Any]]:
        directory = self.state_dir / "runs"
        if not directory.exists():
            return None
        latest = None
        for path in sorted(directory.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            validate_run_manifest(raw)
            if raw["status"] == "success":
                latest = raw
        return latest

    def validate_evidence(self, evidence: Evidence, require_current_hash: bool = True) -> None:
        source_path = self.evidence_path(evidence.source)
        if not source_path.is_file():
            if not self.require_evidence_sources:
                return
            raise EvidenceError("evidence source is missing: %s" % evidence.source)
        if require_current_hash and evidence.source_sha256 != sha256_file(source_path):
            raise EvidenceError("evidence fingerprint does not match: %s" % evidence.source)
        if require_current_hash:
            try:
                line_count = len(source_path.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeError) as exc:
                raise EvidenceError("cannot read evidence source: %s" % evidence.source) from exc
            if evidence.line_end > line_count:
                raise EvidenceError(
                    "evidence lines %d-%d exceed %s (%d lines)"
                    % (evidence.line_start, evidence.line_end, evidence.source, line_count)
                )

    def validate_all(self) -> Dict[str, int]:
        objects = self.load_knowledge()
        for obj in objects:
            for evidence in obj.evidence:
                # A synced note may legitimately have changed since an older evidence
                # record was captured. Candidate promotion checks the current hash;
                # corpus validation checks that the historical digest is well-formed
                # and that its source/line locator still exists.
                self.validate_evidence(evidence, require_current_hash=False)
        review_count = len(self.load_review_ids())
        state_count = sum(1 for _ in self.iter_source_states())
        run_count = 0
        runs = self.state_dir / "runs"
        if runs.exists():
            for path in sorted(runs.glob("*.json")):
                raw = json.loads(path.read_text(encoding="utf-8"))
                validate_run_manifest(raw)
                run_count += 1
        index_dir = self.knowledge_dir / "_index"
        if index_dir.exists():
            link_pattern = re.compile(r"\]\((\.\./[^)#]+\.md)\)")
            for index_path in sorted(index_dir.glob("*.md")):
                text = index_path.read_text(encoding="utf-8")
                for target in link_pattern.findall(text):
                    resolved = (index_path.parent / target).resolve()
                    try:
                        resolved.relative_to(self.knowledge_dir.resolve())
                    except ValueError as exc:
                        raise SchemaError(
                            "generated index link escapes knowledge/: %s in %s"
                            % (target, index_path)
                        ) from exc
                    if not resolved.is_file():
                        raise SchemaError(
                            "generated index points to missing canonical file: %s in %s"
                            % (target, index_path)
                        )
        result = {
            "knowledge_objects": len(objects),
            "review_items": review_count,
            "source_states": state_count,
            "run_manifests": run_count,
        }
        # A machine index is optional and never authoritative. When present,
        # report its state so callers can rebuild instead of trusting drift.
        from .machine_index import machine_index_status

        result["machine_index_status"] = machine_index_status(self)
        return result

    def commit(self, changes: Dict[Path, bytes]) -> List[str]:
        """Apply a validated multi-file change set and roll back on failure."""
        if not changes:
            return []
        normalized: Dict[Path, bytes] = {}
        for path, data in changes.items():
            path = Path(path).resolve()
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise StorageError("transaction target escapes repository: %s" % path) from exc
            normalized[path] = data

        transaction_dir = Path(tempfile.mkdtemp(prefix=".knowledge-txn-", dir=str(self.root)))
        staged = transaction_dir / "staged"
        backups = transaction_dir / "backups"
        applied: List[Tuple[Path, Optional[Path]]] = []
        try:
            for index, (target, data) in enumerate(sorted(normalized.items(), key=lambda item: str(item[0]))):
                stage_path = staged / str(index)
                atomic_write(stage_path, data)
                backup = None
                if target.exists():
                    backup = backups / str(index)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(target), str(backup))
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(str(stage_path), str(target))
                except OSError as exc:
                    raise StorageError("atomic replace failed for %s: %s" % (target, exc)) from exc
                applied.append((target, backup))
        except Exception:
            rollback_errors = []
            for target, backup in reversed(applied):
                try:
                    if backup is None:
                        target.unlink()
                    else:
                        os.replace(str(backup), str(target))
                except OSError as exc:
                    rollback_errors.append("%s: %s" % (target, exc))
            if rollback_errors:
                raise StorageError("transaction failed and rollback was incomplete: %s" % "; ".join(rollback_errors))
            raise
        finally:
            shutil.rmtree(str(transaction_dir), ignore_errors=True)
        return [self._relative(path) for path in sorted(normalized, key=str)]

    def write_manifest(self, path: Path, manifest: Dict[str, Any]) -> None:
        validate_run_manifest(manifest)
        atomic_write(path, json_bytes(manifest))
