"""REST endpoints for the multi-agent sync subsystem (spec §7).

Every mutating endpoint:
  1. Validates the payload (Pydantic).
  2. Persists to the DB inside a transaction.
  3. Fans out to enrolled agents via `SyncService`.
  4. Returns a `SyncEnvelope[T]` — `{"data": ..., "sync": [...], "warnings": [...]}`.

DB-commit success ⇒ HTTP 200 EVEN IF one or more agents failed to sync
(spec §2 B2 — partial success). Real HTTP 4xx/5xx is reserved for
validation failures / not-found / server errors.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.api._serialize import iso_utc
from csm.models.drift_record import DriftRecord
from csm.models.instruction import Instruction
from csm.models.mcp_server import McpServer
from csm.models.skill import Skill, SkillFile
from csm.models.sync_activity import SyncActivity
from csm.models.sync_common import DriftReason, SyncModule
from csm.models.sync_config import SyncConfig
from csm.modules.sync.bundle import (
    BundleTooLarge,
    count_bundle_files,
    validate_rel_path,
)
from csm.modules.sync.service import (
    PerAgentResult,
    SyncService,
    envelope_sync_list,
    envelope_warnings,
)
from csm.modules.sync.skill_store import replace_skill_files
from csm.utils.time import now_utc_naive

router = APIRouter(prefix="/api/sync", tags=["sync"])
log = logging.getLogger(__name__)


# ============================================================================
# Pydantic schemas
# ============================================================================

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _validate_name(v: str) -> str:
    if not _NAME_RE.match(v):
        raise ValueError(f"invalid name (want ^[a-z0-9][a-z0-9-]{{0,63}}$): {v!r}")
    return v


class InstructionIn(BaseModel):
    name: str
    title: str
    body: str
    share_scope: list[str] = Field(default_factory=list)
    priority: int = 0

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _validate_name(v)


class InstructionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    title: str
    body: str
    share_scope: list[str]
    priority: int
    created_at: datetime
    updated_at: datetime


class McpServerIn(BaseModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args_json: list[str] = Field(default_factory=list)
    url: str | None = None
    env_json: dict[str, str] = Field(default_factory=dict)
    enabled_for: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _validate_name(v)

    def check_transport_shape(self) -> None:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires command")
            if self.url:
                raise ValueError("stdio transport must not set url")
        else:  # http | sse
            if not self.url:
                raise ValueError(f"{self.transport} transport requires url")
            if self.command:
                raise ValueError(f"{self.transport} transport must not set command")


class McpServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    transport: str
    command: str | None
    args_json: list[str]
    url: str | None
    env_json: dict[str, str]
    enabled_for: list[str]
    created_at: datetime
    updated_at: datetime


class SkillFileIn(BaseModel):
    """One bundle file beside SKILL.md.

    `content` is UTF-8 text by default; set `encoding="base64"` for binary.
    `mode` carries the permission bits — omit it and a helper script arrives
    non-executable, which is its own kind of broken.
    """

    rel_path: str
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    mode: int = 0o644

    @field_validator("rel_path")
    @classmethod
    def _rp(cls, v):
        try:
            return validate_rel_path(v)
        except ValueError as e:
            raise ValueError(str(e)) from e

    def to_bytes(self) -> bytes:
        if self.encoding == "base64":
            try:
                return base64.b64decode(self.content, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ValueError(f"{self.rel_path}: invalid base64 content") from e
        return self.content.encode("utf-8")


class SkillIn(BaseModel):
    name: str
    description: str
    body_md: str
    share_scope: list[str] = Field(default_factory=list)
    # Omitted entirely = "don't touch the bundle" on update, empty on create.
    # An explicit `[]` DOES clear it — that distinction is what lets the UI
    # patch a description without having to round-trip a hundred files.
    files: list[SkillFileIn] | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _validate_name(v)

    @field_validator("body_md")
    @classmethod
    def _b(cls, v):
        if not v.startswith("---"):
            raise ValueError("body_md must start with YAML frontmatter '---'")
        return v

    def bundle(self) -> list[dict[str, Any]] | None:
        if self.files is None:
            return None
        return [
            {"rel_path": f.rel_path, "content": f.to_bytes(), "mode": f.mode}
            for f in self.files
        ]


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    body_md: str
    share_scope: list[str]
    created_at: datetime
    updated_at: datetime


class SyncConfigIn(BaseModel):
    enrolled_agents: list[str] | None = None
    poll_interval_sec: int | None = None
    enabled: bool | None = None
    sync_mode: Literal["lock", "agent"] | None = None
    tick_interval_hours: int | None = None
    tick_interval_minutes: int | None = None
    # Resource-name allowlist for this module. Present-fields-only semantics via
    # model_fields_set: OMITTED = leave unchanged; explicit `null` = clear the
    # filter (sync everything); a list = restrict sync to exactly those names.
    resource_allowlist: list[str] | None = None


class SyncConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    module: str
    enrolled_agents: list[str]
    poll_interval_sec: int
    enabled: bool
    sync_mode: str
    tick_interval_hours: int
    tick_interval_minutes: int
    resource_allowlist: list[str] | None
    updated_at: datetime


# ============================================================================
# Dependency helpers
# ============================================================================


def _svc(request: Request) -> SyncService:
    svc = getattr(request.app.state, "sync_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="sync service not initialized")
    return svc


def _known_agents(request: Request) -> set[str]:
    reg = request.app.state.adapter_registry
    return set(reg.names())


def _validate_agents(names: list[str], known: set[str]) -> None:
    unknown = [n for n in names if n not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown agents: {unknown} (known: {sorted(known)})",
        )


def _envelope(data: Any, results: list[PerAgentResult]) -> dict[str, Any]:
    return {
        "data": data,
        "sync": envelope_sync_list(results),
        "warnings": envelope_warnings(results),
    }


def _iso(dt: datetime | None) -> str | None:
    return iso_utc(dt) if dt else None


# ============================================================================
# Serializers (avoid Pydantic's async lazy-load quirks with SQLA)
# ============================================================================


def _dump_instruction(row: Instruction) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "title": row.title,
        "body": row.body,
        "share_scope": list(row.share_scope or []),
        "priority": row.priority,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _dump_mcp(row: McpServer) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "command": row.command,
        "args_json": list(row.args_json or []),
        "url": row.url,
        "env_json": dict(row.env_json or {}),
        "enabled_for": list(row.enabled_for or []),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _dump_skill_file(f: SkillFile) -> dict[str, Any]:
    """One bundle file. Content is returned as text when it decodes as
    UTF-8, base64 otherwise — a skill may legitimately carry a PNG under
    `assets/`, and the UI has to be able to tell the two apart."""
    out: dict[str, Any] = {
        "rel_path": f.rel_path,
        "mode": f.mode,
        "size": len(f.content or b""),
        "sha256": f.sha256,
    }
    try:
        out["content"] = (f.content or b"").decode("utf-8")
        out["encoding"] = "utf-8"
    except UnicodeDecodeError:
        out["content"] = base64.b64encode(f.content or b"").decode("ascii")
        out["encoding"] = "base64"
    return out


def _dump_skill(row: Skill, *, include_files: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "body_md": row.body_md,
        "share_scope": list(row.share_scope or []),
        "file_count": len(row.files),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    # The list endpoint stays metadata-only: a bundle can run to hundreds of
    # KB and the skills table renders none of it.
    if include_files:
        out["files"] = [_dump_skill_file(f) for f in row.files]
    return out


def _dump_config(row: SyncConfig) -> dict[str, Any]:
    return {
        "id": row.id,
        "module": row.module,
        "enrolled_agents": list(row.enrolled_agents or []),
        "poll_interval_sec": row.poll_interval_sec,
        "enabled": row.enabled,
        "sync_mode": row.sync_mode or "lock",
        "tick_interval_hours": row.tick_interval_hours or 0,
        "tick_interval_minutes": row.tick_interval_minutes or 0,
        "resource_allowlist": getattr(row, "resource_allowlist", None),
        "updated_at": _iso(row.updated_at),
    }


# ============================================================================
# /sync/config
# ============================================================================


@router.get("/config")
async def list_config(sm: async_sessionmaker = Depends(get_db_sessionmaker)):
    """Return exactly the SyncConfig rows that exist; missing modules are
    surfaced as `null` so the frontend can render an empty-state UI."""
    async with sm() as session:
        rows = (await session.execute(select(SyncConfig))).scalars().all()
    by_module = {r.module: _dump_config(r) for r in rows}
    return {"config": [{"module": m.value, "entry": by_module.get(m.value)} for m in SyncModule]}


@router.put("/config/{module}")
async def update_config(
    module: str,
    body: SyncConfigIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    try:
        mod = SyncModule(module)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown module: {module}")

    if body.enrolled_agents is not None:
        _validate_agents(body.enrolled_agents, _known_agents(request))

    async with sm() as session:
        row = (
            await session.execute(select(SyncConfig).where(SyncConfig.module == mod.value))
        ).scalar_one_or_none()
        if row is None:
            row = SyncConfig(
                module=mod.value,
                enrolled_agents=body.enrolled_agents or [],
                poll_interval_sec=body.poll_interval_sec or 30,
                enabled=True if body.enabled is None else body.enabled,
                sync_mode=body.sync_mode or "lock",
                tick_interval_hours=body.tick_interval_hours or 0,
                tick_interval_minutes=body.tick_interval_minutes or 0,
                resource_allowlist=body.resource_allowlist,
                updated_at=now_utc_naive(),
            )
            session.add(row)
        else:
            if body.enrolled_agents is not None:
                row.enrolled_agents = body.enrolled_agents
            if body.poll_interval_sec is not None:
                row.poll_interval_sec = body.poll_interval_sec
            if body.enabled is not None:
                row.enabled = body.enabled
            if body.sync_mode is not None:
                row.sync_mode = body.sync_mode
            if body.tick_interval_hours is not None:
                if body.tick_interval_hours < 0:
                    raise HTTPException(status_code=422, detail="tick_interval_hours must be >= 0")
                row.tick_interval_hours = body.tick_interval_hours
            if body.tick_interval_minutes is not None:
                if body.tick_interval_minutes < 0:
                    raise HTTPException(
                        status_code=422, detail="tick_interval_minutes must be >= 0"
                    )
                row.tick_interval_minutes = body.tick_interval_minutes
            # Present-fields-only: explicit null clears the filter, list sets it.
            if "resource_allowlist" in body.model_fields_set:
                row.resource_allowlist = body.resource_allowlist
        await session.commit()
        await session.refresh(row)
    return _dump_config(row)


def _skill_source_hint(path: str | None) -> str:
    """Heuristic label 'user' vs 'marketplace' for a skill's SKILL.md.

    Marketplace-published skills carry a `version:` line in their frontmatter;
    ad-hoc hand-authored ones don't. Best-effort — reads only the leading
    frontmatter, defaults to 'user' on any doubt so the user's own skills are
    never hidden by a bad guess. (codex additionally keeps its built-ins under
    `.system/`, which its adapter already filters out.)"""
    if not path:
        return "user"
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(2000)
    except Exception:
        return "user"
    m = re.match(r"^---\n(.*?)\n---", head, re.S)
    frontmatter = m.group(1) if m else head
    for line in frontmatter.splitlines():
        if line.strip().lower().startswith("version:"):
            return "marketplace"
    return "user"


@router.get("/skills/available")
async def list_available_skills(
    request: Request,
    agent: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Skills present on disk for `agent` (or the union across all agents when
    omitted) — name + description + source_hint ONLY, never the body. Powers
    the allowlist picker so the user chooses which skills to sync. Generalizes:
    no hard-coded names/prefixes, deduped by skill name, each entry lists which
    agents have it, plus a 'user' / 'marketplace' source hint (see
    _skill_source_hint) so the UI can offer 'select my own'."""
    reg = request.app.state.adapter_registry
    agent_names = [agent] if agent else list(reg.names())
    seen: dict[str, dict[str, Any]] = {}
    for aname in agent_names:
        try:
            adapter = reg.get(aname)
        except Exception:
            continue
        try:
            skills = adapter.list_skills()  # name + path + description, no body
        except Exception:
            log.exception("list_skills failed for %s", aname)
            skills = []
        for s in skills:
            nm = s.get("name")
            if not nm:
                continue
            entry = seen.setdefault(
                nm,
                {
                    "name": nm,
                    "description": s.get("description"),
                    "agents": [],
                    "file_count": {},
                    "source_hint": _skill_source_hint(s.get("path")),
                },
            )
            if aname not in entry["agents"]:
                entry["agents"].append(aname)
            # Bundle size per agent — a mismatch between two agents is
            # exactly the symptom worth seeing at a glance. Counted with a
            # stat-only walk; reading every skill's tree on each render of
            # the picker would be gratuitous.
            try:
                entry["file_count"][aname] = count_bundle_files(Path(s["path"]).parent)
            except (OSError, KeyError):
                entry["file_count"][aname] = 0
    return sorted(seen.values(), key=lambda x: x["name"])


@router.get("/status")
async def summary_status(
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as session:
        configs = {r.module: r for r in (await session.execute(select(SyncConfig))).scalars().all()}
        unresolved = (
            (
                await session.execute(
                    select(DriftRecord).where(DriftRecord.resolved == False)  # noqa: E712
                )
            )
            .scalars()
            .all()
        )
    drift_by_module: dict[str, int] = {m.value: 0 for m in SyncModule}
    for d in unresolved:
        drift_by_module[d.module] = drift_by_module.get(d.module, 0) + 1
    return {
        "modules": [
            {
                "module": m.value,
                "enrolled_agents": list(configs[m.value].enrolled_agents)
                if m.value in configs
                else [],
                "enabled": bool(configs[m.value].enabled) if m.value in configs else False,
                "unresolved_drift": drift_by_module.get(m.value, 0),
            }
            for m in SyncModule
        ],
    }


# ============================================================================
# /sync/memory/instructions
# ============================================================================


@router.get("/memory/instructions")
async def list_instructions(sm: async_sessionmaker = Depends(get_db_sessionmaker)):
    async with sm() as session:
        rows = (
            (
                await session.execute(
                    select(Instruction).order_by(Instruction.priority.desc(), Instruction.name)
                )
            )
            .scalars()
            .all()
        )
    return {"items": [_dump_instruction(r) for r in rows]}


@router.post("/memory/instructions")
async def create_instruction(
    body: InstructionIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    _validate_agents(body.share_scope, _known_agents(request))
    async with sm() as session:
        existing = (
            await session.execute(select(Instruction).where(Instruction.name == body.name))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"name exists: {body.name}")
        ins = Instruction(
            name=body.name,
            title=body.title,
            body=body.body,
            share_scope=body.share_scope,
            priority=body.priority,
        )
        session.add(ins)
        await session.commit()
        await session.refresh(ins)
    results = await _svc(request).sync_instruction(ins)
    return _envelope(_dump_instruction(ins), results)


@router.get("/memory/instructions/{iid}")
async def get_instruction(iid: int, sm: async_sessionmaker = Depends(get_db_sessionmaker)):
    async with sm() as session:
        row = await session.get(Instruction, iid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"instruction {iid} not found")
    return _dump_instruction(row)


@router.put("/memory/instructions/{iid}")
async def update_instruction(
    iid: int,
    body: InstructionIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    _validate_agents(body.share_scope, _known_agents(request))
    async with sm() as session:
        row = await session.get(Instruction, iid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"instruction {iid} not found")
        row.name = body.name
        row.title = body.title
        row.body = body.body
        row.share_scope = body.share_scope
        row.priority = body.priority
        await session.commit()
        await session.refresh(row)
    results = await _svc(request).sync_instruction(row)
    return _envelope(_dump_instruction(row), results)


@router.delete("/memory/instructions/{iid}")
async def delete_instruction(
    iid: int,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as session:
        row = await session.get(Instruction, iid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"instruction {iid} not found")
    # Strip from agents BEFORE deleting DB row so the marker id is still known.
    results = await _svc(request).remove_instruction(row)
    async with sm() as session:
        row = await session.get(Instruction, iid)
        if row is not None:
            await session.delete(row)
            await session.commit()
    return _envelope({"deleted": True, "id": iid}, results)


# ============================================================================
# /sync/mcp/servers
# ============================================================================


@router.get("/mcp/servers")
async def list_mcp_servers(sm: async_sessionmaker = Depends(get_db_sessionmaker)):
    async with sm() as session:
        rows = (await session.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
    return {"items": [_dump_mcp(r) for r in rows]}


@router.post("/mcp/servers")
async def create_mcp_server(
    body: McpServerIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    try:
        body.check_transport_shape()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_agents(body.enabled_for, _known_agents(request))
    async with sm() as session:
        existing = (
            await session.execute(select(McpServer).where(McpServer.name == body.name))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"name exists: {body.name}")
        srv = McpServer(
            name=body.name,
            transport=body.transport,
            command=body.command,
            args_json=body.args_json,
            url=body.url,
            env_json=body.env_json,
            enabled_for=body.enabled_for,
        )
        session.add(srv)
        await session.commit()
        await session.refresh(srv)
    results = await _svc(request).sync_mcp_server(srv)
    return _envelope(_dump_mcp(srv), results)


@router.put("/mcp/servers/{sid}")
async def update_mcp_server(
    sid: int,
    body: McpServerIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    try:
        body.check_transport_shape()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_agents(body.enabled_for, _known_agents(request))
    async with sm() as session:
        row = await session.get(McpServer, sid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"mcp {sid} not found")
        row.name = body.name
        row.transport = body.transport
        row.command = body.command
        row.args_json = body.args_json
        row.url = body.url
        row.env_json = body.env_json
        row.enabled_for = body.enabled_for
        await session.commit()
        await session.refresh(row)
    results = await _svc(request).sync_mcp_server(row)
    return _envelope(_dump_mcp(row), results)


@router.delete("/mcp/servers/{sid}")
async def delete_mcp_server(
    sid: int,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as session:
        row = await session.get(McpServer, sid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"mcp {sid} not found")
    results = await _svc(request).remove_mcp_server(row)
    async with sm() as session:
        row = await session.get(McpServer, sid)
        if row is not None:
            await session.delete(row)
            await session.commit()
    return _envelope({"deleted": True, "id": sid}, results)


# ============================================================================
# /sync/skills
# ============================================================================


@router.get("/skills")
async def list_skills(sm: async_sessionmaker = Depends(get_db_sessionmaker)):
    async with sm() as session:
        rows = (await session.execute(select(Skill).order_by(Skill.name))).scalars().all()
    return {"items": [_dump_skill(r) for r in rows]}


@router.post("/skills")
async def create_skill(
    body: SkillIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    _validate_agents(body.share_scope, _known_agents(request))
    async with sm() as session:
        existing = (
            await session.execute(select(Skill).where(Skill.name == body.name))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"name exists: {body.name}")
        sk = Skill(
            name=body.name,
            description=body.description,
            body_md=body.body_md,
            share_scope=body.share_scope,
        )
        session.add(sk)
        await session.flush()
        try:
            await replace_skill_files(session, sk, body.bundle() or [])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await session.commit()
        await session.refresh(sk)
    results = await _svc(request).sync_skill(sk)
    return _envelope(_dump_skill(sk), results)


@router.put("/skills/{kid}")
async def update_skill(
    kid: int,
    body: SkillIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    _validate_agents(body.share_scope, _known_agents(request))
    async with sm() as session:
        row = await session.get(Skill, kid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"skill {kid} not found")
        row.name = body.name
        row.description = body.description
        row.body_md = body.body_md
        row.share_scope = body.share_scope
        bundle = body.bundle()
        if bundle is not None:  # None = leave the bundle alone; [] = clear it
            try:
                await replace_skill_files(session, row, bundle)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        await session.commit()
        await session.refresh(row)
    results = await _svc(request).sync_skill(row)
    return _envelope(_dump_skill(row), results)


@router.get("/skills/{kid}")
async def get_skill(
    kid: int,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """One skill, bundle contents included. The list endpoint omits them."""
    async with sm() as session:
        row = await session.get(Skill, kid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"skill {kid} not found")
        return _dump_skill(row, include_files=True)


@router.post("/skills/reingest")
async def reingest_skills(
    request: Request,
    agent: str = Query(..., description="agent to re-read the bundles from"),
    names: list[str] | None = Query(None),
    push: bool = Query(True, description="re-push each refreshed skill to its scope"),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Re-read skill bundles off `agent`'s disk into the CSM rows.

    The repair path for skills that were ingested before bundle sync existed
    and so carry only a SKILL.md. Matches on skill NAME, refreshes body +
    bundle, and (unless `push=false`) re-materialises each one across its
    existing `share_scope` — so the agents that were missing the helper
    files get them.

    Only touches skills CSM already knows about; it never adopts new ones.
    """
    reg = request.app.state.adapter_registry
    try:
        adapter = reg.get(agent)
    except Exception:
        raise HTTPException(status_code=404, detail=f"unknown agent: {agent}")

    wanted = set(names) if names else None
    out: list[dict[str, Any]] = []

    async with sm() as session:
        rows = (await session.execute(select(Skill).order_by(Skill.name))).scalars().all()
        targets = [r.id for r in rows if wanted is None or r.name in wanted]
        names_by_id = {r.id: r.name for r in rows}

    for sid in targets:
        name = names_by_id[sid]
        try:
            disk = adapter.read_skill_bundle(name)
        except BundleTooLarge as e:
            out.append({"name": name, "action": "skipped", "detail": str(e)})
            continue
        except Exception as e:
            log.exception("reingest: read_skill_bundle failed for %s/%s", agent, name)
            out.append({"name": name, "action": "error", "detail": f"{type(e).__name__}: {e}"})
            continue
        if disk is None:
            out.append({"name": name, "action": "absent", "detail": f"not on {agent}"})
            continue

        async with sm() as session:
            row = await session.get(Skill, sid)
            if row is None:
                continue
            row.body_md = disk.get("body_md") or row.body_md
            if disk.get("description"):
                row.description = disk["description"]
            await replace_skill_files(session, row, disk.get("files") or [])
            await session.commit()

        entry: dict[str, Any] = {
            "name": name,
            "action": "reingested",
            "file_count": len(disk.get("files") or []),
        }
        if disk.get("skipped"):
            entry["skipped_files"] = disk["skipped"]
        if push:
            async with sm() as session:
                row = await session.get(Skill, sid)
                # Never push back to the agent we just read from: it already
                # has this content by definition, and for a skill that lives
                # as a symlink into a skill-book checkout the write would be
                # refused anyway — logging drift for a round trip that had
                # nothing to say.
                results = (
                    await _svc(request).sync_skill(row, exclude_agents={agent})
                    if row
                    else []
                )
            entry["sync"] = envelope_sync_list(results)
        out.append(entry)

    return {"agent": agent, "items": out}


@router.delete("/skills/{kid}")
async def delete_skill(
    kid: int,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as session:
        row = await session.get(Skill, kid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"skill {kid} not found")
    results = await _svc(request).remove_skill(row)
    async with sm() as session:
        row = await session.get(Skill, kid)
        if row is not None:
            await session.delete(row)
            await session.commit()
    return _envelope({"deleted": True, "id": kid}, results)


# ============================================================================
# /sync/{module}/import-preview — read-only enumeration of on-agent state
# ============================================================================


@router.get("/{module}/import-preview")
async def import_preview(module: str, agent: str, request: Request):
    try:
        mod = SyncModule(module)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown module: {module}")

    reg = request.app.state.adapter_registry
    if agent not in reg:
        raise HTTPException(status_code=422, detail=f"unknown agent: {agent}")
    adapter = reg.get(agent)

    if mod is SyncModule.MEMORY:
        paths = adapter.memory_paths("user")
        text = adapter.read_memory(paths[0]) if paths else ""
        return {
            "module": mod.value,
            "agent": agent,
            "path": str(paths[0]) if paths else None,
            "body": text,
        }
    if mod is SyncModule.MCP:
        try:
            entries = await adapter.mcp_list()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"mcp_list failed: {e}")
        return {"module": mod.value, "agent": agent, "entries": entries}
    if mod is SyncModule.SKILLS:
        return {"module": mod.value, "agent": agent, "entries": adapter.list_skills()}
    raise HTTPException(status_code=500, detail="unreachable")


class MigrateIn(BaseModel):
    source: str
    target: str
    names: list[str] | None = None


@router.post("/{module}/migrate")
async def migrate(module: str, body: MigrateIn, request: Request):
    """Deterministically migrate `source` agent's existing resources to `target`.

    User-controlled, LLM-free A→B: reads the source agent's on-disk state,
    upserts each item as a CSM canonical row (source wins on same-name), and
    fans it out to the target. `names` (optional) limits to specific items.

    Per-module scope: skills + memory are fully supported; mcp returns
    `unsupported` because `mcp list` doesn't expose command/url/env needed to
    rebuild a server (define it once in CSM Resources to fan it out instead).
    """
    try:
        mod = SyncModule(module)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown module: {module}")
    _validate_agents([body.source, body.target], _known_agents(request))
    items = await _svc(request).migrate_agent_to_agent(
        mod,
        body.source,
        body.target,
        body.names,
    )
    return {
        "module": mod.value,
        "source": body.source,
        "target": body.target,
        "items": items,
    }


# ============================================================================
# /sync/drift + /sync/activity
# ============================================================================


@router.get("/drift")
async def list_drift(
    resolved: bool = False,
    limit: int = Query(50, ge=1, le=500),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as session:
        q = (
            select(DriftRecord)
            .where(DriftRecord.resolved == resolved)
            .order_by(
                DriftRecord.ts.desc(),
            )
            .limit(limit)
        )
        rows = (await session.execute(q)).scalars().all()
    return {"items": [_dump_drift(r) for r in rows]}


@router.post("/drift/{did}/resolve")
async def resolve_drift(
    did: int,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Reconcile a drifted agent back to the CSM authoritative version.

    Historically this endpoint only flipped `resolved=True` — the service
    docstring claimed a reconcile happened but no adapter write ever ran,
    so "resolve" was a silent no-op ("已读不回"). It now genuinely re-pushes
    the CSM version of `(resource_type, resource_id)` to the drifted agent
    via `SyncService.sync_by_type_id` before marking the row resolved.

    The re-push result is returned under `reconcile`. A push failure does
    NOT block the resolve flag — the row is still marked resolved and the
    failure surfaces in the response so the caller can see what happened.
    """
    async with sm() as session:
        row = await session.get(DriftRecord, did)
        if row is None:
            raise HTTPException(status_code=404, detail=f"drift {did} not found")
        resource_type = row.resource_type
        resource_id = row.resource_id
        agent = row.agent

    # Re-push CSM's authoritative version back to the single drifted agent.
    # DriftRecord.resource_type is already the string sync_by_type_id wants
    # ("instruction" / "mcp_server" / "skill").
    reconcile: list[dict[str, Any]]
    try:
        results = await _svc(request).sync_by_type_id(
            resource_type,
            resource_id,
            [agent],
        )
        reconcile = envelope_sync_list(results)
    except Exception as e:  # noqa: BLE001 — never block the resolve flag
        reconcile = [
            {
                "agent": agent,
                "status": "error",
                "detail": f"reconcile failed: {type(e).__name__}: {e}",
            }
        ]

    async with sm() as session:
        row = await session.get(DriftRecord, did)
        if row is None:
            raise HTTPException(status_code=404, detail=f"drift {did} not found")
        row.resolved = True
        row.resolved_at = now_utc_naive()
        await session.commit()
        await session.refresh(row)
    return {**_dump_drift(row), "reconcile": reconcile}


@router.get("/activity")
async def list_activity(
    module: str | None = None,
    since: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as session:
        q = select(SyncActivity)
        if module:
            try:
                mod = SyncModule(module)
            except ValueError:
                raise HTTPException(status_code=404, detail=f"unknown module: {module}")
            q = q.where(SyncActivity.module == mod.value)
        if since is not None:
            q = q.where(SyncActivity.ts >= since)
        q = q.order_by(SyncActivity.ts.desc()).limit(limit)
        rows = (await session.execute(q)).scalars().all()
    return {"items": [_dump_activity(r) for r in rows]}


def _dump_drift(row: DriftRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": _iso(row.ts),
        "module": row.module,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "agent": row.agent,
        "reason": row.reason,
        "expected_hash": row.expected_hash,
        "actual_hash": row.actual_hash,
        "resolved": bool(row.resolved),
        "resolved_at": _iso(row.resolved_at),
        "detail_json": row.detail_json,
    }


def _dump_activity(row: SyncActivity) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": _iso(row.ts),
        "module": row.module,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "agent": row.agent,
        "action": row.action,
        "status": row.status,
        "duration_ms": row.duration_ms,
        "detail_json": row.detail_json,
    }


# suppress accidental "unused" flag for enum imported for schema clarity
_ = DriftReason


# ============================================================================
# Sync v2 agent-driven endpoints (Phase 5)
# ============================================================================


from csm.models.fanout_ledger import FanoutLedger  # noqa: E402
from csm.models.pending_decision import PendingDecision  # noqa: E402
from csm.models.sync_agent_run import SyncAgentRun  # noqa: E402
from csm.models.sync_policy import SyncPolicy  # noqa: E402
from csm.modules.sync.sentinels import (  # noqa: E402
    HASH_SENTINEL_UNKNOWN,
    make_diverged_sentinel,
    read_agent_side_body,
)

_MAX_RESOLVE_RETRY = 5


# ---- Manual tick + run inspection -----------------------------------------


class AgentTickIn(BaseModel):
    """Body for POST /agent-tick.

    `user_intent` (optional) is free text describing what the user wants from
    this tick (e.g. "migrate claude's mcp to codex"). It's threaded into the
    payload meta so the SyncAgent can bias its decisions — safety rails still
    apply (secrets human-gated, conflicts still escalate).
    """

    model_config = ConfigDict(extra="forbid")
    user_intent: str | None = None


@router.post("/agent-tick")
async def manual_agent_tick(
    request: Request,
    body: AgentTickIn | None = None,
) -> dict[str, Any]:
    """Fire one SyncAgent tick in the BACKGROUND, returning immediately.

    The decision now runs as a real AUTO session (tens of seconds), which
    would blow past the frontend's 30s HTTP timeout if awaited inline. So we
    acquire the tick lock, kick off `run_tick` as a background task, wait just
    long enough for the run row to exist, and return its `run_id`. The client
    then polls `GET /agent-runs/{run_id}` for `live_phase` progress.

    Returns:
      200 + `{run_id, status: "running"}` once the run row exists
      409 + `{error, current_run_id}` if a tick is already in progress

    Design v4 §8: the check-and-set uses the bool `_tick_running` flag
    (non-reentrant, single-thread asyncio guarantees atomicity between the if
    and the set — no await in that section). `run_tick` releases the lock in
    its own `finally`, so the background task owns the lock for its lifetime.
    """
    orch = request.app.state.sync_orchestrator
    if not orch.try_acquire_tick():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "tick_in_progress",
                "current_run_id": orch._current_run_id,
            },
        )

    user_intent = body.user_intent if body else None

    # Create the run row up front so we can return its id immediately — no
    # racing the background task's own row creation (a fast/disabled tick
    # would otherwise finish and clear _current_run_id before we read it).
    try:
        run_id = await orch.create_run_row("manual")
    except Exception:
        orch.release_tick()
        raise

    async def _run() -> None:
        try:
            await orch.run_tick(
                trigger="manual", user_intent=user_intent, run_row_id=run_id,
            )
        except Exception:
            logging.getLogger(__name__).exception("background agent-tick failed")

    task = asyncio.create_task(_run(), name="sync-manual-tick")
    # Keep a strong ref so the task isn't garbage-collected mid-flight.
    tasks: set = getattr(request.app.state, "_sync_tick_tasks", None)
    if tasks is None:
        tasks = set()
        request.app.state._sync_tick_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)

    return {"run_id": run_id, "status": "running"}


@router.get("/agent-runs")
async def list_agent_runs(
    limit: int = Query(50, ge=1, le=200),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> list[dict[str, Any]]:
    async with sm() as db:
        rows = (
            (await db.execute(select(SyncAgentRun).order_by(SyncAgentRun.ts.desc()).limit(limit)))
            .scalars()
            .all()
        )
    return [_dump_agent_run(r) for r in rows]


@router.get("/agent-runs/{rid}")
async def get_agent_run(
    rid: int,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        row = await db.get(SyncAgentRun, rid)
    if row is None:
        raise HTTPException(404, f"agent_run {rid} not found")
    # Live phase from orchestrator if this is the currently-running row.
    live_phase = None
    orch = request.app.state.sync_orchestrator
    if orch._current_run_id == rid:
        live_phase = orch._current_phase
    return {**_dump_agent_run(row), "live_phase": live_phase}


def _dump_agent_run(r: SyncAgentRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "ts": _iso(r.ts),
        "trigger": r.trigger,
        "phase": r.phase,
        "prompt_hash": r.prompt_hash,
        "input_state_hash": r.input_state_hash,
        "decisions_count": r.decisions_count,
        "applied_count": r.applied_count,
        "rejected_count": r.rejected_count,
        "stale_skipped_count": r.stale_skipped_count,
        "deleted_after_collect_count": r.deleted_after_collect_count,
        "error": r.error,
        "duration_ms": r.duration_ms,
        "token_usage_json": r.token_usage_json,
        "parent_run_id": r.parent_run_id,
    }


# ---- Pending decisions -----------------------------------------------------


class ResolveIn(BaseModel):
    """Body for POST /pending-decisions/{id}/resolve.

    `resolution` values:
      - `take_agent:<agent>`  — adopt that agent's version + fanout
      - `keep_diverged`       — accept divergence; write DIVERGED sentinel
      - `dismiss`             — silently close
    """

    resolution: str = Field(..., min_length=1, max_length=64)


_ALLOWED_RESOLVE_PATTERN = re.compile(r"^(take_agent:[a-z0-9_-]+|keep_diverged|dismiss)$")


@router.get("/pending-decisions")
async def list_pending_decisions(
    status: Literal["pending", "resolve_failed", "resolved", "dismissed", "all"] = Query("pending"),
    limit: int = Query(100, ge=1, le=500),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> list[dict[str, Any]]:
    async with sm() as db:
        stmt = select(PendingDecision).order_by(PendingDecision.ts.desc()).limit(limit)
        if status != "all":
            stmt = (
                select(PendingDecision)
                .where(
                    PendingDecision.status == status,
                )
                .order_by(PendingDecision.ts.desc())
                .limit(limit)
            )
        rows = (await db.execute(stmt)).scalars().all()
    return [_dump_pending(r) for r in rows]


@router.post("/pending-decisions/{pid}/resolve")
async def resolve_pending(
    pid: int,
    body: ResolveIn,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Apply a resolution to a pending decision.

    Returns 200 with `{id, status, retry_count, apply_error}` where
    `status` is one of resolved / resolve_failed. Retry cap enforced at
    5 (design v3 §10 P0-2 → 429 on 6th attempt).

    keep_diverged uses realtime adapter read (v7 §1 P1-V6-1) — sentinel
    reflects the agent-side body hash AT resolve time, not at decide time.
    """
    if not _ALLOWED_RESOLVE_PATTERN.match(body.resolution):
        raise HTTPException(400, f"invalid resolution: {body.resolution!r}")

    # Phase 1 (DB tx): validate + acquire retry slot + apply CSM-side updates.
    # Fanout to agents (Phase 2) happens OUTSIDE this transaction so it can't
    # hold a SQLite write lock while doing network / adapter I/O.
    pending_snapshot: dict[str, Any] = {}
    async with sm() as session, session.begin():
        pending = await session.get(PendingDecision, pid)
        if pending is None:
            raise HTTPException(404, f"pending_decision {pid} not found")
        if pending.status not in ("pending", "resolve_failed"):
            raise HTTPException(
                409,
                f"pending_decision {pid} status={pending.status!r} — not retryable",
            )
        if (pending.retry_count or 0) >= _MAX_RESOLVE_RETRY:
            raise HTTPException(
                429,
                f"pending_decision {pid} exceeded max retry ({_MAX_RESOLVE_RETRY})",
            )
        pending.retry_count = (pending.retry_count or 0) + 1
        pending.resolution = body.resolution
        pending.resolved_at = now_utc_naive()

        try:
            if body.resolution.startswith("take_agent:"):
                source_agent = body.resolution.split(":", 1)[1]
                # Body update happens inside this tx; fanout deferred to Phase 2.
                await _apply_takeover_db_only(session, pending, source_agent)
                pending_snapshot = {
                    "kind": "take_agent",
                    "source_agent": source_agent,
                    "resource_type": pending.resource_type,
                    "resource_id": pending.resource_id,
                }
            elif body.resolution == "keep_diverged":
                # keep_diverged reads adapter live — also defer to Phase 2.
                pending_snapshot = {
                    "kind": "keep_diverged",
                    "resource_type": pending.resource_type,
                    "resource_id": pending.resource_id,
                    "candidates": dict(pending.candidates_json or {}),
                }
            else:  # dismiss
                pending.status = "dismissed"
                pending.applied_at = now_utc_naive()
                pending.apply_error = None
                pending_snapshot = {"kind": "dismiss"}
        except Exception as exc:  # noqa: BLE001
            pending.status = "resolve_failed"
            pending.apply_error = f"{type(exc).__name__}: {exc}"
            pending_snapshot = {"kind": "error_pre_fanout"}

    # Phase 2 (no DB tx): fanout to agents / read adapter side.
    fanout_error: str | None = None
    if pending_snapshot.get("kind") == "take_agent":
        try:
            await _apply_takeover_fanout(
                pending_snapshot["source_agent"],
                pending_snapshot["resource_type"],
                pending_snapshot["resource_id"],
                request,
            )
        except Exception as exc:  # noqa: BLE001
            fanout_error = f"{type(exc).__name__}: {exc}"
    elif pending_snapshot.get("kind") == "keep_diverged":
        try:
            await _apply_keep_diverged_realtime_no_tx(
                pending_snapshot,
                request,
            )
        except Exception as exc:  # noqa: BLE001
            fanout_error = f"{type(exc).__name__}: {exc}"

    # Phase 3 (short DB tx): finalize status based on fanout outcome.
    if pending_snapshot.get("kind") in ("take_agent", "keep_diverged"):
        async with sm() as session, session.begin():
            pending_final = await session.get(PendingDecision, pid)
            if pending_final is None:
                pass
            elif fanout_error is not None:
                pending_final.status = "resolve_failed"
                pending_final.apply_error = fanout_error
            else:
                pending_final.status = "resolved"
                pending_final.applied_at = now_utc_naive()
                pending_final.apply_error = None

    async with sm() as db:
        pending = await db.get(PendingDecision, pid)
    return _dump_pending(pending)


async def _apply_takeover_db_only(session, pending, source_agent: str) -> None:
    """Phase 1 DB update for take_agent — just mutate the row body."""
    body = (pending.candidates_json or {}).get(source_agent)
    if body is None:
        raise ValueError(
            f"candidates_json has no entry for {source_agent!r}",
        )
    model = {
        "instruction": Instruction,
        "mcp_server": McpServer,
        "skill": Skill,
    }[pending.resource_type]
    if pending.resource_id is None:
        return
    row = await session.get(model, pending.resource_id)
    if row is None:
        return
    if pending.resource_type == "instruction":
        row.body = body
    elif pending.resource_type == "skill":
        row.body_md = body
    # mcp: cannot rebuild env from stable body subset; leave untouched.


async def _apply_takeover_fanout(
    source_agent: str,
    resource_type: str,
    resource_id: int | None,
    request,
) -> None:
    """Phase 2 fanout for take_agent — call SyncService WITHOUT holding a DB tx."""
    if resource_id is None:
        return
    orch = request.app.state.sync_orchestrator
    reg = request.app.state.adapter_registry
    targets = [a for a in reg.names() if a != source_agent]
    if targets:
        await orch._svc.sync_by_type_id(
            resource_type,
            resource_id,
            targets,
        )


async def _apply_keep_diverged_realtime_no_tx(snapshot: dict[str, Any], request) -> None:
    """Phase 2 for keep_diverged — read adapter + write sentinels.

    Uses its own short DB tx at the end to write the sentinels; adapter
    reads happen with no DB tx held.
    """
    import hashlib as _hashlib

    resource_type = snapshot["resource_type"]
    resource_id = snapshot["resource_id"]
    if resource_id is None:
        return
    model = {
        "instruction": Instruction,
        "mcp_server": McpServer,
        "skill": Skill,
    }[resource_type]
    reg = request.app.state.adapter_registry

    # Read all agent-side bodies first — no DB tx.
    sm_local = request.app.state.sessionmaker
    async with sm_local() as db:
        row = await db.get(model, resource_id)
    if row is None:
        return
    locator = _body_locator(row, resource_type)

    new_hashes: dict[str, str] = {}
    for agent_name in (snapshot.get("candidates") or {}).keys():
        if agent_name == "csm":
            continue
        adapter = reg.get(agent_name)
        if adapter is None:
            new_hashes[agent_name] = HASH_SENTINEL_UNKNOWN
            continue
        try:
            current_body = await read_agent_side_body(
                adapter,
                resource_type,
                locator,
            )
        except Exception:
            new_hashes[agent_name] = HASH_SENTINEL_UNKNOWN
            continue
        if current_body is None:
            new_hashes[agent_name] = HASH_SENTINEL_UNKNOWN
            continue
        agent_hash = _hashlib.sha256(current_body.encode("utf-8")).hexdigest()
        new_hashes[agent_name] = make_diverged_sentinel(agent_hash)

    # Now write hashes in a short DB tx.
    async with sm_local() as db, db.begin():
        row = await db.get(model, resource_id)
        if row is None:
            return
        existing = dict(getattr(row, "last_synced_hashes", None) or {})
        existing.update(new_hashes)
        row.last_synced_hashes = existing


def _body_locator(row: Any, resource_type: str) -> str:
    """Return the locator string used by read_agent_side_body."""
    return row.name


def _dump_pending(row: PendingDecision) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_run_id": row.agent_run_id,
        "ts": _iso(row.ts),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "proposed_action": row.proposed_action,
        "candidates_json": row.candidates_json,
        "status": row.status,
        "resolution": row.resolution,
        "resolved_at": _iso(row.resolved_at),
        "applied_at": _iso(row.applied_at),
        "apply_error": row.apply_error,
        "retry_count": row.retry_count,
    }


# ---- Fanout ledger endpoints ---------------------------------------------


@router.get("/fanout-ledger")
async def list_fanout_ledger(
    status: Literal["non_done", "pending", "phase2_done", "done", "failed_terminal", "all"] = Query(
        "non_done"
    ),
    limit: int = Query(100, ge=1, le=500),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> list[dict[str, Any]]:
    async with sm() as db:
        stmt = select(FanoutLedger).order_by(FanoutLedger.ts.desc()).limit(limit)
        if status == "non_done":
            stmt = (
                select(FanoutLedger)
                .where(
                    FanoutLedger.status != "done",
                )
                .order_by(FanoutLedger.ts.desc())
                .limit(limit)
            )
        elif status != "all":
            stmt = (
                select(FanoutLedger)
                .where(
                    FanoutLedger.status == status,
                )
                .order_by(FanoutLedger.ts.desc())
                .limit(limit)
            )
        rows = (await db.execute(stmt)).scalars().all()
    return [_dump_ledger(r) for r in rows]


@router.post("/fanout-ledger/{lid}/retry")
async def retry_ledger(
    lid: int,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Reset a failed_terminal ledger row back to pending for next tick."""
    async with sm() as session, session.begin():
        row = await session.get(FanoutLedger, lid)
        if row is None:
            raise HTTPException(404, f"fanout_ledger {lid} not found")
        if row.status != "failed_terminal":
            raise HTTPException(
                409,
                f"fanout_ledger {lid} status={row.status!r} — only failed_terminal is retryable",
            )
        row.status = "pending"
        row.attempt_count = 0
    async with sm() as db:
        row = await db.get(FanoutLedger, lid)
    return _dump_ledger(row)


@router.post("/fanout-ledger/{lid}/dismiss")
async def dismiss_ledger(
    lid: int,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Manually close any non-done ledger row as 'done'."""
    async with sm() as session, session.begin():
        row = await session.get(FanoutLedger, lid)
        if row is None:
            raise HTTPException(404, f"fanout_ledger {lid} not found")
        if row.status == "done":
            raise HTTPException(409, f"fanout_ledger {lid} already done")
        row.status = "done"
    async with sm() as db:
        row = await db.get(FanoutLedger, lid)
    return _dump_ledger(row)


def _dump_ledger(row: FanoutLedger) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": _iso(row.ts),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "body_hash": row.body_hash,
        "target_agents": row.target_agents,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "attempted_at": _iso(row.attempted_at),
        "fanout_result_json": row.fanout_result_json,
    }


# ---- Policy endpoints -----------------------------------------------------


class PolicyIn(BaseModel):
    prompt: str = Field(..., min_length=100, max_length=50_000)


@router.get("/policy")
async def get_policy(
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        row = await db.get(SyncPolicy, 1)
    if row is None:
        raise HTTPException(500, "sync_policy(id=1) missing — run migrations")
    prompt_hash = (
        __import__("hashlib")
        .sha256(
            row.prompt.encode("utf-8"),
        )
        .hexdigest()
    )
    return {
        "id": row.id,
        "prompt": row.prompt,
        "prompt_hash": prompt_hash,
        "updated_at": _iso(row.updated_at),
    }


@router.put("/policy")
async def update_policy(
    body: PolicyIn,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as session, session.begin():
        row = await session.get(SyncPolicy, 1)
        if row is None:
            raise HTTPException(500, "sync_policy(id=1) missing")
        row.prompt = body.prompt
        row.updated_at = now_utc_naive()
    return await get_policy(sm=sm)


@router.post("/policy/reset")
async def reset_policy(
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Restore the shipped default prompt.

    Reads the seed prompt from the current alembic migration file so
    edits to the shipped default (via a follow-up migration) auto-flow
    here without a code change.
    """
    try:
        import importlib.util

        from csm.config import settings as _s

        # Current shipped default lives in the skill-reference migration
        # (V0_5). Edits to the default flow here via a follow-up migration
        # without a code change.
        spec = importlib.util.spec_from_file_location(
            "_seed_prompt_mod",
            str(_s.project_root / "alembic" / "versions" / "v5w6x7y8z904_sync_policy_skill_reference.py"),
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("seed migration file not locatable")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seed = mod._SEED_PROMPT_V0_5
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500,
            f"seed prompt unavailable: {exc}",
        ) from None
    async with sm() as session, session.begin():
        row = await session.get(SyncPolicy, 1)
        row.prompt = seed
        row.updated_at = now_utc_naive()
    return await get_policy(sm=sm)


# ---- Unenroll agent (clears hash keys per §5.6) ---------------------------


@router.delete("/config/{module}/agents/{agent}")
async def unenroll_agent(
    module: str,
    agent: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Remove `agent` from a module's enrolled list AND strip it from
    every resource row's `last_synced_hashes` map (design v4 §5.6)."""
    async with sm() as session, session.begin():
        cfg = (
            await session.execute(
                select(SyncConfig).where(SyncConfig.module == module),
            )
        ).scalar_one_or_none()
        if cfg is None:
            raise HTTPException(404, f"sync_config for module={module!r} not found")
        cfg.enrolled_agents = [a for a in (cfg.enrolled_agents or []) if a != agent]
        cfg.updated_at = now_utc_naive()

        # Strip hash key from every resource row.
        stripped = 0
        for cls in (Instruction, McpServer, Skill):
            rows = (await session.execute(select(cls))).scalars().all()
            for r in rows:
                hashes = dict(getattr(r, "last_synced_hashes", None) or {})
                if agent in hashes:
                    hashes.pop(agent)
                    r.last_synced_hashes = hashes
                    stripped += 1
    return {
        "module": module,
        "unenrolled_agent": agent,
        "resource_hashes_stripped": stripped,
    }


__all__ = ["router"]
