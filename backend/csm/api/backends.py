"""GET /api/backends — list every registered CLI adapter + probe status.

Used by the frontend first-run wizard (which adapters can the user pick?)
and the "New session" dialog (which agents are usable right now?).

Response shape:
    [
        {
            "name": "claude",
            "display_name": "Claude Code",
            "icon": "claude",
            "enabled": true,           # default; CSM_ENABLE_CLAUDE=0 disables
            "status": {
                "installed": true,
                "authenticated": true,
                "version": "1.2.3",
                "error": null,
                "capabilities": ["hooks", "pre_spawn_session_id", "interactive_stream"],
                "usable": true
            }
        },
        ...
    ]

Probe results are NOT cached — the CLI's install / auth state can change
between requests (user runs `claude login`, uninstalls codex, etc.). If
probe cost becomes a bottleneck we'd add TTL caching in the registry.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from csm.backends import AdapterRegistry
from csm.backends.base import flag_to_dict
from csm.backends.registry import is_agent_enabled

router = APIRouter(prefix="/api/backends", tags=["backends"])


def _registry(request: Request) -> AdapterRegistry:
    reg = getattr(request.app.state, "adapter_registry", None)
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail="adapter registry not initialized",
        )
    return reg


def _serialize_backend(adapter, *, include_ui_schema: bool = True) -> dict:
    """Shared serialization for both list + single endpoints.

    `include_ui_schema=True` adds `color / default_argv / flags_schema` — the
    frontend schema-driven UI depends on these. Only set to False if we
    ever add a lightweight probe endpoint that shouldn't do the full dump.
    """
    st = adapter.probe()
    body: dict = {
        "name": adapter.name,
        "display_name": adapter.display_name,
        "icon": adapter.icon,
        "enabled": is_agent_enabled(adapter.name),
        "status": {
            "installed": st.installed,
            "authenticated": st.authenticated,
            "version": st.version,
            "error": st.error,
            "capabilities": sorted(c.value for c in st.capabilities),
            "usable": st.usable,
        },
    }
    if include_ui_schema:
        body["color"] = getattr(adapter, "color", None) or "var(--ink)"
        # Older adapters may not have implemented the schema methods yet;
        # fall back to empty defaults so the API stays 200-OK.
        try:
            body["default_argv"] = adapter.default_argv()
        except (AttributeError, NotImplementedError):
            body["default_argv"] = adapter.name
        try:
            body["flags_schema"] = [flag_to_dict(f) for f in adapter.flags_schema()]
        except (AttributeError, NotImplementedError):
            body["flags_schema"] = []
    return body


@router.get("")
async def list_backends(request: Request) -> list[dict]:
    """Return every registered adapter with current probe status + UI schema."""
    reg = _registry(request)
    return [_serialize_backend(a) for a in reg.all()]


@router.get("/{name}")
async def get_backend(name: str, request: Request) -> dict:
    """Return one adapter by name. 404 if not registered."""
    reg = _registry(request)
    if name not in reg:
        raise HTTPException(
            status_code=404,
            detail=f"unknown agent {name!r}; registered: {reg.names()}",
        )
    return _serialize_backend(reg.get(name))
