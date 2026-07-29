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
    REVIEW_STATUSES,
)
from .errors import EvidenceError, SchemaError, StaleReviewError, StorageError
from .models import (
    Evidence,
    KnowledgeObject,
    MeetingSource,
    ReviewItem,
    validate_review_run_manifest,
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
        self.suggestion_dir = self.review_dir / "suggestions"
        self.state_dir = self.root / ".knowledge-state"
        self.review_run_dir = self.state_dir / "review-runs"
        self.outputs_dir = self.root / "outputs" / "Durable-Knowledge"
        self.logs_dir = self.root / "logs"

    def ensure_layout(self) -> None:
        for category in CATEGORIES:
            (self.knowledge_dir / category).mkdir(parents=True, exist_ok=True)
        for status in REVIEW_STATUSES:
            (self.review_dir / status).mkdir(parents=True, exist_ok=True)
        self.suggestion_dir.mkdir(parents=True, exist_ok=True)
        for child in ("sources", "runs", "review-runs"):
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
        resolution = ""
        if item.resolution_action:
            resolution = (
                "\n\n## Resolution\n\n"
                "- Action: `%s`\n"
                "- Reviewer: %s\n"
                "- Resolved at: %s\n"
                "- Affected objects: %s\n"
                "- Duplicate of: %s\n"
                "- Allowed stale evidence: %s\n\n"
                "%s"
                % (
                    item.resolution_action,
                    item.reviewer,
                    item.resolved_at,
                    ", ".join("`%s`" % value for value in item.affected_object_ids)
                    or "None",
                    "`%s`" % item.duplicate_of if item.duplicate_of else "None",
                    "yes" if item.allowed_stale_evidence else "no",
                    item.resolution_note,
                )
            )
        text = (
            "---\n%s\n---\n\n"
            "# %s\n\n"
            "## Existing knowledge\n\n%s\n\n"
            "### Existing evidence\n\n%s\n\n"
            "## New candidate\n\n%s\n\n"
            "### Candidate evidence\n\n%s\n\n"
            "## Why review is required\n\n%s\n\n"
            "## Suggested actions\n\n"
            "- accept as replacement\n"
            "- accept as refinement\n"
            "- reconfirm or retain the existing knowledge\n"
            "- create a separate knowledge object\n"
            "- reject or merge a duplicate candidate"
            "%s\n"
        ) % (
            dump_frontmatter(item.to_frontmatter()),
            item.title,
            existing,
            existing_evidence,
            item.candidate_statement,
            candidate_evidence,
            item.explanation,
            resolution,
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

        def parsed_evidence(value: str, label: str) -> List[Evidence]:
            evidence = []
            pattern = re.compile(
                r"(?m)^- `(?P<source>[^`]+)` "
                r"\(sha256 `(?P<sha>[0-9a-f]{64})`, "
                r"anchor `(?P<anchor>.*)`, "
                r"lines (?P<start>\d+)-(?P<end>\d+), "
                r"observed (?P<observed>\d{4}-\d{2}-\d{2})\)$"
            )
            for match in pattern.finditer(value):
                evidence.append(
                    Evidence.from_dict(
                        {
                            "source": match.group("source"),
                            "source_sha256": match.group("sha"),
                            "anchor": match.group("anchor"),
                            "line_start": int(match.group("start")),
                            "line_end": int(match.group("end")),
                            "observed_at": match.group("observed"),
                        },
                        complete=True,
                    )
                )
            if "- None" not in value and not evidence:
                raise SchemaError(
                    "review item %s has invalid %s evidence" % (path, label)
                )
            return evidence

        title_match = re.search(r"(?m)^# (.+)$", body)
        if not title_match:
            raise SchemaError("review item %s is missing title" % path)
        existing_section = section("Existing knowledge", "New candidate")
        existing_parts = existing_section.split("\n\n### Existing evidence", 1)
        existing = existing_parts[0].strip()
        if existing.startswith("_No single"):
            existing = None
        existing_evidence = (
            parsed_evidence(existing_parts[1], "existing")
            if len(existing_parts) == 2
            else []
        )
        candidate_section = section("New candidate", "Why review is required")
        candidate_parts = candidate_section.split("\n\n### Candidate evidence", 1)
        candidate = candidate_parts[0].strip()
        candidate_evidence = (
            parsed_evidence(candidate_parts[1], "candidate")
            if len(candidate_parts) == 2
            else []
        )
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
            existing_evidence=existing_evidence,
            candidate_evidence=candidate_evidence,
            explanation=explanation_match.group(1).strip(),
            path=path,
        )
        if path.parent.name in REVIEW_STATUSES and item.status != path.parent.name:
            raise SchemaError("review status does not match directory for %s" % path)
        for source in item.sources:
            try:
                source_path = self.evidence_path(source)
            except EvidenceError as exc:
                raise SchemaError(str(exc)) from exc
            if self.require_evidence_sources and not source_path.is_file():
                raise SchemaError("review source is missing: %s" % source)
        return item

    def load_reviews(self, status: Optional[str] = None) -> List[ReviewItem]:
        if status is not None and status not in REVIEW_STATUSES:
            raise ValueError("unsupported review status: %s" % status)
        statuses = (status,) if status else REVIEW_STATUSES
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
                missing_affected = sorted(set(item.affected_object_ids) - known_ids)
                if missing_affected:
                    raise SchemaError(
                        "%s resolution refers to missing knowledge objects: %s"
                        % (item.id, ", ".join(missing_affected))
                    )
                seen.add(item.id)
                values.append(item)
        for item in values:
            if item.duplicate_of and item.duplicate_of not in seen:
                raise SchemaError(
                    "%s duplicates missing review item: %s"
                    % (item.id, item.duplicate_of)
                )
        return values

    def load_review_ids(self) -> set:
        return {item.id for item in self.load_reviews()}

    @staticmethod
    def _artifact_id(value: str, label: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", value
        ):
            raise SchemaError("%s has unsafe characters: %s" % (label, value))
        return value

    def suggestion_path(self, review_id: str, suggestion_id: str) -> Path:
        review_id = self._artifact_id(review_id, "review ID")
        suggestion_id = self._artifact_id(suggestion_id, "suggestion ID")
        return self.suggestion_dir / review_id / ("%s.json" % suggestion_id)

    def load_suggestion(self, review_id: str, suggestion_id: str) -> Any:
        from .review_suggestions import ReviewSuggestion

        path = self.suggestion_path(review_id, suggestion_id)
        if not path.is_file():
            raise SchemaError("suggestion artifact not found: %s" % suggestion_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SchemaError("invalid suggestion artifact %s: %s" % (path, exc)) from exc
        suggestion = ReviewSuggestion.from_dict(raw)
        if suggestion.id != suggestion_id or suggestion.review_id != review_id:
            raise SchemaError("suggestion identity does not match its path: %s" % path)
        return suggestion

    def load_suggestions(self, review_id: str) -> List[Any]:
        review_id = self._artifact_id(review_id, "review ID")
        directory = self.suggestion_dir / review_id
        if not directory.exists():
            return []
        values = []
        for path in sorted(directory.glob("*.json")):
            values.append(self.load_suggestion(review_id, path.stem))
        return sorted(values, key=lambda value: (value.generated_at, value.id))

    def write_suggestion(self, suggestion: Any) -> str:
        from .review_suggestions import ReviewSuggestion

        if not isinstance(suggestion, ReviewSuggestion):
            raise TypeError("suggestion must be a ReviewSuggestion")
        # Round-trip validation happens before the immutable artifact is written.
        ReviewSuggestion.from_dict(suggestion.to_dict())
        if suggestion.review_id not in self.load_review_ids():
            raise SchemaError(
                "suggestion refers to missing review: %s" % suggestion.review_id
            )
        path = self.suggestion_path(suggestion.review_id, suggestion.id)
        if path.exists():
            raise StorageError("suggestion artifact already exists: %s" % path)
        atomic_write(path, json_bytes(suggestion.to_dict()))
        return self._relative(path)

    def review_run_path(self, run_id: str) -> Path:
        run_id = self._artifact_id(run_id, "review run ID")
        return self.review_run_dir / ("%s.json" % run_id)

    def write_review_run_manifest(self, manifest: Dict[str, Any]) -> str:
        validate_review_run_manifest(manifest)
        path = self.review_run_path(manifest["run_id"])
        if path.exists():
            raise StorageError("review run manifest already exists: %s" % path)
        for bucket in ("suggestions_created", "suggestions_reused"):
            for review_id, suggestion_id in manifest[bucket].items():
                self.load_suggestion(review_id, suggestion_id)
        atomic_write(path, json_bytes(manifest))
        return self._relative(path)

    def iter_review_run_manifests(self) -> Iterable[Tuple[Path, Dict[str, Any]]]:
        if not self.review_run_dir.exists():
            return
        for path in sorted(self.review_run_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SchemaError(
                    "invalid review run manifest %s: %s" % (path, exc)
                ) from exc
            validate_review_run_manifest(raw)
            for bucket in ("suggestions_created", "suggestions_reused"):
                for review_id, suggestion_id in raw[bucket].items():
                    self.load_suggestion(review_id, suggestion_id)
            yield path, raw

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
        known_review_ids = self.load_review_ids()
        suggestion_count = 0
        if self.suggestion_dir.exists():
            for directory in sorted(self.suggestion_dir.iterdir()):
                if not directory.is_dir():
                    raise SchemaError(
                        "unexpected file beneath suggestion directory: %s" % directory
                    )
                if directory.name not in known_review_ids:
                    raise SchemaError(
                        "suggestions refer to missing review: %s" % directory.name
                    )
                suggestion_count += len(self.load_suggestions(directory.name))
        review_run_count = sum(1 for _ in self.iter_review_run_manifests())
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
            "suggestions": suggestion_count,
            "review_run_manifests": review_run_count,
        }
        # A machine index is optional and never authoritative. When present,
        # report its state so callers can rebuild instead of trusting drift.
        from .machine_index import machine_index_status

        result["machine_index_status"] = machine_index_status(self)
        return result

    def commit(
        self,
        changes: Dict[Path, bytes],
        deletes: Sequence[Path] = (),
        preconditions: Optional[Dict[Path, Optional[str]]] = None,
    ) -> List[str]:
        """Apply validated writes/deletions as one transaction and roll back on failure."""
        if not changes and not deletes:
            return []
        normalized: Dict[Path, bytes] = {}
        for path, data in changes.items():
            path = Path(path).resolve()
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise StorageError("transaction target escapes repository: %s" % path) from exc
            normalized[path] = data
        normalized_deletes = []
        seen_deletes = set()
        for path in deletes:
            path = Path(path).resolve()
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise StorageError(
                    "transaction deletion escapes repository: %s" % path
                ) from exc
            if path in normalized:
                raise StorageError("transaction may not write and delete %s" % path)
            if path in seen_deletes:
                raise StorageError("duplicate transaction deletion target: %s" % path)
            if not path.is_file():
                raise StorageError("transaction deletion target is missing: %s" % path)
            seen_deletes.add(path)
            normalized_deletes.append(path)

        normalized_preconditions: Dict[Path, Optional[str]] = {}
        for path, expected in (preconditions or {}).items():
            path = Path(path).resolve()
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise StorageError(
                    "transaction precondition escapes repository: %s" % path
                ) from exc
            if path not in normalized and path not in seen_deletes:
                raise StorageError(
                    "transaction precondition is not a mutation target: %s" % path
                )
            if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise StorageError(
                    "transaction precondition must be a SHA-256 digest or null"
                )
            normalized_preconditions[path] = expected

        transaction_dir = Path(tempfile.mkdtemp(prefix=".knowledge-txn-", dir=str(self.root)))
        staged = transaction_dir / "staged"
        backups = transaction_dir / "backups"
        applied: List[Tuple[Path, Optional[Path]]] = []
        try:
            operations = [
                (target, data)
                for target, data in sorted(normalized.items(), key=lambda item: str(item[0]))
            ]
            operations.extend(
                (target, None) for target in sorted(normalized_deletes, key=str)
            )
            # Check every target immediately before the first replacement. No
            # repository write has occurred at this point.
            for target, expected in normalized_preconditions.items():
                if expected is None:
                    if target.exists():
                        raise StaleReviewError(
                            "transaction target was created after it was loaded: %s"
                            % self._relative(target)
                        )
                elif not target.is_file() or sha256_file(target) != expected:
                    raise StaleReviewError(
                        "transaction target changed after it was loaded: %s"
                        % self._relative(target)
                    )
            for index, (target, data) in enumerate(operations):
                stage_path = staged / str(index)
                if data is not None:
                    atomic_write(stage_path, data)
                backup = None
                if target.exists():
                    backup = backups / str(index)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(target), str(backup))
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if data is None:
                        target.unlink()
                    else:
                        os.replace(str(stage_path), str(target))
                except OSError as exc:
                    raise StorageError(
                        "transaction operation failed for %s: %s" % (target, exc)
                    ) from exc
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
        touched = set(normalized) | set(normalized_deletes)
        return [self._relative(path) for path in sorted(touched, key=str)]

    def write_manifest(self, path: Path, manifest: Dict[str, Any]) -> None:
        validate_run_manifest(manifest)
        atomic_write(path, json_bytes(manifest))
