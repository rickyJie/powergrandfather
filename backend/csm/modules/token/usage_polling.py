"""Probe Claude Code's built-in `/usage` slash command and parse the output.

Rationale
---------
Claude Code's `/usage` panel shows 5h + weekly quota % + reset timestamps. It is
computed client-side in the CLI (no reachable HTTP endpoint we can call). To
mirror those numbers in CSM we spawn a short-lived `claude` interactive
session via pexpect, type `/usage`, capture the rendered pane, and regex the
four fields out.

Notes on the interaction protocol (learned empirically):
- `claude -p '/usage'` is refused (`/usage isn't available in this environment`)
  so we must run interactive.
- Ink's keypress handler drops chars if we bulk-send the slash command; type
  char-by-char with a small delay.
- `sendline('/usage')` submits `\\n` which is intercepted by the slash-command
  auto-complete overlay. Send the command, wait, then send `\\r` (CR) to
  commit.
- Ink repaints the pane by cursor motion (lots of `\\r`), so mid-flight the
  string "Resets" can briefly appear overdrawn as "Reses". Wait for the
  "Esc to cancel" footer as a stable-render marker, then drain a small grace
  period, then parse.

Each probe uses its own tempdir (auto-cleaned in finally) so nothing lingers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pexpect

from csm.config import settings
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


ANSI_RE = re.compile(r'\x1b(\[[0-9;?]*[a-zA-Z]|\][^\x07]*\x07|[()][A-B0-2]|[=>])')

# A rendered quota line ("19% used"). Presence == the network fetch resolved;
# the "Current session" label paints *before* the fetch, so waiting on the
# label alone races the loader and scrapes an empty pane.
PCT_USED_RE = re.compile(r'\d{1,3}\s*%\s*used')

# The CLI's own "fetch failed" footers. "Could not refresh" can co-exist with
# stale cached numbers (warm sessions); only treat as failure when no % renders.
FETCH_ERR_RE = re.compile(r'(Failed to load usage data|Could not refresh usage data)')

TRAFFIC_FLAG = 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub('', s)


def _with_traffic_override(claude_cmd: str, probe_cwd: str) -> tuple[str, dict[str, str]]:
    """Return (spawn_cmd, env) that force the usage-limits fetch back on.

    Newer Claude Code classifies the `/usage` plan-limits fetch as *non-essential*
    traffic. If the user set ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`` in
    ``~/.claude/settings.json`` the fetch is blocked and the pane renders
    "Could not refresh usage data" with no percentages — which is exactly what
    the probe scrapes. We disable the flag **for the probe process only** via a
    high-precedence ``--settings`` file (deep-merged, so the user's proxy / other
    env survive) plus stripping it from the spawn env as belt-and-suspenders.

    Empty string is required: the CLI treats any non-empty value (incl. ``"0"``)
    as truthy. The user's global settings.json is never touched.

    ``--settings`` is only appended when the command actually launches ``claude``
    (tests may substitute ``bash -i`` via a poller override).
    """
    import json

    env = {k: v for k, v in os.environ.items() if k != TRAFFIC_FLAG}
    binary = claude_cmd.split(' ', 1)[0]
    if 'claude' not in os.path.basename(binary):
        return claude_cmd, env
    override_path = os.path.join(probe_cwd, '_probe_settings.json')
    try:
        with open(override_path, 'w') as f:
            json.dump({'env': {TRAFFIC_FLAG: ''}}, f)
    except OSError as e:
        log.warning('usage_polling: could not write settings override (%s); '
                    'probe may hit the non-essential-traffic gate', e)
        return claude_cmd, env
    return f'{claude_cmd} --settings {override_path}', env


@dataclass
class UsageProbeResult:
    """Parsed /usage panel snapshot. All fields may be None if parse failed."""
    session_pct: int | None
    session_reset: str | None
    week_pct: int | None
    week_reset: str | None
    tier: str | None
    subscription_type: str | None
    raw_pane: str
    error: str | None
    duration_ms: int
    fetched_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['fetched_at'] = self.fetched_at.isoformat()
        return d

    @property
    def ok(self) -> bool:
        return self.session_pct is not None and self.week_pct is not None


def _read_credentials() -> tuple[str | None, str | None]:
    """Return (subscription_type, rate_limit_tier) from ~/.claude/.credentials.json.

    Best-effort — missing file or malformed JSON returns (None, None). The
    poller degrades to raw pane-only output in that case.
    """
    import json
    import os
    path = os.path.expanduser('~/.claude/.credentials.json')
    try:
        with open(path) as f:
            d = json.load(f)
        oauth = d.get('claudeAiOauth', {})
        return oauth.get('subscriptionType'), oauth.get('rateLimitTier')
    except (OSError, ValueError):
        return None, None


def _wait_for_any(child, markers: list[str], timeout: float) -> tuple[int, str]:
    """Poll read_nonblocking until one of the markers appears in ANSI-stripped
    stream. Returns (marker_index, accumulated_raw_text).

    We need this because pexpect.expect() matches the raw stream and Ink
    interleaves ANSI escapes into every string ("Yes,[1CI[1Ctrust[1Cthis[1Cfolder"),
    so a plain regex on "Yes, I trust this folder" never matches.
    """
    deadline = time.monotonic() + timeout
    accum = ''
    while time.monotonic() < deadline:
        try:
            chunk = child.read_nonblocking(size=8192, timeout=0.3)
            if chunk:
                accum += chunk
        except pexpect.exceptions.TIMEOUT:
            pass
        except pexpect.exceptions.EOF:
            break
        clean = _strip_ansi(accum).replace(' ', '')
        for i, m in enumerate(markers):
            if m.replace(' ', '') in clean:
                return i, accum
    return -1, accum


def _probe_sync(claude_cmd: str, timeout_sec: int, probe_cwd: str) -> UsageProbeResult:
    """Blocking probe — runs on a worker thread from the async wrapper.

    ``probe_cwd`` is a permanent CSM-owned directory (e.g. ``~/.csm/usage_probe/``).
    Claude Code trusts a directory permanently after first confirmation, so we
    reuse the same dir and auto-confirm the dialog if it appears (first run only).
    """
    started = time.monotonic()
    fetched_at = now_utc_naive()
    os.makedirs(probe_cwd, exist_ok=True)
    child: pexpect.spawn | None = None
    subscription_type, tier = _read_credentials()
    spawn_cmd, spawn_env = _with_traffic_override(claude_cmd, probe_cwd)

    try:
        child = pexpect.spawn(
            spawn_cmd,
            cwd=probe_cwd,
            encoding='utf-8',
            dimensions=(60, 220),
            timeout=timeout_sec,
            env=spawn_env,
        )
        # Two possible ready states — poll on ANSI-stripped stream because
        # pexpect.expect() sees raw bytes with escapes interleaved.
        i, _ = _wait_for_any(child, ['Yes, I trust this folder',
                                     'bypass permissions on'], timeout=30)
        if i == -1:
            return UsageProbeResult(
                session_pct=None, session_reset=None,
                week_pct=None, week_reset=None,
                tier=tier, subscription_type=subscription_type,
                raw_pane='', error='ready-marker timeout (30s)',
                duration_ms=int((time.monotonic() - started) * 1000),
                fetched_at=fetched_at,
            )
        if i == 0:
            log.info('usage_polling: trust dialog seen, auto-confirming (%s)', probe_cwd)
            time.sleep(0.5)
            child.send('\r')
            j, _ = _wait_for_any(child, ['bypass permissions on'], timeout=15)
            if j == -1:
                return UsageProbeResult(
                    session_pct=None, session_reset=None,
                    week_pct=None, week_reset=None,
                    tier=tier, subscription_type=subscription_type,
                    raw_pane='', error='main-screen timeout after trust (15s)',
                    duration_ms=int((time.monotonic() - started) * 1000),
                    fetched_at=fetched_at,
                )
        time.sleep(2.5)  # ink settles the steady-state frame

        # Type command character-by-character (bulk send is dropped by ink).
        for ch in '/usage':
            child.send(ch)
            time.sleep(0.08)
        time.sleep(1.2)  # let auto-complete render
        child.send('\r')  # CR to commit

        # Poll until the quota numbers actually render ("NN% used") AND the
        # "Esc to cancel" footer is painted. Waiting on the "Current session"
        # label alone races the async plan-limits fetch and parses an empty
        # pane. If the fetch errors with no cached value to fall back on, press
        # `r` to retry a couple of times before giving up.
        deadline = time.monotonic() + 25
        accum = ''
        stable_marker_flat = 'Esctocancel'
        found_stable = False
        retries_left = 2
        last_retry = 0.0
        while time.monotonic() < deadline:
            try:
                chunk = child.read_nonblocking(size=8192, timeout=0.3)
                if chunk:
                    accum += chunk
            except pexpect.exceptions.TIMEOUT:
                pass
            except pexpect.exceptions.EOF:
                break
            clean = _strip_ansi(accum)
            has_pct = bool(PCT_USED_RE.search(clean))
            found_stable = stable_marker_flat in clean.replace(' ', '')
            if has_pct and found_stable:
                break
            # Fetch failed and nothing cached to show → nudge a retry.
            if (not has_pct and retries_left > 0
                    and (time.monotonic() - last_retry) > 3.0
                    and FETCH_ERR_RE.search(clean)):
                log.info('usage_polling: fetch error seen, retrying (r) '
                         '[%d left]', retries_left)
                child.send('r')
                retries_left -= 1
                last_retry = time.monotonic()

        # Grace period to let ink finish touch-ups on the "Resets" line.
        grace_end = time.monotonic() + 2.0
        while time.monotonic() < grace_end:
            try:
                chunk = child.read_nonblocking(size=8192, timeout=0.3)
                if chunk:
                    accum += chunk
            except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF):
                pass

        text = _strip_ansi(accum)
        parsed = _parse_pane(text)

        # Stable marker missing is only a warning if we still parsed values —
        # the marker helps quality, not correctness.
        if parsed[0] is None or parsed[2] is None:
            # Distinguish "the CLI itself could not fetch limits" (actionable:
            # network / non-essential-traffic gate) from "we scraped a loaded
            # pane but the anchors moved" (a parser bug). Only the latter should
            # read as "parse failed".
            if FETCH_ERR_RE.search(text):
                error = 'usage limits unavailable (claude could not load usage data)'
            else:
                error = 'parse failed (no session/week % found)'
        elif not found_stable:
            error = 'stable-marker not seen (values may be mid-render)'
        else:
            error = None

        return UsageProbeResult(
            session_pct=parsed[0],
            session_reset=parsed[1],
            week_pct=parsed[2],
            week_reset=parsed[3],
            tier=tier,
            subscription_type=subscription_type,
            raw_pane=text[-4000:],  # keep tail only, saves DB space
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
            fetched_at=fetched_at,
        )
    except pexpect.exceptions.ExceptionPexpect as e:
        return UsageProbeResult(
            session_pct=None,
            session_reset=None,
            week_pct=None,
            week_reset=None,
            tier=tier,
            subscription_type=subscription_type,
            raw_pane='',
            error=f'pexpect: {e.__class__.__name__}: {e}',
            duration_ms=int((time.monotonic() - started) * 1000),
            fetched_at=fetched_at,
        )
    finally:
        if child is not None:
            try:
                # Best-effort clean exit — Esc to clear overlay, then /exit.
                child.send('\x1b')
                time.sleep(0.2)
                child.send('/exit')
                time.sleep(0.2)
                child.send('\r')
                child.expect(pexpect.EOF, timeout=5)
            except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF, OSError):
                pass
            finally:
                try:
                    child.terminate(force=True)
                except Exception:
                    pass
                # Belt-and-suspenders: even if wall-clock timeout in the async
                # wrapper abandoned this thread, still try to close the pexpect
                # child so the subprocess is SIGTERM'd (assuming it responds).
                try:
                    child.close(force=True)
                except Exception:
                    pass


def _parse_pane(text: str) -> tuple[int | None, str | None, int | None, str | None]:
    """Split by 'Current week' anchor, then extract % + Resets per block."""
    m_split = re.search(r'Current session([\s\S]*?)Current week([\s\S]*)', text)
    if not m_split:
        return None, None, None, None
    session_block = m_split.group(1)
    week_block = m_split.group(2)

    def find_pct(block: str) -> int | None:
        m = re.search(r'(\d{1,3})\s*%\s*used', block)
        return int(m.group(1)) if m else None

    def find_reset(block: str) -> str | None:
        # Tolerate mid-render overdraw (Rese/Reses/Reset all seen).
        m = re.search(r'Rese[a-z]{0,3}\s*([A-Za-z0-9][^\r\n]+)', block)
        return m.group(1).strip() if m else None

    return (
        find_pct(session_block),
        find_reset(session_block),
        find_pct(week_block),
        find_reset(week_block),
    )


class UsagePoller:
    """Async wrapper around _probe_sync. Spawns one probe at a time (lock)."""

    def __init__(self, claude_cmd: str = 'claude --dangerously-skip-permissions',
                 timeout_sec: int = 45,
                 probe_cwd: str | None = None):
        self._cmd = claude_cmd
        self._timeout = timeout_sec
        # Permanent trusted dir — must NOT be under /tmp so a reboot / cleanup
        # doesn't retrigger the trust dialog.
        self._cwd = probe_cwd or str(Path.home() / '.csm' / 'usage_probe')
        self._lock = asyncio.Lock()

    async def probe(self) -> UsageProbeResult | None:
        # Serialize: two overlapping probes would fight over stdin ordering
        # (and both would open real claude sessions).
        async with self._lock:
            # Outer wall-clock guard: pexpect blocking reads can hang past our
            # internal deadlines. asyncio.to_thread cannot kill the worker, so
            # the thread may leak (acceptable trade-off vs. every poller
            # blocked forever). Buffer above internal deadline so a well-behaved
            # probe always finishes inside asyncio.wait_for.
            wall_timeout = settings.usage_probe_timeout_sec + 10
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_probe_sync, self._cmd, self._timeout, self._cwd),
                    timeout=wall_timeout,
                )
            except TimeoutError:
                log.warning(
                    "UsagePoller probe wall-clock timeout (%.1fs); thread may be leaked "
                    "(pexpect child abandoned)", wall_timeout,
                )
                return None
