"""End-to-end skill-bundle sync across the REAL claude and codex adapters.

Everything else in the sync test suite runs against `FakeSyncAdapter`. This
file deliberately does not: the bug being guarded against — a skill arriving
on the target with only its SKILL.md — lived in the real adapters' write
path, and a fake that reimplements that path can't catch a regression in it.

The homes are temp dirs via `CSM_CLAUDE_HOME` / `CSM_CODEX_HOME`, so nothing
here touches the developer's actual `~/.claude` or `~/.codex`.
"""
from __future__ import annotations

import os
import tempfile

import pytest
import pytest_asyncio
from csm.api.sync import router as sync_router
from csm.backends.base import Capability
from csm.backends.claude.adapter import ClaudeAdapter
from csm.backends.codex.adapter import CodexAdapter
from csm.backends.registry import AdapterRegistry
from csm.models import Base
from csm.modules.sync.service import SyncService
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

SKILL_MD = (
    "---\nname: resource-query\ndescription: query the cluster\n---\n"
    "Call `./query.py` — do NOT curl the API directly.\n"
)


def _write_source_skill(skills_root, name="resource-query"):
    """A skill shaped like the one that exposed the bug: SKILL.md whose
    instructions are useless without the sibling script."""
    d = skills_root / name
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD)
    q = d / "query.py"
    q.write_text("#!/usr/bin/env python3\nprint('gpus')\n")
    q.chmod(0o755)
    (d / "references" / "api.md").write_text("# endpoints\n")
    return d


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    (claude_home / "skills").mkdir(parents=True)
    (codex_home / "skills").mkdir(parents=True)
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CSM_CODEX_HOME", str(codex_home))

    claude, codex = ClaudeAdapter(), CodexAdapter()
    # SYNC_SKILLS is normally added by the boot-time capability probe, which
    # shells out to the real CLIs; grant it directly here.
    for a in (claude, codex):
        a.capabilities = frozenset(a.capabilities | {Capability.SYNC_SKILLS})
    registry = AdapterRegistry([claude, codex])

    app = FastAPI()
    app.state.sessionmaker = sm
    app.state.adapter_registry = registry
    app.state.sync_service = SyncService(sessionmaker=sm, adapter_registry=registry)
    app.include_router(sync_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, sm, {"claude": claude_home, "codex": codex_home}

    await engine.dispose()
    os.unlink(path)


async def _enrol(ac):
    r = await ac.put(
        "/api/sync/config/skills", json={"enrolled_agents": ["claude", "codex"]}
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- migrate


async def test_migrate_carries_the_whole_bundle_to_the_target(client):
    """The headline case. Before bundle sync, codex got SKILL.md and nothing
    else, so its instructions pointed at a file that wasn't there."""
    ac, _, homes = client
    _write_source_skill(homes["claude"] / "skills")
    await _enrol(ac)

    r = await ac.post(
        "/api/sync/skills/migrate", json={"source": "claude", "target": "codex"}
    )
    assert r.status_code == 200, r.text
    entry = next(i for i in r.json()["items"] if i["name"] == "resource-query")
    assert entry["file_count"] == 2
    assert all(s["status"] == "ok" for s in entry["sync"])

    target = homes["codex"] / "skills" / "resource-query"
    assert (target / "SKILL.md").read_text() == SKILL_MD
    assert (target / "query.py").read_text() == "#!/usr/bin/env python3\nprint('gpus')\n"
    assert (target / "references" / "api.md").is_file()


async def test_migrated_helper_script_is_executable_on_the_target(client):
    """`./query.py` in SKILL.md only works if the copy kept its +x bit."""
    ac, _, homes = client
    _write_source_skill(homes["claude"] / "skills")
    await _enrol(ac)
    await ac.post(
        "/api/sync/skills/migrate", json={"source": "claude", "target": "codex"}
    )

    q = homes["codex"] / "skills" / "resource-query" / "query.py"
    assert q.stat().st_mode & 0o777 == 0o755
    assert os.access(q, os.X_OK)


async def test_migrate_skips_generated_artefacts_named_in_csmsyncignore(client):
    ac, _, homes = client
    d = _write_source_skill(homes["claude"] / "skills")
    (d / "reports").mkdir()
    (d / "reports" / "run-1.md").write_text("noise")
    (d / ".csmsyncignore").write_text("reports/\n")
    await _enrol(ac)

    await ac.post(
        "/api/sync/skills/migrate", json={"source": "claude", "target": "codex"}
    )

    target = homes["codex"] / "skills" / "resource-query"
    assert (target / "query.py").is_file()
    assert not (target / "reports").exists()
    assert not (target / ".csmsyncignore").exists()


# ---------------------------------------------------------------- reingest


async def test_reingest_repairs_a_skill_that_predates_bundle_sync(client):
    """The upgrade path: rows created before this feature hold only a body.
    `reingest` re-reads them off disk and re-pushes the missing files."""
    ac, _, homes = client
    _write_source_skill(homes["claude"] / "skills")
    await _enrol(ac)

    # A row as the old code would have made it — body only, no bundle.
    r = await ac.post("/api/sync/skills", json={
        "name": "resource-query",
        "description": "query the cluster",
        "body_md": SKILL_MD,
        "share_scope": ["claude", "codex"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["file_count"] == 0
    target = homes["codex"] / "skills" / "resource-query"
    assert not (target / "query.py").exists()  # the broken state

    r = await ac.post("/api/sync/skills/reingest", params={"agent": "claude"})
    assert r.status_code == 200, r.text
    entry = next(i for i in r.json()["items"] if i["name"] == "resource-query")
    assert entry["action"] == "reingested"
    assert entry["file_count"] == 2

    assert (target / "query.py").is_file()
    assert os.access(target / "query.py", os.X_OK)


async def test_reingest_does_not_push_back_to_the_agent_it_read_from(client):
    """Reading a bundle off claude and writing it straight back to claude is a
    round trip with nothing to say — and for the common case where the skill
    dir is a symlink into a skill-book checkout, the write is refused and logs
    drift for the trouble."""
    ac, _, homes = client
    _write_source_skill(homes["claude"] / "skills")
    await _enrol(ac)
    await ac.post("/api/sync/skills", json={
        "name": "resource-query",
        "description": "query the cluster",
        "body_md": SKILL_MD,
        "share_scope": ["claude", "codex"],
    })

    r = await ac.post("/api/sync/skills/reingest", params={"agent": "claude"})
    entry = next(i for i in r.json()["items"] if i["name"] == "resource-query")
    agents = {s["agent"] for s in entry["sync"]}
    assert agents == {"codex"}, agents

    # The other direction still fans out to claude.
    r = await ac.post("/api/sync/skills/reingest", params={"agent": "codex"})
    entry = next(i for i in r.json()["items"] if i["name"] == "resource-query")
    assert {s["agent"] for s in entry["sync"]} == {"claude"}


async def test_reingest_reports_skills_absent_from_the_source(client):
    ac, _, homes = client
    await _enrol(ac)
    await ac.post("/api/sync/skills", json={
        "name": "ghost", "description": "d",
        "body_md": "---\nname: ghost\n---\n", "share_scope": ["codex"],
    })

    r = await ac.post("/api/sync/skills/reingest", params={"agent": "claude"})
    entry = next(i for i in r.json()["items"] if i["name"] == "ghost")
    assert entry["action"] == "absent"


# ---------------------------------------------------------------- CRUD


async def test_create_skill_with_an_inline_bundle(client):
    ac, _, homes = client
    await _enrol(ac)

    r = await ac.post("/api/sync/skills", json={
        "name": "inline",
        "description": "d",
        "body_md": "---\nname: inline\n---\nrun ./go.sh\n",
        "share_scope": ["claude", "codex"],
        "files": [
            {"rel_path": "go.sh", "content": "#!/bin/sh\necho hi\n", "mode": 0o755},
            {"rel_path": "data/blob.bin", "content": "AAEC", "encoding": "base64"},
        ],
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["file_count"] == 2

    for home in homes.values():
        d = home / "skills" / "inline"
        assert (d / "go.sh").stat().st_mode & 0o777 == 0o755
        assert (d / "data" / "blob.bin").read_bytes() == b"\x00\x01\x02"


async def test_update_without_files_key_leaves_the_bundle_alone(client):
    """A description-only edit must not silently empty the bundle — that
    would be the original bug, reintroduced through the API."""
    ac, _, homes = client
    await _enrol(ac)
    r = await ac.post("/api/sync/skills", json={
        "name": "keepme", "description": "before",
        "body_md": "---\nname: keepme\n---\n", "share_scope": ["claude"],
        "files": [{"rel_path": "helper.py", "content": "x = 1\n"}],
    })
    kid = r.json()["data"]["id"]

    r = await ac.put(f"/api/sync/skills/{kid}", json={
        "name": "keepme", "description": "after",
        "body_md": "---\nname: keepme\n---\n", "share_scope": ["claude"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["file_count"] == 1
    assert (homes["claude"] / "skills" / "keepme" / "helper.py").is_file()


async def test_update_with_an_explicit_empty_list_clears_the_bundle(client):
    ac, _, homes = client
    await _enrol(ac)
    r = await ac.post("/api/sync/skills", json={
        "name": "clearme", "description": "d",
        "body_md": "---\nname: clearme\n---\n", "share_scope": ["claude"],
        "files": [{"rel_path": "helper.py", "content": "x = 1\n"}],
    })
    kid = r.json()["data"]["id"]

    r = await ac.put(f"/api/sync/skills/{kid}", json={
        "name": "clearme", "description": "d",
        "body_md": "---\nname: clearme\n---\n", "share_scope": ["claude"],
        "files": [],
    })
    assert r.json()["data"]["file_count"] == 0
    assert not (homes["claude"] / "skills" / "clearme" / "helper.py").exists()


async def test_get_one_skill_returns_bundle_contents(client):
    ac, _, _ = client
    await _enrol(ac)
    r = await ac.post("/api/sync/skills", json={
        "name": "detailed", "description": "d",
        "body_md": "---\nname: detailed\n---\n", "share_scope": ["claude"],
        "files": [{"rel_path": "a.md", "content": "hello", "mode": 0o644}],
    })
    kid = r.json()["data"]["id"]

    r = await ac.get(f"/api/sync/skills/{kid}")
    assert r.status_code == 200
    f = r.json()["files"][0]
    assert (f["rel_path"], f["content"], f["encoding"]) == ("a.md", "hello", "utf-8")

    # The list endpoint stays metadata-only.
    r = await ac.get("/api/sync/skills")
    row = next(i for i in r.json()["items"] if i["name"] == "detailed")
    assert row["file_count"] == 1 and "files" not in row


@pytest.mark.parametrize("bad_path", ["../escape.md", "/abs.md", "SKILL.md"])
async def test_create_skill_rejects_unsafe_rel_paths(client, bad_path):
    ac, _, _ = client
    await _enrol(ac)
    r = await ac.post("/api/sync/skills", json={
        "name": "evil", "description": "d",
        "body_md": "---\nname: evil\n---\n", "share_scope": ["claude"],
        "files": [{"rel_path": bad_path, "content": "x"}],
    })
    assert r.status_code == 422, r.text


async def test_available_lists_bundle_size_per_agent(client):
    """A file-count mismatch between two agents is the symptom the picker
    should surface at a glance."""
    ac, _, homes = client
    _write_source_skill(homes["claude"] / "skills")
    # codex has the same skill but only its SKILL.md — the broken shape.
    d = homes["codex"] / "skills" / "resource-query"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD)

    r = await ac.get("/api/sync/skills/available")
    entry = next(i for i in r.json() if i["name"] == "resource-query")
    assert entry["file_count"] == {"claude": 2, "codex": 0}


# ---------------------------------------------------------------- symlink guard


async def test_push_refuses_to_write_through_a_symlinked_skill_dir(client):
    """Most real skill dirs are symlinks into a skill-book repo. Syncing must
    not edit that working tree — it should skip, visibly."""
    ac, sm, homes = client
    external = homes["claude"].parent / "skill-book" / "linked"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: linked\n---\nORIGINAL\n")
    os.symlink(external, homes["claude"] / "skills" / "linked")
    await _enrol(ac)

    r = await ac.post("/api/sync/skills", json={
        "name": "linked", "description": "d",
        "body_md": "---\nname: linked\n---\nOVERWRITTEN\n",
        "share_scope": ["claude", "codex"],
    })
    assert r.status_code == 200, r.text
    by_agent = {s["agent"]: s for s in r.json()["sync"]}
    assert by_agent["claude"]["status"] == "skipped"
    assert "symlink" in by_agent["claude"]["detail"]
    # codex is a normal directory, so it still gets the update.
    assert by_agent["codex"]["status"] == "ok"

    assert "ORIGINAL" in (external / "SKILL.md").read_text()

    r = await ac.get("/api/sync/drift")
    reasons = {d["reason"] for d in r.json()["items"]}
    assert "external_source" in reasons
