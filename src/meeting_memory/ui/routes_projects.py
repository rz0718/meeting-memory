"""Project scope routes: the source universe, live resolution, and saving.

Saving a scope is the one write in this module, and it is deliberately outside
the audited-decision machinery: a project file under
``.knowledge-state/projects/`` is neither canonical knowledge nor review state,
so it does not cross the boundary that ``ReviewResolver``, ``KnowledgeMerger``,
and ``KnowledgeRemover`` guard. It writes through ``save_project``, under the
repository mutation lock, exactly as ``project create`` does -- an overwrite
still requires ``replace``, so a scope cannot be silently redefined under a
question that already ran against it.

There is no dry-run gate here because there is no consequence to preview: a
scope changes what future retrieval considers, never what the repository says.
What the editor previews instead is the resolution -- which sources and objects
the selection admits -- and that is a read.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..knowledge.consumption import load_documents
from ..knowledge.projects import load_projects, save_project
from .arguments import ProjectRequest, project_scope
from .project_payloads import empty_resolution, projects_payload, resolve_scope
from .service import UiService


router = APIRouter(prefix="/api", tags=["projects"])


def service_of(request: Request) -> UiService:
    return request.app.state.service


@router.get("/projects")
def list_projects(request: Request):
    """Saved scopes, plus the observed sources a new one can be built from."""
    service = service_of(request)
    with service.read_cache():
        return projects_payload(
            load_projects(service.repository), load_documents(service.repository)
        )


@router.post("/projects/preview")
def preview_project(request: Request, body: ProjectRequest):
    """Resolve a selection without saving it.

    This is the live count under the editor. It reports the fuzzy expansion as
    well as the total, because a meeting selector matches by substring and the
    sources it admits are not always the sources that were clicked.
    """
    service = service_of(request)
    with service.read_cache():
        documents = load_documents(service.repository)
        if not any(
            value.strip() for value in body.meeting_names + body.slack_names
        ):
            return empty_resolution(documents)
        # The selection is resolvable before it is named; only saving needs a
        # name, and refusing to count until one is typed would make the editor
        # useless for deciding what to call the project.
        draft = ProjectRequest(
            name=body.name.strip() or "Draft",
            meeting_names=body.meeting_names,
            slack_names=body.slack_names,
        )
        return resolve_scope(project_scope(draft), documents)


@router.post("/projects")
def create_project(request: Request, body: ProjectRequest):
    """Persist one scope, then return what it resolves to right now."""
    service = service_of(request)
    scope = project_scope(body)
    path = save_project(service.repository, scope, replace_existing=body.replace)
    with service.read_cache():
        resolution = resolve_scope(scope, load_documents(service.repository))
    return {"project": scope.to_dict(), "path": path, "resolution": resolution}
