"""Budget evaluation — per-scope token / cost allowances over a recurring period.

A budget answers "are we within the allowance for X over period Y?". Status is
recomputed on demand (cheap SQL aggregate) or on a 60s loop by BudgetEvaluator.
When a budget transitions from ok→warn or warn→breached, an event is emitted
that the NotificationBus translates into a token_warning notification (Phase 3
adds the Lark channel routing).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Budget, RawTokenEvent
from csm.models.budget import BudgetPeriod, BudgetScopeType
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


def period_window(period: BudgetPeriod, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return [start, end) for the period containing ``now``.

    Calendar-aligned: hourly snaps to top of hour, daily to UTC midnight, etc.
    WINDOW_5H is rolling, anchored on ``now``.
    """
    now = now or now_utc_naive()
    if period == BudgetPeriod.WINDOW_5H:
        return now - timedelta(hours=5), now
    if period == BudgetPeriod.HOURLY:
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if period == BudgetPeriod.DAILY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if period == BudgetPeriod.WEEKLY:
        # ISO week: Monday-Sunday.
        d = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=7)
    if period == BudgetPeriod.MONTHLY:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end
    raise ValueError(f"unknown period: {period}")


def _scope_filter(budget: Budget):
    """SQLAlchemy WHERE clause restricting rows to this budget's scope.

    GLOBAL → always true; otherwise match the raw_token_event column.
    """
    if budget.scope_type == BudgetScopeType.GLOBAL:
        return None
    col = {
        BudgetScopeType.PROJECT: RawTokenEvent.project_path,
        BudgetScopeType.TASK: RawTokenEvent.task_name,
        BudgetScopeType.SOURCE: RawTokenEvent.source,
        BudgetScopeType.MODEL: RawTokenEvent.model,
        # `external_session_id` is the CLI's own session id — the same column
        # every other token query groups by (trend, /api/tokens, aggregator),
        # so a session id copied out of an alert or the Tokens table resolves
        # here too. It was `claude_session_id` until the agent-abstraction
        # rename; see the test that now pins every scope.
        BudgetScopeType.SESSION: RawTokenEvent.external_session_id,
    }[budget.scope_type]
    if budget.scope_type == BudgetScopeType.MODEL and budget.scope_value:
        # match by family substring so users can write "opus" / "sonnet" / "haiku".
        return col.ilike(f"%{budget.scope_value}%")
    return col == budget.scope_value


async def compute_status(db, budget: Budget, now: datetime | None = None) -> dict[str, Any]:
    """Compute current consumption snapshot for a single budget."""
    start, end = period_window(budget.period, now)
    stmt = select(
        func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
        func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0),
        func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
        func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
        func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
        func.count(RawTokenEvent.id),
    ).where(RawTokenEvent.ts >= start, RawTokenEvent.ts < end)
    sf = _scope_filter(budget)
    if sf is not None:
        stmt = stmt.where(sf)
    res = (await db.execute(stmt)).one()
    in_t, cc, cr, out, cost, msgs = res
    total = int(in_t + cc + cr + out)
    pct_tokens = (
        total / budget.token_limit * 100.0
        if budget.token_limit and budget.token_limit > 0
        else None
    )
    pct_cost = (
        float(cost) / budget.cost_limit * 100.0
        if budget.cost_limit and budget.cost_limit > 0
        else None
    )
    # State: max(pct_tokens, pct_cost) vs 100 / warn_pct.
    eff_pct = max([p for p in (pct_tokens, pct_cost) if p is not None] or [0.0])
    if eff_pct >= 100.0:
        state = "breached"
    elif eff_pct >= budget.warn_pct:
        state = "warn"
    else:
        state = "ok"
    return {
        "budget_id": budget.id,
        "name": budget.name,
        "scope_type": budget.scope_type.value if hasattr(budget.scope_type, "value") else budget.scope_type,
        "scope_value": budget.scope_value,
        "period": budget.period.value if hasattr(budget.period, "value") else budget.period,
        "period_start": start.isoformat() + "Z",
        "period_end": end.isoformat() + "Z",
        "current_tokens": total,
        "current_cost_usd": float(cost),
        "msg_count": int(msgs),
        "token_limit": budget.token_limit,
        "cost_limit": budget.cost_limit,
        "pct_tokens": round(pct_tokens, 2) if pct_tokens is not None else None,
        "pct_cost": round(pct_cost, 2) if pct_cost is not None else None,
        "effective_pct": round(eff_pct, 2),
        "state": state,
        "warn_pct": budget.warn_pct,
        "action": budget.action.value if hasattr(budget.action, "value") else budget.action,
        "notify_channel": budget.notify_channel or [],
    }


class BudgetEvaluator:
    """Periodically evaluates all enabled budgets and fires notifications.

    On every tick (default 60s):
      1. Load all enabled `Budget` rows.
      2. For each budget call `compute_status()`.
      3. If state transitioned to warn or breached AND cooldown elapsed,
         emit a `BUDGET_BREACHED` event so the
         NotificationBus turns it into a TOKEN_WARNING notification (Phase 3
         also dispatches to Lark when configured).
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        interval_sec: float = 60.0,
    ):
        self._sm = sessionmaker
        self._es = event_stream
        self._interval = interval_sec
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="csm-budget-evaluator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.evaluate_once()
            except Exception:
                log.exception("budget evaluator tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
            else:
                break

    async def evaluate_once(self) -> int:
        """Run all enabled budgets; return number that fired this tick."""
        fired = 0
        async with self._sm() as db:
            budgets = list(
                (await db.execute(select(Budget).where(Budget.enabled.is_(True))))
                .scalars().all()
            )
            now = now_utc_naive()
            for b in budgets:
                status = await compute_status(db, b, now)
                new_state = status["state"]
                # Fire on transition to warn/breached, with cooldown.
                if new_state in ("warn", "breached") and new_state != b.last_state:
                    cd_ok = (
                        b.last_fired_at is None
                        or (now - b.last_fired_at).total_seconds() / 60.0 >= b.cooldown_minutes
                    )
                    if cd_ok:
                        await self._fire(b, status)
                        b.last_fired_at = now
                        fired += 1
                b.last_state = new_state
            await db.commit()
        return fired

    async def _fire(self, budget: Budget, status: dict[str, Any]) -> None:
        severity = "critical" if status["state"] == "breached" else "warn"
        await self._es.emit(Event(
            type=EventType.BUDGET_BREACHED,
            ts=datetime.now(UTC),
            session_id=None,
            project_path=None,
            payload={
                "budget_id": budget.id,
                "budget_name": budget.name,
                "state": status["state"],
                "effective_pct": status["effective_pct"],
                "current_tokens": status["current_tokens"],
                "current_cost_usd": status["current_cost_usd"],
                "token_limit": budget.token_limit,
                "cost_limit": budget.cost_limit,
                "scope_type": status["scope_type"],
                "scope_value": status["scope_value"],
                "period": status["period"],
                "notify_channel": status["notify_channel"],
                "severity": severity,
                "_dedup_key": f"budget:{budget.id}:{status['state']}",
                "_bypass_dedup": status["state"] == "breached",
            },
        ))
