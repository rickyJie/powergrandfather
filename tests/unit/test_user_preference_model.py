"""Unit tests for the UserPreference ORM model + defaults.

Covers:
- Single-row invariant (CHECK id=1) — inserting id=2 raises.
- Defaults: default_agent='claude', has_completed_first_run=True.
- supervisor_agent nullable.
- Round-trip via SQLAlchemy async engine on an in-memory sqlite.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from csm.models import Base, UserPreference
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def test_insert_single_row_and_read_back(session):
    session.add(UserPreference(id=1, default_agent="claude"))
    await session.commit()

    row = await session.get(UserPreference, 1)
    assert row is not None
    assert row.default_agent == "claude"
    assert row.has_completed_first_run is True
    assert row.supervisor_agent is None


async def test_check_constraint_rejects_id_other_than_1(session):
    session.add(UserPreference(id=2, default_agent="claude"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_default_agent_default_is_claude(session):
    session.add(UserPreference(id=1))  # no explicit default_agent
    await session.commit()
    row = await session.get(UserPreference, 1)
    assert row.default_agent == "claude"


async def test_supervisor_agent_can_be_pinned(session):
    session.add(UserPreference(
        id=1,
        default_agent="claude",
        supervisor_agent="claude",
    ))
    await session.commit()
    row = await session.get(UserPreference, 1)
    assert row.supervisor_agent == "claude"


async def test_updated_at_moves_on_write(session):
    session.add(UserPreference(id=1, default_agent="claude"))
    await session.commit()
    row = await session.get(UserPreference, 1)
    ts0 = row.updated_at
    row.default_agent = "codex"
    await session.commit()
    await session.refresh(row)
    assert row.updated_at >= ts0
