"""Feedback endpoints reachability under mobile mount."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_feedback_list_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/feedback")
    async def _stub():
        return {"items": [{"id": "f1", "kind": "bug", "body": "test", "created_at": "2026-08-14"}]}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/feedback")
        assert r.status_code == 200


def test_feedback_submit_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/feedback")
    async def _stub(body: dict):
        return {
            "id": "f-new",
            "kind": body.get("kind", "?"),
            "body": body.get("body", ""),
            "created_at": "2026-08-14",
        }

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/feedback", json={"kind": "bug", "body": "Mobile crashed"})
        assert r.status_code == 200
        assert r.json()["kind"] == "bug"
