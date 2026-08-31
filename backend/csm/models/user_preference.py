"""UserPreference ORM model — single-row settings for the local user.

Single-user local app, so exactly one row exists (id=1). Enforced by
a CHECK constraint at the DB level and by convention on the read side
(API always queries `db.get(UserPreference, 1)`).

Fields:
    - default_agent: the adapter name to use when no explicit override
      is given at spawn time. Must be a name registered in the
      AdapterRegistry (enforced on write via API layer).
    - supervisor_agent: optional override for the SupervisorAgent's
      review calls. NULL means "follow default_agent". Kept separate
      so cheaper Haiku-style review models can be pinned without
      changing the main default.
    - has_completed_first_run: gates the first-run wizard on the
      frontend. Existing installs are seeded with 1 (skip); a fresh DB
      is seeded with 1 too, but a hypothetical brand-new install-from-
      scratch flow would set 0 to trigger the wizard.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class UserPreference(Base):
    __tablename__ = "user_preference"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    default_agent: Mapped[str] = mapped_column(
        String(32), nullable=False, default="claude", server_default="claude",
    )
    supervisor_agent: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    has_completed_first_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    # `local:gl#7 / LaTeX` follow-up: text auto-sent as the first user
    # input on every INTERACTIVE session spawn when
    # `default_session_prompt_enabled=true`. Delivered via the same
    # `_deliver_initial_prompt` path as AutomationRunner uses, so it's
    # indistinguishable from a real first message on the wire (and
    # therefore CLI-agnostic — works for claude / codex / any adapter).
    # AUTO / CHAT_AGENT sessions skip this because their prompts are
    # workflow-defined and must not be poisoned by a global default.
    # Empty string treated as "no override" even if enabled=true.
    default_session_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_session_prompt_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    # Optional supplementary note appended AFTER `default_session_prompt` at
    # delivery time (only when both are enabled + non-empty). Lets the user
    # tag the auto-sent prompt as informational — e.g. "no need to reply to
    # this message" — so the model doesn't treat it as a real instruction and
    # burn a turn. Spliced in `SessionManager.create_session`, never at the
    # shared `_deliver_initial_prompt` channel, so automation / agent-deck
    # prompts are untouched. Empty string == "no note" even when enabled.
    default_session_prompt_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_session_prompt_note_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    # Runtime-managed retention window for `raw_token_event` rows, in days.
    # This is the SOURCE OF TRUTH for the RollupWorker's TTL — it reads this
    # value fresh on every hourly tick, so changing it via PUT /api/preferences
    # takes effect without a restart (config `raw_event_retention_days` is only
    # the boot-time fallback used when this row can't be read).
    #   0  → keep raw events forever (rollup still runs; trend charts unaffected).
    #   N  → delete raw events older than N days after they've been rolled up.
    # Floor caveat: monthly budgets scoped by task/source read RAW for the whole
    # current calendar month, so a value between 1 and ~34 can undercount them.
    # 180 (default) keeps ~half a year of per-session/task drill-down.
    raw_event_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=180, server_default="180",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_utc_naive,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=now_utc_naive, onupdate=now_utc_naive,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_user_preference_singleton"),
    )


__all__ = ["UserPreference"]
