"""SyncConfig ORM model — per-module enrollment + poll interval.

One row per `SyncModule` (memory / mcp / skills). The `module` column is
`UNIQUE` so we never accidentally end up with two configs for the same
module (see spec §4 B4). `enrolled_agents` is a JSON list of adapter
names — cross-validated in Python against `AdapterRegistry.names()`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class SyncConfig(Base):
    __tablename__ = "sync_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # SyncModule enum value: "memory" | "mcp" | "skills".
    # UNIQUE so we can't create duplicate rows for the same module.
    module: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    # JSON list of adapter names, e.g. ["claude", "codex"]. Validated in
    # Python against `AdapterRegistry.names()` on write.
    enrolled_agents: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_utc_naive,
        onupdate=now_utc_naive,
        nullable=False,
    )

    # ---- sync v2 agent-driven (P1 migration m5n6o7p8q9rs) ----
    # `sync_mode`: 'lock' (rule-driven v1 drift poller, default) OR
    # 'agent' (agent-driven v2 scheduler). Users opt in via the Sync tab
    # config wizard.
    sync_mode: Mapped[str] = mapped_column(
        String(16),
        default="lock",
        server_default="lock",
        nullable=True,
    )
    # `tick_interval_hours`: 0 = manual only, N = scheduled every N hours.
    tick_interval_hours: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=True,
    )
    # `tick_interval_minutes`: finer-grained scheduled tick. 0 = fall back to
    # tick_interval_hours. When >0 it TAKES PRECEDENCE (minutes wins over
    # hours) so an agent-mode module can tick more often than hourly. The
    # scheduler clamps effective cadence to >= 1 minute so a stray small
    # value can't turn the 60s loop into a busy-spin.
    tick_interval_minutes: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=True,
    )

    # ---- resource allowlist (skills selection) ----
    # Optional list of resource NAMES this module is allowed to sync. `None`
    # (default) = no filter — consider everything the agent exposes (backward
    # compatible). A list restricts sync to EXACTLY those names. Primary use:
    # `skills`, so the user syncs only the skills they picked rather than every
    # skill installed under ~/.claude/skills/ (marketplace + authored are mixed
    # there with no reliable source marker). Applies generally — matched by
    # name, no hard-coded prefixes/counts.
    resource_allowlist: Mapped[list[str] | None] = mapped_column(
        JSON,
        default=None,
        nullable=True,
    )
