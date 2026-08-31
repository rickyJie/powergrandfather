"""API tests for `GET /api/backends`."""
from __future__ import annotations

import pytest_asyncio
from csm.api.backends import router as backends_router
from csm.backends.registry import AdapterRegistry
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.backends._fake_adapter import FakeAdapter


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("CSM_ENABLE_CLAUDE", "1")
    monkeypatch.delenv("CSM_ENABLE_CODEX", raising=False)
    app = FastAPI()
    app.state.adapter_registry = AdapterRegistry([
        FakeAdapter("claude", display_name="Claude Code"),
        FakeAdapter("codex", display_name="Codex CLI", authenticated=False),
    ])
    app.include_router(backends_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


async def test_list_backends_returns_every_registered(client):
    resp = await client.get("/api/backends")
    assert resp.status_code == 200
    body = resp.json()
    names = {b["name"] for b in body}
    assert names == {"claude", "codex"}


async def test_list_backends_status_shape(client):
    resp = await client.get("/api/backends")
    body = resp.json()
    claude = next(b for b in body if b["name"] == "claude")
    assert claude["display_name"] == "Claude Code"
    assert claude["enabled"] is True   # env flag set in fixture
    assert claude["status"]["installed"] is True
    assert claude["status"]["authenticated"] is True
    assert claude["status"]["usable"] is True

    codex = next(b for b in body if b["name"] == "codex")
    assert codex["enabled"] is True    # registered adapters default on
    assert codex["status"]["authenticated"] is False
    assert codex["status"]["usable"] is False


async def test_list_backends_includes_ui_schema(client):
    """M9.1 contract: `color`, `default_argv`, `flags_schema` present.

    These fields drive the schema-driven frontend — adding a new adapter
    must NOT require frontend changes, so the API MUST emit them.
    """
    resp = await client.get("/api/backends")
    body = resp.json()
    for b in body:
        assert "color" in b, f"{b['name']!r} missing color"
        assert "default_argv" in b, f"{b['name']!r} missing default_argv"
        assert "flags_schema" in b, f"{b['name']!r} missing flags_schema"
        # flags_schema entries must have a `kind` discriminator
        for f in b["flags_schema"]:
            assert "kind" in f, f"{b['name']!r} flag missing kind: {f}"
            assert f["kind"] in ("checkbox", "select", "resume", "info")


async def test_get_one_backend_by_name(client):
    resp = await client.get("/api/backends/claude")
    assert resp.status_code == 200
    assert resp.json()["name"] == "claude"


async def test_get_unknown_backend_404s(client):
    resp = await client.get("/api/backends/gemini")
    assert resp.status_code == 404


async def test_no_registry_returns_503(monkeypatch):
    app = FastAPI()  # no adapter_registry
    app.include_router(backends_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.get("/api/backends")
    assert resp.status_code == 503
