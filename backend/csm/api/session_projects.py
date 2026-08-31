"""REST endpoints for SessionProject buckets that group interactive sessions.

Mirrors `api/projects.py` (which groups workflows) but is deliberately a
separate table + router. See `models/session_project.py` for the split
rationale. A session with `session_project_id = NULL` falls back to an
auto "cwd 2-level" virtual group synthesised client-side.

Endpoints (all under /api/session-projects):
  GET    /                    list all (optional include_archived)
  POST   /                    create by name
  PATCH  /{sp_id}             rename / edit description
  POST   /{sp_id}/archive     soft-archive (un-assigns child sessions)
  POST   /{sp_id}/unarchive   restore
  DELETE /{sp_id}             hard delete (un-assigns child sessions)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.models import Session as SessionModel
from csm.models import SessionProject
from csm.utils.time import now_utc_naive

router = APIRouter(prefix="/api/session-projects", tags=["session-projects"])


def _serialize(sp: SessionProject, session_count: int) -> dict[str, Any]:
    return {
        "id": sp.id,
        "name": sp.name,
        "description": sp.description,
        "session_count": session_count,
        "archived_at": sp.archived_at.isoformat() if sp.archived_at else None,
        "created_at": sp.created_at.isoformat() if sp.created_at else None,
        "updated_at": sp.updated_at.isoformat() if sp.updated_at else None,
    }


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class PatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


async def _session_counts(db, project_ids: list[str]) -> dict[str, int]:
    """Batch fetch: session_count keyed by session_project_id."""
    if not project_ids:
        return {}
    rows = (await db.execute(
        select(SessionModel.session_project_id, func.count(SessionModel.id))
        .where(SessionModel.session_project_id.in_(project_ids))
        .group_by(SessionModel.session_project_id)
    )).all()
    return {pid: cnt for pid, cnt in rows}


@router.get("")
async def list_projects(
    include_archived: bool = False,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        stmt = select(SessionProject).order_by(SessionProject.name.asc())
        if not include_archived:
            stmt = stmt.where(SessionProject.archived_at.is_(None))
        rows = list((await db.execute(stmt)).scalars().all())
        counts = await _session_counts(db, [r.id for r in rows])
        return {"items": [_serialize(r, counts.get(r.id, 0)) for r in rows]}


@router.post("")
async def create_project(
    body: CreateBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        # Uniqueness on `name` is enforced at the DB layer, but surface it
        # as 409 (not 500) for a nicer client story.
        existing = (await db.execute(
            select(SessionProject).where(SessionProject.name == body.name)
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"session project '{body.name}' already exists")
        sp = SessionProject(name=body.name, description=body.description)
        db.add(sp)
        await db.commit()
        await db.refresh(sp)
        return _serialize(sp, 0)


@router.patch("/{sp_id}")
async def patch_project(
    sp_id: str,
    body: PatchBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        sp = await db.get(SessionProject, sp_id)
        if sp is None:
            raise HTTPException(status_code=404, detail="not found")
        if body.name is not None and body.name != sp.name:
            clash = (await db.execute(
                select(SessionProject).where(
                    SessionProject.name == body.name,
                    SessionProject.id != sp_id,
                )
            )).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=409, detail=f"session project '{body.name}' already exists")
            sp.name = body.name
        if body.description is not None:
            sp.description = body.description
        await db.commit()
        await db.refresh(sp)
        counts = await _session_counts(db, [sp.id])
        return _serialize(sp, counts.get(sp.id, 0))


async def _unassign_sessions(db, sp_id: str) -> int:
    """Set every child session's project FK back to NULL. Returns row count."""
    result = await db.execute(
        update(SessionModel)
        .where(SessionModel.session_project_id == sp_id)
        .values(session_project_id=None)
    )
    return result.rowcount or 0


@router.post("/{sp_id}/archive")
async def archive_project(
    sp_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        sp = await db.get(SessionProject, sp_id)
        if sp is None:
            raise HTTPException(status_code=404, detail="not found")
        if sp.archived_at is None:
            sp.archived_at = now_utc_naive()
        unassigned = await _unassign_sessions(db, sp_id)
        await db.commit()
        await db.refresh(sp)
        return {**_serialize(sp, 0), "sessions_unassigned": unassigned}


@router.post("/{sp_id}/unarchive")
async def unarchive_project(
    sp_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        sp = await db.get(SessionProject, sp_id)
        if sp is None:
            raise HTTPException(status_code=404, detail="not found")
        sp.archived_at = None
        await db.commit()
        await db.refresh(sp)
        counts = await _session_counts(db, [sp.id])
        return _serialize(sp, counts.get(sp.id, 0))


@router.delete("/{sp_id}")
async def delete_project(
    sp_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        sp = await db.get(SessionProject, sp_id)
        if sp is None:
            raise HTTPException(status_code=404, detail="not found")
        unassigned = await _unassign_sessions(db, sp_id)
        await db.delete(sp)
        await db.commit()
        return {"deleted": sp_id, "sessions_unassigned": unassigned}
