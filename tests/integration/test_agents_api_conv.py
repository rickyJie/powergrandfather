"""Integration test for /api/agents conversation endpoints (spawn/end/messages).

Uses `bash` as a claude stand-in via SessionManager so the real PTY plumbing
runs without depending on the actual claude binary.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest_asyncio
from csm.api.agents import router as agents_router
from csm.backends import build_default_registry
from csm.core.event_stream import EventStream
from csm.models import Base
from csm.modules.agent.agent_store import AgentStore
from csm.modules.agent.conversation import AgentConversationManager
from csm.modules.session_manager.manager import SessionManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_proj = tempfile.mkdtemp()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    es = EventStream(
        projects_root=Path(tmp_proj),
        poll_interval_sec=10.0,
        watchdog_interval_sec=10.0,
    )
    sessman = SessionManager(
        sessionmaker=sm,
        event_stream=es,
        adapter_registry=build_default_registry(),
        ring_buffer_bytes=4096,
        stop_grace_sec=1,
        claude_argv=["bash", "-i"],
    )
    store = AgentStore(sessionmaker=sm)
    convmgr = AgentConversationManager(sessionmaker=sm, session_manager=sessman)

    app = FastAPI()
    app.state.agent_store = store
    app.state.agent_conv_manager = convmgr
    app.include_router(agents_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await sessman.shutdown()
    await es.stop()
    await engine.dispose()
    os.unlink(path)


async def _create_agent(client: AsyncClient, name: str = "rev") -> str:
    r = await client.post(
        "/api/agents",
        json={
            "name": name,
            "display_name": name,
            "cwd": "/tmp",
            "prompt_cached": "you are a reviewer",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_spawn_returns_conversation_with_session(client: AsyncClient):
    aid = await _create_agent(client)
    r = await client.post(f"/api/agents/{aid}/spawn")
    assert r.status_code == 200
    body = r.json()
    assert body["reused"] is False
    assert body["agent_def_id"] == aid
    assert body["session_id"]


async def test_spawn_twice_reuses(client: AsyncClient):
    aid = await _create_agent(client)
    first = (await client.post(f"/api/agents/{aid}/spawn")).json()
    second = (await client.post(f"/api/agents/{aid}/spawn")).json()
    assert second["reused"] is True
    assert second["id"] == first["id"]


async def test_active_conversation_query(client: AsyncClient):
    aid = await _create_agent(client)
    r0 = await client.get(f"/api/agents/{aid}/active-conversation")
    assert r0.json()["active"] is None

    spawn = (await client.post(f"/api/agents/{aid}/spawn")).json()
    r1 = await client.get(f"/api/agents/{aid}/active-conversation")
    assert r1.json()["active"]["id"] == spawn["id"]


async def test_send_message_to_live(client: AsyncClient):
    aid = await _create_agent(client)
    cid = (await client.post(f"/api/agents/{aid}/spawn")).json()["id"]
    r = await client.post(
        f"/api/agents/conversations/{cid}/messages",
        json={"text": "echo hello_agent_e2e"},
    )
    assert r.status_code == 200


async def test_end_conversation(client: AsyncClient):
    aid = await _create_agent(client)
    cid = (await client.post(f"/api/agents/{aid}/spawn")).json()["id"]
    r = await client.delete(f"/api/agents/conversations/{cid}")
    assert r.status_code == 200
    # Sending after end → 409.
    r2 = await client.post(
        f"/api/agents/conversations/{cid}/messages",
        json={"text": "too late"},
    )
    assert r2.status_code == 409


async def test_delete_agent_with_active_conversation_409(client: AsyncClient):
    aid = await _create_agent(client)
    await client.post(f"/api/agents/{aid}/spawn")
    r = await client.delete(f"/api/agents/{aid}")
    assert r.status_code == 409
    assert "active conversation" in r.json()["detail"]


async def test_spawn_unknown_agent_404(client: AsyncClient):
    r = await client.post("/api/agents/nope/spawn")
    assert r.status_code == 404


async def test_get_conversation_unknown_404(client: AsyncClient):
    r = await client.get("/api/agents/conversations/nope")
    assert r.status_code == 404


async def test_send_empty_message_422(client: AsyncClient):
    aid = await _create_agent(client)
    cid = (await client.post(f"/api/agents/{aid}/spawn")).json()["id"]
    r = await client.post(
        f"/api/agents/conversations/{cid}/messages", json={"text": ""}
    )
    # Pydantic min_length=1 → 422.
    assert r.status_code == 422
