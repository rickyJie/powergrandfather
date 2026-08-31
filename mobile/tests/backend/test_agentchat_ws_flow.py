"""Route-level tests for the agent conversation WS lifecycle contract.

These are contract tests — they exercise the mount-time behavior + route
routing, not the deep JsonlFastTail loop (that is covered by the desktop
suite in `tests/`). Our concern here is only that the mobile mount does
not shadow / break the WS routes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_ws_routes_not_shadowed_by_mobile_mount(bare_app_no_catchall, mobile_dist):
    """After register(), /api/ws/agents/... and /api/sessions/{sid}/ws
    remain in app.router.routes (i.e. not shadowed) and the mobile mount
    routes are moved to the front. Actual WS handshake/protocol is
    exercised by the desktop `tests/` suite — this test only guards the
    mount patch's contract."""
    from fastapi import WebSocket

    @bare_app_no_catchall.websocket("/api/ws/agents/conversations/{cid}")
    async def _stub_a(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    @bare_app_no_catchall.websocket("/api/sessions/{sid}/ws")
    async def _stub_s(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    register(bare_app_no_catchall)

    paths = [getattr(r, "path", "") for r in bare_app_no_catchall.router.routes]
    assert "/api/ws/agents/conversations/{cid}" in paths
    assert "/api/sessions/{sid}/ws" in paths
    # Mobile-attached routes should come before the WS stubs (they were
    # inserted at position 0 by register()).
    mobile_idx = next(
        i for i, p in enumerate(paths) if p.startswith("/m")
    )
    ws_idx = paths.index("/api/ws/agents/conversations/{cid}")
    assert mobile_idx < ws_idx


def test_send_message_endpoint_shape(bare_app_no_catchall, mobile_dist):
    """Just a shape check that /api/agents/conversations/{cid}/messages
    isn't captured by the mobile mount."""
    @bare_app_no_catchall.post("/api/agents/conversations/{cid}/messages")
    async def _stub_send(cid: str, body: dict):
        return {"sent": cid, "text": body.get("text", "")}

    register(bare_app_no_catchall)

    with TestClient(bare_app_no_catchall) as client:
        r = client.post(
            "/api/agents/conversations/abc/messages",
            json={"text": "hi"},
        )
        assert r.status_code == 200
        assert r.json() == {"sent": "abc", "text": "hi"}


def test_interrupt_endpoint_shape(bare_app_no_catchall, mobile_dist):
    """Ctrl-C endpoint reachability."""
    @bare_app_no_catchall.post("/api/agents/conversations/{cid}/interrupt")
    async def _stub_interrupt(cid: str):
        return {"interrupted": cid, "bytes_written": 1}

    register(bare_app_no_catchall)

    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/agents/conversations/abc/interrupt")
        assert r.status_code == 200
        assert r.json()["interrupted"] == "abc"
