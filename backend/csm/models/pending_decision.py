"""PendingDecision ORM model — conflicts / user-review queue.

Written by `SyncOrchestrator.apply_decisions` when the SyncAgent
returns `propose_conflict`, or when an `adopt_to_csm` decision collides
with an existing CSM row of the same name but different body hash
(auto-converted to a conflict at apply time — see design v4 §6).

Resolved by `POST /api/sync/pending-decisions/{id}/resolve` with one of:

- `take_agent:<agent>`  — adopt that agent's version into CSM + fan out
- `keep_diverged`       — accept the divergence; sync layer writes
                          `DIVERGED:<hex>` sentinels to
                          `last_synced_hashes` so the agent stops
                          proposing until either side changes again
- `dismiss`             — silently close the pending row

Fan-out failures during a resolve flip `status` to `resolve_failed` and
retain `retry_count` (capped at 5) for a UI-driven retry loop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class PendingDecision(Base):
    __tablename__ = "pending_decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    agent_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sync_agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # "adopt_to_csm" | "propagate_to_agent" | "propose_conflict"
    proposed_action: Mapped[str] = mapped_column(String(32), nullable=False)

    # `{"<agent>": "<body-text>"}` for propose_conflict, or the adopted
    # candidate wrapped as `{"<source_agent>": "<body>"}` for the
    # auto-conflict path.
    candidates_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # "pending" | "resolved" | "resolve_failed" | "dismissed"
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )

    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(
        String(32), default="user_ui", nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retry loop cap: `/resolve` returns 429 once `retry_count >= 5`.
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
