"""C5 — X-CSM-Client header enforcement on /api/*.

Middleware `RequireClientHeaderMiddleware` is defined in `csm.main`. It
returns 400 on non-OPTIONS /api/* requests missing the header, except
for a small exempt list (hooks / metrics / events/stream). We mount a
minimal FastAPI app with the middleware attached — full app.main import
would try to spin up a real EventStream / SessionManager / etc via
lifespan, which is not what this test cares about.
"""
from __future__ import annotations

import pytest
from csm.config import settings
from csm.main import RequireClientHeaderMiddleware
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.add_middleware(RequireClientHeaderMiddleware)

    # Register the routes we care about — same prefixes as real routers so
    # the middleware's prefix logic sees the same shape.
    @a.get("/api/sessions")
    async def _sessions() -> dict:
        return {"ok": True}

    @a.post("/api/sessions")
    async def _sessions_post() -> dict:
        return {"created": True}

    @a.post("/api/hooks/notification")
    async def _hooks() -> dict:
        return {"received": True}

    @a.get("/api/metrics")
    async def _metrics() -> str:
        return "csm_up 1"

    @a.get("/api/events/stream")
    async def _sse() -> dict:
        return {"streaming": True}

    @a.get("/api/events/recent")
    async def _recent() -> dict:
        return {"events": []}

    # File preview GETs — added to exempt list today because window.open /
    # <img src> / <a href download> can't attach custom headers.
    @a.get("/api/files/preview")
    async def _files_preview() -> dict:
        return {"preview": True}

    @a.get("/api/files/raw")
    async def _files_raw() -> dict:
        return {"raw": True}

    @a.get("/api/files/oss-redirect")
    async def _files_oss() -> dict:
        return {"oss": True}

    @a.get("/api/files/recent/abc")
    async def _files_recent() -> dict:
        return {"recent": True}

    @a.get("/health")  # non-/api path — always allowed
    async def _health() -> dict:
        return {"status": "ok"}

    return a


@pytest.mark.asyncio
async def test_api_without_client_header_returns_400(app):
    """GET /api/sessions without the header is rejected 400."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/sessions")
        assert r.status_code == 400
        assert "X-CSM-Client" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_with_client_header_passes(app):
    """GET /api/sessions with X-CSM-Client: 1 reaches the handler."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/sessions", headers={"X-CSM-Client": "1"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_api_post_without_client_header_returns_400(app):
    """POST /api/sessions (form-encoded CSRF vector) also 400s without header."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/api/sessions",
            content="cwd=/tmp",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_hooks_exempt(app):
    """/api/hooks/* bypasses the header requirement (loopback+Host gated)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/hooks/notification")
        assert r.status_code == 200
        assert r.json() == {"received": True}


@pytest.mark.asyncio
async def test_metrics_exempt(app):
    """/api/metrics bypasses (Prometheus scraper can't set custom headers)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/metrics")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_sse_exempt(app):
    """/api/events/stream bypasses (browser EventSource can't set headers)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/events/stream")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_events_recent_still_gated(app):
    """/api/events/recent (JSON, not SSE) still requires the header — the
    exempt prefix is specifically `/api/events/stream`, not `/api/events`."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/events/recent")
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_non_api_paths_unaffected(app):
    """Non-/api/* paths pass without the header (SPA fallback, /health, etc)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_files_preview_exempt(app):
    """/api/files/preview bypasses — opened via window.open, no custom header."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files/preview?path=/tmp/x")
        assert r.status_code == 200
        assert r.json() == {"preview": True}


@pytest.mark.asyncio
async def test_files_raw_exempt(app):
    """/api/files/raw bypasses — used as <img src> and <a href download>."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files/raw?path=/tmp/x")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_files_oss_redirect_exempt(app):
    """/api/files/oss-redirect bypasses — window.open target for s3:// links."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files/oss-redirect?uri=s3://b/k")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_files_recent_still_gated(app):
    """/api/files/recent/{sid} still requires header — it's fetched via axios."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files/recent/abc")
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_options_preflight_bypasses_check(app):
    """OPTIONS preflight requests are not blocked (CORS middleware handles them).

    The middleware short-circuits on method == OPTIONS so the CORS layer
    below can respond with the appropriate Access-Control-* headers.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.options("/api/sessions")
        # No OPTIONS handler is registered, so FastAPI returns 405, but
        # crucially not 400 from our middleware.
        assert r.status_code != 400


@pytest.mark.asyncio
async def test_access_token_protects_api_and_browser_routes(app, monkeypatch):
    monkeypatch.setattr(settings, "access_token", "secret-token")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        denied = await c.get("/health")
        assert denied.status_code == 401

        bootstrap = await c.get("/health?token=secret-token")
        assert bootstrap.status_code == 200
        assert "HttpOnly" in bootstrap.headers["set-cookie"]

        allowed = await c.get(
            "/api/sessions",
            headers={"X-CSM-Client": "1", "X-CSM-Token": "secret-token"},
        )
        assert allowed.status_code == 200
