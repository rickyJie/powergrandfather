"""Backup / restore endpoints (Phase 4 of Top-5 rollout).

Creates a consistent snapshot of a running CSM without requiring the
server to stop. The bundle contains:

    csm-backup/
      csm.db              — snapshotted via SQLite's online backup API
      tasks/              — workflow YAML definitions
      alembic/versions/   — migration scripts (for schema alignment on restore)
      metadata.json       — timestamp, source paths, alembic head, sizes

Restore is *not* an HTTP action: SQLite live-restore would fight the
running process for the WAL, and swapping the DB out from under
lifespan-owned handles is a foot-gun. Instead we ship a shell script
(``scripts/restore_backup.sh``) that the user runs while the service
is stopped.

Security: names are validated against a strict regex before touching
the filesystem; the download endpoint refuses anything containing a
path separator or a leading dot. All I/O is scoped to
``settings.project_root / "backups"``.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from csm.config import settings

router = APIRouter(prefix="/api/backup", tags=["backup"])

# Human-readable timestamped names only. This regex is also the
# path-traversal guard for download/delete — anything outside this
# shape is rejected before it can be joined onto the backup dir.
_NAME_RE = re.compile(r"^csm-backup-\d{8}-\d{6}(?:-[a-z0-9]{1,32})?\.tar\.gz$")
# Cap the number of backups kept before create() starts rejecting to
# avoid a runaway disk-fill via UI mash. Users can delete manually.
_MAX_BACKUPS = 20


def _backup_dir() -> Path:
    d = settings.project_root / "backups"
    d.mkdir(exist_ok=True)
    return d


def _resolved_db_path() -> Path:
    p = settings.db_path
    return p if p.is_absolute() else settings.project_root / p


def _sqlite_online_backup(src: Path, dst: Path) -> None:
    """Copy a SQLite DB using the online backup API.

    Safe to call while other connections (in this process or elsewhere)
    are actively writing — SQLite's backup API handles the concurrency.
    We must open a plain sqlite3 connection because the online backup
    API is a C-level routine on the connection object.
    """
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def _current_alembic_head(db_path: Path) -> str | None:
    """Best-effort read of the current alembic version stamped on the DB.

    Returns None if the alembic_version table doesn't exist yet (fresh
    DBs) — caller reports that as informational.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid backup name {name!r}")


def _backup_entry(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


@router.post("/create")
async def create_backup(note: str = "") -> dict:
    """Snapshot DB + workflow YAMLs + alembic scripts into one tar.gz."""
    root = settings.project_root
    db_path = _resolved_db_path()
    if not db_path.exists():
        raise HTTPException(status_code=409, detail=f"database not found at {db_path}")

    existing = sorted(_backup_dir().glob("csm-backup-*.tar.gz"))
    if len(existing) >= _MAX_BACKUPS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"backup retention cap reached ({len(existing)}/{_MAX_BACKUPS}); "
                "delete an old backup before creating a new one"
            ),
        )

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    name = f"csm-backup-{ts}.tar.gz"
    out_path = _backup_dir() / name

    with tempfile.TemporaryDirectory(prefix="csm-backup-") as tmp:
        stage = Path(tmp) / "csm-backup"
        stage.mkdir()

        # 1. Consistent DB snapshot via online backup API.
        _sqlite_online_backup(db_path, stage / "csm.db")

        # 2. Workflow YAMLs (whole tree, excluding __pycache__).
        tasks_dir = root / "tasks"
        if tasks_dir.is_dir():
            shutil.copytree(
                tasks_dir,
                stage / "tasks",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

        # 3. Alembic version scripts — needed to reproduce schema on
        #    the target box if it's on an older HEAD.
        alembic_versions = root / "alembic" / "versions"
        if alembic_versions.is_dir():
            dest = stage / "alembic" / "versions"
            dest.mkdir(parents=True)
            for f in alembic_versions.glob("*.py"):
                shutil.copy2(f, dest / f.name)

        # 4. Metadata for the recipient.
        meta = {
            "created_at": datetime.now(tz=UTC).isoformat(),
            "csm_project_root": str(root),
            "csm_db_path": str(db_path),
            "alembic_head": _current_alembic_head(db_path),
            "workflow_count": (
                len(list(tasks_dir.glob("*.workflow.yaml"))) if tasks_dir.is_dir() else 0
            ),
            "db_size_bytes": db_path.stat().st_size,
            "note": note[:500],
            "restore_hint": (
                "Stop the CSM service, then run scripts/restore_backup.sh "
                "<this-tar.gz> from the target project root."
            ),
        }
        (stage / "metadata.json").write_text(json.dumps(meta, indent=2))

        # 5. Tar the staging dir. arcname='csm-backup' keeps the outer
        #    prefix so `tar tzf` shows the expected layout.
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(stage, arcname="csm-backup")

    return {
        **_backup_entry(out_path),
        "workflow_count": meta["workflow_count"],
        "alembic_head": meta["alembic_head"],
    }


@router.get("/list")
async def list_backups() -> dict:
    """List available backups, newest first, plus disk usage total."""
    files = sorted(_backup_dir().glob("csm-backup-*.tar.gz"), reverse=True)
    entries = [_backup_entry(f) for f in files]
    total = sum(e["size_bytes"] for e in entries)
    return {
        "backups": entries,
        "count": len(entries),
        "total_bytes": total,
        "max_backups": _MAX_BACKUPS,
        "backup_dir": str(_backup_dir()),
    }


@router.get("/download/{name}")
async def download_backup(name: str) -> FileResponse:
    """Serve a specific backup file for the user to save locally."""
    _validate_name(name)
    path = _backup_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="backup not found")
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=name,
    )


@router.delete("/{name}")
async def delete_backup(name: str) -> dict:
    """Remove a specific backup from disk. Irreversible."""
    _validate_name(name)
    path = _backup_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="backup not found")
    path.unlink()
    return {"name": name, "deleted": True}
