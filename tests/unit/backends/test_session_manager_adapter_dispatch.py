"""SessionManager adapter dispatch integration tests (M3).

Verifies that when an `adapter_registry` is wired in, `create_session`
delegates argv + session-id lifecycle through the CLIAdapter methods,
not the legacy claude/codex if-else branches.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from csm.backends.base import (
    AdapterArgvResult,
    AdapterStatus,
    Capability,
)
from csm.backends.registry import AdapterRegistry
from csm.models import Base
from csm.modules.session_manager.manager import SessionManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class _RecordingAdapter:
    """Adapter that records every call for assertion."""

    def __init__(self, name: str, capabilities: frozenset[Capability] | None = None):
        self.name = name
        self.display_name = name
        self.icon = name[0].upper() if name else "?"
        self.color = "#888888"
        self.capabilities = capabilities or frozenset({Capability.PRE_SPAWN_SESSION_ID})
        self.pre_spawn_calls: list[str] = []
        self.build_argv_calls: list[dict] = []
        self.post_spawn_calls: list[tuple[str, str]] = []
        self.prepare_bind_calls: list[str] = []
        self._session_id_to_return = str(uuid.uuid4())

    def home_dir(self) -> Path:
        return Path("/tmp/x")

    def default_home_name(self) -> str:
        return f".{self.name}"

    def auth_file(self):
        return None

    def probe(self) -> AdapterStatus:
        return AdapterStatus(name=self.name, installed=True, authenticated=True)

    def pre_spawn_session_id(self, cwd: str) -> str | None:
        self.pre_spawn_calls.append(cwd)
        if Capability.PRE_SPAWN_SESSION_ID in self.capabilities:
            return self._session_id_to_return
        return None

    def post_spawn_bind(self, session_row_id: str, cwd: str):
        from csm.backends.base import PostSpawnBindResult
        self.post_spawn_calls.append((session_row_id, cwd))
        # M10.A2: adapters now return PostSpawnBindResult so SessionManager
        # can persist both the id and the artifact path.
        return PostSpawnBindResult(
            external_session_id="post-bound-id",
            artifact_path="/tmp/fake/rollout-abc.jsonl",
        )

    def prepare_post_spawn_bind(self, session_row_id: str) -> None:
        self.prepare_bind_calls.append(session_row_id)

    def build_argv(self, base_argv, cwd, **kwargs):
        self.build_argv_calls.append({
            "base_argv": base_argv,
            "cwd": cwd,
            **kwargs,
        })
        return AdapterArgvResult(
            argv=list(base_argv),  # pass through
            session_id=kwargs.get("session_id"),
            prompt_appended=False,
        )

    def artifact_root(self) -> Path:
        return Path("/tmp/x")

    def artifact_glob(self) -> str:
        return "/tmp/x/**/*.jsonl"

    def scan_events(self) -> list:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, snap) -> None:
        pass

    def take_newly_seen(self) -> set[str]:
        return set()

    def tail_states(self) -> list[dict[str, Any]]:
        return []

    def install_hooks(self, project_root, callback_url) -> None:
        pass

    def default_argv(self) -> str:
        return self.name

    def flags_schema(self):
        return []


@pytest_asyncio.fixture
async def sm_fixture(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def stubbed_pty(monkeypatch):
    """Stub PTY spawn so we don't actually launch a subprocess.

    Records the argv it was handed on `last_argv`, so tests can assert on what
    would have reached the CLI rather than only on what landed in the DB row.
    """
    fake = MagicMock()
    fake.pid = 12345
    fake.fileno.return_value = -1  # bogus, we never read
    fake.last_argv = None

    def _spawn(argv, cwd, env):
        fake.last_argv = list(argv)
        return fake

    monkeypatch.setattr(
        "csm.modules.session_manager.manager.claude_subprocess.spawn", _spawn,
    )
    yield fake


# ---------------------------------------------------------------------------
# Adapter dispatch is invoked
# ---------------------------------------------------------------------------


async def test_registry_wired_pre_spawn_and_build_argv_called(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """When registry is set, pre_spawn_session_id + build_argv fire."""
    adapter = _RecordingAdapter("claude")
    registry = AdapterRegistry([adapter])
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=registry,
        claude_argv=["bash"],
    )

    # Silence the reader loop so it doesn't panic on our fake PTY.
    monkeypatch.setattr(
        SessionManager, "_reader_loop",
        AsyncMock(),
    )

    row = await manager.create_session(cwd="/tmp", agent="claude")
    assert row is not None
    assert adapter.pre_spawn_calls == ["/tmp"]
    assert len(adapter.build_argv_calls) == 1
    call = adapter.build_argv_calls[0]
    assert call["cwd"] == "/tmp"
    assert call["session_id"] == adapter._session_id_to_return


async def test_external_session_id_persisted_from_adapter(
    sm_fixture, stubbed_pty, monkeypatch,
):
    adapter = _RecordingAdapter("claude")
    registry = AdapterRegistry([adapter])
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=registry,
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())
    row = await manager.create_session(cwd="/tmp", agent="claude")
    assert row.external_session_id == adapter._session_id_to_return


async def test_resume_from_is_never_dropped_for_non_hook_adapter(
    sm_fixture, stubbed_pty, monkeypatch,
):
    adapter = _RecordingAdapter(
        "codex",
        capabilities=frozenset({
            Capability.POST_SPAWN_BIND,
            Capability.RESUME_SESSION,
        }),
    )
    registry = AdapterRegistry([adapter])
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=registry,
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())
    await manager.create_session(
        cwd="/tmp",
        agent="codex",
        argv=["bash"],
        resume_from="existing-codex-thread",
    )
    assert adapter.build_argv_calls[0]["resume_from"] == "existing-codex-thread"
    assert "hooks_base_url" not in adapter.build_argv_calls[0]


async def test_codex_external_session_id_populated_via_post_spawn_bind(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """M10.A2 fix: POST_SPAWN_BIND adapter (codex) now populates both
    Session.external_session_id and Session.rollout_path via the
    background _post_spawn_bind task — previously it was a `pass` stub
    and every codex downstream lookup missed."""
    import asyncio as _asyncio
    codex_adapter = _RecordingAdapter(
        "codex",
        capabilities=frozenset({Capability.POST_SPAWN_BIND}),
    )
    registry = AdapterRegistry([codex_adapter])
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=registry,
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())
    row = await manager.create_session(cwd="/tmp", agent="codex", argv=["bash"])
    assert row.external_session_id is None   # not yet — bind is async
    assert row.agent == "codex"
    # Let the scheduled _post_spawn_bind task run.
    await _asyncio.sleep(0.1)
    # Re-fetch to observe the DB write.
    refreshed = await manager.get_session(row.id)
    assert refreshed is not None
    assert refreshed.external_session_id == "post-bound-id"
    assert refreshed.rollout_path == "/tmp/fake/rollout-abc.jsonl"
    assert codex_adapter.prepare_bind_calls == [row.id]


async def test_post_spawn_bind_scheduled_for_capable_adapter(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """Codex-style adapter with POST_SPAWN_BIND capability should have
    _post_spawn_bind scheduled as a background task."""
    import asyncio as _asyncio
    codex_adapter = _RecordingAdapter(
        "codex",
        capabilities=frozenset({Capability.POST_SPAWN_BIND}),
    )
    registry = AdapterRegistry([codex_adapter])
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=registry,
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())
    await manager.create_session(cwd="/some/cwd", agent="codex", argv=["bash"])
    # Give the scheduled task a tick to run.
    await _asyncio.sleep(0.05)
    assert codex_adapter.post_spawn_calls
    # First (and only) call should have the cwd we passed in.
    _sid, cwd = codex_adapter.post_spawn_calls[0]
    assert cwd == "/some/cwd"


async def test_unknown_agent_via_registry_raises_unknown_agent_error(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """create_session(agent='gemini') with only claude registered → error."""
    from csm.backends.errors import UnknownAgentError
    adapter = _RecordingAdapter("claude")
    registry = AdapterRegistry([adapter])
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=registry,
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())
    with pytest.raises(UnknownAgentError):
        await manager.create_session(cwd="/tmp", agent="gemini")


# ---------------------------------------------------------------------------
# Workspace pre-trust must run on the REGISTRY path.
#
# Production returns early from the registry dispatch, so a call placed only
# in the legacy per-agent branch below it is dead code. That is exactly how
# the first version shipped: unit tests passed, and a real session in a new
# folder still hung on codex's invisible trust modal. These tests exercise
# `create_session` itself so the wiring — not just the helper — is covered.
# ---------------------------------------------------------------------------


class _TrustRecordingAdapter(_RecordingAdapter):
    def __init__(self, name: str = "codex"):
        super().__init__(name)
        self.trusted: list[str] = []

    def ensure_workspace_trusted(self, cwd: str) -> bool:
        self.trusted.append(cwd)
        return True


async def test_create_session_pretrusts_the_workspace(
    sm_fixture, stubbed_pty, monkeypatch,
):
    adapter = _TrustRecordingAdapter()
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([adapter]),
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    await manager.create_session(cwd="/tmp/fresh", agent="codex")

    assert adapter.trusted == ["/tmp/fresh"]


async def test_opt_out_marker_suppresses_pretrust(
    sm_fixture, stubbed_pty, monkeypatch,
):
    from csm.modules.session_manager.spawners import NO_TRUST_WORKSPACE_FLAG

    adapter = _TrustRecordingAdapter()
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([adapter]),
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    await manager.create_session(
        cwd="/tmp/fresh", agent="codex",
        argv=["codex", NO_TRUST_WORKSPACE_FLAG],
    )

    assert adapter.trusted == []


async def test_adapter_without_the_hook_is_not_disturbed(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """Duck-typed on purpose — claude has no such method and must not care."""
    adapter = _RecordingAdapter("claude")
    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([adapter]),
        claude_argv=["bash"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    row = await manager.create_session(cwd="/tmp", agent="claude")

    assert row is not None


# ---------------------------------------------------------------------------
# The real ClaudeAdapter, not the recording stub — pass-through argv
# ---------------------------------------------------------------------------


async def test_substituted_argv_does_not_claim_a_claude_session_id(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """`CSM_CLAUDE_ARGV='bash -i'` must not stamp the row with a claude uuid.

    `ClaudeAdapter.build_argv` is strict pass-through for a non-`claude`
    argv[0]: no `--session-id` reaches the command line, so no transcript will
    ever be written under that uuid. `create_session` used to adopt
    `pre_spawn_session_id()` up front — which meant a dev/test session claimed
    an `external_session_id` that could never resolve, and (because the column
    carries a partial unique index on live rows) could collide with a real one.
    The id is now taken only from what the adapter reports it actually used.
    """
    from csm.backends.claude.adapter import ClaudeAdapter

    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([ClaudeAdapter()]),
        claude_argv=["bash", "-i"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    row = await manager.create_session(cwd="/tmp", agent="claude")

    assert row is not None
    assert row.external_session_id is None
    # And the substituted argv reached spawn untouched.
    assert stubbed_pty.last_argv == ["bash", "-i"]


async def test_real_claude_argv_still_gets_its_session_id(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """The other half of the contract: real `claude` argv DOES bind an id."""
    from csm.backends.claude.adapter import ClaudeAdapter

    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([ClaudeAdapter()]),
        claude_argv=["claude"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    row = await manager.create_session(cwd="/tmp", agent="claude")

    assert row is not None
    assert row.external_session_id
    # The id on the row is the one actually passed to the CLI.
    argv = stubbed_pty.last_argv
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == row.external_session_id


async def test_substituted_argv_with_resume_does_not_claim_the_resumed_id(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """Pass-through argv + `resume_from` must still bind nothing.

    `ClaudeAdapter.build_argv` returns early for a non-`claude` argv[0], so no
    `--resume` reaches the command line and nothing is actually resumed. An
    intermediate version seeded `external_sid = resume_from` before calling the
    adapter, so this row claimed the predecessor's id — and because
    `ux_session_external_sid_active` is a partial unique index over live rows,
    resuming a still-running session raised ClaudeSessionIdConflict → HTTP 409
    in exactly the dev/test configuration (`CSM_CLAUDE_ARGV='bash -i'`) that is
    supposed to be the safe one.
    """
    from csm.backends.claude.adapter import ClaudeAdapter

    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([ClaudeAdapter()]),
        claude_argv=["bash", "-i"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    first = await manager.create_session(cwd="/tmp", agent="claude")
    second = await manager.create_session(
        cwd="/tmp", agent="claude", resume_from="some-live-session-id",
    )

    assert first is not None
    assert second is not None, "resuming under substituted argv must not 409"
    assert second.external_session_id is None
    assert "--resume" not in stubbed_pty.last_argv


async def test_real_claude_resume_still_binds_the_resumed_id(
    sm_fixture, stubbed_pty, monkeypatch,
):
    """The production half: real argv DOES adopt `resume_from` and pass it on."""
    from csm.backends.claude.adapter import ClaudeAdapter

    manager = SessionManager(
        sessionmaker=sm_fixture,
        event_stream=MagicMock(),
        adapter_registry=AdapterRegistry([ClaudeAdapter()]),
        claude_argv=["claude"],
    )
    monkeypatch.setattr(SessionManager, "_reader_loop", AsyncMock())

    row = await manager.create_session(
        cwd="/tmp", agent="claude", resume_from="prior-session-id",
    )

    assert row is not None
    assert row.external_session_id == "prior-session-id"
    argv = stubbed_pty.last_argv
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "prior-session-id"
