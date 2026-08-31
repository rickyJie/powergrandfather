"""Agent-alert evaluator — per-rule polling loop.

For each enabled `AgentAlertRule`, run its Python `check(window)` script every
`poll_interval_sec` seconds. When the script fires and `cooldown_sec` has
elapsed since `last_fired_at`, we:

  1. Persist `last_fired_at` (so restart-in-progress doesn't cause a double-fire).
  2. If `rule.escalate`, ask the Phase-3 escalation module to build a rich
     agent-authored body. Otherwise use a plain "metric X crossed threshold"
     line built from the script's returned payload.
  3. Emit a `TOKEN_ALERT_TRIGGERED` event — NotificationBus
     converts it to `TOKEN_WARNING` and routes to InApp / Lark per the payload.

Design notes:
  - Each rule owns a dedicated asyncio.Task; reload() reconciles the set.
  - Sandbox failures set `rule.last_error` and skip firing (no spam on broken
    scripts).
  - Cooldown is per-rule; the previous v1 had a "critical bypasses cooldown"
    exception — we drop that since severity is gone from the new model.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import AgentAlertRule
from csm.modules.token.agent_alert.sandbox import run_check_script
from csm.modules.token.trend import TrendQueries
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


class AgentAlertEvaluator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        *,
        escalation_callback=None,   # async (rule, window, payload) -> dict | None
    ):
        self._sm = sessionmaker
        self._es = event_stream
        self._trend = TrendQueries(sessionmaker)
        self._escalate = escalation_callback   # Phase-3 wires this in
        # rule_id → (task, current poll_interval_sec). The interval is stored
        # alongside the task so `reload()` can detect a poll cadence change
        # and respawn immediately (otherwise the old task would sleep out its
        # entire previous interval before picking up the new one).
        self._tasks: dict[str, tuple[asyncio.Task, int]] = {}
        # rule_id → asyncio.Lock, serializes concurrent ticks on the same rule.
        # This prevents a "double-fire": a tick that has already read
        # last_fired_at (found cooldown OK) and is deep in a 10-60s claude -p
        # escalation call would otherwise let a fresh tick pass the same
        # cooldown gate and fire again.
        self._tick_locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._stop.clear()
        await self._normalize_legacy_agent_cadence()
        await self.reload()

    # Old escalate-default presets used to ship at poll=1800s / cooldown=3600s.
    # After the "所有默认 agent 相关调用频率变成 3h 一次" policy change we
    # bump the DEFAULTS to 10800/10800. Existing rules that were created
    # from those presets AND still carry the old defaults are auto-migrated
    # here; anything the user hand-tuned is left alone.
    _LEGACY_AGENT_POLL_SEC = 1800
    _LEGACY_AGENT_COOLDOWN_SEC = 3600
    _NORMALIZED_AGENT_SEC = 10800
    _AGENT_ESCALATE_PRESETS: frozenset[str] = frozenset({
        "session_burn", "cache_hit_drop", "total_tokens_warn",
    })

    async def _normalize_legacy_agent_cadence(self) -> None:
        async with self._sm() as db:
            rows = (await db.execute(select(AgentAlertRule))).scalars().all()
            bumped = 0
            for r in rows:
                preset_id = (r.rule_metadata or {}).get("preset_id")
                if preset_id not in self._AGENT_ESCALATE_PRESETS:
                    continue
                if (
                    r.poll_interval_sec == self._LEGACY_AGENT_POLL_SEC
                    and r.cooldown_sec == self._LEGACY_AGENT_COOLDOWN_SEC
                ):
                    r.poll_interval_sec = self._NORMALIZED_AGENT_SEC
                    r.cooldown_sec = self._NORMALIZED_AGENT_SEC
                    bumped += 1
            if bumped:
                await db.commit()
                log.info(
                    "agent-alert: normalized %d legacy escalate-preset rule(s) to 3h/3h",
                    bumped,
                )

    async def stop(self) -> None:
        self._stop.set()
        tasks = [t for t, _ in self._tasks.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._tick_locks.clear()

    async def reload(self) -> None:
        """Reconcile per-rule tasks against current DB state.

        Called on startup and by the API layer after any CRUD on rules.
        Idempotent — safe to invoke repeatedly.

        Reconciliation:
          * Rule removed / disabled → cancel task, drop lock.
          * Rule interval changed   → cancel + respawn immediately (otherwise
            the old task would sleep out its previous cadence before noticing).
          * New rule                → spawn.
        """
        if self._stop.is_set():
            return
        async with self._sm() as db:
            rows = (await db.execute(
                select(AgentAlertRule).where(AgentAlertRule.enabled.is_(True))
            )).scalars().all()
        want: dict[str, int] = {r.id: max(10, r.poll_interval_sec) for r in rows}

        # Cancel tasks for removed / disabled / interval-changed rules.
        for rid in list(self._tasks.keys()):
            task, cur_interval = self._tasks[rid]
            if rid not in want or want[rid] != cur_interval or task.done():
                task.cancel()
                self._tasks.pop(rid)
                # Keep the lock — a fresh task with new interval still wants
                # per-rule serialization. Only drop the lock when the rule
                # itself is going away.
                if rid not in want:
                    self._tick_locks.pop(rid, None)

        # Start tasks for new / respawned rules.
        for rid, interval in want.items():
            if rid in self._tasks:
                continue
            self._tasks[rid] = (
                asyncio.create_task(
                    self._rule_loop(rid, interval),
                    name=f"csm-agent-alert-{rid[:8]}",
                ),
                interval,
            )

    def set_escalation_callback(self, cb) -> None:
        """Late-bound wiring for the Phase-3 escalation coroutine."""
        self._escalate = cb

    # ---------- per-rule loop ----------

    async def _rule_loop(self, rule_id: str, initial_interval: int) -> None:
        # Small jitter avoids thundering-herd on multi-rule reload().
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            return  # stopped during warmup
        except TimeoutError:
            pass

        interval = max(10, initial_interval)
        while not self._stop.is_set():
            try:
                await self._tick_rule(rule_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("agent-alert rule %s tick failed: %s", rule_id, e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                # refresh interval in case a PATCH changed it
                interval = await self._current_interval(rule_id, fallback=interval)

    async def _current_interval(self, rule_id: str, *, fallback: int) -> int:
        async with self._sm() as db:
            row = await db.get(AgentAlertRule, rule_id)
        if row is None or not row.enabled:
            return fallback  # loop will exit on next tick when rule disappears
        return max(10, row.poll_interval_sec)

    async def _tick_rule(self, rule_id: str) -> None:
        # Per-rule lock: only one tick body may run at a time for a given rule.
        # Without this, a slow escalation (up to 60s claude -p) could allow the
        # next scheduled tick to pass the same cooldown gate and double-fire.
        lock = self._tick_locks.setdefault(rule_id, asyncio.Lock())
        async with lock:
            await self._tick_rule_locked(rule_id)

    async def _tick_rule_locked(self, rule_id: str) -> None:
        # Reload rule fresh each tick — the source of truth is DB, not memory.
        async with self._sm() as db:
            rule = await db.get(AgentAlertRule, rule_id)
        if rule is None or not rule.enabled:
            return  # will be cleaned up on next reload()

        now = now_utc_naive()

        # Snooze gate — user hit "静音 Nh" from the UI. Skip entirely (no
        # sandbox run, no last_error write) until the snooze expires.
        if rule.snoozed_until is not None and rule.snoozed_until > now:
            return

        # Cooldown gate.
        if rule.last_fired_at is not None and rule.cooldown_sec > 0:
            elapsed = (now - rule.last_fired_at).total_seconds()
            if elapsed < rule.cooldown_sec:
                return

        # Snapshot window.
        try:
            window = await self._trend.current_window(hours=5.0)
        except Exception as e:
            await self._record_error(rule.id, f"window snapshot: {e}")
            return

        # Sandbox run.
        result = await run_check_script(script=rule.check_script, window=window)
        if not result.ok:
            await self._record_error(rule.id, result.error or "unknown sandbox error")
            return

        # Clear stale error since this tick succeeded.
        if rule.last_error:
            await self._clear_error(rule.id)

        if not result.fired:
            return

        # Fired: persist last_fired_at BEFORE emitting so a crash mid-emit
        # doesn't re-fire on restart.
        await self._mark_fired(rule.id, now)

        # Build payload for NotificationBus.
        payload: dict = {
            "alert_rule_id": rule.id,
            "alert_name": rule.name,
            "nl_description": rule.nl_description,
            "channels": rule.channels or [],
            "escalate": rule.escalate,
            "actual": (result.payload or {}).get("actual"),
            "threshold": (result.payload or {}).get("threshold"),
            "metric": (result.payload or {}).get("metric", "custom"),
            "script_payload": result.payload,
            "window_snapshot": window,
            "notify_channel": rule.channels or [],
            "lark_chat_id": rule.lark_chat_id,
            "lark_user_id": rule.lark_user_id,
            "_dedup_key": f"agent_alert:{rule.id}",
        }

        # Phase-3 escalation: if enabled and callback wired in, ask the agent
        # for a richer body. Never fatal — degrade gracefully on failure.
        if rule.escalate:
            if self._escalate is None:
                log.warning(
                    "agent-alert %s: escalate=True but no escalation callback wired; "
                    "sending plain notification instead", rule.id,
                )
            else:
                try:
                    extra = await self._escalate(rule, window, result.payload or {})
                    if isinstance(extra, dict) and extra.get("title") and extra.get("body"):
                        payload["agent_summary"] = extra
                    elif extra is None:
                        log.info(
                            "agent-alert %s: escalation returned None — degrading to plain "
                            "notification (check csm.log for the escalate.py reason)", rule.id,
                        )
                    else:
                        log.warning(
                            "agent-alert %s: escalation returned malformed summary "
                            "(missing title/body); degrading to plain notification", rule.id,
                        )
                except Exception as e:
                    log.warning("agent-alert %s escalation failed: %s", rule.id, e)

        await self._es.emit(Event(
            type=EventType.TOKEN_ALERT_TRIGGERED,
            ts=datetime.now(UTC),
            session_id=None,
            project_path=None,
            payload=payload,
        ))

    async def _mark_fired(self, rule_id: str, when: datetime) -> None:
        async with self._sm() as db:
            row = await db.get(AgentAlertRule, rule_id)
            if row is not None:
                row.last_fired_at = when
                row.last_error = None
                await db.commit()

    async def _record_error(self, rule_id: str, message: str) -> None:
        async with self._sm() as db:
            row = await db.get(AgentAlertRule, rule_id)
            if row is not None:
                row.last_error = message[:1000]
                await db.commit()

    async def _clear_error(self, rule_id: str) -> None:
        async with self._sm() as db:
            row = await db.get(AgentAlertRule, rule_id)
            if row is not None and row.last_error:
                row.last_error = None
                await db.commit()
