"""PricingConfig ORM model — per-model-family rates (USD per 1M tokens).

When present in this table, rates override the hardcoded defaults in
``csm.modules.token.aggregator.RATES``. Lets ops update prices without a deploy.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class PricingConfig(Base):
    __tablename__ = "pricing_config"

    # model_family: "opus" / "sonnet" / "haiku" / etc.
    model_family: Mapped[str] = mapped_column(String(50), primary_key=True)
    input_per_million: Mapped[float] = mapped_column(Float)
    cache_creation_per_million: Mapped[float] = mapped_column(Float)
    cache_read_per_million: Mapped[float] = mapped_column(Float)
    output_per_million: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
