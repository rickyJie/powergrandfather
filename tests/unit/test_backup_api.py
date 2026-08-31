"""Happy-path + boundary coverage for `/api/backup/*` (Phase 4).

The router snapshots ``settings.db_path`` via SQLite's online backup
API, tars in ``tasks/`` + ``alembic/versions/`` alongside a
``metadata.json``, and drops the archive under
``settings.project_root / "backups"``. Tests point both settings at a
tmp dir and pre-populate them with fixture files.
"""
from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest
import pytest_asyncio
from csm.api.backup import router as backup_router
from csm.config import settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES ('deadbeef1234')")
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.execute("INSERT INTO dummy VALUES (1)")
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Mount backup router against a tmp project root + seeded sqlite."""
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "db_path", Path("csm.db"))  # relative → resolves under project_root
    _seed_db(tmp_path / "csm.db")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "smoke.workflow.yaml").write_text("name: smoke\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001_bootstrap.py").write_text("# fixture\n")

    app = FastAPI()
    app.include_router(backup_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_create_bundles_db_tasks_alembic(client, tmp_path):
    r = await client.post("/api/backup/create", params={"note": "fixture note"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_count"] == 1
    assert body["alembic_head"] == "deadbeef1234"

    archive = tmp_path / "backups" / body["name"]
    assert archive.exists()

    with tarfile.open(archive, "r:gz") as tar:
        members = {m.name for m in tar.getmembers()}
    assert "csm-backup/csm.db" in members
    assert "csm-backup/tasks/smoke.workflow.yaml" in members
    assert "csm-backup/alembic/versions/0001_bootstrap.py" in members
    assert "csm-backup/metadata.json" in members


@pytest.mark.asyncio
async def test_list_reports_created_backup(client):
    r = await client.post("/api/backup/create")
    name = r.json()["name"]
    lst = await client.get("/api/backup/list")
    body = lst.json()
    assert body["count"] == 1
    assert body["backups"][0]["name"] == name
    assert body["total_bytes"] > 0
    assert body["max_backups"] >= 1


@pytest.mark.asyncio
async def test_download_returns_gzip(client):
    r = await client.post("/api/backup/create")
    name = r.json()["name"]
    dl = await client.get(f"/api/backup/download/{name}")
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("application/gzip")
    # gzip magic bytes
    assert dl.content[:2] == b"\x1f\x8b"


@pytest.mark.asyncio
async def test_delete_removes_file(client, tmp_path):
    r = await client.post("/api/backup/create")
    name = r.json()["name"]
    d = await client.delete(f"/api/backup/{name}")
    assert d.status_code == 200
    assert d.json() == {"name": name, "deleted": True}
    assert not (tmp_path / "backups" / name).exists()


@pytest.mark.asyncio
async def test_delete_bogus_name_rejected(client):
    # Path traversal / invalid names must not reach the filesystem.
    for bad in [
        "../../etc/passwd",
        "csm-backup-xxxx.tar.gz",     # not YYYYMMDD-HHMMSS
        "csm-backup-20260101-000000.zip",
        ".hidden",
    ]:
        r = await client.delete(f"/api/backup/{bad}")
        # 400 for regex reject, 404 for well-formed but nonexistent — both
        # OK; the point is the filesystem was not touched.
        assert r.status_code in (400, 404), (bad, r.status_code)


@pytest.mark.asyncio
async def test_create_requires_database_present(client, tmp_path):
    (tmp_path / "csm.db").unlink()
    r = await client.post("/api/backup/create")
    assert r.status_code == 409
    assert "database not found" in r.json()["detail"]
