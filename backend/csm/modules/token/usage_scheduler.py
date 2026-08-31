"""Background scheduler for the CLI-adapter usage probes.

Runs periodically. Each tick fires an independent probe per adapter:
    - claude: `usage_polling.UsagePoller` → `/usage` slash-cmd (session%
      + week% + tier)
    - codex:  `codex_usage_probe.CodexUsagePoller` → `/status` slash-cmd
      (week% + tier). Codex has no 5h session window.

Both probes are per-agent — a claude-only user sees the claude card;
codex-only user sees the codex card; a user with both installed gets
both persisted and the Tokens page picks which to render per scope.

Persistence goes to `usage_snapshot` with `agent` discriminator so the
API can pull "latest per agent". Codex writes into the same shared
columns as claude (session_pct=None, week_pct, tier, ...) so the API
serializer and frontend render both agents through one code path.
Persistence + probe are best-effort — a crashed probe writes a marker
snapshot with `error=` so the UI can render "N/A" instead of stale
numbers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import UsageSnapshot
from csm.modules.token.codex_usage_probe import CodexUsagePoller, CodexUsageProbeResult
from csm.modules.token.usage_polling import UsagePoller, UsageProbeResult
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


class UsageScheduler:
    """Periodically runs claude + codex `/usage` probes independently.

    M14: was claude-only; now dispatches per adapter. Both probes fire
    each tick — a slow / crashed one doesn't block the other.
    """

    def __init__(
        self,
        poller: UsagePoller,
        sessionmaker: async_sessionmaker,
        interval_min: int = 30,
        adapter_registry=None,   # AdapterRegistry | None
        codex_poller: CodexUsagePoller | None = None,
    ):
        self._poller = poller
        # Codex poller is constructed here (not injected) since it takes
        # no external state. Test can pass a mock via `codex_poller=`.
        self._codex_poller = codex_poller or CodexUsagePoller()
        self._sm = sessionmaker
        self._interval_min = int(interval_min)
        self._registry = adapter_registry
        self._probe_by_agent = {
            "claude": self._probe_claude_once,
            "codex": self._probe_codex_once,
        }
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._interval_min <= 0:
            log.info("UsageScheduler: interval=%s, scheduler disabled (manual only)",
                     self._interval_min)
            return
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="usage-poller")
        log.info("UsageScheduler: started, interval=%d min", self._interval_min)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            interval_sec = self._interval_min * 60
            first_delay = 15
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=first_delay)
                return
            except TimeoutError:
                pass
            await self._probe_all(source='startup')
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=interval_sec)
                    return
                except TimeoutError:
                    pass
                await self._probe_all(source='scheduled')
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "UsageScheduler: loop crashed — UI will stall on stale/empty snapshots. "
                "Restart the backend to recover."
            )

    async def run_now(
        self, source: str = 'manual', agent: str = 'claude',
    ) -> dict[str, Any]:
        """Force a single-agent probe outside the scheduled cadence.

        M14: `agent` picks which probe to run. Default 'claude' matches
        the legacy behaviour so existing API callers stay compatible.
        Returns the probe result as a dict for API serialisation.
        """
        probe = self._probe_by_agent.get(agent)
        if probe is None:
            return {}
        r = await probe(source)
        return r.to_dict() if hasattr(r, 'to_dict') else {}

    async def _probe_all(self, source: str) -> None:
        """Fire both probes independently. Errors in one don't affect the other."""
        # Run in parallel; each has its own DB write so partial failure
        # still persists whatever succeeded.
        await asyncio.gather(
            self._probe_claude_once(source),
            self._probe_codex_once(source),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Claude branch
    # ------------------------------------------------------------------

    async def _claude_skip_reason(self) -> str | None:
        return await _adapter_skip_reason(self._registry, "claude")

    async def _probe_claude_once(self, source: str) -> UsageProbeResult:
        skip = await self._claude_skip_reason()
        if skip:
            r = UsageProbeResult(
                session_pct=None, session_reset=None,
                week_pct=None, week_reset=None,
                tier=None, subscription_type=None,
                raw_pane='', error=f'skip: {skip}',
                duration_ms=0, fetched_at=now_utc_naive(),
            )
        else:
            try:
                r = await self._poller.probe()
            except Exception as e:
                log.exception("UsageScheduler: claude probe crashed")
                r = UsageProbeResult(
                    session_pct=None, session_reset=None,
                    week_pct=None, week_reset=None,
                    tier=None, subscription_type=None,
                    raw_pane='', error=f'crash: {e.__class__.__name__}: {e}',
                    duration_ms=0, fetched_at=now_utc_naive(),
                )
        try:
            async with self._sm() as db:
                db.add(UsageSnapshot(
                    ts=r.fetched_at,
                    agent='claude',
                    session_pct=r.session_pct,
                    session_reset=r.session_reset,
                    week_pct=r.week_pct,
                    week_reset=r.week_reset,
                    tier=r.tier,
                    subscription_type=r.subscription_type,
                    source=source,
                    duration_ms=r.duration_ms,
                    error=r.error,
                    raw_pane=r.raw_pane if r.error else None,
                ))
                await db.commit()
        except Exception:
            log.exception("UsageScheduler: failed to persist claude snapshot")
        return r

    # ------------------------------------------------------------------
    # Codex branch (M14)
    # ------------------------------------------------------------------

    async def _codex_skip_reason(self) -> str | None:
        return await _adapter_skip_reason(self._registry, "codex")

    async def _probe_codex_once(self, source: str) -> CodexUsageProbeResult:
        skip = await self._codex_skip_reason()
        if skip:
            r = CodexUsageProbeResult(
                session_pct=None, session_reset=None,
                week_pct=None, week_reset=None,
                tier=None, subscription_type=None,
                raw_pane='', error=f'skip: {skip}',
                duration_ms=0, fetched_at=now_utc_naive(),
            )
        else:
            try:
                r = await self._codex_poller.probe()
            except Exception as e:
                log.exception("UsageScheduler: codex probe crashed")
                r = CodexUsageProbeResult(
                    session_pct=None, session_reset=None,
                    week_pct=None, week_reset=None,
                    tier=None, subscription_type=None,
                    raw_pane='', error=f'crash: {e.__class__.__name__}: {e}',
                    duration_ms=0, fetched_at=now_utc_naive(),
                )
        # Codex writes into the SAME shared columns claude uses (no
        # session window, so those stay None). Legacy codex_lifetime/peak/
        # streak/longest columns are left NULL — kept in schema for
        # backward compatibility but no longer written.
        try:
            async with self._sm() as db:
                db.add(UsageSnapshot(
                    ts=r.fetched_at,
                    agent='codex',
                    session_pct=r.session_pct,
                    session_reset=r.session_reset,
                    week_pct=r.week_pct,
                    week_reset=r.week_reset,
                    tier=r.tier,
                    subscription_type=r.subscription_type,
                    source=source,
                    duration_ms=r.duration_ms,
                    error=r.error,
                    raw_pane=r.raw_pane if r.error else None,
                ))
                await db.commit()
        except Exception:
            log.exception("UsageScheduler: failed to persist codex snapshot")
        return r


async def _adapter_skip_reason(registry, name: str) -> str | None:
    """Shared skip check: adapter must be registered AND usable."""
    if registry is None:
        return None  # legacy path — always try
    try:
        if name not in registry:
            return f"no {name} adapter registered"
        adapter = registry.get(name)
    except Exception:
        return f"no {name} adapter registered"
    try:
        st = adapter.probe()
    except Exception:
        return f"{name} adapter probe failed"
    if not st.usable:
        return f"{name} adapter not usable"
    return None
