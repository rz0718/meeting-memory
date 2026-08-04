"""Bounded deterministic context-packet selection and rendering."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .consumption import SearchDocument, normalize_phrase, normalize_query, stemmed
from .errors import KnowledgeError
from .models import ReviewItem
from .presentation import EvidenceExcerpt, evidence_excerpts
from .repository import KnowledgeRepository
from .search import SearchFilters, SearchResult, search_documents
from .util import EVIDENCE_VERIFIED, atomic_write, run_id


class InvalidContextBudgetError(KnowledgeError):
    """The requested context budget cannot preserve a complete core statement."""


@dataclass(frozen=True)
class SelectedDocument:
    document: SearchDocument
    selection_reason: str
    score: Optional[int]
    matched_fields: Tuple[str, ...]
    evidence_limit: int
    include_related: bool = True
    include_history: bool = True
    include_manual_notes: bool = False
    include_evidence_excerpts: bool = False


@dataclass(frozen=True)
class ContextPacket:
    query: str
    selected: Tuple[SelectedDocument, ...]
    reviews: Tuple[ReviewItem, ...]
    omissions: Tuple[str, ...]
    markdown: str
    output_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "selected_objects": [
                {
                    "id": item.document.id,
                    "score": item.score,
                    "selection_reason": item.selection_reason,
                    "matched_fields": list(item.matched_fields),
                }
                for item in self.selected
            ],
            "review_items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "reason": item.reason,
                    "possible_existing_ids": list(item.possible_existing_ids),
                    "sources": list(item.sources),
                    "candidate_statement": item.candidate_statement,
                }
                for item in self.reviews
            ],
            "omissions": list(self.omissions),
            "markdown": self.markdown,
            "output_path": self.output_path,
        }


INSTRUCTIONS = """Use only the supplied durable knowledge.

Rules:

- Prefer approved knowledge over proposed knowledge.
- Treat unclear knowledge as unresolved.
- Surface open conflicts explicitly.
- Do not infer missing facts.
- Distinguish current policy from historical discussion.
- Cite the supplied knowledge object IDs and source documents.
- State when the available evidence is insufficient."""


def _diversified_direct(
    results: Sequence[SearchResult], limit: int
) -> List[SearchResult]:
    if not results or limit < 1:
        return []
    selected = [results[0]]
    remaining = list(results[1:])
    seen = {results[0].document.category}
    while remaining and len(selected) < limit:
        threshold = int(results[0].score * 0.8)
        index = next(
            (
                position
                for position, value in enumerate(remaining)
                if value.score >= threshold and value.document.category not in seen
            ),
            0,
        )
        value = remaining.pop(index)
        selected.append(value)
        seen.add(value.document.category)
    return selected


def select_context_documents(
    documents: Sequence[SearchDocument], query: str, limit: int
) -> Tuple[SelectedDocument, ...]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    direct_results = search_documents(documents, query, limit=None)
    direct = _diversified_direct(direct_results, limit)
    selected = [
        SelectedDocument(
            document=result.document,
            selection_reason="direct deterministic search match: %s"
            % ", ".join(result.matched_fields),
            score=result.score,
            matched_fields=result.matched_fields,
            evidence_limit=3,
        )
        for result in direct
    ]
    selected_ids = {item.document.id for item in selected}
    by_id = {item.id: item for item in documents}
    for result in direct:
        if len(selected) >= limit:
            break
        for related_id in result.document.related_objects:
            if len(selected) >= limit:
                break
            if related_id in selected_ids:
                continue
            related = by_id.get(related_id)
            if related is None:
                continue
            selected.append(
                SelectedDocument(
                    document=related,
                    selection_reason="one-hop relation from %s" % result.document.id,
                    score=None,
                    matched_fields=("related_objects",),
                    evidence_limit=3,
                )
            )
            selected_ids.add(related_id)
    return tuple(selected)


def _strong_text_match(query: str, value: str) -> bool:
    query_value = normalize_query(query)
    if query_value.phrase and query_value.phrase in normalize_phrase(value):
        return True
    query_stems = set(query_value.stems)
    if not query_stems:
        return False
    matched = query_stems & set(stemmed(value))
    return len(matched) >= max(1, (len(query_stems) + 1) // 2)


def connected_reviews(
    reviews: Sequence[ReviewItem],
    selected: Sequence[SelectedDocument],
    query: str,
    source_predicate: Optional[Callable[[str], bool]] = None,
) -> Tuple[ReviewItem, ...]:
    ids = {item.document.id for item in selected}
    sources = {
        source for item in selected for source in item.document.evidence_sources
    }
    values = []
    for review in reviews:
        if review.status != "pending":
            continue
        if source_predicate is not None:
            sources_in_scope = [
                source for source in review.sources if source_predicate(source)
            ]
            if not sources_in_scope:
                continue
            # Review sources are rendered in packets, so prune a straddling
            # review just as scope_documents prunes a straddling object.
            review = replace(review, sources=sources_in_scope)
        connected = bool(ids & set(review.possible_existing_ids))
        connected = connected or bool(sources & set(review.sources))
        connected = connected or _strong_text_match(query, review.title)
        connected = connected or _strong_text_match(query, review.candidate_statement)
        if connected:
            values.append(review)
    values.sort(key=lambda item: (item.created_at, item.title.casefold(), item.id))
    return tuple(values)


def _object_block(
    repository: KnowledgeRepository, number: int, selected: SelectedDocument
) -> str:
    document = selected.document
    lines = [
        "### %d. %s" % (number, document.title),
        "",
        "- ID: `%s`" % document.id,
        "- Category: %s" % document.category,
        "- Status: %s" % document.status,
        "- Owner: %s" % (document.owner or "Unknown"),
        "- Last confirmed: %s"
        % (
            document.last_confirmed.isoformat()
            if document.last_confirmed
            else "Unknown"
        ),
        "- Confidence: %s" % document.confidence,
        "- Source file: `%s`" % document.file_path,
        "- Selected because: %s" % selected.selection_reason,
        "",
        "Statement:",
        "",
        document.statement,
    ]
    evidence = document.evidence[: selected.evidence_limit]
    if evidence:
        lines.extend(["", "Evidence:", ""])
        for item in evidence:
            lines.append("- `%s`" % item.source)
    if selected.include_evidence_excerpts and evidence:
        lines.extend(["", "Evidence excerpts:", ""])
        for excerpt in evidence_excerpts(repository, document, selected.evidence_limit):
            label = "- `%s` lines %d-%d" % (
                excerpt.source,
                excerpt.line_start,
                excerpt.line_end,
            )
            if excerpt.stale:
                label += " — drifted; anchor no longer at these lines"
            elif excerpt.freshness == EVIDENCE_VERIFIED:
                label += " — source changed elsewhere; anchor re-verified"
            lines.append(label)
            if excerpt.error:
                lines.append("  - Unavailable: %s" % excerpt.error)
            else:
                lines.extend(["", "  " + (excerpt.text or "").replace("\n", "\n  ")])
    if selected.include_related and document.related_objects:
        lines.extend(["", "Related objects:", ""])
        for item in document.related_objects:
            lines.append("- `%s`" % item)
    if selected.include_history and document.history_text:
        lines.extend(["", "History:", ""])
        for item in document.history_text.splitlines():
            lines.append("- %s" % item)
    if selected.include_manual_notes and document.manual_notes:
        lines.extend(["", "Manual notes:", "", document.manual_notes])
    return "\n".join(lines).rstrip()


def _review_block(number: int, review: ReviewItem) -> str:
    lines = [
        "### %d. %s" % (number, review.title),
        "",
        "- Review ID: `%s`" % review.id,
        "- Reason: %s" % review.reason,
        "- Possible canonical IDs: %s"
        % (
            ", ".join("`%s`" % item for item in review.possible_existing_ids)
            or "None"
        ),
        "",
        "New candidate:",
        "",
        review.candidate_statement,
        "",
        "Supporting sources:",
        "",
    ]
    lines.extend("- `%s`" % value for value in review.sources)
    return "\n".join(lines).rstrip()


def _render(
    repository: KnowledgeRepository,
    query: str,
    selected: Sequence[SelectedDocument],
    reviews: Sequence[ReviewItem],
    omissions: Sequence[str],
) -> str:
    categories = len({item.document.category for item in selected})
    lines = [
        "# Durable Knowledge Context",
        "",
        "## Query",
        "",
        query,
        "",
        "## Retrieval Summary",
        "",
        "- %d knowledge objects selected" % len(selected),
        "- %d open review items selected" % len(reviews),
        "- %d categories represented" % categories,
        "",
        "## Instructions for the Consuming Model",
        "",
        INSTRUCTIONS,
        "",
        "## Knowledge Objects",
        "",
    ]
    if selected:
        for index, item in enumerate(selected, start=1):
            lines.extend([_object_block(repository, index, item), ""])
    else:
        lines.extend(["_No matching canonical knowledge was found._", ""])
    lines.extend(["## Open Review Items", ""])
    if reviews:
        for index, item in enumerate(reviews, start=1):
            lines.extend([_review_block(index, item), ""])
    else:
        lines.extend(["_No connected pending review items were selected._", ""])
    lines.extend(["## Retrieval Notes", ""])
    if omissions:
        lines.extend("- %s" % item for item in omissions)
    else:
        lines.append("- No content was omitted.")
    return "\n".join(lines).rstrip() + "\n"


def build_context_packet(
    repository: KnowledgeRepository,
    documents: Sequence[SearchDocument],
    query: str,
    limit: int = 8,
    max_chars: int = 30000,
    max_evidence_per_object: int = 3,
    include_review_items: bool = True,
    include_manual_notes: bool = False,
    include_evidence_excerpts: bool = False,
    source_predicate: Optional[Callable[[str], bool]] = None,
) -> ContextPacket:
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if max_evidence_per_object < 0:
        raise ValueError("max_evidence_per_object may not be negative")
    selected = [
        replace(
            item,
            evidence_limit=min(len(item.document.evidence), max_evidence_per_object),
            include_manual_notes=include_manual_notes,
            include_evidence_excerpts=include_evidence_excerpts,
        )
        for item in select_context_documents(documents, query, limit)
    ]
    reviews = list(
        connected_reviews(
            repository.load_reviews("pending"),
            selected,
            query,
            source_predicate=source_predicate,
        )
        if include_review_items
        else ()
    )
    omissions: List[str] = []

    def render_current() -> str:
        return _render(repository, query, selected, reviews, omissions)

    while len(render_current()) > max_chars:
        changed = False
        for index in range(len(selected) - 1, -1, -1):
            item = selected[index]
            if item.include_manual_notes and item.document.manual_notes:
                selected[index] = replace(item, include_manual_notes=False)
                omissions.append("Manual notes omitted for `%s` to fit the character budget." % item.document.id)
                changed = True
                break
        if changed:
            continue
        for index in range(len(selected) - 1, -1, -1):
            item = selected[index]
            if item.include_history and item.document.history_text:
                selected[index] = replace(item, include_history=False)
                omissions.append("History omitted for `%s` to fit the character budget." % item.document.id)
                changed = True
                break
        if changed:
            continue
        for index in range(len(selected) - 1, -1, -1):
            item = selected[index]
            if item.include_evidence_excerpts:
                selected[index] = replace(item, include_evidence_excerpts=False)
                omissions.append("Evidence excerpts omitted for `%s` to fit the character budget." % item.document.id)
                changed = True
                break
        if changed:
            continue
        for index in range(len(selected) - 1, -1, -1):
            item = selected[index]
            if item.include_related and item.document.related_objects:
                selected[index] = replace(item, include_related=False)
                omissions.append("Related IDs omitted for `%s` to fit the character budget." % item.document.id)
                changed = True
                break
        if changed:
            continue
        for index in range(len(selected) - 1, -1, -1):
            item = selected[index]
            if item.evidence_limit > 0:
                selected[index] = replace(item, evidence_limit=item.evidence_limit - 1)
                omissions.append("One evidence reference omitted for `%s` to fit the character budget." % item.document.id)
                changed = True
                break
        if changed:
            continue
        if reviews:
            removed = reviews.pop()
            omissions.append("Review item `%s` omitted to fit the character budget." % removed.id)
            continue
        if len(selected) > 1:
            removed = selected.pop()
            omissions[:] = [
                value for value in omissions if "`%s`" % removed.document.id not in value
            ]
            omissions.append("Knowledge object `%s` omitted to fit the character budget." % removed.document.id)
            continue
        break

    markdown = render_current()
    if len(markdown) > max_chars:
        raise InvalidContextBudgetError(
            "max_chars=%d cannot preserve the context header and a complete core statement"
            % max_chars
        )
    return ContextPacket(
        query=query,
        selected=tuple(selected),
        reviews=tuple(reviews),
        omissions=tuple(omissions),
        markdown=markdown,
    )


def write_context_packet(
    repository: KnowledgeRepository,
    packet: ContextPacket,
    output: Optional[Path] = None,
    timestamp: Optional[dt.datetime] = None,
) -> ContextPacket:
    if output is None:
        output = repository.outputs_dir.parent / "Knowledge-Context" / (
            "context-%s.md" % run_id(timestamp)
        )
    else:
        output = Path(output)
        if not output.is_absolute():
            output = repository.root / output
    atomic_write(output, packet.markdown.encode("utf-8"))
    try:
        relative = repository._relative(output)
    except Exception:
        relative = str(output)
    return replace(packet, output_path=relative)
