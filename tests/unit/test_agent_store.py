"""Unit tests for AgentStore CRUD + AgentDefinition / AgentConversation models."""
from __future__ import annotations

import os
import tempfile

import pytest
from csm.models import (
    AgentConversation,
    AgentDefinition,
    Base,
    Session,
)
from csm.models.session import SessionStatus, SessionType
from csm.modules.agent.agent_store import (
    AgentCreate,
    AgentPatch,
    AgentStore,
    AgentStoreError,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()
    os.unlink(path)


@pytest.fixture
async def store(db):
    return AgentStore(sessionmaker=db)


async def test_agent_definition_roundtrip(db):
    async with db() as s:
        a = AgentDefinition(
            name="code_reviewer",
            display_name="代码评审员",
            cwd="/tmp/p",
            prompt_cached="you are a reviewer",
            icon="🔍",
            description="review diffs",
            prompt_source="/tmp/r.md",
        )
        s.add(a)
        await s.commit()
        got = await s.get(AgentDefinition, a.id)
        assert got is not None
        assert got.name == "code_reviewer"
        assert got.icon == "🔍"
        assert got.prompt_cached == "you are a reviewer"


async def test_agent_conversation_roundtrip(db):
    async with db() as s:
        sess = Session(cwd="/tmp", type=SessionType.CHAT_AGENT, status=SessionStatus.RUNNING)
        s.add(sess)
        await s.flush()
        a = AgentDefinition(
            name="rev", display_name="rev", cwd="/tmp", prompt_cached="p"
        )
        s.add(a)
        await s.flush()
        c = AgentConversation(agent_def_id=a.id, session_id=sess.id, title="hi")
        s.add(c)
        await s.commit()
        got = await s.get(AgentConversation, c.id)
        assert got is not None
        assert got.agent_def_id == a.id
        assert got.session_id == sess.id


async def test_create_basic(store):
    row = await store.create(
        AgentCreate(
            name="rev1",
            display_name="rev1",
            cwd="/tmp",
            prompt_cached="hello",
        )
    )
    assert row.id
    assert row.name == "rev1"
    assert row.prompt_source is None


async def test_create_rejects_bad_name(store):
    with pytest.raises(AgentStoreError, match="name must match"):
        await store.create(
            AgentCreate(name="1bad", display_name="x", cwd="/tmp", prompt_cached="p")
        )


async def test_create_rejects_duplicate_name(store):
    await store.create(
        AgentCreate(name="dup", display_name="x", cwd="/tmp", prompt_cached="p")
    )
    with pytest.raises(AgentStoreError, match="already exists"):
        await store.create(
            AgentCreate(name="dup", display_name="y", cwd="/tmp", prompt_cached="q")
        )


async def test_create_rejects_missing_fields(store):
    with pytest.raises(AgentStoreError):
        await store.create(
            AgentCreate(name="a", display_name="", cwd="/tmp", prompt_cached="p")
        )
    with pytest.raises(AgentStoreError):
        await store.create(
            AgentCreate(name="a", display_name="x", cwd="", prompt_cached="p")
        )
    with pytest.raises(AgentStoreError):
        await store.create(
            AgentCreate(name="a", display_name="x", cwd="/tmp", prompt_cached="")
        )


async def test_get_and_list(store):
    await store.create(
        AgentCreate(name="a1", display_name="a", cwd="/tmp", prompt_cached="p")
    )
    await store.create(
        AgentCreate(name="a2", display_name="b", cwd="/tmp", prompt_cached="q")
    )
    rows = await store.list()
    assert len(rows) == 2
    assert {r.name for r in rows} == {"a1", "a2"}

    by_name = await store.get_by_name("a1")
    assert by_name is not None
    by_id = await store.get(by_name.id)
    assert by_id is not None
    assert by_id.name == "a1"


async def test_patch_updates_fields(store):
    row = await store.create(
        AgentCreate(name="p1", display_name="orig", cwd="/tmp", prompt_cached="p")
    )
    patched = await store.patch(
        row.id,
        AgentPatch(display_name="updated", icon="🎯", prompt_cached="new"),
    )
    assert patched is not None
    assert patched.display_name == "updated"
    assert patched.icon == "🎯"
    assert patched.prompt_cached == "new"


async def test_patch_rejects_empty_required(store):
    row = await store.create(
        AgentCreate(name="p2", display_name="x", cwd="/tmp", prompt_cached="p")
    )
    with pytest.raises(AgentStoreError):
        await store.patch(row.id, AgentPatch(display_name=""))
    with pytest.raises(AgentStoreError):
        await store.patch(row.id, AgentPatch(cwd=""))
    with pytest.raises(AgentStoreError):
        await store.patch(row.id, AgentPatch(prompt_cached=""))


async def test_patch_missing_returns_none(store):
    out = await store.patch("nonexistent", AgentPatch(display_name="x"))
    assert out is None


async def test_delete_basic(store):
    row = await store.create(
        AgentCreate(name="d1", display_name="x", cwd="/tmp", prompt_cached="p")
    )
    ok = await store.delete(row.id)
    assert ok is True
    assert await store.get(row.id) is None


async def test_delete_blocked_by_active_conversation(store, db):
    row = await store.create(
        AgentCreate(name="d2", display_name="x", cwd="/tmp", prompt_cached="p")
    )
    async with db() as s:
        sess = Session(cwd="/tmp", type=SessionType.CHAT_AGENT, status=SessionStatus.RUNNING)
        s.add(sess)
        await s.flush()
        c = AgentConversation(agent_def_id=row.id, session_id=sess.id)
        s.add(c)
        await s.commit()
    with pytest.raises(AgentStoreError, match="active conversation"):
        await store.delete(row.id)


async def test_delete_ok_when_only_ended_conversations(store, db):
    from datetime import datetime
    row = await store.create(
        AgentCreate(name="d3", display_name="x", cwd="/tmp", prompt_cached="p")
    )
    async with db() as s:
        sess = Session(cwd="/tmp", type=SessionType.CHAT_AGENT, status=SessionStatus.EXITED)
        s.add(sess)
        await s.flush()
        c = AgentConversation(
            agent_def_id=row.id, session_id=sess.id, ended_at=datetime.utcnow()
        )
        s.add(c)
        await s.commit()
    ok = await store.delete(row.id)
    assert ok is True


async def test_delete_missing_returns_false(store):
    assert await store.delete("nonexistent") is False
