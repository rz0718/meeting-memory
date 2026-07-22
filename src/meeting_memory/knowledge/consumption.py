"""Normalized read-only models for durable-knowledge consumption."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from .constants import CATEGORIES, CONFIDENCES, STATUSES
from .models import Evidence, KnowledgeObject
from .repository import KnowledgeRepository


STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "work",
        "works",
        "working",
    }
)


def normalize_unicode(value: str) -> str:
    return unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))


def normalize_display(value: str) -> str:
    return " ".join(normalize_unicode(value).split())


def normalize_phrase(value: str) -> str:
    text = normalize_unicode(value).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def tokenize(value: str, remove_stop_words: bool = False) -> Tuple[str, ...]:
    tokens = tuple(part for part in normalize_phrase(value).split() if part)
    if remove_stop_words:
        tokens = tuple(part for part in tokens if part not in STOP_WORDS)
    return tokens


@dataclass(frozen=True)
class NormalizedQuery:
    original: str
    phrase: str
    tokens: Tuple[str, ...]


def normalize_query(value: str) -> NormalizedQuery:
    original = normalize_display(value)
    phrase = normalize_phrase(value)
    return NormalizedQuery(original=original, phrase=phrase, tokens=tokenize(value, True))


@dataclass(frozen=True)
class EvidenceReference:
    source: str
    source_sha256: Optional[str]
    anchor: str
    line_start: int
    line_end: int
    observed_at: Optional[dt.date]

    @classmethod
    def from_evidence(cls, evidence: Evidence) -> "EvidenceReference":
        observed = (
            dt.date.fromisoformat(evidence.observed_at) if evidence.observed_at else None
        )
        return cls(
            source=normalize_display(evidence.source),
            source_sha256=evidence.source_sha256,
            anchor=normalize_display(evidence.anchor),
            line_start=evidence.line_start,
            line_end=evidence.line_end,
            observed_at=observed,
        )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_sha256": self.source_sha256,
            "anchor": self.anchor,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True)
class SearchDocument:
    id: str
    title: str
    category: str
    status: str
    statement: str
    owner: Optional[str]
    confidence: str
    effective_date: Optional[dt.date]
    last_confirmed: Optional[dt.date]
    created_at: dt.datetime
    updated_at: dt.datetime
    related_objects: Tuple[str, ...]
    evidence: Tuple[EvidenceReference, ...]
    history_text: str
    manual_notes: str
    file_path: str

    @property
    def evidence_sources(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(item.source for item in self.evidence))

    def to_dict(self, include_detail: bool = True) -> dict:
        result = {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "status": self.status,
            "owner": self.owner,
            "confidence": self.confidence,
            "statement": self.statement,
            "effective_date": self.effective_date.isoformat()
            if self.effective_date
            else None,
            "last_confirmed": self.last_confirmed.isoformat()
            if self.last_confirmed
            else None,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": self.updated_at.isoformat().replace("+00:00", "Z"),
            "related_objects": list(self.related_objects),
            "evidence_sources": list(self.evidence_sources),
            "file_path": self.file_path,
        }
        if include_detail:
            result.update(
                {
                    "evidence": [item.to_dict() for item in self.evidence],
                    "history": self.history_text,
                    "manual_notes": self.manual_notes,
                }
            )
        return result


def _parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _manual_notes(value: Optional[str]) -> str:
    if not value:
        return ""
    lines = value.splitlines()
    if lines and lines[0].strip().startswith("<!-- BEGIN MANUAL NOTES"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("<!-- END MANUAL NOTES"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def document_from_object(
    repository: KnowledgeRepository, obj: KnowledgeObject
) -> SearchDocument:
    if obj.path is None:
        raise ValueError("knowledge object has no canonical path: %s" % obj.id)
    relative = repository._relative(Path(obj.path))
    return SearchDocument(
        id=normalize_display(obj.id),
        title=normalize_display(obj.title),
        category=normalize_display(obj.category),
        status=normalize_display(obj.status),
        statement=normalize_display(obj.statement),
        owner=normalize_display(obj.owner) if obj.owner else None,
        confidence=normalize_display(obj.confidence),
        effective_date=dt.date.fromisoformat(obj.effective_date)
        if obj.effective_date
        else None,
        last_confirmed=dt.date.fromisoformat(obj.last_confirmed)
        if obj.last_confirmed
        else None,
        created_at=_parse_timestamp(obj.created_at),
        updated_at=_parse_timestamp(obj.updated_at),
        related_objects=tuple(normalize_display(value) for value in obj.related_objects),
        evidence=tuple(EvidenceReference.from_evidence(value) for value in obj.evidence),
        history_text="\n".join(normalize_display(value) for value in obj.history),
        manual_notes=_manual_notes(obj.manual_section),
        file_path=relative,
    )


def load_documents(repository: KnowledgeRepository) -> Tuple[SearchDocument, ...]:
    objects = repository.load_knowledge()
    for obj in objects:
        for evidence in obj.evidence:
            # Historical evidence fingerprints may legitimately be stale after
            # a source note is resynced, but its confined source path must
            # still exist for every consumption command.
            repository.validate_evidence(evidence, require_current_hash=False)
    return tuple(document_from_object(repository, obj) for obj in objects)


def latest_document_date(
    documents: Sequence[SearchDocument], fallback: Optional[dt.date] = None
) -> dt.date:
    values = [item.updated_at.date() for item in documents]
    if values:
        return max(values)
    return fallback or dt.date(1970, 1, 1)


def validate_filter_value(value: Optional[str], allowed: Iterable[str], name: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError("%s must be one of: %s" % (name, ", ".join(allowed)))


def validate_document_enums(document: SearchDocument) -> None:
    validate_filter_value(document.category, CATEGORIES, "category")
    validate_filter_value(document.status, STATUSES, "status")
    validate_filter_value(document.confidence, CONFIDENCES, "confidence")
