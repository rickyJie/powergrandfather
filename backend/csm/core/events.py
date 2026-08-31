"""Domain events emitted by Claude Event Stream.

Events are in-memory only (not persisted). Consumers subscribe to types they care
about and react. Each event carries enough context to identify the source
session and the underlying JSONL line for traceability.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    SESSION_CRASHED = "session.crashed"
    SESSION_IDLE = "session.idle"
    MESSAGE_USER_SENT = "message.user_sent"
    MESSAGE_ASSISTANT_DONE = "message.assistant_done"
    TOOL_INVOKED = "tool.invoked"
    TOOL_COMPLETED = "tool.completed"
    USAGE_RECORDED = "usage.recorded"
    API_ERROR = "api.error"
    RATE_LIMIT_HIT = "rate_limit.hit"
    # H4: Claude Code hook events (per-session, instant via HTTP callback)
    SESSION_WAITING_AUTH = "session.waiting_auth"
    SESSION_WAITING_INPUT = "session.waiting_input"
    SESSION_TOOL_PROGRESS = "session.tool_progress"
    # User interrupted an in-flight turn (Ctrl-C / SIGINT into the PTY, or the
    # API interrupt endpoint). The CLI aborts without a normal `Stop` hook or
    # `end_turn` assistant message, so nothing else transitions the session out
    # of RUNNING. Emitted by SessionManager on \x03 detection so the status
    # column, the worktime interval, and the frontend badge all get released.
    SESSION_INTERRUPTED = "session.interrupted"
    # Notification-source events. Previously all of these piggy-backed on
    # `API_ERROR` with an `_<name>=True` marker key; the marker pattern
    # was un-searchable and made routing to NotificationBus a big
    # if/elif tree. Each producer now emits its own EventType directly.
    TOKEN_ALERT_TRIGGERED = "token.alert_triggered"
    BUDGET_BREACHED = "token.budget_breached"
    PORT_CONFLICT_DETECTED = "ports.conflict_detected"
    SUPERVISOR_REVIEW_REQUESTED = "supervisor.review_requested"
    # Mission (workflow orchestrator) reached a terminal state. `payload.status`
    # is the MissionStatus value (`succeeded` / `failed`). CANCELLED is
    # intentionally NOT emitted here — a cancel is user-initiated and doesn't
    # deserve a "your mission just ended" bell.
    MISSION_ENDED = "mission.ended"


@dataclass
class Event:
    """A single domain event derived from Claude JSONL or watchdog inference."""
    type: EventType
    ts: datetime
    session_id: str | None              # claude session id (from JSONL filename)
    project_path: str | None            # cwd that hosts the JSONL
    payload: dict[str, Any] = field(default_factory=dict)
    source_offset: int | None = None    # byte offset within the JSONL
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


__all__ = ["Event", "EventType"]
