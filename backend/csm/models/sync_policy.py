"""SyncPolicy ORM model — singleton system prompt for the SyncAgent.

One row (id=1) seeded at migration time with the v0.4 prompt (design
v4 §11 + v6 §5.2 + v7 §4). Editable via `PUT /api/sync/policy`; a
Reset button restores the shipped default via
`POST /api/sync/policy/reset`.

`prompt_hash` is not stored — the orchestrator computes sha256 on read
and stamps it onto each `SyncAgentRun.prompt_hash` for audit.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class SyncPolicy(Base):
    __tablename__ = "sync_policy"

    # Singleton — always id=1. New rows are refused at the API layer.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
