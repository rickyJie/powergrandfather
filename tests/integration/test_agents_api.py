"""Integration test for /api/agents — covers pydantic body parsing + HTTP error mapping."""
from __future__ import annotations

import os
import tempfile

import pytest_asyncio
from csm.api.agents import router as agents_router
from csm.models import Base
from csm.modules.agent.agent_store import AgentStore
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.state.agent_store = AgentStore(sessionmaker=sm)
    app.include_router(agents_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await engine.dispose()
    os.unlink(path)


async def test_list_empty(client: AsyncClient):
    r = await client.get("/api/agents")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "items": []}


async def test_create_then_get_then_list(client: AsyncClient):
    r = await client.post(
        "/api/agents",
        json={
            "name": "rev",
            "display_name": "Code Reviewer",
            "cwd": "/tmp",
            "prompt_cached": "you are a reviewer",
            "icon": "🔍",
            "description": "review diffs",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "rev"
    assert body["icon"] == "🔍"
    aid = body["id"]

    r2 = await client.get(f"/api/agents/{aid}")
    assert r2.status_code == 200
    assert r2.json()["prompt_cached"] == "you are a reviewer"

    r3 = await client.get("/api/agents")
    assert r3.status_code == 200
    j = r3.json()
    assert j["count"] == 1


async def test_create_without_prompt_cached_or_source_400(client: AsyncClient):
    r = await client.post(
        "/api/agents",
        json={"name": "x", "display_name": "X", "cwd": "/tmp"},
    )
    assert r.status_code == 400
    assert "prompt_cached" in r.json()["detail"]


async def test_create_bad_name_400(client: AsyncClient):
    r = await client.post(
        "/api/agents",
        json={
            "name": "1bad",
            "display_name": "X",
            "cwd": "/tmp",
            "prompt_cached": "p",
        },
    )
    assert r.status_code == 400


async def test_create_duplicate_400(client: AsyncClient):
    payload = {
        "name": "dup",
        "display_name": "X",
        "cwd": "/tmp",
        "prompt_cached": "p",
    }
    r1 = await client.post("/api/agents", json=payload)
    assert r1.status_code == 200
    r2 = await client.post("/api/agents", json=payload)
    assert r2.status_code == 400


async def test_patch_then_delete(client: AsyncClient):
    r = await client.post(
        "/api/agents",
        json={
            "name": "patchme",
            "display_name": "orig",
            "cwd": "/tmp",
            "prompt_cached": "p",
        },
    )
    aid = r.json()["id"]

    rp = await client.patch(
        f"/api/agents/{aid}",
        json={"display_name": "updated", "icon": "🎯"},
    )
    assert rp.status_code == 200
    assert rp.json()["display_name"] == "updated"
    assert rp.json()["icon"] == "🎯"

    rd = await client.delete(f"/api/agents/{aid}")
    assert rd.status_code == 200
    assert rd.json()["deleted"] == aid

    assert (await client.get(f"/api/agents/{aid}")).status_code == 404


async def test_get_missing_404(client: AsyncClient):
    r = await client.get("/api/agents/nonexistent")
    assert r.status_code == 404


async def test_delete_missing_404(client: AsyncClient):
    r = await client.delete("/api/agents/nonexistent")
    assert r.status_code == 404


async def test_patch_missing_404(client: AsyncClient):
    r = await client.patch(
        "/api/agents/nonexistent", json={"display_name": "x"}
    )
    assert r.status_code == 404


async def test_patch_refresh_without_source_400(client: AsyncClient):
    r = await client.post(
        "/api/agents",
        json={
            "name": "nosrc",
            "display_name": "x",
            "cwd": "/tmp",
            "prompt_cached": "p",
        },
    )
    aid = r.json()["id"]
    rp = await client.patch(
        f"/api/agents/{aid}", json={"refresh_from_source": True}
    )
    assert rp.status_code == 400
    assert "prompt_source" in rp.json()["detail"]
