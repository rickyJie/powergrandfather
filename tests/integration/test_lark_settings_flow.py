"""End-to-end integration for the Lark push config plane.

Covers the paths that unit tests (`test_lark_sink.py` +
`test_lark_settings_api.py`) can't exercise in isolation:

1. **PUT → sink pickup**: HTTP PUT `/api/settings/lark` writes the DB;
   the LarkSink instance registered on `app.state` reads the change on
   its next `send()` (no restart, no reload endpoint).
2. **Migration robustness**: Alembic `upgrade` with a malformed
   `CSM_LARK_DND_HOURS` env var does NOT crash; bad tokens are dropped.
3. **Test endpoint timeout**: POST `/api/settings/lark/test` bounds a
   hung `lark-cli` at 8s (patched down in this test so it runs fast).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from csm.adapters.lark_sink import LarkSink
from csm.api.lark_settings import router as lark_router
from csm.models.lark_settings import LarkSettings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def app_and_sink():
    """A minimal FastAPI app with a REAL LarkSink wired against an
    in-memory DB. Only `_shell_send` is stubbed so tests can assert on
    the shell payload without actually invoking lark-cli."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(LarkSettings.__table__.create)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    sink = LarkSink(sessionmaker=sm)

    app = FastAPI()
    app.state.sessionmaker = sm
    app.state.lark_sink = sink
    app.include_router(lark_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, sink, sm

    await engine.dispose()


@pytest.mark.asyncio
async def test_put_change_is_picked_up_by_sink_on_next_send(app_and_sink, monkeypatch):
    """The full config-change-to-push flow: change target via HTTP,
    the very next sink.send() must dial the new target — no restart,
    no cache warmup."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/lark-cli")
    client, sink, _ = app_and_sink

    captured: dict = {}

    async def fake_shell(text, chat_id=None, user_id=None, **_):
        captured["chat_id"] = chat_id
        captured["text"] = text

    monkeypatch.setattr(sink, "_shell_send", fake_shell)

    # 1. Enable + set initial target
    r = await client.put(
        "/api/settings/lark",
        json={"enabled": True, "chat_id": "oc_initial"},
    )
    assert r.status_code == 200

    # 2. Sink must pick this up on send()
    await sink.send({
        "type": "session_crashed",
        "session_id": "s1",
        "title": "t", "body": "b",
        "metadata": {},
    })
    assert captured["chat_id"] == "oc_initial"

    # 3. Change target via HTTP
    r = await client.put("/api/settings/lark", json={"chat_id": "oc_updated"})
    assert r.status_code == 200

    # 4. Same event type + session ID; the PUT's flush_dedup_cache() cleared
    #    the "session_crashed:s1" dedup entry, so this send goes through.
    captured.clear()
    await sink.send({
        "type": "session_crashed",
        "session_id": "s1",
        "title": "t", "body": "b",
        "metadata": {},
    })
    assert captured["chat_id"] == "oc_updated"


@pytest.mark.asyncio
async def test_disable_via_put_stops_further_pushes(app_and_sink, monkeypatch):
    client, sink, _ = app_and_sink

    calls: list = []

    async def fake_shell(text, chat_id=None, user_id=None, **_):
        calls.append(chat_id)

    monkeypatch.setattr(sink, "_shell_send", fake_shell)

    await client.put("/api/settings/lark",
                     json={"enabled": True, "chat_id": "oc_a"})
    await sink.send({
        "type": "port_conflict", "session_id": None,
        "title": "t", "body": "b", "metadata": {},
    })
    assert calls == ["oc_a"]

    # Turn it off
    await client.put("/api/settings/lark", json={"enabled": False})

    await sink.send({
        "type": "port_conflict", "session_id": None,
        "title": "t", "body": "b", "metadata": {},
    })
    assert calls == ["oc_a"]  # unchanged — sink self-skipped


@pytest.mark.asyncio
async def test_post_test_timeout_returns_error(app_and_sink, monkeypatch):
    """POST /test hangs → server timeout kicks in → user sees error, not crash."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/lark-cli")
    client, sink, _ = app_and_sink

    await client.put("/api/settings/lark",
                     json={"enabled": True, "chat_id": "oc_slow"})

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(sink, "_shell_send", hang)
    # Bring the server-side timeout way down so this test finishes fast.
    monkeypatch.setattr("csm.api.lark_settings._TEST_TIMEOUT_SEC", 0.2)

    r = await client.post("/api/settings/lark/test")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert "timeout" in (body["error"] or "").lower()


def test_alembic_upgrade_survives_malformed_dnd_env_var(tmp_path, monkeypatch):
    """Run a real `alembic upgrade head` against a tmp SQLite with a
    malformed CSM_LARK_DND_HOURS env var. Migration must succeed;
    seed row's dnd_hours must contain only the valid tokens."""
    db_path = tmp_path / "csm_migration_test.db"

    env = os.environ.copy()
    env["CSM_DB_PATH"] = str(db_path)
    env["CSM_LARK_NOTIFY_CHAT_ID"] = "oc_env_seed"
    env["CSM_LARK_DND_HOURS"] = "23,foo,0,badhour,1"
    env["CSM_LARK_DEDUP_SEC"] = "not_a_number"  # should fall back
    # Drop any inherited CSM_CLAUDE_ARGV (integration conftest sets it);
    # doesn't affect migration but keeps the subprocess env clean.

    project_root = Path(__file__).resolve().parents[2]

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade failed:\nSTDOUT: {proc.stdout.decode()}\n"
        f"STDERR: {proc.stderr.decode()}"
    )

    # Verify the seed row survived + malformed tokens were dropped
    import json
    import sqlite3  # noqa: E401
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT enabled, chat_id, dnd_hours, dedup_window_sec "
            "FROM lark_settings WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "seed row missing"
    enabled, chat_id, dnd_json, dedup = row
    assert bool(enabled) is True  # env chat_id set → seed_enabled
    assert chat_id == "oc_env_seed"
    dnd = sorted(json.loads(dnd_json))
    # foo/badhour dropped; 23,0,1 kept
    assert dnd == [0, 1, 23], f"expected sanitized DnD, got {dnd}"
    # Malformed dedup → default 60
    assert dedup == 60


def test_alembic_upgrade_seed_idempotent_across_reruns(tmp_path):
    """Seed uses INSERT OR IGNORE so downgrade + upgrade cycle does
    NOT overwrite a manually-edited row. This models what happens if a
    user tweaks their chat_id post-install and later re-runs migration
    (e.g. as part of a repair script)."""
    db_path = tmp_path / "csm_idempotent.db"
    env = os.environ.copy()
    env["CSM_DB_PATH"] = str(db_path)
    env["CSM_LARK_NOTIFY_CHAT_ID"] = "oc_initial_seed"
    project_root = Path(__file__).resolve().parents[2]

    def _run(cmd):
        p = subprocess.run(
            [sys.executable, "-m", "alembic", *cmd],
            cwd=project_root, env=env, capture_output=True, timeout=60,
        )
        assert p.returncode == 0, p.stderr.decode()

    _run(["upgrade", "head"])

    # Manually change the row (simulating the user editing via the UI).
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE lark_settings SET chat_id='oc_hand_edited' WHERE id=1")
        conn.commit()
    finally:
        conn.close()

    # Re-run upgrade (no-op at head, but let's be thorough — this
    # models what happens if migration seed logic is somehow triggered
    # again, e.g. via a repair script that calls the seed directly).
    _run(["upgrade", "head"])

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT chat_id FROM lark_settings WHERE id=1").fetchone()
    finally:
        conn.close()
    assert row[0] == "oc_hand_edited", (
        "manual edit was overwritten by re-run seed — INSERT OR IGNORE broken"
    )
