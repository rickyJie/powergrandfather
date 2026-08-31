"""Adapter idempotency audit — enforces the sync v2 contract.

See docs/backends/adapter_idempotency_contract.md for the full spec.

Every adapter that declares SYNC_MEMORY / SYNC_MCP / SYNC_SKILLS MUST
make its mutating methods idempotent: two consecutive calls with the
same arguments produce the same observable end state as one call.

If sync v2's fanout_ledger crash-recovery replays a completed fanout,
these adapters get called with identical args a second time — any
non-idempotent behaviour would produce duplicate blocks / stacked
entries / write errors.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from csm.backends.claude.adapter import ClaudeAdapter
from csm.backends.codex.adapter import CodexAdapter
from csm.modules.sync.errors import ExternalSkillSource

# Both filesystem-convention skill adapters must satisfy the same contract.
# Parametrised rather than duplicated so a third adapter is one line.
_SKILL_ADAPTERS = [
    pytest.param(ClaudeAdapter, "CSM_CLAUDE_HOME", id="claude"),
    pytest.param(CodexAdapter, "CSM_CODEX_HOME", id="codex"),
]

# ---------------------------------------------------------------------------
# ClaudeAdapter — write_memory_marker_block
# ---------------------------------------------------------------------------


def test_claude_write_memory_marker_block_twice_same_body_is_no_op(
    tmp_path, monkeypatch,
):
    """Second call with identical body → file byte-identical to one call."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    memfile = fake_home / "CLAUDE.md"

    a.write_memory_marker_block(memfile, "no-sudo", "Do not sudo.")
    after_first = memfile.read_bytes()

    a.write_memory_marker_block(memfile, "no-sudo", "Do not sudo.")
    after_second = memfile.read_bytes()

    assert after_first == after_second


def test_claude_write_memory_marker_block_twice_different_body_overwrites(
    tmp_path, monkeypatch,
):
    """Second call with different body → overwrites in place, no dup."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    memfile = fake_home / "CLAUDE.md"

    a.write_memory_marker_block(memfile, "id-x", "version 1")
    a.write_memory_marker_block(memfile, "id-x", "version 2")

    text = memfile.read_text()
    # Only one marker block per id — 'version 1' MUST NOT survive.
    assert text.count("csm:start id=id-x") == 1
    assert "version 1" not in text
    assert "version 2" in text


def test_claude_write_memory_marker_block_different_ids_coexist(
    tmp_path, monkeypatch,
):
    """Different marker ids create separate blocks (no cross-overwrite)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    memfile = fake_home / "CLAUDE.md"

    a.write_memory_marker_block(memfile, "id-a", "body A")
    a.write_memory_marker_block(memfile, "id-b", "body B")

    text = memfile.read_text()
    assert "csm:start id=id-a" in text
    assert "csm:start id=id-b" in text
    assert "body A" in text
    assert "body B" in text


# ---------------------------------------------------------------------------
# ClaudeAdapter — write_simple_skill
# ---------------------------------------------------------------------------


def test_claude_write_simple_skill_twice_same_body_is_byte_identical(
    tmp_path, monkeypatch,
):
    """SKILL.md content stays byte-identical across repeat writes."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    spec = {
        "name": "mytest-skill",
        "description": "a test skill",
        "body_md": (
            "---\nname: mytest-skill\ndescription: a test skill\n---\n"
            "\ncontent goes here"
        ),
    }
    a.write_simple_skill(spec)
    target = fake_home / "skills" / "mytest-skill" / "SKILL.md"
    first = target.read_bytes()
    a.write_simple_skill(spec)
    second = target.read_bytes()
    assert first == second


def test_claude_write_simple_skill_different_body_overwrites(
    tmp_path, monkeypatch,
):
    """Different body → last write wins (no append/concatenate)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    a.write_simple_skill({
        "name": "overwrite-me",
        "description": "d",
        "body_md": "---\nname: overwrite-me\n---\nversion 1",
    })
    a.write_simple_skill({
        "name": "overwrite-me",
        "description": "d",
        "body_md": "---\nname: overwrite-me\n---\nversion 2",
    })
    target = fake_home / "skills" / "overwrite-me" / "SKILL.md"
    text = target.read_text()
    assert "version 1" not in text
    assert "version 2" in text


# ---------------------------------------------------------------------------
# ClaudeAdapter / CodexAdapter — write_skill_bundle
# ---------------------------------------------------------------------------


def _bundle_spec():
    return {
        "name": "bundled-skill",
        "description": "has helpers",
        "body_md": "---\nname: bundled-skill\n---\nrun ./scripts/go.py\n",
        "files": [
            {"rel_path": "scripts/go.py", "content": b"#!/usr/bin/env python3\n",
             "mode": 0o755},
            {"rel_path": "references/notes.md", "content": b"# notes\n", "mode": 0o644},
        ],
        "prune": None,
    }


def _tree_snapshot(root):
    """(rel_path, bytes, mode) for every file under `root`, sorted."""
    return sorted(
        (p.relative_to(root).as_posix(), p.read_bytes(), p.stat().st_mode & 0o7777)
        for p in root.rglob("*")
        if p.is_file()
    )


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_write_skill_bundle_twice_is_byte_identical(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """The whole directory tree — content AND permission bits — is stable
    across a replayed fanout, not just SKILL.md."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(home_env, str(fake_home))
    a = adapter_cls()

    a.write_skill_bundle(_bundle_spec())
    first = _tree_snapshot(fake_home / "skills" / "bundled-skill")
    a.write_skill_bundle(_bundle_spec())
    second = _tree_snapshot(fake_home / "skills" / "bundled-skill")

    assert first == second
    assert [rel for rel, _, _ in first] == [
        "SKILL.md", "references/notes.md", "scripts/go.py",
    ]


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_write_skill_bundle_preserves_executable_bit(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """A helper script that arrives non-executable is as broken as a missing
    one — atomic_write creates its temp file 0600, so the chmod is load-bearing."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(home_env, str(fake_home))
    a = adapter_cls()

    a.write_skill_bundle(_bundle_spec())
    script = fake_home / "skills" / "bundled-skill" / "scripts" / "go.py"

    assert script.stat().st_mode & 0o777 == 0o755
    assert os.access(script, os.X_OK)


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_write_skill_bundle_prunes_only_what_it_wrote(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """Dropping a file from the bundle removes it from the target — but a
    file the user put there by hand is left alone."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(home_env, str(fake_home))
    a = adapter_cls()
    skill_dir = fake_home / "skills" / "bundled-skill"

    a.write_skill_bundle(_bundle_spec())
    handmade = skill_dir / "my-own-notes.md"
    handmade.write_text("mine")

    spec = _bundle_spec()
    spec["files"] = [f for f in spec["files"] if f["rel_path"] != "scripts/go.py"]
    spec["prune"] = {"scripts/go.py": "sha", "references/notes.md": "sha"}
    result = a.write_skill_bundle(spec)

    assert result["pruned"] == ["scripts/go.py"]
    assert not (skill_dir / "scripts" / "go.py").exists()
    assert (skill_dir / "references" / "notes.md").exists()  # still in the bundle
    assert handmade.read_text() == "mine"  # never in a manifest → untouched


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_write_skill_bundle_refuses_symlinked_skill_dir(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """Most real skill dirs are symlinks into a skill-book repo. Writing
    through one would silently edit the user's git working tree."""
    fake_home = tmp_path / "home"
    (fake_home / "skills").mkdir(parents=True)
    monkeypatch.setenv(home_env, str(fake_home))
    external = tmp_path / "skill-book" / "bundled-skill"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: bundled-skill\n---\noriginal\n")
    os.symlink(external, fake_home / "skills" / "bundled-skill")

    a = adapter_cls()
    with pytest.raises(ExternalSkillSource):
        a.write_skill_bundle(_bundle_spec())

    # The repo's copy is untouched.
    assert "original" in (external / "SKILL.md").read_text()


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_remove_skill_refuses_symlinked_skill_dir(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """Same reasoning as the write guard — rmtree follows symlinks."""
    fake_home = tmp_path / "home"
    (fake_home / "skills").mkdir(parents=True)
    monkeypatch.setenv(home_env, str(fake_home))
    external = tmp_path / "skill-book" / "linked-skill"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: linked-skill\n---\n")
    os.symlink(external, fake_home / "skills" / "linked-skill")

    a = adapter_cls()
    with pytest.raises(ExternalSkillSource):
        a.remove_skill("linked-skill")

    assert (external / "SKILL.md").exists()


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_write_skill_bundle_rejects_path_traversal(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """rel_path is re-validated at write time — the DB is reachable via the API."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(home_env, str(fake_home))
    a = adapter_cls()

    spec = _bundle_spec()
    spec["files"] = [{"rel_path": "../../escaped.txt", "content": b"x", "mode": 0o644}]
    with pytest.raises(ValueError):
        a.write_skill_bundle(spec)

    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize("adapter_cls,home_env", _SKILL_ADAPTERS)
def test_write_skill_bundle_read_round_trip(
    adapter_cls, home_env, tmp_path, monkeypatch,
):
    """What `read_skill_bundle` returns is what `write_skill_bundle` accepts."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(home_env, str(fake_home))
    a = adapter_cls()
    a.write_skill_bundle(_bundle_spec())

    got = a.read_skill_bundle("bundled-skill")
    assert got is not None
    assert {f.rel_path for f in got["files"]} == {
        "scripts/go.py", "references/notes.md",
    }
    assert {f.rel_path: f.mode for f in got["files"]}["scripts/go.py"] == 0o755
    assert got["body_md"] == _bundle_spec()["body_md"]
    assert a.read_skill_bundle("never-written") is None


def test_claude_remove_skill_absent_is_noop(tmp_path, monkeypatch):
    """Removing a non-existent skill must not error."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    # Should not raise
    a.remove_skill("never-existed")


def test_claude_remove_skill_refuses_path_traversal(tmp_path, monkeypatch):
    """Path-traversal guard: `../..` in name is rejected before deletion."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    # ValueError specifically — a blind `Exception` would also pass if the
    # guard never ran and something else (AttributeError, OSError) blew up.
    with pytest.raises(ValueError):
        a.remove_skill("../../etc")


# ---------------------------------------------------------------------------
# ClaudeAdapter — mcp_add (real CLI mocked)
# ---------------------------------------------------------------------------


def test_claude_mcp_add_twice_same_shape_short_circuits(monkeypatch):
    """mcp_add called twice with same name → second is a no-op (rc=0)."""
    a = ClaudeAdapter()

    # After first add, mcp_list returns the entry.
    added_state: list[dict] = []

    async def fake_list():
        return list(added_state)

    async def fake_run_cli(argv, timeout=None, env=None):
        # Only fires on the first actual add call.
        from csm.modules.sync.cli_runner import CLIResult
        added_state.append({"name": argv[3], "transport": "stdio",
                            "raw": f"{argv[3]}: stdio"})
        return CLIResult(argv=tuple(argv), returncode=0, stdout="",
                         stderr="", duration_ms=1, timed_out=False)

    monkeypatch.setattr(a, "mcp_list", fake_list)
    monkeypatch.setattr(
        "csm.backends.claude.adapter.run_cli", fake_run_cli,
    )

    r1 = asyncio.run(a.mcp_add(
        "myserver", transport="stdio", command="node srv.js",
    ))
    r2 = asyncio.run(a.mcp_add(
        "myserver", transport="stdio", command="node srv.js",
    ))
    assert r1.returncode == 0
    assert r2.returncode == 0
    # Only one entry ever added.
    assert len(added_state) == 1


def test_claude_mcp_remove_absent_returns_synthetic_ok(monkeypatch):
    """Removing an absent mcp entry returns rc=0 without hitting CLI."""
    a = ClaudeAdapter()

    async def fake_list():
        return []

    monkeypatch.setattr(a, "mcp_list", fake_list)

    # If run_cli were called, we'd know because of this side effect.
    called = []

    async def fake_run_cli(argv, timeout=None, env=None):
        called.append(argv)
        from csm.modules.sync.cli_runner import CLIResult
        return CLIResult(argv=tuple(argv), returncode=1, stdout="",
                         stderr="not found", duration_ms=1, timed_out=False)

    monkeypatch.setattr(
        "csm.backends.claude.adapter.run_cli", fake_run_cli,
    )

    r = asyncio.run(a.mcp_remove("nonexistent"))
    assert r.returncode == 0  # synthetic
    assert called == []  # CLI never invoked
