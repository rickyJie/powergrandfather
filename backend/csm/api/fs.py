"""Filesystem browse endpoint for the session cwd picker (I1).

Local-only by design: the backend runs on the user's machine, so this endpoint
just lists directory entries. We refuse to traverse system roots that have no
business being a Claude cwd.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.config import settings
from csm.models.session import Session

router = APIRouter(prefix="/api/fs", tags=["fs"])


# Roots we never let the picker enter. Returning them in the list is fine
# (so user sees they exist) but `path=` cannot resolve into them.
_FORBIDDEN_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/boot")

# Browse is confined to these roots. Anything outside -> 403. Defense in depth
# on top of the forbidden-prefix blocklist below.
_ALLOWED_ROOTS: list[Path] = [
    Path.home(),
    Path("/tmp"),
    Path("/data"),
    Path(settings.project_root),
    Path(settings.claude_projects_dir),
    (Path(settings.tasks_dir) if Path(settings.tasks_dir).is_absolute()
     else Path(settings.project_root) / settings.tasks_dir),
]


def _is_allowed(p: Path) -> bool:
    return any(p == r or p.is_relative_to(r) for r in _ALLOWED_ROOTS)


def _is_forbidden(p: Path) -> bool:
    sp = str(p)
    return any(sp == fp or sp.startswith(fp + "/") for fp in _FORBIDDEN_PREFIXES)


@router.get("/browse")
async def browse(request: Request, path: str = "/") -> dict[str, Any]:
    """List entries under `path`. Returns dirs first, then a few non-hidden files for context."""
    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad path: {e}") from e
    if not _is_allowed(p):
        raise HTTPException(status_code=403, detail="path not allowed")
    if _is_forbidden(p):
        raise HTTPException(status_code=403, detail="forbidden root")
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")

    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if child.name.startswith("."):
                continue  # skip dot-files in the picker
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir and _is_forbidden(child):
                continue
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": is_dir,
            })
            if len(entries) >= 200:
                break
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied") from None

    parent = str(p.parent) if str(p) != "/" else None
    return {
        "path": str(p),
        "parent": parent,
        "entries": entries,
    }


@router.get("/recent-cwds")
async def recent_cwds(
    request: Request,
    limit: int = 10,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Distinct recent cwds of past sessions, ordered by most-recent-use."""
    async with sm() as db:
        # SQLite doesn't have DISTINCT ON; do it in Python from latest sessions.
        res = await db.execute(
            select(Session.cwd, Session.started_at).order_by(Session.started_at.desc()).limit(200)
        )
        seen: list[str] = []
        for cwd, _ts in res.all():
            if not cwd or cwd in seen or not os.path.isdir(cwd):
                continue
            try:
                allowed = _is_allowed(Path(cwd).resolve())
            except Exception:
                allowed = False
            if allowed:
                seen.append(cwd)
            if len(seen) >= limit:
                break
    return {"items": seen}
