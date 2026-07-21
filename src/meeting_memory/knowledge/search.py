"""Deterministic, explainable lexical search."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import CATEGORIES, CONFIDENCES, STATUSES
from .consumption import (
    NormalizedQuery,
    SearchDocument,
    normalize_phrase,
    normalize_query,
    tokenize,
)


STATUS_ORDER = {"approved": 0, "proposed": 1, "unclear": 2, "deprecated": 3}
MATCH_FIELD_ORDER = (
    "id",
    "title",
    "statement",
    "owner",
    "category",
    "status",
    "related_objects",
    "evidence_sources",
    "history",
    "manual_notes",
)


@dataclass(frozen=True)
class SearchFilters:
    category: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    confidence: Optional[str] = None
    updated_since: Optional[dt.date] = None
    confirmed_since: Optional[dt.date] = None

    def to_dict(self) -> dict:
        return {
            key: value.isoformat() if isinstance(value, dt.date) else value
            for key, value in (
                ("category", self.category),
                ("status", self.status),
                ("owner", self.owner),
                ("confidence", self.confidence),
                ("updated_since", self.updated_since),
                ("confirmed_since", self.confirmed_since),
            )
            if value is not None
        }

    def validate(self) -> None:
        if self.category is not None and self.category not in CATEGORIES:
            raise ValueError("unsupported category: %s" % self.category)
        if self.status is not None and self.status not in STATUSES:
            raise ValueError("unsupported status: %s" % self.status)
        if self.confidence is not None and self.confidence not in CONFIDENCES:
            raise ValueError("unsupported confidence: %s" % self.confidence)


@dataclass(frozen=True)
class SearchResult:
    document: SearchDocument
    score: int
    matched_fields: Tuple[str, ...]
    reasons: Tuple[str, ...]

    def to_dict(self, rank: int) -> dict:
        return {
            "rank": rank,
            "score": self.score,
            "id": self.document.id,
            "title": self.document.title,
            "category": self.document.category,
            "status": self.document.status,
            "owner": self.document.owner,
            "statement": self.document.statement,
            "last_confirmed": self.document.last_confirmed.isoformat()
            if self.document.last_confirmed
            else None,
            "file_path": self.document.file_path,
            "evidence_sources": list(self.document.evidence_sources),
            "matched_fields": list(self.matched_fields),
        }


def _contains_tokens(value: str, tokens: Sequence[str]) -> Tuple[str, ...]:
    values = set(tokenize(value))
    return tuple(token for token in tokens if token in values)


def _matches(document: SearchDocument, filters: SearchFilters) -> bool:
    if filters.category is not None and document.category != filters.category:
        return False
    if filters.status is not None and document.status != filters.status:
        return False
    if filters.confidence is not None and document.confidence != filters.confidence:
        return False
    if filters.owner is not None:
        if document.owner is None or normalize_phrase(document.owner) != normalize_phrase(
            filters.owner
        ):
            return False
    if (
        filters.updated_since is not None
        and document.updated_at.date() < filters.updated_since
    ):
        return False
    if filters.confirmed_since is not None and (
        document.last_confirmed is None
        or document.last_confirmed < filters.confirmed_since
    ):
        return False
    return True


def _recency_score(document: SearchDocument, latest: Optional[dt.date]) -> int:
    if latest is None or document.last_confirmed is None:
        return 0
    days = max(0, (latest - document.last_confirmed).days)
    if days <= 30:
        return 3
    if days <= 90:
        return 2
    if days <= 365:
        return 1
    return 0


def score_document(
    document: SearchDocument, query: NormalizedQuery, latest_confirmed: Optional[dt.date]
) -> Optional[SearchResult]:
    score = 0
    matched: Dict[str, bool] = {}
    reasons: List[str] = []
    tokens = query.tokens

    id_phrase = normalize_phrase(document.id)
    title_phrase = normalize_phrase(document.title)
    if query.phrase and query.phrase == id_phrase:
        score += 100
        matched["id"] = True
        reasons.append("exact ID (+100)")
    else:
        id_tokens = _contains_tokens(document.id, tokens)
        if id_tokens:
            matched["id"] = True

    if query.phrase and query.phrase == title_phrase:
        score += 80
        matched["title"] = True
        reasons.append("exact title (+80)")
    elif query.phrase and query.phrase in title_phrase:
        score += 50
        matched["title"] = True
        reasons.append("title phrase (+50)")

    title_tokens = _contains_tokens(document.title, tokens)
    if title_tokens:
        score += 12 * len(title_tokens)
        matched["title"] = True
        reasons.append("title tokens %s (+%d)" % (", ".join(title_tokens), 12 * len(title_tokens)))

    statement_tokens = _contains_tokens(document.statement, tokens)
    if statement_tokens:
        score += 5 * len(statement_tokens)
        matched["statement"] = True
        reasons.append(
            "statement tokens %s (+%d)"
            % (", ".join(statement_tokens), 5 * len(statement_tokens))
        )

    owner_phrase = normalize_phrase(document.owner or "")
    if document.owner and (
        (query.phrase and query.phrase in owner_phrase)
        or _contains_tokens(document.owner, tokens)
    ):
        score += 10
        matched["owner"] = True
        reasons.append("owner (+10)")

    category_phrase = normalize_phrase(document.category)
    if (query.phrase and query.phrase == category_phrase) or _contains_tokens(
        document.category, tokens
    ):
        score += 8
        matched["category"] = True
        reasons.append("category (+8)")

    if (query.phrase and query.phrase == normalize_phrase(document.status)) or _contains_tokens(
        document.status, tokens
    ):
        matched["status"] = True

    related_text = " ".join(document.related_objects)
    related_tokens = _contains_tokens(related_text, tokens)
    if related_tokens:
        score += 8
        matched["related_objects"] = True
        reasons.append("related object (+8)")

    evidence_names = " ".join(
        PurePosixPath(value).name for value in document.evidence_sources
    )
    evidence_tokens = _contains_tokens(evidence_names, tokens)
    if evidence_tokens:
        score += 4
        matched["evidence_sources"] = True
        reasons.append("evidence filename (+4)")

    if _contains_tokens(document.history_text, tokens):
        matched["history"] = True
    if _contains_tokens(document.manual_notes, tokens):
        matched["manual_notes"] = True

    textual_match = bool(matched)
    if not textual_match or not query.tokens and query.phrase not in (id_phrase, title_phrase):
        return None

    if document.status == "approved":
        score += 3
        reasons.append("approved status (+3)")
    recency = _recency_score(document, latest_confirmed)
    if recency:
        score += recency
        reasons.append("recent confirmation (+%d)" % recency)

    return SearchResult(
        document=document,
        score=score,
        matched_fields=tuple(field for field in MATCH_FIELD_ORDER if field in matched),
        reasons=tuple(reasons),
    )


def search_documents(
    documents: Sequence[SearchDocument],
    query_text: str,
    filters: Optional[SearchFilters] = None,
    limit: Optional[int] = 10,
) -> Tuple[SearchResult, ...]:
    query = normalize_query(query_text)
    if not query.phrase:
        return ()
    filters = filters or SearchFilters()
    filters.validate()
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    latest = max(
        (item.last_confirmed for item in documents if item.last_confirmed),
        default=None,
    )
    results = []
    for document in documents:
        if not _matches(document, filters):
            continue
        result = score_document(document, query, latest)
        if result is not None:
            results.append(result)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    results.sort(
        key=lambda item: (
            -item.score,
            STATUS_ORDER[item.document.status],
            -(item.document.last_confirmed.toordinal() if item.document.last_confirmed else 0),
            -int((item.document.updated_at - epoch).total_seconds()),
            item.document.title.casefold(),
            item.document.id,
        )
    )
    if limit is not None:
        results = results[:limit]
    return tuple(results)


def search_payload(
    query: str, filters: SearchFilters, results: Iterable[SearchResult]
) -> dict:
    return {
        "query": query,
        "filters": filters.to_dict(),
        "results": [
            result.to_dict(rank) for rank, result in enumerate(results, start=1)
        ],
    }


def render_search_results(query: str, results: Sequence[SearchResult]) -> str:
    if not results:
        return "No knowledge objects matched %r.\n" % query
    lines = ["Search results for %r" % query, ""]
    for rank, result in enumerate(results, start=1):
        item = result.document
        lines.extend(
            [
                "%d. %s" % (rank, item.title),
                "   ID: %s" % item.id,
                "   Category: %s | Status: %s | Owner: %s"
                % (item.category, item.status, item.owner or "Unknown"),
                "   Last confirmed: %s | Score: %d"
                % (
                    item.last_confirmed.isoformat() if item.last_confirmed else "Unknown",
                    result.score,
                ),
                "   Matched: %s" % ", ".join(result.matched_fields),
                "   Statement: %s" % item.statement,
                "   Evidence: %s"
                % (", ".join(item.evidence_sources) or "None"),
                "   File: %s" % item.file_path,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
