"""SyncAgentRun ORM model — one row per SyncAgent tick invocation.

Each row records the full audit trail of a single sync agent decision
cycle: the input snapshot the agent saw (redacted — see
`sync/state.py::_redact_for_snapshot`), the raw + parsed response, and
the terminal counters emitted by the orchestrator's apply loop.

Batched cold-start runs (>= 400 resources — see design v6 §6) chain via
`parent_run_id` so the UI can present "1 sync run (3 sub-runs, split by
module)". The parent row is the aggregate wrapper; sub-rows are the
actual per-module decisions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class SyncAgentRun(Base):
    __tablename__ = "sync_agent_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # "manual" | "scheduled" | "startup_replay" | "sub_run"
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)

    prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_state_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # Redacted skeleton — see sync/state.py::_redact_for_snapshot.
    input_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    response_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_parsed: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    decisions_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stale_skipped_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_after_collect_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Self-reference for cold-start batching.
    parent_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sync_agent_run.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Progress polling: "collecting" | "deciding" | "applying" | "done".
    phase: Mapped[str | None] = mapped_column(String(16), nullable=True)
