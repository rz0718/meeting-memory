"""FastAPI application: loopback-only, single reviewer, no auth.

Domain exceptions are mapped to status codes here so routes can call the
library directly and let a typed failure surface as itself. Nothing is ever
retried automatically -- a lock contention or precondition failure is reported
with its exception text so the human decides what to do next.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..knowledge.errors import (
    AuthenticationError,
    ConfigurationError,
    EvidenceError,
    KnowledgeError,
    MergeError,
    RemovalError,
    ReviewNotFoundError,
    ReviewResolutionError,
    SchemaError,
    StaleReviewError,
    StorageError,
    TransientExtractionError,
)
from ..knowledge.presentation import ObjectNotFoundError
from ..knowledge.repository import KnowledgeRepository
from . import (
    routes_actions,
    routes_ask,
    routes_projects,
    routes_reviews,
    routes_runs,
)
from .service import UiService


STATIC_DIR = Path(__file__).resolve().parent / "static"


class RevalidatedStatic(StaticFiles):
    """Serve the UI's own assets with revalidation forced.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, which leaves
    the browser free to apply heuristic freshness and skip revalidating. The
    front end is a graph of ES modules, so one asset served from cache while its
    neighbours are re-fetched links a fresh module against a stale copy of one
    it imports. The import fails, and a failed import takes down the whole
    graph -- including the shell -- for a blank page and no clue in the network
    log beyond a few 304s. no-cache keeps the cheap 304 round trip and drops the
    guessing.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Ordered most specific first; the first matching class wins.
ERROR_STATUS = (
    ((ReviewNotFoundError, ObjectNotFoundError), 404),
    ((StaleReviewError,), 409),
    ((AuthenticationError, ConfigurationError), 503),
    ((TransientExtractionError,), 502),
    ((ReviewResolutionError, MergeError, RemovalError, EvidenceError), 400),
    ((SchemaError, StorageError), 500),
    ((KnowledgeError,), 500),
)


def _status_for(exc: Exception) -> int:
    for classes, status in ERROR_STATUS:
        if isinstance(exc, classes):
            return status
    return 500


def _error_body(exc: Exception) -> Dict[str, Any]:
    return {"error": str(exc), "type": type(exc).__name__}


def create_app(
    repository: KnowledgeRepository,
    *,
    reviewer: str = "rui",
    review_model: Optional[str] = None,
    ask_model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 120,
    context_lines: int = 5,
    advisor_factory: Optional[Any] = None,
    answerer_factory: Optional[Any] = None,
) -> FastAPI:
    app = FastAPI(
        title="Meeting Memory review",
        description="Localhost review UI over the durable-knowledge repository.",
        version="1",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.service = UiService(
        repository,
        reviewer=reviewer,
        review_model=review_model,
        ask_model=ask_model,
        api_key=api_key,
        timeout=timeout,
        context_lines=context_lines,
        advisor_factory=advisor_factory,
        answerer_factory=answerer_factory,
    )

    @app.exception_handler(KnowledgeError)
    def knowledge_error(request: Request, exc: KnowledgeError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content=_error_body(exc))

    @app.exception_handler(ValueError)
    def value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_error_body(exc))

    @app.exception_handler(OSError)
    def os_error(request: Request, exc: OSError) -> JSONResponse:
        return JSONResponse(status_code=500, content=_error_body(exc))

    app.include_router(routes_runs.router)
    app.include_router(routes_reviews.router)
    app.include_router(routes_actions.router)
    app.include_router(routes_ask.router)
    app.include_router(routes_projects.router)

    if STATIC_DIR.is_dir():
        app.mount(
            "/static", RevalidatedStatic(directory=str(STATIC_DIR)), name="static"
        )

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(
                str(STATIC_DIR / "index.html"),
                headers={"Cache-Control": "no-cache"},
            )

    return app
