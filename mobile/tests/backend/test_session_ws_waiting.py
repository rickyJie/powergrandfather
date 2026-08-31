"""The mobile chat WS must WAIT for a not-yet-reconciled claude_session_id
instead of giving up with a terminal 4500.

A freshly-spawned claude session often has no `external_session_id` (the JSONL
uuid) for the first few hundred ms–seconds. The old handler closed 4500
immediately, so opening such a session on the phone showed an instant error and
the client (which treats 4500 as permanent) never retried. The handler now
sends a `session_status` frame and polls until the id appears, answering the
client's heartbeat meanwhile.

That status is NOT one value. A session with no transcript is two different
situations and the client renders them differently:

  live, no transcript yet  → `empty`   — an idle codex parked on its splash
                                         screen writes nothing until the first
                                         user turn and can sit there for hours;
                                         that is a usable session, so the
                                         client shows an empty chat with a
                                         composer and the handler waits with no
                                         deadline.
  ended, never wrote one   → `waiting` — can never produce a transcript now, so
                                         the wait is bounded and closes
                                         retryable.

Reporting the first case as an error was the bug: it made a perfectly usable
session indistinguishable from an unsupported one.
"""

from __future__ import annotations

from types import SimpleNamespace

import csm.api.sessions as sessions_mod
from csm.api.sessions import router as sessions_router
from csm.models.session import SessionStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _SequenceMgr:
    """`get_session` returns each supplied row in turn, repeating the last —
    lets a test model reconciliation (None id → real id) across poll calls."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0

    async def get_session(self, sid):  # noqa: ANN001 - test double
        row = self._rows[min(self._i, len(self._rows) - 1)]
        self._i += 1
        return row


def _build_app(rows) -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.session_manager = _SequenceMgr(rows)
    return app


def _claude_row(tmp_path, ext_id, status=SessionStatus.RUNNING):
    return SimpleNamespace(
        status=status,
        agent="claude",
        external_session_id=ext_id,
        cwd=str(tmp_path),
    )


def test_ws_waits_then_proceeds_when_id_appears(tmp_path, monkeypatch):
    jsonl = tmp_path / "conv.jsonl"
    jsonl.write_text("")
    monkeypatch.setattr(
        sessions_mod, "conversation_jsonl_path", lambda *a, **k: jsonl
    )

    real_id = "12121212-3434-5656-7878-909090909090"
    # Initial fetch → no id (waiting). A ping-driven poll → id present.
    rows = [_claude_row(tmp_path, None), _claude_row(tmp_path, real_id)]
    app = _build_app(rows)

    with TestClient(app) as client:
        with client.websocket_connect("/api/sessions/s1/messages") as ws:
            first = ws.receive_json()
            assert first["type"] == "session_status"
            # Live and transcript-less: an empty chat the user can type into,
            # not an error.
            assert first["status"] == "empty", first
            # Nudge the wait loop's receive so the poll runs without the 1s
            # timeout; the poll now sees the reconciled id.
            ws.send_text("ping")
            # The handler answers the ping AND then emits the real status once
            # the id appears. Drain until we see a non-waiting session_status.
            seen_real = False
            for _ in range(6):
                frame = ws.receive_json()
                if frame.get("type") == "session_status" and frame.get(
                    "status"
                ) != "waiting":
                    assert frame["external_session_id"] == real_id
                    seen_real = True
                    break
            assert seen_real, "handler never proceeded after the id appeared"


def test_ws_answers_ping_while_waiting(tmp_path, monkeypatch):
    jsonl = tmp_path / "conv.jsonl"
    jsonl.write_text("")
    monkeypatch.setattr(
        sessions_mod, "conversation_jsonl_path", lambda *a, **k: jsonl
    )

    # Stay transcript-less (id never appears within these polls) so the ping is
    # answered from INSIDE the wait loop. This is the load-bearing half of the
    # no-deadline branch: waiting forever is only safe if the socket stays
    # demonstrably alive, otherwise the client's watchdog tears it down.
    rows = [_claude_row(tmp_path, None)]
    app = _build_app(rows)

    with TestClient(app) as client:
        with client.websocket_connect("/api/sessions/s1/messages") as ws:
            assert ws.receive_json()["status"] == "empty"
            ws.send_text("ping")
            got_pong = False
            for _ in range(4):
                frame = ws.receive_json()
                if frame.get("type") == "pong":
                    got_pong = True
                    break
            assert got_pong, "backend did not answer ping while waiting"


def test_ws_reports_waiting_for_a_session_that_ended_without_a_transcript(
    tmp_path, monkeypatch
):
    """The other half of the split: `waiting`, still reachable, still bounded.

    An EXITED session with no transcript can never grow one, so this must NOT
    take the open-ended `empty` path — that would park the socket forever on a
    session that is provably never going to say anything.
    """
    jsonl = tmp_path / "conv.jsonl"
    jsonl.write_text("")
    monkeypatch.setattr(
        sessions_mod, "conversation_jsonl_path", lambda *a, **k: jsonl
    )

    rows = [_claude_row(tmp_path, None, status=SessionStatus.EXITED)]
    app = _build_app(rows)

    with TestClient(app) as client:
        with client.websocket_connect("/api/sessions/s1/messages") as ws:
            first = ws.receive_json()
            assert first["type"] == "session_status"
            assert first["status"] == "waiting", first
            assert "ended" in (first.get("detail") or "")
