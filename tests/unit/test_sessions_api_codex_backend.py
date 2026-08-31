"""Unit tests for the codex agent guard on POST /api/sessions.

Multi-agent v2: `backend` is a deprecated alias for `agent`. Both fields
are accepted on input; the response echoes both under the transitional
compat window. Guard behaviour:
  1. `agent=codex` (or legacy `backend=codex`) is enabled by default and
     refused with 400 only when `settings.enable_codex` is explicitly false.
  2. When enable_codex is on, argv[0] must be 'codex' (unless
     CSM_ALLOW_ARBITRARY_ARGV=1) — the allowlist is now agent-aware.
  3. Response includes both `agent` / `rollout_path` (canonical) AND
     `backend` / `codex_rollout_path` (deprecated aliases).

These tests use _FakeManager / _FakeRow stubs so no real DB or codex
binary is required.
"""
from __future__ import annotations

from datetime import datetime

import pytest_asyncio
from csm.api.sessions import router as sessions_router
from csm.models.session import SessionStatus, SessionType
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _FakeRow:
    def __init__(self, cwd: str, agent: str = "claude") -> None:
        self.id = "sess-fake-codex"
        self.title = None
        self.type = SessionType.INTERACTIVE
        self.cwd = cwd
        self.status = SessionStatus.RUNNING
        self.pid = 4242
        self.started_at = datetime.utcnow()
        self.ended_at = None
        self.exit_code = None
        self.external_session_id = None
        self.agent = agent
        self.rollout_path = None
        self.superseded_by = None
        self.associated_run_id = None
        self.tags = []
        self.last_activity_ts = None
        self.current_tool = None
        self.last_assistant_msg = None
        self.unread_count = 0
        self.session_project_id = None
        self.pinned = False
        self.manual_unread = False


class _FakeManager:
    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []

    async def create_session(self, **kwargs):
        self.spawn_calls.append(kwargs)
        return _FakeRow(cwd=kwargs.get("cwd", "/tmp"),
                        agent=kwargs.get("agent", "claude"))

    async def get_session(self, sid):
        return None


@pytest_asyncio.fixture
async def client(tmp_path):
    app = FastAPI()
    app.state.session_manager = _FakeManager()
    app.state.sessionmaker = None  # not touched by these tests
    app.include_router(sessions_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, app


async def test_agent_codex_rejected_when_flag_off(client, tmp_path, monkeypatch):
    ac, _app = client
    from csm.config import settings as _settings
    monkeypatch.setattr(_settings, "enable_codex", False)
    resp = await ac.post(
        "/api/sessions",
        json={"cwd": str(tmp_path), "agent": "codex", "argv": ["codex"]},
    )
    assert resp.status_code == 400
    assert "CSM_ENABLE_CODEX" in resp.json()["detail"]


async def test_legacy_backend_field_still_accepted(client, tmp_path, monkeypatch):
    """`backend` alias must still work for one release."""
    ac, _app = client
    from csm.config import settings as _settings
    monkeypatch.setattr(_settings, "enable_codex", False)
    resp = await ac.post(
        "/api/sessions",
        json={"cwd": str(tmp_path), "backend": "codex", "argv": ["codex"]},
    )
    assert resp.status_code == 400  # same rejection as `agent: "codex"`


async def test_agent_codex_argv0_must_be_codex(client, tmp_path, monkeypatch):
    ac, _app = client
    from csm.config import settings as _settings
    monkeypatch.setattr(_settings, "enable_codex", True)
    resp = await ac.post(
        "/api/sessions",
        json={"cwd": str(tmp_path), "agent": "codex", "argv": ["claude"]},
    )
    assert resp.status_code == 400
    assert "argv[0] must be 'codex'" in resp.json()["detail"]


async def test_agent_codex_accepts_valid_argv(client, tmp_path, monkeypatch):
    ac, app = client
    from csm.config import settings as _settings
    monkeypatch.setattr(_settings, "enable_codex", True)
    resp = await ac.post(
        "/api/sessions",
        json={"cwd": str(tmp_path), "agent": "codex", "argv": ["codex"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Canonical field
    assert body["agent"] == "codex"
    assert body["rollout_path"] is None
    # Deprecated aliases still present
    assert body["backend"] == "codex"
    assert body["codex_rollout_path"] is None
    # Manager received the resolved agent name
    mgr = app.state.session_manager
    assert mgr.spawn_calls[-1]["agent"] == "codex"


async def test_agent_claude_still_default_and_unchanged(client, tmp_path):
    """The whole point of compat: claude flow must remain byte-identical
    to what it was before the v2 field rename."""
    ac, app = client
    resp = await ac.post(
        "/api/sessions",
        json={"cwd": str(tmp_path), "argv": ["claude"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "claude"
    assert body["backend"] == "claude"  # alias
    assert body["rollout_path"] is None
    assert body["codex_rollout_path"] is None
