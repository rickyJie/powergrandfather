"""Integration tests for /api/settings/proxy-env.

Mounts the router on a bare FastAPI instance with a stub SessionManager
so we can:

  * assert GET reads app.state.proxy_resolve (the boot-time snapshot),
  * assert POST /refresh re-runs the resolver and pushes into the SM,
  * verify per-var provenance + warnings surface to the wire.

We patch `resolve_proxy_env` on the api module (that's the one the route
imports) so the refresh doesn't actually shell out.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from csm.modules.session_manager.env import ProxyResolveResult
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _StubSessionManager:
    def __init__(self) -> None:
        self.env: dict[str, str] = {}

    def set_proxy_env(self, env: dict[str, str]) -> None:
        self.env = dict(env)


@pytest.fixture
async def client():
    from csm.api.proxy_env import router

    app = FastAPI()
    app.include_router(router)
    app.state.proxy_resolve = ProxyResolveResult(
        env={"HTTP_PROXY": "http://boot:1"},
        sources={"HTTP_PROXY": "sniff"},
        sniff_shell="/bin/zsh",
        env_file_path=Path("/tmp/proxy.env"),
        env_file_exists=False,
        warnings=("startup warning",),
    )
    app.state.session_manager = _StubSessionManager()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        yield c, app


async def test_get_returns_boot_time_snapshot(client) -> None:
    c, _app = client
    r = await c.get("/api/settings/proxy-env")
    assert r.status_code == 200
    body = r.json()
    assert body["vars"] == {
        "HTTP_PROXY": {"value": "http://boot:1", "source": "sniff"}
    }
    assert body["sniff_shell"] == "/bin/zsh"
    assert body["env_file_path"] == "/tmp/proxy.env"
    assert body["env_file_exists"] is False
    assert body["warnings"] == ["startup warning"]


async def test_refresh_reruns_resolver_and_pushes_to_sm(client) -> None:
    c, app = client
    fresh = ProxyResolveResult(
        env={"HTTPS_PROXY": "http://refreshed:2", "NO_PROXY": "localhost"},
        sources={"HTTPS_PROXY": "file", "NO_PROXY": "sniff"},
        sniff_shell="/bin/zsh",
        env_file_path=Path("/tmp/proxy.env"),
        env_file_exists=True,
        warnings=(),
    )
    with patch("csm.api.proxy_env.resolve_proxy_env", return_value=fresh) as mock_resolve:
        r = await c.post("/api/settings/proxy-env/refresh")
    assert r.status_code == 200
    assert mock_resolve.call_count == 1

    body = r.json()
    assert body["vars"]["HTTPS_PROXY"] == {"value": "http://refreshed:2", "source": "file"}
    assert body["vars"]["NO_PROXY"] == {"value": "localhost", "source": "sniff"}
    assert body["env_file_exists"] is True

    # State pushed into the SessionManager + updated on app.state.
    assert app.state.session_manager.env == {
        "HTTPS_PROXY": "http://refreshed:2",
        "NO_PROXY": "localhost",
    }
    assert app.state.proxy_resolve is fresh


async def test_refresh_survives_missing_session_manager(client) -> None:
    c, app = client
    # Simulate lifespan not yet finished (unlikely at runtime but the guard
    # exists — cover it).
    del app.state.session_manager
    fresh = ProxyResolveResult(env={}, sources={}, sniff_shell=None,
                               env_file_path=None, env_file_exists=False,
                               warnings=())
    with patch("csm.api.proxy_env.resolve_proxy_env", return_value=fresh):
        r = await c.post("/api/settings/proxy-env/refresh")
    assert r.status_code == 200


# ---------- PUT / DELETE .../file ---------------------------------------


async def test_put_file_writes_and_pushes(client, tmp_path, monkeypatch) -> None:
    c, app = client
    file_path = tmp_path / "sub" / "proxy.env"  # parent dir does not exist
    monkeypatch.setattr("csm.api.proxy_env.settings.proxy_env_file", file_path)

    fresh = ProxyResolveResult(
        env={"HTTP_PROXY": "http://p:1", "NO_PROXY": "localhost"},
        sources={"HTTP_PROXY": "file", "NO_PROXY": "file"},
        sniff_shell="/bin/zsh",
        env_file_path=file_path,
        env_file_exists=True,
        warnings=(),
    )
    with patch("csm.api.proxy_env.resolve_proxy_env", return_value=fresh):
        r = await c.put(
            "/api/settings/proxy-env/file",
            json={"entries": {"HTTP_PROXY": "http://p:1", "NO_PROXY": "localhost"}},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["vars"]["HTTP_PROXY"] == {"value": "http://p:1", "source": "file"}
    # File written with parent dir created.
    assert file_path.exists()
    contents = file_path.read_text()
    assert "HTTP_PROXY='http://p:1'" in contents
    assert "NO_PROXY='localhost'" in contents
    # Pushed to SM.
    assert app.state.session_manager.env == {
        "HTTP_PROXY": "http://p:1",
        "NO_PROXY": "localhost",
    }


async def test_put_file_rejects_non_whitelist_key(client, tmp_path, monkeypatch) -> None:
    c, _ = client
    monkeypatch.setattr(
        "csm.api.proxy_env.settings.proxy_env_file", tmp_path / "proxy.env"
    )
    r = await c.put(
        "/api/settings/proxy-env/file",
        json={"entries": {"PATH": "/usr/bin"}},
    )
    assert r.status_code == 422  # pydantic validator → 422


async def test_put_file_escapes_single_quotes(client, tmp_path, monkeypatch) -> None:
    c, _ = client
    file_path = tmp_path / "proxy.env"
    monkeypatch.setattr("csm.api.proxy_env.settings.proxy_env_file", file_path)

    fresh = ProxyResolveResult(
        env={"HTTP_PROXY": "weird'value"},
        sources={"HTTP_PROXY": "file"},
        sniff_shell=None, env_file_path=file_path, env_file_exists=True, warnings=(),
    )
    with patch("csm.api.proxy_env.resolve_proxy_env", return_value=fresh):
        r = await c.put(
            "/api/settings/proxy-env/file",
            json={"entries": {"HTTP_PROXY": "weird'value"}},
        )
    assert r.status_code == 200
    # Round-trip: shlex-parse the file we wrote and confirm the value survives.
    import shlex
    for line in file_path.read_text().splitlines():
        if line.startswith("HTTP_PROXY="):
            _, _, rhs = line.partition("=")
            parsed = shlex.split(rhs, posix=True)
            assert parsed == ["weird'value"]
            break
    else:  # pragma: no cover
        raise AssertionError("HTTP_PROXY= line not found")


async def test_delete_file_removes_and_refreshes(client, tmp_path, monkeypatch) -> None:
    c, _ = client
    file_path = tmp_path / "proxy.env"
    file_path.write_text("HTTP_PROXY=http://old:1\n")
    monkeypatch.setattr("csm.api.proxy_env.settings.proxy_env_file", file_path)

    fresh = ProxyResolveResult(
        env={}, sources={}, sniff_shell=None,
        env_file_path=file_path, env_file_exists=False, warnings=(),
    )
    with patch("csm.api.proxy_env.resolve_proxy_env", return_value=fresh):
        r = await c.delete("/api/settings/proxy-env/file")
    assert r.status_code == 200
    assert not file_path.exists()


async def test_delete_file_missing_is_ok(client, tmp_path, monkeypatch) -> None:
    c, _ = client
    monkeypatch.setattr(
        "csm.api.proxy_env.settings.proxy_env_file", tmp_path / "never-existed.env"
    )
    fresh = ProxyResolveResult(env={}, sources={}, sniff_shell=None,
                               env_file_path=None, env_file_exists=False, warnings=())
    with patch("csm.api.proxy_env.resolve_proxy_env", return_value=fresh):
        r = await c.delete("/api/settings/proxy-env/file")
    assert r.status_code == 200
