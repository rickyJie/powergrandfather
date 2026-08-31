"""FanoutLedger ORM model — three-phase apply crash-recovery record.

Every adopt / propagate decision that reaches the "commit to CSM DB +
fan out to agents" step allocates one ledger row and walks it through
three phases (design v7 §3):

- Phase 1 (short DB tx): insert row (status='pending', attempt_count=0)
- Phase 2 (no DB lock):   `SyncService.sync_by_type_id(...)` — network
                          + adapter writes, may take seconds
- Phase 3 (short DB tx):  write `fanout_result_json` + update
                          `last_synced_hashes` on the resource row +
                          `close_ledger(status='done')`. In v7 Phase
                          2.5 (save result_json) is merged into
                          Phase 3 so there is only ONE post-fanout
                          transaction.

Crash windows:

- Between Phase 1 commit and Phase 2 start: `status='pending'`
- Between Phase 2 complete (in-memory) and Phase 3 begin: `status='pending'`
- Inside Phase 3 transaction: SQLAlchemy rollback, `status='pending'`

All three above → next tick re-processes the same resource → walks a
new three-phase, adapter idempotency (see
`docs/backends/adapter_idempotency_contract.md`) guarantees no
duplicate side effects. Old ledger 'pending' rows are swept to
`failed_terminal` by the scheduler's `_cleanup_stale_pending_ledger`
after 30 days.

The legacy `phase2_done` status is retained for compatibility with
in-flight v6 rows; startup `replay_pending_fanout_ledger()` only picks
up `phase2_done` entries (design v7 §2).

Uniqueness on `(resource_type, resource_id, body_hash, ts)` prevents
double-inserts for the same tick+resource+body combination.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class FanoutLedger(Base):
    __tablename__ = "fanout_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)

    body_hash: Mapped[str] = mapped_column(Text, nullable=False)
    target_agents: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    # "pending" | "phase2_done" | "done" | "failed_terminal"
    status: Mapped[str] = mapped_column(
        String(24), default="pending", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Filled at Phase 2.5 (v6) or during Phase 3 (v7 merged path).
    fanout_result_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "body_hash",
            "ts",
            name="uq_fanout_ledger_resource_hash_ts",
        ),
    )
