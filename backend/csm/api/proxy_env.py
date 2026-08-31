"""/api/settings/proxy-env — inspect / refresh / edit the cached proxy env
that SessionManager layers into every spawned session's env.

Endpoints
---------
GET    /api/settings/proxy-env
    Return the resolve result computed at CSM startup (per-var value +
    source, sniff shell, override file path, warnings).

POST   /api/settings/proxy-env/refresh
    Re-run sniff + file merge and push the new env into the running
    SessionManager. New sessions pick up the update immediately;
    already-running children keep their frozen fork-time env.

PUT    /api/settings/proxy-env/file
    body: { entries: { HTTP_PROXY: "...", HTTPS_PROXY: "..." } }
    Overwrite `~/.csm/proxy.env` with the supplied whitelist-filtered
    entries. Auto-resolves + pushes after write. 400 on non-whitelisted
    keys — silent-drop would let typos no-op.

DELETE /api/settings/proxy-env/file
    Delete the override file (missing-file → no-op). Auto-resolves so
    the returned view reflects the pure sniff result.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from csm.config import settings
from csm.modules.session_manager.env import (
    PROXY_WHITELIST,
    ProxyResolveResult,
    resolve_proxy_env,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProxyEnvVar(BaseModel):
    value: str
    source: str  # "sniff" | "file"


class ProxyEnvView(BaseModel):
    vars: dict[str, ProxyEnvVar]
    sniff_enabled: bool
    sniff_shell: str | None
    env_file_path: str | None
    env_file_exists: bool
    warnings: list[str]


class ProxyEnvFilePut(BaseModel):
    """Payload for PUT /api/settings/proxy-env/file.

    Only whitelisted proxy variable names are accepted; anything else in
    `entries` is rejected with 400 (drop-silently would let a typo in
    HTTPS_PROXY silently no-op).
    """

    entries: dict[str, str] = Field(default_factory=dict)

    @field_validator("entries")
    @classmethod
    def _check_whitelist(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if k not in PROXY_WHITELIST]
        if bad:
            raise ValueError(
                f"non-whitelisted keys: {sorted(bad)}. Allowed: {sorted(PROXY_WHITELIST)}"
            )
        return v


def _view(result: ProxyResolveResult, *, sniff_enabled: bool) -> ProxyEnvView:
    return ProxyEnvView(
        vars={
            name: ProxyEnvVar(value=value, source=result.sources.get(name, "sniff"))
            for name, value in result.env.items()
        },
        sniff_enabled=sniff_enabled,
        sniff_shell=result.sniff_shell,
        env_file_path=str(result.env_file_path) if result.env_file_path else None,
        env_file_exists=result.env_file_exists,
        warnings=list(result.warnings),
    )


@router.get("/proxy-env", response_model=ProxyEnvView)
def get_proxy_env(request: Request) -> ProxyEnvView:
    """Return the resolve result computed at CSM startup.

    Warnings + provenance persist from the boot-time sniff — refreshing
    the browser doesn't re-shell to zsh (that would be silly).
    """
    result: ProxyResolveResult = request.app.state.proxy_resolve
    return _view(result, sniff_enabled=settings.proxy_auto_sniff)


def _resolve_and_push(request: Request) -> ProxyResolveResult:
    """Re-run the resolver, stash on app.state, push into SessionManager."""
    result = resolve_proxy_env(
        auto_sniff=settings.proxy_auto_sniff,
        env_file=settings.proxy_env_file,
        sniff_timeout=settings.proxy_sniff_timeout_sec,
    )
    request.app.state.proxy_resolve = result
    sm = getattr(request.app.state, "session_manager", None)
    if sm is not None:
        sm.set_proxy_env(result.env)
    return result


@router.post("/proxy-env/refresh", response_model=ProxyEnvView)
def refresh_proxy_env(request: Request) -> ProxyEnvView:
    """Re-run the sniff + file merge and push the new env into the
    running SessionManager. Sessions already alive keep their old env.
    """
    result = _resolve_and_push(request)
    return _view(result, sniff_enabled=settings.proxy_auto_sniff)


def _serialize_env_file(entries: dict[str, str]) -> str:
    """Emit `KEY=VALUE` lines, escaping single-quotes by wrapping in single
    quotes and using the `'\\''` idiom. Empty values become `KEY=`.
    """
    lines = ["# Managed by CSM (/api/settings/proxy-env). Manual edits are OK."]
    for k in sorted(entries):
        v = entries[k]
        if not v:
            lines.append(f"{k}=")
            continue
        # Single-quote-wrap and escape embedded single quotes.
        escaped = v.replace("'", "'\\''")
        lines.append(f"{k}='{escaped}'")
    return "\n".join(lines) + "\n"


@router.put("/proxy-env/file", response_model=ProxyEnvView)
def put_proxy_env_file(payload: ProxyEnvFilePut, request: Request) -> ProxyEnvView:
    """Write `settings.proxy_env_file` with the supplied entries.

    Empty-string values are kept (writing `HTTP_PROXY=` explicitly clears
    a sniffed value at merge time — file source always wins). To remove a
    key entirely, omit it from `entries` (or use DELETE for a full wipe).

    Directory is created if missing. After writing, the resolver re-runs
    so the returned view already reflects the file's contribution.
    """
    path = settings.proxy_env_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize_env_file(payload.entries), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write {path}: {exc}") from exc

    result = _resolve_and_push(request)
    return _view(result, sniff_enabled=settings.proxy_auto_sniff)


@router.delete("/proxy-env/file", response_model=ProxyEnvView)
def delete_proxy_env_file(request: Request) -> ProxyEnvView:
    """Delete `settings.proxy_env_file`. Missing file is a no-op (200).

    After deletion the resolver re-runs — the panel then reflects the
    pure sniff result (or empty if sniff also produced nothing).
    """
    path = settings.proxy_env_file
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not delete {path}: {exc}") from exc

    result = _resolve_and_push(request)
    return _view(result, sniff_enabled=settings.proxy_auto_sniff)
