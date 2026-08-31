"""ToolInvocation — one row per tool_use block in an assistant message.

Renamed 2026-07-25 from `claude_session_id` to `external_session_id`
so codex (and any future adapter) can share the same table. The
external id is whatever the CLI itself assigns — see Session.external_session_id
docstring.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class ToolInvocation(Base):
    __tablename__ = "tool_invocation"
    __table_args__ = (
        Index("ix_ti_ts", "ts"),
        Index("ix_ti_tool", "tool_name"),
        Index("ix_ti_session", "external_session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts: Mapped[datetime] = mapped_column(DateTime)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    csm_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    command_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-tool token attribution: when an assistant message has N tool_use blocks,
    # that message's usage is split N-ways and each share lands here. Simple equal
    # split — not perfect for "Bash vs Read" weighting but the only signal we have
    # without re-running the model.
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
