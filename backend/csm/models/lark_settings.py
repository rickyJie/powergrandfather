"""LarkSettings ORM model — single-row Lark push config.

Single-user local app, so exactly one row exists (id=1). Enforced by a
CHECK constraint at the DB level; the LarkSink and API always target
`db.get(LarkSettings, 1)`.

"Sink no-op" cases (LarkSink._load_config returns None):
    1. id=1 row does not exist (never migrated / db reset)
    2. row.enabled = False (user disabled via UI)
    3. row.enabled = True but chat_id AND user_id both empty

Fields:
    enabled: master switch. Off => sink returns immediately.
    chat_id / user_id: push target. At least one required if enabled.
      Both may be set — chat_id wins in _shell_send (see lark_sink.py).
    dedup_window_sec: same (type, session_id, dedup_key) suppressed
      within this window (unless caller sets metadata._bypass_dedup=True).
    dnd_hours: list of ints ∈ [0, 23]. Server-local by default; if `tz`
      is set, evaluated in that zone. `_bypass_dnd` metadata flag
      overrides.
    tz: IANA name (e.g. "Asia/Shanghai") or "UTC". Empty = server local.
      Invalid values are tolerated at read time (fallback + warn log) so
      a config typo can't kill the whole push path.
    enabled_types: per-NotificationType toggle. Conservative default:
      a key missing from this dict is treated as False. Migration
      explicitly seeds the 4 legacy PUSH_TYPES to True so upgraders keep
      current behavior; a fresh install with enabled=True still needs
      to opt into each type.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class LarkSettings(Base):
    __tablename__ = "lark_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedup_window_sec: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60",
    )
    dnd_hours: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )
    tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # dict[str, bool] semantically — SQLAlchemy JSON type doesn't
    # enforce the value shape, but the API layer validates writes and
    # the sink coerces reads (`bool(v)`), so any stray non-bool value
    # is normalized on read.
    enabled_types: Mapped[dict[str, bool]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_utc_naive,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=now_utc_naive, onupdate=now_utc_naive,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_lark_settings_singleton"),
    )


__all__ = ["LarkSettings"]
