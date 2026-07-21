"""Human and JSON presentation helpers, including safe evidence excerpts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .consumption import EvidenceReference, SearchDocument
from .errors import EvidenceError, KnowledgeError
from .repository import KnowledgeRepository


class ObjectNotFoundError(KnowledgeError):
    """An exact stable knowledge ID was not found."""


@dataclass(frozen=True)
class EvidenceExcerpt:
    source: str
    anchor: str
    line_start: int
    line_end: int
    text: Optional[str]
    stale: bool
    error: Optional[str]

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "anchor": self.anchor,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "text": self.text,
            "stale": self.stale,
            "error": self.error,
        }


def exact_document(
    documents: Sequence[SearchDocument], object_id: str
) -> SearchDocument:
    by_id = {item.id: item for item in documents}
    try:
        return by_id[object_id]
    except KeyError as exc:
        raise ObjectNotFoundError("knowledge object not found: %s" % object_id) from exc


def read_evidence_excerpt(
    repository: KnowledgeRepository,
    evidence: EvidenceReference,
    max_lines: int = 20,
    max_chars: int = 4000,
) -> EvidenceExcerpt:
    try:
        source_path = repository.evidence_path(evidence.source)
    except EvidenceError:
        return EvidenceExcerpt(
            evidence.source,
            evidence.anchor,
            evidence.line_start,
            evidence.line_end,
            None,
            False,
            "unsafe evidence path",
        )
    if not source_path.is_file():
        return EvidenceExcerpt(
            evidence.source,
            evidence.anchor,
            evidence.line_start,
            evidence.line_end,
            None,
            False,
            "source file is missing",
        )
    try:
        data = source_path.read_bytes()
        lines = data.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return EvidenceExcerpt(
            evidence.source,
            evidence.anchor,
            evidence.line_start,
            evidence.line_end,
            None,
            False,
            "source file cannot be read: %s" % exc,
        )
    if evidence.line_start > len(lines):
        return EvidenceExcerpt(
            evidence.source,
            evidence.anchor,
            evidence.line_start,
            evidence.line_end,
            None,
            False,
            "stored line range is outside the source",
        )
    finish = min(evidence.line_end, len(lines), evidence.line_start + max_lines - 1)
    excerpt = "\n".join(lines[evidence.line_start - 1 : finish])
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max(0, max_chars - 1)].rstrip() + "…"
    digest = hashlib.sha256(data).hexdigest()
    stale = bool(evidence.source_sha256 and digest != evidence.source_sha256)
    return EvidenceExcerpt(
        evidence.source,
        evidence.anchor,
        evidence.line_start,
        finish,
        excerpt,
        stale,
        None,
    )


def evidence_excerpts(
    repository: KnowledgeRepository,
    document: SearchDocument,
    limit: Optional[int] = None,
) -> Tuple[EvidenceExcerpt, ...]:
    values = document.evidence if limit is None else document.evidence[:limit]
    return tuple(read_evidence_excerpt(repository, item) for item in values)


def show_payload(
    repository: KnowledgeRepository,
    document: SearchDocument,
    with_evidence: bool = False,
) -> dict:
    result = document.to_dict()
    if with_evidence:
        result["evidence_excerpts"] = [
            item.to_dict() for item in evidence_excerpts(repository, document)
        ]
    return result


def render_show(
    repository: KnowledgeRepository,
    document: SearchDocument,
    with_evidence: bool = False,
) -> str:
    lines = [
        document.title,
        "",
        "ID: %s" % document.id,
        "Category: %s" % document.category,
        "Status: %s" % document.status,
        "Owner: %s" % (document.owner or "Unknown"),
        "Effective date: %s"
        % (document.effective_date.isoformat() if document.effective_date else "Unknown"),
        "Last confirmed: %s"
        % (
            document.last_confirmed.isoformat()
            if document.last_confirmed
            else "Unknown"
        ),
        "Confidence: %s" % document.confidence,
        "Related objects: %s"
        % (", ".join(document.related_objects) or "None"),
        "Evidence: %s" % (", ".join(document.evidence_sources) or "None"),
        "Canonical file: %s" % document.file_path,
        "",
        "Statement:",
        document.statement,
    ]
    if with_evidence:
        lines.extend(["", "Evidence excerpts:"])
        for excerpt in evidence_excerpts(repository, document):
            label = "- %s lines %d-%d" % (
                excerpt.source,
                excerpt.line_start,
                excerpt.line_end,
            )
            if excerpt.stale:
                label += " [STALE SOURCE FINGERPRINT]"
            lines.append(label)
            if excerpt.error:
                lines.append("  Unavailable: %s" % excerpt.error)
            else:
                lines.append("  " + (excerpt.text or "").replace("\n", "\n  "))
    return "\n".join(lines).rstrip() + "\n"
