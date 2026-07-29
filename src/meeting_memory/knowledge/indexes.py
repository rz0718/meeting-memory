"""Human and machine index generation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import CATEGORIES
from .consumption import SearchDocument, latest_document_date, load_documents
from .machine_index import machine_index_payloads
from .repository import KnowledgeRepository, mutation_locked
from .util import sha256_bytes


STATUS_ORDER = ("approved", "proposed", "unclear", "deprecated")
CATEGORY_LABELS = {
    "decisions": "Decisions",
    "policies": "Policies",
    "processes": "Processes",
    "projects": "Projects",
    "systems": "Systems",
    "metrics": "Metrics",
    "people-and-ownership": "People and Ownership",
}


@dataclass(frozen=True)
class IndexResult:
    loaded: int
    generated: int
    changed: int
    unchanged: int
    written: int
    changed_paths: Tuple[str, ...]
    unchanged_paths: Tuple[str, ...]
    machine_index_status: str
    dry_run: bool
    forced: bool

    def to_dict(self) -> dict:
        return {
            "loaded": self.loaded,
            "generated": self.generated,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "written": self.written,
            "changed_paths": list(self.changed_paths),
            "unchanged_paths": list(self.unchanged_paths),
            "machine_index_status": self.machine_index_status,
            "dry_run": self.dry_run,
            "forced": self.forced,
        }


def _display_date(value: Optional[dt.date]) -> str:
    return value.isoformat() if value else "Unknown"


def _document_sort_key(document: SearchDocument) -> tuple:
    updated = int(document.updated_at.timestamp())
    return (-updated, document.title.casefold(), document.id)


def _canonical_link(document: SearchDocument) -> str:
    path = PurePosixPath(document.file_path)
    return "../%s" % path.relative_to("knowledge").as_posix()


def _entry(document: SearchDocument) -> str:
    return "\n".join(
        [
            "### [%s](%s)" % (document.title, _canonical_link(document)),
            "",
            "- ID: `%s`" % document.id,
            "- Status: %s" % document.status,
            "- Owner: %s" % (document.owner or "Unknown owner"),
            "- Effective date: %s" % _display_date(document.effective_date),
            "- Last confirmed: %s" % _display_date(document.last_confirmed),
            "- Confidence: %s" % document.confidence,
            "",
            document.statement,
        ]
    )


def _grouped_index(title: str, documents: Sequence[SearchDocument]) -> str:
    lines = ["# %s" % title, "", "Generated from canonical durable knowledge.", ""]
    for status in STATUS_ORDER:
        lines.extend(["## %s" % status.title(), ""])
        values = sorted(
            (item for item in documents if item.status == status),
            key=_document_sort_key,
        )
        if not values:
            lines.extend(["_No objects._", ""])
            continue
        for document in values:
            lines.extend([_entry(document), ""])
    return "\n".join(lines).rstrip() + "\n"


def _root_readme() -> str:
    lines = [
        "# Durable Knowledge",
        "",
        "This directory contains curated, evidence-backed knowledge extracted from meeting and Slack sources.",
        "Markdown objects in category directories are canonical; `README.md` and `_index/` are",
        "generated browse surfaces that are safe to delete and rebuild on the source VM.",
        "",
        "## Browse by category",
        "",
        "- [All knowledge](_index/all.md)",
    ]
    for category in CATEGORIES:
        lines.append(
            "- [%s](_index/%s.md)" % (CATEGORY_LABELS[category], category)
        )
    lines.extend(
        [
            "",
            "## Other views",
            "",
            "- [Recently updated](_index/recently-updated.md)",
            "- [Unclear and proposed](_index/unclear-and-proposed.md)",
            "- [By owner](_index/owners.md)",
            "",
            "Each canonical object lists source evidence paths and stored line ranges. Follow",
            "those references back to `meetings/` when the supporting discussion is needed.",
            "",
            "## Command-line search",
            "",
            "```bash",
            'meeting-memory search "withdrawal SLA"',
            "```",
            "",
            "The search is deterministic and reports matched fields and scores. Generated",
            "indexes never replace canonical Markdown as the source of truth.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _recent_index(
    documents: Sequence[SearchDocument], recent_days: int, reference_date: dt.date
) -> str:
    earliest = reference_date - dt.timedelta(days=recent_days - 1)
    values = sorted(
        (
            item
            for item in documents
            if earliest <= item.updated_at.date() <= reference_date
        ),
        key=lambda item: (
            -item.updated_at.date().toordinal(),
            item.title.casefold(),
            item.id,
        ),
    )
    lines = [
        "# Recently Updated",
        "",
        "Objects updated from %s through %s (%d days)."
        % (earliest.isoformat(), reference_date.isoformat(), recent_days),
        "",
    ]
    current = None
    for document in values:
        changed = document.updated_at.date()
        if changed != current:
            lines.extend(["## %s" % changed.isoformat(), ""])
            current = changed
        lines.extend(
            [
                "- [%s](%s) — %s; %s; owner: %s; last confirmed: %s"
                % (
                    document.title,
                    _canonical_link(document),
                    document.category,
                    document.status,
                    document.owner or "Unknown owner",
                    _display_date(document.last_confirmed),
                )
            ]
        )
    if not values:
        lines.append("_No recently updated objects._")
    return "\n".join(lines).rstrip() + "\n"


def _review_surface(documents: Sequence[SearchDocument]) -> str:
    values = sorted(
        (item for item in documents if item.status in ("unclear", "proposed")),
        key=lambda item: (
            STATUS_ORDER.index(item.status),
            *_document_sort_key(item),
        ),
    )
    lines = [
        "# Unclear and Proposed Knowledge",
        "",
        "A human review surface for canonical objects that are not approved.",
        "",
    ]
    if not values:
        lines.append("_No unclear or proposed objects._")
    for document in values:
        lines.extend([_entry(document), ""])
    return "\n".join(lines).rstrip() + "\n"


def _owner_key(owner: Optional[str]) -> tuple:
    if not owner:
        return (1, "unknown owner")
    return (0, " ".join(owner.casefold().split()))


def _owners_index(documents: Sequence[SearchDocument]) -> str:
    grouped: Dict[tuple, List[SearchDocument]] = {}
    displays: Dict[tuple, str] = {}
    for document in documents:
        key = _owner_key(document.owner)
        grouped.setdefault(key, []).append(document)
        if key not in displays:
            displays[key] = document.owner or "Unknown owner"
        elif document.owner and document.owner.casefold() < displays[key].casefold():
            displays[key] = document.owner
    lines = [
        "# Knowledge by Owner",
        "",
        "Owner grouping uses whitespace and case normalization only.",
        "",
    ]
    for key in sorted(grouped):
        lines.extend(["## %s" % displays[key], ""])
        owner_documents = grouped[key]
        for category in CATEGORIES:
            values = sorted(
                (item for item in owner_documents if item.category == category),
                key=_document_sort_key,
            )
            if not values:
                continue
            lines.extend(["### %s" % CATEGORY_LABELS[category], ""])
            for document in values:
                lines.append(
                    "- [%s](%s) — `%s`; %s; last confirmed: %s"
                    % (
                        document.title,
                        _canonical_link(document),
                        document.id,
                        document.status,
                        _display_date(document.last_confirmed),
                    )
                )
            lines.append("")
    if not grouped:
        lines.append("_No knowledge objects._")
    return "\n".join(lines).rstrip() + "\n"


def render_human_indexes(
    repository: KnowledgeRepository,
    documents: Sequence[SearchDocument],
    recent_days: int = 30,
    reference_date: Optional[dt.date] = None,
) -> Dict:
    if recent_days < 1:
        raise ValueError("recent_days must be at least 1")
    reference_date = reference_date or latest_document_date(documents)
    index_dir = repository.knowledge_dir / "_index"
    result = {
        repository.knowledge_dir / "README.md": _root_readme().encode("utf-8"),
        index_dir / "all.md": _grouped_index("All Durable Knowledge", documents).encode(
            "utf-8"
        ),
        index_dir / "recently-updated.md": _recent_index(
            documents, recent_days, reference_date
        ).encode("utf-8"),
        index_dir / "unclear-and-proposed.md": _review_surface(documents).encode(
            "utf-8"
        ),
        index_dir / "owners.md": _owners_index(documents).encode("utf-8"),
    }
    for category in CATEGORIES:
        result[index_dir / ("%s.md" % category)] = _grouped_index(
            CATEGORY_LABELS[category],
            tuple(item for item in documents if item.category == category),
        ).encode("utf-8")
    return result


@mutation_locked
def generate_indexes(
    repository: KnowledgeRepository,
    recent_days: int = 30,
    dry_run: bool = False,
    force: bool = False,
    reference_date: Optional[dt.date] = None,
) -> IndexResult:
    documents = load_documents(repository)
    human = render_human_indexes(
        repository, documents, recent_days=recent_days, reference_date=reference_date
    )
    machine = machine_index_payloads(repository, documents)
    payloads = dict(human)
    payloads.update(machine)
    changed = []
    unchanged = []
    preconditions = {}
    for path in sorted(payloads, key=str):
        existing = path.read_bytes() if path.is_file() else None
        preconditions[path] = (
            sha256_bytes(existing)
            if existing is not None
            else None
        )
        if existing is None or existing != payloads[path]:
            changed.append(path)
        else:
            unchanged.append(path)
    to_write = payloads if force else {path: payloads[path] for path in changed}
    written = []
    if not dry_run and to_write:
        written = repository.commit(
            to_write,
            preconditions={path: preconditions[path] for path in to_write},
        )
    return IndexResult(
        loaded=len(documents),
        generated=len(human),
        changed=len(changed),
        unchanged=len(unchanged),
        written=len(written),
        changed_paths=tuple(repository._relative(path) for path in changed),
        unchanged_paths=tuple(repository._relative(path) for path in unchanged),
        machine_index_status="planned" if dry_run else "current",
        dry_run=dry_run,
        forced=force,
    )
