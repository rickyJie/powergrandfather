"""REST endpoints for user-managed Project buckets that group workflows.

Projects sit **on top of** the auto-derived `repo_root` grouping in the
UI: a workflow with `project_id` set displays under the linked Project;
one with NULL falls back to the heuristic bucket. Archiving a project
un-assigns its child workflows (project_id → NULL) so they re-appear
under their heuristic bucket rather than vanishing from the list.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.models import Project, WorkflowDefinition
from csm.utils.time import now_utc_naive

router = APIRouter(prefix="/api", tags=["projects"])


def _serialize(p: Project, workflow_count: int) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "workflow_count": workflow_count,
        "archived_at": p.archived_at.isoformat() if p.archived_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


class CreateProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    # Optional: when the frontend clicks "convert auto-group → project",
    # it passes the list of workflow names discovered in that heuristic
    # bucket so they get bound in the same call.
    workflow_names: list[str] | None = None


class UpdateProjectBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class AbsorbBody(BaseModel):
    """Body for `POST /api/projects/{project_id}/absorb` — batch reparent
    a set of workflows into an existing project in one commit."""
    workflow_names: list[str] = Field(min_length=1, max_length=500)


@router.get("/projects")
async def list_projects(
    include_archived: bool = False,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """List all user-owned project buckets with their workflow count.

    `include_archived=false` (default) hides soft-deleted projects; pass
    `true` to include them (e.g. an admin view).
    """
    async with sm() as db:
        stmt = select(Project).order_by(Project.name)
        if not include_archived:
            stmt = stmt.where(Project.archived_at.is_(None))
        rows = (await db.execute(stmt)).scalars().all()

        counts: dict[str, int] = {}
        if rows:
            # One aggregate query for all project counts. Only count
            # active (non-archived) workflows so the sidebar number
            # matches the row count the user actually sees.
            from sqlalchemy import func
            count_rows = (await db.execute(
                select(
                    WorkflowDefinition.project_id,
                    func.count(WorkflowDefinition.id),
                )
                .where(
                    WorkflowDefinition.project_id.in_([r.id for r in rows]),
                    WorkflowDefinition.archived_at.is_(None),
                )
                .group_by(WorkflowDefinition.project_id)
            )).all()
            counts = {pid: n for pid, n in count_rows}
    items = [_serialize(p, counts.get(p.id, 0)) for p in rows]
    return {"count": len(items), "items": items}


@router.post("/projects")
async def create_project(
    body: CreateProjectBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Create a new project bucket. Optionally binds a batch of existing
    workflows in the same transaction — used by the "Convert to project" flow
    that takes a heuristic auto-group and materializes it.
    """
    async with sm() as db:
        # Reject duplicate names up-front so the client gets a clean 409
        # rather than a UNIQUE-constraint IntegrityError from the driver.
        clash = await db.scalar(
            select(Project).where(Project.name == body.name)
        )
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"project name {body.name!r} already exists",
            )
        proj = Project(name=body.name, description=body.description)
        db.add(proj)
        await db.flush()

        bound = 0
        if body.workflow_names:
            result = await db.execute(
                update(WorkflowDefinition)
                .where(WorkflowDefinition.name.in_(body.workflow_names))
                .values(project_id=proj.id)
            )
            bound = result.rowcount or 0

        await db.commit()
        await db.refresh(proj)
        return {**_serialize(proj, bound), "bound_workflows": bound}


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    body: UpdateProjectBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Rename or edit description. Both fields optional; sending an empty
    body is a no-op that still bumps `updated_at`.
    """
    async with sm() as db:
        proj = await db.get(Project, project_id)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
        if body.name is not None and body.name != proj.name:
            clash = await db.scalar(
                select(Project).where(Project.name == body.name, Project.id != project_id)
            )
            if clash is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"project name {body.name!r} already exists",
                )
            proj.name = body.name
        if body.description is not None:
            proj.description = body.description
        await db.commit()
        await db.refresh(proj)

        # Recompute count for the response so the client can update its
        # local cache without a second /projects GET.
        from sqlalchemy import func
        n = await db.scalar(
            select(func.count(WorkflowDefinition.id))
            .where(
                WorkflowDefinition.project_id == proj.id,
                WorkflowDefinition.archived_at.is_(None),
            )
        )
    return _serialize(proj, n or 0)


@router.post("/projects/{project_id}/absorb")
async def absorb_workflows(
    project_id: str,
    body: AbsorbBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Bulk-move a set of workflows into an existing project in one commit.

    Motivating case: a workflow's `repo_root` last segment collides with an
    existing Project name (e.g. project `sample` already exists AND another
    workflow's repo_root=/x/sample renders under an auto-bucket also named
    `sample`). The user cannot merge via `POST /projects` (409 name clash),
    so this endpoint takes the auto-bucket's workflow list and reparents
    them under the existing project id.
    """
    async with sm() as db:
        proj = await db.get(Project, project_id)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
        if proj.archived_at is not None:
            raise HTTPException(
                status_code=409,
                detail=f"project {project_id!r} is archived; cannot absorb into it",
            )
        # Only active (non-archived) workflows can be bound. `rowcount` reports
        # actual updates so the caller can compare against len(workflow_names)
        # to spot names that were archived or don't exist.
        result = await db.execute(
            update(WorkflowDefinition)
            .where(
                WorkflowDefinition.name.in_(body.workflow_names),
                WorkflowDefinition.archived_at.is_(None),
            )
            .values(project_id=proj.id)
        )
        bound = result.rowcount or 0
        await db.commit()
    return {"project_id": project_id, "project_name": proj.name, "bound": bound}


@router.post("/projects/{project_id}/archive")
async def archive_project(
    project_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Soft-delete a project. Child workflows get project_id → NULL so
    they re-appear in their heuristic bucket instead of vanishing.

    Idempotent: archiving an already-archived project just returns its
    existing archived_at timestamp.
    """
    async with sm() as db:
        proj = await db.get(Project, project_id)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
        if proj.archived_at is not None:
            return {"archived": True, "id": project_id, "archived_at": proj.archived_at.isoformat()}
        proj.archived_at = now_utc_naive()
        # Un-assign workflows so they fall back to the heuristic bucket
        # instead of pointing at a hidden project.
        await db.execute(
            update(WorkflowDefinition)
            .where(WorkflowDefinition.project_id == project_id)
            .values(project_id=None)
        )
        await db.commit()
        await db.refresh(proj)
    return {"archived": True, "id": project_id, "archived_at": proj.archived_at.isoformat()}
