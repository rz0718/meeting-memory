"""Tab 1 actions and session state: reviewer, merge, and the removal basket.

These are the two audited mutation paths that already exist. Merge previews and
confirms; removal keeps the friction the CLI designs in -- an ID inventory whose
count and SHA-256 the apply call must assert.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..knowledge.errors import RemovalError
from ..knowledge.merge import KnowledgeMerger
from ..knowledge.removal import KnowledgeRemover
from ..knowledge.util import local_timezone_label, local_timezone_offset_minutes
from .arguments import (
    BasketItemRequest,
    MergeRequest,
    RemovalRequest,
    ReviewerRequest,
    merge_arguments,
)
from .service import UiService


router = APIRouter(prefix="/api", tags=["actions"])


def service_of(request: Request) -> UiService:
    return request.app.state.service


@router.get("/session")
def get_session(request: Request):
    service = service_of(request)
    with service.read_cache():
        basket = service.basket_state()
    return {
        "reviewer": service.reviewer,
        "review_model": service.review_model,
        "review_model_configured": bool(service.review_model),
        "ask_model": service.ask_model,
        "ask_model_configured": bool(service.ask_model),
        "context_lines": service.context_lines,
        "root": str(service.repository.root),
        "meetings_dir": str(service.repository.meetings_dir),
        # Every stored timestamp is UTC. The client renders them in the
        # operating calendar's zone rather than the browser's, so two reviewers
        # on different laptops read the same wall-clock time for the same run.
        "timezone": {
            "label": local_timezone_label(),
            "offset_minutes": local_timezone_offset_minutes(),
        },
        "removal_basket": basket,
    }


@router.post("/session/reviewer")
def set_reviewer(request: Request, body: ReviewerRequest):
    service = service_of(request)
    return {"reviewer": service.set_reviewer(body.reviewer)}


@router.post("/merge")
def merge_objects(request: Request, body: MergeRequest):
    service = service_of(request)
    arguments = merge_arguments(body, service.reviewer)
    key = "merge:%s->%s" % (arguments["loser_id"], arguments["survivor_id"])
    merger = KnowledgeMerger(service.repository)
    if arguments["dry_run"]:
        result = merger.merge(**arguments)
        return {
            "preview": result.to_dict(),
            "decision_fingerprint": service.record_preview(key, arguments),
        }
    service.require_preview(key, arguments)
    result = merger.merge(**arguments)
    service.clear_preview(key)
    return {"applied": result.to_dict()}


@router.get("/removal-basket")
def get_basket(request: Request):
    service = service_of(request)
    with service.read_cache():
        return service.basket_state()


@router.post("/removal-basket/items")
def add_to_basket(request: Request, body: BasketItemRequest):
    service = service_of(request)
    service.basket_add(body.object_id)
    return service.basket_state()


@router.delete("/removal-basket/items/{object_id}")
def remove_from_basket(request: Request, object_id: str):
    service = service_of(request)
    service.basket_remove(object_id)
    return service.basket_state()


@router.delete("/removal-basket")
def clear_basket(request: Request):
    service = service_of(request)
    service.basket_clear()
    return service.basket_state()


@router.post("/removal-basket/preview")
def preview_removal(request: Request, body: RemovalRequest):
    """Write the approved ID inventory, then dry-run against it."""
    service = service_of(request)
    state = service.write_basket_inventory()
    object_ids, digest = service.read_previewed_inventory(
        state["inventory_count"], state["inventory_sha256"]
    )
    result = KnowledgeRemover(service.repository).remove(
        object_ids,
        (body.reviewer or service.reviewer),
        body.note,
        inventory_sha256=digest,
        dry_run=True,
        refresh_indexes=not body.no_index,
    )
    return {"preview": result.to_dict(), "basket": state}


@router.post("/removal-basket/apply")
def apply_removal(request: Request, body: RemovalRequest):
    """Remove exactly the inventory that was previewed, count and digest asserted."""
    service = service_of(request)
    if body.confirm_count is None or body.inventory_sha256 is None:
        raise RemovalError(
            "removal requires the confirmed count and inventory SHA-256 from the preview"
        )
    object_ids, digest = service.read_previewed_inventory(
        body.confirm_count, body.inventory_sha256
    )
    result = KnowledgeRemover(service.repository).remove(
        object_ids,
        (body.reviewer or service.reviewer),
        body.note,
        inventory_sha256=digest,
        dry_run=False,
        refresh_indexes=not body.no_index,
    )
    service.basket_clear()
    return {"applied": result.to_dict(), "basket": service.basket_state()}
