"""Shared enums for the multi-agent sync subsystem.

Kept in a separate module so ORM models and API Pydantic schemas can both
import without pulling either half of the stack. Values are lowercase
strings — per CLAUDE.md wire-format convention (all API enum fields
serialize as `Enum.value`).
"""
from __future__ import annotations

from enum import StrEnum


class SyncModule(StrEnum):
    """Top-level module identifier used by `sync_config.module` and
    every downstream table's `module` column.
    """
    MEMORY = "memory"
    MCP = "mcp"
    SKILLS = "skills"


class DriftResourceType(StrEnum):
    """Discriminator for `(resource_type, resource_id)` cross-table pointer.

    See spec §4 (B4): we intentionally do NOT use a polymorphic FK — the
    tuple lives as two plain columns and cross-table joins are done in
    Python. Enum values match the target table name for clarity.
    """
    INSTRUCTION = "instruction"
    MCP_SERVER = "mcp_server"
    SKILL = "skill"


class DriftReason(StrEnum):
    """Why a drift_record row exists.

    - `concurrent_write` : B1 hash mismatch after our atomic write, meaning
                           the CLI REPL rename-race won and clobbered our
                           update. We recorded the mismatch and skipped the
                           sync attempt; drift poll will reconcile.
    - `external_edit`    : Drift poll saw a value that CSM never wrote
                           (user hand-edited `~/.claude/settings.json`,
                           `~/.codex/config.toml`, or a marker block).
    - `missing`          : A marker block / MCP entry / skill that CSM
                           expects on the agent side is not present.
    - `external_source`  : The target is a symlink to content CSM does not
                           own — typically `~/.claude/skills/<name>` linked
                           into a skill-book git repo. Writing would mutate
                           the user's working tree, so we refuse. Unlike the
                           others this never self-heals: the user has to
                           unlink it, or drop the agent from `share_scope`.
    """
    CONCURRENT_WRITE = "concurrent_write"
    EXTERNAL_EDIT = "external_edit"
    MISSING = "missing"
    EXTERNAL_SOURCE = "external_source"


class SyncStatus(StrEnum):
    """Per-agent sync attempt outcome — surfaced in the
    `SyncEnvelope.sync[*].status` API field AND stored in
    `sync_activity.status`.
    """
    OK = "ok"
    TIMEOUT = "timeout"          # subprocess wrapper hit its 10s cap
    UNSUPPORTED = "unsupported"  # adapter probe says the CLI lacks the sub
    SKIPPED = "skipped"          # B1 drift detected, backed off
    ERROR = "error"              # CLI returncode != 0
