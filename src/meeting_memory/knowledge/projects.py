"""Saved project scopes for source-bounded knowledge retrieval."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, List, Sequence, Tuple

from .consumption import SearchDocument, normalize_display, normalize_phrase
from .errors import SchemaError
from .repository import KnowledgeRepository
from .util import atomic_write, json_bytes, slugify


PROJECT_SCOPE_VERSION = 1


def _names(values: Iterable[str], field_name: str) -> Tuple[str, ...]:
    result: List[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SchemaError(
                "project.%s entries must be non-empty strings" % field_name
            )
        normalized = normalize_display(value)
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True)
class ProjectScope:
    """A named set of meeting-filename and Slack-source selectors.

    Meeting selectors are deliberately fuzzy: after punctuation and case are
    normalized, a selector may occur anywhere in the meeting filename. Slack
    selectors are exact (case-insensitive) matches for either ``C123`` or
    ``slack-C123``. This makes a project key such as ``CCI`` cover every meeting
    filename containing CCI without accidentally admitting similarly named
    Slack channels.
    """

    name: str
    meeting_names: Tuple[str, ...] = ()
    slack_names: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SchemaError("project.name must be a non-empty string")
        object.__setattr__(self, "name", normalize_display(self.name))
        object.__setattr__(
            self, "meeting_names", _names(self.meeting_names, "meeting_names")
        )
        object.__setattr__(
            self, "slack_names", _names(self.slack_names, "slack_names")
        )
        if not self.meeting_names and not self.slack_names:
            raise SchemaError(
                "project must contain at least one meeting name or Slack name"
            )

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @classmethod
    def from_dict(cls, raw: Any) -> "ProjectScope":
        if not isinstance(raw, dict):
            raise SchemaError("project scope must be a JSON object")
        version = raw.get("version", PROJECT_SCOPE_VERSION)
        if isinstance(version, bool) or version != PROJECT_SCOPE_VERSION:
            raise SchemaError("unsupported project scope version: %s" % version)
        meeting_names = raw.get("meeting_names", [])
        slack_names = raw.get("slack_names", [])
        if not isinstance(meeting_names, list):
            raise SchemaError("project.meeting_names must be an array")
        if not isinstance(slack_names, list):
            raise SchemaError("project.slack_names must be an array")
        return cls(
            name=raw.get("name"),
            meeting_names=tuple(meeting_names),
            slack_names=tuple(slack_names),
        )

    def to_dict(self) -> dict:
        return {
            "version": PROJECT_SCOPE_VERSION,
            "name": self.name,
            "slug": self.slug,
            "meeting_names": list(self.meeting_names),
            "slack_names": list(self.slack_names),
        }

    def matches_source(self, source: str) -> bool:
        """Return whether one portable evidence path belongs to this project."""
        filename = PurePosixPath(source).name
        stem = PurePosixPath(filename).stem
        folded_stem = stem.casefold()
        if folded_stem.startswith("slack-"):
            channel = stem[len("slack-") :]
            aliases = {
                filename.casefold(),
                stem.casefold(),
                channel.casefold(),
                source.casefold(),
            }
            return any(value.casefold() in aliases for value in self.slack_names)

        meeting_name = normalize_phrase(stem)
        return any(
            selector and selector in meeting_name
            for selector in (
                normalize_phrase(PurePosixPath(value).stem)
                for value in self.meeting_names
            )
        )


def scope_documents(
    scope: ProjectScope, documents: Sequence[SearchDocument]
) -> Tuple[SearchDocument, ...]:
    """Narrow documents and prune each admitted object's out-of-scope evidence."""
    result = []
    for document in documents:
        evidence = tuple(
            item for item in document.evidence if scope.matches_source(item.source)
        )
        if evidence:
            result.append(replace(document, evidence=evidence))
    return tuple(result)


def projects_directory(repository: KnowledgeRepository) -> Path:
    return repository.state_dir / "projects"


def project_path(repository: KnowledgeRepository, name: str) -> Path:
    return projects_directory(repository) / ("%s.json" % slugify(name))


def save_project(
    repository: KnowledgeRepository,
    scope: ProjectScope,
    replace_existing: bool = False,
) -> str:
    """Persist one scope atomically and return its repository-relative path."""
    path = project_path(repository, scope.name)
    with repository.mutation_lock():
        if path.exists() and not replace_existing:
            raise ValueError(
                "project %r already exists; use --replace to update it" % scope.name
            )
        if path.exists() and replace_existing:
            existing = _load_path(path)
            if existing.name.casefold() != scope.name.casefold():
                raise ValueError(
                    "project name %r collides with existing project %r"
                    % (scope.name, existing.name)
                )
        atomic_write(path, json_bytes(scope.to_dict()))
    return repository._relative(path)


def _load_path(path: Path) -> ProjectScope:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError("invalid project JSON %s: %s" % (path, exc)) from exc
    except UnicodeError as exc:
        raise SchemaError("project JSON is not valid UTF-8: %s" % path) from exc
    scope = ProjectScope.from_dict(raw)
    if path.stem != scope.slug:
        raise SchemaError(
            "project filename %s does not match project name %r"
            % (path.name, scope.name)
        )
    return scope


def load_projects(repository: KnowledgeRepository) -> Tuple[ProjectScope, ...]:
    directory = projects_directory(repository)
    if not directory.exists():
        return ()
    scopes = [_load_path(path) for path in sorted(directory.glob("*.json"))]
    seen = set()
    for scope in scopes:
        key = scope.name.casefold()
        if key in seen:
            raise SchemaError("duplicate project name: %s" % scope.name)
        seen.add(key)
    return tuple(sorted(scopes, key=lambda item: (item.name.casefold(), item.slug)))


def load_project(repository: KnowledgeRepository, name: str) -> ProjectScope:
    requested = normalize_display(name).casefold()
    requested_slug = slugify(name)
    for scope in load_projects(repository):
        if scope.name.casefold() == requested or scope.slug == requested_slug:
            return scope
    raise ValueError("project scope not found: %s" % name)
