"""P4 readonly endpoints reachability under mobile mount:
budgets, ports, worktime, preferences, lark-settings, files."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_budgets_list_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/budgets")
    async def _stub():
        return {"items": [{"id": "b1", "name": "cap", "scope": "day", "limit": 100}]}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/budgets")
        assert r.status_code == 200
        assert r.json()["items"][0]["id"] == "b1"


def test_budgets_status_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/budgets/status")
    async def _stub():
        return {"items": [{"id": "b1", "used": 42, "limit": 100, "pct": 42.0, "triggered": False}]}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/budgets/status")
        assert r.status_code == 200


def test_ports_list_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/ports")
    async def _stub():
        return {"items": [{"port": 8000, "status": "in_use", "pid": 999}]}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/ports")
        assert r.status_code == 200


def test_ports_scan_now_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.post("/api/ports/scan-now")
    async def _stub():
        return {"scan": "triggered"}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.post("/api/ports/scan-now")
        assert r.status_code == 200


def test_worktime_live_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/worktime/live")
    async def _stub():
        return {
            "today_agent_sec": 3600,
            "today_idle_sec": 1200,
            "today_disconnected_sec": 0,
            "all_agent_sec": 100000,
            "all_idle_sec": 50000,
        }

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/worktime/live")
        assert r.status_code == 200
        assert r.json()["today_agent_sec"] == 3600


def test_preferences_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/preferences")
    async def _stub():
        return {"theme": "dark", "auto_refresh": True}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/preferences")
        assert r.status_code == 200
        assert r.json()["theme"] == "dark"


def test_lark_settings_reachable(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/lark-settings")
    async def _stub():
        return {"enabled": False}

    @bare_app_no_catchall.post("/api/lark-settings/test")
    async def _test():
        return {"ok": True}

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/lark-settings")
        assert r.status_code == 200
        r2 = client.post("/api/lark-settings/test")
        assert r2.status_code == 200


def test_files_recent_and_raw(bare_app_no_catchall, mobile_dist):
    @bare_app_no_catchall.get("/api/files/recent/{sid}")
    async def _stub_recent(sid: str):
        return {"items": [{"path": "/tmp/a.txt", "size": 42, "modified": 1000}]}

    @bare_app_no_catchall.get("/api/files/raw")
    async def _stub_raw(path: str, sid: str | None = None):
        return "file content"

    register(bare_app_no_catchall)
    with TestClient(bare_app_no_catchall) as client:
        r = client.get("/api/files/recent/sess1")
        assert r.status_code == 200
        r2 = client.get("/api/files/raw", params={"path": "/tmp/a.txt", "sid": "sess1"})
        assert r2.status_code == 200
