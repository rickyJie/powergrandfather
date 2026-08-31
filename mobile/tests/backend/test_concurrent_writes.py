"""Concurrent-write scenarios (mobile + desktop hitting the same
endpoint at once). We simulate the shape with sequential requests
against a stubbed handler that tracks state, because a real thread
pool test would need a full app boot.

These tests document the intended semantics rather than exercising real
concurrency — that lives in main repo `tests/` integration suite.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_double_cancel_mission_idempotent(bare_app_no_catchall, mobile_dist):
    state = {"cancelled": False}

    @bare_app_no_catchall.post("/api/missions/{mid}/cancel")
    async def _cancel(mid: str):
        if state["cancelled"]:
            raise HTTPException(status_code=409, detail="already cancelled")
        state["cancelled"] = True
        return {"id": mid, "status": "cancelled"}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r1 = client.post("/api/missions/m1/cancel")
        r2 = client.post("/api/missions/m1/cancel")
        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409]


def test_double_interrupt_returns_ok(bare_app_no_catchall, mobile_dist):
    """Ctrl-C is safe to send twice (PTY tty driver dedups multiple 0x03)."""
    hits = {"count": 0}

    @bare_app_no_catchall.post("/api/agents/conversations/{cid}/interrupt")
    async def _interrupt(cid: str):
        hits["count"] += 1
        return {"interrupted": cid, "bytes_written": 1}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r1 = client.post("/api/agents/conversations/c1/interrupt")
        r2 = client.post("/api/agents/conversations/c1/interrupt")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert hits["count"] == 2


def test_double_send_message_serialized(bare_app_no_catchall, mobile_dist):
    """Two messages arriving 'concurrently' should both land; server-side
    write_lock in real code prevents byte interleaving. Here we just assert
    both requests succeed and the payloads are preserved in order."""
    log: list[str] = []

    @bare_app_no_catchall.post("/api/agents/conversations/{cid}/messages")
    async def _send(cid: str, body: dict):
        log.append(body["text"])
        return {"sent": cid}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r1 = client.post("/api/agents/conversations/c1/messages", json={"text": "hi"})
        r2 = client.post("/api/agents/conversations/c1/messages", json={"text": "there"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert log == ["hi", "there"]
