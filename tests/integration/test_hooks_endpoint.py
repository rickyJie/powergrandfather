"""Finding 5 regression: hook endpoint must return 200 on ANY internal failure.

Claude Code treats non-2xx hook responses as failure and blocks the REPL
waiting for a retry that never comes. In the Finding-5 incident an HTTP
502 from `/api/hooks/{sid}` left a real claude process hung for 10h. The
contract now is: only the loopback-check may return non-200; every other
failure path swallows the exception and returns 200 {}.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from csm.models import Base
from csm.models.session import Session, SessionStatus, SessionType
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def app_client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    from csm import db as csm_db
    monkeypatch.setattr(csm_db, "_engine", engine)
    monkeypatch.setattr(csm_db, "_sessionmaker", sm)

    from csm.api.hooks import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    # D4 migration: hooks handler reads request.app.state.sessionmaker
    # instead of the module-level csm.db.get_sessionmaker() shim.
    app.state.sessionmaker = sm

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        yield client, sm

    await engine.dispose()
    os.unlink(db_path)


async def _seed_session(sm) -> str:
    async with sm() as s:
        sess = Session(
            id="sid-1",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.STARTING,
        )
        s.add(sess)
        await s.commit()
        return sess.id


async def test_success_returns_200(app_client):
    client, sm = app_client
    sid = await _seed_session(sm)
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionStart", "session_id": "claude-abc"},
    )
    assert r.status_code == 200
    assert r.json() == {}
    async with sm() as s:
        sess = await s.get(Session, sid)
        # SessionStart fires before any user prompt — status should be
        # IDLE ("waiting for input"), not RUNNING ("agent working").
        assert sess.status == SessionStatus.IDLE
        assert sess.external_session_id == "claude-abc"


async def test_stop_transitions_to_idle(app_client):
    """Regression for feedback 4965351a: Stop hook fires when claude
    finishes an assistant turn — the session should transition to IDLE,
    not stay at RUNNING. The v1 bug set RUNNING here, leaving sessions
    visibly stuck at "running" between turns until an unreliable
    Notification:idle_prompt event flipped them.
    """
    client, sm = app_client
    sid = await _seed_session(sm)
    # Drive the session to RUNNING first (simulate an active turn).
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "UserPromptSubmit"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, sid)
        assert sess.status == SessionStatus.RUNNING
    # Assistant finishes → Stop → should land IDLE.
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "Stop"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, sid)
        assert sess.status == SessionStatus.IDLE
        assert sess.current_tool is None


@pytest.mark.parametrize(
    ("event_name", "body"),
    [
        ("SessionStart", {"session_id": "claude-orphan"}),
        ("UserPromptSubmit", {}),
        ("PreToolUse", {"tool_name": "Read", "tool_input": {"path": "/tmp/x"}}),
        ("Notification", {"notification_type": "idle_prompt"}),
        ("Stop", {}),
    ],
)
async def test_orphaned_session_is_not_revived_by_late_hooks(
    app_client, event_name, body,
):
    client, sm = app_client
    async with sm() as db:
        db.add(Session(
            id="orphan-hook",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.ORPHANED,
            pid=os.getpid(),
        ))
        await db.commit()

    response = await client.post(
        "/api/hooks/orphan-hook",
        json={"hook_event_name": event_name, **body},
    )
    assert response.status_code == 200
    async with sm() as db:
        session = await db.get(Session, "orphan-hook")
        assert session.status == SessionStatus.ORPHANED


async def test_session_end_may_close_an_orphaned_session(app_client):
    client, sm = app_client
    async with sm() as db:
        db.add(Session(
            id="orphan-ended",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.ORPHANED,
            pid=os.getpid(),
        ))
        await db.commit()
    response = await client.post(
        "/api/hooks/orphan-ended",
        json={"hook_event_name": "SessionEnd"},
    )
    assert response.status_code == 200
    async with sm() as db:
        session = await db.get(Session, "orphan-ended")
        assert session.status == SessionStatus.EXITED
        assert session.ended_at is not None


# ---------------------------------------------------------------------------
# `/clear` in-place reset must NOT retire the session
# ---------------------------------------------------------------------------


async def test_session_end_clear_reason_does_not_retire(app_client):
    """Regression: `/clear` fires SessionEnd(reason=clear) while the PTY
    process stays alive and re-inits via SessionStart. CSM must NOT mark the
    row EXITED / set ended_at on a soft reason — otherwise the session
    vanishes from the active list on a mere context clear."""
    client, sm = app_client
    sid = await _seed_session(sm)
    # Drive to RUNNING to prove the soft SessionEnd leaves status untouched.
    await client.post(f"/api/hooks/{sid}", json={"hook_event_name": "UserPromptSubmit"})
    async with sm() as s:
        assert (await s.get(Session, sid)).status == SessionStatus.RUNNING

    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionEnd", "reason": "clear"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, sid)
        assert sess.status == SessionStatus.RUNNING, (
            f"soft SessionEnd(clear) must not retire; got {sess.status}"
        )
        assert sess.ended_at is None


async def test_clear_sequence_rebinds_to_new_sid(app_client, tmp_path, monkeypatch):
    """Full `/clear` sequence: SessionEnd(reason=clear) then SessionStart
    (source=clear) with a fresh sid. The session must stay alive AND adopt
    the new claude_session_id even though the pre-clear JSONL has content —
    the half-born-claude guard must be bypassed for an intentional reset."""
    from csm.config import settings as _settings

    client, sm = app_client
    monkeypatch.setattr(_settings, "claude_projects_dir", tmp_path)

    async with sm() as s:
        s.add(Session(
            id="sid-clear",
            cwd="/tmp/clear",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            external_session_id="pre-clear-sid",
        ))
        await s.commit()
    # Pre-clear transcript has real content → _old_sid_is_replaceable == False.
    _plant_jsonl(
        tmp_path, "/tmp/clear", "pre-clear-sid",
        [{"type": "user", "message": {"content": "before clear"}}],
    )

    # 1) SessionEnd(reason=clear) — must not retire.
    await client.post(
        "/api/hooks/sid-clear",
        json={"hook_event_name": "SessionEnd", "reason": "clear"},
    )
    async with sm() as s:
        assert (await s.get(Session, "sid-clear")).status != SessionStatus.EXITED

    # 2) SessionStart(source=clear) with the new sid — must adopt it.
    r = await client.post(
        "/api/hooks/sid-clear",
        json={
            "hook_event_name": "SessionStart",
            "session_id": "post-clear-sid",
            "source": "clear",
        },
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, "sid-clear")
        assert sess.status == SessionStatus.IDLE
        assert sess.ended_at is None
        assert sess.external_session_id == "post-clear-sid", (
            f"source=clear must adopt new sid despite content-ful old JSONL; "
            f"got {sess.external_session_id!r}"
        )


async def test_emit_suppresses_session_ended_for_clear():
    """`_emit` must not translate a soft SessionEnd(clear) into SESSION_ENDED —
    that event is what makes the frontend drop the card / NotificationBus
    auto-mark-read. A genuine reason still emits."""
    from csm.api.hooks import _emit
    from csm.core.events import EventType

    class _FakeES:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    es = _FakeES()
    await _emit(es, "sid-1", "csid", "/tmp", "SessionEnd", {"reason": "clear"})
    assert es.events == [], "soft SessionEnd(clear) must not emit SESSION_ENDED"

    await _emit(es, "sid-1", "csid", "/tmp", "SessionEnd", {"reason": "prompt_input_exit"})
    assert [e.type for e in es.events] == [EventType.SESSION_ENDED]


async def test_unknown_sid_returns_200(app_client):
    """Even for a session we don't know about (deleted / never existed), return 200."""
    client, _sm = app_client
    r = await client.post(
        "/api/hooks/does-not-exist",
        json={"hook_event_name": "SessionStart"},
    )
    assert r.status_code == 200
    assert r.json() == {}


async def test_dispatch_exception_returns_200(app_client, monkeypatch):
    """The critical Finding-5 case: if internal dispatch raises, still return 200.

    Otherwise claude sees a 5xx and hangs the session forever. We assert
    both status code and empty body — Claude Code cares about the code,
    but keeping the body shape stable avoids surprising future consumers.
    """
    client, sm = app_client
    sid = await _seed_session(sm)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated DB / dispatch failure")

    from csm.api import hooks as hooks_mod
    monkeypatch.setattr(hooks_mod, "_dispatch", boom)

    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "Stop"},
    )
    assert r.status_code == 200
    assert r.json() == {}


async def test_non_loopback_still_403(app_client):
    """Loopback boundary preserved — only 127.0.0.1 / ::1 may hit this route."""
    client, sm = app_client
    sid = await _seed_session(sm)
    # AsyncClient with ASGITransport reports client host as 127.0.0.1 by
    # default; override via the transport-level client to test the reject path.
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionStart"},
        headers={"X-Forwarded-For": "10.0.0.1"},  # ignored; kept for future proxy tests
    )
    # Baseline: ASGITransport client is 127.0.0.1 → should pass.
    assert r.status_code == 200


async def test_hooks_rejects_evil_host_header(app_client):
    """DNS rebinding defence: even with a loopback client, an attacker-supplied
    Host header (e.g. their own domain CNAMEd to 127.0.0.1) must be rejected.
    Otherwise a browser page on evil.example can issue authenticated requests
    against our loopback-only endpoints."""
    client, sm = app_client
    sid = await _seed_session(sm)
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionStart"},
        headers={"Host": "evil.example"},
    )
    assert r.status_code == 403
    assert "Host header" in r.json().get("detail", "")


async def test_hooks_accepts_localhost_host_header(app_client):
    """Baseline positive: `Host: localhost` (with or without port) is on the
    allowlist and reaches the normal 200 path."""
    client, sm = app_client
    sid = await _seed_session(sm)
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionStart", "session_id": "claude-xyz"},
        headers={"Host": "localhost:8000"},
    )
    assert r.status_code == 200
    assert r.json() == {}


async def test_hooks_rejects_ip_lookalike_host_header(app_client):
    """Host header allowlist is exact-match on the hostname portion; a
    lookalike IP (127.0.0.2) is not on the list and must be rejected."""
    client, sm = app_client
    sid = await _seed_session(sm)
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionStart"},
        headers={"Host": "127.0.0.2"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# ended_at split-brain regression
# ---------------------------------------------------------------------------


async def test_activity_hook_clears_stale_ended_at(app_client):
    """Regression: a non-SessionEnd hook must clear ``ended_at``.

    Split-brain scenario reproduced from a live incident: SessionEnd fired
    (setting ``ended_at`` + status EXITED) but the claude process kept
    running and emitted more hooks. Before the fix, those hooks flipped
    status back to RUNNING but never cleared ``ended_at``, leaving the row
    permanently inconsistent (``status=running`` + ``ended_at=<past>``).
    """
    from datetime import UTC, datetime

    client, sm = app_client
    sid = await _seed_session(sm)

    # Simulate the bug's precondition: ended_at is set, status=exited.
    async with sm() as s:
        sess = await s.get(Session, sid)
        sess.status = SessionStatus.EXITED
        sess.ended_at = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
        await s.commit()

    # A PreToolUse hook proves the process is still alive.
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, sid)
        assert sess.status == SessionStatus.RUNNING
        assert sess.ended_at is None, (
            f"non-SessionEnd hook must clear ended_at; got {sess.ended_at}"
        )


async def test_session_end_hook_still_sets_ended_at(app_client):
    """Guard the invariant from the other side: SessionEnd itself is the
    one hook that legitimately sets ``ended_at``, and the safety net must
    not clear it."""
    client, sm = app_client
    sid = await _seed_session(sm)
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionEnd"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, sid)
        assert sess.status == SessionStatus.EXITED
        assert sess.ended_at is not None


# ---------------------------------------------------------------------------
# Guarded SessionStart sid overwrite — openpi incident regression
# ---------------------------------------------------------------------------


def _plant_jsonl(projects_root, cwd: str, sid: str, lines: list[dict]) -> None:
    """Materialise a JSONL transcript at the location claude would use."""
    import json as _json
    from pathlib import Path

    encoded = cwd.rstrip("/").replace("/", "-")
    proj_dir = Path(projects_root) / encoded
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{sid}.jsonl").write_text(
        "".join(_json.dumps(o) + "\n" for o in lines),
        encoding="utf-8",
    )


async def test_session_start_preserves_healthy_sid(app_client, tmp_path, monkeypatch):
    """The openpi bug: a claude that crashes 500ms after spawn (only wrote
    permission-mode) fires SessionStart with a fresh sid and clobbers the
    long-lived healthy sid. Fix: refuse the overwrite when the old sid's
    JSONL has real content."""
    from csm.config import settings as _settings

    client, sm = app_client
    monkeypatch.setattr(_settings, "claude_projects_dir", tmp_path)

    # Seed a session bound to a healthy sid (has a real user message).
    async with sm() as s:
        s.add(Session(
            id="sid-1",
            cwd="/repo/openpi-learn",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            external_session_id="healthy-sid",
        ))
        await s.commit()
    _plant_jsonl(
        tmp_path,
        "/repo/openpi-learn",
        "healthy-sid",
        [{"type": "user", "message": {"content": "hi"}}],
    )
    # The half-born fresh JSONL claude would create on --resume before crashing.
    _plant_jsonl(
        tmp_path,
        "/repo/openpi-learn",
        "half-born-sid",
        [{"type": "permission-mode", "permissionMode": "bypassPermissions"}],
    )

    r = await client.post(
        "/api/hooks/sid-1",
        json={"hook_event_name": "SessionStart", "session_id": "half-born-sid"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, "sid-1")
        assert sess.external_session_id == "healthy-sid", (
            f"expected healthy sid preserved, got {sess.external_session_id!r}"
        )


async def test_session_start_overwrites_empty_sid(app_client, tmp_path, monkeypatch):
    """Symmetric case: if the old sid's JSONL is missing or empty, the
    overwrite MUST happen (that's the original correctness fix from v2)."""
    from csm.config import settings as _settings

    client, sm = app_client
    monkeypatch.setattr(_settings, "claude_projects_dir", tmp_path)

    async with sm() as s:
        s.add(Session(
            id="sid-2",
            cwd="/tmp/x",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            external_session_id="stale-sid",
        ))
        await s.commit()
    # stale-sid JSONL exists but has ONLY meta lines — a v2-style broken
    # transcript. Must be replaceable.
    _plant_jsonl(
        tmp_path, "/tmp/x", "stale-sid",
        [{"type": "permission-mode"}, {"type": "file-history-snapshot"}],
    )

    r = await client.post(
        "/api/hooks/sid-2",
        json={"hook_event_name": "SessionStart", "session_id": "fresh-sid"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, "sid-2")
        assert sess.external_session_id == "fresh-sid"


async def test_session_start_sets_sid_when_none(app_client):
    """First-bind case: current sid is None (fresh Session row) → any
    incoming sid should win. This is the v1 behaviour and must survive."""
    client, sm = app_client
    sid = await _seed_session(sm)  # created with external_session_id=None
    r = await client.post(
        f"/api/hooks/{sid}",
        json={"hook_event_name": "SessionStart", "session_id": "first-sid"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, sid)
        assert sess.external_session_id == "first-sid"


async def test_session_start_overwrites_when_old_jsonl_missing(
    app_client, tmp_path, monkeypatch,
):
    """Old sid's JSONL file entirely gone (claude pruned it) → the
    overwrite must proceed so future Resume clicks find the new sid."""
    from csm.config import settings as _settings

    client, sm = app_client
    monkeypatch.setattr(_settings, "claude_projects_dir", tmp_path)

    async with sm() as s:
        s.add(Session(
            id="sid-3",
            cwd="/tmp/y",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            external_session_id="pruned-sid",
        ))
        await s.commit()
    # Deliberately do NOT plant pruned-sid.jsonl.

    r = await client.post(
        "/api/hooks/sid-3",
        json={"hook_event_name": "SessionStart", "session_id": "new-sid"},
    )
    assert r.status_code == 200
    async with sm() as s:
        sess = await s.get(Session, "sid-3")
        assert sess.external_session_id == "new-sid"


# ---------------------------------------------------------------------------
# N2 — IntegrityError rollback branch (hooks.py:97-108)
# ---------------------------------------------------------------------------


async def test_integrity_error_rollback_preserves_original_sid(app_client, caplog):
    """SessionStart hook trying to claim a external_session_id already held by
    another live Session must hit the partial unique index
    (``ux_session_claude_sid_active``), the handler must catch IntegrityError,
    roll back, and leave the original row's ``external_session_id`` untouched.

    The fixture builds the schema via ``Base.metadata.create_all`` so the
    Alembic-installed partial unique index isn't there — we install it
    manually to mirror the production shape.
    """
    client, sm = app_client

    # Install the partial unique index the way alembic revision
    # n6h8c9d0edbf does in prod.
    from sqlalchemy import text
    async with sm() as s:
        await s.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_session_claude_sid_active "
            "ON session (external_session_id) "
            "WHERE external_session_id IS NOT NULL AND ended_at IS NULL"
        ))
        await s.commit()

    # Seed two live sessions with distinct external_session_ids.
    async with sm() as s:
        s.add(Session(
            id="sid-A",
            cwd="/tmp/a",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            external_session_id="sid-A",
        ))
        s.add(Session(
            id="sid-B",
            cwd="/tmp/b",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            external_session_id="sid-B",
        ))
        await s.commit()

    # Fire SessionStart on row A claiming B's external_session_id.
    caplog.set_level("WARNING", logger="csm.api.hooks")
    r = await client.post(
        "/api/hooks/sid-A",
        json={"hook_event_name": "SessionStart", "session_id": "sid-B"},
    )
    assert r.status_code == 200
    assert r.json() == {}

    # The IntegrityError-catch branch must have logged its distinctive
    # "unique-constraint" WARNING — otherwise the test would spuriously
    # pass on any code path that leaves sid-A unchanged.
    assert any(
        "unique-constraint on external_session_id" in rec.getMessage()
        for rec in caplog.records if rec.levelname == "WARNING"
    ), f"IntegrityError branch never fired; warnings: {[r.getMessage() for r in caplog.records]}"

    # A's external_session_id must still be its original value.
    async with sm() as s:
        a = await s.get(Session, "sid-A")
        b = await s.get(Session, "sid-B")
        assert a.external_session_id == "sid-A", (
            f"expected rollback to preserve sid-A, got {a.external_session_id!r}"
        )
        assert b.external_session_id == "sid-B", "sid-B must be untouched"


# ---------------------------------------------------------------------------
# N3 — unknown-sid retry loop exhausts after 6 probes (hooks.py:74-85)
# ---------------------------------------------------------------------------


async def test_unknown_sid_retry_exhausts_after_six(app_client, monkeypatch, caplog):
    """When the row genuinely doesn't exist, the retry loop must probe the DB
    exactly ``_UNKNOWN_SID_RETRY_ATTEMPTS`` (= 6) times, then emit a WARNING
    and still return 200 (the Finding-5 contract)."""
    client, _sm = app_client

    from csm.api import hooks as hooks_mod
    from csm.models.session import Session as _Session

    # Speed the retry loop up so the test doesn't stall for ~1s.
    monkeypatch.setattr(hooks_mod, "_UNKNOWN_SID_RETRY_DELAY_SEC", 0.0)

    call_count = 0
    # Wrap AsyncSession.get so every lookup for the Session model returns None
    # and we can count invocations. Non-Session lookups (should be none in
    # this path) pass through.
    from sqlalchemy.ext.asyncio import AsyncSession
    original_get = AsyncSession.get

    async def counting_get(self, entity, ident, *args, **kwargs):
        nonlocal call_count
        if entity is _Session:
            call_count += 1
            return None
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", counting_get)

    caplog.set_level("WARNING", logger="csm.api.hooks")
    r = await client.post(
        "/api/hooks/ghost-sid",
        json={"hook_event_name": "SessionStart"},
    )
    assert r.status_code == 200
    assert r.json() == {}
    # Exactly 6 probes — the retry loop bails on the first success, so the
    # None-forcing monkeypatch guarantees we exhaust every attempt.
    assert call_count == 6, f"expected 6 db probes, got {call_count}"
    # WARNING log must have fired with the "unknown session" phrase.
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("unknown session" in rec.getMessage() for rec in warnings), (
        f"expected an 'unknown session' WARNING; got: {[r.getMessage() for r in warnings]}"
    )
