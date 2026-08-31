"""Unit tests for the SyncAgent AUTO-session decide path.

Verifies that decide() spawns a session under the default agent, harvests
`decisions.json`, and parses it — without any ANTHROPIC_API_KEY. The session
manager + event stream are faked: create_session writes the file (standing in
for the real agent) and schedules the assistant-done event that unblocks the
harvest.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from csm.core.events import Event, EventType
from csm.models import UserPreference
from csm.models.base import Base
from csm.models.sync_policy import SyncPolicy
from csm.modules.sync.agent import SyncAgent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

VALID_DECISIONS = json.dumps({"decisions": [], "summary": "nothing to do"})


@pytest.fixture
async def sm(tmp_path, monkeypatch):
    # Keep the scratch dir inside the test tmp so we never touch ~/.csm, and
    # point HOME at tmp so the claude-trust pre-seed can't mutate the real
    # ~/.claude.json (it early-returns when the file is absent).
    monkeypatch.setenv("CSM_SYNC_DECIDE_CWD", str(tmp_path / "sd"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CSM_SYNC_DISABLED", raising=False)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(engine, expire_on_commit=False)
    async with SM() as db:
        db.add(SyncPolicy(id=1, prompt="POLICY TEXT", updated_at=datetime.utcnow()))
        db.add(UserPreference(id=1, default_agent="claude"))
        await db.commit()
    yield SM
    await engine.dispose()


class FakeEventStream:
    def __init__(self):
        self._handlers: dict[str, object] = {}

    def subscribe(self, types, handler):
        self._handlers["s"] = handler
        return "s"

    def unsubscribe(self, sub_id):
        self._handlers.pop(sub_id, None)

    async def fire(self, event: Event):
        for h in list(self._handlers.values()):
            await h(event)


class FakeSessionManager:
    """create_session writes `decisions.json` (as the agent would) and fires a
    delayed assistant-done event so the harvest wait unblocks."""

    def __init__(self, event_stream: FakeEventStream, decisions_body: str,
                 write_file: bool = True):
        self._es = event_stream
        self._body = decisions_body
        self._write = write_file
        self.stopped: list[str] = []
        self.spawned_agent: str | None = None

    async def create_session(self, cwd, type, title=None, initial_prompt=None,
                             agent="claude", **kw):
        self.spawned_agent = agent
        if self._write:
            Path(cwd, "decisions.json").write_text(self._body, encoding="utf-8")
        sid = "sess-1"

        async def _fire():
            await asyncio.sleep(0.01)
            await self._es.fire(Event(
                type=EventType.MESSAGE_ASSISTANT_DONE,
                ts=datetime.utcnow(),
                session_id="claude-uuid",
                project_path=str(cwd),
                payload={"csm_session_id": sid, "assistant_text": "done"},
            ))

        asyncio.create_task(_fire())
        return SimpleNamespace(id=sid)

    async def stop_session(self, sid, graceful=True):
        self.stopped.append(sid)
        return 0


@pytest.mark.asyncio
async def test_session_path_parses_decisions_file(sm):
    es = FakeEventStream()
    mgr = FakeSessionManager(es, VALID_DECISIONS)
    agent = SyncAgent(sessionmaker=sm, session_manager=mgr, event_stream=es)

    assert agent.enabled is True  # session path is ready → enabled w/o api key

    payload, meta = await agent.decide({"csm_resources": {}, "agents": {}})

    assert payload is not None
    assert payload.summary == "nothing to do"
    assert meta.get("error") is None and meta.get("parse_error") is None
    assert meta["model"] == "session:claude"          # honored default_agent
    assert mgr.spawned_agent == "claude"
    assert mgr.stopped == ["sess-1"]                   # session cleaned up


@pytest.mark.asyncio
async def test_session_path_honors_default_agent(sm):
    # Flip the user's default agent to codex → session spawns under codex.
    async with sm() as db:
        pref = await db.get(UserPreference, 1)
        pref.default_agent = "codex"
        await db.commit()

    es = FakeEventStream()
    mgr = FakeSessionManager(es, VALID_DECISIONS)
    agent = SyncAgent(sessionmaker=sm, session_manager=mgr, event_stream=es)

    payload, meta = await agent.decide({"csm_resources": {}, "agents": {}})
    assert payload is not None
    assert mgr.spawned_agent == "codex"
    assert meta["model"] == "session:codex"


@pytest.mark.asyncio
async def test_session_path_missing_file_is_error(sm):
    es = FakeEventStream()
    mgr = FakeSessionManager(es, VALID_DECISIONS, write_file=False)
    agent = SyncAgent(sessionmaker=sm, session_manager=mgr, event_stream=es)

    payload, meta = await agent.decide({"csm_resources": {}, "agents": {}})
    assert payload is None
    assert "no_decisions_file" in meta["error"]
    assert mgr.stopped == ["sess-1"]  # still cleans up the session


@pytest.mark.asyncio
async def test_disabled_when_no_session_infra_and_no_key(sm, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = SyncAgent(sessionmaker=sm)  # no session_manager / event_stream
    assert agent.enabled is False
    payload, meta = await agent.decide({})
    assert payload is None
    assert meta["error"] == "sync_agent_disabled"


@pytest.mark.asyncio
async def test_disabled_flag_forces_off_even_with_session_infra(sm, monkeypatch):
    monkeypatch.setenv("CSM_SYNC_DISABLED", "1")
    es = FakeEventStream()
    mgr = FakeSessionManager(es, VALID_DECISIONS)
    agent = SyncAgent(sessionmaker=sm, session_manager=mgr, event_stream=es)
    assert agent.enabled is False
