"""Isolated pytest conftest for the mobile backend patch test suite.

Design intent: DO NOT import from `tests/conftest.py`. If a fixture is
useful for both suites, refactor it into `csm.*` production code first
(or duplicate it here). The main repo `tests/` is untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Half this suite imports `mobile.backend_patch.*`, which needs the REPO ROOT on
# sys.path — `pip install -e .` only puts `backend/` there. Without this, the
# documented `pytest mobile/tests/backend` dies at collection with `No module
# named 'mobile'`, and the suite is only runnable by whoever remembers to set
# PYTHONPATH. A suite that is awkward to run is a suite that stops being run.
#
# Safe to do after this file's own imports: pytest imports conftest before any
# test module, so the path is in place by the time `mobile.*` is first needed.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _make_app_without_catchall() -> FastAPI:
    """Shim without any catch-all — useful for tests that add their own
    /api/... stubs AFTER register(), since register()'s reordering only
    moves mobile routes to the front, not test-added stubs."""
    app = FastAPI()

    @app.get("/api/health", include_in_schema=False)
    async def _health():
        return {"status": "ok", "source": "test-shim"}

    return app


def _add_desktop_catchall(app: FastAPI) -> None:
    """Append a desktop spa_fallback shim mirroring csm.main:spa_fallback.
    Call AFTER register(app) if the test wants both patches on one app."""
    @app.get("/", include_in_schema=False)
    @app.get("/{spa_path:path}", include_in_schema=False)
    async def _desktop_spa_fallback(spa_path: str = ""):
        from fastapi import HTTPException

        if spa_path.startswith("api/") or spa_path.startswith("proxy/"):
            raise HTTPException(status_code=404)
        return HTMLResponse("<html><body>desktop-shim</body></html>")


@pytest.fixture
def bare_app() -> FastAPI:
    """A minimal FastAPI app resembling the shape of `csm.main:app`, with
    desktop spa_fallback catch-all pre-registered. Use this when the test
    only needs to verify mount behavior against the desktop shim."""
    app = _make_app_without_catchall()
    _add_desktop_catchall(app)
    return app


@pytest.fixture
def bare_app_no_catchall() -> FastAPI:
    """FastAPI app without any /{spa_path:path} catch-all. Use this when
    the test needs to register its own /api/... stubs AFTER `register()`
    and doesn't want them shadowed by a desktop catch-all."""
    return _make_app_without_catchall()


@pytest.fixture
def mobile_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Populate a temp `mobile/frontend/dist` directory and point the mount
    patch at it via monkeypatching `_MOBILE_DIST`. Yields the dist path so
    tests can add / remove files to simulate build states."""
    dist = tmp_path / "mobile" / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><body>mobile-shim</body></html>", encoding="utf-8"
    )
    (dist / "assets" / "hello.js").write_text("console.log('hi');", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")

    from mobile.backend_patch import mount as mount_mod

    monkeypatch.setattr(mount_mod, "_MOBILE_DIST", dist)
    return dist


@pytest.fixture
def empty_mobile_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Dist directory that does NOT contain index.html — register() must no-op."""
    dist = tmp_path / "mobile" / "frontend" / "dist"
    dist.mkdir(parents=True)
    from mobile.backend_patch import mount as mount_mod

    monkeypatch.setattr(mount_mod, "_MOBILE_DIST", dist)
    return dist
