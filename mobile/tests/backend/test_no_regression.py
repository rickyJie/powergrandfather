"""Prove that applying `register(app)` does not break existing desktop
route behavior. Uses the bare_app shim (which includes desktop spa_fallback
+ a stub /api/health) to detect any shadowing.

These tests are the mobile-side complement to `tests/` — main repo
tests remain untouched, but we can still guard the invariants from here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mobile.backend_patch import register


def test_desktop_root_unchanged(bare_app, mobile_dist):
    """/ still returns the desktop SPA shell after register()."""
    register(bare_app)
    with TestClient(bare_app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "desktop-shim" in r.text


def test_desktop_deep_path_unchanged(bare_app, mobile_dist):
    """Random desktop path still routes to desktop catch-all."""
    register(bare_app)
    with TestClient(bare_app) as client:
        r = client.get("/dashboard/foo")
        assert r.status_code == 200
        assert "desktop-shim" in r.text


def test_api_still_json(bare_app, mobile_dist):
    """/api/health returns JSON, not shadowed by /m/ or catch-all."""
    register(bare_app)
    with TestClient(bare_app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_api_404_still_404(bare_app, mobile_dist):
    """Non-existent /api/foo returns 404 (via desktop catch-all's api-prefix rejection)."""
    register(bare_app)
    with TestClient(bare_app) as client:
        r = client.get("/api/nonexistent")
        assert r.status_code == 404


def test_mobile_and_desktop_coexist(bare_app, mobile_dist):
    """Mount both patches; verify both /m/ and / work simultaneously."""
    register(bare_app)
    with TestClient(bare_app) as client:
        mobile_r = client.get("/m/")
        desktop_r = client.get("/")
        assert mobile_r.status_code == 200
        assert desktop_r.status_code == 200
        assert "mobile-shim" in mobile_r.text
        assert "desktop-shim" in desktop_r.text


def test_register_returns_false_on_second_call(bare_app, mobile_dist):
    """Idempotent contract: second register() is a no-op."""
    assert register(bare_app) is True
    assert register(bare_app) is False
    # And routes still work
    with TestClient(bare_app) as client:
        assert client.get("/m/").status_code == 200
        assert client.get("/").status_code == 200
