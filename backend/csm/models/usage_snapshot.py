"""UsageSnapshot — one row per usage probe result.

Persisted so the UI has recent history to show without re-spawning the
probe on every page load. The poller writes; the API reads the latest
row per agent.

Holds snapshots for BOTH claude and codex probes, discriminated by
`agent`. Both agents write into the SAME shared columns (session_pct /
session_reset / week_pct / week_reset / tier / subscription_type) so the
API + frontend render both from one code path. Codex has no 5h session
window (its /status only reports the weekly limit), so codex rows leave
session_pct / session_reset = None.

The four codex_* columns below are LEGACY (once held /usage-panel
history — lifetime / peak / streak / longest task). Retained in schema
for backward compat with older snapshots; no probe writes them anymore
and the UI doesn't read them.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class UsageSnapshot(Base):
    __tablename__ = "usage_snapshot"
    __table_args__ = (
        Index("ix_us_ts", "ts"),
        Index("ix_us_agent_ts", "agent", "ts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    # M14 discriminator — which CLI-adapter this probe covers.
    agent: Mapped[str] = mapped_column(
        String(32), default="claude", server_default="claude",
    )

    # ---- Shared quota fields ----
    # Claude fills all six from `/usage`. Codex fills week_* + tier from
    # `/status` and leaves session_* as None (no 5h window).
    session_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_reset: Mapped[str | None] = mapped_column(String(200), nullable=True)
    week_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    week_reset: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(80), nullable=True)
    subscription_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- Legacy codex /usage columns (no longer written; kept for compat) ----
    codex_lifetime_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    codex_peak_daily_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    codex_streak_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codex_longest_task_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- Meta ----
    # How the probe was triggered: 'scheduled' | 'manual' | 'startup'
    source: Mapped[str] = mapped_column(String(16), default="scheduled")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Truncated pane text kept for debugging when parse fails. Not shown in UI.
    raw_pane: Mapped[str | None] = mapped_column(Text, nullable=True)
