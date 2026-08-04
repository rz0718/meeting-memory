"""Tab 1 read routes: run manifests, object detail, evidence excerpts."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

from .payloads import (
    knowledge_index_payload,
    knowledge_payload,
    run_detail_payload,
    runs_payload,
)
from .service import UiService


router = APIRouter(prefix="/api", tags=["runs"])


def service_of(request: Request) -> UiService:
    return request.app.state.service


@router.get("/runs")
def list_runs(
    request: Request,
    date: Optional[str] = Query(None, description="run start date, YYYY-MM-DD"),
    start: Optional[str] = Query(None, description="inclusive range start"),
    end: Optional[str] = Query(None, description="inclusive range end"),
    limit: Optional[int] = Query(None, ge=1),
):
    service = service_of(request)
    with service.read_cache():
        return runs_payload(
            service.repository, date=date, start=start, end=end, limit=limit
        )


@router.get("/runs/{run_id}")
def show_run(request: Request, run_id: str):
    service = service_of(request)
    with service.read_cache():
        return run_detail_payload(service.repository, run_id)


@router.get("/knowledge")
def list_knowledge(request: Request):
    service = service_of(request)
    with service.read_cache():
        return knowledge_index_payload(service.repository)


@router.get("/knowledge/{object_id}")
def show_knowledge(request: Request, object_id: str):
    service = service_of(request)
    with service.read_cache():
        return knowledge_payload(service.repository, object_id)
