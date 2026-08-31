"""/api/settings/lark — read/write the Lark push singleton row + test push.

Endpoints
---------
GET  /api/settings/lark
    Return current row (or synthetic disabled default if the row is
    missing) + a `cli_installed` probe (`which lark-cli`).

PUT  /api/settings/lark
    Patch-style body: only fields present in the request are updated.
    Cross-field validation: if the merged result has enabled=True but
    both chat_id and user_id empty → 400. On success, flushes the
    LarkSink dedup cache so a `dedup_window_sec` change takes effect
    immediately.

POST /api/settings/lark/test
    Fire a synthetic push using current DB config. Wrapped in an 8s
    server-side timeout so the frontend loading spinner has a bound.
    Returns `{sent, error, duration_ms}`; a False sent with no error
    means the sink self-skipped (row missing / disabled / no target).
"""
from __future__ import annotations

import builtins
import shutil
from asyncio import wait_for
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.adapters.lark_sink import LarkSink
from csm.api._serialize import iso_utc
from csm.models.lark_settings import LarkSettings
from csm.models.notification import NotificationType

router = APIRouter(prefix="/api/settings/lark", tags=["settings"])

# Server-side timeout for the /test endpoint. Bounds the frontend
# spinner so a hung lark-cli can't leave the UI stuck > 10s.
_TEST_TIMEOUT_SEC = 8.0

# The 4 legacy PUSH_TYPES seeded to True by the Alembic migration.
# Used by GET to backfill missing keys so the client sees the full
# enabled_types shape (with False for anything not explicitly opted in).
_KNOWN_NOTIFICATION_TYPES = tuple(t.value for t in NotificationType)


def _sink(request: Request) -> LarkSink:
    sink = getattr(request.app.state, "lark_sink", None)
    if sink is None:
        raise HTTPException(status_code=503, detail="lark sink not initialized")
    return sink


def _sm(request: Request) -> async_sessionmaker:
    sm = getattr(request.app.state, "sessionmaker", None)
    if sm is None:
        raise HTTPException(status_code=503, detail="sessionmaker not initialized")
    return sm


def _cli_installed(cli: str = "lark-cli") -> bool:
    return shutil.which(cli) is not None


# ---------- pydantic models ----------
class LarkSettingsView(BaseModel):
    enabled: bool
    chat_id: str | None
    user_id: str | None
    dedup_window_sec: int
    dnd_hours: list[int]
    tz: str | None
    enabled_types: dict[str, bool]
    cli_installed: bool
    updated_at: str | None


class LarkSettingsPatch(BaseModel):
    """Patch semantics: fields absent from the request body are left
    unchanged. Empty-string `chat_id` / `user_id` clears the value.
    Cross-field validation happens in the handler (needs merged view)."""

    enabled: bool | None = None
    chat_id: str | None = None
    user_id: str | None = None
    dedup_window_sec: int | None = Field(default=None, ge=1, le=86400)
    dnd_hours: list[int] | None = None
    tz: str | None = None
    enabled_types: dict[str, bool] | None = None

    @field_validator("dnd_hours")
    @classmethod
    def _check_dnd(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        bad = [h for h in v if not isinstance(h, int) or h < 0 or h > 23]
        if bad:
            raise ValueError(f"dnd_hours entries must be int in [0, 23]; got {bad}")
        # De-dup + sort for canonical storage.
        return sorted(set(v))

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if v.upper() == "UTC":
            return v
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("enabled_types")
    @classmethod
    def _check_enabled_types(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if v is None:
            return v
        unknown = [k for k in v if k not in _KNOWN_NOTIFICATION_TYPES]
        if unknown:
            raise ValueError(
                f"unknown notification types: {sorted(unknown)}; "
                f"allowed: {sorted(_KNOWN_NOTIFICATION_TYPES)}"
            )
        return v


class TestPushResult(BaseModel):
    sent: bool
    error: str | None
    duration_ms: int


# ---------- serialization ----------
def _view(row: LarkSettings | None) -> LarkSettingsView:
    """Serialize a row (or a synthetic disabled default if row is None).

    Missing enabled_types keys are backfilled with False so the client
    always sees the full type shape — makes it obvious which types are
    not currently opted in.
    """
    if row is None:
        et_full = dict.fromkeys(_KNOWN_NOTIFICATION_TYPES, False)
        return LarkSettingsView(
            enabled=False,
            chat_id=None,
            user_id=None,
            dedup_window_sec=60,
            dnd_hours=[],
            tz=None,
            enabled_types=et_full,
            cli_installed=_cli_installed(),
            updated_at=None,
        )

    stored = dict(row.enabled_types or {})
    et_full = {k: bool(stored.get(k, False)) for k in _KNOWN_NOTIFICATION_TYPES}
    return LarkSettingsView(
        enabled=bool(row.enabled),
        chat_id=row.chat_id or None,
        user_id=row.user_id or None,
        dedup_window_sec=int(row.dedup_window_sec or 60),
        dnd_hours=sorted({int(h) for h in (row.dnd_hours or [])}),
        tz=row.tz or None,
        enabled_types=et_full,
        cli_installed=_cli_installed(),
        updated_at=iso_utc(row.updated_at),
    )


# ---------- routes ----------
@router.get("", response_model=LarkSettingsView)
async def get_lark_settings(request: Request) -> LarkSettingsView:
    sm = _sm(request)
    async with sm() as db:
        row = await db.get(LarkSettings, 1)
    return _view(row)


@router.put("", response_model=LarkSettingsView)
async def update_lark_settings(
    body: LarkSettingsPatch, request: Request
) -> LarkSettingsView:
    sm = _sm(request)
    sink = _sink(request)

    async with sm() as db:
        row = await db.get(LarkSettings, 1)
        if row is None:
            row = LarkSettings(id=1)
            db.add(row)

        # Compute merged view first so cross-field validation sees the
        # end state, not just the delta. Empty-string is normalized to
        # None so a client clearing a field with "" is treated as unset
        # for the "enabled requires target" check.
        def _merge_target(new: str | None, existing: str | None) -> str | None:
            if new is None:
                return existing
            return new or None  # "" → None

        old_dedup_window = int(row.dedup_window_sec or 60)
        merged_enabled = body.enabled if body.enabled is not None else bool(row.enabled)
        merged_chat = _merge_target(body.chat_id, row.chat_id or None)
        merged_user = _merge_target(body.user_id, row.user_id or None)

        if merged_enabled and not (merged_chat or merged_user):
            raise HTTPException(
                status_code=400,
                detail="cannot enable Lark push without chat_id or user_id",
            )

        # Apply fields.
        row.enabled = merged_enabled
        row.chat_id = merged_chat
        row.user_id = merged_user
        if body.dedup_window_sec is not None:
            row.dedup_window_sec = body.dedup_window_sec
        if body.dnd_hours is not None:
            row.dnd_hours = body.dnd_hours
        if body.tz is not None:
            row.tz = body.tz or None
        if body.enabled_types is not None:
            # Merge into existing (patch on enabled_types dict too — so
            # a caller sending {"session_crashed": False} doesn't wipe
            # the other 3 types).
            merged_et = dict(row.enabled_types or {})
            merged_et.update(body.enabled_types)
            row.enabled_types = merged_et
        elif merged_enabled and not (row.enabled_types or {}):
            # Empty-types UX safety net: fires whenever enabled=True
            # AND the row's enabled_types dict is empty (freshly
            # created via API, or raw-SQL wiped). Auto-seeds the types
            # that should page by default so a bare "enable + set
            # target" flow actually pushes something. Once the dict
            # has ANY key (even all False), this branch stops firing —
            # user's explicit type choices win over the safety net.
            #
            # Keep aligned with the fresh-install seed in
            # alembic/versions/a9u2pd3rfqot_lark_settings.py and with
            # the upgrade backfill in
            # d1s3t5u7v9wx_lark_enabled_types_backfill.py.
            row.enabled_types = {
                "new_message": True,
                "session_crashed": True,
                "auto_run_failed": True,
                "auto_needs_review": True,
                "token_warning": True,
                "port_conflict": True,
                "mission_done": True,
            }
        row.updated_at = datetime.utcnow()

        await db.commit()
        await db.refresh(row)

    # Flush sink cache. Two triggers matter:
    #   1. dedup_window_sec change → old cached entries could either
    #      hold too long (60→10) or release too soon (10→60).
    #   2. target change → don't preserve dedup state that was measured
    #      against the previous target.
    # Anything else (dnd/tz/enabled_types) doesn't need a flush, but
    # calling it is harmless (single-user tool, no traffic loss).
    dedup_window_changed = (
        body.dedup_window_sec is not None
        and body.dedup_window_sec != old_dedup_window
    )
    target_changed = body.chat_id is not None or body.user_id is not None
    if dedup_window_changed or target_changed:
        sink.flush_dedup_cache()

    return _view(row)


@router.post("/test", response_model=TestPushResult)
async def test_lark_push(request: Request) -> TestPushResult:
    """Fire a synthetic push through the sink.

    Returns quickly on:
      - sink self-skip (row missing / disabled / no target): `sent=False, error=None`
      - lark-cli transport failure: `sent=False, error=<lark-cli stderr>`
      - timeout: `sent=False, error="timeout after 8s"`
      - success: `sent=True, error=None`
    """
    if not _cli_installed():
        raise HTTPException(
            status_code=400,
            detail="lark-cli not found on PATH — install and run `lark auth login`",
        )
    sink = _sink(request)
    try:
        ok, err, dur = await wait_for(sink.send_test(), timeout=_TEST_TIMEOUT_SEC)
        return TestPushResult(sent=ok, error=err, duration_ms=int(dur * 1000))
    except builtins.TimeoutError:
        return TestPushResult(
            sent=False,
            error=f"timeout after {_TEST_TIMEOUT_SEC:.0f}s (lark-cli hung?)",
            duration_ms=int(_TEST_TIMEOUT_SEC * 1000),
        )


# Re-export the type set for tests that lock down the schema.
KNOWN_NOTIFICATION_TYPES = _KNOWN_NOTIFICATION_TYPES

__all__: list[Any] = ["router", "KNOWN_NOTIFICATION_TYPES"]
