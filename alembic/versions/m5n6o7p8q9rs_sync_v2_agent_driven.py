"""sync v2 agent-driven: origin/last_synced_hashes + sync_mode + 4 new tables

Adds the schema surface required by the agent-driven sync v2 design
(v7 + v7.1 micro-patch):

- `instruction / mcp_server / skill` gain:
    * `origin TEXT DEFAULT 'csm'` (provenance: 'csm' | 'agent_import:<a>' |
      'agent_adopt:<a>')
    * `last_synced_hashes JSON DEFAULT '{}'` (per-agent fanout hash map,
      values may be a hex hash, or the sentinel strings 'UNSUPPORTED' /
      'UNKNOWN' / 'DIVERGED:<hex>'; see design v6 §5 / v7 §1 / v7.1)
    * Drops any legacy `last_synced_hash` singular column if present
      (idempotent — historical v1 attempts may have added it).

- `sync_config` gains:
    * `sync_mode TEXT DEFAULT 'lock'`  ('lock' vs 'agent')
    * `tick_interval_hours INTEGER DEFAULT 0`  (0 = manual)

- Four new tables:
    * `sync_agent_run`     — one row per SyncAgent tick (audit + counters)
    * `pending_decision`   — conflicts / manual-review queue with retry
    * `sync_policy`        — singleton (id=1) system prompt for SyncAgent
    * `fanout_ledger`      — three-phase apply crash-recovery ledger
      (status: pending | phase2_done | done | failed_terminal)

- Seeds a single `sync_policy(id=1)` row with the v0.4 prompt (design v4
  §11 + v6 §5.2 + v7 §4 + v7.1 sentinel notes).

Column adds use a PRAGMA-based `_has_column` guard rather than
`sqlalchemy.inspect(...).get_columns(...)`, because Alembic's inspector
result caches per-connection and returns stale views when we've just
mutated the same table earlier in this migration.

Downgrade is strict-reverse: drops the 4 new tables, then removes the
added columns via `batch_alter_table` (SQLite < 3.35 has no native
DROP COLUMN). The legacy `last_synced_hash` drop is NOT re-added on
downgrade — v1 never depended on it existing after this migration.

Revision ID: m5n6o7p8q9rs
Revises: d1s3t5u7v9wx
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "m5n6o7p8q9rs"
down_revision: str | Sequence[str] | None = "d1s3t5u7v9wx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_column(bind: sa.engine.Connection, table: str, col: str) -> bool:
    """Idempotent column probe using SQLite PRAGMA.

    Preferred over `sqlalchemy.inspect(bind).get_columns(table)` because
    the inspector caches per-connection and can serve stale results after
    an in-migration ALTER on the same table.
    """
    rows = bind.execute(sa.text(f'PRAGMA table_info("{table}")')).fetchall()
    return any(r[1] == col for r in rows)


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = :n"
        ).bindparams(n=table)
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Seed prompt (SyncAgent v0.4)
# ---------------------------------------------------------------------------

_SEED_PROMPT_V0_4 = """\
You are the CSM Sync Agent, a decision-only LLM inside the PowerGrandFather /
Claude Session Manager (CSM) single-user local console. On each tick you
receive a snapshot of:

  - CSM's own DB rows for three modules (instructions / mcp_servers / skills)
  - the current live state of each enrolled CLI agent (e.g. claude, codex):
    memory file contents, mcp server listings, skill files
  - meta: recent pending decisions you've previously produced, per-agent
    last_synced_hashes on each CSM resource, and any sentinel markers

Your ONLY output is a strict-JSON `SyncDecisionsPayload` matching the schema
CSM validates with Pydantic. You never write files, spawn processes, or make
network calls directly — CSM's rule layer executes what you propose.

## Allowed actions (discriminator: `action`)

1. `adopt_to_csm`   — pull an agent-side resource into CSM DB
2. `propagate_to_agent` — push a CSM row out to a specific agent
3. `propose_conflict`   — surface a genuine two-sided divergence for user review
4. `skip`               — record a no-op decision with rationale

You MAY NOT propose `delete_from_csm`, `merge`, or any other action; those
will fail Pydantic validation and abort the entire tick.

## Output volume constraint

You may output at most 30 non-skip decisions per tick. If you judge more
than 30 resources need attention, keep the top 30 by importance, and emit
`skip` entries for the rest with rationale = "defer to next tick due to
output cap". CSM will surface them again next tick.

## Sentinel semantics in `last_synced_hashes`

- `"UNSUPPORTED"` — CSM has probed that this agent can't hold this module
  (e.g. codex doesn't support skills). Never `propagate_to_agent` to that
  agent for that resource; treat as permanently out of scope.
- `"UNKNOWN"`     — a previous sync failed or state was never established.
  You MAY propose again if it looks resolvable.
- `"DIVERGED:<hex>"` — the user explicitly accepted a divergence at some
  earlier point. Do NOT propose `propose_conflict` again for that
  (resource, agent) pair unless the agent-side body has changed since.
  You don't need to compute the hex; CSM's rule layer auto-clears the
  sentinel when it detects real change and will feed you a cleared state.

## Idempotency & duplicates

- If a `pending_decision` for the same resource already exists (see
  `input.pending_decisions_recent`), do NOT re-propose the same conflict
  unless both sides have genuinely changed.
- If you'd `adopt_to_csm` a name that already exists in CSM with the same
  body hash, don't emit it — CSM's rule layer will filter, but you can
  save a slot by preferring `skip` with rationale.

## Truncation warning

If an agent's `memory_full` ends with a marker like
`<!-- truncated at 100KB, marker blocks may be incomplete -->`,
consider only marker blocks whose `csm:start` and `csm:end` pair is
fully visible. Skip half-visible blocks with a rationale explaining
truncation.

## No secrets propagation

If a body contains an obvious secret pattern (e.g. `sk-...`, AWS key IDs,
long base64-looking tokens) and the recommended_scope crosses agents you
haven't seen before, downgrade to `propose_conflict` and explain in the
rationale.

## Naming rules

`candidate.name` must match `^[a-z0-9][a-z0-9-]{0,79}$`. Rename if needed
(e.g. underscores → hyphens, uppercase → lowercase) and note it in the
rationale.

## Output shape

Return a JSON object matching:

```json
{
  "decisions": [ { "action": "...", ... }, ... ],
  "summary":   "one-paragraph description of this tick's overall shape"
}
```

Return nothing else — no markdown fences, no prose before/after. If you
cannot satisfy the schema, emit `[{"action":"skip","rationale":"..."}]`
plus a summary explaining why; do NOT emit malformed JSON.
"""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # ---- Column additions on 3 resource tables --------------------------
    for tbl in ("instruction", "mcp_server", "skill"):
        with op.batch_alter_table(tbl) as batch:
            if not _has_column(bind, tbl, "origin"):
                batch.add_column(
                    sa.Column("origin", sa.Text(), server_default="csm", nullable=True)
                )
            if not _has_column(bind, tbl, "last_synced_hashes"):
                batch.add_column(
                    sa.Column(
                        "last_synced_hashes",
                        sa.JSON(),
                        server_default="{}",
                        nullable=True,
                    )
                )
            # Legacy v1 attempts may have added a singular column; drop it.
            if _has_column(bind, tbl, "last_synced_hash"):
                batch.drop_column("last_synced_hash")

    # ---- sync_config additions ------------------------------------------
    with op.batch_alter_table("sync_config") as batch:
        if not _has_column(bind, "sync_config", "sync_mode"):
            batch.add_column(
                sa.Column(
                    "sync_mode", sa.Text(), server_default="lock", nullable=True
                )
            )
        if not _has_column(bind, "sync_config", "tick_interval_hours"):
            batch.add_column(
                sa.Column(
                    "tick_interval_hours",
                    sa.Integer(),
                    server_default="0",
                    nullable=True,
                )
            )

    # ---- New table: sync_agent_run --------------------------------------
    if not _has_table(bind, "sync_agent_run"):
        op.create_table(
            "sync_agent_run",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("trigger", sa.String(32), nullable=False),
            sa.Column("prompt_hash", sa.Text(), nullable=False),
            sa.Column("input_state_hash", sa.Text(), nullable=False),
            sa.Column("input_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("response_raw", sa.Text(), nullable=True),
            sa.Column("response_parsed", sa.JSON(), nullable=True),
            sa.Column("decisions_count", sa.Integer(), nullable=True),
            sa.Column("applied_count", sa.Integer(), nullable=True),
            sa.Column("rejected_count", sa.Integer(), nullable=True),
            sa.Column("stale_skipped_count", sa.Integer(), nullable=True),
            sa.Column("deleted_after_collect_count", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("token_usage_json", sa.JSON(), nullable=True),
            sa.Column(
                "parent_run_id",
                sa.Integer(),
                sa.ForeignKey("sync_agent_run.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("phase", sa.String(16), nullable=True),
        )
        op.create_index(
            "ix_sync_agent_run_ts", "sync_agent_run", ["ts"], unique=False
        )
        op.create_index(
            "ix_sync_agent_run_parent",
            "sync_agent_run",
            ["parent_run_id"],
            unique=False,
        )

    # ---- New table: pending_decision ------------------------------------
    if not _has_table(bind, "pending_decision"):
        op.create_table(
            "pending_decision",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "agent_run_id",
                sa.Integer(),
                sa.ForeignKey("sync_agent_run.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("resource_type", sa.String(32), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=True),
            sa.Column("proposed_action", sa.String(32), nullable=False),
            sa.Column("candidates_json", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                server_default="pending",
                nullable=False,
            ),
            sa.Column("resolution", sa.Text(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column(
                "resolved_by",
                sa.String(32),
                server_default="user_ui",
                nullable=True,
            ),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("apply_error", sa.Text(), nullable=True),
            sa.Column(
                "retry_count", sa.Integer(), server_default="0", nullable=False
            ),
        )
        op.create_index(
            "ix_pending_decision_status_ts",
            "pending_decision",
            ["status", "ts"],
            unique=False,
        )
        op.create_index(
            "ix_pending_decision_resource",
            "pending_decision",
            ["resource_type", "resource_id"],
            unique=False,
        )

    # ---- New table: sync_policy -----------------------------------------
    if not _has_table(bind, "sync_policy"):
        op.create_table(
            "sync_policy",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        # Seed singleton (id=1).
        op.execute(
            sa.text(
                "INSERT INTO sync_policy (id, prompt, updated_at) "
                "VALUES (1, :p, CURRENT_TIMESTAMP)"
            ).bindparams(p=_SEED_PROMPT_V0_4)
        )

    # ---- New table: fanout_ledger ---------------------------------------
    if not _has_table(bind, "fanout_ledger"):
        op.create_table(
            "fanout_ledger",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("resource_type", sa.String(32), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=False),
            sa.Column("body_hash", sa.Text(), nullable=False),
            sa.Column("target_agents", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(24),
                server_default="pending",
                nullable=False,
            ),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column("attempted_at", sa.DateTime(), nullable=True),
            sa.Column("fanout_result_json", sa.JSON(), nullable=True),
            sa.UniqueConstraint(
                "resource_type",
                "resource_id",
                "body_hash",
                "ts",
                name="uq_fanout_ledger_resource_hash_ts",
            ),
        )
        op.create_index(
            "ix_fanout_ledger_status", "fanout_ledger", ["status"], unique=False
        )


def downgrade() -> None:
    # Drop the 4 new tables first (they depend on nothing in this migration).
    for tbl in ("fanout_ledger", "pending_decision", "sync_agent_run", "sync_policy"):
        if _has_table(op.get_bind(), tbl):
            op.drop_table(tbl)

    # Strip added columns via batch_alter (SQLite < 3.35 has no native DROP COLUMN).
    with op.batch_alter_table("sync_config") as batch:
        for col in ("tick_interval_hours", "sync_mode"):
            if _has_column(op.get_bind(), "sync_config", col):
                batch.drop_column(col)

    for tbl in ("instruction", "mcp_server", "skill"):
        with op.batch_alter_table(tbl) as batch:
            for col in ("last_synced_hashes", "origin"):
                if _has_column(op.get_bind(), tbl, col):
                    batch.drop_column(col)
    # Note: legacy `last_synced_hash` is NOT re-added; v1 never guaranteed it.
