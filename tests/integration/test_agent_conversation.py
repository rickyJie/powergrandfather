"""Integration test for AgentConversationManager — spawn / end / single-instance.

Uses `bash` as a claude stand-in; the Session row + PTY are real, but no
JSONL is produced (that's exercised in P3's tests).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from csm.backends import build_default_registry
from csm.core.event_stream import EventStream
from csm.models import Base
from csm.models.session import SessionStatus
from csm.modules.agent.agent_store import AgentCreate, AgentStore
from csm.modules.agent.conversation import (
    AgentConversationError,
    AgentConversationManager,
)
from csm.modules.session_manager.manager import SessionManager
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def setup():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_proj = tempfile.mkdtemp()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
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

    yield store, convmgr, sessman

    await sessman.shutdown()
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def _wait_for_session_status(sessman, sid, statuses, timeout=2.0):
    for _ in range(int(timeout / 0.05)):
        row = await sessman.get_session(sid)
        if row.status in statuses:
            return row
        await asyncio.sleep(0.05)
    return await sessman.get_session(sid)


async def test_spawn_creates_session_and_conversation(setup):
    store, convmgr, sessman = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    res = await convmgr.spawn(a.id)
    assert res.reused is False
    assert res.conversation.session_id == res.session.id
    # Post-spawn initial state is IDLE (see manager.py step 4 rationale).
    assert res.session.status == SessionStatus.IDLE


async def test_spawn_unknown_agent_raises(setup):
    _, convmgr, _ = setup
    with pytest.raises(AgentConversationError, match="not found"):
        await convmgr.spawn("nonexistent")


async def test_spawn_reuses_existing_live_conversation(setup):
    store, convmgr, _ = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    first = await convmgr.spawn(a.id)
    second = await convmgr.spawn(a.id)
    assert second.reused is True
    assert second.conversation.id == first.conversation.id
    assert second.session.id == first.session.id


async def test_end_kills_session_and_marks_ended(setup):
    store, convmgr, sessman = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    res = await convmgr.spawn(a.id)
    ok = await convmgr.end(res.conversation.id)
    assert ok is True
    await _wait_for_session_status(
        sessman, res.session.id, {SessionStatus.EXITED, SessionStatus.CRASHED}
    )
    pair = await convmgr.get(res.conversation.id)
    assert pair is not None
    assert pair[0].ended_at is not None


async def test_end_after_session_died_reaps(setup):
    """If the session died externally, next spawn must not 'reuse' the dead row."""
    store, convmgr, sessman = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    first = await convmgr.spawn(a.id)
    # Kill the session out-of-band (simulates claude crash).
    await sessman.kill_session(first.session.id)
    await _wait_for_session_status(
        sessman, first.session.id, {SessionStatus.EXITED, SessionStatus.CRASHED}
    )
    second = await convmgr.spawn(a.id)
    assert second.reused is False
    assert second.conversation.id != first.conversation.id
    # The first conversation should have been auto-reaped.
    pair = await convmgr.get(first.conversation.id)
    assert pair[0].ended_at is not None


async def test_active_conversation_query(setup):
    store, convmgr, _ = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    assert await convmgr.active_conversation_for_agent(a.id) is None
    res = await convmgr.spawn(a.id)
    active = await convmgr.active_conversation_for_agent(a.id)
    assert active is not None
    assert active.id == res.conversation.id
    await convmgr.end(res.conversation.id)
    assert await convmgr.active_conversation_for_agent(a.id) is None


async def test_send_user_message_writes_to_pty(setup):
    store, convmgr, sessman = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    res = await convmgr.spawn(a.id)
    ok = await convmgr.send_user_message(res.conversation.id, "echo hello_agent")
    assert ok is True
    await asyncio.sleep(0.4)
    live = sessman._live[res.session.id]
    snap = live.ring.snapshot()
    assert b"hello_agent" in snap


async def test_send_user_message_sets_title_from_first(setup):
    store, convmgr, _ = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    res = await convmgr.spawn(a.id)
    await convmgr.send_user_message(res.conversation.id, "what does this repo do?")
    pair = await convmgr.get(res.conversation.id)
    assert pair is not None
    assert pair[0].title == "what does this repo do?"
    # Second send must not overwrite.
    await convmgr.send_user_message(res.conversation.id, "another question")
    pair = await convmgr.get(res.conversation.id)
    assert pair[0].title == "what does this repo do?"


async def test_send_message_to_ended_returns_false(setup):
    store, convmgr, _ = setup
    a = await store.create(
        AgentCreate(name="rev", display_name="rev", cwd="/tmp", prompt_cached="hi")
    )
    res = await convmgr.spawn(a.id)
    await convmgr.end(res.conversation.id)
    ok = await convmgr.send_user_message(res.conversation.id, "hello")
    assert ok is False
