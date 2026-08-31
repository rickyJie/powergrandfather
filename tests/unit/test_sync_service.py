"""SyncService + DriftPoller unit tests.

Uses an in-memory SQLite + a fake adapter that implements the full sync
Protocol surface. Adapter shell-outs are pure Python — no subprocess is
launched.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from csm.backends.base import Capability, MarkerSyntax
from csm.backends.registry import AdapterRegistry
from csm.models import Base
from csm.models.drift_record import DriftRecord
from csm.models.instruction import Instruction
from csm.models.mcp_server import McpServer
from csm.models.skill import Skill
from csm.models.sync_activity import SyncActivity
from csm.models.sync_common import DriftReason, SyncModule, SyncStatus
from csm.models.sync_config import SyncConfig
from csm.modules.sync.bundle import BundleFile, bundle_hash
from csm.modules.sync.cli_runner import CLIResult
from csm.modules.sync.service import DriftPoller, SyncService
from csm.modules.sync.skill_store import replace_skill_files
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ------------------------------------------------------------------- fake adapter


class FakeSyncAdapter:
    """Stand-in that implements every SYNC_* method deterministically."""

    def __init__(
        self,
        name: str,
        *,
        home: Path,
        capabilities: frozenset[Capability] | None = None,
        support_skills: bool = True,
    ) -> None:
        self.name = name
        self.display_name = name
        self.icon = name[:1].upper()
        self.color = "#000"
        self.capabilities = capabilities or frozenset({
            Capability.SYNC_MEMORY, Capability.SYNC_MCP, Capability.SYNC_SKILLS,
        })
        self._home = home
        self._support_skills = support_skills
        self._mcp: dict[str, dict] = {}
        self._memory_writes: list[tuple[str, str]] = []  # (marker_id, body)

    # ---- identity / env
    def home_dir(self) -> Path:
        return self._home

    def default_home_name(self) -> str:
        return f".{self.name}"

    def default_argv(self) -> str: return self.name
    def flags_schema(self): return []
    def auth_file(self): return None
    def probe(self): return None
    def pre_spawn_session_id(self, cwd): return None
    def post_spawn_bind(self, sid, cwd): return None
    def build_argv(self, *a, **kw): return None
    def artifact_root(self): return self._home
    def artifact_glob(self): return "*"
    def scan_events(self): return []
    def snapshot(self): return {}
    def restore(self, snap): pass
    def tail_states(self): return []
    def take_newly_seen(self): return set()
    def install_hooks(self, root, url): pass

    # ---- memory
    def memory_paths(self, scope):
        return [self._home / "MEM.md"] if scope == "user" else []

    def read_memory(self, path):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write_memory_marker_block(self, path, marker_id, body):
        from csm.modules.sync.marker_block import replace_or_append_marker_block
        current = self.read_memory(path)
        updated = replace_or_append_marker_block(
            current, self.marker_syntax(), marker_id, body,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        self._memory_writes.append((marker_id, body))

    # ---- mcp
    async def mcp_add(self, name, *, transport, command=None, args=None,
                      url=None, env=None):
        if name in self._mcp:
            return CLIResult(argv=(), returncode=0, stdout="", stderr="",
                             duration_ms=1, timed_out=False)
        self._mcp[name] = {"name": name, "transport": transport,
                           "command": command, "url": url}
        return CLIResult(argv=(), returncode=0, stdout="", stderr="",
                         duration_ms=1, timed_out=False)

    async def mcp_remove(self, name):
        if name not in self._mcp:
            return CLIResult(argv=(), returncode=0, stdout="", stderr="",
                             duration_ms=1, timed_out=False)
        del self._mcp[name]
        return CLIResult(argv=(), returncode=0, stdout="", stderr="",
                         duration_ms=1, timed_out=False)

    async def mcp_list(self):
        return [dict(v) for v in self._mcp.values()]

    # ---- skills
    def skills_dir(self):
        return (self._home / "skills") if self._support_skills else None

    def list_skills(self):
        d = self.skills_dir()
        if d is None or not d.is_dir():
            return []
        return [
            {"name": p.name, "path": str(p / "SKILL.md"),
             "description": ""}
            for p in d.iterdir() if p.is_dir()
        ]

    def write_simple_skill(self, spec):
        self.write_skill_bundle({**spec, "files": [], "prune": None})

    def write_skill_bundle(self, spec):
        """Deliberately an INDEPENDENT implementation of the bundle write,
        not a call into `_skill_fs` — the adapter idempotency contract uses
        this fake as a second opinion, which it can't be if it shares code
        with the thing under test."""
        if not self._support_skills:
            raise NotImplementedError("skills unsupported")
        d = self.skills_dir() / spec["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(spec["body_md"], encoding="utf-8")
        written = ["SKILL.md"]
        keep = set()
        for f in spec.get("files") or []:
            rel = f["rel_path"] if isinstance(f, dict) else f.rel_path
            content = f["content"] if isinstance(f, dict) else f.content
            mode = (f.get("mode", 0o644) if isinstance(f, dict) else f.mode)
            target = d / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
            target.chmod(mode & 0o7777)
            written.append(rel)
            keep.add(rel)
        pruned = []
        for rel in sorted(dict(spec.get("prune") or {})):
            if rel in keep or rel == "SKILL.md":
                continue
            t = d / rel
            if t.is_file():
                t.unlink()
                pruned.append(rel)
        return {"written": written, "pruned": pruned}

    def read_skill_bundle(self, name):
        if not self._support_skills:
            return None
        d = self.skills_dir() / name
        md = d / "SKILL.md"
        if not md.is_file():
            return None
        files = []
        for p in sorted(d.rglob("*")):
            if not p.is_file() or p.name == "SKILL.md":
                continue
            files.append(BundleFile(
                rel_path=p.relative_to(d).as_posix(),
                content=p.read_bytes(),
                mode=p.stat().st_mode & 0o7777,
            ))
        return {
            "name": name,
            "description": "",
            "body_md": md.read_text(encoding="utf-8"),
            "files": files,
            "skipped": [],
        }

    def remove_skill(self, name):
        if not self._support_skills:
            return
        d = self.skills_dir() / name
        if d.is_dir():
            import shutil
            shutil.rmtree(d)

    def marker_syntax(self):
        return MarkerSyntax.html_comment()

    async def probe_sync_capabilities(self):
        return self.capabilities

    # ---- *_full readers (used by A→B migration + agent-tick collect) ----
    def read_memory_full(self, scope):
        paths = self.memory_paths(scope)
        if not paths:
            return None
        return "\n\n".join(self.read_memory(p) for p in paths)

    def list_skills_full(self):
        out = []
        for e in self.list_skills():
            body = ""
            try:
                body = Path(e["path"]).read_text(encoding="utf-8")
            except (OSError, KeyError):
                pass
            b = self.read_skill_bundle(e["name"]) or {}
            files = b.get("files") or []
            out.append({
                **e,
                "body_md": body,
                "file_count": len(files),
                "bundle_hash": bundle_hash(body, files),
            })
        return out

    async def list_mcp_servers_full(self):
        return await self.mcp_list()


# --------------------------------------------------------------- fixtures


@pytest.fixture
async def setup(tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_home.mkdir()
    codex_home.mkdir()

    registry = AdapterRegistry([
        FakeSyncAdapter("claude", home=claude_home),
        FakeSyncAdapter("codex", home=codex_home),
    ])

    # Seed enrollment for all three modules.
    async with sm() as session:
        for mod in SyncModule:
            session.add(SyncConfig(
                module=mod.value,
                enrolled_agents=["claude", "codex"],
                poll_interval_sec=30, enabled=True,
            ))
        await session.commit()

    svc = SyncService(sessionmaker=sm, adapter_registry=registry)
    yield svc, sm, registry, {"claude": claude_home, "codex": codex_home}
    await engine.dispose()
    os.unlink(db_path)


# --------------------------------------------------------------- memory tests


async def test_sync_instruction_writes_to_all_enrolled(setup):
    svc, sm, reg, homes = setup
    async with sm() as session:
        ins = Instruction(
            name="rules", title="Lint rules", body="use ruff",
            share_scope=["claude", "codex"], priority=0,
        )
        session.add(ins)
        await session.commit()
        await session.refresh(ins)

    results = await svc.sync_instruction(ins)
    assert {r.agent for r in results} == {"claude", "codex"}
    assert all(r.status is SyncStatus.OK for r in results)

    for _name, home in homes.items():
        text = (home / "MEM.md").read_text()
        assert "csm:start id=rules" in text
        assert "use ruff" in text

    async with sm() as session:
        acts = (await session.execute(select(SyncActivity))).scalars().all()
    assert {a.agent for a in acts} == {"claude", "codex"}
    assert all(a.action == "add" and a.status == "ok" for a in acts)


async def test_sync_instruction_respects_share_scope(setup):
    """share_scope=['claude'] must skip codex even if enrolled."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        ins = Instruction(name="only-claude", title="x", body="body",
                          share_scope=["claude"], priority=0)
        session.add(ins)
        await session.commit()
        await session.refresh(ins)

    results = await svc.sync_instruction(ins)
    assert {r.agent for r in results} == {"claude"}
    assert not (homes["codex"] / "MEM.md").exists()


async def test_remove_instruction_strips_marker(setup):
    svc, sm, reg, homes = setup
    async with sm() as session:
        ins = Instruction(name="rules", title="x", body="b",
                          share_scope=["claude"], priority=0)
        session.add(ins)
        await session.commit()
        await session.refresh(ins)
    await svc.sync_instruction(ins)
    assert "csm:start id=rules" in (homes["claude"] / "MEM.md").read_text()

    results = await svc.remove_instruction(ins)
    assert results[0].status is SyncStatus.OK
    assert "csm:start id=rules" not in (homes["claude"] / "MEM.md").read_text()


# --------------------------------------------------------------- mcp tests


async def test_sync_mcp_server_writes_and_is_idempotent(setup):
    svc, sm, reg, homes = setup
    async with sm() as session:
        srv = McpServer(name="slack", transport="stdio", command="mcp-slack",
                        args_json=[], url=None, env_json={},
                        enabled_for=["claude", "codex"])
        session.add(srv)
        await session.commit()
        await session.refresh(srv)

    results = await svc.sync_mcp_server(srv)
    assert all(r.status is SyncStatus.OK for r in results)
    for name in ("claude", "codex"):
        adapter = reg.get(name)
        assert "slack" in adapter._mcp

    # Second call is idempotent (fake adapter no-ops when name present).
    results = await svc.sync_mcp_server(srv)
    assert all(r.status is SyncStatus.OK for r in results)


async def test_sync_mcp_server_env_undefined_returns_error(setup):
    svc, sm, reg, homes = setup
    async with sm() as session:
        srv = McpServer(name="s", transport="stdio", command="c",
                        args_json=[], url=None,
                        env_json={"SECRET": "${UNDEFINED_VAR_XYZ_9999}"},
                        enabled_for=["claude"])
        session.add(srv)
        await session.commit()
        await session.refresh(srv)

    # Ensure the var really isn't set.
    os.environ.pop("UNDEFINED_VAR_XYZ_9999", None)

    results = await svc.sync_mcp_server(srv)
    assert results[0].status is SyncStatus.ERROR
    assert "UNDEFINED_VAR_XYZ_9999" in (results[0].detail or "")


async def test_remove_mcp_server_calls_adapter(setup):
    svc, sm, reg, _ = setup
    async with sm() as session:
        srv = McpServer(name="slack", transport="stdio", command="c",
                        args_json=[], url=None, env_json={},
                        enabled_for=["claude"])
        session.add(srv)
        await session.commit()
        await session.refresh(srv)
    await svc.sync_mcp_server(srv)
    assert "slack" in reg.get("claude")._mcp
    await svc.remove_mcp_server(srv)
    assert "slack" not in reg.get("claude")._mcp


# --------------------------------------------------------------- skill tests


async def test_sync_skill_writes_to_enrolled(setup):
    svc, sm, reg, homes = setup
    async with sm() as session:
        sk = Skill(name="grep", description="quick grep",
                   body_md="---\nname: grep\ndescription: quick grep\n---\n",
                   share_scope=["claude", "codex"])
        session.add(sk)
        await session.commit()
        await session.refresh(sk)

    results = await svc.sync_skill(sk)
    assert all(r.status is SyncStatus.OK for r in results)
    for _name, home in homes.items():
        assert (home / "skills" / "grep" / "SKILL.md").is_file()


async def test_sync_skill_materialises_the_whole_bundle(setup):
    """The regression this whole subsystem exists for: a skill whose
    SKILL.md says `run ./scripts/go.py` must arrive WITH scripts/go.py."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        sk = Skill(name="bundled", description="has helpers",
                   body_md="---\nname: bundled\n---\nrun ./scripts/go.py\n",
                   share_scope=["claude", "codex"])
        session.add(sk)
        await session.flush()
        await replace_skill_files(session, sk, [
            BundleFile(rel_path="scripts/go.py", content=b"#!/usr/bin/env python3\n",
                       mode=0o755),
            BundleFile(rel_path="references/notes.md", content=b"# notes\n", mode=0o644),
        ])
        await session.commit()
        await session.refresh(sk)

    results = await svc.sync_skill(sk)
    assert all(r.status is SyncStatus.OK for r in results)
    for home in homes.values():
        d = home / "skills" / "bundled"
        assert (d / "SKILL.md").is_file()
        assert (d / "scripts" / "go.py").read_bytes() == b"#!/usr/bin/env python3\n"
        assert (d / "scripts" / "go.py").stat().st_mode & 0o777 == 0o755
        assert (d / "references" / "notes.md").is_file()


async def test_sync_skill_records_the_manifest_per_agent(setup):
    """`last_synced_files` is what the next push prunes against — without it
    a removed helper would linger on the target forever."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        sk = Skill(name="manifested", description="d",
                   body_md="---\nname: manifested\n---\n",
                   share_scope=["claude", "codex"])
        session.add(sk)
        await session.flush()
        await replace_skill_files(session, sk, [
            BundleFile(rel_path="a.md", content=b"a", mode=0o644),
        ])
        await session.commit()
        await session.refresh(sk)

    await svc.sync_skill(sk)

    async with sm() as session:
        row = await session.get(Skill, sk.id)
        assert set(row.last_synced_files) == {"claude", "codex"}
        assert set(row.last_synced_files["claude"]) == {"a.md"}


async def test_second_sync_prunes_a_removed_bundle_file(setup):
    """Drop a file from the bundle → it disappears from every agent."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        sk = Skill(name="shrinking", description="d",
                   body_md="---\nname: shrinking\n---\n",
                   share_scope=["claude"])
        session.add(sk)
        await session.flush()
        await replace_skill_files(session, sk, [
            BundleFile(rel_path="keep.md", content=b"keep", mode=0o644),
            BundleFile(rel_path="drop.md", content=b"drop", mode=0o644),
        ])
        await session.commit()
        await session.refresh(sk)
    await svc.sync_skill(sk)
    assert (homes["claude"] / "skills" / "shrinking" / "drop.md").is_file()

    async with sm() as session:
        row = await session.get(Skill, sk.id)
        await replace_skill_files(session, row, [
            BundleFile(rel_path="keep.md", content=b"keep", mode=0o644),
        ])
        await session.commit()
        await session.refresh(row)
    await svc.sync_skill(row)

    d = homes["claude"] / "skills" / "shrinking"
    assert (d / "keep.md").is_file()
    assert not (d / "drop.md").exists()


async def test_sync_skill_reports_unsupported(setup, tmp_path):
    svc, sm, reg, homes = setup
    # Replace codex with a no-skill fake.
    codex_home = homes["codex"]
    reg._by_name["codex"] = FakeSyncAdapter(
        "codex", home=codex_home,
        capabilities=frozenset({Capability.SYNC_MEMORY, Capability.SYNC_MCP}),
        support_skills=False,
    )
    async with sm() as session:
        sk = Skill(name="hi", description="d",
                   body_md="---\nname: hi\ndescription: d\n---\n",
                   share_scope=["claude", "codex"])
        session.add(sk)
        await session.commit()
        await session.refresh(sk)

    results = await svc.sync_skill(sk)
    by_agent = {r.agent: r for r in results}
    assert by_agent["claude"].status is SyncStatus.OK
    assert by_agent["codex"].status is SyncStatus.UNSUPPORTED


# --------------------------------------------------------------- drift poller


async def test_drift_poller_detects_missing_marker(setup):
    svc, sm, reg, homes = setup
    async with sm() as session:
        ins = Instruction(name="missing-block", title="x", body="expected body",
                          share_scope=["claude"], priority=0)
        session.add(ins)
        await session.commit()
        await session.refresh(ins)

    poller = DriftPoller(sessionmaker=sm, adapter_registry=reg,
                         sync_service=svc, tick_interval_sec=60.0)
    await poller.tick_once()

    async with sm() as session:
        drifts = (await session.execute(select(DriftRecord))).scalars().all()
    memory_drifts = [d for d in drifts if d.module == SyncModule.MEMORY.value]
    assert len(memory_drifts) >= 1
    assert any(d.reason == DriftReason.MISSING.value for d in memory_drifts)


async def test_drift_poller_detects_external_edit(setup):
    """Marker present but body differs → EXTERNAL_EDIT."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        ins = Instruction(name="rules", title="x", body="CANONICAL",
                          share_scope=["claude"], priority=0)
        session.add(ins)
        await session.commit()
        await session.refresh(ins)
    await svc.sync_instruction(ins)

    # User hand-edits the block.
    mem = (homes["claude"] / "MEM.md")
    text = mem.read_text().replace("CANONICAL", "USER-EDITED")
    mem.write_text(text)

    poller = DriftPoller(sessionmaker=sm, adapter_registry=reg,
                         sync_service=svc, tick_interval_sec=60.0)
    await poller.tick_once()

    async with sm() as session:
        drifts = (await session.execute(select(DriftRecord))).scalars().all()
    assert any(
        d.reason == DriftReason.EXTERNAL_EDIT.value and d.agent == "claude"
        for d in drifts
    )


async def test_drift_poller_detects_missing_mcp_entry(setup):
    svc, sm, reg, _ = setup
    async with sm() as session:
        srv = McpServer(name="slack", transport="stdio", command="c",
                        args_json=[], url=None, env_json={},
                        enabled_for=["claude"])
        session.add(srv)
        await session.commit()
        await session.refresh(srv)
    # Deliberately don't sync — so CLI-side never has it.
    poller = DriftPoller(sessionmaker=sm, adapter_registry=reg,
                         sync_service=svc, tick_interval_sec=60.0)
    await poller.tick_once()

    async with sm() as session:
        drifts = (await session.execute(select(DriftRecord))).scalars().all()
    assert any(
        d.module == SyncModule.MCP.value
        and d.reason == DriftReason.MISSING.value
        for d in drifts
    )


async def test_drift_poller_detects_a_deleted_bundle_file(setup):
    """The bug that made this whole thing invisible: the poller used to
    check only that the skill DIRECTORY existed, so a bundle stripped down
    to its SKILL.md polled green forever."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        sk = Skill(name="loseit", description="d",
                   body_md="---\nname: loseit\n---\nrun ./go.py\n",
                   share_scope=["claude"])
        session.add(sk)
        await session.flush()
        await replace_skill_files(session, sk, [
            BundleFile(rel_path="go.py", content=b"print(1)\n", mode=0o755),
        ])
        await session.commit()
        await session.refresh(sk)
    await svc.sync_skill(sk)

    poller = DriftPoller(sessionmaker=sm, adapter_registry=reg,
                         sync_service=svc, tick_interval_sec=60.0)
    await poller.tick_once()
    async with sm() as session:
        assert (await session.execute(select(DriftRecord))).scalars().all() == []

    # Someone deletes the helper out from under us.
    (homes["claude"] / "skills" / "loseit" / "go.py").unlink()
    await poller.tick_once()

    async with sm() as session:
        drifts = (await session.execute(select(DriftRecord))).scalars().all()
    assert any(
        d.module == SyncModule.SKILLS.value
        and d.reason == DriftReason.MISSING.value
        and d.agent == "claude"
        for d in drifts
    ), [(d.module, d.reason) for d in drifts]


async def test_drift_poller_detects_a_stripped_exec_bit(setup):
    """A helper script that lost +x is broken in exactly the way a missing
    one is, so it has to register as drift too."""
    svc, sm, reg, homes = setup
    async with sm() as session:
        sk = Skill(name="chmodded", description="d",
                   body_md="---\nname: chmodded\n---\n",
                   share_scope=["claude"])
        session.add(sk)
        await session.flush()
        await replace_skill_files(session, sk, [
            BundleFile(rel_path="go.py", content=b"print(1)\n", mode=0o755),
        ])
        await session.commit()
        await session.refresh(sk)
    await svc.sync_skill(sk)

    (homes["claude"] / "skills" / "chmodded" / "go.py").chmod(0o644)

    poller = DriftPoller(sessionmaker=sm, adapter_registry=reg,
                         sync_service=svc, tick_interval_sec=60.0)
    await poller.tick_once()

    async with sm() as session:
        drifts = (await session.execute(select(DriftRecord))).scalars().all()
    assert any(d.module == SyncModule.SKILLS.value for d in drifts)


async def test_drift_poller_start_stop_lifecycle(setup):
    svc, sm, reg, _ = setup
    poller = DriftPoller(sessionmaker=sm, adapter_registry=reg,
                         sync_service=svc, tick_interval_sec=0.05)
    await poller.start()
    assert poller._task is not None
    # give it a moment to run at least one tick
    import asyncio as _aio
    await _aio.sleep(0.15)
    await poller.stop()
    assert poller._task is None


# --------------------------------------------------------- envelope helpers


def test_envelope_warnings_flags_non_ok():
    from csm.modules.sync.service import PerAgentResult, envelope_warnings
    results = [
        PerAgentResult(agent="claude", status=SyncStatus.OK),
        PerAgentResult(agent="codex", status=SyncStatus.TIMEOUT,
                       detail="exceeded 10000ms"),
    ]
    w = envelope_warnings(results)
    assert len(w) == 1
    assert "codex" in w[0]
    assert "timeout" in w[0]


# --------------------------------------------------------- A→B migration


async def test_migrate_skills_creates_csm_row_and_pushes_to_target(setup):
    svc, sm, reg, homes = setup
    d = homes["claude"] / "skills" / "greet"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: hi\n---\nbody", encoding="utf-8")

    res = await svc.migrate_agent_to_agent(SyncModule.SKILLS, "claude", "codex")
    assert len(res) == 1
    assert res[0]["name"] == "greet"
    assert res[0]["action"] == "created"
    async with sm() as s:
        row = (await s.execute(
            select(Skill).where(Skill.name == "greet")
        )).scalar_one()
        assert set(row.share_scope) == {"claude", "codex"}
    assert (homes["codex"] / "skills" / "greet" / "SKILL.md").is_file()


async def test_migrate_skills_names_filter(setup):
    svc, sm, reg, homes = setup
    for n in ("a-skill", "b-skill"):
        d = homes["claude"] / "skills" / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\n---\nx", encoding="utf-8")
    res = await svc.migrate_agent_to_agent(
        SyncModule.SKILLS, "claude", "codex", names=["a-skill"],
    )
    assert [r["name"] for r in res] == ["a-skill"]
    assert not (homes["codex"] / "skills" / "b-skill").exists()


async def test_migrate_skills_updates_existing_row(setup):
    svc, sm, reg, homes = setup
    async with sm() as s:
        s.add(Skill(name="greet", description="old", body_md="---\n---\nold",
                    share_scope=["claude"]))
        await s.commit()
    d = homes["claude"] / "skills" / "greet"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\nNEW", encoding="utf-8")
    res = await svc.migrate_agent_to_agent(SyncModule.SKILLS, "claude", "codex")
    assert res[0]["action"] == "updated"
    async with sm() as s:
        row = (await s.execute(
            select(Skill).where(Skill.name == "greet")
        )).scalar_one()
        assert row.body_md == "---\n---\nNEW"
        assert set(row.share_scope) == {"claude", "codex"}


async def test_migrate_memory_packs_single_instruction(setup):
    svc, sm, reg, homes = setup
    (homes["claude"] / "MEM.md").write_text("hello from claude", encoding="utf-8")
    res = await svc.migrate_agent_to_agent(SyncModule.MEMORY, "claude", "codex")
    assert len(res) == 1
    assert res[0]["name"] == "migrated-from-claude"
    async with sm() as s:
        row = (await s.execute(
            select(Instruction).where(Instruction.name == "migrated-from-claude")
        )).scalar_one()
        assert "hello from claude" in row.body
    assert "hello from claude" in (homes["codex"] / "MEM.md").read_text()


async def test_migrate_memory_empty_source_skipped(setup):
    svc, sm, reg, homes = setup
    res = await svc.migrate_agent_to_agent(SyncModule.MEMORY, "claude", "codex")
    assert res[0]["action"] == "skipped"


async def test_migrate_mcp_unsupported(setup):
    svc, sm, reg, homes = setup
    res = await svc.migrate_agent_to_agent(SyncModule.MCP, "claude", "codex")
    assert res[0]["action"] == "unsupported"


async def test_migrate_same_agent_is_error(setup):
    svc, *_ = setup
    res = await svc.migrate_agent_to_agent(SyncModule.SKILLS, "claude", "claude")
    assert res[0]["action"] == "error"


async def test_migrate_target_not_enrolled_is_error(setup):
    svc, sm, reg, homes = setup
    async with sm() as s:
        row = (await s.execute(
            select(SyncConfig).where(SyncConfig.module == "skills")
        )).scalar_one()
        row.enrolled_agents = ["claude"]
        await s.commit()
    res = await svc.migrate_agent_to_agent(SyncModule.SKILLS, "claude", "codex")
    assert res[0]["action"] == "error"
    assert "not enrolled" in res[0]["detail"]
