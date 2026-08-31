"""Codex `/status` probe — parallel to `usage_polling.py` for claude.

Codex's TUI `/status` slash-command opens a status panel that includes
the current subscription's rate-limit window. Rendered pane looks like:

    ╭──────────────────────────────────────────────────────────────╮
    │  >_ OpenAI Codex (v0.145.0)                                  │
    │                                                              │
    │ Visit https://chatgpt.com/codex/settings/usage for up-to-date│
    │ information on rate limits and credits                       │
    │                                                              │
    │  Model:                gpt-5.6-sol (reasoning low, ...)      │
    │  Directory:            /tmp                                  │
    │  Permissions:          Full Access                           │
    │  Agents.md:            <none>                                │
    │  Account:              user@example.com (Plus)               │
    │  Collaboration mode:   Default                               │
    │  Session:              019f...                               │
    │                                                              │
    │  Weekly limit:  [████████████████████] 100% left             │
    │                 (resets 14:11 on 2 Aug)                      │
    ╰──────────────────────────────────────────────────────────────╯

Codex only exposes a *weekly* window — no 5h "session" limit like
claude — so we always leave `session_pct` / `session_reset` = None.

# /status protocol quirk

First `/status` call returns:
    Limits: refresh requested; run /status again shortly.

The real numbers only appear on the SECOND call after codex has fetched
fresh data server-side. Probe sends `/status` twice with a wait between.

# Field mapping (aligned with claude for shared UI rendering)

- `week_pct` — **used** percentage (converted from codex's "N% left" so
  a lower number means less-consumed in both agents' rows)
- `week_reset` — raw display string, e.g. `"14:11 on 2 Aug"`. Year is
  omitted by codex; we keep the raw so the UI shows exactly what codex
  showed. No parse into ISO — same as claude.
- `tier` / `subscription_type` — both hold the tier name from the
  Account line, e.g. `"Plus"` / `"Pro"` / `"Team"` / `"Enterprise"`.
  Duplicated to match claude's split; UI reads whichever it uses.

# Failure modes handled

- codex binary missing → `error='codex binary not on PATH'`
- pexpect timeout → `error='pexpect: TIMEOUT: ...'`
- `/status` pane never renders → `week_pct=None`, `error='parse missed weekly limit'`
- refresh never completes (both /status calls still say "refresh requested")
  → still tries to parse, records `error='refresh not ready'`

# Concurrency

Serialised inside the poller via an asyncio.Lock. Wrapped by
`UsageScheduler` which uses `asyncio.gather` per-tick (claude + codex).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pexpect

from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


@dataclass
class CodexUsageProbeResult:
    """Parsed codex /status panel snapshot. All fields may be None.

    Shape mirrors claude's `UsageProbeResult` so the API + frontend can
    render both from the same code path. Codex has no 5h session
    window, so session_* stays None.
    """
    session_pct: int | None       # always None for codex
    session_reset: str | None     # always None for codex
    week_pct: int | None          # 0-100, used% (converted from "N% left")
    week_reset: str | None        # raw string, e.g. "14:11 on 2 Aug"
    tier: str | None              # "Plus" / "Pro" / "Team" / ...
    subscription_type: str | None # same as tier — duplicated for claude parity
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
        return self.week_pct is not None


_ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\(B|\x1b\\')
_CTRL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def _strip_ansi(s: str) -> str:
    return _CTRL.sub('', _ANSI.sub('', s))


# Weekly limit line — codex renders the % INSIDE parentheses on some
# widths and outside on others. Match "N% left" appearing after the
# "Weekly limit" label but before the next line's opening char, allowing
# the bracketed bar to appear in between.
_WEEK_PCT_RE = re.compile(
    r'Weekly\s+limit\s*:?[^\n]*?(\d{1,3})\s*%\s*left',
    re.IGNORECASE,
)
# Reset clause can be on the same line as the % or wrapped to the next.
# Match up to the closing paren OR end-of-line.
_WEEK_RESET_RE = re.compile(
    r'resets?\s+([^)\n]+?)(?:\)|\r|\n|$)',
    re.IGNORECASE,
)
# Account: <email> (Tier)  — tier is the parenthesised token at line-end.
_ACCOUNT_TIER_RE = re.compile(
    r'Account\s*:\s*\S[^\n]*?\(([A-Za-z][A-Za-z0-9 +]*?)\)',
    re.IGNORECASE,
)


def _parse_pane(pane: str) -> tuple[int | None, str | None, str | None]:
    """Extract (week_pct_used, week_reset, tier) from a stripped pane.

    Each field is optional — one missing / renamed line doesn't fail the
    whole parse, matching claude's parser philosophy.
    """
    week_pct: int | None = None
    week_reset: str | None = None
    tier: str | None = None

    m = _WEEK_PCT_RE.search(pane)
    if m:
        left = int(m.group(1))
        # Clamp: codex has occasionally rendered 101% during refresh.
        left = max(0, min(100, left))
        week_pct = 100 - left

    # Search reset only in the region AFTER the Weekly limit label so we
    # don't accidentally pick up a stale "resets" from elsewhere in the
    # pane.
    week_label = re.search(r'Weekly\s+limit', pane, re.IGNORECASE)
    if week_label:
        tail = pane[week_label.end():]
        rm = _WEEK_RESET_RE.search(tail)
        if rm:
            week_reset = rm.group(1).strip()

    tm = _ACCOUNT_TIER_RE.search(pane)
    if tm:
        tier = tm.group(1).strip()

    return week_pct, week_reset, tier


class CodexUsagePoller:
    """Async wrapper — pexpect + codex TUI + double `/status` + parse."""

    def __init__(
        self,
        codex_cmd: str = 'codex --dangerously-bypass-approvals-and-sandbox -c mcp_servers={}',
        timeout_sec: int = 60,
        cwd: str | None = None,
    ):
        self._cmd = codex_cmd
        self._timeout = timeout_sec
        # Stable trusted dir under $HOME so a reboot / /tmp cleanup doesn't
        # re-trigger the trust prompt on every probe.
        self._cwd = cwd or str(Path.home() / '.csm' / 'codex_status_probe')
        self._lock = asyncio.Lock()

    async def probe(self) -> CodexUsageProbeResult:
        async with self._lock:
            return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self) -> CodexUsageProbeResult:
        start = time.monotonic()
        Path(self._cwd).mkdir(parents=True, exist_ok=True)
        child: pexpect.spawn | None = None
        try:
            child = pexpect.spawn(
                self._cmd,
                encoding='utf-8',
                timeout=self._timeout,
                dimensions=(60, 200),
                cwd=self._cwd,
            )
            # Wait for TUI to fully boot (MCP + welcome screen).
            time.sleep(8)
            _drain(child, timeout=1.0)

            # First /status — usually returns "refresh requested".
            _send_cmd(child, '/status')
            time.sleep(3)
            _drain(child, timeout=1.0)

            # Give the server-side refresh a beat to complete.
            time.sleep(5)

            # Second /status — has real Weekly limit line.
            _send_cmd(child, '/status')
            time.sleep(4)
            pane = _drain(child, timeout=1.5)
            clean = _strip_ansi(pane)

            week_pct, week_reset, tier = _parse_pane(clean)
            elapsed = int((time.monotonic() - start) * 1000)

            err: str | None = None
            if week_pct is None:
                # If codex still says "refresh requested" both times, be
                # explicit about why we got nothing.
                if 'refresh requested' in clean.lower():
                    err = 'refresh not ready'
                else:
                    err = 'parse missed weekly limit'

            return CodexUsageProbeResult(
                session_pct=None,
                session_reset=None,
                week_pct=week_pct,
                week_reset=week_reset,
                tier=tier,
                subscription_type=tier,
                raw_pane=clean[-4000:] if err else '',
                error=err,
                duration_ms=elapsed,
                fetched_at=now_utc_naive(),
            )
        except pexpect.exceptions.ExceptionPexpect as e:
            return _error_result(start, f'{e.__class__.__name__}: {e}')
        except FileNotFoundError:
            return _error_result(start, 'codex binary not on PATH')
        finally:
            if child is not None:
                try: child.sendcontrol('c')
                except Exception: pass
                time.sleep(0.3)
                try: child.terminate(force=True)
                except Exception: pass


def _error_result(start: float, err: str) -> CodexUsageProbeResult:
    return CodexUsageProbeResult(
        session_pct=None, session_reset=None,
        week_pct=None, week_reset=None,
        tier=None, subscription_type=None,
        raw_pane='', error=err,
        duration_ms=int((time.monotonic() - start) * 1000),
        fetched_at=now_utc_naive(),
    )


def _send_cmd(child: pexpect.spawn, cmd: str) -> None:
    """Type a slash-command and commit via CR. Char-by-char avoids Ink
    dropping keystrokes on bulk send."""
    for ch in cmd:
        child.send(ch)
        time.sleep(0.03)
    time.sleep(0.4)
    child.send('\r')


def _drain(child: pexpect.spawn, timeout: float = 0.5) -> str:
    """Read whatever's pending; return accumulated text. Never raises."""
    accum = ''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = child.read_nonblocking(size=16384, timeout=0.3)
            if chunk:
                accum += chunk
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        except Exception:
            break
    return accum
