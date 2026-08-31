"""REST endpoints for the Token Module."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.api._serialize import iso_utc
from csm.modules.token.trend import TrendQueries
from csm.utils.time import now_utc_naive


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid datetime: {s}")


def _filters_from_query(
    model: list[str] | None,
    project: list[str] | None,
    source: list[str] | None,
    task: list[str] | None,
    command_type: list[str] | None,
    session: list[str] | None,
    agent: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "project": project,
        "source": source,
        "task": task,
        "command_type": command_type,
        "session": session,
        # M12: agent-scoped filter. Frontend passes the user's default
        # agent by default; explicit UI toggle can switch to another or
        # clear (view all agents).
        "agent": agent,
    }


router = APIRouter(prefix="/api/tokens", tags=["tokens"])


def _trend(sm: async_sessionmaker) -> TrendQueries:
    return TrendQueries(sm)


@router.get("/current")
async def current_window(
    hours: float = 5.0,
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    filters = _filters_from_query(model, project, source, task, command_type, session, agent)
    return await _trend(sm).current_window(hours=hours, filters=filters)


@router.get("/period-delta")
async def period_delta(
    hours: int = 168,
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Compare the current [-hours, now] window against the previous
    [-2*hours, -hours] window. Powers the "this period vs prior period"
    delta badge on the Tokens hero row.

    M13: honours the same filter shape as `/current` / `/history` etc.
    Critical for agent-scoped view — without this, switching to
    `agent=codex` scope would still show a delta over the whole
    account's spend (both windows unfiltered).

    Returns raw totals + percent deltas. If the previous period had zero
    consumption, returns `null` for the pct so the frontend can render "—".
    """
    from datetime import timedelta

    now = now_utc_naive()
    cur_end = now
    cur_start = now - timedelta(hours=hours)
    # Previous period: shift window back by `hours`.
    prev_end = now - timedelta(hours=hours)
    prev_start = prev_end - timedelta(hours=hours)
    async with sm() as db:
        from sqlalchemy import func

        from csm.models import HourlyRollup, RawTokenEvent
        raw_total_expr = (
            RawTokenEvent.input_tokens + RawTokenEvent.cache_creation_tokens
            + RawTokenEvent.cache_read_tokens + RawTokenEvent.output_tokens
        )
        roll_total_expr = (
            HourlyRollup.input_tokens + HourlyRollup.cache_creation_tokens
            + HourlyRollup.cache_read_tokens + HourlyRollup.output_tokens
        )

        # Filter-clause lists — built once, applied to raw and rollup
        # summing queries + the min() probes. rollup carries fewer
        # columns (bucket_hour, model, project_path, agent) so its
        # list is a subset. Everything is IN-clause so single-value
        # arrays work identically to multi-value.
        raw_where: list = []
        roll_where: list = []
        if agent:
            raw_where.append(RawTokenEvent.agent.in_(agent))
            roll_where.append(HourlyRollup.agent.in_(agent))
        if model:
            raw_where.append(RawTokenEvent.model.in_(model))
            roll_where.append(HourlyRollup.model.in_(model))
        if project:
            raw_where.append(RawTokenEvent.project_path.in_(project))
            roll_where.append(HourlyRollup.project_path.in_(project))
        if source:
            raw_where.append(RawTokenEvent.source.in_(source))
        if task:
            raw_where.append(RawTokenEvent.task_name.in_(task))
        if command_type:
            raw_where.append(RawTokenEvent.command_type.in_(command_type))
        if session:
            raw_where.append(RawTokenEvent.external_session_id.in_(session))

        # Rollups do not retain source/task/command/session dimensions. Using
        # them anyway would silently mix unrelated data into a filtered view.
        can_use_rollups = not any((source, task, command_type, session))

        # raw + rollup are both live simultaneously (rollup is written every
        # hour; raw stays until TTL). To avoid double-counting we cut them at
        # the earliest raw ts: raw covers [earliest, now], rollup covers
        # anything BEFORE `earliest`. This way each hour is aggregated once.
        er_stmt = select(func.min(RawTokenEvent.ts))
        for w in raw_where:
            er_stmt = er_stmt.where(w)
        earliest_raw = (await db.execute(er_stmt)).scalar_one_or_none()
        earliest_raw_hour = (
            earliest_raw.replace(minute=0, second=0, microsecond=0)
            if earliest_raw is not None else None
        )
        earliest_roll = None
        if can_use_rollups:
            el_stmt = select(func.min(HourlyRollup.bucket_hour))
            for w in roll_where:
                el_stmt = el_stmt.where(w)
            earliest_roll = (await db.execute(el_stmt)).scalar_one_or_none()

        async def _sum_window(start, end):
            # raw side (restricted to [max(start, earliest_raw), end])
            raw_msg = raw_tok = 0
            raw_cost = 0.0
            if earliest_raw is not None:
                raw_lo = max(start, earliest_raw)
                if raw_lo < end:
                    stmt = (
                        select(
                            func.count(RawTokenEvent.id),
                            func.coalesce(func.sum(raw_total_expr), 0),
                            func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
                        )
                        .where(RawTokenEvent.ts >= raw_lo, RawTokenEvent.ts < end)
                    )
                    for w in raw_where:
                        stmt = stmt.where(w)
                    r = (await db.execute(stmt)).one()
                    raw_msg, raw_tok, raw_cost = int(r[0]), int(r[1]), float(r[2])
            # rollup side (bucket_hour BEFORE earliest_raw so we don't overlap)
            roll_msg = roll_tok = 0
            roll_cost = 0.0
            roll_upper = (
                min(end, earliest_raw_hour)
                if earliest_raw_hour is not None else end
            )
            if can_use_rollups and roll_upper > start:
                stmt = (
                    select(
                        func.coalesce(func.sum(HourlyRollup.msg_count), 0),
                        func.coalesce(func.sum(roll_total_expr), 0),
                        func.coalesce(func.sum(HourlyRollup.estimated_cost_usd), 0.0),
                    )
                    .where(HourlyRollup.bucket_hour >= start, HourlyRollup.bucket_hour < roll_upper)
                )
                for w in roll_where:
                    stmt = stmt.where(w)
                r = (await db.execute(stmt)).one()
                roll_msg, roll_tok, roll_cost = int(r[0]), int(r[1]), float(r[2])
            return raw_msg + roll_msg, raw_tok + roll_tok, raw_cost + roll_cost

        cur_msg, cur_tok, cur_cost = await _sum_window(cur_start, cur_end)
        prev_msg, prev_tok, prev_cost = await _sum_window(prev_start, prev_end)

    # `coverage` is how much of the prior window is actually inside the data
    # range (raw + rollup unioned). We do NOT hide the delta when coverage
    # is low — the user asked to see it even when history is thin — but we
    # expose the ratio so the frontend tooltip can flag it.
    def _min(a, b):
        if a is None: return b
        if b is None: return a
        return a if a < b else b
    earliest = _min(earliest_raw, earliest_roll)
    if earliest is not None:
        window_sec = (prev_end - prev_start).total_seconds()
        covered_sec = max(0.0, (prev_end - max(prev_start, earliest)).total_seconds())
        prev_coverage = covered_sec / window_sec if window_sec > 0 else 0.0
    else:
        prev_coverage = 0.0

    def _pct(cur_v: float, prev_v: float) -> float | None:
        # Only null when the prior period is truly empty — otherwise the
        # user sees the raw ratio (however dramatic) and the coverage hint
        # explains the outlier.
        if prev_v <= 0:
            return None
        return round(100.0 * (cur_v - prev_v) / prev_v, 1)

    return {
        "hours": hours,
        "current": {
            "msg_count": cur_msg,
            "total_tokens": cur_tok,
            "estimated_cost_usd": cur_cost,
        },
        "previous": {
            "msg_count": prev_msg,
            "total_tokens": prev_tok,
            "estimated_cost_usd": prev_cost,
        },
        "delta_pct": {
            "msg_count": _pct(cur_msg, prev_msg),
            "total_tokens": _pct(cur_tok, prev_tok),
            "estimated_cost_usd": _pct(cur_cost, prev_cost),
        },
        "prev_coverage": round(prev_coverage, 3),
    }


@router.get("/data-range")
async def data_range(
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Earliest and latest usage timestamp — powers the "data since X"
    subheader so users know how far back they can look.

    Uses the same filter scope as the rest of the Tokens page and avoids
    double-counting the overlapping raw/rollup ranges.
    """
    filters = _filters_from_query(model, project, source, task, command_type, session, agent)
    return await _trend(sm).data_range(filters=filters)


@router.get("/quota")
async def quota(
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Quota% + runway estimate based on hit_observation history."""
    from csm.modules.token.quota import QuotaEstimator
    return await QuotaEstimator(sm).estimate()


def _serialize_usage_snapshot(s) -> dict[str, Any] | None:
    if s is None:
        return None
    # Both agents share the same shape now — codex fills week_* + tier
    # from /status and leaves session_* None (no 5h window for codex).
    return {
        "ts": iso_utc(s.ts),
        "agent": getattr(s, "agent", "claude"),
        "session_pct": s.session_pct,
        "session_reset": s.session_reset,
        "week_pct": s.week_pct,
        "week_reset": s.week_reset,
        "tier": s.tier,
        "subscription_type": s.subscription_type,
        "source": s.source,
        "duration_ms": s.duration_ms,
        "error": s.error,
    }


@router.get("/usage-live")
async def usage_live(
    request: Request,
    agent: str = "claude",
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Latest usage panel snapshot for the given adapter.

    `agent` param selects which probe's latest snapshot to return.
    Both agents share the same shape (session_pct/session_reset/
    week_pct/week_reset/tier). Claude parses from `/usage`; codex from
    `/status`. Codex leaves session_* = None (no 5h window).

    Returns null-fields if no probe has landed yet plus staleness signals
    so the UI can decide whether to auto-refresh.
    """
    from csm.models import UsageSnapshot
    async with sm() as db:
        row = (await db.execute(
            select(UsageSnapshot)
            .where(UsageSnapshot.agent == agent)
            .order_by(UsageSnapshot.ts.desc())
            .limit(1)
        )).scalar_one_or_none()
    return {
        "latest": _serialize_usage_snapshot(row),
        "interval_min": getattr(request.app.state, "usage_scheduler", None)._interval_min
            if hasattr(request.app.state, "usage_scheduler") else 0,
    }


@router.post("/usage-live/refresh")
async def usage_live_refresh(
    request: Request,
    agent: str = "claude",
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Force a probe outside the scheduled cadence.

    `agent` picks which probe to run (`claude` or `codex`). Blocks until
    the probe finishes (~10-30s for claude, ~20-30s for codex — two
    /status roundtrips) so the UI can immediately reflect fresh numbers.
    """
    scheduler = getattr(request.app.state, "usage_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="usage scheduler not initialized")
    result = await scheduler.run_now(source="manual", agent=agent)
    # Read back the just-persisted row so the response shape matches GET.
    from csm.models import UsageSnapshot
    async with sm() as db:
        row = (await db.execute(
            select(UsageSnapshot)
            .where(UsageSnapshot.agent == agent)
            .order_by(UsageSnapshot.ts.desc())
            .limit(1)
        )).scalar_one_or_none()
    # Both agents populate week_pct on success. For claude session_pct
    # is also populated; codex leaves it None.
    return {
        "ok": bool(result.get("week_pct") is not None or result.get("session_pct") is not None),
        "latest": _serialize_usage_snapshot(row),
    }


@router.get("/history")
async def history(
    hours: int = 24,
    granularity: str = "hour",
    start: str | None = None,
    end: str | None = None,
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    filters = _filters_from_query(model, project, source, task, command_type, session, agent)
    items = await _trend(sm).history(
        hours=hours,
        granularity=granularity,
        start=_parse_dt(start),
        end=_parse_dt(end),
        filters=filters,
    )
    return {"items": items}


@router.get("/top")
async def top(
    scope: str = "session",
    hours: int = 5,
    limit: int = 10,
    sort_by: str = "cache_creation_tokens",
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    try:
        items = await _trend(sm).top_consumers(
            scope=scope,
            hours=hours,
            limit=limit,
            sort_by=sort_by,
            filters=_filters_from_query(model, project, source, task, command_type, session, agent),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"items": items, "scope": scope, "sort_by": sort_by}


@router.get("/tools/top")
async def tools_top(
    hours: int = 24,
    limit: int = 20,
    sort_by: str = "total_tokens",
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Top tool invocations aggregated by tool_name within the last N hours.

    Token columns are per-tool shares — when an assistant message has N tool_use
    blocks, that message's usage is split N-ways across the invocations (see
    EventStream._handle_record). sort_by ∈ {total_tokens, cost, count}.
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from csm.models import RawTokenEvent, ToolInvocation
    start = now_utc_naive() - timedelta(hours=hours)
    filters = _filters_from_query(model, project, source, task, command_type, session, agent)

    # tool_invocation has local columns for project/source/command_type/session,
    # but model + task_name live on raw_token_event. Split filters accordingly.
    _TOOL_COLS = {
        "project": ToolInvocation.project_path,
        "source": ToolInvocation.source,
        "command_type": ToolInvocation.command_type,
        "session": ToolInvocation.external_session_id,
    }

    def _apply_tool_filters(stmt):
        # Local WHEREs
        for k, col in _TOOL_COLS.items():
            v = filters.get(k)
            if v:
                stmt = stmt.where(col.in_(v) if isinstance(v, list) else col == v)
        # model / task via EXISTS on raw_token_event (same session, ts).
        rte_filter = {k: filters.get(k) for k in ("model", "task") if filters.get(k)}
        if rte_filter:
            sub = select(RawTokenEvent.id).where(
                RawTokenEvent.external_session_id == ToolInvocation.external_session_id,
                RawTokenEvent.ts == ToolInvocation.ts,
            )
            for k, v in rte_filter.items():
                col = RawTokenEvent.model if k == "model" else RawTokenEvent.task_name
                sub = sub.where(col.in_(v) if isinstance(v, list) else col == v)
            stmt = stmt.where(sub.exists())
        return stmt
    sort_exprs = {
        "total_tokens": (
            func.sum(ToolInvocation.input_tokens)
            + func.sum(ToolInvocation.cache_creation_tokens)
            + func.sum(ToolInvocation.cache_read_tokens)
            + func.sum(ToolInvocation.output_tokens)
        ),
        "cost": func.sum(ToolInvocation.estimated_cost_usd),
        "count": func.count(ToolInvocation.id),
    }
    if sort_by not in sort_exprs:
        raise HTTPException(status_code=400, detail=f"unknown sort_by: {sort_by}")
    async with sm() as db:
        stmt = (
            select(
                ToolInvocation.tool_name,
                func.count(ToolInvocation.id),
                func.count(func.distinct(ToolInvocation.external_session_id)),
                func.coalesce(func.sum(ToolInvocation.input_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.cache_creation_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.cache_read_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.output_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.estimated_cost_usd), 0.0),
            )
            .where(ToolInvocation.ts > start)
            .group_by(ToolInvocation.tool_name)
            .order_by(sort_exprs[sort_by].desc())
            .limit(limit)
        )
        stmt = _apply_tool_filters(stmt)
        rows = (await db.execute(stmt)).all()
        # Sanity aggregates: tool_total comes from this query's grand-sum (so the
        # caller can check the panel's own arithmetic), window_total comes from
        # raw_token_event in the same window — that's the Hero "总 token" number.
        tot_stmt = (
            select(
                func.coalesce(func.sum(ToolInvocation.input_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.cache_creation_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.cache_read_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.output_tokens), 0),
                func.coalesce(func.sum(ToolInvocation.estimated_cost_usd), 0.0),
                func.count(ToolInvocation.id),
            )
            .where(ToolInvocation.ts > start)
        )
        tot_stmt = _apply_tool_filters(tot_stmt)
        tot = (await db.execute(tot_stmt)).one()
        tool_total = int(tot[0] + tot[1] + tot[2] + tot[3])
        win_stmt = (
            select(
                func.coalesce(func.sum(RawTokenEvent.input_tokens), 0)
                + func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0)
                + func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0)
                + func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
            )
            .where(RawTokenEvent.ts > start)
        )
        win_stmt = _trend(sm)._apply_filters(win_stmt, filters)
        win = (await db.execute(win_stmt)).one()
        window_total = int(win[0])
        share_pct = (tool_total / window_total * 100.0) if window_total > 0 else 0.0
        return {
            "items": [
                {
                    "tool_name": r[0],
                    "count": int(r[1]),
                    "session_count": int(r[2]),
                    "input_tokens": int(r[3]),
                    "cache_creation_tokens": int(r[4]),
                    "cache_read_tokens": int(r[5]),
                    "output_tokens": int(r[6]),
                    "total_tokens": int(r[3] + r[4] + r[5] + r[6]),
                    "estimated_cost_usd": float(r[7]),
                }
                for r in rows
            ],
            "aggregate": {
                "tool_total_tokens": tool_total,
                "tool_total_cost_usd": float(tot[4]),
                "tool_invocation_count": int(tot[5]),
                "window_total_tokens": window_total,
                "window_total_cost_usd": float(win[1]),
                "share_pct": round(share_pct, 2),
            },
            "window_hours": hours,
            "sort_by": sort_by,
        }


@router.get("/spike-turns")
async def spike_turns(
    hours: int = 24,
    limit: int = 20,
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Top N assistant turns by (input + cc + cr + output) in the last N hours.

    Joins tool_invocation on (external_session_id, ts) to surface which tools
    fired at the same instant — typically 1 turn → N tool_use blocks share one
    raw_token_event row, so this tells you "this expensive turn was caused by
    Read of a big file" or "Bash with huge output".
    """
    from datetime import timedelta

    from sqlalchemy import select

    from csm.models import RawTokenEvent, ToolInvocation
    start = now_utc_naive() - timedelta(hours=hours)
    total_expr = (
        RawTokenEvent.input_tokens
        + RawTokenEvent.cache_creation_tokens
        + RawTokenEvent.cache_read_tokens
        + RawTokenEvent.output_tokens
    )
    filters = _filters_from_query(model, project, source, task, command_type, session, agent)
    # Reuse TrendQueries._apply_filters — same column mapping applies here.
    trend = _trend(sm)
    async with sm() as db:
        stmt = (
            select(
                RawTokenEvent.id,
                RawTokenEvent.ts,
                RawTokenEvent.external_session_id,
                RawTokenEvent.project_path,
                RawTokenEvent.model,
                RawTokenEvent.source,
                RawTokenEvent.input_tokens,
                RawTokenEvent.cache_creation_tokens,
                RawTokenEvent.cache_read_tokens,
                RawTokenEvent.output_tokens,
                RawTokenEvent.estimated_cost_usd,
                total_expr.label("total"),
            )
            .where(RawTokenEvent.ts > start)
            .order_by(total_expr.desc())
            .limit(limit)
        )
        stmt = trend._apply_filters(stmt, filters)
        rows = (await db.execute(stmt)).all()
        # Best-effort tool lookup per row: same (session, ts) typically.
        items: list[dict] = []
        for r in rows:
            tool_stmt = (
                select(ToolInvocation.tool_name)
                .where(
                    ToolInvocation.external_session_id == r[2],
                    ToolInvocation.ts == r[1],
                )
            )
            tnames = [t[0] for t in (await db.execute(tool_stmt)).all() if t[0]]
            items.append({
                "id": r[0],
                "ts": iso_utc(r[1]),
                "external_session_id": r[2],
                "project_path": r[3],
                "model": r[4],
                "source": r[5],
                "input_tokens": int(r[6]),
                "cache_creation_tokens": int(r[7]),
                "cache_read_tokens": int(r[8]),
                "output_tokens": int(r[9]),
                "total_tokens": int(r[11]),
                "estimated_cost_usd": float(r[10]),
                "tools": tnames,
            })
        return {"items": items, "window_hours": hours}


@router.get("/facets/{facet}")
async def facet(
    facet: str,
    hours: int = 24,
    limit: int = 50,
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    try:
        values = await _trend(sm).facet_values(facet=facet, hours=hours, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"facet": facet, "values": values}


@router.get("/export.csv")
async def export_csv(
    start: str | None = None,
    end: str | None = None,
    limit: int = 50000,
    model: list[str] | None = Query(default=None),
    project: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    task: list[str] | None = Query(default=None),
    command_type: list[str] | None = Query(default=None),
    session: list[str] | None = Query(default=None),
    agent: list[str] | None = Query(default=None),
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    rows = await _trend(sm).export_rows(
        start=_parse_dt(start),
        end=_parse_dt(end),
        filters=_filters_from_query(model, project, source, task, command_type, session, agent),
        limit=limit,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "ts", "external_session_id", "project_path", "model", "source", "task_name",
        "command_type", "input_tokens", "cache_creation_tokens", "cache_read_tokens",
        "output_tokens", "estimated_cost_usd",
    ])
    for r in rows:
        w.writerow([r[0].isoformat() + "Z" if r[0] else "", *[("" if x is None else x) for x in r[1:]]])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="token_events.csv"'},
    )


@router.get("/hit-observations")
async def hits(
    limit: int = 50,
    request: Request = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    return {"items": await _trend(sm).hit_observations(limit=limit)}


# AgentAlertRule endpoints live in csm.api.agent_alerts (Phase 1).
