"""Unit tests for `/api/files/oss-redirect` key whitelist.

C4 (slot 3): The endpoint 302s to `{oss_base_url}/{key}` and used to
accept any string after `s3://`, including `?evil=…`, `#frag`, `..`,
etc. That made it a phishing amplifier — a link starting with the CSM
host would appear trustworthy but ultimately land on an arbitrary path
under `oss.example.com`. This module verifies the whitelist regex
`^[A-Za-z0-9._\\-/]+$` plus the query/fragment and `..` rejections.

The endpoint has no DB / subsystem dependency, so a bare `FastAPI()`
with just the files router mounted is enough.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from csm.api.files import router as files_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(monkeypatch):
    # `oss_base_url` ships empty — an object-store endpoint is
    # deployment-specific — and the endpoint 503s before it ever validates the
    # key when none is set. Pin one here so these cases exercise the whitelist
    # rather than the unset guard, and so the suite doesn't depend on whatever
    # the shipped default happens to be.
    from csm.config import settings

    monkeypatch.setattr(settings, "oss_base_url", "https://oss.example.com")
    app = FastAPI()
    app.include_router(files_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_oss_redirect_valid_key_returns_302(client: AsyncClient):
    """A plain `s3://bucket/key/with/dots.txt` should 302 to the OSS base."""
    r = await client.get(
        "/api/files/oss-redirect",
        params={"uri": "s3://mybucket/path/to/file_v1.2-final.txt"},
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.endswith("/mybucket/path/to/file_v1.2-final.txt"), loc


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_char", ["?", "#", "&"])
async def test_oss_redirect_invalid_chars_returns_400(
    client: AsyncClient, bad_char: str
):
    """`?`, `#`, `&` must all be rejected — they are the phishing vectors."""
    r = await client.get(
        "/api/files/oss-redirect",
        params={"uri": f"s3://bucket/foo{bad_char}evil=1"},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    # The `?#&` guard fires first with a distinct message; the whitelist
    # regex catches `&` as well. Either way, mention of query/fragment
    # OR "invalid OSS key" is acceptable.
    assert "query/fragment" in detail or "invalid OSS key" in detail


@pytest.mark.asyncio
async def test_oss_redirect_dotdot_returns_400(client: AsyncClient):
    """`..` as a path segment must be blocked (path-traversal guard)."""
    r = await client.get(
        "/api/files/oss-redirect",
        params={"uri": "s3://bucket/foo/../../etc/passwd"},
    )
    assert r.status_code == 400, r.text
    assert ".." in r.json()["detail"]


@pytest.mark.asyncio
async def test_oss_redirect_dotdot_only_bucket_returns_400(client: AsyncClient):
    """`s3://../evil` — `..` in the bucket position must also be rejected."""
    r = await client.get(
        "/api/files/oss-redirect",
        params={"uri": "s3://../evil"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_key",
    [
        "s3://bucket/has space/file.txt",   # space
        "s3://bucket/back\\slash",          # backslash
        "s3://bucket/中文/file.txt",         # non-ASCII
        "s3://bucket/percent%20encoded",    # `%` outside whitelist
        "s3://bucket/at@sign",              # `@`
    ],
)
async def test_oss_redirect_special_chars_returns_400(
    client: AsyncClient, bad_key: str
):
    r = await client.get("/api/files/oss-redirect", params={"uri": bad_key})
    assert r.status_code == 400, r.text
    assert "invalid OSS key" in r.json()["detail"]


@pytest.mark.asyncio
async def test_oss_redirect_non_s3_scheme_returns_400(client: AsyncClient):
    """Sanity: pre-existing scheme guard still fires."""
    r = await client.get(
        "/api/files/oss-redirect",
        params={"uri": "https://evil.example.com/foo"},
    )
    assert r.status_code == 400
