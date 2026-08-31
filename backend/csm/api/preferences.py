"""GET/PUT /api/preferences — the user's single-row UserPreference.

Single-user local app, so exactly one row exists (id=1, enforced by CHECK
constraint). Endpoints:

    GET  /api/preferences
        → current preferences + `is_first_run: bool`.
        (The frontend uses `is_first_run` to decide whether to pop the
        wizard on load.)

    PUT  /api/preferences
        body: { default_agent?: str, supervisor_agent?: str | null,
                has_completed_first_run?: bool }
        Only fields present in the body are updated (patch-style semantics).
        Validation: `default_agent` and `supervisor_agent` (if non-null)
        MUST match a registered adapter name; else 400.

    POST /api/preferences/complete-first-run
        Convenience: sets has_completed_first_run=true. Called by the
        wizard after the user picks an adapter.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._serialize import iso_utc
from csm.backends import AdapterRegistry
from csm.models import UserPreference

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


def _sm(request: Request) -> async_sessionmaker:
    sm = getattr(request.app.state, "sessionmaker", None)
    if sm is None:
        raise HTTPException(status_code=503, detail="sessionmaker not initialized")
    return sm


def _registry(request: Request) -> AdapterRegistry:
    reg = getattr(request.app.state, "adapter_registry", None)
    if reg is None:
        raise HTTPException(status_code=503, detail="adapter registry not initialized")
    return reg


class PreferencePatch(BaseModel):
    """Patch body — every field optional, present-fields-only semantics."""
    default_agent: str | None = None
    supervisor_agent: str | None | None = None   # explicit null = clear
    has_completed_first_run: bool | None = None
    # LaTeX / global preamble follow-up. `default_session_prompt` accepts
    # explicit null to clear (uses `model_fields_set` at the write site,
    # same trick as supervisor_agent). Sending `""` (empty string) is
    # equivalent — the write path normalizes to null.
    default_session_prompt: str | None = None
    default_session_prompt_enabled: bool | None = None
    # Supplementary note appended after the default prompt at delivery time.
    # Same null/empty→clear + present-fields-only semantics as the prompt.
    default_session_prompt_note: str | None = None
    default_session_prompt_note_enabled: bool | None = None
    # Runtime-managed raw_token_event retention window (days). 0 = keep forever.
    # The RollupWorker reads this live each tick, so a PUT takes effect on the
    # next hourly rollup without a restart.
    raw_event_retention_days: int | None = None


def _serialize(row: UserPreference) -> dict:
    return {
        "default_agent": row.default_agent,
        "supervisor_agent": row.supervisor_agent,
        "has_completed_first_run": bool(row.has_completed_first_run),
        "default_session_prompt": row.default_session_prompt,
        "default_session_prompt_enabled": bool(row.default_session_prompt_enabled),
        "default_session_prompt_note": row.default_session_prompt_note,
        "default_session_prompt_note_enabled": bool(row.default_session_prompt_note_enabled),
        "raw_event_retention_days": row.raw_event_retention_days,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
        # Convenience alias for the frontend wizard trigger.
        "is_first_run": not bool(row.has_completed_first_run),
    }


async def _get_or_seed(sm: async_sessionmaker) -> UserPreference:
    """Fetch the singleton row; if the seed migration didn't run for some
    reason, insert a fresh default row."""
    async with sm() as db:
        row = await db.get(UserPreference, 1)
        if row is None:
            row = UserPreference(id=1, default_agent="claude", has_completed_first_run=False)
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row


@router.get("")
async def get_preferences(request: Request) -> dict:
    sm = _sm(request)
    row = await _get_or_seed(sm)
    return _serialize(row)


@router.put("")
async def update_preferences(body: PreferencePatch, request: Request) -> dict:
    sm = _sm(request)
    reg = _registry(request)

    # Validate agent names against the registry — invalid names must
    # 400 rather than silently persist and produce runtime errors later.
    if body.default_agent is not None:
        if body.default_agent not in reg:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown agent {body.default_agent!r}; "
                    f"registered: {reg.names()}"
                ),
            )
    if body.supervisor_agent is not None and body.supervisor_agent != "":
        if body.supervisor_agent not in reg:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown supervisor_agent {body.supervisor_agent!r}; "
                    f"registered: {reg.names()}"
                ),
            )

    # Retention window must be a non-negative day count. 0 = keep forever;
    # a positive value below ~35 is allowed but risks undercounting monthly
    # budgets that scope by task/source (they read RAW for the whole current
    # calendar month) — that's the caller's call, so we don't hard-block it.
    if body.raw_event_retention_days is not None and body.raw_event_retention_days < 0:
        raise HTTPException(
            status_code=400,
            detail="raw_event_retention_days must be >= 0 (0 = keep forever)",
        )

    async with sm() as db:
        row = await db.get(UserPreference, 1)
        if row is None:
            row = UserPreference(id=1)
            db.add(row)
        if body.default_agent is not None:
            row.default_agent = body.default_agent
        # `supervisor_agent` uses explicit-null to clear. We can't tell
        # "field omitted" from "field=null" with pydantic BaseModel's
        # default handling; the fastapi request body will preserve the
        # None IFF the client sent `"supervisor_agent": null`. We treat
        # empty string the same as None (clear the pin).
        if "supervisor_agent" in body.model_fields_set:
            row.supervisor_agent = body.supervisor_agent or None
        if body.has_completed_first_run is not None:
            row.has_completed_first_run = body.has_completed_first_run
        if "default_session_prompt" in body.model_fields_set:
            # Explicit null OR empty string → clear. Trim so trailing
            # whitespace doesn't create a "prompt is truthy but effectively
            # empty" ambiguity when the enabled flag is on.
            trimmed = (body.default_session_prompt or "").strip()
            row.default_session_prompt = trimmed or None
        if body.default_session_prompt_enabled is not None:
            row.default_session_prompt_enabled = bool(body.default_session_prompt_enabled)
        if "default_session_prompt_note" in body.model_fields_set:
            trimmed_note = (body.default_session_prompt_note or "").strip()
            row.default_session_prompt_note = trimmed_note or None
        if body.default_session_prompt_note_enabled is not None:
            row.default_session_prompt_note_enabled = bool(
                body.default_session_prompt_note_enabled
            )
        if body.raw_event_retention_days is not None:
            row.raw_event_retention_days = int(body.raw_event_retention_days)
        await db.commit()
        await db.refresh(row)
    return _serialize(row)


@router.post("/complete-first-run")
async def complete_first_run(request: Request) -> dict:
    """Mark the first-run wizard as completed. Frontend calls this after
    the user picks their default agent. Idempotent."""
    sm = _sm(request)
    async with sm() as db:
        row = await db.get(UserPreference, 1)
        if row is None:
            row = UserPreference(id=1, has_completed_first_run=True)
            db.add(row)
        else:
            row.has_completed_first_run = True
        await db.commit()
        await db.refresh(row)
    return _serialize(row)
