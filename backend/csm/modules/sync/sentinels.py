"""Sync v2 sentinel values + per-agent sync-need decision.

`last_synced_hashes` (on Instruction / McpServer / Skill rows) is a
`{agent_name: hash-or-sentinel}` dict. Values are either:

- a hex sha256 (fanout succeeded, agent has that exact body)
- `HASH_SENTINEL_UNSUPPORTED` — CSM has probed that this agent's CLI
  cannot hold this module (e.g. codex + skills). Never re-propose.
- `HASH_SENTINEL_UNKNOWN`      — fanout failed / never attempted; retry OK.
- `"DIVERGED:<hex>"`           — user explicitly accepted a divergence at
  some earlier point; `<hex>` is the agent-side body hash AT that moment
  (design v6 §5). The rule layer auto-clears this sentinel when the
  agent-side body no longer matches `<hex>` — signalling that the
  user's implicit "OK to differ" no longer applies.

`STABLE_MCP_KEYS` locks in the v7.1 micro-patch: the `raw` field in
`mcp_list()` output can drift across CLI versions (line format tweaks,
emoji, version tags), so hashing over the full dict would flip
sentinels on cosmetic upgrades. Only `(name, transport)` participate
in the stable subset — enough to identify a server for divergence
tracking; per-arg diffs surface via a fresh `propose_conflict`.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HASH_SENTINEL_UNSUPPORTED = "UNSUPPORTED"
HASH_SENTINEL_UNKNOWN = "UNKNOWN"
HASH_SENTINEL_DIVERGED_PREFIX = "DIVERGED:"

# v7.1 micro-patch: only these keys enter the mcp diverge-tracking hash.
# `raw` is intentionally excluded — cross-version unstable (see design
# v7 QA §"conditional P1" + v7.1 §"micro-patch rationale").
STABLE_MCP_KEYS: tuple[str, ...] = ("name", "transport")


# ---------------------------------------------------------------------------
# Sentinel encode / decode
# ---------------------------------------------------------------------------


def make_diverged_sentinel(agent_body_hash: str) -> str:
    """Encode the agent-side body hash at diverge time.

    Example: `make_diverged_sentinel("abc123")` → `"DIVERGED:abc123"`.
    """
    return f"{HASH_SENTINEL_DIVERGED_PREFIX}{agent_body_hash}"


def parse_diverged_sentinel(value: Any) -> str | None:
    """Return the encoded hash, or None if `value` is not a diverged sentinel.

    Non-string / unrelated values → None (safe fallback for polymorphic
    `last_synced_hashes` dict values).
    """
    if isinstance(value, str) and value.startswith(HASH_SENTINEL_DIVERGED_PREFIX):
        return value[len(HASH_SENTINEL_DIVERGED_PREFIX):]
    return None


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# _agent_needs_sync — the per-agent decision helper
# ---------------------------------------------------------------------------


def agent_needs_sync(
    last_synced_hashes: dict[str, Any] | None,
    agent_name: str,
    current_body_hash: str,
    current_agent_body_hash: str | None,
) -> tuple[bool, str | None]:
    """Decide whether `agent_name` needs a fresh fanout of this resource.

    Returns `(need_sync, clear_sentinel_reason)`.

    - `need_sync=False` when the last synced hash equals current CSM body
      hash, when it's `UNSUPPORTED`, or when it's a `DIVERGED:<hex>`
      sentinel whose baseline still matches the agent-side body.
    - `need_sync=True` when the sentinel is `UNKNOWN`, when the hash
      simply differs, or when the diverged baseline no longer matches
      (in which case `clear_sentinel_reason` is populated so the caller
      can record an audit line and reset the sentinel).
    """
    h = (last_synced_hashes or {}).get(agent_name)

    if h == HASH_SENTINEL_UNSUPPORTED:
        return False, None

    # Diverged sentinel: check agent-side change since acceptance.
    diverged_baseline = parse_diverged_sentinel(h)
    if diverged_baseline is not None:
        if current_agent_body_hash is None:
            # Can't read agent side; conservative skip until next tick.
            return False, None
        if current_agent_body_hash == diverged_baseline:
            # Agent side hasn't changed since user accepted divergence.
            return False, None
        return (
            True,
            f"agent {agent_name!r} body changed after diverge "
            f"(baseline={diverged_baseline[:8]}, "
            f"now={current_agent_body_hash[:8]}), clearing sentinel",
        )

    # Plain hash comparison.
    if h == current_body_hash:
        return False, None
    return True, None


# ---------------------------------------------------------------------------
# _read_agent_side_body — resource-type-dispatched adapter read
# ---------------------------------------------------------------------------

# Minimal structural typing for what this helper needs from an adapter.
# The full CLIAdapter Protocol lives in csm.backends.base — but we don't
# want to import it here (circular: sync depends on backends via
# service.py already, but sentinels.py is a pure helper module).


class _AdapterReadShape(Protocol):
    async def mcp_list(self) -> list[dict[str, Any]]: ...

    def memory_paths(self, scope: str) -> list[Any]: ...

    def read_memory(self, path: Any) -> str: ...

    def marker_syntax(self) -> Any: ...

    def list_skills_full(self) -> list[dict[str, Any]]: ...


async def read_agent_side_body(
    adapter: _AdapterReadShape,
    resource_type: Literal["instruction", "mcp_server", "skill"],
    locator: str,
) -> str | None:
    """Resolve a single agent-side body for `resource_type + locator`.

    - `instruction`: read memory file for `scope="user"`, extract the
      csm-marker block whose id == locator. Returns the body text or None.
    - `mcp_server`: `mcp_list()` on the adapter, find the entry with
      `name == locator`, serialize the STABLE_MCP_KEYS subset (v7.1)
      via `json.dumps(..., sort_keys=True)`. Returns the string or None.
    - `skill`: `list_skills_full()` on the adapter, find entry with
      `name == locator`, return its `body_md` (or None if missing).

    Raises ValueError on unknown resource_type.

    Callers should catch exceptions from adapter I/O and fall back to
    the `HASH_SENTINEL_UNKNOWN` sentinel (design v7 §1 keep_diverged
    error path).
    """
    # Local import to keep this module import-cost cheap for tests that
    # only touch the sentinel helpers.
    from csm.modules.sync.service import _extract_marker_body

    if resource_type == "instruction":
        paths = adapter.memory_paths("user")
        if not paths:
            return None
        text = adapter.read_memory(paths[0])
        return _extract_marker_body(text, adapter.marker_syntax(), locator)

    if resource_type == "mcp_server":
        entries = await adapter.mcp_list()
        for e in entries:
            if e.get("name") == locator:
                # v7.1: only (name, transport) participate in the diverge
                # hash. `raw` is unstable across CLI versions.
                stable = {k: e.get(k) for k in STABLE_MCP_KEYS}
                return json.dumps(stable, sort_keys=True, ensure_ascii=False)
        return None

    if resource_type == "skill":
        entries = adapter.list_skills_full()
        for e in entries:
            if e.get("name") == locator:
                return e.get("body_md", "")
        return None

    raise ValueError(f"unknown resource_type: {resource_type!r}")


__all__ = [
    "HASH_SENTINEL_UNSUPPORTED",
    "HASH_SENTINEL_UNKNOWN",
    "HASH_SENTINEL_DIVERGED_PREFIX",
    "STABLE_MCP_KEYS",
    "make_diverged_sentinel",
    "parse_diverged_sentinel",
    "agent_needs_sync",
    "read_agent_side_body",
]
