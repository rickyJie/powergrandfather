"""Unit tests for sync v2 sentinels + realtime read helper (Phase 2c).

Covers:
- DIVERGED sentinel encode/parse round-trip
- 4 sentinel branches of `agent_needs_sync`
- `read_agent_side_body` for memory / mcp / skill resource types
- v7.1 STABLE_MCP_KEYS: hash stable across `raw` field variation
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from csm.backends.base import MarkerSyntax
from csm.modules.sync.sentinels import (
    HASH_SENTINEL_DIVERGED_PREFIX,
    HASH_SENTINEL_UNKNOWN,
    HASH_SENTINEL_UNSUPPORTED,
    STABLE_MCP_KEYS,
    agent_needs_sync,
    make_diverged_sentinel,
    parse_diverged_sentinel,
    read_agent_side_body,
)

# ---------------------------------------------------------------------------
# DIVERGED sentinel encode/decode
# ---------------------------------------------------------------------------


def test_diverged_sentinel_round_trip():
    s = make_diverged_sentinel("abcdef1234")
    assert s == "DIVERGED:abcdef1234"
    assert s.startswith(HASH_SENTINEL_DIVERGED_PREFIX)
    assert parse_diverged_sentinel(s) == "abcdef1234"


@pytest.mark.parametrize("val", [
    None,
    "",
    "plain-hash",
    "UNSUPPORTED",
    "UNKNOWN",
    123,
    ["diverged:x"],
])
def test_parse_diverged_sentinel_returns_none_for_non_diverged(val):
    assert parse_diverged_sentinel(val) is None


# ---------------------------------------------------------------------------
# _agent_needs_sync branches
# ---------------------------------------------------------------------------


def test_agent_needs_sync_hash_match_returns_false():
    need, reason = agent_needs_sync(
        {"claude": "abc"}, "claude", "abc", None,
    )
    assert need is False
    assert reason is None


def test_agent_needs_sync_hash_mismatch_returns_true():
    need, reason = agent_needs_sync(
        {"claude": "old"}, "claude", "new", None,
    )
    assert need is True
    assert reason is None


def test_agent_needs_sync_unsupported_sentinel_blocks():
    need, reason = agent_needs_sync(
        {"claude": HASH_SENTINEL_UNSUPPORTED}, "claude", "abc", None,
    )
    assert need is False
    assert reason is None


def test_agent_needs_sync_unknown_sentinel_allows_retry():
    need, reason = agent_needs_sync(
        {"claude": HASH_SENTINEL_UNKNOWN}, "claude", "abc", None,
    )
    assert need is True


def test_agent_needs_sync_diverged_baseline_unchanged_stays_diverged():
    """Agent-side hash equals sentinel baseline → keep silent."""
    need, reason = agent_needs_sync(
        {"claude": make_diverged_sentinel("agent-old")},
        "claude", "csm-current",
        current_agent_body_hash="agent-old",
    )
    assert need is False
    assert reason is None


def test_agent_needs_sync_diverged_baseline_changed_clears_sentinel():
    """Agent side changed since diverge acceptance → re-consider."""
    need, reason = agent_needs_sync(
        {"claude": make_diverged_sentinel("agent-old")},
        "claude", "csm-current",
        current_agent_body_hash="agent-new",
    )
    assert need is True
    assert reason is not None
    assert "agent" in reason.lower()
    assert "diverge" in reason.lower()


def test_agent_needs_sync_diverged_no_agent_hash_conservative():
    """Can't read agent side → don't propose again yet."""
    need, reason = agent_needs_sync(
        {"claude": make_diverged_sentinel("agent-old")},
        "claude", "csm-current",
        current_agent_body_hash=None,
    )
    assert need is False


def test_agent_needs_sync_missing_agent_key_needs_sync():
    """Empty hashes for the target agent → true (never synced)."""
    need, _ = agent_needs_sync({}, "claude", "abc", None)
    assert need is True


# ---------------------------------------------------------------------------
# _read_agent_side_body
# ---------------------------------------------------------------------------


def _make_marker_text(marker_id: str, body: str) -> str:
    return (
        f"<!-- csm:start id={marker_id} -->\n"
        f"{body}\n"
        f"<!-- csm:end id={marker_id} -->\n"
    )


def test_read_agent_side_body_instruction_extracts_marker(tmp_path):
    """Memory branch: read marker block by locator (marker_id)."""
    mem_file = tmp_path / "CLAUDE.md"
    mem_file.write_text(
        "prelude\n" + _make_marker_text("no-sudo", "Do not use sudo."),
        encoding="utf-8",
    )
    adapter = MagicMock()
    adapter.memory_paths = MagicMock(return_value=[mem_file])
    adapter.read_memory = MagicMock(return_value=mem_file.read_text())
    adapter.marker_syntax = MagicMock(return_value=MarkerSyntax.html_comment())

    body = asyncio.run(
        read_agent_side_body(adapter, "instruction", "no-sudo")
    )
    assert body == "Do not use sudo."


def test_read_agent_side_body_instruction_missing_marker_returns_none(
    tmp_path,
):
    mem_file = tmp_path / "CLAUDE.md"
    mem_file.write_text("no markers here", encoding="utf-8")
    adapter = MagicMock()
    adapter.memory_paths = MagicMock(return_value=[mem_file])
    adapter.read_memory = MagicMock(return_value=mem_file.read_text())
    adapter.marker_syntax = MagicMock(return_value=MarkerSyntax.html_comment())

    body = asyncio.run(
        read_agent_side_body(adapter, "instruction", "nope")
    )
    assert body is None


def test_read_agent_side_body_instruction_no_memory_paths_returns_none():
    adapter = MagicMock()
    adapter.memory_paths = MagicMock(return_value=[])
    body = asyncio.run(
        read_agent_side_body(adapter, "instruction", "x")
    )
    assert body is None


def test_read_agent_side_body_mcp_uses_stable_subset_only():
    """v7.1: hashing subset excludes `raw` field."""
    adapter = MagicMock()
    entry_v1 = {"name": "srv", "transport": "stdio", "raw": "srv: stdio v1.0"}
    entry_v2 = {"name": "srv", "transport": "stdio", "raw": "srv: stdio v2.0"}

    adapter.mcp_list = AsyncMock(return_value=[entry_v1])
    body_v1 = asyncio.run(read_agent_side_body(adapter, "mcp_server", "srv"))

    adapter.mcp_list = AsyncMock(return_value=[entry_v2])
    body_v2 = asyncio.run(read_agent_side_body(adapter, "mcp_server", "srv"))

    # Same stable subset → identical serialised form.
    assert body_v1 == body_v2
    # Explicit shape check.
    parsed = json.loads(body_v1)
    assert set(parsed.keys()) == set(STABLE_MCP_KEYS)
    assert parsed == {"name": "srv", "transport": "stdio"}


def test_read_agent_side_body_mcp_name_not_found_returns_none():
    adapter = MagicMock()
    adapter.mcp_list = AsyncMock(return_value=[
        {"name": "other", "transport": "http", "raw": "other: http"},
    ])
    body = asyncio.run(read_agent_side_body(adapter, "mcp_server", "srv"))
    assert body is None


def test_read_agent_side_body_skill_returns_body_md():
    adapter = MagicMock()
    adapter.list_skills_full = MagicMock(return_value=[
        {"name": "sk1", "path": "/p/sk1", "description": "d",
         "body_md": "---\nname: sk1\n---\nbody one"},
        {"name": "sk2", "path": "/p/sk2", "description": "d",
         "body_md": "body two"},
    ])
    body = asyncio.run(read_agent_side_body(adapter, "skill", "sk2"))
    assert body == "body two"


def test_read_agent_side_body_skill_missing_body_md_field_returns_empty():
    """list_skills_full entry without body_md key → returns "" not None."""
    adapter = MagicMock()
    adapter.list_skills_full = MagicMock(return_value=[
        {"name": "sk1", "path": "/p/sk1", "description": "d"},
    ])
    body = asyncio.run(read_agent_side_body(adapter, "skill", "sk1"))
    assert body == ""


def test_read_agent_side_body_unknown_type_raises():
    adapter = MagicMock()
    with pytest.raises(ValueError, match="unknown resource_type"):
        asyncio.run(read_agent_side_body(adapter, "bogus", "x"))


# ---------------------------------------------------------------------------
# STABLE_MCP_KEYS invariant (v7.1)
# ---------------------------------------------------------------------------


def test_stable_mcp_keys_is_name_and_transport_only():
    """v7.1 lock: exact tuple ('name', 'transport'). No `raw` or `command`."""
    assert STABLE_MCP_KEYS == ("name", "transport")
