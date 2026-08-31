"""Sessions REST route reachability under the mobile mount.

Same pattern as test_agentchat_ws_flow.py — stub the endpoint on the bare
app and verify the mobile mount doesn't preempt or 404 them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_list_sessions_not_captured_by_mobile(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/sessions")
    async def _stub_list():
        return {"count": 0, "items": [], "has_more": False, "offset": 0, "page_count": 0}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/sessions")
        assert r.status_code == 200
        assert r.json()["count"] == 0


def test_create_session_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/sessions")
    async def _stub_create(body: dict):
        return {"id": "new-sid", "cwd": body.get("cwd", ""), "status": "spawning"}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/sessions", json={"cwd": "/tmp"})
        assert r.status_code == 200
        assert r.json()["id"] == "new-sid"


def test_delete_session_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.delete("/api/sessions/{sid}")
    async def _stub_delete(sid: str):
        return {"stopped": sid}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.delete("/api/sessions/abc")
        assert r.status_code == 200
        assert r.json()["stopped"] == "abc"


def test_kill_endpoint_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/sessions/{sid}/kill")
    async def _stub_kill(sid: str):
        return {"killed": sid}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/sessions/abc/kill")
        assert r.status_code == 200
        assert r.json()["killed"] == "abc"
