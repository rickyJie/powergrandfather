"""Lark IM notification sink — pushes critical notifications via lark-cli.

Config source is the singleton `lark_settings` row (see
`csm/models/lark_settings.py`). LarkSink re-reads the row on every
`send()` so config updates from `PUT /api/settings/lark` take effect
without a restart.

"Sink no-op" cases — `_load_config` returns None:
    1. lark_settings row does not exist
    2. row.enabled = False
    3. row.enabled = True but chat_id AND user_id are both empty

Push types are per-row (`enabled_types` JSON dict). The dict maps
`NotificationType.value` → bool. **Conservative default**: a key
missing from this dict is treated as False. The Alembic seed writes
the 4 legacy PUSH_TYPES to True so upgraders keep current behavior.

Per-notification metadata overrides:
    _skip_lark: True         → skip regardless of config
    _bypass_dedup: True      → send even if the dedup key was just used
    _bypass_dnd: True        → send even inside a DnD window
    _force_type_pass: True   → bypass enabled_types gate. **Only honored
                                when notification.type == "test"** —
                                normal notifications setting this flag
                                have it ignored, so a copy-paste of test
                                metadata can't accidentally opt into
                                pushing types the user hasn't enabled.
    _dedup_key: str          → use this key instead of "{type}:{sid}"
    lark_chat_id / lark_user_id: per-event target override

Dedup: same _dedup_key suppressed within `dedup_window_sec`. Rule state
(`_last_sent`) is process-local; the API PUT handler calls
`flush_dedup_cache()` after config updates that could change dedup
semantics (dedup_window_sec change or target change).

Transport: shells out to `lark-cli im +messages-send`. lark-cli must
be on PATH and pre-authenticated (`lark auth login`). Failures are
returned as (False, err) from send_test() and swallowed in send() so
the NotificationBus fire-and-forget path stays isolated.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.config import settings as app_settings
from csm.models.lark_settings import LarkSettings

log = logging.getLogger(__name__)


# Sentinel type used by send_test() so the _force_type_pass gate can be
# scoped narrowly (see class docstring).
_TEST_NOTIFICATION_TYPE = "test"


@dataclass(frozen=True)
class _LarkConfig:
    """Immutable snapshot loaded from the lark_settings row per send()."""
    chat_id: str | None
    user_id: str | None
    dedup_window_sec: int
    dnd_hours: frozenset[int]
    tz: Any  # ZoneInfo | UTC | None
    enabled_types: dict[str, bool]


class LarkSink:
    """Best-effort one-way push to a Lark chat or user via lark-cli.

    Construction is cheap and side-effect-free. `send(notification_dict)`
    is the only public method callers use; `send_test(note)` is invoked
    by POST /api/settings/lark/test.
    """

    _DEDUP_TTL_MULT = 3

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        cli_binary: str = "lark-cli",
    ) -> None:
        self._sm = sessionmaker
        self._cli = cli_binary
        # in-mem dedup map: dedup_key → last_sent_epoch. Bounded by
        # _prune_dedup() on each send and by flush_dedup_cache() from
        # the API PUT handler.
        self._last_sent: dict[str, float] = {}

    # ---- config loading ----
    async def _load_config(self) -> _LarkConfig | None:
        """Read the singleton row; return None if the sink should no-op."""
        async with self._sm() as db:
            row = await db.get(LarkSettings, 1)
        if row is None:
            return None
        if not row.enabled:
            return None
        if not (row.chat_id or row.user_id):
            return None

        # dnd_hours: SQLite JSON columns round-trip ints correctly via
        # SQLAlchemy's JSON type, but if someone hand-edited the DB and
        # stored strings ("23" vs 23), `now.hour in dnd_hours` would
        # silently be False forever. int() defense keeps DnD working.
        dnd: set[int] = set()
        for h in (row.dnd_hours or []):
            try:
                dnd.add(int(h))
            except (TypeError, ValueError):
                log.warning("lark_sink: dropping non-int dnd_hours element %r", h)

        # tz: tolerate bad values (minimal image without full zoneinfo,
        # user typo) so a config typo can't kill the whole push path.
        tz: Any = None
        if row.tz:
            try:
                tz = UTC if row.tz.upper() == "UTC" else ZoneInfo(row.tz)
            except Exception:
                log.warning(
                    "lark_sink: invalid tz=%r in DB; falling back to server local",
                    row.tz,
                )

        # Coerce enabled_types values to bool defensively.
        et = {k: bool(v) for k, v in (row.enabled_types or {}).items()}

        return _LarkConfig(
            chat_id=row.chat_id or None,
            user_id=row.user_id or None,
            dedup_window_sec=int(row.dedup_window_sec or 60),
            dnd_hours=frozenset(dnd),
            tz=tz,
            enabled_types=et,
        )

    # ---- public sending ----
    async def send(self, notification: dict[str, Any]) -> bool:
        """Fire-and-forget path used by NotificationBus. Returns True if
        a push was actually attempted (i.e. shelled out successfully),
        False otherwise (config off, gated, or transport error)."""
        cfg = await self._load_config()
        if cfg is None:
            return False

        meta = notification.get("metadata") or {}
        if meta.get("_skip_lark"):
            return False

        # Target: per-notification override wins over DB default.
        target_chat = meta.get("lark_chat_id") or cfg.chat_id
        target_user = meta.get("lark_user_id") or cfg.user_id
        if not (target_chat or target_user):
            return False

        notif_type = notification.get("type")

        # Type filter. `_force_type_pass` is scoped to type=="test" to
        # prevent a copy-pasted metadata block from opting into pushing
        # a notification kind the user hasn't enabled.
        force_type = bool(meta.get("_force_type_pass")) and notif_type == _TEST_NOTIFICATION_TYPE
        if not force_type:
            if not cfg.enabled_types.get(notif_type, False):
                return False

        bypass_dedup = bool(meta.get("_bypass_dedup"))
        bypass_dnd = bool(meta.get("_bypass_dnd"))

        # DnD: independent flag from bypass_dedup. Historically these
        # were coupled; v2 split them so semantics are explicit.
        if not bypass_dnd and cfg.dnd_hours:
            now_dt = datetime.now(cfg.tz) if cfg.tz is not None else datetime.now()
            if now_dt.hour in cfg.dnd_hours:
                return False

        # Dedup: caller-provided key preferred; fall back to (type, sid).
        dedup_key = meta.get("_dedup_key") or f"{notif_type}:{notification.get('session_id')}"
        now = time.time()
        self._prune_dedup(now, cfg.dedup_window_sec)
        if not bypass_dedup:
            last = self._last_sent.get(dedup_key, 0.0)
            if now - last < cfg.dedup_window_sec:
                return False
        self._last_sent[dedup_key] = now

        text = self._format(notification, tz=cfg.tz)
        try:
            await self._shell_send(text, chat_id=target_chat, user_id=target_user)
            return True
        except Exception:
            log.exception("lark_sink: shell push failed for %s", dedup_key)
            return False

    async def send_test(self, note: str = "") -> tuple[bool, str | None, float]:
        """Trigger a synthetic push for POST /api/settings/lark/test.

        Loads config through `_load_config` (so config edge cases like
        "row missing / disabled / no target" still surface here as
        `sent=False, err=<reason>`), then calls `_shell_send` DIRECTLY
        so a transport error (`chat_id not found`, `auth expired`,
        timeout) reaches the caller as a real error string. Going
        through `send()` would swallow the RuntimeError and leave the
        user staring at a generic "failed" with no root cause.

        Returns (sent, err_msg, duration_sec).
          - (True, None, dur):   push succeeded
          - (False, "…", dur):   any concrete error (config or transport)
        """
        t0 = time.monotonic()
        cfg = await self._load_config()
        if cfg is None:
            return (
                False,
                "sink disabled (check enabled=True and a chat_id/user_id is set)",
                time.monotonic() - t0,
            )
        text = self._format({
            "type": _TEST_NOTIFICATION_TYPE,
            "title": "PowerGrandFather test ping",
            "body": note or f"sent at {datetime.now(UTC).isoformat(timespec='seconds')}",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }, tz=cfg.tz)
        try:
            await self._shell_send(text, chat_id=cfg.chat_id, user_id=cfg.user_id)
            return True, None, time.monotonic() - t0
        except Exception as e:
            # Surface the full lark-cli stderr / timeout message. This
            # is the whole point of the test endpoint — the operator
            # needs to see WHY the push failed, not just THAT it did.
            return False, str(e), time.monotonic() - t0

    # ---- cache management (called by API PUT handler) ----
    def flush_dedup_cache(self) -> int:
        """Drop all in-memory dedup state. Called by PUT /api/settings/lark
        when the update could change dedup semantics (dedup_window_sec
        change or target change). The API handler decides whether to
        call this — the sink itself has no policy."""
        n = len(self._last_sent)
        self._last_sent.clear()
        return n

    # ---- internals ----
    def _prune_dedup(self, now: float, window_sec: int) -> int:
        cutoff = now - (window_sec * self._DEDUP_TTL_MULT)
        stale = [k for k, ts in self._last_sent.items() if ts < cutoff]
        for k in stale:
            self._last_sent.pop(k, None)
        return len(stale)

    # ---- formatting ----
    #
    # Per-type formatters replace the earlier one-size-fits-all render.
    # Each type has a different "salient bit" — for NEW_MESSAGE it's the
    # body preview; for SESSION_CRASHED it's the exit code; for
    # MISSION_DONE it's the success/fail badge — so the layouts diverge
    # to put that bit closest to the eye. Common across every type:
    #
    #   Line 1  : 【PowerGrandFather】{type-icon} {header text}
    #   Line 2+ : type-specific context / body / detail (blank-line delim)
    #   Footer  : 🕐 <tz-localized time>  🔗 <deep link>
    #
    # We keep the brand prefix (per user request) so recipients bridging
    # multiple bots know where the push came from.

    _TYPE_ICONS: dict[str, str] = {
        "new_message": "💬",
        "auto_needs_review": "🔍",
        "session_crashed": "🚨",
        "auto_run_failed": "⚠️",
        "token_warning": "📊",
        "port_conflict": "🔌",
        "mission_done": "🎯",
        # Sentinel for send_test — see _TEST_NOTIFICATION_TYPE.
        "test": "🧪",
    }
    _DEFAULT_ICON = "📢"

    # Leading-emoji stripper for titles that pre-baked a status marker
    # (MISSION_DONE, budget-routed TOKEN_WARNING). Prevents double-icon
    # renders like "【…】✅ ✅ Mission …".
    _LEADING_EMOJI_RE = re.compile(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F900-\U0001F9FF]+\s*")

    # Exit-code signal-name hints for crash / failure bodies. Not
    # exhaustive — just the ones users routinely see. Falls back to
    # "exit N" with no annotation for anything unmapped.
    _EXIT_HINTS: dict[int, str] = {
        137: "SIGKILL (possibly OOM)",
        139: "SIGSEGV (segfault)",
        143: "SIGTERM",
        130: "SIGINT (Ctrl+C)",
    }

    def _format(self, n: dict[str, Any], tz: Any = None) -> str:
        """Dispatch a notification dict to its per-type formatter.

        `tz` is the ZoneInfo (or UTC) loaded from LarkSettings.tz; None
        means "not configured" → footer time keeps naive UTC and marks it
        as such. Every formatter is expected to return a string ready
        to hand to lark-cli; empty-string means "skip" but no producer
        currently returns that."""
        ntype = str(n.get("type") or "")
        dispatch = {
            "new_message": self._format_new_message,
            "auto_needs_review": self._format_auto_review,
            "session_crashed": self._format_session_crashed,
            "auto_run_failed": self._format_auto_failed,
            "token_warning": self._format_token_warning,
            "port_conflict": self._format_port_conflict,
            "mission_done": self._format_mission_done,
        }
        fmt = dispatch.get(ntype, self._format_default)
        return fmt(n, tz).strip()

    # ---- per-type formatters ----
    def _format_new_message(self, n: dict[str, Any], tz: Any) -> str:
        meta = n.get("metadata") or {}
        session_title = str(meta.get("session_title") or "").strip() or "(unnamed session)"
        agent = str(meta.get("agent") or "").strip()
        # Count is encoded in the notification title as "N new message(s)".
        # Extract N; fall back to 1 on parse fail (defensive).
        count = 1
        m = re.match(r"^(\d+)\s+new\s+messages?$", str(n.get("title") or ""))
        if m:
            try:
                count = int(m.group(1))
            except ValueError:
                pass
        header_ctx: list[str] = [session_title]
        if agent:
            header_ctx.append(f"@{agent}")
        if count > 1:
            header_ctx.append(f"{count} new")
        header = self._header_line("new_message", " · ".join(header_ctx))

        blocks: list[str] = [header]
        body = self._clean_snippet(n.get("body") or "", max_len=140)
        if body:
            blocks.append(body)
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_auto_review(self, n: dict[str, Any], tz: Any) -> str:
        title = str(n.get("title") or "").strip()
        # Two title shapes land here: "Needs review: <label>" (supervisor)
        # or "Permission required" (H5 waiting-auth). Render header label
        # accordingly so recipients know which one.
        label = "Needs a human" if title.lower().startswith("needs review") else (
            "Waiting for permission" if title.lower().startswith("permission")
            else title or "Needs a human"
        )
        header = self._header_line("auto_needs_review", label)

        blocks: list[str] = [header]
        ctx = self._context_line(n)
        body = self._clean_snippet(n.get("body") or "", max_len=140)
        detail_lines: list[str] = []
        if ctx:
            detail_lines.append(ctx)
        if body:
            detail_lines.append(f"Verdict: {body}")
        if detail_lines:
            blocks.append("\n".join(detail_lines))
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_session_crashed(self, n: dict[str, Any], tz: Any) -> str:
        header = self._header_line("session_crashed", "Session crashed")
        blocks: list[str] = [header]

        ctx = self._context_line(n)
        exit_line = self._exit_code_line(n)
        detail = [ln for ln in (ctx, exit_line) if ln]
        if detail:
            blocks.append("\n".join(detail))
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_auto_failed(self, n: dict[str, Any], tz: Any) -> str:
        header = self._header_line("auto_run_failed", "Automation failed")
        blocks: list[str] = [header]

        ctx = self._context_line(n)
        exit_line = self._exit_code_line(n)
        detail = [ln for ln in (ctx, exit_line) if ln]
        if detail:
            blocks.append("\n".join(detail))
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_token_warning(self, n: dict[str, Any], tz: Any) -> str:
        # TOKEN_WARNING is emitted from two sources (alert + budget) and
        # the title already carries the important marker (metric name,
        # budget-name + pct, urgency emoji). We strip any leading emoji
        # from the title so it doesn't collide with our type icon, then
        # show the title as the "what triggered" line and the body as
        # the numeric detail. No session context — token/budget is a
        # global concern.
        title = self._LEADING_EMOJI_RE.sub("", str(n.get("title") or "")).strip() or "Token alert"
        header = self._header_line("token_warning", "Token alert")
        blocks: list[str] = [header, title]
        body = str(n.get("body") or "").strip()
        if body:
            blocks.append(body)
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_port_conflict(self, n: dict[str, Any], tz: Any) -> str:
        meta = n.get("metadata") or {}
        port = meta.get("port")
        header_label = f"Port conflict :{port}" if port else "Port conflict"
        header = self._header_line("port_conflict", header_label)
        blocks: list[str] = [header]

        body = str(n.get("body") or "").strip()
        if body:
            blocks.append(body)
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_mission_done(self, n: dict[str, Any], tz: Any) -> str:
        meta = n.get("metadata") or {}
        # Mission status: prefer explicit metadata.status; fall back to
        # sniffing the leading emoji of the title ("✅ …" / "❌ …") which
        # the notification producer sets.
        status = str(meta.get("status") or "").lower()
        title_raw = str(n.get("title") or "")
        if not status:
            if title_raw.startswith("✅"):
                status = "succeeded"
            elif title_raw.startswith("❌"):
                status = "failed"
        wf_name = meta.get("workflow_name")
        mission_id = meta.get("mission_id")

        if status == "failed":
            header_label = "Mission failed"
            icon_override = "❌"
        else:
            header_label = "Mission succeeded"
            icon_override = "✅"
        header = f"【PowerGrandFather】{icon_override} {header_label}"

        ctx_bits: list[str] = []
        if wf_name:
            ctx_bits.append(f"workflow: {wf_name}")
        if mission_id:
            ctx_bits.append(f"mission #{str(mission_id)[:8]}")
        detail: list[str] = []
        if ctx_bits:
            detail.append("📍 " + " · ".join(ctx_bits))
        if status == "failed":
            reason = meta.get("failure_reason")
            if reason:
                detail.append(f"Reason: {reason}")

        blocks: list[str] = [header]
        if detail:
            blocks.append("\n".join(detail))
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    def _format_default(self, n: dict[str, Any], tz: Any) -> str:
        """Fallback for unknown types + the send_test synthetic push.
        Minimal shape so we don't crash on schema drift."""
        ntype = str(n.get("type") or "")
        title = str(n.get("title") or "(no title)").strip()
        header = self._header_line(ntype, title)
        blocks: list[str] = [header]
        body = str(n.get("body") or "").strip()
        if body:
            blocks.append(body)
        blocks.append(self._footer_line(n, tz))
        return "\n\n".join(blocks)

    # ---- shared render helpers ----
    def _header_line(self, ntype: str, text: str) -> str:
        icon = self._TYPE_ICONS.get(ntype, self._DEFAULT_ICON)
        return f"【PowerGrandFather】{icon} {text}"

    def _context_line(self, n: dict[str, Any]) -> str:
        """Standard `📍 session_title · @agent` line for session-scoped
        notifications. Returns empty string when neither piece is known."""
        meta = n.get("metadata") or {}
        session_title = str(meta.get("session_title") or "").strip()
        agent = str(meta.get("agent") or "").strip()
        parts: list[str] = []
        if session_title:
            parts.append(session_title)
        if agent:
            parts.append(f"@{agent}")
        return ("📍 " + " · ".join(parts)) if parts else ""

    def _exit_code_line(self, n: dict[str, Any]) -> str:
        """Reformat body=`exit_code=N` into `exit N (hint)`. Falls back
        to the raw body for other shapes."""
        body = str(n.get("body") or "").strip()
        if not body:
            return ""
        m = re.match(r"^exit_code=(-?\d+)$", body)
        if not m:
            return body
        code = int(m.group(1))
        hint = self._EXIT_HINTS.get(code)
        return f"exit {code} ({hint})" if hint else f"exit {code}"

    def _footer_line(self, n: dict[str, Any], tz: Any) -> str:
        """Compose `🕐 <time>  🔗 <link>` — either half may be missing."""
        parts: list[str] = []
        ts = self._localize_ts(n.get("created_at"), tz)
        if ts:
            parts.append(f"🕐 {ts}")
        sid = n.get("session_id")
        if sid:
            base = app_settings.public_base_url.rstrip("/") if app_settings.public_base_url else f"https://localhost:{app_settings.port}"
            parts.append(f"🔗 {base}/sessions/{sid}")
        return "  ".join(parts)

    def _localize_ts(self, created_at: Any, tz: Any) -> str:
        """Format created_at in the configured tz. API contract per
        CLAUDE.md: naked ISO strings are naive UTC — attach UTC then
        convert. Falls back to raw-string strip if parse fails so a
        schema drift doesn't blank the timestamp."""
        if not created_at:
            return ""
        s = str(created_at)
        try:
            # datetime.fromisoformat handles both `...+00:00` and naked
            # `...` shapes in 3.11+; normalize any trailing `Z` first
            # (fromisoformat doesn't accept Z until 3.11 spot-fix).
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if tz is not None:
                dt = dt.astimezone(tz)
                # strftime %Z on UTC ZoneInfo returns "UTC"; on named zones
                # like Asia/Shanghai returns "CST". Always populated.
                return dt.strftime("%Y-%m-%d %H:%M %Z")
            # tz unset — surface as UTC so reader isn't confused
            return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            return s.split(".", 1)[0].replace("T", " ")

    def _clean_snippet(self, text: str, max_len: int = 140) -> str:
        """Strip common markdown symbols that render as noise in Lark
        plain-text messages, then truncate. Called for NEW_MESSAGE body
        and AUTO_NEEDS_REVIEW verdict — types where the input is a
        free-form assistant/supervisor reply. Other types have
        structured bodies that we don't touch.
        """
        if not text:
            return ""
        s = text
        # Fenced code blocks → placeholder (they never render well in Lark).
        s = re.sub(r"```[\s\S]*?```", "[code]", s)
        # Inline code: strip backticks (keep text).
        s = re.sub(r"`([^`]+)`", r"\1", s)
        # Bold / italic: **x** / __x__ / *x* / _x_ → x. Order matters:
        # do double-marker before single so we don't half-strip **x**.
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"__([^_]+)__", r"\1", s)
        s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", s)
        s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", s)
        # Heading markers at line start (H1-H6).
        s = re.sub(r"(?m)^#{1,6}\s+", "", s)
        # Blockquote markers.
        s = re.sub(r"(?m)^>\s+", "", s)
        # Collapse whitespace within each paragraph, keep paragraph
        # markers via `⏎` so structure isn't lost.
        parts = [" ".join(p.split()) for p in s.splitlines() if p.strip()]
        flat = " ⏎ ".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
        if len(flat) <= max_len:
            return flat
        cut = flat.rfind(" ", 0, max_len)
        if cut < max_len - 30:
            cut = max_len
        return flat[:cut].rstrip() + "…"

    # Default lark-cli timeout. Injectable via _shell_send(timeout_sec=...)
    # so tests can drive the timeout branch without waiting the full 10s.
    _SHELL_TIMEOUT_SEC = 10.0

    async def _shell_send(
        self,
        text: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        timeout_sec: float | None = None,
    ) -> None:
        # `--as bot` is mandatory per project decision — user-identity
        # pushes look like they came from the user's own account, which
        # is confusing for a monitoring tool.
        cmd = [self._cli, "im", "+messages-send", "--as", "bot", "--text", text]
        if chat_id:
            cmd += ["--chat-id", chat_id]
        elif user_id:
            cmd += ["--user-id", user_id]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout = self._SHELL_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            # Kill and reap so the outer wait_for cancellation (from the
            # API's 8s bound) doesn't leave an orphan lark-cli hanging
            # forever. Any cancellation raised from here still cancels
            # the coroutine, we just clean the subprocess first.
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            raise RuntimeError(f"lark-cli timed out after {timeout}s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"lark-cli exit {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}"
            )
