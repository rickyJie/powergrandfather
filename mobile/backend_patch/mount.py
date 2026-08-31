"""Idempotent mount patch — attaches the mobile SPA under /m/ to an existing FastAPI app.

Called by ``mobile.backend_patch._factory`` (which is what
``mobile/scripts/start_with_mobile.sh`` hands to ``uvicorn --factory``).
The main repo's ``backend/csm/main.py`` is not modified — if the wrapper
is not used, ``register`` is never called and the mobile SPA never mounts.

Idempotency: ``register(app)`` may be called multiple times; the second
call is a no-op (returns False). We track this via a private attribute on
the app instance so subsequent imports or hot-reloads don't stack duplicate
mounts / routes.
"""

from __future__ import annotations

from csm.config import settings
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_MOBILE_DIST = settings.project_root / "mobile" / "frontend" / "dist"

_REGISTERED_ATTR = "_csm_mobile_mount_registered"

# Entry documents carry no content hash in their URL, so a stale cached copy
# pins a client to an old build forever: it never re-requests index.html, so
# it never learns the new hashed chunk names. FileResponse sets only
# last-modified + etag, and a response with no Cache-Control is subject to
# HEURISTIC freshness — roughly 10% of the response's age. For an index.html
# that is days old by the time it's cached, that is hours of staleness.
#
# This is not theoretical: after a mobile rebuild the Android WebView
# (WebViewActivity sets cacheMode = LOAD_DEFAULT) kept serving the previous
# build and never hit the network for /m/ at all — only sw.js, which browsers
# always revalidate, showed up in the access log. Force revalidation on the
# unhashed entry points; /m/assets/* is content-addressed and stays cacheable.
_ENTRY_FILES = frozenset({"index.html", "sw.js", "manifest.webmanifest"})
_NO_HEURISTIC_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


def _serve(path):
    """FileResponse that refuses heuristic caching for unhashed entry files."""
    if path.name in _ENTRY_FILES:
        return FileResponse(path, headers=_NO_HEURISTIC_CACHE)
    return FileResponse(path)


def dist_dir():
    """Return the resolved mobile SPA dist directory (Path)."""
    return _MOBILE_DIST


def register(app: FastAPI) -> bool:
    """Attach mobile SPA mount to ``app``.

    Returns True on first successful mount, False if the mobile dist is
    missing or if ``register`` was already called on this app.

    Route ordering: `csm.main:app` registers a desktop catch-all
    ``@app.get("/{spa_path:path}")`` for its SPA fallback. FastAPI matches
    routes in registration order, so a naively-appended mobile route would
    get shadowed. We work around this by snapshotting the router.routes
    length before adding mobile handlers, then moving newly-added routes
    to the FRONT of the router. Mobile `/m/*` wins; anything else still
    falls through to the desktop catch-all.
    """
    if getattr(app, _REGISTERED_ATTR, False):
        return False
    if not _MOBILE_DIST.is_dir() or not (_MOBILE_DIST / "index.html").exists():
        return False

    before = len(app.router.routes)

    assets_dir = _MOBILE_DIST / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/m/assets",
            StaticFiles(directory=assets_dir),
            name="mobile-assets",
        )

    @app.get("/m", include_in_schema=False)
    @app.get("/m/", include_in_schema=False)
    @app.get("/m/{spa_path:path}", include_in_schema=False)
    async def mobile_spa_fallback(spa_path: str = ""):
        # Match desktop spa_fallback symmetry: serve real files (favicon,
        # manifest.webmanifest, service worker, ...) if present, otherwise
        # fall through to index.html so Vue Router history mode works.
        if spa_path:
            candidate = (_MOBILE_DIST / spa_path).resolve()
            dist_root = _MOBILE_DIST.resolve()
            if candidate.is_file() and candidate.is_relative_to(dist_root):
                return _serve(candidate)
        return _serve(_MOBILE_DIST / "index.html")

    # Move newly-added routes to the front so they preempt any pre-existing
    # catch-all `/{spa_path:path}`. Order among the new routes is preserved.
    new_routes = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = new_routes

    setattr(app, _REGISTERED_ATTR, True)
    return True
