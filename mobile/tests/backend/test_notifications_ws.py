"""Notifications endpoint reachability under mobile mount."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_notifications_list_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/notifications")
    async def _stub_list():
        return {"count": 0, "items": []}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/notifications")
        assert r.status_code == 200
        assert r.json()["count"] == 0


def test_unread_summary_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/notifications/unread-summary")
    async def _stub_summary():
        return {"total": 3, "by_session": {"s1": 2, "s2": 1}}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/notifications/unread-summary")
        assert r.status_code == 200
        assert r.json()["total"] == 3


def test_mark_read_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/notifications/{nid}/read")
    async def _stub_read(nid: str):
        return {"id": nid, "read": True}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/notifications/n1/read")
        assert r.status_code == 200
        assert r.json()["read"] is True


def test_dismiss_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/notifications/{nid}/dismiss")
    async def _stub_dismiss(nid: str):
        return {"id": nid, "dismissed": True}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/notifications/n1/dismiss")
        assert r.status_code == 200


def test_ws_notifications_route_not_shadowed(bare_app_no_catchall, mobile_dist):
    from fastapi import WebSocket

    @bare_app_no_catchall.websocket("/api/notifications/ws")
    async def _stub_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    register(bare_app_no_catchall)
    paths = [getattr(r, "path", "") for r in bare_app_no_catchall.router.routes]
    assert "/api/notifications/ws" in paths
    mobile_idx = next(i for i, p in enumerate(paths) if p.startswith("/m"))
    ws_idx = paths.index("/api/notifications/ws")
    assert mobile_idx < ws_idx
