"""McpServer ORM model — one MCP server definition, materialised via
`<cli> mcp add <name> [...]`.

`env_json` values may embed `${VAR}` references — the spec (§5 B5)
requires these to be expanded ONLY into the subprocess env dict at sync
time, never into argv. The DB row stores the template as-is.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class McpServer(Base):
    __tablename__ = "mcp_server"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # CSM-authoritative name (unique). CLI-side gets a `csm-` prefix so
    # user-owned entries never collide with CSM-managed ones.
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    # "stdio" | "http" | "sse"
    transport: Mapped[str] = mapped_column(String(16), nullable=False)

    # For stdio: the command to exec. NULL for http/sse.
    command: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extra argv passed after `command`. JSON list of strings.
    args_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # For http/sse: the endpoint URL. NULL for stdio.
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # env dict template. Values MAY contain `${VAR}` refs; expansion is
    # deferred to sync time and lives in `resolve_env_refs()`.
    env_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # JSON list of adapter names this MCP server should be materialised on.
    enabled_for: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc_naive, onupdate=now_utc_naive, nullable=False,
    )

    # ---- sync v2 agent-driven (P1 migration m5n6o7p8q9rs) ----
    origin: Mapped[str] = mapped_column(
        Text, default="csm", server_default="csm", nullable=True,
    )
    last_synced_hashes: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=True,
    )
