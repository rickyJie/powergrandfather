"""REST endpoints for AgentAlertRule — agent-authored token alert rules.

Two-step authoring flow:
  1. POST /api/tokens/agent-alerts/generate — user gives NL spec, gets back
     a Python check script + dry-run result. Nothing is persisted.
  2. POST /api/tokens/agent-alerts — user commits the previewed script.

Plus standard list / patch / delete for lifecycle management. Reload of the
evaluator's watched rules is triggered automatically on any mutation.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.api._serialize import iso_utc
from csm.models import AgentAlertRule
from csm.modules.token.agent_alert import PRESETS, generate_check_script, preset_catalog
from csm.modules.token.trend import TrendQueries

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tokens/agent-alerts", tags=["agent-alerts"])


# ---------- request / response schemas ----------


class GenerateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    nl_description: str = Field(..., min_length=1)
    threshold_spec: dict[str, Any] = Field(default_factory=dict)
    escalate: bool = False


class CreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    nl_description: str = Field(..., min_length=1)
    threshold_spec: dict[str, Any] = Field(default_factory=dict)
    check_script: str = Field(..., min_length=1)
    poll_interval_sec: int = Field(default=60, ge=10, le=86400)
    cooldown_sec: int = Field(default=300, ge=0, le=86400)
    channels: list[str] = Field(default_factory=lambda: ["inapp"])
    escalate: bool = False
    lark_chat_id: str | None = None
    lark_user_id: str | None = None
    enabled: bool = True


class PatchRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    poll_interval_sec: int | None = Field(default=None, ge=10, le=86400)
    cooldown_sec: int | None = Field(default=None, ge=0, le=86400)
    channels: list[str] | None = None
    escalate: bool | None = None
    lark_chat_id: str | None = None
    lark_user_id: str | None = None
    # Full editing of the check script + threshold_spec + nl_description is
    # allowed — caller is responsible for keeping them in sync (typically by
    # going through /generate or /update-from-preset which do that atomically).
    nl_description: str | None = None
    threshold_spec: dict[str, Any] | None = None
    check_script: str | None = None


class UpdateFromPresetRequest(BaseModel):
    """Rebuild an existing rule's script from its preset with new params.

    Used when the user edits a preset-based rule's thresholds. The preset id
    is inferred from `rule_metadata.preset_id` — if the rule was custom
    (no preset), this endpoint returns 400.
    """
    params: dict[str, Any] = Field(default_factory=dict)


def _serialize(r: AgentAlertRule) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "enabled": r.enabled,
        "nl_description": r.nl_description,
        "threshold_spec": r.threshold_spec or {},
        "check_script": r.check_script,
        "poll_interval_sec": r.poll_interval_sec,
        "cooldown_sec": r.cooldown_sec,
        "channels": r.channels or [],
        "escalate": r.escalate,
        "lark_chat_id": r.lark_chat_id,
        "lark_user_id": r.lark_user_id,
        "last_fired_at": iso_utc(r.last_fired_at),
        "last_error": r.last_error,
        "snoozed_until": iso_utc(r.snoozed_until),
        "created_at": iso_utc(r.created_at),
        "updated_at": iso_utc(r.updated_at),
        "rule_metadata": r.rule_metadata or {},
    }


def _validate_channels(channels: list[str]) -> None:
    allowed = {"inapp", "lark"}
    bad = [c for c in channels if c not in allowed]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown channel(s): {bad}; allowed: {sorted(allowed)}",
        )


async def _notify_evaluator(request: Request) -> None:
    """Poke the AgentAlertEvaluator to reload its rule set.

    Best-effort — the evaluator may not yet be attached (Phase 2 wires it up).
    Errors are logged, not raised, since a failed reload is a "stale rule set"
    problem the operator should see in csm.log but not a request-time failure.
    """
    ev = getattr(request.app.state, "agent_alert_evaluator", None)
    if ev is not None:
        try:
            await ev.reload()
        except Exception as e:
            log.warning("agent-alert evaluator reload failed: %s", e, exc_info=True)


# ---------- endpoints ----------


class FromPresetRequest(BaseModel):
    preset_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None                       # user-overridable label
    # None → use the preset's own defaults (varies by preset — escalate-heavy
    # presets ship with slower cadence to avoid burning claude tokens).
    poll_interval_sec: int | None = Field(default=None, ge=10, le=86400)
    cooldown_sec: int | None = Field(default=None, ge=0, le=86400)
    channels: list[str] = Field(default_factory=lambda: ["inapp"])
    escalate: bool | None = None                  # override preset default
    lark_chat_id: str | None = None
    lark_user_id: str | None = None
    enabled: bool = True


@router.get("/presets")
async def list_presets() -> dict[str, Any]:
    """Catalog of one-click preset templates (user-visible copy in Chinese)."""
    return {"items": preset_catalog()}


@router.post("/from-preset")
async def create_from_preset(
    body: FromPresetRequest,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Instantiate a preset with user-supplied thresholds — no `claude -p` call."""
    preset = PRESETS.get(body.preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"unknown preset_id: {body.preset_id}")

    try:
        resolved = preset.resolve_params(body.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_channels(body.channels)

    nl_description, threshold_spec, check_script = preset.build(resolved)
    escalate = preset.escalate_default if body.escalate is None else body.escalate
    poll_interval = body.poll_interval_sec if body.poll_interval_sec is not None else preset.poll_default_sec
    cooldown = body.cooldown_sec if body.cooldown_sec is not None else preset.cooldown_default_sec

    async with sm() as db:
        rule = AgentAlertRule(
            name=(body.name or preset.title).strip()[:200],
            nl_description=nl_description,
            threshold_spec=threshold_spec,
            check_script=check_script,
            poll_interval_sec=poll_interval,
            cooldown_sec=cooldown,
            channels=body.channels,
            escalate=escalate,
            lark_chat_id=body.lark_chat_id,
            lark_user_id=body.lark_user_id,
            enabled=body.enabled,
            rule_metadata={"preset_id": preset.id, "preset_params": resolved},
        )
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        serialized = _serialize(rule)
    await _notify_evaluator(request)
    return serialized


@router.post("/generate")
async def generate(
    body: GenerateRequest,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Spawn `claude -p` to synthesize a check script, dry-run it, return preview.

    Nothing is persisted. Caller decides whether to POST /agent-alerts to commit.
    """
    # Dry-run against the current 5h window so the user sees a realistic result.
    trend = TrendQueries(sm)
    try:
        window = await trend.current_window(hours=5.0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cannot snapshot window for dry-run: {e}")

    result = await generate_check_script(
        name=body.name,
        nl_description=body.nl_description,
        threshold_spec=body.threshold_spec,
        escalate=body.escalate,
        dry_run_window=window,
    )
    return {
        "ok": result.ok,
        "script": result.script,
        "dry_run": (
            None if result.dry_run is None
            else {
                "fired": result.dry_run.fired,
                "payload": result.dry_run.payload,
                "error": result.dry_run.error,
                "duration_sec": round(result.dry_run.duration_sec, 3),
            }
        ),
        "duration_sec": round(result.duration_sec, 3),
        "error": result.error,
        "window_snapshot": window,
    }


@router.get("")
async def list_rules(
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        rows = (await db.execute(select(AgentAlertRule).order_by(AgentAlertRule.created_at.desc()))).scalars().all()
        return {"items": [_serialize(r) for r in rows]}


@router.post("")
async def create_rule(
    body: CreateRequest,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    _validate_channels(body.channels)
    async with sm() as db:
        rule = AgentAlertRule(
            name=body.name,
            nl_description=body.nl_description,
            threshold_spec=body.threshold_spec,
            check_script=body.check_script,
            poll_interval_sec=body.poll_interval_sec,
            cooldown_sec=body.cooldown_sec,
            channels=body.channels,
            escalate=body.escalate,
            lark_chat_id=body.lark_chat_id,
            lark_user_id=body.lark_user_id,
            enabled=body.enabled,
        )
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        serialized = _serialize(rule)
    await _notify_evaluator(request)
    return serialized


@router.patch("/{rule_id}")
async def patch_rule(
    rule_id: str,
    body: PatchRequest,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    if body.channels is not None:
        _validate_channels(body.channels)
    async with sm() as db:
        rule = await db.get(AgentAlertRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="not found")
        for field_name, value in body.model_dump(exclude_unset=True).items():
            setattr(rule, field_name, value)
        await db.commit()
        await db.refresh(rule)
        serialized = _serialize(rule)
    await _notify_evaluator(request)
    return serialized


class SnoozeRequest(BaseModel):
    minutes: int = Field(default=120, ge=1, le=10080)   # 1min to 1 week


@router.post("/{rule_id}/snooze")
async def snooze_rule(
    rule_id: str,
    body: SnoozeRequest,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Temporarily mute a rule (evaluator skips it until `snoozed_until`)."""
    from datetime import timedelta as _td

    from csm.utils.time import now_utc_naive
    async with sm() as db:
        rule = await db.get(AgentAlertRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="not found")
        rule.snoozed_until = now_utc_naive() + _td(minutes=body.minutes)
        await db.commit()
        await db.refresh(rule)
        return _serialize(rule)


@router.post("/{rule_id}/unsnooze")
async def unsnooze_rule(
    rule_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        rule = await db.get(AgentAlertRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="not found")
        rule.snoozed_until = None
        await db.commit()
        await db.refresh(rule)
        return _serialize(rule)


@router.post("/{rule_id}/simulate")
async def simulate_rule(
    rule_id: str,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Force-fire a rule with a mock payload so the user can preview the
    notification visually.

    Does NOT call the real check script or the agent escalation — instead:
      * Builds a realistic-looking payload from the rule's threshold_spec.
      * If `escalate=True`, generates a canned agent_summary with a "[simulated]"
        prefix so users can preview the expand-report UI without burning
        claude tokens.
      * Emits the API_ERROR event through the same NotificationBus path a
        real trigger would, so the toast/badge/panel all activate.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from csm.core.events import Event, EventType
    from csm.utils.time import now_utc_naive

    async with sm() as db:
        rule = await db.get(AgentAlertRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="not found")

    # Mock payload — pull thresholds from spec, invent an "actual" a bit
    # past the threshold so it looks like a real breach.
    spec = rule.threshold_spec or {}

    def _mock_summary_body(preset_id: str | None) -> str:
        if preset_id == "msg_count_warn":
            return (
                "Message count over the last 5 hours is approaching the rate "
                "limit. Consider pausing for a few minutes, or dropping to Sonnet."
            )
        if preset_id == "total_tokens_warn":
            return (
                "[simulated] Main consumers:\n"
                "- session 9425ad60 (Opus, /data/foo): 78%, mostly cache_read\n"
                "- session dc7ed262 (Opus, /data/bar): 15%\n\n"
                "Suggestions:\n"
                "1. Check 9425ad60 for a runaway loop\n"
                "2. High cache_read means the prompt cache is hitting well, but "
                "the absolute volume is large\n"
                "3. Consider clearing old conversations and reopening the session"
            )
        if preset_id == "session_burn":
            return (
                "[simulated] The session in question:\n"
                "- session ef8f105f (/data/sample): 88% of the window\n"
                "- dominant tool: Bash × 42 (same command in a loop)\n\n"
                "Suggestions:\n"
                "1. Likely an auto session stuck in a loop — consider killing it\n"
                "2. Or a long task you started deliberately, in which case let it finish"
            )
        if preset_id == "cache_hit_drop":
            return (
                "[simulated] Cache-miss diagnosis:\n"
                "- session ef8f105f hit rate 15% (normally 95%+)\n"
                "- cause: recent --resume into a new conversation invalidated "
                "the whole cache\n\n"
                "Suggestions:\n"
                "1. Avoid frequent --resume\n"
                "2. Check whether CLAUDE.md changed recently — it invalidates "
                "every session's cache"
            )
        return "[simulated] A stand-in notification body, for checking how the UI renders it."

    preset_id = (rule.rule_metadata or {}).get("preset_id")
    payload: dict[str, Any] = {
        "alert_rule_id": rule.id,
        "alert_name": rule.name,
        "nl_description": rule.nl_description,
        "channels": rule.channels or [],
        "escalate": rule.escalate,
        "metric": spec.get("metric", "simulated"),
        "actual": spec.get("value", "n/a"),
        "threshold": spec.get("value", "n/a"),
        "script_payload": {"simulated": True, **spec},
        "notify_channel": rule.channels or [],
        "lark_chat_id": rule.lark_chat_id,
        "lark_user_id": rule.lark_user_id,
        # ms-precision so two clicks within the same second still get distinct
        # keys and LarkSink's dedup window doesn't swallow the second one.
        "_dedup_key": f"simulate:{rule.id}:{now_utc_naive().timestamp():.3f}",
        "simulated": True,
        "_bypass_dedup": True,   # every simulate click should push through
    }
    if rule.escalate:
        payload["agent_summary"] = {
            "title": f"[simulated] {rule.name} fired",
            "body": _mock_summary_body(preset_id),
        }

    es = request.app.state.event_stream
    await es.emit(Event(
        type=EventType.TOKEN_ALERT_TRIGGERED,
        ts=_dt.now(UTC),
        session_id=None,
        project_path=None,
        payload=payload,
    ))
    return {"ok": True, "simulated": True, "payload": payload}


@router.post("/{rule_id}/update-from-preset")
async def update_from_preset(
    rule_id: str,
    body: UpdateFromPresetRequest,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Re-render the check script for a preset-based rule with new params."""
    async with sm() as db:
        rule = await db.get(AgentAlertRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="not found")
        meta = rule.rule_metadata or {}
        preset_id = meta.get("preset_id")
        if not preset_id:
            raise HTTPException(
                status_code=400,
                detail="this rule was not created from a preset; edit nl_description via PATCH or regenerate via /generate",
            )
        preset = PRESETS.get(preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"preset {preset_id!r} no longer exists")
        try:
            resolved = preset.resolve_params(body.params)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        nl_description, threshold_spec, check_script = preset.build(resolved)
        rule.nl_description = nl_description
        rule.threshold_spec = threshold_spec
        rule.check_script = check_script
        rule.rule_metadata = {**meta, "preset_params": resolved}
        # Clear stale error since the script just changed.
        rule.last_error = None
        await db.commit()
        await db.refresh(rule)
        serialized = _serialize(rule)
    await _notify_evaluator(request)
    return serialized


@router.delete("/{rule_id}")
async def delete_rule(
    rule_id: str,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, str]:
    async with sm() as db:
        rule = await db.get(AgentAlertRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="not found")
        await db.delete(rule)
        await db.commit()
    await _notify_evaluator(request)
    return {"deleted": rule_id}
