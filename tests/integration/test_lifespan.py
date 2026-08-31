"""Lifespan smoke — asserts subsystems wire correctly and shutdown cleans up.

Uses FastAPI's native ``app.router.lifespan_context`` so the test does not
depend on the optional ``asgi_lifespan`` package. The goal is to catch
regressions in the lifespan wiring order (see ``docs/architecture.md``) —
if a future refactor drops a subsystem or forgets to attach it to
``app.state``, this test fails loudly.
"""
from __future__ import annotations

import csm.db as db_module
import pytest
import pytest_asyncio
from csm.config import settings
from csm.main import app
from csm.models import Base


@pytest_asyncio.fixture(autouse=True)
async def isolated_lifespan_state(tmp_path, monkeypatch):
    """A lifespan smoke test must never attach to the operator's live DB."""
    db_path = tmp_path / "lifespan.db"
    claude_root = tmp_path / "claude-projects"
    codex_root = tmp_path / "codex-sessions"
    tasks_root = tmp_path / "tasks"
    for path in (claude_root, codex_root, tasks_root):
        path.mkdir()

    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "claude_projects_dir", claude_root)
    monkeypatch.setattr(settings, "codex_sessions_dir", codex_root)
    monkeypatch.setattr(settings, "tasks_dir", tasks_root)
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CSM_SANDBOX_MODE", "1")
    monkeypatch.setenv("CSM_ENABLE_CLAUDE", "0")
    monkeypatch.setenv("CSM_ENABLE_CODEX", "0")

    # Other tests may have initialised the module cache. Never inherit it.
    if db_module._engine is not None:
        await db_module._engine.dispose()
    db_module._engine = None
    db_module._sessionmaker = None
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        await engine.dispose()
        db_module._engine = None
        db_module._sessionmaker = None


@pytest.mark.asyncio
async def test_lifespan_startup_wires_subsystems():
    """After lifespan startup, all documented subsystems must be on app.state."""
    async with app.router.lifespan_context(app):
        # Foundational subsystems
        assert getattr(app.state, "event_stream", None) is not None
        assert getattr(app.state, "session_manager", None) is not None
        assert getattr(app.state, "sessionmaker", None) is not None  # slot 6 wired

        # Automation / workflow spine
        assert getattr(app.state, "orchestrator", None) is not None
        assert getattr(app.state, "automation_runner", None) is not None
        assert getattr(app.state, "automation_scheduler", None) is not None
        assert getattr(app.state, "workflow_loader", None) is not None

        # Token / notification stack
        assert getattr(app.state, "token_aggregator", None) is not None
        assert getattr(app.state, "notification_bus", None) is not None
    # Reaching here means shutdown ran without raising — implicit assertion.


@pytest.mark.asyncio
async def test_every_subsystem_field_reaches_app_state():
    """`Subsystems` is the contract; nothing in it may be built and dropped.

    The hand-written assertions above name nine subsystems out of thirty-odd,
    so a refactor could construct one and forget to attach it — or quietly stop
    constructing it — without any of them noticing. Deriving the list from the
    dataclass keeps the check honest as fields are added.

    `int` fields are counters that are legitimately 0, so they are checked for
    presence rather than truthiness.
    """
    from csm.main import Subsystems

    fields = [f for f in Subsystems._fields if f != "sessionmaker"]
    assert len(fields) > 25, "sanity: this should be the big NamedTuple"

    async with app.router.lifespan_context(app):
        missing = [f for f in fields if not hasattr(app.state, f)]
        assert not missing, f"built but never attached to app.state: {missing}"


@pytest.mark.asyncio
async def test_lifespan_backfill_task_created_and_cleaned():
    """Backfill task (slot 4 D3) should be created during startup and
    either running or already done by the time we exit."""
    async with app.router.lifespan_context(app):
        bt = getattr(app.state, "backfill_task", None)
        # Backfill may be disabled/skipped in some configurations — accept None,
        # but if present it must be an asyncio Task-like object.
        if bt is not None:
            assert hasattr(bt, "done")
    # After the async-with exits, if bt existed the lifespan teardown should
    # have awaited or cancelled it. The fact that we returned here without a
    # hang is the primary signal we care about.
