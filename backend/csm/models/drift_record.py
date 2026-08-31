"""DriftRecord ORM model — one row per detected divergence.

Written by:
- B1 hash-guard fires (`reason=concurrent_write`);
- Drift poll worker sees CLI-side state that CSM never authored
  (`reason=external_edit` or `missing`).

Rows are marked `resolved=True` when either (a) the user clicks the
"resolve" button in the UI, or (b) the next successful sync of the same
resource confirms convergence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class DriftRecord(Base):
    __tablename__ = "drift_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, nullable=False)

    # SyncModule enum value.
    module: Mapped[str] = mapped_column(String(32), nullable=False)

    # DriftResourceType enum value.
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)

    agent: Mapped[str] = mapped_column(String(32), nullable=False)

    # DriftReason enum value.
    reason: Mapped[str] = mapped_column(String(32), nullable=False)

    expected_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
