"""Multi-adapter EventStream integration tests (M3).

Verifies the P0 fix from the backend review: `_tick_once` fans out
`scan_events()` calls across every enabled adapter concurrently, using
`asyncio.gather + return_exceptions` so a failing adapter can't block
the others.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest_asyncio
from csm.backends.base import Capability
from csm.backends.registry import AdapterRegistry
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Base, FileState, Session
from csm.models.session import SessionStatus, SessionType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubAdapter:
    """Adapter that yields a canned event list on every scan_events()."""

    def __init__(self, name: str, events_to_yield: list[Event] | None = None,
                 raises: BaseException | None = None):
        self.name = name
        self.display_name = name
        self.icon = name[0].upper() if name else "?"
        self.color = "#888888"
        self.capabilities = frozenset({Capability.INTERACTIVE_STREAM})
        self._events = events_to_yield or []
        self._raises = raises
        self.scan_count = 0
        self.snapshot_value: dict[str, Any] = {}
        self.restored: dict[str, Any] | None = None

    def default_argv(self) -> str:
        return self.name

    def flags_schema(self):
        return []

    def home_dir(self) -> Path:
        return Path("/tmp/x")

    def default_home_name(self) -> str:
        return "." + self.name

    def auth_file(self) -> Path | None:
        return None

    def probe(self):
        from csm.backends.base import AdapterStatus
        return AdapterStatus(name=self.name, installed=True, authenticated=True)

    def pre_spawn_session_id(self, cwd: str) -> str | None:
        return None

    def post_spawn_bind(self, session_row_id: str, cwd: str) -> str | None:
        return None

    def build_argv(self, *args, **kwargs):
        from csm.backends.base import AdapterArgvResult
        return AdapterArgvResult(argv=[])

    def artifact_root(self) -> Path:
        return Path("/tmp/x")

    def artifact_glob(self) -> str:
        return "/tmp/x/**/*.jsonl"

    def scan_events(self) -> list[Event]:
        self.scan_count += 1
        if self._raises is not None:
            raise self._raises
        return list(self._events)

    def snapshot(self) -> dict[str, Any]:
        return self.snapshot_value

    def restore(self, snap: dict[str, Any]) -> None:
        self.restored = snap

    def take_newly_seen(self) -> set[str]:
        return set()

    def tail_states(self) -> list[dict[str, Any]]:
        return []

    def install_hooks(self, project_root, callback_url):
        pass


def _make_ev(sid: str, agent: str = "claude") -> Event:
    return Event(
        type=EventType.MESSAGE_USER_SENT,
        ts=datetime.now(UTC),
        session_id=sid,
        project_path="/tmp/x",
        payload={"backend": agent},
    )


@pytest_asyncio.fixture
async def stream_with_two_adapters(tmp_path, monkeypatch):
    """Fresh EventStream + registry with two enabled StubAdapters."""
    monkeypatch.setenv("CSM_ENABLE_A", "1")
    monkeypatch.setenv("CSM_ENABLE_B", "1")
    a = _StubAdapter("a", [_make_ev("sid-a", "a")])
    b = _StubAdapter("b", [_make_ev("sid-b", "b")])
    registry = AdapterRegistry([a, b])
    es = EventStream(
        projects_root=tmp_path,
        poll_interval_sec=0.1,
        watchdog_interval_sec=60.0,
        adapter_registry=registry,
        seed_stale_history_as_idle=False,
    )
    yield es, registry, a, b


# ---------------------------------------------------------------------------
# Basic multi-adapter dispatch
# ---------------------------------------------------------------------------


async def test_tick_once_calls_every_enabled_adapter(stream_with_two_adapters):
    es, _reg, a, b = stream_with_two_adapters
    await es._tick_once()
    assert a.scan_count == 1
    assert b.scan_count == 1


async def test_tick_once_emits_events_from_all_adapters(stream_with_two_adapters):
    es, _reg, _a, _b = stream_with_two_adapters
    captured: list[Event] = []
    es.subscribe(None, lambda ev: _capture(captured, ev))
    await es._tick_once()
    sids = {ev.session_id for ev in captured}
    assert "sid-a" in sids
    assert "sid-b" in sids


async def _capture(bag, ev):
    bag.append(ev)


# ---------------------------------------------------------------------------
# P0 fix — one adapter failing must NOT block others
# ---------------------------------------------------------------------------


async def test_failing_adapter_does_not_prevent_other_adapters_from_emitting(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CSM_ENABLE_HEALTHY", "1")
    monkeypatch.setenv("CSM_ENABLE_BROKEN", "1")
    healthy = _StubAdapter("healthy", [_make_ev("sid-healthy")])
    broken = _StubAdapter("broken", raises=RuntimeError("simulated fault"))
    registry = AdapterRegistry([broken, healthy])  # broken first!
    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=registry,
        seed_stale_history_as_idle=False,
    )
    captured: list[Event] = []
    es.subscribe(None, lambda ev: _capture(captured, ev))
    # Must not raise.
    await es._tick_once()
    # Healthy adapter's events still land.
    assert any(ev.session_id == "sid-healthy" for ev in captured)
    # Both adapters were called (broken didn't short-circuit healthy).
    assert broken.scan_count == 1
    assert healthy.scan_count == 1


# ---------------------------------------------------------------------------
# Concurrency — adapters run in parallel, not serially
# ---------------------------------------------------------------------------


async def test_adapters_scan_concurrently(tmp_path, monkeypatch):
    """Verify wall-clock scan time ≈ max(adapter times), not sum().

    Each stub sleeps ~200ms in scan_events. Two adapters serially = 400ms;
    concurrent = ~200ms. Assert we're closer to concurrent.
    """
    import time as _t

    class _SlowAdapter(_StubAdapter):
        def scan_events(self):
            self.scan_count += 1
            _t.sleep(0.2)
            return []

    monkeypatch.setenv("CSM_ENABLE_S1", "1")
    monkeypatch.setenv("CSM_ENABLE_S2", "1")
    s1 = _SlowAdapter("s1")
    s2 = _SlowAdapter("s2")
    registry = AdapterRegistry([s1, s2])
    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=registry,
        seed_stale_history_as_idle=False,
    )
    t0 = asyncio.get_event_loop().time()
    await es._tick_once()
    elapsed = asyncio.get_event_loop().time() - t0
    # Serial would be 0.4s; concurrent is ~0.2s. Allow generous headroom.
    assert elapsed < 0.35, f"tick took {elapsed:.3f}s — suggests serial exec"


# ---------------------------------------------------------------------------
# Env flag off = adapter not enabled = not called
# ---------------------------------------------------------------------------


async def test_disabled_adapter_is_not_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("CSM_ENABLE_ON", "1")
    monkeypatch.setenv("CSM_ENABLE_OFF", "0")
    on = _StubAdapter("on")
    off = _StubAdapter("off")
    registry = AdapterRegistry([on, off])
    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=registry,
        seed_stale_history_as_idle=False,
    )
    await es._tick_once()
    assert on.scan_count == 1
    assert off.scan_count == 0


async def test_registry_tail_offsets_round_trip_per_adapter(tmp_path, monkeypatch):
    """Production registry mode must persist/restore each adapter's tailer."""
    monkeypatch.setenv("CSM_ENABLE_A", "1")
    monkeypatch.setenv("CSM_ENABLE_B", "1")
    a = _StubAdapter("a")
    b = _StubAdapter("b")
    a.snapshot_value = {
        "/tmp/a.jsonl": {
            "offset": 101, "mtime": 1.5, "session_id": "a-session",
        },
    }
    b.snapshot_value = {
        "/tmp/b.jsonl": {
            "offset": 202, "mtime": 2.5, "codex_session_id": "b-session",
        },
    }
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=AdapterRegistry([a, b]),
        sessionmaker=sm,
        flush_interval_sec=0,
        seed_stale_history_as_idle=False,
    )
    await es._flush_file_state()
    async with sm() as db:
        rows = (await db.execute(select(FileState))).scalars().all()
        assert {(r.agent, r.last_offset, r.session_id) for r in rows} == {
            ("a", 101, "a-session"),
            ("b", 202, "b-session"),
        }

    a.restored = None
    b.restored = None
    await es._restore_file_state()
    assert a.restored is not None
    assert a.restored["/tmp/a.jsonl"]["offset"] == 101
    assert b.restored is not None
    assert b.restored["/tmp/b.jsonl"]["codex_session_id"] == "b-session"
    await engine.dispose()


# ---------------------------------------------------------------------------
# Adapter event -> durable Session state projection
# ---------------------------------------------------------------------------


async def test_task_complete_binds_codex_row_and_is_idle_before_publish(
    tmp_path, monkeypatch,
):
    """Codex keeps its TUI process alive after a turn; task_complete is IDLE.

    The projection must commit before EventStream publishes the event so the
    frontend's event-triggered refresh cannot race and read stale RUNNING.
    """
    monkeypatch.setenv("CSM_ENABLE_CODEX", "1")
    rollout = str(tmp_path / "rollout-existing.jsonl")
    event = Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="codex-external-1",
        project_path="/tmp/project",
        payload={
            "backend": "codex",
            "rollout_path": rollout,
            "assistant_text": "finished",
        },
    )
    adapter = _StubAdapter("codex", [event])
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'projection.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        row = Session(
            cwd="/tmp/project",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            agent="codex",
            pid=123,
        )
        db.add(row)
        await db.commit()
        csm_sid = row.id

    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=AdapterRegistry([adapter]),
        sessionmaker=sm,
        seed_stale_history_as_idle=False,
    )
    observed_statuses: list[SessionStatus] = []
    observed_csm_ids: list[str | None] = []

    async def _observe_after_projection(_event: Event) -> None:
        async with sm() as db:
            projected = await db.get(Session, csm_sid)
            assert projected is not None
            observed_statuses.append(projected.status)
            observed_csm_ids.append(_event.payload.get("csm_session_id"))

    es.subscribe(None, _observe_after_projection)
    await es._tick_once()

    async with sm() as db:
        projected = await db.get(Session, csm_sid)
        assert projected is not None
        assert projected.status == SessionStatus.IDLE
        assert projected.external_session_id == "codex-external-1"
        assert projected.rollout_path == rollout
        assert projected.last_assistant_msg == "finished"
    assert observed_statuses == [SessionStatus.IDLE]
    assert observed_csm_ids == [csm_sid]
    await engine.dispose()


async def test_user_message_moves_bound_codex_row_back_to_running(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CSM_ENABLE_CODEX", "1")
    event = Event(
        type=EventType.MESSAGE_USER_SENT,
        ts=datetime.now(UTC),
        session_id="codex-external-2",
        project_path="/tmp/project",
        payload={"backend": "codex", "text": "next turn"},
    )
    adapter = _StubAdapter("codex", [event])
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'projection-running.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        row = Session(
            cwd="/tmp/project",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.IDLE,
            agent="codex",
            external_session_id="codex-external-2",
            pid=123,
        )
        db.add(row)
        await db.commit()
        csm_sid = row.id

    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=AdapterRegistry([adapter]),
        sessionmaker=sm,
        seed_stale_history_as_idle=False,
    )
    await es._tick_once()

    async with sm() as db:
        projected = await db.get(Session, csm_sid)
        assert projected is not None
        assert projected.status == SessionStatus.RUNNING
        assert projected.ended_at is None
    await engine.dispose()


async def test_tool_progress_updates_bound_codex_row_and_publishes_csm_id(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CSM_ENABLE_CODEX", "1")
    event = Event(
        type=EventType.SESSION_TOOL_PROGRESS,
        ts=datetime.now(UTC),
        session_id="codex-external-tool",
        project_path="/tmp/project",
        payload={"backend": "codex", "tool_name": "apply_patch"},
    )
    adapter = _StubAdapter("codex", [event])
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'projection-tool.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        row = Session(
            cwd="/tmp/project",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.IDLE,
            agent="codex",
            external_session_id="codex-external-tool",
            pid=123,
        )
        db.add(row)
        await db.commit()
        csm_sid = row.id

    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=AdapterRegistry([adapter]),
        sessionmaker=sm,
        seed_stale_history_as_idle=False,
    )
    await es._tick_once()

    async with sm() as db:
        projected = await db.get(Session, csm_sid)
        assert projected is not None
        assert projected.status == SessionStatus.RUNNING
        assert projected.current_tool == "apply_patch"
    assert event.payload["csm_session_id"] == csm_sid
    await engine.dispose()


async def test_tool_progress_hint_renders_like_the_claude_hook(
    tmp_path, monkeypatch,
):
    """`current_tool` must read `"<Tool>: <arg head>"` — the same shape the
    claude PreToolUse hook writes (api/hooks.py) — so a codex session card is
    indistinguishable from a claude one. The head is capped so a 4KB heredoc
    can't blow out the column."""
    monkeypatch.setenv("CSM_ENABLE_CODEX", "1")
    event = Event(
        type=EventType.SESSION_TOOL_PROGRESS,
        ts=datetime.now(UTC),
        session_id="codex-external-hint",
        project_path="/tmp/project",
        payload={
            "backend": "codex",
            "tool_name": "Bash",
            "tool_hint": "pytest -q " + "x" * 500,
        },
    )
    adapter = _StubAdapter("codex", [event])
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'projection-hint.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        row = Session(
            cwd="/tmp/project",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.IDLE,
            agent="codex",
            external_session_id="codex-external-hint",
            pid=123,
        )
        db.add(row)
        await db.commit()
        csm_sid = row.id

    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=AdapterRegistry([adapter]),
        sessionmaker=sm,
        seed_stale_history_as_idle=False,
    )
    await es._tick_once()

    async with sm() as db:
        projected = await db.get(Session, csm_sid)
        assert projected is not None
        assert projected.current_tool.startswith("Bash: pytest -q ")
        assert len(projected.current_tool) <= 200
    await engine.dispose()


async def test_tool_progress_without_hint_keeps_the_bare_tool_name(
    tmp_path, monkeypatch,
):
    """`tool_hint` is optional: adapters that can't produce one (and every
    pre-existing caller) must still get exactly the old behaviour."""
    monkeypatch.setenv("CSM_ENABLE_CODEX", "1")
    event = Event(
        type=EventType.SESSION_TOOL_PROGRESS,
        ts=datetime.now(UTC),
        session_id="codex-external-nohint",
        project_path="/tmp/project",
        payload={"backend": "codex", "tool_name": "web__run", "tool_hint": "   "},
    )
    adapter = _StubAdapter("codex", [event])
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'projection-nohint.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        row = Session(
            cwd="/tmp/project",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.IDLE,
            agent="codex",
            external_session_id="codex-external-nohint",
            pid=123,
        )
        db.add(row)
        await db.commit()
        csm_sid = row.id

    es = EventStream(
        projects_root=tmp_path,
        adapter_registry=AdapterRegistry([adapter]),
        sessionmaker=sm,
        seed_stale_history_as_idle=False,
    )
    await es._tick_once()

    async with sm() as db:
        projected = await db.get(Session, csm_sid)
        assert projected is not None
        assert projected.current_tool == "web__run"
    await engine.dispose()
