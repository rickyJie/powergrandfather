"""Integration tests for sync v2 agent-driven endpoints (Phase 5).

Covers:
- POST /agent-tick: happy path + 409 concurrent lock
- GET /agent-runs, GET /agent-runs/{id} + live_phase
- GET /pending-decisions filter by status
- POST /pending-decisions/{id}/resolve: dismiss, take_agent, retry cap
- GET /fanout-ledger + POST retry / dismiss (with 409 guards)
- GET/PUT/POST /policy roundtrip
- DELETE /config/{module}/agents/{agent}: strips hash keys

Uses in-memory sqlite + ASGI transport; no real Anthropic (SyncAgent
patched to disabled + orchestrator run_tick monkey-patched to a fake).
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import pytest_asyncio
from csm.api.sync import router as sync_router
from csm.backends.registry import AdapterRegistry
from csm.models import Base
from csm.models.fanout_ledger import FanoutLedger
from csm.models.instruction import Instruction
from csm.models.pending_decision import PendingDecision
from csm.models.sync_agent_run import SyncAgentRun
from csm.models.sync_config import SyncConfig
from csm.models.sync_policy import SyncPolicy
from csm.modules.sync.agent import SyncAgent
from csm.modules.sync.orchestrator import SyncOrchestrator
from csm.modules.sync.service import SyncService
from csm.utils.time import now_utc_naive
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

    # Seed policy row (mimics alembic seed).
    async with sm() as db:
        db.add(SyncPolicy(id=1, prompt="X" * 200, updated_at=now_utc_naive()))
        await db.commit()

    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_home.mkdir()
    codex_home.mkdir()
    registry = AdapterRegistry([
        FakeSyncAdapter("claude", home=claude_home),
        FakeSyncAdapter("codex", home=codex_home),
    ])
    svc = SyncService(sessionmaker=sm, adapter_registry=registry)
    # SyncAgent disabled by default (no API key) — never actually calls Anthropic.
    agent = SyncAgent(sessionmaker=sm, api_key=None)
    orch = SyncOrchestrator(
        sessionmaker=sm, adapter_registry=registry,
        sync_service=svc, sync_agent=agent,
    )

    app = FastAPI()
    app.state.sessionmaker = sm
    app.state.adapter_registry = registry
    app.state.sync_service = svc
    app.state.sync_agent = agent
    app.state.sync_orchestrator = orch
    app.include_router(sync_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, sm, orch, registry

    await engine.dispose()
    os.unlink(path)


# ---------------------------------------------------------------------------
# /agent-tick
# ---------------------------------------------------------------------------


async def test_agent_tick_happy_path_returns_run_id(client):
    import asyncio

    ac, _, orch, _ = client
    # The tick now runs in the BACKGROUND (the decide step is a real session);
    # the endpoint returns immediately with the pre-created run_id + "running".
    r = await ac.post("/api/sync/agent-tick", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["run_id"] > 0
    # Let the background task drain (agent is disabled → finishes fast) so the
    # lock is released and no task is left pending at teardown.
    for _ in range(100):
        if not orch._tick_running:
            break
        await asyncio.sleep(0.02)
    assert orch._tick_running is False


async def test_agent_tick_second_call_returns_409_when_locked(client):
    ac, _, orch, _ = client
    # Simulate a manual tick already in progress.
    assert orch.try_acquire_tick() is True
    r = await ac.post("/api/sync/agent-tick", json={})
    assert r.status_code == 409
    j = r.json()
    assert j["detail"]["error"] == "tick_in_progress"
    orch.release_tick()


# ---------------------------------------------------------------------------
# /agent-runs
# ---------------------------------------------------------------------------


async def test_agent_runs_list_and_get(client):
    ac, sm, _, _ = client
    async with sm() as db:
        db.add_all([
            SyncAgentRun(
                ts=now_utc_naive() - timedelta(minutes=i),
                trigger="scheduled",
                prompt_hash="ph",
                input_state_hash="ih",
                input_snapshot_json={"x": i},
                phase="done", decisions_count=i,
            )
            for i in range(3)
        ])
        await db.commit()
    r = await ac.get("/api/sync/agent-runs?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3

    rid = body[0]["id"]
    r2 = await ac.get(f"/api/sync/agent-runs/{rid}")
    assert r2.status_code == 200
    single = r2.json()
    assert single["id"] == rid
    assert single["live_phase"] is None  # not currently running


async def test_agent_run_not_found_404(client):
    ac, _, _, _ = client
    r = await ac.get("/api/sync/agent-runs/99999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /pending-decisions + /resolve
# ---------------------------------------------------------------------------


async def _seed_pending(sm, resource_id=1, status="pending"):
    async with sm() as db:
        # First need a sync_agent_run row (FK).
        run = SyncAgentRun(
            ts=now_utc_naive(), trigger="manual",
            prompt_hash="p", input_state_hash="i",
            input_snapshot_json={}, phase="done",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        p = PendingDecision(
            agent_run_id=run.id, ts=now_utc_naive(),
            resource_type="instruction", resource_id=resource_id,
            proposed_action="propose_conflict",
            candidates_json={"claude": "v1", "codex": "v2"},
            status=status,
        )
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def test_pending_decisions_filter_default_pending(client):
    ac, sm, _, _ = client
    await _seed_pending(sm, status="pending")
    await _seed_pending(sm, resource_id=2, status="resolved")
    r = await ac.get("/api/sync/pending-decisions")
    body = r.json()
    assert len(body) == 1
    assert body[0]["status"] == "pending"


async def test_resolve_dismiss_marks_status_dismissed(client):
    ac, sm, _, _ = client
    pid = await _seed_pending(sm)
    r = await ac.post(
        f"/api/sync/pending-decisions/{pid}/resolve",
        json={"resolution": "dismiss"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "dismissed"
    assert body["retry_count"] == 1


async def test_resolve_invalid_resolution_400(client):
    ac, sm, _, _ = client
    pid = await _seed_pending(sm)
    r = await ac.post(
        f"/api/sync/pending-decisions/{pid}/resolve",
        json={"resolution": "delete_everything"},
    )
    assert r.status_code == 400


async def test_resolve_after_max_retry_returns_429(client):
    ac, sm, _, _ = client
    pid = await _seed_pending(sm, status="resolve_failed")
    # Push retry_count to 5.
    async with sm() as db:
        p = await db.get(PendingDecision, pid)
        p.retry_count = 5
        await db.commit()
    r = await ac.post(
        f"/api/sync/pending-decisions/{pid}/resolve",
        json={"resolution": "dismiss"},
    )
    assert r.status_code == 429


async def test_resolve_take_agent_happy_path(client):
    ac, sm, _, _ = client
    # Real Instruction row so takeover can update.
    async with sm() as db:
        row = Instruction(
            name="target", title="T", body="original",
            share_scope=["claude", "codex"], priority=0,
            created_at=now_utc_naive(), updated_at=now_utc_naive(),
        )
        db.add(row)
        await db.commit()
        rid = row.id
    pid = await _seed_pending(sm, resource_id=rid)
    r = await ac.post(
        f"/api/sync/pending-decisions/{pid}/resolve",
        json={"resolution": "take_agent:claude"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved", body.get("apply_error")
    async with sm() as db:
        row2 = await db.get(Instruction, rid)
    # `claude` body was "v1" per _seed_pending.
    assert row2.body == "v1"


async def test_resolve_pending_missing_returns_404(client):
    ac, _, _, _ = client
    r = await ac.post(
        "/api/sync/pending-decisions/99999/resolve",
        json={"resolution": "dismiss"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /fanout-ledger
# ---------------------------------------------------------------------------


async def _seed_ledger(sm, status="failed_terminal"):
    async with sm() as db:
        row = FanoutLedger(
            ts=now_utc_naive(), resource_type="instruction",
            resource_id=1, body_hash="h", target_agents=["claude"],
            status=status, attempt_count=3,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def test_ledger_list_default_non_done(client):
    ac, sm, _, _ = client
    await _seed_ledger(sm, status="failed_terminal")
    await _seed_ledger(sm, status="done")
    r = await ac.get("/api/sync/fanout-ledger")
    body = r.json()
    assert len(body) == 1
    assert body[0]["status"] == "failed_terminal"


async def test_ledger_retry_resets_failed_terminal_to_pending(client):
    ac, sm, _, _ = client
    lid = await _seed_ledger(sm, status="failed_terminal")
    r = await ac.post(f"/api/sync/fanout-ledger/{lid}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0


async def test_ledger_retry_rejects_non_terminal_with_409(client):
    ac, sm, _, _ = client
    lid = await _seed_ledger(sm, status="pending")
    r = await ac.post(f"/api/sync/fanout-ledger/{lid}/retry")
    assert r.status_code == 409


async def test_ledger_dismiss_marks_done(client):
    ac, sm, _, _ = client
    lid = await _seed_ledger(sm, status="pending")
    r = await ac.post(f"/api/sync/fanout-ledger/{lid}/dismiss")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"


async def test_ledger_dismiss_rejects_already_done(client):
    ac, sm, _, _ = client
    lid = await _seed_ledger(sm, status="done")
    r = await ac.post(f"/api/sync/fanout-ledger/{lid}/dismiss")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# /policy
# ---------------------------------------------------------------------------


async def test_policy_get_returns_seed(client):
    ac, _, _, _ = client
    r = await ac.get("/api/sync/policy")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1
    assert len(body["prompt"]) >= 100
    assert len(body["prompt_hash"]) == 64


async def test_policy_put_updates_prompt_and_hash(client):
    ac, _, _, _ = client
    new_prompt = "A NEW STYLE PROMPT " * 20
    r = await ac.put("/api/sync/policy", json={"prompt": new_prompt})
    assert r.status_code == 200
    body = r.json()
    assert body["prompt"] == new_prompt

    # Hash changed vs seed.
    r2 = await ac.get("/api/sync/policy")
    assert r2.json()["prompt_hash"] == body["prompt_hash"]


async def test_policy_put_rejects_too_short(client):
    ac, _, _, _ = client
    r = await ac.put("/api/sync/policy", json={"prompt": "short"})
    assert r.status_code == 422


async def test_policy_reset_restores_seed(client):
    ac, _, _, _ = client
    # Change prompt first.
    await ac.put("/api/sync/policy", json={"prompt": "X" * 200})
    r = await ac.post("/api/sync/policy/reset")
    assert r.status_code == 200
    body = r.json()
    assert "SyncAgent" in body["prompt"] or "sync" in body["prompt"].lower()


# ---------------------------------------------------------------------------
# DELETE /config/{module}/agents/{agent}
# ---------------------------------------------------------------------------


async def test_unenroll_agent_strips_hash_keys(client):
    ac, sm, _, _ = client
    # Seed sync_config + an instruction with per-agent hashes.
    async with sm() as db:
        db.add(SyncConfig(
            module="memory", enrolled_agents=["claude", "codex"],
            poll_interval_sec=30, enabled=True,
            updated_at=now_utc_naive(),
        ))
        row = Instruction(
            name="w", title="T", body="body",
            share_scope=["claude", "codex"], priority=0,
            created_at=now_utc_naive(), updated_at=now_utc_naive(),
            last_synced_hashes={"claude": "h1", "codex": "h2"},
        )
        db.add(row)
        await db.commit()
        rid = row.id

    r = await ac.delete("/api/sync/config/memory/agents/codex")
    assert r.status_code == 200
    body = r.json()
    assert body["unenrolled_agent"] == "codex"
    assert body["resource_hashes_stripped"] == 1

    async with sm() as db:
        row2 = await db.get(Instruction, rid)
    assert "codex" not in row2.last_synced_hashes
    assert "claude" in row2.last_synced_hashes


async def test_unenroll_unknown_module_404(client):
    ac, _, _, _ = client
    r = await ac.delete("/api/sync/config/mystery/agents/claude")
    assert r.status_code == 404
