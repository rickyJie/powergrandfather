"""Integration tests for /api/sync/* routes.

Uses the same FakeSyncAdapter as the SyncService unit tests + a real
FastAPI app + ASGI transport. No subprocess, no real CLI.
"""
from __future__ import annotations

import os
import tempfile

import pytest_asyncio
from csm.api.sync import router as sync_router
from csm.backends.registry import AdapterRegistry
from csm.models import Base
from csm.modules.sync.service import SyncService
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.unit.test_sync_service import FakeSyncAdapter


@pytest_asyncio.fixture
async def client(tmp_path):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_home.mkdir()
    codex_home.mkdir()
    registry = AdapterRegistry([
        FakeSyncAdapter("claude", home=claude_home),
        FakeSyncAdapter("codex", home=codex_home),
    ])

    app = FastAPI()
    app.state.sessionmaker = sm
    app.state.adapter_registry = registry
    app.state.sync_service = SyncService(sessionmaker=sm, adapter_registry=registry)
    app.include_router(sync_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, sm, {"claude": claude_home, "codex": codex_home}

    await engine.dispose()
    os.unlink(path)


# --------------------------------------------------------------- /config


async def test_list_config_empty(client):
    ac, _, _ = client
    r = await ac.get("/api/sync/config")
    assert r.status_code == 200
    body = r.json()
    assert {c["module"] for c in body["config"]} == {"memory", "mcp", "skills"}
    assert all(c["entry"] is None for c in body["config"])


async def test_put_config_creates_row(client):
    ac, _, _ = client
    r = await ac.put(
        "/api/sync/config/memory",
        json={"enrolled_agents": ["claude", "codex"], "poll_interval_sec": 30},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["module"] == "memory"
    assert j["enrolled_agents"] == ["claude", "codex"]

    r2 = await ac.get("/api/sync/config")
    entries = {c["module"]: c["entry"] for c in r2.json()["config"]}
    assert entries["memory"] is not None
    assert entries["memory"]["enrolled_agents"] == ["claude", "codex"]


async def test_put_config_unknown_agent_returns_422(client):
    ac, _, _ = client
    r = await ac.put("/api/sync/config/memory",
                     json={"enrolled_agents": ["ghost"]})
    assert r.status_code == 422
    assert "ghost" in r.text


async def test_put_config_unknown_module_returns_404(client):
    ac, _, _ = client
    r = await ac.put("/api/sync/config/whatever", json={})
    assert r.status_code == 404


# --------------------------------------------------------------- instructions


async def _enroll_memory(ac):
    r = await ac.put("/api/sync/config/memory",
                     json={"enrolled_agents": ["claude", "codex"]})
    assert r.status_code == 200


async def test_create_instruction_envelope_ok(client):
    ac, _, homes = client
    await _enroll_memory(ac)

    r = await ac.post(
        "/api/sync/memory/instructions",
        json={"name": "rules", "title": "Rules", "body": "use ruff",
              "share_scope": ["claude", "codex"], "priority": 0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["name"] == "rules"
    agent_statuses = {row["agent"]: row["status"] for row in body["sync"]}
    assert agent_statuses == {"claude": "ok", "codex": "ok"}
    assert body["warnings"] == []
    # File actually written on both agents
    for home in homes.values():
        assert "csm:start id=rules" in (home / "MEM.md").read_text()


async def test_create_instruction_partial_sync(client, tmp_path):
    """If one enrolled agent lacks the capability, envelope surfaces UNSUPPORTED
    but DB commit still succeeds → HTTP 200 (spec §2 B2)."""
    ac, sm, homes = client
    await _enroll_memory(ac)
    # Downgrade codex's capabilities to remove SYNC_MEMORY.
    from csm.backends.base import Capability
    codex = FakeSyncAdapter(
        "codex", home=homes["codex"],
        capabilities=frozenset({Capability.SYNC_MCP}),
    )
    # Swap in the registry directly (accessing private is fine in tests).
    ac._transport.app.state.adapter_registry._by_name["codex"] = codex

    r = await ac.post(
        "/api/sync/memory/instructions",
        json={"name": "rules", "title": "R", "body": "b",
              "share_scope": ["claude", "codex"], "priority": 0},
    )
    assert r.status_code == 200
    body = r.json()
    st = {row["agent"]: row["status"] for row in body["sync"]}
    assert st["claude"] == "ok"
    assert st["codex"] == "unsupported"
    assert any("codex" in w for w in body["warnings"])


async def test_create_instruction_bad_name_returns_422(client):
    ac, _, _ = client
    await _enroll_memory(ac)
    r = await ac.post(
        "/api/sync/memory/instructions",
        json={"name": "BAD_NAME", "title": "x", "body": "b",
              "share_scope": ["claude"], "priority": 0},
    )
    assert r.status_code == 422


async def test_create_instruction_duplicate_name_409(client):
    ac, _, _ = client
    await _enroll_memory(ac)
    body = {"name": "dup", "title": "x", "body": "b",
            "share_scope": ["claude"], "priority": 0}
    r1 = await ac.post("/api/sync/memory/instructions", json=body)
    assert r1.status_code == 200
    r2 = await ac.post("/api/sync/memory/instructions", json=body)
    assert r2.status_code == 409


async def test_update_and_delete_instruction(client):
    ac, _, homes = client
    await _enroll_memory(ac)
    r = await ac.post(
        "/api/sync/memory/instructions",
        json={"name": "x", "title": "t", "body": "old",
              "share_scope": ["claude"], "priority": 0},
    )
    iid = r.json()["data"]["id"]

    r = await ac.put(
        f"/api/sync/memory/instructions/{iid}",
        json={"name": "x", "title": "t2", "body": "new",
              "share_scope": ["claude"], "priority": 5},
    )
    assert r.status_code == 200
    assert "new" in (homes["claude"] / "MEM.md").read_text()

    r = await ac.delete(f"/api/sync/memory/instructions/{iid}")
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True
    assert "csm:start id=x" not in (homes["claude"] / "MEM.md").read_text()


# --------------------------------------------------------------- mcp servers


async def test_mcp_create_transport_shape_validation(client):
    ac, _, _ = client
    # Enroll first (needed so create endpoint works even with no sync effect).
    await ac.put("/api/sync/config/mcp", json={"enrolled_agents": ["claude"]})
    # stdio requires command
    r = await ac.post(
        "/api/sync/mcp/servers",
        json={"name": "bad", "transport": "stdio",
              "enabled_for": ["claude"]},
    )
    assert r.status_code == 400
    assert "stdio" in r.text and "command" in r.text.lower()
    # http requires url
    r = await ac.post(
        "/api/sync/mcp/servers",
        json={"name": "bad", "transport": "http",
              "enabled_for": ["claude"]},
    )
    assert r.status_code == 400


async def test_mcp_create_and_delete_roundtrip(client):
    ac, _, _ = client
    await ac.put("/api/sync/config/mcp", json={"enrolled_agents": ["claude"]})
    r = await ac.post(
        "/api/sync/mcp/servers",
        json={"name": "slack", "transport": "stdio", "command": "mcp-slack",
              "args_json": [], "env_json": {}, "enabled_for": ["claude"]},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["data"]["id"]
    assert r.json()["sync"][0]["status"] == "ok"

    r = await ac.delete(f"/api/sync/mcp/servers/{sid}")
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True


# --------------------------------------------------------------- skills


async def test_skill_body_md_frontmatter_required(client):
    ac, _, _ = client
    await ac.put("/api/sync/config/skills", json={"enrolled_agents": ["claude"]})
    r = await ac.post(
        "/api/sync/skills",
        json={"name": "no-fm", "description": "d", "body_md": "no frontmatter",
              "share_scope": ["claude"]},
    )
    assert r.status_code == 422
    assert "frontmatter" in r.text.lower()


async def test_skill_crud_roundtrip(client):
    ac, _, _ = client
    await ac.put("/api/sync/config/skills", json={"enrolled_agents": ["claude"]})
    body = {"name": "grep", "description": "quick",
            "body_md": "---\nname: grep\ndescription: quick\n---\n",
            "share_scope": ["claude"]}
    r = await ac.post("/api/sync/skills", json=body)
    assert r.status_code == 200, r.text
    kid = r.json()["data"]["id"]
    assert r.json()["sync"][0]["status"] == "ok"

    r = await ac.delete(f"/api/sync/skills/{kid}")
    assert r.status_code == 200


# --------------------------------------------------------------- import-preview


async def test_import_preview_mcp(client):
    ac, _, homes = client
    await ac.put("/api/sync/config/mcp", json={"enrolled_agents": ["claude"]})
    # Push one server so mcp_list has something.
    r = await ac.post(
        "/api/sync/mcp/servers",
        json={"name": "slack", "transport": "stdio", "command": "c",
              "args_json": [], "env_json": {}, "enabled_for": ["claude"]},
    )
    assert r.status_code == 200

    r = await ac.get("/api/sync/mcp/import-preview?agent=claude")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert any(e.get("name") == "slack" for e in entries)


async def test_import_preview_unknown_agent(client):
    ac, _, _ = client
    r = await ac.get("/api/sync/memory/import-preview?agent=ghost")
    assert r.status_code == 422


# --------------------------------------------------------------- drift & activity


async def test_activity_records_grow_with_writes(client):
    ac, sm, _ = client
    await _enroll_memory(ac)
    for i in range(3):
        r = await ac.post(
            "/api/sync/memory/instructions",
            json={"name": f"i{i}", "title": "x", "body": "b",
                  "share_scope": ["claude"], "priority": 0},
        )
        assert r.status_code == 200

    r = await ac.get("/api/sync/activity?limit=100")
    items = r.json()["items"]
    assert len(items) >= 3
    assert {i["action"] for i in items} == {"add"}


async def test_resolve_drift(client, tmp_path):
    """Round-trip: force an unresolved drift, then resolve it."""
    ac, sm, _ = client
    # Insert a drift row directly.
    from csm.models.drift_record import DriftRecord
    from csm.utils.time import now_utc_naive
    async with sm() as session:
        d = DriftRecord(
            ts=now_utc_naive(), module="memory", resource_type="instruction",
            resource_id=999, agent="claude", reason="external_edit",
            expected_hash="a", actual_hash="b",
        )
        session.add(d)
        await session.commit()
        await session.refresh(d)
        did = d.id

    r = await ac.get("/api/sync/drift?resolved=false")
    assert r.status_code == 200
    assert any(x["id"] == did for x in r.json()["items"])

    r = await ac.post(f"/api/sync/drift/{did}/resolve")
    assert r.status_code == 200
    assert r.json()["resolved"] is True

    r = await ac.get("/api/sync/drift?resolved=false")
    assert not any(x["id"] == did for x in r.json()["items"])


async def test_summary_status_counts_unresolved_drift(client):
    ac, sm, _ = client
    from csm.models.drift_record import DriftRecord
    from csm.utils.time import now_utc_naive
    async with sm() as session:
        for _ in range(2):
            session.add(DriftRecord(
                ts=now_utc_naive(), module="mcp",
                resource_type="mcp_server", resource_id=1,
                agent="claude", reason="missing",
            ))
        await session.commit()

    r = await ac.get("/api/sync/status")
    assert r.status_code == 200
    modules = {m["module"]: m for m in r.json()["modules"]}
    assert modules["mcp"]["unresolved_drift"] == 2
