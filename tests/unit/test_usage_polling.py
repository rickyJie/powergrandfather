"""Tests for `UsagePoller` wall-clock timeout guard (D5).

The pexpect probe inside `_probe_sync` runs on a worker thread via
`asyncio.to_thread`. That thread cannot be killed from the outside, so a
hung pexpect read would permanently occupy a slot in the default
10-worker thread pool. `UsagePoller.probe` wraps the `to_thread` call in
`asyncio.wait_for` with a wall-clock bound; on timeout it returns None
and logs a warning (thread is knowingly leaked — better than blocking
every future poller tick).
"""
from __future__ import annotations

import asyncio
import time

import pytest
from csm.config import settings
from csm.modules.token import usage_polling
from csm.modules.token.usage_polling import UsagePoller


@pytest.mark.asyncio
async def test_probe_wall_clock_timeout_returns_none(monkeypatch):
    """When _probe_sync hangs past the wall-clock guard, the async wrapper
    must return None (not raise, not block forever).

    Strategy: monkeypatch `asyncio.wait_for` inside the `usage_polling`
    module so the +10s buffer is bypassed in the test. We stub `_probe_sync`
    with a blocking sleep to model the hung pexpect thread.
    """

    def _hang_forever(*_a, **_kw):
        # Simulate pexpect blocking read that never returns.
        time.sleep(30)
        return "should never see this"

    monkeypatch.setattr(usage_polling, "_probe_sync", _hang_forever)
    # Force a tiny effective wall-clock (0.3s) regardless of the +10 buffer
    # so the test finishes quickly.
    original_wait_for = asyncio.wait_for

    async def short_wait_for(coro, timeout):
        return await original_wait_for(coro, timeout=0.3)

    monkeypatch.setattr(usage_polling.asyncio, "wait_for", short_wait_for)

    poller = UsagePoller()
    started = time.monotonic()
    result = await poller.probe()
    elapsed = time.monotonic() - started

    assert result is None, "probe should return None on wall-clock timeout"
    # Should return well before the fake sleep(30), proving the timeout fired.
    assert elapsed < 2.0, f"probe took {elapsed:.2f}s — timeout did not fire"


@pytest.mark.asyncio
async def test_probe_returns_result_when_sync_completes(monkeypatch):
    """Sanity: when _probe_sync returns quickly, the wrapper propagates the
    result and does not swallow it as a timeout."""

    fake_result = usage_polling.UsageProbeResult(
        session_pct=42,
        session_reset=None,
        week_pct=17,
        week_reset=None,
        tier=None,
        subscription_type=None,
        raw_pane="",
        error=None,
        duration_ms=5,
        fetched_at=usage_polling.datetime.utcnow(),
    )

    def _fast(*_a, **_kw):
        return fake_result

    monkeypatch.setattr(usage_polling, "_probe_sync", _fast)
    monkeypatch.setattr(settings, "usage_probe_timeout_sec", 60)

    poller = UsagePoller()
    result = await poller.probe()

    assert result is fake_result
    assert result.session_pct == 42
