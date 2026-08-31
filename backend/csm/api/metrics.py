"""Prometheus-format /metrics endpoint.

Exposes the most useful token-consumption counters and current budget/quota
gauges so external monitoring (Prometheus / Grafana / alerts) can scrape CSM
without going through the JSON API.

Metric naming follows Prometheus conventions: snake_case, units in suffix.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.models import Budget, RawTokenEvent
from csm.modules.token.budget import compute_status
from csm.modules.token.quota import QuotaEstimator
from csm.utils.time import now_utc_naive

router = APIRouter(tags=["metrics"])


def _esc(v: str | None) -> str:
    if v is None:
        return ""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(sm: async_sessionmaker = Depends(get_db_sessionmaker)) -> str:
    lines: list[str] = []

    # ---- 5h window aggregates by model ----
    now = now_utc_naive()
    start = now - timedelta(hours=5)
    async with sm() as db:
        rows = (await db.execute(
            select(
                RawTokenEvent.model,
                func.count(RawTokenEvent.id),
                func.coalesce(func.sum(RawTokenEvent.input_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.output_tokens), 0),
                func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0),
            ).where(RawTokenEvent.ts > start).group_by(RawTokenEvent.model)
        )).all()

    lines.append("# HELP csm_tokens_5h Sum of tokens by category over rolling 5h window.")
    lines.append("# TYPE csm_tokens_5h gauge")
    lines.append("# HELP csm_messages_5h Number of assistant messages over rolling 5h window.")
    lines.append("# TYPE csm_messages_5h gauge")
    lines.append("# HELP csm_cost_5h_usd Estimated USD cost over rolling 5h window.")
    lines.append("# TYPE csm_cost_5h_usd gauge")
    for model, msgs, in_t, cc, cr, out, cost in rows:
        m = _esc(model or "unknown")
        lines.append(f'csm_messages_5h{{model="{m}"}} {int(msgs)}')
        lines.append(f'csm_tokens_5h{{model="{m}",kind="input"}} {int(in_t)}')
        lines.append(f'csm_tokens_5h{{model="{m}",kind="cache_creation"}} {int(cc)}')
        lines.append(f'csm_tokens_5h{{model="{m}",kind="cache_read"}} {int(cr)}')
        lines.append(f'csm_tokens_5h{{model="{m}",kind="output"}} {int(out)}')
        lines.append(f'csm_cost_5h_usd{{model="{m}"}} {float(cost):.6f}')

    # ---- Quota runway (no percentage — see ADR-0001) ----
    quota = await QuotaEstimator(sm).estimate()
    lines.append("# HELP csm_runway_minutes Estimated minutes until oldest 5h-window tokens age out.")
    lines.append("# TYPE csm_runway_minutes gauge")
    if quota.get("runway_minutes") is not None:
        lines.append(f'csm_runway_minutes {float(quota["runway_minutes"]):.2f}')

    # ---- Budgets ----
    async with sm() as db:
        budgets = list(
            (await db.execute(select(Budget).where(Budget.enabled.is_(True)))).scalars().all()
        )
        lines.append("# HELP csm_budget_pct Effective consumption percentage per budget.")
        lines.append("# TYPE csm_budget_pct gauge")
        lines.append("# HELP csm_budget_tokens Current token consumption per budget.")
        lines.append("# TYPE csm_budget_tokens gauge")
        lines.append("# HELP csm_budget_cost_usd Current USD cost per budget.")
        lines.append("# TYPE csm_budget_cost_usd gauge")
        for b in budgets:
            s = await compute_status(db, b)
            labels = (
                f'name="{_esc(b.name)}",'
                f'scope_type="{_esc(s["scope_type"])}",'
                f'scope_value="{_esc(s["scope_value"] or "")}",'
                f'period="{_esc(s["period"])}",'
                f'state="{_esc(s["state"])}"'
            )
            lines.append(f'csm_budget_pct{{{labels}}} {s["effective_pct"]:.4f}')
            lines.append(f'csm_budget_tokens{{{labels}}} {s["current_tokens"]}')
            lines.append(f'csm_budget_cost_usd{{{labels}}} {s["current_cost_usd"]:.6f}')

    return "\n".join(lines) + "\n"
