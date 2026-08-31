"""Response compression, and the middleware ordering it silently depends on.

Nothing used to be compressed: a mobile session list is 227KB on the wire and
the desktop history page 180KB, re-sent over an SSH tunnel on every cold
start. gzip cuts both by ~62%.

The ordering matters and fails quietly. `GZipMiddleware` only honours
`minimum_size` when it can see a Content-Length; when it can't, it compresses
unconditionally. `RequireClientHeaderMiddleware` is a BaseHTTPMiddleware,
which re-emits the response as a stream and drops that header — so with gzip
registered OUTSIDE it, `minimum_size` was dead and every 60-byte /api/health
poll came back `content-encoding: gzip`. Nothing errors; you just pay CPU to
make tiny responses bigger.
"""
from __future__ import annotations

import pytest
from csm.main import RequireClientHeaderMiddleware
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from httpx import ASGITransport, AsyncClient

BIG = "x" * 8000
HEADERS = {"X-CSM-Client": "1", "Accept-Encoding": "gzip"}


def _app(gzip_innermost: bool) -> FastAPI:
    a = FastAPI()
    # add_middleware prepends, so whatever is added FIRST ends up innermost.
    if gzip_innermost:
        a.add_middleware(GZipMiddleware, minimum_size=1024)
        a.add_middleware(RequireClientHeaderMiddleware)
    else:
        a.add_middleware(RequireClientHeaderMiddleware)
        a.add_middleware(GZipMiddleware, minimum_size=1024)

    @a.get("/api/small")
    async def _small() -> dict:
        return {"ok": True}

    @a.get("/api/big")
    async def _big() -> dict:
        return {"blob": BIG}

    return a


async def _get(app: FastAPI, path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path, headers=HEADERS)


@pytest.mark.asyncio
async def test_large_response_is_compressed():
    r = await _get(_app(gzip_innermost=True), "/api/big")

    assert r.headers.get("content-encoding") == "gzip"
    # httpx decodes transparently, so compare the wire length to the body.
    assert len(r.content) > 4000          # decoded payload is the full blob
    assert int(r.headers["content-length"]) < 1000   # ...but little was sent


@pytest.mark.asyncio
async def test_small_response_is_left_alone():
    """Compressing a sub-1KB body costs CPU and can make it larger."""
    r = await _get(_app(gzip_innermost=True), "/api/small")

    assert "content-encoding" not in r.headers


@pytest.mark.asyncio
async def test_gzip_outside_the_base_middleware_breaks_minimum_size():
    """Pins WHY the ordering in main.py is what it is: flip it and the size
    threshold stops working, which is exactly the regression to prevent."""
    r = await _get(_app(gzip_innermost=False), "/api/small")

    assert r.headers.get("content-encoding") == "gzip", (
        "expected the known-bad ordering to compress even a tiny body; if this "
        "now passes uncompressed, GZipMiddleware changed and main.py's "
        "ordering comment needs revisiting"
    )


def test_real_app_registers_gzip_inside_the_header_middleware():
    """The app under test is the real one, not a hand-built stand-in."""
    from csm.main import app

    stack = [m.cls.__name__ for m in app.user_middleware]
    assert "GZipMiddleware" in stack, stack
    # user_middleware is outermost-first; innermost must come LAST.
    assert stack.index("GZipMiddleware") > stack.index(
        "RequireClientHeaderMiddleware"
    ), f"gzip must sit inside the header middleware, got {stack}"
