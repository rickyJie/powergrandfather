"""HourlyRollup ORM model — hourly aggregate of token usage (long-term storage)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class HourlyRollup(Base):
    __tablename__ = "hourly_rollup"
    __table_args__ = (
        Index("ix_hr_bucket", "bucket_hour"),
        Index(
            "ix_hr_bucket_model_proj_agent",
            "bucket_hour",
            "model",
            "project_path",
            "agent",
            unique=True,
        ),
        Index("ix_hr_agent", "agent"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bucket_hour: Mapped[datetime] = mapped_column(DateTime)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    project_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, default="claude", server_default="claude"
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    msg_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
