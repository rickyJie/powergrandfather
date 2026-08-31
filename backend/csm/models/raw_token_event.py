"""RawTokenEvent ORM model — one row per assistant message with usage data."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class RawTokenEvent(Base):
    __tablename__ = "raw_token_event"
    __table_args__ = (
        Index("ix_rte_ts", "ts"),
        Index("ix_rte_session", "external_session_id"),
        Index("ix_rte_source", "source"),
        Index("ix_rte_task_name", "task_name"),
        Index("ix_rte_csm_session", "csm_session_id"),
        Index("ix_rte_agent", "agent"),
        # Partial unique index that dedupes re-ingest of the same JSONL
        # message after an offset-flush loss (see migration m5g7b8c9dcae
        # and aggregator's ON CONFLICT DO NOTHING). Rows with NULL on
        # either column fall outside the index (best-effort for legacy).
        Index(
            "ux_rte_session_offset",
            "external_session_id",
            "jsonl_offset",
            unique=True,
            sqlite_where=text(
                "external_session_id IS NOT NULL AND jsonl_offset IS NOT NULL"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts: Mapped[datetime] = mapped_column(DateTime)
    external_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    is_subagent: Mapped[bool] = mapped_column(Boolean, default=False)
    # Attribution columns (added 2026-06-22):
    # source: where the session was spawned from. Values:
    #   manual_external (claude run outside csm), interactive, auto,
    #   onboarding_agent, supervisor_agent, subagent, unknown.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # task_name: TaskDefinition.name if this event came from an auto Run.
    task_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # command_type: derived from JSONL path / context. Values: subagent, direct.
    command_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # csm_session_id: internal Session.id row this event belongs to, if known.
    csm_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # M12: which CLI-adapter produced this event. EventStream supplies the
    # adapter even for manual_external sessions; legacy rows are migrated.
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, default="claude", server_default="claude"
    )
    # jsonl_offset: byte offset of the message start inside the JSONL file.
    # Paired with external_session_id, it uniquely identifies the source line,
    # letting re-ingest be idempotent (see partial unique index
    # ux_rte_session_offset added in migration m5g7b8c9dcae).
    jsonl_offset: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
