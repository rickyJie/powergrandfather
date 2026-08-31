"""REST endpoints for the AgentDeck module.

CRUD on AgentDefinition + spawn/end/send for AgentConversation. WS endpoint
for the structured message stream is added in P3.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from csm.api._deps import _require_access_ws
from csm.api._serialize import iso_utc
from csm.models import AgentConversation, AgentDefinition, Session
from csm.modules.agent.agent_store import (
    AgentCreate,
    AgentPatch,
    AgentStore,
    AgentStoreError,
)
from csm.modules.agent.conversation import (
    AgentConversationError,
    AgentConversationManager,
)

router = APIRouter(prefix="/api", tags=["agents"])


def _store(request: Request) -> AgentStore:
    s = getattr(request.app.state, "agent_store", None)
    if s is None:
        raise HTTPException(status_code=503, detail="agent store not initialized")
    return s


def _convmgr(request: Request) -> AgentConversationManager:
    m = getattr(request.app.state, "agent_conv_manager", None)
    if m is None:
        raise HTTPException(status_code=503, detail="agent conversation manager not initialized")
    return m


def _conv_dict(c: AgentConversation, sess: Session | None = None) -> dict[str, Any]:
    return {
        "id": c.id,
        "agent_def_id": c.agent_def_id,
        "session_id": c.session_id,
        "title": c.title,
        "created_at": iso_utc(c.created_at),
        "last_activity_ts": iso_utc(c.last_activity_ts),
        "ended_at": iso_utc(c.ended_at),
        "session_status": (sess.status.value if sess and sess.status else None),
        "external_session_id": (sess.external_session_id if sess else None),
        # Deprecated alias — one release compat window.
        "claude_session_id": (sess.external_session_id if sess else None),
        "cwd": (sess.cwd if sess else None),
    }


def _agent_dict(a: AgentDefinition) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "display_name": a.display_name,
        "icon": a.icon,
        "description": a.description,
        "cwd": a.cwd,
        "prompt_source": a.prompt_source,
        "prompt_cached": a.prompt_cached,
        "disable_skills": bool(a.disable_skills),
        "created_at": iso_utc(a.created_at),
        "updated_at": iso_utc(a.updated_at),
    }


class CreateAgentBody(BaseModel):
    name: str = Field(..., description="machine id, [a-zA-Z][a-zA-Z0-9_]*")
    display_name: str
    cwd: str
    prompt_cached: str | None = Field(
        None, description="explicit prompt body; if absent, prompt_source is resolved"
    )
    prompt_source: str | None = Field(
        None, description="local file path or http(s):// URL"
    )
    icon: str | None = None
    description: str | None = None
    disable_skills: bool = Field(
        False,
        description="if True, spawn passes --disallowedTools Skill so the agent cannot call any Claude Code skill",
    )


class PatchAgentBody(BaseModel):
    display_name: str | None = None
    cwd: str | None = None
    prompt_cached: str | None = None
    prompt_source: str | None = None
    icon: str | None = None
    description: str | None = None
    disable_skills: bool | None = None
    refresh_from_source: bool = Field(
        False,
        description="if True and prompt_source is set, re-read the source and overwrite prompt_cached",
    )


@router.get("/agents")
async def list_agents(request: Request):
    rows = await _store(request).list()
    return {"count": len(rows), "items": [_agent_dict(a) for a in rows]}


@router.get("/agents-overview")
async def list_agents_overview(request: Request, history_per_agent: int = 5):
    """One-shot overview for AgentDeck: every agent + its active conversation +
    up to N recent ended conversations. Replaces the N+1 fan-out the deck used
    to do (list + activeConversation per agent)."""
    store = _store(request)
    convmgr = _convmgr(request)
    rows = await store.list()
    out = []
    for a in rows:
        convs = await convmgr.list_for_agent(a.id, include_ended=True)
        active_conv = None
        for c in convs:
            if c.ended_at is None:
                pair = await convmgr.get(c.id)
                if pair is not None:
                    active_conv = _conv_dict(pair[0], pair[1])
                break
        history = []
        for c in convs:
            if c.ended_at is None:
                continue
            if len(history) >= history_per_agent:
                break
            pair = await convmgr.get(c.id)
            if pair is not None:
                history.append(_conv_dict(pair[0], pair[1]))
        out.append({
            "agent": _agent_dict(a),
            "active": active_conv,
            "history": history,
        })
    return {"count": len(out), "items": out}


@router.get("/agents/{agent_id}/conversations")
async def list_agent_conversations(
    agent_id: str, request: Request, include_ended: bool = True, limit: int = 50
):
    """List conversations (active + ended) for a single agent. Used by the
    history drawer on the agent card."""
    convmgr = _convmgr(request)
    convs = await convmgr.list_for_agent(agent_id, include_ended=include_ended)
    out = []
    for c in convs[:limit]:
        pair = await convmgr.get(c.id)
        if pair is not None:
            out.append(_conv_dict(pair[0], pair[1]))
    return {"count": len(out), "items": out}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    row = await _store(request).get(agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return _agent_dict(row)


@router.post("/agents")
async def create_agent(body: CreateAgentBody, request: Request):
    prompt_cached = body.prompt_cached
    if prompt_cached is None:
        if not body.prompt_source:
            raise HTTPException(
                status_code=400,
                detail="either prompt_cached or prompt_source is required",
            )
        try:
            from csm.modules.agent.prompt_loader import load_prompt
            prompt_cached = await load_prompt(body.prompt_source)
        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="prompt_source resolution not yet wired; supply prompt_cached directly",
            )
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"prompt_source load failed: {e}"
            ) from e
    payload = AgentCreate(
        name=body.name,
        display_name=body.display_name,
        cwd=body.cwd,
        prompt_cached=prompt_cached,
        prompt_source=body.prompt_source,
        icon=body.icon,
        description=body.description,
        disable_skills=body.disable_skills,
    )
    try:
        row = await _store(request).create(payload)
    except AgentStoreError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _agent_dict(row)


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, body: PatchAgentBody, request: Request):
    prompt_cached = body.prompt_cached
    if body.refresh_from_source:
        source = body.prompt_source
        if source is None:
            existing = await _store(request).get(agent_id)
            source = existing.prompt_source if existing else None
        if not source:
            raise HTTPException(
                status_code=400,
                detail="refresh_from_source requires a prompt_source on the row or in the body",
            )
        try:
            from csm.modules.agent.prompt_loader import load_prompt
            prompt_cached = await load_prompt(source)
        except ImportError:
            raise HTTPException(
                status_code=400, detail="prompt_source resolution not yet wired"
            )
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"prompt_source load failed: {e}"
            ) from e
    payload = AgentPatch(
        display_name=body.display_name,
        cwd=body.cwd,
        prompt_cached=prompt_cached,
        prompt_source=body.prompt_source,
        icon=body.icon,
        description=body.description,
        disable_skills=body.disable_skills,
    )
    try:
        row = await _store(request).patch(agent_id, payload)
    except AgentStoreError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return _agent_dict(row)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    try:
        ok = await _store(request).delete(agent_id)
    except AgentStoreError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": agent_id}


# ---- conversations ----
@router.post("/agents/{agent_id}/spawn")
async def spawn_conversation(agent_id: str, request: Request):
    """Spawn (or reuse) the single live conversation for this agent."""
    try:
        result = await _convmgr(request).spawn(agent_id)
    except AgentConversationError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {
        "reused": result.reused,
        **_conv_dict(result.conversation, result.session),
    }


@router.get("/agents/{agent_id}/active-conversation")
async def get_active_conversation(agent_id: str, request: Request):
    """Return the live conversation (if any) without spawning. Used by the card
    state-dot query in the deck UI."""
    conv = await _convmgr(request).active_conversation_for_agent(agent_id)
    if conv is None:
        return {"active": None}
    pair = await _convmgr(request).get(conv.id)
    if pair is None:
        return {"active": None}
    c, s = pair
    return {"active": _conv_dict(c, s)}


@router.get("/agents/conversations/{cid}")
async def get_conversation(cid: str, request: Request):
    pair = await _convmgr(request).get(cid)
    if pair is None:
        raise HTTPException(status_code=404, detail="not found")
    conv, sess = pair
    return _conv_dict(conv, sess)


@router.delete("/agents/conversations/{cid}")
async def end_conversation(cid: str, request: Request):
    ok = await _convmgr(request).end(cid)
    if not ok:
        raise HTTPException(status_code=404, detail="not found or already ended")
    return {"ended": cid}


class SendMessageBody(BaseModel):
    text: str = Field(..., min_length=1)


@router.post("/agents/conversations/{cid}/messages")
async def send_message(cid: str, body: SendMessageBody, request: Request):
    ok = await _convmgr(request).send_user_message(cid, body.text)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="conversation not live (ended, missing, or session dead)",
        )
    return {"sent": cid}


@router.post("/agents/conversations/{cid}/interrupt")
async def interrupt_conversation(cid: str, request: Request):
    """Send Ctrl-C (0x03) to the conversation's PTY — the TTY driver turns it
    into SIGINT for claude, which interrupts in-flight generation/tool loops
    without killing the session."""
    convmgr = _convmgr(request)
    pair = await convmgr.get(cid)
    if pair is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    conv, sess = pair
    if conv.ended_at is not None or sess is None:
        raise HTTPException(status_code=409, detail="conversation not live")
    sm = request.app.state.session_manager
    written = await sm.write_input(conv.session_id, b"\x03")
    return {"interrupted": cid, "bytes_written": written}


log = logging.getLogger(__name__)


@router.websocket("/ws/agents/conversations/{cid}")
async def conversation_stream(ws: WebSocket, cid: str):
    """Push structured chat events to the client.

    Lifecycle:
      1. Accept the WS.
      2. Resolve the conversation → session.cwd + claude_session_id.
      3. Spin up a JsonlFastTail; first scan replays history.
      4. Forward each parsed event as JSON.
      5. Wait for disconnect; stop the tail.

    The WS doesn't accept incoming text — message sending goes through
    POST /api/agents/conversations/{cid}/messages.
    """
    from csm.config import settings
    from csm.modules.agent.jsonl_fast_tail import (
        JsonlFastTail,
        conversation_jsonl_path,
    )

    _require_access_ws(ws)
    await ws.accept()
    app = ws.app
    convmgr = getattr(app.state, "agent_conv_manager", None)
    if convmgr is None:
        await ws.send_json({"type": "error", "detail": "conversation manager not ready"})
        await ws.close(code=4500)
        return
    pair = await convmgr.get(cid)
    if pair is None:
        await ws.send_json({"type": "error", "detail": "conversation not found"})
        await ws.close(code=4404)
        return
    conv, sess = pair
    if not sess or not sess.external_session_id:
        await ws.send_json({"type": "error", "detail": "session has no claude_session_id"})
        await ws.close(code=4500)
        return

    path = conversation_jsonl_path(
        settings.claude_projects_dir, sess.cwd, sess.external_session_id
    )
    await ws.send_json({
        "type": "session_status",
        "status": sess.status.value,
        "external_session_id": sess.external_session_id,
        # Deprecated alias — one release compat window.
        "claude_session_id": sess.external_session_id,
        "jsonl_path": str(path),
    })

    send_lock = asyncio.Lock()

    async def on_event(event: dict) -> None:
        async with send_lock:
            try:
                await ws.send_json(event)
            except Exception:
                log.exception("ws send failed for %s", cid)

    async def on_replay(events: list[dict]) -> None:
        # Ship the whole replay as one frame to avoid N round-trips on connect.
        async with send_lock:
            try:
                await ws.send_json({"type": "history", "events": events})
            except Exception:
                log.exception("ws send failed for %s (history)", cid)

    tail = JsonlFastTail(path=path, on_event=on_event, poll_interval_sec=0.2)
    await tail.start(replay_from_start=True, on_replay=on_replay)

    try:
        while True:
            # No app input expected here except the mobile heartbeat: the client
            # pings so it can detect a silently-dead socket over a stalled SSH
            # tunnel. Reply so its staleness watchdog stays fed.
            msg = await ws.receive_text()
            if msg == "ping":
                async with send_lock:
                    try:
                        await ws.send_json({"type": "pong"})
                    except Exception:
                        break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws receive failed for %s", cid)
    finally:
        await tail.stop()
