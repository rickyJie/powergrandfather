"""CodexAdapter sync-method tests — mirrors the claude suite where behaviour
matches; asserts skills stay unsupported (spec §8.4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from csm.backends.base import Capability
from csm.backends.codex.adapter import CodexAdapter
from csm.modules.sync.cli_runner import CLIResult


def _ok(stdout: str = "") -> CLIResult:
    return CLIResult(
        argv=("codex",),
        returncode=0,
        stdout=stdout,
        stderr="",
        duration_ms=1,
        timed_out=False,
    )


@pytest.fixture
def sandboxed_codex(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("csm.core.paths.codex_home", lambda: tmp_path)
    return CodexAdapter()


# ---------------------------------------------------------------------- memory


def test_memory_paths_user_returns_agents_md(sandboxed_codex, tmp_path):
    assert sandboxed_codex.memory_paths("user") == [tmp_path / "AGENTS.md"]


def test_memory_paths_project_returns_empty(sandboxed_codex):
    assert sandboxed_codex.memory_paths("project") == []


def test_write_memory_marker_block_appends(sandboxed_codex, tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# heading\n")
    sandboxed_codex.write_memory_marker_block(target, "policy", "body text")
    text = target.read_text()
    assert "csm:start id=policy" in text
    assert "body text" in text
    assert "# heading" in text


def test_write_memory_marker_block_replaces(sandboxed_codex, tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("<!-- csm:start id=p -->\nold\n<!-- csm:end id=p -->\n")
    sandboxed_codex.write_memory_marker_block(target, "p", "brand-new")
    assert "brand-new" in target.read_text()
    assert "old" not in target.read_text()


# ------------------------------------------------------------------------- mcp


@pytest.mark.asyncio
async def test_mcp_add_argv_shape(sandboxed_codex):
    captured = {}

    async def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _ok()

    with patch("csm.backends.codex.adapter.run_cli", side_effect=fake_run):
        with patch.object(CodexAdapter, "mcp_list", new=AsyncMock(return_value=[])):
            r = await sandboxed_codex.mcp_add(
                "slack",
                transport="stdio",
                command="mcp-slack",
                args=["--w", "eng"],
                env={"TOK": "1"},
            )
    assert r.ok
    assert captured["argv"] == [
        "codex",
        "mcp",
        "add",
        "slack",
        "--transport",
        "stdio",
        "--command",
        "mcp-slack",
        "--",
        "--w",
        "eng",
    ]
    assert captured["env"]["TOK"] == "1"


@pytest.mark.asyncio
async def test_mcp_add_idempotent(sandboxed_codex):
    with patch.object(
        CodexAdapter,
        "mcp_list",
        new=AsyncMock(return_value=[{"name": "slack"}]),
    ):
        with patch("csm.backends.codex.adapter.run_cli") as m_run:
            r = await sandboxed_codex.mcp_add(
                "slack",
                transport="stdio",
                command="mcp-slack",
            )
    assert r.ok
    m_run.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_remove_absent_synthetic_ok(sandboxed_codex):
    with patch.object(CodexAdapter, "mcp_list", new=AsyncMock(return_value=[])):
        with patch("csm.backends.codex.adapter.run_cli") as m_run:
            r = await sandboxed_codex.mcp_remove("nothing")
    assert r.ok
    m_run.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_list_parses_output(sandboxed_codex):
    stdout = "slack: stdio · mcp-slack\nfetch: http · https://x/mcp\n"
    with patch(
        "csm.backends.codex.adapter.run_cli", new=AsyncMock(return_value=_ok(stdout=stdout))
    ):
        entries = await sandboxed_codex.mcp_list()
    assert [e["name"] for e in entries] == ["slack", "fetch"]


# ---------------------------------------------------------------------- skills


def test_skills_dir_points_to_codex_skills(sandboxed_codex, tmp_path):
    """codex-cli 0.145.0 ships ~/.codex/skills/<name>/SKILL.md."""
    assert sandboxed_codex.skills_dir() == tmp_path / "skills"


def test_list_skills_empty_when_no_dir(sandboxed_codex):
    assert sandboxed_codex.list_skills() == []


def test_list_skills_excludes_system_and_lists_user(sandboxed_codex, tmp_path):
    """`.system/` (codex built-ins) and dot-dirs are excluded; user skills listed."""
    skills = tmp_path / "skills"
    # codex built-in — must NOT be adopted into CSM
    (skills / ".system" / "review-agent").mkdir(parents=True)
    (skills / ".system" / "review-agent" / "SKILL.md").write_text(
        "---\ndescription: builtin\n---\n",
        encoding="utf-8",
    )
    # a real user skill — must be listed
    (skills / "my-skill").mkdir(parents=True)
    (skills / "my-skill" / "SKILL.md").write_text(
        "---\ndescription: mine\n---\nbody",
        encoding="utf-8",
    )
    listed = sandboxed_codex.list_skills()
    assert [s["name"] for s in listed] == ["my-skill"]
    assert listed[0]["description"] == "mine"


def test_write_simple_skill_materialises(sandboxed_codex, tmp_path):
    sandboxed_codex.write_simple_skill(
        {"name": "greet", "description": "d", "body_md": "---\ndescription: d\n---\nhi"}
    )
    target = tmp_path / "skills" / "greet" / "SKILL.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "---\ndescription: d\n---\nhi"


def test_write_simple_skill_rejects_bad_name(sandboxed_codex):
    with pytest.raises(ValueError):
        sandboxed_codex.write_simple_skill(
            {"name": "../escape", "description": "d", "body_md": "x"}
        )


def test_remove_skill_deletes_dir(sandboxed_codex, tmp_path):
    sandboxed_codex.write_simple_skill({"name": "gone", "description": "d", "body_md": "x"})
    assert (tmp_path / "skills" / "gone").is_dir()
    sandboxed_codex.remove_skill("gone")
    assert not (tmp_path / "skills" / "gone").exists()


def test_remove_skill_is_noop_when_absent(sandboxed_codex):
    sandboxed_codex.remove_skill("never-existed")  # no raise


# ----------------------------------------------------------------- capabilities


def test_static_capabilities_have_sync_memory(sandboxed_codex):
    assert Capability.SYNC_MEMORY in sandboxed_codex.capabilities


@pytest.mark.asyncio
async def test_probe_yields_sync_skills(sandboxed_codex):
    """codex has no `skill` subcommand, so SYNC_SKILLS is gated on skills_dir()
    being available (a directory convention) — not on the CLI probe helper."""

    async def fake_probe(cli_name):
        return frozenset({"mcp"})  # helper never reports skills for codex

    with patch(
        "csm.backends.codex.adapter._probe_helper",
        side_effect=fake_probe,
    ):
        caps = await sandboxed_codex.probe_sync_capabilities()
    assert Capability.SYNC_MCP in caps
    assert Capability.SYNC_MEMORY in caps
    assert Capability.SYNC_SKILLS in caps
