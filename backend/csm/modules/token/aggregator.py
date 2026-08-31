"""Token aggregator — subscribes to Event Stream and persists usage + hit events.

Ports the spirit of <REPOS>/claude-learn/monitor/aggregator.py:
- Per-message usage with input / cache_creation / cache_read / output split
- USD estimation by model family
- Rate-limit hit observations with 5h window snapshot

Reuses Event Stream as the upstream data source (no second JSONL tail).
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import HitObservation, RawTokenEvent, Session

log = logging.getLogger(__name__)
_LEGACY_DEFAULT_AGENT = "claude"


# List prices in USD / 1M tokens — defaults if no PricingConfig row overrides.
# Anthropic families (claude): opus / sonnet / haiku.
# OpenAI-ish families (codex): gpt-4o / gpt-4o-mini / o3 / o3-mini. Rates as
# of 2026-07 public pricing; user-visible cost is only ever an estimate —
# a `PricingConfig` DB row overrides per family on demand.
RATES = {
    # Anthropic — Claude
    "opus":         {"in": 15.0,  "cc": 18.75, "cr": 1.5,  "out": 75.0},
    "sonnet":       {"in": 3.0,   "cc": 3.75,  "cr": 0.3,  "out": 15.0},
    "haiku":        {"in": 1.0,   "cc": 1.25,  "cr": 0.1,  "out": 5.0},
    # OpenAI — Codex (public per-1M pricing; approximate)
    "gpt-4o":       {"in": 5.0,   "cc": 5.0,   "cr": 2.5,  "out": 15.0},
    "gpt-4o-mini":  {"in": 0.15,  "cc": 0.15,  "cr": 0.075, "out": 0.6},
    "gpt-4.1":      {"in": 2.0,   "cc": 2.0,   "cr": 1.0,  "out": 8.0},
    "o3":           {"in": 15.0,  "cc": 15.0,  "cr": 7.5,  "out": 60.0},
    "o3-mini":      {"in": 3.0,   "cc": 3.0,   "cr": 1.5,  "out": 12.0},
    # Fallback for unknown codex/openai models — non-zero so users see
    # *something* on the cost dashboard rather than treating codex as free.
    "codex":        {"in": 3.0,   "cc": 3.0,   "cr": 1.5,  "out": 12.0},
}


# Module-level reference to the currently active TokenAggregator, populated by
# `TokenAggregator.__init__`. Used only by the backward-compat shims below so
# that `estimate_cost(...)` and `refresh_pricing_overrides(sm)` still work from
# `api/pricing.py` and `tool_logger.py` without threading the aggregator handle
# through every call site. Aggregator instance owns the mutable overrides dict.
_ACTIVE_AGGREGATOR: TokenAggregator | None = None


def model_family(name: str | None) -> str:
    """Return the pricing family key for a model name.

    Recognises both Anthropic (Claude) and OpenAI-ish (Codex) model
    families. Unknown models fall back to `sonnet` (historical default
    for pre-multi-agent code) UNLESS the name looks OpenAI-ish, in
    which case it falls back to `codex` — that way unknown codex model
    names get a non-zero price estimate instead of Claude's Sonnet rate.
    """
    n = (name or "").lower()
    # Anthropic families
    if "opus" in n:
        return "opus"
    if "haiku" in n:
        return "haiku"
    if "sonnet" in n:
        return "sonnet"
    # OpenAI / codex families
    if "gpt-4o-mini" in n:
        return "gpt-4o-mini"
    if "gpt-4o" in n:
        return "gpt-4o"
    if "gpt-4.1" in n:
        return "gpt-4.1"
    if "o3-mini" in n:
        return "o3-mini"
    if "o3" in n:
        return "o3"
    # Anything else starting with gpt- / o- / containing "codex" is
    # treated as an unknown-OpenAI model → codex fallback.
    if n.startswith("gpt-") or n.startswith("o-") or "codex" in n:
        return "codex"
    # Historic default for unknown Anthropic-shaped names.
    return "sonnet"


def estimate_cost(input_t: int, cc: int, cr: int, out: int, model: str | None) -> float:
    """Cost in USD for a single message. Uses active aggregator's overrides if set."""
    fam = model_family(model)
    overrides = _ACTIVE_AGGREGATOR._pricing_overrides if _ACTIVE_AGGREGATOR is not None else {}
    r = overrides.get(fam) or RATES.get(fam) or RATES["sonnet"]
    return (
        input_t * r["in"]
        + cc * r["cc"]
        + cr * r["cr"]
        + out * r["out"]
    ) / 1_000_000.0


async def refresh_pricing_overrides(sessionmaker) -> int:
    """Backward-compat shim: reload PricingConfig rows into the active aggregator.

    If no aggregator has been constructed yet (e.g. during early lifespan
    bootstrap before TokenAggregator.__init__ runs) this is a no-op returning 0.
    """
    if _ACTIVE_AGGREGATOR is None:
        log.warning("refresh_pricing_overrides called with no active TokenAggregator; skipping")
        return 0
    return await _ACTIVE_AGGREGATOR.refresh_pricing_overrides(sessionmaker)


class TokenAggregator:
    """Persists Claude token usage and rate-limit-hit observations.

    Subscribes to two Event Stream types:
      - `USAGE_RECORDED` → writes a `RawTokenEvent` row with input / cache /
        output token counts and an estimated USD cost (per `RATES`).
      - `RATE_LIMIT_HIT` → snapshots all RawTokenEvents in the prior 5h
        window into a single `HitObservation` row. This is the long-term
        ground truth used by the (deferred-to-v2) quota%-estimation
        analysis.

    No public state. `start()` / `stop()` manage the subscriptions; all
    persistence happens inside the event handlers.
    """

    def __init__(self, sessionmaker: async_sessionmaker, event_stream: EventStream):
        self._sm = sessionmaker
        self._es = event_stream
        self._sub_id: str | None = None
        self._hit_sub_id: str | None = None
        # Instance-owned pricing override cache (was module-level dict).
        # Populated by `refresh_pricing_overrides()`.
        self._pricing_overrides: dict[str, dict[str, float]] = {}
        # Register as the current active aggregator so module-level shims
        # (`estimate_cost`, module `refresh_pricing_overrides`) can find us.
        global _ACTIVE_AGGREGATOR
        _ACTIVE_AGGREGATOR = self

    async def refresh_pricing_overrides(self, sessionmaker=None) -> int:
        """Reload PricingConfig rows into this aggregator's override cache."""
        from csm.models import PricingConfig
        sm = sessionmaker or self._sm
        async with sm() as db:
            rows = list((await db.execute(select(PricingConfig))).scalars().all())
        new_map = {
            r.model_family: {
                "in": r.input_per_million,
                "cc": r.cache_creation_per_million,
                "cr": r.cache_read_per_million,
                "out": r.output_per_million,
            }
            for r in rows
        }
        # Assign atomically rather than clear()+update() so concurrent readers
        # never observe a half-cleared dict (protects a future to_thread path).
        self._pricing_overrides = new_map
        return len(new_map)

    def estimate_cost(self, input_t: int, cc: int, cr: int, out: int, model: str | None) -> float:
        fam = model_family(model)
        r = self._pricing_overrides.get(fam) or RATES.get(fam) or RATES["sonnet"]
        return (
            input_t * r["in"]
            + cc * r["cc"]
            + cr * r["cr"]
            + out * r["out"]
        ) / 1_000_000.0

    async def start(self) -> None:
        self._sub_id = self._es.subscribe([EventType.USAGE_RECORDED], self._on_usage)
        self._hit_sub_id = self._es.subscribe([EventType.RATE_LIMIT_HIT], self._on_rate_limit_hit)

    async def stop(self) -> None:
        if self._sub_id:
            self._es.unsubscribe(self._sub_id)
            self._sub_id = None
        if self._hit_sub_id:
            self._es.unsubscribe(self._hit_sub_id)
            self._hit_sub_id = None
        # Deregister as active aggregator on shutdown (test cleanliness).
        global _ACTIVE_AGGREGATOR
        if _ACTIVE_AGGREGATOR is self:
            _ACTIVE_AGGREGATOR = None

    async def _on_usage(self, event: Event) -> None:
        payload: dict[str, Any] = event.payload or {}
        in_t = int(payload.get("input_tokens", 0))
        cc = int(payload.get("cache_creation_input_tokens", 0))
        cr = int(payload.get("cache_read_input_tokens", 0))
        out = int(payload.get("output_tokens", 0))
        model = payload.get("model")
        cost = self.estimate_cost(in_t, cc, cr, out, model)
        is_subagent = bool(payload.get("is_subagent"))
        async with self._sm() as db:
            attribution = await self._lookup_attribution(db, event.session_id, is_subagent)
            values = {
                "id": str(uuid.uuid4()),
                "ts": _to_naive(event.ts),
                "external_session_id": event.session_id,
                "project_path": event.project_path,
                "model": model,
                "input_tokens": in_t,
                "cache_creation_tokens": cc,
                "cache_read_tokens": cr,
                "output_tokens": out,
                "estimated_cost_usd": cost,
                "is_subagent": is_subagent,
                "source": attribution["source"],
                "task_name": attribution["task_name"],
                "command_type": attribution["command_type"],
                "csm_session_id": attribution["csm_session_id"],
                # M12: agent scope for Tokens page filter.
                "agent": (
                    attribution["agent"]
                    or payload.get("agent")
                    or _LEGACY_DEFAULT_AGENT
                ),
                "jsonl_offset": event.source_offset,
            }
            # ON CONFLICT DO NOTHING against ux_rte_session_offset makes
            # replay after a lost offset-flush window a no-op instead of
            # double-counting tokens. SQLite requires the partial index's
            # WHERE clause to be spelled out on the conflict target.
            stmt = sqlite_insert(RawTokenEvent).values(**values)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["external_session_id", "jsonl_offset"],
                index_where=(
                    RawTokenEvent.external_session_id.is_not(None)
                    & RawTokenEvent.jsonl_offset.is_not(None)
                ),
            )
            await db.execute(stmt)
            await db.commit()

    async def _lookup_attribution(
        self,
        db,
        external_session_id: str | None,
        is_subagent: bool,
    ) -> dict[str, str | None]:
        """Resolve source / task_name / csm_session_id from external_session_id.

        If subagent → command_type=subagent. Else direct.
        P2 (workflow-only): `task_name` used to come from a TaskDefinition
        row associated with the Run; since M4 is retired the field is
        always None here. Mission-name based attribution can be added
        later via Run.mission_id → Mission.workflow_def_id → name if the
        rollup UI needs it.
        """
        command_type = "subagent" if is_subagent else "direct"
        if not external_session_id:
            return {
                "source": "unknown",
                "task_name": None,
                "command_type": command_type,
                "csm_session_id": None,
                "agent": None,
            }
        sess = (await db.execute(
            select(Session).where(
                Session.external_session_id == external_session_id,
                Session.superseded_by.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if sess is None:
            return {
                "source": "manual_external",
                "task_name": None,
                "command_type": command_type,
                "csm_session_id": None,
                "agent": None,
            }
        source = sess.type.value if hasattr(sess.type, "value") else str(sess.type)
        return {
            "source": source,
            "task_name": None,  # M4 retired; see docstring
            "command_type": command_type,
            "csm_session_id": sess.id,
            "agent": sess.agent,
        }

    async def _on_rate_limit_hit(self, event: Event) -> None:
        """Snapshot the 5h window ending at the hit moment."""
        ts = _to_naive(event.ts)
        t_start = ts - timedelta(hours=5)
        async with self._sm() as db:
            stmt = select(RawTokenEvent).where(
                RawTokenEvent.ts > t_start,
                RawTokenEvent.ts <= ts,
            )
            res = await db.execute(stmt)
            rows = list(res.scalars().all())
            in_sum = sum(r.input_tokens for r in rows)
            cc_sum = sum(r.cache_creation_tokens for r in rows)
            cr_sum = sum(r.cache_read_tokens for r in rows)
            out_sum = sum(r.output_tokens for r in rows)
            obs = HitObservation(
                ts=ts,
                reset_text=(event.payload or {}).get("reset_text"),
                msg_count_5h=len(rows),
                cc_tokens_5h=cc_sum,
                cr_tokens_5h=cr_sum,
                input_tokens_5h=in_sum,
                output_tokens_5h=out_sum,
                raw_session_id=event.session_id,
            )
            db.add(obs)
            await db.commit()


def _to_naive(ts: datetime) -> datetime:
    """SQLite DateTime columns are naive; strip tz."""
    if ts.tzinfo is not None:
        return ts.astimezone(UTC).replace(tzinfo=None)
    return ts
