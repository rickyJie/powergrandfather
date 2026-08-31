"""Verify SessionManager layers the cached proxy env into spawn env.

We don't actually spawn a subprocess here — that's covered by the
integration test. This unit test exercises the precedence rules of
`_apply_proxy_env`:

    explicit request env > uvicorn's os.environ > sniffed proxy

by driving a bare SessionManager instance's helper directly.
"""

from __future__ import annotations

from csm.backends import build_default_registry
from csm.modules.session_manager.manager import SessionManager


def _sm(proxy_env: dict[str, str]) -> SessionManager:
    # SessionManager's live methods pull from self._sm / self._es etc., but
    # _apply_proxy_env only touches self._proxy_env. Constructing with dummy
    # deps is enough — we never call any lifecycle methods here.
    return SessionManager(
        sessionmaker=None,  # type: ignore[arg-type]
        event_stream=None,  # type: ignore[arg-type]
        adapter_registry=build_default_registry(),
        proxy_env=proxy_env,
    )


def test_apply_proxy_env_fills_missing_vars() -> None:
    sm = _sm({"HTTP_PROXY": "http://sniffed:1", "HTTPS_PROXY": "http://sniffed:1"})
    spawn_env = {"PATH": "/usr/bin"}
    sm._apply_proxy_env(spawn_env)
    assert spawn_env == {
        "PATH": "/usr/bin",
        "HTTP_PROXY": "http://sniffed:1",
        "HTTPS_PROXY": "http://sniffed:1",
    }


def test_apply_proxy_env_does_not_override_existing() -> None:
    sm = _sm({"HTTP_PROXY": "http://sniffed:1"})
    spawn_env = {"HTTP_PROXY": "http://explicit:9"}  # request-provided value
    sm._apply_proxy_env(spawn_env)
    assert spawn_env == {"HTTP_PROXY": "http://explicit:9"}


def test_set_proxy_env_replaces_cache() -> None:
    sm = _sm({"HTTP_PROXY": "http://old:1"})
    sm.set_proxy_env({"HTTPS_PROXY": "http://new:2"})
    # Cache fully replaced, not merged.
    assert sm.proxy_env == {"HTTPS_PROXY": "http://new:2"}


def test_proxy_env_snapshot_is_a_copy() -> None:
    sm = _sm({"HTTP_PROXY": "http://x:1"})
    snap = sm.proxy_env
    snap["HTTP_PROXY"] = "mutated"
    assert sm.proxy_env == {"HTTP_PROXY": "http://x:1"}
