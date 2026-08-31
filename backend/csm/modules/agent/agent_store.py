"""CRUD operations for AgentDefinition.

The actual prompt-source resolution (file vs URL fetch) lives in `prompt_loader.py`
and is wired in P2. `AgentStore` here is intentionally a pure DB layer so the
HTTP API can call it directly with either a raw `prompt_cached` body (UI inline
edit) or a `prompt_source` + `prompt_cached` pair (when the caller has already
resolved the source).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import AgentDefinition
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


class AgentStoreError(Exception):
    pass


@dataclass
class AgentCreate:
    name: str
    display_name: str
    cwd: str
    prompt_cached: str
    icon: str | None = None
    description: str | None = None
    prompt_source: str | None = None
    disable_skills: bool = False


@dataclass
class AgentPatch:
    display_name: str | None = None
    icon: str | None = None
    description: str | None = None
    cwd: str | None = None
    prompt_source: str | None = None
    prompt_cached: str | None = None
    disable_skills: bool | None = None


class AgentStore:
    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    async def start(self) -> None:
        """No-op — Startable protocol conformance."""
        return None

    async def stop(self) -> None:
        """No-op — Startable protocol conformance."""
        return None

    async def list(self) -> list[AgentDefinition]:
        async with self._sm() as db:
            res = await db.execute(select(AgentDefinition).order_by(AgentDefinition.name))
            return list(res.scalars().all())

    async def get(self, agent_id: str) -> AgentDefinition | None:
        async with self._sm() as db:
            return await db.get(AgentDefinition, agent_id)

    async def get_by_name(self, name: str) -> AgentDefinition | None:
        async with self._sm() as db:
            res = await db.execute(
                select(AgentDefinition).where(AgentDefinition.name == name)
            )
            return res.scalar_one_or_none()

    async def create(self, payload: AgentCreate) -> AgentDefinition:
        if not _NAME_RE.match(payload.name):
            raise AgentStoreError("name must match [a-zA-Z][a-zA-Z0-9_]*")
        if not payload.cwd:
            raise AgentStoreError("cwd is required")
        if not payload.prompt_cached:
            raise AgentStoreError("prompt_cached is required")
        if not payload.display_name:
            raise AgentStoreError("display_name is required")
        async with self._sm() as db:
            dup = await db.execute(
                select(AgentDefinition).where(AgentDefinition.name == payload.name)
            )
            if dup.scalar_one_or_none() is not None:
                raise AgentStoreError(f"agent name {payload.name!r} already exists")
            row = AgentDefinition(
                name=payload.name,
                display_name=payload.display_name,
                icon=payload.icon,
                description=payload.description,
                cwd=payload.cwd,
                prompt_source=payload.prompt_source,
                prompt_cached=payload.prompt_cached,
                disable_skills=payload.disable_skills,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def patch(self, agent_id: str, payload: AgentPatch) -> AgentDefinition | None:
        async with self._sm() as db:
            row = await db.get(AgentDefinition, agent_id)
            if row is None:
                return None
            if payload.display_name is not None:
                if not payload.display_name:
                    raise AgentStoreError("display_name cannot be empty")
                row.display_name = payload.display_name
            if payload.icon is not None:
                row.icon = payload.icon or None
            if payload.description is not None:
                row.description = payload.description or None
            if payload.cwd is not None:
                if not payload.cwd:
                    raise AgentStoreError("cwd cannot be empty")
                row.cwd = payload.cwd
            if payload.prompt_source is not None:
                row.prompt_source = payload.prompt_source or None
            if payload.prompt_cached is not None:
                if not payload.prompt_cached:
                    raise AgentStoreError("prompt_cached cannot be empty")
                row.prompt_cached = payload.prompt_cached
            if payload.disable_skills is not None:
                row.disable_skills = bool(payload.disable_skills)
            row.updated_at = now_utc_naive()
            await db.commit()
            await db.refresh(row)
            return row

    async def delete(self, agent_id: str) -> bool:
        """Delete an AgentDefinition.

        Refuses if any non-ended AgentConversation references it — callers
        should `DELETE /agents/conversations/{cid}` first. P2 wires that up; for
        P1 we simply drop the row when no conversations exist yet.
        """
        from csm.models import AgentConversation
        async with self._sm() as db:
            row = await db.get(AgentDefinition, agent_id)
            if row is None:
                return False
            res = await db.execute(
                select(AgentConversation).where(
                    AgentConversation.agent_def_id == agent_id,
                    AgentConversation.ended_at.is_(None),
                )
            )
            active = res.scalars().first()
            if active is not None:
                raise AgentStoreError(
                    f"agent has active conversation {active.id!r}; end it first"
                )
            await db.delete(row)
            await db.commit()
            return True
