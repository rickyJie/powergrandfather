"""Shared FastAPI dependencies / guard helpers for API routes.

Central place for cross-cutting request checks so multiple routers can reuse
the same policy without drift. Any new module-level helper that guards a
request (auth, loopback, rate-limit) belongs here.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, WebSocket, WebSocketException
from sqlalchemy.ext.asyncio import async_sessionmaker


def get_db_sessionmaker(request: Request) -> async_sessionmaker:
    """FastAPI Dependency: yields the lifespan-wired sessionmaker.

    Prefer this over calling ``csm.db.get_sessionmaker()`` directly in API
    routes — it goes through ``app.state`` and can be swapped for a test
    fixture without global monkey-patching.
    """
    return request.app.state.sessionmaker


def _require_loopback_and_host(request: Request) -> None:
    """Guard for endpoints that must only accept truly local traffic.

    Combines the peer-address loopback check with a `Host:` header allowlist
    to defeat DNS rebinding — an attacker CNAME can resolve to 127.0.0.1 so
    ``request.client.host`` looks loopback while the actual ``Host`` header
    is ``evil.example``, letting the attacker's browser page issue authorized
    requests. Rejecting unknown ``Host`` values closes that path.

    Raises ``HTTPException(403)`` on rejection; otherwise returns ``None``.
    """
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="loopback-only endpoint")
    # Strip optional ":port" suffix; Host header may or may not include one.
    host_header = (request.headers.get("host") or "").split(":")[0].lower()
    allowed = {"127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0"}
    # If the server is bound to a specific address (not the wildcard), also
    # accept that address as a legitimate Host. This is a convenience
    # allowance, not a security one — the loopback client check above is
    # what enforces "only local traffic".
    try:
        from csm.config import settings
        if settings.host and settings.host not in ("0.0.0.0",):
            allowed.add(settings.host.lower())
    except Exception:
        pass
    if host_header not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Host header not in allowlist: {host_header!r}",
        )


def _require_loopback_and_host_ws(websocket: WebSocket) -> None:
    """WebSocket counterpart of ``_require_loopback_and_host``.

    BaseHTTPMiddleware never receives WebSocket scopes, so sensitive
    WebSocket routes must call this before accepting the handshake.
    """
    client_host = websocket.client.host if websocket.client else None
    if client_host not in {"127.0.0.1", "::1"}:
        raise WebSocketException(code=4403, reason="loopback-only endpoint")
    host_header = (websocket.headers.get("host") or "").split(":")[0].lower()
    allowed = {"127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0"}
    try:
        from csm.config import settings
        if settings.host and settings.host != "0.0.0.0":
            allowed.add(settings.host.lower())
    except Exception:
        pass
    if host_header not in allowed:
        raise WebSocketException(
            code=4403,
            reason=f"Host header not in allowlist: {host_header!r}",
        )


def _require_access_ws(websocket: WebSocket) -> None:
    """Require the configured access token for WebSocket handshakes."""
    from csm.config import settings

    expected = settings.access_token
    if not expected:
        return
    supplied = (
        websocket.cookies.get("csm_access_token")
        or websocket.query_params.get("token")
        or websocket.headers.get("x-csm-token")
    )
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise WebSocketException(code=4401, reason="invalid or missing access token")
