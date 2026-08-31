"""Live-handshake test for the mobile chat WS heartbeat (ping → pong).

The mobile client sends an app-level "ping" every 25s and force-closes the
socket if no inbound frame arrives within 45s — this detects a silently-dead
socket over a stalled SSH tunnel (readyState stuck OPEN, no FIN/RST). For that
watchdog NOT to thrash on an idle session, the backend MUST answer "ping" with
{"type":"pong"}. This exercises the real `session_message_stream` handler over
a genuine TestClient WS handshake.

The agent-conversation WS (`/api/ws/agents/conversations/{cid}`) carries the
line-for-line identical pong snippet; testing this endpoint covers the contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import csm.api.sessions as sessions_mod
from csm.api.sessions import router as sessions_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeMgr:
    def __init__(self, row):
        self._row = row

    async def get_session(self, sid):  # noqa: ANN001 - test double
        return self._row


def _build_app(row) -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.session_manager = _FakeMgr(row)
    return app


def _claude_row(tmp_path, ext_id: str):
    return SimpleNamespace(
        status=SimpleNamespace(value="running"),
        agent="claude",
        external_session_id=ext_id,
        cwd=str(tmp_path),
    )


def _drain_for_pong(ws, budget: int = 6) -> bool:
    """Read up to `budget` frames; return True once a pong arrives.

    A history-replay frame may precede the pong, so we skip non-pong frames.
    """
    for _ in range(budget):
        frame = ws.receive_json()
        if frame.get("type") == "pong":
            return True
    return False


def test_session_ws_replies_pong_to_ping(tmp_path, monkeypatch):
    jsonl = tmp_path / "conv.jsonl"
    jsonl.write_text("")  # empty history is fine
    monkeypatch.setattr(
        sessions_mod, "conversation_jsonl_path", lambda *a, **k: jsonl
    )

    app = _build_app(_claude_row(tmp_path, "11111111-2222-3333-4444-555555555555"))
    with TestClient(app) as client:
        with client.websocket_connect("/api/sessions/s1/messages") as ws:
            first = ws.receive_json()
            assert first["type"] == "session_status"
            ws.send_text("ping")
            assert _drain_for_pong(ws), "backend did not answer ping with pong"


def test_session_ws_ignores_non_ping_then_pongs(tmp_path, monkeypatch):
    jsonl = tmp_path / "conv.jsonl"
    jsonl.write_text("")
    monkeypatch.setattr(
        sessions_mod, "conversation_jsonl_path", lambda *a, **k: jsonl
    )

    app = _build_app(_claude_row(tmp_path, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    with TestClient(app) as client:
        with client.websocket_connect("/api/sessions/s1/messages") as ws:
            assert ws.receive_json()["type"] == "session_status"
            # A non-ping line is silently ignored (no pong, no crash) …
            ws.send_text("not-a-ping")
            # … and a subsequent ping is still answered.
            ws.send_text("ping")
            assert _drain_for_pong(ws), "backend did not answer ping with pong"
