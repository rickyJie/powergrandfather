"""Regression: POST /api/sessions/{sid}/resume MUST refuse ORPHANED rows.

Root cause of the 2026-07-25 "sessions dying with exit_code=-9" cascade:
  - Frontend orphan card exposed a "Resume in fresh PTY" button
  - Backend `resume_session` had ORPHANED in its allow-list
  - When the orphan pid was still alive (common — that's what "orphaned"
    means: backend restart lost the PTY handle, process didn't die), it
    would `os.kill(pid, SIGKILL)` before spawning fresh
  - CSM has no way to tell if that pid is genuinely abandoned or if it's
    a claude the user is actively driving from another terminal
  - Result: user's live conversation gets nuked, row lands as CRASHED
    with exit_code=-9

Fix (multi-layer):
  1. Backend removes ORPHANED from allow-list → 409 with a message that
     tells the user to `kill <pid>` manually if they want to reclaim
  2. Frontend removes Resume + Kill buttons from orphan card, shows the
     pid and the manual-recovery instructions instead
  3. `activeSessionResumable` in Sessions.vue drops the orphan branch

This test pins the backend contract. If someone ever puts ORPHANED back
in the allow-list, this test screams before the footgun regrows.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from csm.api.sessions import router as sessions_router
from csm.backends import build_default_registry
from csm.core.event_stream import EventStream
from csm.models import Base
from csm.models import Session as SessRow
from csm.models.session import SessionStatus, SessionType
from csm.modules.session_manager.manager import SessionManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def api_client():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_proj = tempfile.mkdtemp()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    es = EventStream(projects_root=Path(tmp_proj), poll_interval_sec=10.0, watchdog_interval_sec=10.0)
    mgr = SessionManager(
        sessionmaker=sessionmaker,
        event_stream=es,
        adapter_registry=build_default_registry(),
        ring_buffer_bytes=4096,
        stop_grace_sec=1,
        claude_argv=["bash", "-i"],
    )

    app = FastAPI()
    app.state.sessionmaker = sessionmaker
    app.state.session_manager = mgr
    app.include_router(sessions_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sessionmaker, mgr

    await mgr.shutdown()
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def _insert_orphan(sessionmaker) -> str:
    """Create an ORPHANED row pointing at our own pid (guaranteed alive)."""
    async with sessionmaker() as db:
        row = SessRow(
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.ORPHANED,
            pid=os.getpid(),  # our own — definitely alive for the duration of the test
            external_session_id="fake-claude-sid-for-orphan-test",
        )
        db.add(row)
        await db.commit()
        return row.id


async def test_resume_of_orphaned_returns_409(api_client):
    client, sm, _mgr = api_client
    sid = await _insert_orphan(sm)

    r = await client.post(f"/api/sessions/{sid}/resume")

    assert r.status_code == 409, (
        f"Resume of an ORPHANED session must be refused to prevent the "
        f"SIGKILL-of-live-pid regression. Got {r.status_code}: {r.text}"
    )
    body = r.json()
    # The error message should point the user at the manual-kill workaround
    # so they know what to do next. If someone strips this hint they should
    # feel the test push back.
    assert "orphaned" in body["detail"].lower() or "kill" in body["detail"].lower(), \
        f"detail should tell user how to recover; got: {body['detail']!r}"


async def test_resume_of_orphaned_does_not_kill_the_pid(api_client):
    """Belt-and-suspenders: verify no SIGKILL was fired at the orphan pid.

    If the resume path ever regresses to blind-kill, this test catches it
    without needing to spin up a real subprocess — we use our own pid and
    check we're still alive after the request.
    """
    client, sm, _mgr = api_client
    sid = await _insert_orphan(sm)

    r = await client.post(f"/api/sessions/{sid}/resume")

    assert r.status_code == 409  # covered by test above; asserted again for locality
    # os.kill(pid, 0) raises if the pid is dead — if the backend regressed
    # and shot itself, this line would raise ProcessLookupError, but by
    # then the whole test process is likely already dying.
    os.kill(os.getpid(), 0)  # still alive → no SIGKILL landed


async def test_resume_of_exited_is_still_allowed(api_client):
    """Sanity: the fix must NOT break resume for genuinely-dead sessions.

    EXITED / CRASHED rows have a dead pid; the SIGKILL-of-orphan footgun
    can't apply. Resume of those should still be attempted (it will fail
    at spawn time here since the JSONL doesn't exist, but the point is
    the allow-list gate accepts the row — not a 409).
    """
    client, sm, _mgr = api_client
    async with sm() as db:
        row = SessRow(
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            pid=None,
            external_session_id="fake-exited-sid",
        )
        db.add(row)
        await db.commit()
        sid = row.id

    r = await client.post(f"/api/sessions/{sid}/resume")

    # Should NOT be 409 (allow-list rejection). May be 410 (missing JSONL
    # at preflight) or 500 (spawn failure) — either proves the gate passed.
    assert r.status_code != 409, (
        "EXITED rows must remain resumable. If this returns 409 the fix "
        "over-tightened the allow-list and broke history-Resume."
    )
