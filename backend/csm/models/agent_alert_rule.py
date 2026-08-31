"""AgentAlertRule ORM model — agent-authored Python check scripts.

Successor to the retired `AlertRule` (v1 hardcoded absolute/percentage/predictive
condition types). Each row represents:

  * A natural-language rule spec from the user (`nl_description`).
  * An agent-generated Python check function (`check_script`) produced once at
    rule-creation time via `claude -p`, previewed against a dry-run, then
    committed by the user.
  * Runtime knobs: per-rule `poll_interval_sec`, `cooldown_sec`, channel routing.
  * `escalate=true` means when the script fires, CSM builds a rich context blob
    (top sessions / tool distribution / model split / cache ratio / 30min curve)
    and calls `claude -p` again to synthesize a "why + 3 recommendations"
    notification body. `escalate=false` sends a plain threshold-breach notice.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class AgentAlertRule(Base):
    __tablename__ = "agent_alert_rule"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # User inputs captured at rule creation, kept verbatim for audit + re-gen.
    nl_description: Mapped[str] = mapped_column(Text)
    threshold_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Agent-generated Python source. Signature contract (locked):
    #   def check(window: dict) -> tuple[bool, dict]:
    #       ...
    # Runs in a subprocess sandbox with a JSON snapshot of the current 5h window
    # piped in on stdin; must print `{"fired": bool, "payload": {...}}` on stdout.
    check_script: Mapped[str] = mapped_column(Text)

    # Runtime knobs.
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=60)
    cooldown_sec: Mapped[int] = mapped_column(Integer, default=300)

    # Notification routing.
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)   # e.g. ["inapp", "lark"]
    lark_chat_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lark_user_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Agent escalation switch: when True, on fire we build a context blob and
    # call `claude -p` for a natural-language root-cause + recommendations
    # summary before dispatching the notification.
    escalate: Mapped[bool] = mapped_column(Boolean, default=False)

    # State.
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the user hits "静音 2h", we set this to now+2h. The evaluator skips
    # this rule entirely (no sandbox run, no tick log) until now >= snoozed_until.
    # Distinct from `enabled=False` because the intent is temporary, tied to a
    # specific "let me focus / stop pestering me" moment, not a rule disable.
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    rule_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
