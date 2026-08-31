"""Unit tests for `POST /api/sessions` argv guard.

C2 (slot 2): argv[0] is passed straight to PtyProcess.spawn — with the
default LAN bind this is a trivial RCE vector. The guard rejects any
argv whose first element is not `"claude"` unless `CSM_ALLOW_ARBITRARY_ARGV=1`.

These tests exercise the guard in isolation via FastAPI's ASGI transport.
Because the guard raises 400 before any subsystem is touched, we only
need a lightweight `app.state.session_manager` stub — no real DB / PTY
required — for the negative cases. The `argv=["claude", ...]` accept case
routes through to a stub manager that returns a fake row.
"""
from __future__ import annotations

from datetime import datetime

import pytest_asyncio
from csm.api.sessions import router as sessions_router
from csm.models.session import SessionStatus, SessionType
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _FakeRow:
    """Duck-typed stand-in for csm.models.Session, sufficient for
    `_serialize()` in the handler."""

    def __init__(self, cwd: str) -> None:
        self.id = "sess-fake-0001"
        self.title = None
        self.type = SessionType.INTERACTIVE
        self.cwd = cwd
        self.status = SessionStatus.RUNNING
        self.pid = 4242
        self.started_at = datetime.utcnow()
        self.ended_at = None
        self.exit_code = None
        self.external_session_id = None
        self.agent = "claude"
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
        self.highlighted = False


class _FakeManager:
    """SessionManager stub that returns a fake row without spawning."""

    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []

    async def create_session(self, **kwargs):
        # Should only be reached if the argv guard passed.
        self.spawn_calls.append(kwargs)
        return _FakeRow(cwd=kwargs.get("cwd", "/tmp"))

    async def get_session(self, sid):  # unused by POST but part of API
        return None


@pytest_asyncio.fixture
async def client(tmp_path):
    app = FastAPI()
    app.state.session_manager = _FakeManager()
    # D4 migration: session handlers read request.app.state.sessionmaker
    # for post-create session_project_id updates. Not exercised in argv
    # tests (they hit the guard first), but set it so any handler that
    # falls through doesn't AttributeError.
    app.state.sessionmaker = None
    app.include_router(sessions_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Attach cwd so tests can pass an existing directory to satisfy
        # the cwd is_dir() check that runs after the argv guard.
        ac._cwd = str(tmp_path)  # type: ignore[attr-defined]
        yield ac


async def test_create_session_rejects_non_claude_argv(client: AsyncClient, monkeypatch):
    """POST with a shell-style argv should be refused with 400 mentioning argv[0]."""
    monkeypatch.delenv("CSM_ALLOW_ARBITRARY_ARGV", raising=False)
    cwd = client._cwd  # type: ignore[attr-defined]
    r = await client.post(
        "/api/sessions",
        json={
            "cwd": cwd,
            "argv": ["/bin/sh", "-c", "echo pwned"],
        },
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert "argv[0]" in body["detail"]
    assert "/bin/sh" in body["detail"]


async def test_create_session_accepts_claude_argv(client: AsyncClient, monkeypatch):
    """POST with argv[0]=='claude' should pass the guard."""
    monkeypatch.delenv("CSM_ALLOW_ARBITRARY_ARGV", raising=False)
    cwd = client._cwd  # type: ignore[attr-defined]
    r = await client.post(
        "/api/sessions",
        json={
            "cwd": cwd,
            "argv": ["claude", "--version"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cwd"] == cwd


async def test_create_session_allows_override_env(client: AsyncClient, monkeypatch):
    """CSM_ALLOW_ARBITRARY_ARGV=1 disables the guard (dev / test only)."""
    monkeypatch.setenv("CSM_ALLOW_ARBITRARY_ARGV", "1")
    cwd = client._cwd  # type: ignore[attr-defined]
    r = await client.post(
        "/api/sessions",
        json={
            "cwd": cwd,
            "argv": ["bash", "-i"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cwd"] == cwd


async def test_create_session_no_argv_bypasses_guard(client: AsyncClient, monkeypatch):
    """Omitting argv keeps the current default-argv codepath — no 400."""
    monkeypatch.delenv("CSM_ALLOW_ARBITRARY_ARGV", raising=False)
    cwd = client._cwd  # type: ignore[attr-defined]
    r = await client.post("/api/sessions", json={"cwd": cwd})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# jump-rail index (mobile)
# ---------------------------------------------------------------------------


def _idx(events):
    from csm.api.sessions import _user_message_index
    return _user_message_index(events)


def test_rail_index_spans_the_whole_transcript_not_the_shipped_tail():
    """The rail's whole reason for existing.

    History ships only the last 400 events, so on a busy session (~80 events
    per turn) indexing the shipped window covered the last handful of turns
    while the topmost dot still sat at the top as if it were the start of the
    conversation. The index is built over ALL events.
    """
    events = []
    for turn in range(6):
        events.append({"type": "user_message", "ts": "t", "text": f"question {turn}"})
        events += [{"type": "tool_use_start", "ts": "t"} for _ in range(100)]

    nodes = _idx(events)
    assert [n["text"] for n in nodes] == [f"question {t}" for t in range(6)]
    # `i` addresses the same array the history frame's `offset` does, which is
    # how the client tells loaded from not-yet-loaded.
    assert [n["i"] for n in nodes] == [0, 101, 202, 303, 404, 505]


def test_rail_index_drops_machine_filed_user_records():
    # A skill preamble / compaction recap / `claude -p` prompt is filed under
    # role "user" but is not something the human said, so no surface wants it.
    events = [
        {"type": "user_message", "ts": "t", "text": "mine"},
        {"type": "user_message", "ts": "t", "text": "Base directory…", "injected": True},
        {"type": "assistant_text", "ts": "t", "text": "reply"},
        {"type": "user_message", "ts": "t", "text": "also mine"},
    ]
    assert [n["text"] for n in _idx(events)] == ["mine", "also mine"]


def test_rail_index_keeps_slash_commands_for_the_client_to_filter():
    # That rule lives client-side, where it is already tested; splitting it
    # across both would let the two drift.
    events = [{"type": "user_message", "ts": "t", "text": "/compact"}]
    assert [n["text"] for n in _idx(events)] == ["/compact"]


def test_rail_index_truncates_snippets_and_caps_its_length():
    from csm.api.sessions import _NODE_INDEX_MAX, _NODE_TEXT_CHARS

    events = [
        {"type": "user_message", "ts": "t", "text": "x" * 5000}
        for _ in range(_NODE_INDEX_MAX + 25)
    ]
    nodes = _idx(events)
    # Newest kept: an older-than-2000-turns dot would be unclickable anyway.
    assert len(nodes) == _NODE_INDEX_MAX
    assert nodes[-1]["i"] == len(events) - 1
    assert all(len(n["text"]) <= _NODE_TEXT_CHARS for n in nodes)


def test_rail_index_of_an_empty_transcript_is_empty():
    assert _idx([]) == []
