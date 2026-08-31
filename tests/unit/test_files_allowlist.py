"""C6 — optional path allowlist for /api/files/preview and /api/files/raw.

Default (empty list) preserves the original any-path behavior; a
non-empty list restricts access to paths under one of those roots and
403s anything else. Both `preview` and `raw` share the same `_resolve()`
helper, so both are covered.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from csm.api.files import router as files_router
from csm.config import settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(files_router)
    return a


@pytest.fixture
def tmpfile():
    fd, path = tempfile.mkstemp(suffix=".txt", dir="/tmp")
    os.write(fd, b"hello world")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_preview_no_allowlist_allows_any(app, tmpfile, monkeypatch):
    """Empty allowlist (default) = original any-path behavior preserved."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # A path completely outside /tmp — e.g. /etc/hostname (readable).
        r = await c.get("/api/files/preview", params={"path": "/etc/hostname"})
        # Not a 403 from the allowlist; either 200 or 404 depending on file
        # presence — but the request must not be blocked by root policy.
        assert r.status_code != 403
        # And the tmpfile obviously works too.
        r2 = await c.get("/api/files/preview", params={"path": tmpfile})
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_preview_with_allowlist_accepts_under_root(app, tmpfile, monkeypatch):
    """Allowlist containing /tmp accepts a /tmp/... path."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", ["/tmp"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files/preview", params={"path": tmpfile})
        assert r.status_code == 200
        # And /raw for the same path.
        r2 = await c.get("/api/files/raw", params={"path": tmpfile})
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_preview_with_allowlist_rejects_outside_root(app, monkeypatch):
    """Allowlist [/tmp] rejects /etc/passwd with 403."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", ["/tmp"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files/preview", params={"path": "/etc/passwd"})
        assert r.status_code == 403
        assert "not under any allowed root" in r.json()["detail"]
        # /raw same story.
        r2 = await c.get("/api/files/raw", params={"path": "/etc/passwd"})
        assert r2.status_code == 403


@pytest.mark.asyncio
async def test_preview_allowlist_expands_home(app, monkeypatch):
    """`~/...` roots are expanded before comparison."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", ["~"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # A path under home (~/.bashrc likely exists; if not, still not 403).
        home_path = os.path.expanduser("~/.bashrc")
        r = await c.get("/api/files/preview", params={"path": home_path})
        # Either 200 or 404 depending on whether the file exists — but not 403.
        assert r.status_code != 403
        # A path outside home -> 403.
        r2 = await c.get("/api/files/preview", params={"path": "/etc/passwd"})
        assert r2.status_code == 403
