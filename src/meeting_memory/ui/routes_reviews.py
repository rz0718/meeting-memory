"""Tab 2 routes: the review queue, its detail, and every audited write.

Every write here is one ``ReviewResolver.resolve`` or ``ReviewRefresher.refresh``
call. The UI adds no decision semantics: if something cannot be expressed as one
of those calls, there is no route for it.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..knowledge.errors import ReviewNotFoundError
from ..knowledge.review import (
    ReviewRefresher,
    ReviewResolver,
    exact_review,
    list_reviews,
)
from ..knowledge.review_ai import generate_review_suggestions
from ..knowledge.review_suggestions import (
    latest_current_suggestion,
    suggestion_display_payload,
)
from .arguments import (
    RefreshRequest,
    ResolveRequest,
    SuggestRequest,
    refresher_arguments,
    resolver_arguments,
)
from .payloads import review_detail_payload, review_list_payload
from .service import UiService


router = APIRouter(prefix="/api", tags=["reviews"])


def service_of(request: Request) -> UiService:
    return request.app.state.service


@router.get("/reviews")
def get_reviews(
    request: Request,
    status: str = Query("pending"),
    priority: str = Query("all"),
    reason: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    existing_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1),
):
    service = service_of(request)
    with service.read_cache():
        values = list_reviews(
            service.repository,
            status=None if status == "all" else status,
            priority=priority,
            reason=reason,
            category=category,
            existing_id=existing_id,
            source=source,
            limit=limit,
        )
        return review_list_payload(service.repository, values)


@router.get("/reviews/{review_id}")
def get_review(request: Request, review_id: str):
    service = service_of(request)
    with service.read_cache():
        return review_detail_payload(service.repository, review_id)


@router.post("/reviews/{review_id}/resolve")
def resolve_review(request: Request, review_id: str, body: ResolveRequest):
    """Preview, then apply. There is no path from form to disk without both.

    Apply re-sends identical arguments and the resolver re-validates them under
    the mutation lock, so a preview that went stale fails loudly rather than
    applying something the human never saw.
    """
    service = service_of(request)
    arguments = resolver_arguments(body, service.reviewer)
    key = "review:%s" % review_id
    if arguments["dry_run"]:
        result = ReviewResolver(service.repository).resolve(review_id, **arguments)
        fingerprint = service.record_preview(key, arguments)
        return {
            "preview": result.to_dict(),
            "decision_fingerprint": fingerprint,
            "will_record": _disposition(body),
        }
    service.require_preview(key, arguments)
    result = ReviewResolver(service.repository).resolve(review_id, **arguments)
    service.clear_preview(key)
    payload = {
        "applied": result.to_dict(),
        "will_record": _disposition(body),
    }
    try:
        item = exact_review(service.repository, review_id)
    except ReviewNotFoundError:
        item = None
    if item is not None:
        payload["resolution"] = {
            "review_id": item.id,
            "status": item.status,
            "reviewer": item.reviewer,
            "resolved_at": item.resolved_at,
            "action": item.resolution_action,
            "note": item.resolution_note,
            "suggestion_id": item.suggestion_id,
            "suggested_action": item.suggested_action,
            "suggestion_disposition": item.suggestion_disposition,
            "resolution_mode": item.resolution_mode,
            "file_path": (
                service.repository._relative(item.path)
                if item.path is not None
                else None
            ),
        }
    return payload


def _disposition(body: ResolveRequest) -> Dict[str, Optional[str]]:
    """What the audit trail will say, computed from the same flags that are sent.

    The accept/override distinction is the point of the tab, so the answer is
    derived server-side from the arguments actually transmitted rather than
    trusted from a client-side badge.
    """
    if body.suggestion_id is None:
        return {"disposition": "not_used", "mode": body.resolution_mode or "human"}
    return {
        "disposition": "accepted" if body.accept_suggestion else "overridden",
        "mode": body.resolution_mode or "hybrid",
    }


@router.post("/reviews/{review_id}/refresh")
def refresh_review(request: Request, review_id: str, body: RefreshRequest):
    """Rebase a drifted review onto its current canonical snapshot."""
    service = service_of(request)
    arguments = refresher_arguments(body)
    key = "refresh:%s" % review_id
    if arguments["dry_run"]:
        result = ReviewRefresher(service.repository).refresh(review_id, **arguments)
        return {
            "preview": result.to_dict(),
            "decision_fingerprint": service.record_preview(key, arguments),
        }
    service.require_preview(key, arguments)
    result = ReviewRefresher(service.repository).refresh(review_id, **arguments)
    service.clear_preview(key)
    return {"applied": result.to_dict()}


def _suggestion_targets(
    service: UiService, review_id: Optional[str], body: SuggestRequest
) -> List[Any]:
    repository = service.repository
    if review_id is not None:
        item = exact_review(repository, review_id)
        if item.status != "pending":
            raise ValueError("suggestions can only be generated for pending reviews")
        return [item]
    if body.review_ids:
        values = []
        for value in body.review_ids:
            item = exact_review(repository, value)
            if item.status == "pending":
                values.append(item)
        return values
    return list(
        list_reviews(
            repository,
            status="pending",
            priority=body.priority,
            reason=body.reason,
            category=body.category,
            existing_id=body.existing_id,
            source=body.source,
            limit=body.limit,
        )
    )


@router.post("/reviews/{review_id}/suggest")
def suggest_one(request: Request, review_id: str, body: SuggestRequest):
    service = service_of(request)
    values = _suggestion_targets(service, review_id, body)
    result = generate_review_suggestions(
        service.repository,
        values,
        service.advisor(),
        service.require_model(),
        context_lines=body.context_lines,
        force=body.force,
        filters={"review_id": review_id},
    )
    item = exact_review(service.repository, review_id)
    suggestion = latest_current_suggestion(service.repository, item)
    return {
        "manifest": result.manifest,
        "manifest_path": result.manifest_path,
        "suggestion": (
            suggestion_display_payload(service.repository, suggestion, item)
            if suggestion is not None
            else None
        ),
    }


@router.post("/reviews/suggest")
def suggest_batch(request: Request, body: SuggestRequest):
    """Generate over the filtered set, streaming one line per review.

    Failures are already isolated per review by ``generate_review_suggestions``
    and recorded in the review-run manifest, so a single bad item is reported as
    its own line instead of failing the batch.
    """
    service = service_of(request)
    values = _suggestion_targets(service, None, body)
    model = service.require_model()
    advisor = service.advisor()
    repository = service.repository
    events: "queue.Queue" = queue.Queue()

    def work() -> None:
        try:
            result = generate_review_suggestions(
                repository,
                values,
                advisor,
                model,
                context_lines=body.context_lines,
                force=body.force,
                filters=body.filters(),
                on_progress=events.put,
            )
            events.put(
                {
                    "event": "complete",
                    "manifest": result.manifest,
                    "manifest_path": result.manifest_path,
                }
            )
        except Exception as exc:  # Surfaced to the client, never retried here.
            events.put(
                {
                    "event": "error",
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )
        finally:
            events.put(None)

    def stream():
        yield json.dumps(
            {
                "event": "batch",
                "total": len(values),
                "review_ids": [value.id for value in values],
                "model": model,
            }
        ) + "\n"
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while True:
            item = events.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"
        thread.join()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
