"""Missions endpoint reachability under mobile mount."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_list_missions_not_shadowed(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/missions")
    async def _stub_list():
        return {"items": [{"id": "m1", "workflow_def_id": "w1", "status": "running"}]}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/missions")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["status"] == "running"


def test_launch_mission_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/missions/launch")
    async def _stub_launch(body: dict):
        return {"id": "m-new", "workflow_def_id": body["workflow_name"], "status": "pending"}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/missions/launch", json={"workflow_name": "wf1", "params": {}})
        assert r.status_code == 200
        assert r.json()["id"] == "m-new"


def test_cancel_mission_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/missions/{mid}/cancel")
    async def _stub_cancel(mid: str):
        return {"id": mid, "status": "cancelled"}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/missions/abc/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


def test_retry_mission_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/missions/{mid}/retry")
    async def _stub_retry(mid: str, stage: str, mode: str = "rerun"):
        return {"id": mid, "status": "pending", "current_stage": stage, "mode": mode}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/missions/abc/retry", params={"stage": "s1", "mode": "rerun"})
        assert r.status_code == 200
        assert r.json()["current_stage"] == "s1"


def test_concurrent_cancel_idempotent(bare_app_no_catchall, mobile_dist):
    """Two rapid-fire cancels — the shim tracks state, second returns 4xx.
    This mirrors the intended real-world semantics documented in QA report P1-1."""
    state = {"cancelled": False}

    @bare_app_no_catchall.post("/api/missions/{mid}/cancel")
    async def _stub_cancel(mid: str):
        from fastapi import HTTPException
        if state["cancelled"]:
            raise HTTPException(status_code=409, detail="already cancelled")
        state["cancelled"] = True
        return {"id": mid, "status": "cancelled"}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r1 = client.post("/api/missions/abc/cancel")
        r2 = client.post("/api/missions/abc/cancel")
        assert {r1.status_code, r2.status_code} == {200, 409}
