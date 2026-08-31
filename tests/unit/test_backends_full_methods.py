"""Unit tests for the sync-v2-agent-driven adapter extensions.

Covers the three `*_full` methods added to CLIAdapter Protocol:

- `read_memory_full(scope)` — returns concatenated memory text or None
- `list_mcp_servers_full()` — SyncAgent-facing mcp enumeration
- `list_skills_full()` — like list_skills() plus body_md

Tests use temporary skill dirs / memory files, no real CLI subprocess.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from csm.backends.claude.adapter import ClaudeAdapter
from csm.backends.codex.adapter import CodexAdapter

# ---------------------------------------------------------------------------
# read_memory_full
# ---------------------------------------------------------------------------


def test_claude_read_memory_full_user_returns_string(tmp_path, monkeypatch):
    """user scope: return concatenated CLAUDE.md content."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "CLAUDE.md").write_text("hello world\n", encoding="utf-8")
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    out = a.read_memory_full("user")
    assert out == "hello world\n"


def test_claude_read_memory_full_missing_file_returns_empty(tmp_path, monkeypatch):
    """user scope with no CLAUDE.md → "" (paths exist, files don't)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    assert a.read_memory_full("user") == ""


def test_claude_read_memory_full_project_returns_none(tmp_path, monkeypatch):
    """project scope has no paths → None."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    assert a.read_memory_full("project") is None


def test_codex_read_memory_full_user(tmp_path, monkeypatch):
    """Codex user scope: read AGENTS.md."""
    fake_home = tmp_path / "codex_home"
    fake_home.mkdir()
    (fake_home / "AGENTS.md").write_text("codex agents md\n", encoding="utf-8")
    monkeypatch.setenv("CSM_CODEX_HOME", str(fake_home))
    a = CodexAdapter()
    assert a.read_memory_full("user") == "codex agents md\n"


# ---------------------------------------------------------------------------
# list_skills_full
# ---------------------------------------------------------------------------


def test_claude_list_skills_full_reads_body_md(tmp_path, monkeypatch):
    """Each entry gains a `body_md` field with the SKILL.md text."""
    fake_home = tmp_path / "home"
    (fake_home / "skills" / "my-skill").mkdir(parents=True)
    (fake_home / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: test\n---\nsome body",
        encoding="utf-8",
    )
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    entries = a.list_skills_full()
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "my-skill"
    assert "body_md" in e
    assert "some body" in e["body_md"]
    assert e["body_md"].startswith("---")


def test_claude_list_skills_full_empty_dir_returns_empty(tmp_path, monkeypatch):
    """No skills dir → empty list, not error."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    assert a.list_skills_full() == []


def test_codex_list_skills_full_reads_body_md(tmp_path, monkeypatch):
    """Codex skills live at `~/.codex/skills/<name>/SKILL.md`, same shape as claude.

    This used to assert `== []` on the grounds that "codex has no skills
    concept". That stopped being true (codex-cli 0.145.0 ships a skills tree),
    and because the test never isolated the home dir it was reading the
    developer's real `~/.codex/skills` — so it only passed on a machine that
    happened to have none, and would have passed vacuously in CI forever.
    """
    fake_home = tmp_path / "codex-home"
    (fake_home / "skills" / "my-skill").mkdir(parents=True)
    (fake_home / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: test\n---\nsome body",
        encoding="utf-8",
    )
    monkeypatch.setenv("CSM_CODEX_HOME", str(fake_home))
    a = CodexAdapter()
    entries = a.list_skills_full()
    assert len(entries) == 1
    assert entries[0]["name"] == "my-skill"
    assert "some body" in entries[0]["body_md"]


def test_codex_list_skills_full_excludes_system_dir(tmp_path, monkeypatch):
    """codex ships built-in skills under `.system/`; those are not user skills."""
    fake_home = tmp_path / "codex-home"
    (fake_home / "skills" / ".system" / "builtin").mkdir(parents=True)
    (fake_home / "skills" / ".system" / "builtin" / "SKILL.md").write_text(
        "---\nname: builtin\n---\n", encoding="utf-8"
    )
    monkeypatch.setenv("CSM_CODEX_HOME", str(fake_home))
    a = CodexAdapter()
    assert a.list_skills_full() == []


def test_claude_list_skills_full_skill_without_readable_body(
    tmp_path, monkeypatch
):
    """If SKILL.md becomes unreadable between list_skills() and body read,
    body_md is empty string rather than raising."""
    fake_home = tmp_path / "home"
    skill_dir = fake_home / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: broken\n---\n", encoding="utf-8")
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake_home))
    a = ClaudeAdapter()
    entries = a.list_skills_full()
    assert len(entries) == 1

    # Now delete the file to simulate race.
    (skill_dir / "SKILL.md").unlink()
    entries2 = a.list_skills_full()
    # list_skills() itself skips missing SKILL.md, so 0 entries.
    assert entries2 == []


# ---------------------------------------------------------------------------
# list_mcp_servers_full
# ---------------------------------------------------------------------------


def test_claude_list_mcp_servers_full_delegates_to_mcp_list(monkeypatch):
    """list_mcp_servers_full is an alias of mcp_list for claude."""
    a = ClaudeAdapter()
    fake_entries = [
        {"name": "context7", "transport": "stdio", "raw": "context7: stdio"},
    ]

    async def fake_list():
        return fake_entries

    with patch.object(a, "mcp_list", side_effect=fake_list):
        result = asyncio.run(a.list_mcp_servers_full())
    assert result == fake_entries


def test_codex_list_mcp_servers_full_returns_empty_on_cli_fail(monkeypatch):
    """When `codex mcp list` fails, both mcp_list and *_full return []."""
    a = CodexAdapter()

    async def fake_list():
        return []

    with patch.object(a, "mcp_list", side_effect=fake_list):
        assert asyncio.run(a.list_mcp_servers_full()) == []


# ---------------------------------------------------------------------------
# STABLE_MCP_KEYS v7.1 contract check
# ---------------------------------------------------------------------------


def test_mcp_list_result_shape_includes_raw_but_stable_subset_is_name_transport():
    """v7.1: `raw` is included in mcp_list output but MUST be excluded
    from hashing. The stable subset for sync hashing is (name, transport).

    This test locks in the CLI-parsing shape; the actual hash consumer
    lives in sync/sentinels.py::STABLE_MCP_KEYS (added in Phase 2c).
    """
    # We only assert shape here — the sentinel module implements the
    # STABLE_MCP_KEYS constant separately.
    from csm.backends.claude.adapter import ClaudeAdapter as _CA
    a = _CA()
    fake_entries = [
        {"name": "srv", "transport": "http", "raw": "srv: http (v1.2)"},
    ]

    async def fake_list():
        return fake_entries

    with patch.object(a, "mcp_list", side_effect=fake_list):
        result = asyncio.run(a.list_mcp_servers_full())
    e = result[0]
    assert "name" in e and "transport" in e
    # `raw` MAY be present; downstream must not hash it.
    assert "raw" in e
