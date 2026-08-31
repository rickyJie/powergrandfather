"""ClaudeAdapter sync-method tests (memory / mcp / skills).

CLI shell-outs are patched via `run_cli`; only the argv shape + returncode
interpretation are exercised. Real end-to-end goes to integration tests
(kept out of the unit suite so we don't need a real claude binary).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from csm.backends.base import Capability
from csm.backends.claude.adapter import ClaudeAdapter
from csm.modules.sync.cli_runner import CLIResult


def _ok(stdout: str = "") -> CLIResult:
    return CLIResult(
        argv=("claude",), returncode=0, stdout=stdout,
        stderr="", duration_ms=1, timed_out=False,
    )


def _fail(stderr: str = "boom", rc: int = 1) -> CLIResult:
    return CLIResult(
        argv=("claude",), returncode=rc, stdout="",
        stderr=stderr, duration_ms=1, timed_out=False,
    )


@pytest.fixture
def sandboxed_claude(tmp_path: Path, monkeypatch):
    """ClaudeAdapter whose home_dir() lives under tmp_path."""
    monkeypatch.setattr(
        "csm.core.paths.claude_home", lambda: tmp_path,
    )
    return ClaudeAdapter()


# ---------------------------------------------------------------------- memory


def test_memory_paths_user_scope(sandboxed_claude, tmp_path):
    paths = sandboxed_claude.memory_paths("user")
    assert paths == [tmp_path / "CLAUDE.md"]


def test_memory_paths_project_scope_returns_empty(sandboxed_claude):
    assert sandboxed_claude.memory_paths("project") == []


def test_read_memory_missing_returns_empty(sandboxed_claude, tmp_path):
    assert sandboxed_claude.read_memory(tmp_path / "nope.md") == ""


def test_read_memory_returns_utf8(sandboxed_claude, tmp_path):
    p = tmp_path / "x.md"
    p.write_text("héllo", encoding="utf-8")
    assert sandboxed_claude.read_memory(p) == "héllo"


def test_write_memory_marker_block_appends_when_missing(sandboxed_claude, tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# heading\n")
    sandboxed_claude.write_memory_marker_block(target, "lint", "rules body")
    text = target.read_text()
    assert "csm:start id=lint" in text
    assert "rules body" in text
    assert "# heading" in text


def test_write_memory_marker_block_replaces_in_place(sandboxed_claude, tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "# heading\n\n"
        "<!-- csm:start id=lint -->\n"
        "old\n"
        "<!-- csm:end id=lint -->\n"
    )
    sandboxed_claude.write_memory_marker_block(target, "lint", "new-body")
    text = target.read_text()
    assert "new-body" in text
    assert "old" not in text


# ------------------------------------------------------------------------- mcp


@pytest.mark.asyncio
async def test_mcp_add_stdio_shape(sandboxed_claude):
    """Stdio transport: argv includes --command and forwards args after --."""
    captured = {}

    async def fake_run_cli(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _ok()

    with patch("csm.backends.claude.adapter.run_cli", side_effect=fake_run_cli):
        # short-circuit mcp_list to empty so mcp_add proceeds
        with patch.object(ClaudeAdapter, "mcp_list", new=AsyncMock(return_value=[])):
            r = await sandboxed_claude.mcp_add(
                "slack", transport="stdio", command="mcp-slack",
                args=["--workspace", "eng"],
                env={"SLACK_TOKEN": "x"},
            )
    assert r.ok
    assert captured["argv"] == [
        "claude", "mcp", "add", "slack",
        "--transport", "stdio",
        "--command", "mcp-slack",
        "--", "--workspace", "eng",
    ]
    # env: our custom var is present and merged with os.environ
    assert captured["env"]["SLACK_TOKEN"] == "x"


@pytest.mark.asyncio
async def test_mcp_add_http_requires_url(sandboxed_claude):
    with patch.object(ClaudeAdapter, "mcp_list", new=AsyncMock(return_value=[])):
        with pytest.raises(ValueError, match="url"):
            await sandboxed_claude.mcp_add("x", transport="http")


@pytest.mark.asyncio
async def test_mcp_add_is_idempotent_via_list(sandboxed_claude):
    """When the name already exists, we don't invoke the CLI at all."""
    with patch.object(
        ClaudeAdapter, "mcp_list",
        new=AsyncMock(return_value=[{"name": "slack", "transport": "stdio"}]),
    ):
        with patch("csm.backends.claude.adapter.run_cli") as m_run:
            r = await sandboxed_claude.mcp_add(
                "slack", transport="stdio", command="mcp-slack",
            )
    assert r.ok
    m_run.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_remove_when_absent_is_synthetic_ok(sandboxed_claude):
    with patch.object(ClaudeAdapter, "mcp_list", new=AsyncMock(return_value=[])):
        with patch("csm.backends.claude.adapter.run_cli") as m_run:
            r = await sandboxed_claude.mcp_remove("nothing")
    assert r.ok
    m_run.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_remove_when_present_calls_cli(sandboxed_claude):
    with patch.object(
        ClaudeAdapter, "mcp_list",
        new=AsyncMock(return_value=[{"name": "slack"}]),
    ):
        captured = {}

        async def fake_run(argv, **kw):
            captured["argv"] = argv
            return _ok()

        with patch("csm.backends.claude.adapter.run_cli", side_effect=fake_run):
            r = await sandboxed_claude.mcp_remove("slack")
    assert r.ok
    assert captured["argv"] == ["claude", "mcp", "remove", "slack"]


@pytest.mark.asyncio
async def test_mcp_list_parses_output(sandboxed_claude):
    stdout = (
        "slack: stdio · mcp-slack\n"
        "fetch: http · https://example.com/mcp\n"
        "\n"
        "# a comment\n"
    )
    with patch("csm.backends.claude.adapter.run_cli",
               new=AsyncMock(return_value=_ok(stdout=stdout))):
        entries = await sandboxed_claude.mcp_list()
    names = [e["name"] for e in entries]
    assert names == ["slack", "fetch"]
    assert entries[0]["transport"] == "stdio"
    assert entries[1]["transport"] == "http"


@pytest.mark.asyncio
async def test_mcp_list_returns_empty_on_cli_failure(sandboxed_claude):
    with patch("csm.backends.claude.adapter.run_cli",
               new=AsyncMock(return_value=_fail())):
        assert await sandboxed_claude.mcp_list() == []


# ---------------------------------------------------------------------- skills


def test_skills_dir_is_under_home(sandboxed_claude, tmp_path):
    assert sandboxed_claude.skills_dir() == tmp_path / "skills"


def test_list_skills_empty_when_dir_missing(sandboxed_claude):
    assert sandboxed_claude.list_skills() == []


def test_write_and_list_skill_roundtrip(sandboxed_claude, tmp_path):
    body = (
        "---\n"
        "name: hello\n"
        "description: greet the user\n"
        "---\n"
        "\n"
        "Say hi.\n"
    )
    sandboxed_claude.write_simple_skill(
        {"name": "hello", "description": "greet the user", "body_md": body}
    )
    listed = sandboxed_claude.list_skills()
    assert len(listed) == 1
    assert listed[0]["name"] == "hello"
    assert listed[0]["description"] == "greet the user"


def test_write_skill_rejects_bad_name(sandboxed_claude):
    with pytest.raises(ValueError, match="invalid skill name"):
        sandboxed_claude.write_simple_skill(
            {"name": "../evil", "description": "d", "body_md": "---\n---\n"}
        )


def test_remove_skill_is_idempotent(sandboxed_claude):
    sandboxed_claude.remove_skill("does-not-exist")  # no raise


def test_remove_skill_deletes_dir(sandboxed_claude):
    sandboxed_claude.write_simple_skill(
        {"name": "temp", "description": "d",
         "body_md": "---\nname: temp\ndescription: d\n---\n"}
    )
    assert (sandboxed_claude.skills_dir() / "temp").is_dir()
    sandboxed_claude.remove_skill("temp")
    assert not (sandboxed_claude.skills_dir() / "temp").exists()


def test_remove_skill_rejects_traversal(sandboxed_claude):
    with pytest.raises(ValueError):
        sandboxed_claude.remove_skill("../escape")


# ----------------------------------------------------------------- capabilities


def test_static_capabilities_include_sync_memory(sandboxed_claude):
    assert Capability.SYNC_MEMORY in sandboxed_claude.capabilities


@pytest.mark.asyncio
async def test_probe_sync_capabilities_reads_help_returncodes(sandboxed_claude):
    async def fake_probe(cli_name):
        return frozenset({"mcp", "skills"})
    with patch(
        "csm.backends.claude.adapter._probe_helper", side_effect=fake_probe,
    ):
        caps = await sandboxed_claude.probe_sync_capabilities()
    assert Capability.SYNC_MEMORY in caps
    assert Capability.SYNC_MCP in caps
    assert Capability.SYNC_SKILLS in caps


@pytest.mark.asyncio
async def test_probe_sync_capabilities_no_mcp_no_skills(sandboxed_claude):
    async def fake_probe(cli_name):
        return frozenset()
    with patch(
        "csm.backends.claude.adapter._probe_helper", side_effect=fake_probe,
    ):
        caps = await sandboxed_claude.probe_sync_capabilities()
    assert Capability.SYNC_MEMORY in caps
    assert Capability.SYNC_MCP not in caps
    assert Capability.SYNC_SKILLS not in caps
