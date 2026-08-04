"""Read models for Tab 3 -- the answer, and the retrieval it rests on.

``ask`` already produces a trustworthy answer: ``_validate_answer`` rejects any
citation that is not in the context packet, so every ID and source path reaching
this module resolves to a real object and a real file. What the CLI loses is the
presentation -- two flat lists the reader has to open by hand. Everything here
exists to turn those lists back into evidence, and to show the parts of
retrieval the CLI never prints at all: what was supplied and *not* cited, and
what the character budget silently dropped.

Nothing in this module writes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

from ..knowledge.answers import KnowledgeAnswer, answer_payload
from ..knowledge.consumption import SearchDocument
from ..knowledge.context import ContextPacket, SelectedDocument
from ..knowledge.models import ReviewItem
from ..knowledge.repository import KnowledgeRepository
from ..knowledge.util import sha256_bytes
from .payloads import reference_excerpt_payload


def _date(value: Optional[dt.date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _object_payload(
    repository: KnowledgeRepository, item: SelectedDocument
) -> Dict[str, Any]:
    """One retrieved object, described exactly as the packet rendered it.

    ``evidence`` is truncated to ``evidence_limit`` -- the entries that actually
    reached the model -- while ``evidence_total`` reports how many the object
    holds, so a budget-trimmed object cannot be mistaken for a thinly evidenced
    one.
    """
    document: SearchDocument = item.document
    return {
        "id": document.id,
        "title": document.title,
        "category": document.category,
        "status": document.status,
        "statement": document.statement,
        "owner": document.owner,
        "confidence": document.confidence,
        "effective_date": _date(document.effective_date),
        "last_confirmed": _date(document.last_confirmed),
        "file_path": document.file_path,
        "selection_reason": item.selection_reason,
        "score": item.score,
        "matched_fields": list(item.matched_fields),
        "related_objects": list(document.related_objects) if item.include_related else [],
        "evidence_total": len(document.evidence),
        "evidence_in_context": item.evidence_limit,
        "evidence": [
            reference_excerpt_payload(repository, reference)
            for reference in document.evidence[: item.evidence_limit]
        ],
        "history_in_context": bool(item.include_history and document.history_text),
        "manual_notes_in_context": bool(
            item.include_manual_notes and document.manual_notes
        ),
        "excerpts_in_context": bool(item.include_evidence_excerpts),
    }


def _review_payload(item: ReviewItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "reason": item.reason,
        "created_at": item.created_at,
        "category": item.candidate_category,
        "possible_existing_ids": list(item.possible_existing_ids),
        "sources": list(item.sources),
        "candidate_statement": item.candidate_statement,
    }


def context_payload(
    repository: KnowledgeRepository,
    packet: ContextPacket,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    """Everything that was retrieved, plus the exact bytes sent to the model.

    ``packet_sha256`` lets the client tell whether the context it displayed
    after retrieval is the context the answer was actually produced from; the
    two calls are separate, and the repository can change between them.
    """
    markdown = packet.markdown
    return {
        "query": packet.query,
        "options": dict(options),
        "retrieval": {
            "objects_selected": len(packet.selected),
            "categories": len({item.document.category for item in packet.selected}),
            "reviews_selected": len(packet.reviews),
            "omissions": len(packet.omissions),
            "chars": len(markdown),
            "max_chars": options.get("max_chars"),
        },
        "objects": [_object_payload(repository, item) for item in packet.selected],
        "reviews": [_review_payload(item) for item in packet.reviews],
        "omissions": list(packet.omissions),
        "markdown": markdown,
        "packet_sha256": sha256_bytes(markdown.encode("utf-8")),
    }


def with_citations(
    context: Dict[str, Any], answer: KnowledgeAnswer
) -> Dict[str, Any]:
    """Join the answer's citation lists back onto the retrieved context.

    ``knowledge_objects_used`` and ``meeting_evidence_used`` are document-level
    lists, not per-claim spans, so nothing here attributes a citation to a
    sentence -- that would be invented precision. The lists are kept in the
    model's own order for the "grounded in" strip, and every retrieved object
    is flagged cited or not so the reader can see what was supplied and passed
    over.

    ``present`` is false only when a citation does not resolve against the
    packet. The answer validator makes that impossible for a validated model
    answer, so the client renders it as the retrieval bug it would be rather
    than hiding it.
    """
    payload = answer_payload(answer)
    cited_ids = set(payload["knowledge_objects_used"])
    cited_sources = set(payload["meeting_evidence_used"])
    by_id = {value["id"]: value for value in context["objects"]}

    for value in context["objects"]:
        value["cited"] = value["id"] in cited_ids
        for entry in value["evidence"]:
            entry["cited"] = entry["source"] in cited_sources

    cited_objects = [
        {
            "id": object_id,
            "title": by_id[object_id]["title"] if object_id in by_id else None,
            "category": by_id[object_id]["category"] if object_id in by_id else None,
            "present": object_id in by_id,
        }
        for object_id in payload["knowledge_objects_used"]
    ]
    cited_evidence = [
        {
            "source": source,
            "object_ids": [
                value["id"]
                for value in context["objects"]
                if any(entry["source"] == source for entry in value["evidence"])
            ],
        }
        for source in payload["meeting_evidence_used"]
    ]
    for entry in cited_evidence:
        entry["present"] = bool(entry["object_ids"])

    return {
        "answer": payload,
        "cited_objects": cited_objects,
        "cited_evidence": cited_evidence,
        "uncited_object_ids": [
            value["id"] for value in context["objects"] if not value["cited"]
        ],
        "context": context,
    }


def render_saved_answer(result: Dict[str, Any]) -> str:
    """A self-contained record: the answer, its citations, and its retrieval.

    An answer saved without the context it came from is a claim without a
    receipt, so the omissions and the considered-but-not-cited objects are part
    of the document rather than a UI-only affordance.
    """
    answer = result["answer"]
    context = result["context"]
    retrieval = context["retrieval"]
    lines: List[str] = [
        "# Question",
        "",
        answer["question"],
        "",
        "# Answer",
        "",
        answer["answer"],
        "",
        "# Confidence",
        "",
        answer["confidence"],
        "",
        "# Knowledge objects used",
        "",
    ]
    lines.extend(_bullets(answer["knowledge_objects_used"], code=True))
    lines.extend(["", "# Source evidence used", ""])
    lines.extend(_bullets(answer["meeting_evidence_used"], code=True))
    lines.extend(["", "# Open conflicts", ""])
    lines.extend(_bullets(answer["open_conflicts"]))
    lines.extend(
        [
            "",
            "# Retrieval",
            "",
            "- Model: %s" % (answer["model"] or "none (insufficient context)"),
            "- Objects retrieved: %d (%d cited)"
            % (
                retrieval["objects_selected"],
                len(answer["knowledge_objects_used"]),
            ),
            "- Open review items included: %d" % retrieval["reviews_selected"],
            "- Context size: %d of %s characters"
            % (retrieval["chars"], retrieval["max_chars"]),
            "- Context SHA-256: `%s`" % context["packet_sha256"],
            "",
            "## Considered but not cited",
            "",
        ]
    )
    uncited = [
        value for value in context["objects"] if value["id"] in result["uncited_object_ids"]
    ]
    if uncited:
        lines.extend(
            "- `%s` — %s" % (value["id"], value["selection_reason"]) for value in uncited
        )
    else:
        lines.append("- None; every retrieved object was cited.")
    lines.extend(["", "## Context omissions", ""])
    lines.extend(_bullets(context["omissions"]))
    return "\n".join(lines).rstrip() + "\n"


def _bullets(values: Sequence[str], code: bool = False) -> List[str]:
    if not values:
        return ["- None"]
    return [("- `%s`" if code else "- %s") % value for value in values]
