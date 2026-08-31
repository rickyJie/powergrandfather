"""Quota runway estimation.

Per ADR-0001 (`docs/decisions/0001-no-quota-percentage.md`), CSM does NOT
estimate a quota percentage — Anthropic does not publish the denominator
and any hardcoded ceiling would be misleading precision. The old
`DEFAULT_CEILING_TOKENS = 220_000_000`, `current_pct` field, and
`CSM_QUOTA_CEILING_TOKENS` env override were removed 2026-07-25 in the
consolidation sweep.

What we still surface:
- Absolute 5h-window token totals (raw counts, no ratio).
- Burn rate over the last 10 min (spiky usage shows up quickly).
- Rolling-window reset ETA: the oldest event in the 5h window ages out
  5h after it landed; that's the earliest moment headroom recovers.
- Hit-observation count + confidence tier (informational only, no
  ceiling calibration since we no longer keep one).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import HitObservation, RawTokenEvent
from csm.utils.time import now_utc_naive


class QuotaEstimator:
    """Read-only computation from raw_token_event + hit_observation."""

    def __init__(self, sessionmaker: async_sessionmaker, min_observations: int = 3):
        self._sm = sessionmaker
        self._min_n = min_observations

    async def estimate(self) -> dict[str, Any]:
        """Return absolute usage, burn rate, window reset ETA, and confidence.

        No quota percentage is returned — see ADR-0001.
        """
        from sqlalchemy import func

        now = now_utc_naive()
        start_5h = now - timedelta(hours=5)

        async with self._sm() as db:
            cur = (await db.execute(
                select(
                    func.coalesce(func.sum(RawTokenEvent.input_tokens), 0)
                    + func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0)
                    + func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0)
                    + func.coalesce(func.sum(RawTokenEvent.output_tokens), 0)
                ).where(RawTokenEvent.ts > start_5h, RawTokenEvent.ts <= now)
            )).scalar() or 0
            # Earliest event ts within the 5h window — used to estimate when
            # the oldest token in the window "ages out" (rolling reset).
            earliest = (await db.execute(
                select(func.min(RawTokenEvent.ts)).where(RawTokenEvent.ts > start_5h, RawTokenEvent.ts <= now)
            )).scalar()
            # Burn rate over last 10 min — more reactive than 30 min.
            start_10m = now - timedelta(minutes=10)
            burn = (await db.execute(
                select(
                    func.coalesce(func.sum(RawTokenEvent.input_tokens), 0)
                    + func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0)
                    + func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0)
                    + func.coalesce(func.sum(RawTokenEvent.output_tokens), 0)
                ).where(RawTokenEvent.ts > start_10m, RawTokenEvent.ts <= now)
            )).scalar() or 0
            # Confidence signal only; ceiling calibration is no longer done.
            obs_rows = (await db.execute(select(HitObservation).order_by(HitObservation.ts.desc()).limit(30))).scalars().all()

        burn_per_min = burn / 10.0 if burn > 0 else 0.0
        # Rolling reset ETA: oldest event ages out 5h after it landed.
        window_reset_in_min: float | None = None
        if earliest is not None:
            ages_out_at = earliest + timedelta(hours=5)
            delta = (ages_out_at - now).total_seconds() / 60.0
            window_reset_in_min = round(max(0.0, delta), 1)
        # Runway degrades to just the window reset — we have no ceiling to
        # burn against (ADR-0001).
        runway_min = window_reset_in_min

        n_obs = len(obs_rows)
        if n_obs >= 10:
            confidence = "high"
        elif n_obs >= 5:
            confidence = "medium"
        elif n_obs >= self._min_n:
            confidence = "low"
        else:
            confidence = "insufficient-data"

        return {
            "n_observations": n_obs,
            "current_tokens": int(cur),
            "burn_per_min": round(burn_per_min, 1),
            "runway_minutes": runway_min,
            "window_reset_in_minutes": window_reset_in_min,
            "confidence": confidence,
        }
