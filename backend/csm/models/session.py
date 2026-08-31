"""Session ORM model — represents a Claude CLI subprocess."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class SessionType(StrEnum):
    INTERACTIVE = "interactive"
    AUTO = "auto"
    CHAT_AGENT = "chat_agent"
    # NOTE: ONBOARDING_AGENT / SUPERVISOR_AGENT were declared in v1 but never
    # constructed anywhere; retired 2026-07-25. See docs/known_issues.md for
    # the v2 deferral notes. Historical DB rows (if any) still round-trip as
    # opaque strings — SQLAlchemy `native_enum=False` only validates on write.


# NOTE: `SessionBackend` (a closed enum) was retired in the v2 multi-agent
# refactor. `Session.agent` is now a free-form string that must match an
# entry in the AdapterRegistry. Callers that used to hardcode
# `SessionBackend.CLAUDE` / `SessionBackend.CODEX` should use the string
# literals "claude" / "codex" — or better, get the name from the registry.
# See `csm/backends/` and docs/backends/adding_a_new_adapter.md.


class SessionStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    WAITING_INPUT = "waiting_input"  # H2: claude is idle, awaiting user prompt
    WAITING_AUTH = "waiting_auth"    # H2: permission prompt blocking
    CRASHED = "crashed"
    EXITED = "exited"
    ORPHANED = "orphaned"


class Session(Base):
    __tablename__ = "session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    type: Mapped[SessionType] = mapped_column(SAEnum(SessionType, native_enum=False), default=SessionType.INTERACTIVE)
    cwd: Mapped[str] = mapped_column(String(1024))
    status: Mapped[SessionStatus] = mapped_column(SAEnum(SessionStatus, native_enum=False), default=SessionStatus.STARTING)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # CLI-adapter-assigned session id, external to CSM. Populated by
    # `SessionManager._post_spawn_bind` from whatever the adapter's
    # `pre_spawn_session_id` (claude uuid) OR `post_spawn_bind` (codex
    # session_meta.session_id) returns. Adapter-neutral: the column
    # holds any string id, semantic meaning is per-adapter.
    external_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Which CLI-adapter backs this session. Written at spawn time. Immutable
    # for the lifetime of the row (resume produces a new row via
    # superseded_by). Open string — must match a name in the AdapterRegistry.
    agent: Mapped[str] = mapped_column(
        String(32),
        default="claude",
        nullable=False,
        server_default="claude",
        index=True,
    )
    # Post-hoc bind: adapters with `POST_SPAWN_BIND` capability (codex)
    # discover a per-session artifact file after spawn and record its
    # absolute path here. Adapters that pre-bake the id (claude) leave
    # this NULL; claude uses `claude_session_id` + JSONL-path convention.
    rollout_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Set when this session was resumed into a fresh row. Predecessors keep
    # their external_session_id so we can still recover the JSONL if needed;
    # EventStream / notification lookups by external_session_id filter
    # `superseded_by IS NULL` to disambiguate to the chain-tail.
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    associated_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    last_activity_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    ring_buffer_offset: Mapped[int] = mapped_column(Integer, default=0)
    # H2: Claude Code hooks-derived signals.
    current_tool: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_assistant_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # local:a79c795d — user-managed project grouping (see SessionProject).
    # NULL means "auto-derived from cwd 2-level" (Sessions.vue synthesises
    # the virtual group at render time — no DB churn for cwd changes).
    session_project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # local:45b259b4 — right-click context menu additions.
    # `pinned` sorts the session to the top of its folder (or the whole
    # tree in flat views). `manual_unread` is a sticky flag that keeps
    # the red badge visible even when unread_count is 0 — an escape
    # hatch for "I want to remember to come back to this".
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    manual_unread: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    # `highlighted` = user-marked "important" flag surfaced as a gold
    # accent on the tree row. Independent of pinned (which reorders) and
    # manual_unread (which is a red badge) — orthogonal so users can
    # combine them without conflict.
    highlighted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    # `title_manual` = true iff the user renamed via CSM UI. Adapters
    # (claude JSONL tail, codex threads.title poll) refuse to overwrite
    # a title with `title_manual=true`. Clearing the title back to null
    # in the UI resets `title_manual` to false so external sources can
    # take over again. Added 2026-08-12 for `local:7a422f9d`.
    title_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    # Soft archive keeps terminal history and agent artifacts recoverable.
    # Permanent deletion remains a separate, explicit purge operation.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
