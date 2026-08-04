"""Localhost review UI over the existing durable-knowledge Python API.

The UI is a client of ``meeting_memory.knowledge``, never a reimplementation of
it. Every write is expressed as a call into ``ReviewResolver``, ``ReviewRefresher``,
``KnowledgeMerger``, or ``KnowledgeRemover`` with the same argument set the
equivalent CLI invocation would produce.
"""

from .service import UiService

__all__ = ["UiService", "create_app"]


def create_app(*args, **kwargs):
    """Import the FastAPI application lazily so importing the package is cheap."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
