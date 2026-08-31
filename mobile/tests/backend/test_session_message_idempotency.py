"""Idempotent session send: a retried POST /message (same client_msg_id) must
NOT double-write to the PTY.

This is the backend half of the tunnel-safe-retry fix — a send whose HTTP
response is lost on a flaky SSH tunnel gets retried by the mobile client with
the same client_msg_id; the backend dedups so claude isn't double-typed.
"""

from __future__ import annotations

from types import SimpleNamespace

from csm.api.sessions import router as sessions_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeMgr:
    """Stands in for SessionManager's PTY-write surface.

    `writes` counts SENDS, not chunks: prose framing is per-CLI and a single
    logical message can legitimately reach the PTY as several writes (claude
    needs a bracketed-paste body and its submit CR in separate writes). What
    this suite is asserting is "did the retry type into claude twice", so the
    counter has to sit at the send boundary, not the chunk boundary.
    """

    def __init__(self):
        self.writes = 0

    async def get_session(self, sid):  # noqa: ANN001 - test double
        return SimpleNamespace(
            status=SimpleNamespace(value="running"),
            agent="claude",
            external_session_id="x",
            cwd="/tmp",
        )

    def frame_prose_sequence(self, agent, text):  # noqa: ANN001 - test double
        return [text.encode("utf-8", errors="replace") + b"\r\n"]

    async def write_input_sequence(self, sid, chunks):  # noqa: ANN001
        self.writes += 1
        total = sum(len(c) for c in chunks)
        return total, total


def _app(mgr) -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.session_manager = mgr
    return app


def test_duplicate_client_msg_id_writes_once():
    mgr = _FakeMgr()
    with TestClient(_app(mgr)) as c:
        r1 = c.post(
            "/api/sessions/s1/message",
            json={"text": "hi", "client_msg_id": "dup-abc"},
        )
        r2 = c.post(
            "/api/sessions/s1/message",
            json={"text": "hi", "client_msg_id": "dup-abc"},
        )
    assert r1.status_code == 200 and r1.json() == {"sent": "s1"}
    assert r2.status_code == 200 and r2.json().get("deduped") is True
    assert mgr.writes == 1  # the retry was deduped — PTY written exactly once


def test_distinct_ids_write_twice():
    mgr = _FakeMgr()
    with TestClient(_app(mgr)) as c:
        c.post("/api/sessions/s1/message", json={"text": "a", "client_msg_id": "id-1"})
        c.post("/api/sessions/s1/message", json={"text": "b", "client_msg_id": "id-2"})
    assert mgr.writes == 2


def test_no_key_is_not_deduped():
    mgr = _FakeMgr()
    with TestClient(_app(mgr)) as c:
        c.post("/api/sessions/s1/message", json={"text": "a"})
        c.post("/api/sessions/s1/message", json={"text": "a"})
    assert mgr.writes == 2  # no idempotency key → at-least-once (unchanged)
