"""Budget scope filters — one test per BudgetScopeType.

Written because every non-GLOBAL budget was dead and nobody noticed. The
`_scope_filter` lookup table is a dict LITERAL, so all six columns are
resolved before the dict is indexed: one stale attribute took out every
scope, not just its own. `RawTokenEvent.claude_session_id` had been renamed
to `external_session_id` during the agent-abstraction refactor, and
`BudgetEvaluator._loop` catches the resulting AttributeError and logs it, so
the feature failed silently on every 60s tick from the rename until now.

The lesson these tests encode: exercise EVERY scope, and assert on the
figure that comes back rather than on "it didn't raise".
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest
from csm.models import Base, Budget, RawTokenEvent
from csm.models.budget import BudgetPeriod, BudgetScopeType
from csm.modules.token.budget import compute_status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 8, 30, 12, 0, 0)


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


def _event(**kw) -> RawTokenEvent:
    """One raw usage row. 100 input tokens each, so totals are countable."""
    base = dict(
        ts=NOW,
        input_tokens=100,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.01,
    )
    base.update(kw)
    return RawTokenEvent(**base)


@pytest.fixture
async def seeded(db):
    """Two rows that differ on every scoped column, so a filter that silently
    matches everything fails as loudly as one that matches nothing."""
    async with db() as s:
        s.add(_event(
            project_path="/home/dev/webapp",
            task_name="nightly",
            source="interactive",
            model="claude-opus-4",
            external_session_id="sess-aaa",
        ))
        s.add(_event(
            project_path="/home/dev/api",
            task_name="weekly",
            source="auto",
            model="claude-haiku-4",
            external_session_id="sess-bbb",
        ))
        await s.commit()
    return db


@pytest.mark.parametrize(
    "scope_type,scope_value",
    [
        (BudgetScopeType.PROJECT, "/home/dev/webapp"),
        (BudgetScopeType.TASK, "nightly"),
        (BudgetScopeType.SOURCE, "interactive"),
        (BudgetScopeType.MODEL, "opus"),          # substring match by family
        (BudgetScopeType.SESSION, "sess-aaa"),
    ],
)
async def test_every_scope_selects_its_own_row(seeded, scope_type, scope_value):
    """Each scope must match exactly one of the two seeded rows.

    Parametrized rather than written as one test with five asserts: a single
    stale column in the lookup table breaks all of them, and five red tests
    name the blast radius where one would not.
    """
    b = Budget(
        name=f"{scope_type.value} budget",
        scope_type=scope_type,
        scope_value=scope_value,
        period=BudgetPeriod.DAILY,
        token_limit=1000,
        # Column defaults only apply on INSERT and these rows are never
        # persisted — set it so the object matches a real one.
        warn_pct=80.0,
    )
    async with seeded() as s:
        status = await compute_status(s, b, NOW)
    assert status["current_tokens"] == 100, f"{scope_type.value} matched the wrong row count"
    assert status["msg_count"] == 1


async def test_global_scope_counts_everything(seeded):
    """GLOBAL returns early with no filter — the control for the tests above."""
    b = Budget(
        name="global",
        scope_type=BudgetScopeType.GLOBAL,
        period=BudgetPeriod.DAILY,
        token_limit=1000,
        # Column defaults only apply on INSERT and these rows are never
        # persisted — set it so the object matches a real one.
        warn_pct=80.0,
    )
    async with seeded() as s:
        status = await compute_status(s, b, NOW)
    assert status["current_tokens"] == 200
    assert status["msg_count"] == 2


async def test_scope_value_that_matches_nothing_is_zero_not_everything(seeded):
    """A filter that silently degrades to no-op would report the global total
    here and quietly let a scoped budget run against the wrong number."""
    b = Budget(
        name="ghost",
        scope_type=BudgetScopeType.SESSION,
        scope_value="sess-does-not-exist",
        period=BudgetPeriod.DAILY,
        token_limit=1000,
        # Column defaults only apply on INSERT and these rows are never
        # persisted — set it so the object matches a real one.
        warn_pct=80.0,
    )
    async with seeded() as s:
        status = await compute_status(s, b, NOW)
    assert status["current_tokens"] == 0
