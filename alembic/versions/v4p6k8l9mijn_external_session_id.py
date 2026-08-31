"""Rename claude_session_id → external_session_id across 3 tables.

M10.A1: multi-agent v2 semantics fix. The column name `claude_session_id`
made sense when CSM only spawned Claude subprocesses (the CLI session id
was Claude's JSONL uuid). With the CLIAdapter layer supporting Codex
(session id from rollout `session_meta.session_id`) and any future
adapter, the column is a generic **external (CLI-assigned) session id**,
not a Claude-specific field.

Renamed columns:
    session.claude_session_id           → session.external_session_id
    raw_token_event.claude_session_id   → raw_token_event.external_session_id
    tool_invocation.claude_session_id   → tool_invocation.external_session_id

Renamed indexes:
    ux_session_claude_sid_active        → ux_session_external_sid_active
    ix_rte_session (unchanged name)     → still points at renamed column
    ux_rte_session_offset (unchanged)   → still valid; expr references new name
    ix_ti_session (unchanged name)      → still points at renamed column

Notes:
- SQLite 3.25+ supports `ALTER TABLE ... RENAME COLUMN` natively; CSM
  ships Python 3.11 with SQLite ≥ 3.40 so this is safe.
- Data is preserved verbatim (column rename is a metadata operation on
  SQLite; existing rows carry through).
- Partial-index `ux_rte_session_offset` has a SQL predicate that
  references `claude_session_id IS NOT NULL`; sqlite migration recreates
  the index against the new column name.
- Serialisation compat: `/api/sessions/*` responses emit BOTH new and
  legacy field names for one release (see api/sessions.py::_serialize).

Revision ID: v4p6k8l9mijn
Revises: u3o5j7k8lhim
Create Date: 2026-07-25
"""
from collections.abc import Sequence

from alembic import op

revision: str = "v4p6k8l9mijn"
down_revision: str | Sequence[str] | None = "u3o5j7k8lhim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- session table ----
    op.drop_index(
        "ux_session_claude_sid_active",
        table_name="session",
        if_exists=True,
    )
    op.alter_column(
        "session",
        "claude_session_id",
        new_column_name="external_session_id",
    )
    # Recreate the partial unique index so two live sessions can't claim
    # the same adapter-assigned id. Predicate uses new column name.
    op.execute(
        "CREATE UNIQUE INDEX ux_session_external_sid_active "
        "ON session (external_session_id) "
        "WHERE external_session_id IS NOT NULL "
        "AND status IN ('starting', 'running', 'idle', 'waiting_input', 'waiting_auth')"
    )

    # ---- raw_token_event table ----
    op.drop_index("ix_rte_session", table_name="raw_token_event", if_exists=True)
    op.drop_index("ux_rte_session_offset", table_name="raw_token_event", if_exists=True)
    op.alter_column(
        "raw_token_event",
        "claude_session_id",
        new_column_name="external_session_id",
    )
    op.create_index(
        "ix_rte_session",
        "raw_token_event",
        ["external_session_id"],
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_rte_session_offset "
        "ON raw_token_event (external_session_id, jsonl_offset) "
        "WHERE external_session_id IS NOT NULL AND jsonl_offset IS NOT NULL"
    )

    # ---- tool_invocation table ----
    op.drop_index("ix_ti_session", table_name="tool_invocation", if_exists=True)
    op.alter_column(
        "tool_invocation",
        "claude_session_id",
        new_column_name="external_session_id",
    )
    op.create_index(
        "ix_ti_session",
        "tool_invocation",
        ["external_session_id"],
    )


def downgrade() -> None:
    # ---- tool_invocation ----
    op.drop_index("ix_ti_session", table_name="tool_invocation", if_exists=True)
    op.alter_column(
        "tool_invocation",
        "external_session_id",
        new_column_name="claude_session_id",
    )
    op.create_index("ix_ti_session", "tool_invocation", ["claude_session_id"])

    # ---- raw_token_event ----
    op.drop_index("ix_rte_session", table_name="raw_token_event", if_exists=True)
    op.drop_index("ux_rte_session_offset", table_name="raw_token_event", if_exists=True)
    op.alter_column(
        "raw_token_event",
        "external_session_id",
        new_column_name="claude_session_id",
    )
    op.create_index("ix_rte_session", "raw_token_event", ["claude_session_id"])
    op.execute(
        "CREATE UNIQUE INDEX ux_rte_session_offset "
        "ON raw_token_event (claude_session_id, jsonl_offset) "
        "WHERE claude_session_id IS NOT NULL AND jsonl_offset IS NOT NULL"
    )

    # ---- session ----
    op.drop_index(
        "ux_session_external_sid_active",
        table_name="session",
        if_exists=True,
    )
    op.alter_column(
        "session",
        "external_session_id",
        new_column_name="claude_session_id",
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_session_claude_sid_active "
        "ON session (claude_session_id) "
        "WHERE claude_session_id IS NOT NULL "
        "AND status IN ('starting', 'running', 'idle', 'waiting_input', 'waiting_auth')"
    )
