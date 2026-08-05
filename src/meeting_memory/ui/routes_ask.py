"""Tab 3 routes: grounded answers and the retrieval behind them.

Retrieval and answering are two calls on purpose. Retrieval is deterministic and
fast; the provider call takes seconds. Splitting them lets the UI render the
evidence the moment it exists, so a slow or failing model still leaves the human
with the objects, the excerpts, and the omissions -- most of the value.

This module holds no mutation lock and takes no decisions. Its one write is the
explicit "save answer", to a server-derived path outside the canonical
knowledge and review trees.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from fastapi import APIRouter, Request

from ..knowledge.answers import KnowledgeAnswer, insufficient_answer
from ..knowledge.consumption import load_documents
from ..knowledge.context import ContextPacket, build_context_packet
from ..knowledge.projects import scoped_documents
from ..knowledge.util import atomic_write, run_id, utc_now
from .arguments import AskRequest, SaveAnswerRequest, context_arguments
from .ask_payloads import context_payload, render_saved_answer, with_citations
from .project_payloads import scope_receipt
from .service import UiService


router = APIRouter(prefix="/api", tags=["ask"])


def service_of(request: Request) -> UiService:
    return request.app.state.service


def _retrieve(
    service: UiService, body: AskRequest
) -> Tuple[ContextPacket, Dict[str, Any], Dict[str, Any]]:
    """Build the packet and its payload under one read cache.

    Both are produced before any provider call so the excerpts, the omissions,
    and the exact bytes sent to the model all come from a single consistent read
    of the repository.

    A project narrows the documents handed to the builder and supplies the same
    source predicate ``ask --project`` supplies, so search, one-hop relation
    expansion, and connected review items are all bounded by the one scope. The
    scope can only narrow: an empty scope produces an empty packet, which the
    answer route reports as such rather than retrying without it.
    """
    arguments = context_arguments(body)
    with service.read_cache():
        documents = load_documents(service.repository)
        scoped, scope = scoped_documents(
            service.repository, body.project or None, documents
        )
        receipt = (
            scope_receipt(scope, documents, scoped) if scope is not None else None
        )
        packet = build_context_packet(
            service.repository,
            scoped,
            body.query,
            source_predicate=(scope.matches_source if scope else None),
            **arguments
        )
        payload = context_payload(service.repository, packet, arguments, receipt)
    return packet, payload, arguments


@router.post("/ask/context")
def ask_context(request: Request, body: AskRequest):
    """Deterministic retrieval only. No provider call, no model required."""
    _, payload, _ = _retrieve(service_of(request), body)
    return payload


@router.post("/ask/answer")
def ask_answer(request: Request, body: AskRequest):
    """Retrieve, then answer, then join the citations back onto the context.

    An empty packet never reaches a provider: the CLI answers it with the
    canned insufficient answer, and so does this route, so "no durable knowledge
    covers this" costs nothing and cannot be confused for a model opinion.
    """
    service = service_of(request)
    packet, payload, _ = _retrieve(service, body)
    if packet.selected:
        answer: KnowledgeAnswer = service.answerer().answer(packet)
    else:
        answer = insufficient_answer(packet)
    result = with_citations(payload, answer)
    result["answer_token"] = service.record_answer(result)
    return result


@router.post("/ask/save")
def ask_save(request: Request, body: SaveAnswerRequest):
    """Write the answer the human read, with its retrieval receipt.

    Saving by token rather than by question is the whole point: re-running the
    provider would be a different sample, and the file has to say what the
    screen said. The destination is derived here and never accepted from the
    client, so no request can name a path inside ``knowledge/`` or
    ``knowledge-review/``.
    """
    service = service_of(request)
    result = service.recorded_answer(body.answer_token)
    path = (
        service.repository.outputs_dir.parent
        / "Knowledge-Answers"
        / ("answer-%s.md" % run_id(utc_now()))
    )
    atomic_write(path, render_saved_answer(result).encode("utf-8"))
    return {
        "saved_path": service.repository._relative(path),
        "answer_token": body.answer_token,
    }
