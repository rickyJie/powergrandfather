"""Budget ORM model — per-scope token / cost allowances over a recurring period.

A Budget answers "are we within the allowance for X over period Y?". Unlike
AgentAlertRule (which fires once when an agent-authored check script signals),
a Budget is a *continuous* statement of intended consumption that can be
queried at any time and triggers actions when consumption percentage crosses
warn/block thresholds.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class BudgetScopeType(StrEnum):
    GLOBAL = "global"          # entire account
    PROJECT = "project"        # by project_path
    TASK = "task"              # by task_name (TaskDefinition.name)
    SOURCE = "source"          # by spawn source (interactive / auto / agent / ...)
    MODEL = "model"            # by model family (opus / sonnet / haiku / ...)
    SESSION = "session"        # by external_session_id (CLI-assigned)


class BudgetPeriod(StrEnum):
    WINDOW_5H = "window_5h"    # rolling 5h
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class BudgetAction(StrEnum):
    WARN = "warn"              # emit notification only
    BLOCK = "block"             # emit notification (block enforcement is advisory in v1)


class Budget(Base):
    __tablename__ = "budget"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scope_type: Mapped[BudgetScopeType] = mapped_column(
        SAEnum(BudgetScopeType, native_enum=False), default=BudgetScopeType.GLOBAL
    )
    scope_value: Mapped[str | None] = mapped_column(String(512), nullable=True)
    period: Mapped[BudgetPeriod] = mapped_column(
        SAEnum(BudgetPeriod, native_enum=False), default=BudgetPeriod.DAILY
    )
    token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    warn_pct: Mapped[float] = mapped_column(Float, default=80.0)
    action: Mapped[BudgetAction] = mapped_column(
        SAEnum(BudgetAction, native_enum=False), default=BudgetAction.WARN
    )
    notify_channel: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
