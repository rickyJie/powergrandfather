"""Route-level tests for `mobile.backend_patch.mount.register`.

Each test builds a bare FastAPI app, applies the mount patch against a
temp dist directory, and asserts the observable routing behavior via
Starlette TestClient. The main repo `backend/csm/main.py` is not
touched — these tests verify the patch's contract in isolation.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mobile.backend_patch import register


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_mobile_root_returns_index_html(bare_app, mobile_dist):
    assert register(bare_app) is True
    r = _client(bare_app).get("/m/")
    assert r.status_code == 200
    assert "mobile-shim" in r.text


def test_mobile_deep_path_returns_index_html(bare_app, mobile_dist):
    """Vue Router history mode: /m/settings → index.html (client-side route)."""
    register(bare_app)
    r = _client(bare_app).get("/m/settings")
    assert r.status_code == 200
    assert "mobile-shim" in r.text


def test_mobile_nested_deep_path_returns_index_html(bare_app, mobile_dist):
    register(bare_app)
    r = _client(bare_app).get("/m/sessions/deep/nested/path")
    assert r.status_code == 200
    assert "mobile-shim" in r.text


def test_mobile_asset_returns_real_file(bare_app, mobile_dist):
    """/m/assets/<name> must serve from dist/assets (StaticFiles mount)."""
    register(bare_app)
    r = _client(bare_app).get("/m/assets/hello.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_mobile_real_root_file_returns_that_file(bare_app, mobile_dist):
    """Root-level real files (favicon, manifest, sw.js) go through the
    catch-all's is_file() branch, not fallback to index.html."""
    register(bare_app)
    r = _client(bare_app).get("/m/favicon.ico")
    assert r.status_code == 200
    assert r.content.startswith(b"\x00\x00\x01\x00")


def test_api_not_captured_by_mobile_mount(bare_app, mobile_dist):
    """After mounting, /api/health still returns JSON from the shim
    endpoint — mobile catch-all only matches /m/*."""
    register(bare_app)
    r = _client(bare_app).get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_desktop_still_served_at_root(bare_app, mobile_dist):
    """Desktop `/` catch-all was registered first in `bare_app`; adding
    mobile mount must not shadow it."""
    register(bare_app)
    r = _client(bare_app).get("/")
    assert r.status_code == 200
    assert "desktop-shim" in r.text


def test_desktop_deep_path_unchanged(bare_app, mobile_dist):
    """Desktop /some/deep/path (non /m/) still hits the desktop catch-all."""
    register(bare_app)
    r = _client(bare_app).get("/dashboard/whatever")
    assert r.status_code == 200
    assert "desktop-shim" in r.text


def test_mobile_mount_no_op_when_dist_missing(bare_app, empty_mobile_dist):
    """dist without index.html → register returns False, no /m/ routes registered."""
    assert register(bare_app) is False
    r = _client(bare_app).get("/m/")
    # Falls through to desktop catch-all which serves desktop-shim (per
    # the shim's implementation) since /m/ doesn't hit /api or /proxy.
    assert r.status_code == 200
    assert "desktop-shim" in r.text


def test_register_idempotent(bare_app, mobile_dist):
    """Calling register twice — second call must return False and not
    stack duplicate routes."""
    assert register(bare_app) is True
    assert register(bare_app) is False
    # Sanity: /m/ still works exactly once
    r = _client(bare_app).get("/m/")
    assert r.status_code == 200
    assert "mobile-shim" in r.text
