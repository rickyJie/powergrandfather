"""Notification Bus — translates Event Stream events into stored Notifications.

v1 sinks: only In-app (WebSocket push). Lark + desktop deferred to v2.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.adapters.inapp_sink import InAppSink
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.core.transcript_provenance import is_headless_transcript
from csm.models import Notification, Session
from csm.models.notification import NotificationType
from csm.models.session import SessionStatus, SessionType
from csm.utils.time import now_utc_naive


def _is_rotation_not_hijack(
    pid: int | None, own_ext_id: str | None, new_ext_id: str, cwd: str
) -> bool:
    """Decide whether an `assistant_done` for `new_ext_id`, seen in `cwd` but
    owned by no session row, is a genuine in-session ROTATION of the picked
    session (safe to rebind) vs a HIJACK by an unrelated claude that merely
    shares the cwd (must skip).

    The assistant_done cwd-fallback rebind cannot otherwise tell "my session
    compacted/forked to a new JSONL uuid" apart from "a different, concurrently
    live claude is writing in the same directory" — and blindly rebinding the
    newest cwd session onto an unrelated uuid clobbers a correctly pre-bound
    (`--session-id`) claude row. See the caller.

    Returns True → allow the rebind; False → skip (hijack detected).
    CONSERVATIVE: only returns False when we can POSITIVELY tell the picked
    session is still living on its own id. Undeterminable → True, preserving
    the existing rotation-recovery behavior (the "有新消息没红点" fix).
    """
    if not own_ext_id or own_ext_id == new_ext_id:
        return True
    try:
        from csm.config import settings
        from csm.modules.agent.jsonl_fast_tail import conversation_jsonl_path

        projects = settings.claude_projects_dir
        own_path = str(conversation_jsonl_path(projects, cwd, own_ext_id))
        new_path = str(conversation_jsonl_path(projects, cwd, new_ext_id))
    except Exception:
        return True

    # (0) A `claude -p` one-shot cannot be a rotation of a PTY session: the
    #     process that would have rotated is still the same process, so its
    #     `entrypoint` stays "cli". A headless transcript in this cwd is
    #     therefore always someone else's — CSM's own agent-alert helper, a
    #     workflow authoring run, a cron-driven skill. This arm exists because
    #     (1) and (2) both MISS that case: the helper is not the picked
    #     session's child so it holds no fd, and an idle session's own JSONL is
    #     stale by definition, which read as "rotated away" and handed the
    #     helper's transcript to a live interactive row (2026-08-30: a token
    #     alert's answer surfaced in an unrelated mobile chat, and survived
    #     into the next Resume because the poisoned id was inherited).
    if is_headless_transcript(new_path):
        return False

    # (1) If the picked session's process holds a JSONL fd, trust it: a fd on
    #     new_path is a real rotation; a fd on its own (different) JSONL means
    #     the process never left its id → the event belongs to someone else.
    if pid:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for n in os.listdir(fd_dir):
                try:
                    link = os.readlink(f"{fd_dir}/{n}")
                except OSError:
                    continue
                if link == new_path:
                    return True
                if link == own_path:
                    return False
        except OSError:
            pass

    # (2) fd inconclusive → freshness: if the picked session's OWN JSONL is
    #     still being written (recent mtime), it did NOT rotate away → hijack.
    try:
        if (time.time() - os.path.getmtime(own_path)) < 180:
            return False
    except OSError:
        pass
    return True

# States we treat as "live" for cwd-fallback rebind. `EXITED` / `CRASHED` /
# `ORPHANED` sessions must not steal notifications from a newly-spawned one.
_LIVE_SESSION_STATUSES = (
    SessionStatus.STARTING,
    SessionStatus.RUNNING,
    SessionStatus.IDLE,
    SessionStatus.WAITING_INPUT,
    SessionStatus.WAITING_AUTH,
)

log = logging.getLogger(__name__)


def _fmt_tokens_short(n: Any) -> str:
    """1_234_567 → '1.2M'; 3_400 → '3.4K'; 42 → '42'."""
    try:
        v = int(n or 0)
    except (TypeError, ValueError):
        return str(n)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _snippet(text: str, max_len: int = 180) -> str:
    """Collapse whitespace + truncate for a notification body preview.

    Assistant messages are often multi-paragraph and include code fences;
    the panel and Lark push both render one line, so newlines get replaced
    with `⏎ ` (visible in Lark, ignored by the panel's `-webkit-line-clamp`)
    and the string is capped with an ellipsis. Preserves original word
    boundary when possible to avoid cutting mid-word.
    """
    if not text:
        return ""
    # Preserve paragraph breaks visually while flattening whitespace within
    # each paragraph — a Bash tool output that's a single line of gibberish
    # doesn't need visible line breaks, but a real response like
    # "1. Foo\n2. Bar" is easier to scan with the arrow markers.
    parts = [" ".join(p.split()) for p in text.splitlines() if p.strip()]
    flat = " ⏎ ".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
    if len(flat) <= max_len:
        return flat
    # Try to break at word boundary near max_len — avoid ugly `abc de…`
    cut = flat.rfind(" ", 0, max_len)
    if cut < max_len - 30:  # no reasonable boundary, hard cut
        cut = max_len
    return flat[:cut].rstrip() + "…"


def _format_token_warning_body(p: dict[str, Any]) -> str:
    """Compact Chinese one-liner per known metric. Falls back to
    'actual X · threshold Y' for custom rules the presets don't cover."""
    payload = p.get("script_payload") or {}
    metric = payload.get("metric") or p.get("metric") or "custom"

    if metric == "msg_count":
        return (
            f"5h messages {payload.get('actual', p.get('actual'))}"
            f"/{payload.get('threshold', p.get('threshold'))}"
        )

    if metric == "total_tokens":
        actual = _fmt_tokens_short(payload.get("actual", p.get("actual")))
        threshold = _fmt_tokens_short(payload.get("threshold", p.get("threshold")))
        return f"5h spend {actual} (threshold {threshold})"

    if metric == "session_burn":
        sid = str(payload.get("session_id") or "?")[:8]
        share = payload.get("actual_share_pct", "?")
        tokens = _fmt_tokens_short(payload.get("actual_tokens"))
        return f"session {sid} at {share}% ({tokens})"

    if metric in ("cache_hit_ratio_claude", "cache_hit_ratio_drop"):
        ratio = payload.get("actual_ratio_pct", "?")
        tokens = _fmt_tokens_short(payload.get("actual_claude_tokens"))
        threshold_ratio = payload.get("threshold_ratio_pct", "?")
        return f"Claude cache hit rate {ratio}% (threshold {threshold_ratio}%, spend {tokens})"

    return f"actual {p.get('actual')} · threshold {p.get('threshold')}"


class NotificationBus:
    """Translates Event Stream events into typed Notifications + InApp WS push.

    Routing (hardcoded for v1 — see ADR-0001):
      - `MESSAGE_ASSISTANT_DONE` on interactive Session → `NEW_MESSAGE`,
        bumps `Session.unread_count`. Multiple events for the same session
        within `dedup_window_sec` merge into one notification (title shows
        "N new messages"). The merge handler also clears `read_at` so the
        badge re-illuminates if the user previously marked it read.
      - `SESSION_CRASHED` → `SESSION_CRASHED`.
      - `SESSION_ENDED` with `exit_code != 0` on an AUTO session →
        `AUTO_RUN_FAILED`.
      - `TOKEN_ALERT_TRIGGERED` → `TOKEN_WARNING` (AgentAlertEvaluator).
      - `BUDGET_BREACHED` → `TOKEN_WARNING` (BudgetEvaluator, stronger
        title / bypass-dedup on breach).
      - `PORT_CONFLICT_DETECTED` → `PORT_CONFLICT`.
      - `SUPERVISOR_REVIEW_REQUESTED` → `AUTO_NEEDS_REVIEW`.

    Concurrency contract: `_on_assistant_done` serialises per-session via
    `_session_locks[sid]` so concurrent assistant-done events on the same
    session merge atomically rather than racing into two separate
    `NEW_MESSAGE` rows.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        in_app_sink: InAppSink,
        dedup_window_sec: int = 5,
        # Must exceed the EventStream poll interval — see
        # `_last_assistant_done_bump` below. main.py derives it from settings.
        cross_source_window_sec: float = 8.0,
        lark_sink: Any = None,
        retention_days: int = 30,
        per_type_cap: int = 1000,
        retention_tick_sec: float = 3600.0,
    ):
        self._sm = sessionmaker
        self._es = event_stream
        self._inapp = in_app_sink
        self._lark = lark_sink  # optional; None or a LarkSink-like object with async .send(dict)
        self._dedup_window = timedelta(seconds=dedup_window_sec)
        self._sub_id: str | None = None
        # in-mem map: session_id → (last new_message notification id, ts, merge_count).
        # The merge_count used to live on `Session.unread_count` in the DB, but
        # notifications are now decoupled from the Sessions module — the count
        # only exists for the "N new messages" merge title and doesn't need to
        # be persisted (a restart naturally starts fresh dedup windows).
        self._last_new_msg: dict[str, tuple[str, datetime, int]] = {}
        # per-session lock so concurrent assistant-done events on the same
        # session can't both insert a fresh notification.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Fast-path guard for _clear_pending_permission_notif. That method used
        # to open a DB session on EVERY tool-progress / waiting-input / idle
        # event (i.e. essentially every tool call in every session) just to
        # find-and-clear a rare "Permission required" row — a round-trip paid
        # on the hot path for a usually-absent outcome (backend-review W1). We
        # populate this set when WE create a permission notif and discard on
        # clear, so the DB round-trip only happens for sids that actually have
        # one pending. Tradeoff: a permission notif created before a restart
        # won't fast-clear (not in the set) — it just lingers in the bell until
        # read, which is acceptable vs. the per-tool-call DB cost.
        self._sids_pending_permission: set[str] = set()
        # sid → (last bump ts, producer). MESSAGE_ASSISTANT_DONE is emitted by
        # TWO sources per turn: the hook Stop path (`api/hooks.py`, fires the
        # instant claude finishes) and the JSONL tail (`backends/claude/
        # events.py`, sees stop_reason=end_turn on the next poll). They do NOT
        # land ~ms apart as v1 assumed — EventStream polls every
        # `event_stream_poll_interval_sec` (5s), so the pair is typically
        # 2-7s apart and the 2s window below never caught it. Result: two
        # NEW_MESSAGE rows per turn, i.e. two OS notifications, two Lark
        # pushes, and a doubled unread count (measured: ~40 duplicate pairs a
        # day, mean gap 4.5s). `_cross_source_window_sec` closes that: an
        # event from the *other* producer inside the window is the same turn.
        # Producer is tracked alongside the ts so the suppression can't chain
        # (a swallowed pair is marked "both", which starts a fresh turn).
        self._last_assistant_done_bump: dict[str, tuple[datetime, str]] = {}
        self._assistant_done_dedup_sec = 2.0
        self._cross_source_window_sec = cross_source_window_sec
        # Retention: prune dismissed rows older than `retention_days`, then
        # enforce a hard `per_type_cap` per NotificationType (keep newest
        # `per_type_cap`). Runs every `retention_tick_sec` in the background.
        self._retention_days = retention_days
        self._per_type_cap = per_type_cap
        self._retention_tick_sec = retention_tick_sec
        self._retention_task: asyncio.Task | None = None
        # Fire-and-forget Lark push tasks: keep strong refs so Python's
        # asyncio GC can't collect an in-flight task and produce
        # "Task was destroyed but it is pending!" warnings + silent drop.
        # Cleared via done-callback so the set doesn't grow unbounded.
        self._pending_lark_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._sub_id is None:
            self._sub_id = self._es.subscribe(None, self._dispatch)
        if self._retention_task is None:
            self._retention_task = asyncio.create_task(
                self._retention_loop(), name="csm-notif-retention"
            )

    async def stop(self) -> None:
        if self._sub_id is not None:
            self._es.unsubscribe(self._sub_id)
            self._sub_id = None
        if self._retention_task is not None and not self._retention_task.done():
            self._retention_task.cancel()
            try:
                await self._retention_task
            except (asyncio.CancelledError, Exception):
                pass
            self._retention_task = None
        # Drain in-flight Lark pushes so shutdown doesn't lose the last
        # batch. Bounded by the sink's own timeout (10s in `_shell_send`)
        # so this can't hang forever.
        if self._pending_lark_tasks:
            await asyncio.gather(*self._pending_lark_tasks, return_exceptions=True)
            self._pending_lark_tasks.clear()

    async def _retention_loop(self) -> None:
        """Background loop: run one retention tick every
        `_retention_tick_sec`. Exceptions in a tick are caught so a
        transient DB error doesn't kill the loop."""
        # Do one tick shortly after start so a long-running deployment
        # doesn't wait a full hour for the first prune after boot.
        try:
            await asyncio.sleep(min(60.0, self._retention_tick_sec))
        except asyncio.CancelledError:
            return
        while True:
            try:
                await self._retention_tick()
            except Exception:
                log.exception("notification retention tick failed")
            try:
                await asyncio.sleep(self._retention_tick_sec)
            except asyncio.CancelledError:
                return

    async def _retention_tick(self) -> tuple[int, int]:
        """One retention pass. Returns `(pruned_by_age, pruned_by_cap)`.

        Age prune: delete rows with `dismissed_at IS NOT NULL AND
        dismissed_at < now - retention_days`. We only prune dismissed
        rows — an unread row is user-facing state and shouldn't vanish
        without their action regardless of age.

        Cap prune: for each NotificationType, keep the newest
        `per_type_cap` rows (by `created_at desc`); delete the rest.
        Runs regardless of read/dismiss state — the cap is a hard
        upper bound on table size to prevent runaway growth.
        """
        from sqlalchemy import delete, func, select
        pruned_age = 0
        pruned_cap = 0
        cutoff = now_utc_naive() - timedelta(days=self._retention_days)
        async with self._sm() as db:
            res = await db.execute(
                delete(Notification)
                .where(Notification.dismissed_at.is_not(None))
                .where(Notification.dismissed_at < cutoff)
            )
            pruned_age = int(res.rowcount or 0)
            # Per-type cap: find types where count > cap, delete the
            # oldest overflow. Doing this as one SQL statement per type
            # is fine — the number of NotificationType values is small
            # and this loop runs hourly.
            types_res = await db.execute(
                select(Notification.type, func.count(Notification.id))
                .group_by(Notification.type)
                .having(func.count(Notification.id) > self._per_type_cap)
            )
            for notif_type, count in types_res.all():
                overflow = int(count) - self._per_type_cap
                if overflow <= 0:
                    continue
                # Fetch the ids of the OLDEST `overflow` rows for this
                # type, then delete them. Two-step (select + delete)
                # avoids SQLite's lack of `LIMIT` in DELETE statements.
                ids_res = await db.execute(
                    select(Notification.id)
                    .where(Notification.type == notif_type)
                    .order_by(Notification.created_at.asc())
                    .limit(overflow)
                )
                ids = [row[0] for row in ids_res.all()]
                if not ids:
                    continue
                res = await db.execute(
                    delete(Notification).where(Notification.id.in_(ids))
                )
                pruned_cap += int(res.rowcount or 0)
            await db.commit()
        if pruned_age or pruned_cap:
            log.info(
                "notification retention: pruned %d aged (>%dd dismissed) + %d over-cap",
                pruned_age, self._retention_days, pruned_cap,
            )
        return pruned_age, pruned_cap

    # ---- read-side API ----
    async def list_notifications(
        self,
        limit: int = 100,
        only_unread: bool = False,
        include_dismissed: bool = False,
    ) -> list[Notification]:
        async with self._sm() as db:
            stmt = select(Notification).order_by(Notification.created_at.desc()).limit(limit)
            if only_unread:
                stmt = stmt.where(Notification.read_at.is_(None))
            if not include_dismissed:
                stmt = stmt.where(Notification.dismissed_at.is_(None))
            return list((await db.execute(stmt)).scalars().all())

    async def unread_by_session(self) -> dict[str, int]:
        """Exact unread NEW_MESSAGE counts, independent of panel pagination."""
        from sqlalchemy import func

        async with self._sm() as db:
            rows = (await db.execute(
                select(Notification.session_id, func.count(Notification.id))
                .where(
                    Notification.type == NotificationType.NEW_MESSAGE,
                    Notification.session_id.is_not(None),
                    Notification.read_at.is_(None),
                    Notification.dismissed_at.is_(None),
                )
                .group_by(Notification.session_id)
            )).all()
        return {str(sid): int(count) for sid, count in rows if sid}

    async def mark_read(self, notif_id: str) -> bool:
        """Mark a single notification read.

        For session-bound NEW_MESSAGE notifs we hold the per-session lock
        while mutating `_last_new_msg` and committing, so a concurrent
        `_on_assistant_done` on the same session can't re-open the row via
        the merge branch (feedback bug: bell red-dot resurrection).
        Non-session notifs (token_warning etc) take the cheap fast path.
        """
        # First peek to learn if this notif needs the per-session lock.
        async with self._sm() as db:
            row = await db.get(Notification, notif_id)
            if row is None:
                return False
            sid = row.session_id if row.type == NotificationType.NEW_MESSAGE else None

        if sid:
            lock = self._session_locks.setdefault(sid, asyncio.Lock())
            async with lock:
                async with self._sm() as db:
                    row = await db.get(Notification, notif_id)
                    if row is None:
                        return False
                    if row.read_at is None:
                        row.read_at = now_utc_naive()
                    sess = await db.get(Session, sid)
                    if sess is not None and sess.unread_count > 0:
                        sess.unread_count = 0
                    self._last_new_msg.pop(sid, None)
                    await db.commit()
            return True

        async with self._sm() as db:
            row = await db.get(Notification, notif_id)
            if row is None:
                return False
            if row.read_at is None:
                row.read_at = now_utc_naive()
                await db.commit()
        return True

    async def dismiss(self, notif_id: str) -> bool:
        """Dismiss a notification. Mirrors `mark_read`'s lock discipline for
        NEW_MESSAGE so a concurrent assistant-done can't revive a
        just-dismissed row through the merge branch.
        """
        async with self._sm() as db:
            row = await db.get(Notification, notif_id)
            if row is None:
                return False
            sid = row.session_id if row.type == NotificationType.NEW_MESSAGE else None

        if sid:
            lock = self._session_locks.setdefault(sid, asyncio.Lock())
            async with lock:
                async with self._sm() as db:
                    row = await db.get(Notification, notif_id)
                    if row is None:
                        return False
                    now = now_utc_naive()
                    row.dismissed_at = now
                    if row.read_at is None:
                        row.read_at = now
                    self._last_new_msg.pop(sid, None)
                    await db.commit()
            return True

        async with self._sm() as db:
            row = await db.get(Notification, notif_id)
            if row is None:
                return False
            now = now_utc_naive()
            row.dismissed_at = now
            if row.read_at is None:
                row.read_at = now
            await db.commit()
        return True

    async def mark_all_read(self) -> dict[str, int]:
        """One-shot "clear the bell": mark every unread notification as
        read+dismissed AND zero every Session.unread_count. Returns
        counts so the caller can log / show a toast.

        This is the nuclear option that powers the "🧹 clear all" button.
        Individual `mark_read` / `dismiss` are still available for
        finer-grained cleanup.

        Concurrency: acquires every currently-tracked session lock BEFORE
        the batch update so a concurrent `_on_assistant_done` can't slip
        between the SQL update and the dict clear and re-open a row we
        just dismissed.
        """
        now = now_utc_naive()
        # Snapshot sids so the acquire order is deterministic; new sids
        # that appear after this line arrive after our clear() and start
        # fresh dedup windows, which is what we want.
        sids = sorted(self._last_new_msg.keys())
        async with AsyncExitStack() as stack:
            for sid in sids:
                lock = self._session_locks.setdefault(sid, asyncio.Lock())
                await stack.enter_async_context(lock)
            async with self._sm() as db:
                notif_result = await db.execute(
                    update(Notification)
                    .where(Notification.dismissed_at.is_(None))
                    .values(read_at=now, dismissed_at=now)
                )
                sess_result = await db.execute(
                    update(Session)
                    .where(Session.unread_count > 0)
                    .values(unread_count=0)
                )
                await db.commit()
            self._last_new_msg.clear()
            self._last_assistant_done_bump.clear()
        return {
            "notifications_cleared": int(notif_result.rowcount or 0),
            "sessions_cleared": int(sess_result.rowcount or 0),
        }

    async def mark_session_read(self, session_id: str) -> int:
        """Clear unread count on the session row + mark all its new_message
        notifs read. Returns 1 on success, 0 if the session doesn't exist
        (so the API layer can 404).

        Held under the per-session lock so a concurrent `_on_assistant_done`
        can't re-open notifs we just marked read via the merge branch.
        """
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            async with self._sm() as db:
                sess = await db.get(Session, session_id)
                if sess is None:
                    # Bogus sid — surface as 0 rows affected so callers can
                    # translate to 404. Also purge any dangling dedup state
                    # for this id so we don't accumulate junk.
                    self._last_new_msg.pop(session_id, None)
                    return 0
                sess.unread_count = 0
                now = now_utc_naive()
                await db.execute(
                    update(Notification)
                    .where(
                        Notification.session_id == session_id,
                        Notification.type == NotificationType.NEW_MESSAGE,
                        Notification.read_at.is_(None),
                    )
                    .values(read_at=now)
                )
                await db.commit()
            self._last_new_msg.pop(session_id, None)
        return 1

    def evict_session(self, sid: str) -> None:
        """Purge in-memory dedup state for a session that's been hard-deleted.
        Call from the sessions purge API to prevent the three tracking
        dicts (`_session_locks`, `_last_new_msg`, `_last_assistant_done_bump`)
        from accumulating dangling entries and — more importantly — to
        prevent a stale `_last_new_msg` pointer from silently swallowing
        NEW_MESSAGE events for a reused session id.
        """
        self._session_locks.pop(sid, None)
        self._last_new_msg.pop(sid, None)
        self._last_assistant_done_bump.pop(sid, None)
        self._sids_pending_permission.discard(sid)

    async def total_unread(self) -> int:
        """Count of Notification rows the user has not read + not dismissed.

        Post-decouple: the Sessions module no longer tracks unread; the
        Notification table alone is authoritative for the bell / sidebar
        badge. Includes both session-bound (NEW_MESSAGE etc.) and non-session
        (TOKEN_WARNING etc.) rows in a single count, so `mark_read` on any
        notification decrements this uniformly.
        """
        from sqlalchemy import func as _func
        async with self._sm() as db:
            count = (await db.execute(
                select(_func.count(Notification.id)).where(
                    Notification.read_at.is_(None),
                    Notification.dismissed_at.is_(None),
                )
            )).scalar_one()
        return int(count or 0)

    async def _clear_pending_permission_notif(self, csm_sid: str) -> None:
        """Mark the OLDEST unread "Permission required" notif for `csm_sid` as read.

        H5 emits an `AUTO_NEEDS_REVIEW` notif with title="Permission required"
        when a session parks at waiting-auth. Once the user answers the
        prompt in the terminal, claude proceeds and the next hook fires
        (SESSION_TOOL_PROGRESS / SESSION_WAITING_INPUT / MESSAGE_ASSISTANT_DONE
        for that sid). At that point the permission notif is stale — the
        request has been resolved — but was silently rotting in the bell.

        Surgical clear (2026-08-11, `local:dc4dceec`): only the OLDEST unread
        row is cleared, not every unread "Permission required" row for the
        session. Claude serially processes tool calls, so at most one
        permission is pending per moved-forward signal — clearing all would
        wrongly reap a NEW permission request if two SESSION_WAITING_AUTH
        events landed back-to-back before this handler ran. Also pushes the
        updated row to inapp_sink so the bell decrements client-side without
        a full page reload (the old bulk-UPDATE never told the WS about it,
        so the badge sat stale until reconnect/reload).

        We match on `title == "Permission required"` because
        `AUTO_NEEDS_REVIEW` also covers Supervisor review requests (which
        have title "Needs review: ...") and those must NOT auto-clear.
        Best-effort: any DB blip is logged + swallowed so a failed sweep
        never kills the dispatcher.
        """
        # Fast path (backend-review W1): skip the DB round-trip entirely unless
        # this sid is known to have a pending permission notif this lifetime.
        # This method fires on every tool-progress/waiting-input/idle event, so
        # the guard removes a per-tool-call SELECT+commit in the common case.
        if csm_sid not in self._sids_pending_permission:
            return
        # We're about to resolve it (or find it already gone) — resync the set.
        self._sids_pending_permission.discard(csm_sid)
        try:
            async with self._sm() as db:
                row = (await db.execute(
                    select(Notification)
                    .where(
                        Notification.session_id == csm_sid,
                        Notification.type == NotificationType.AUTO_NEEDS_REVIEW,
                        Notification.title == "Permission required",
                        Notification.read_at.is_(None),
                    )
                    .order_by(Notification.created_at.asc())
                    .limit(1)
                )).scalar_one_or_none()
                if row is None:
                    return
                row.read_at = now_utc_naive()
                await db.commit()
                await db.refresh(row)
        except Exception:
            log.exception("permission auto-clear failed sid=%s", csm_sid)
            return
        await self._push_inapp(row)

    # ---- dispatch ----
    async def _dispatch(self, event: Event) -> None:
        try:
            await self._route(event)
        except Exception as e:
            log.exception("notification dispatch failed: %s", e)

    async def _route_token_alert(self, event: Event) -> None:
        """Handle a TOKEN_ALERT_TRIGGERED event from AgentAlertEvaluator
        (or the /simulate button). Prefers an agent-authored summary when
        the rule has escalate=True; otherwise falls back to a compact
        title/body built from the check-script payload."""
        p = event.payload
        summary = p.get("agent_summary") if isinstance(p.get("agent_summary"), dict) else None
        if summary and summary.get("title") and summary.get("body"):
            title = summary["title"]
            body = summary["body"]
        else:
            title = str(p.get("alert_name") or "Token alert")
            body = _format_token_warning_body(p)
        # Per-rule channel opt-out: strip the lark routing hints from
        # metadata when "lark" wasn't selected, so LarkSink self-skips
        # even if a default LarkSink target is configured in env.
        channels = p.get("channels") or []
        meta = dict(p)
        if channels and "lark" not in channels:
            meta.pop("lark_chat_id", None)
            meta.pop("lark_user_id", None)
            meta["_skip_lark"] = True
        await self._emit_notification(
            type=NotificationType.TOKEN_WARNING,
            title=title,
            body=body,
            session_id=None,
            metadata=meta,
        )

    async def _route_mission_ended(self, event: Event) -> None:
        """Handle a MISSION_ENDED event from WorkflowOrchestrator.

        `payload.status` is `succeeded` or `failed` — cancellation is
        user-initiated and doesn't emit. The label is the workflow name
        so the panel row reads like `Mission succeeded: nightly_audit`.
        """
        p = event.payload or {}
        status = str(p.get("status") or "").lower()
        wf_name = p.get("workflow_name") or "(unnamed workflow)"
        mission_id = p.get("mission_id")
        if status == "succeeded":
            emoji = "✅"
            title = f"{emoji} Mission succeeded: {wf_name}"
            body = f"mission {mission_id[:8] if mission_id else '?'}"
        elif status == "failed":
            emoji = "❌"
            title = f"{emoji} Mission failed: {wf_name}"
            reason = p.get("failure_reason") or "(no reason)"
            body = f"mission {mission_id[:8] if mission_id else '?'} · {reason}"
        else:
            # Defensive — shouldn't fire in practice (cancellation skips emit)
            return
        meta = dict(p)
        meta.setdefault("workflow_name", wf_name)
        await self._emit_notification(
            type=NotificationType.MISSION_DONE,
            title=title,
            body=body,
            session_id=None,
            metadata=meta,
        )

    async def _route_budget_breached(self, event: Event) -> None:
        """Handle a BUDGET_BREACHED event from BudgetEvaluator. State is
        `warn` or `breached`; the latter maps to a stronger emoji + gets
        `_bypass_dedup=True` at the producer so a breach always pages."""
        p = event.payload
        state = p.get("state", "warn")
        emoji = "🚨" if state == "breached" else "⚠️"
        title = (
            f"{emoji} Budget {state.upper()}: {p.get('budget_name')} "
            f"({p.get('effective_pct')}%)"
        )
        scope_label = p.get("scope_type", "global")
        if p.get("scope_value"):
            scope_label += f"={p['scope_value']}"
        body = (
            f"{p.get('current_tokens', 0):,} tokens · "
            f"${p.get('current_cost_usd', 0):.2f} · "
            f"scope={scope_label} · period={p.get('period')}"
        )
        channels = p.get("channels") or []
        meta = dict(p)
        if channels and "lark" not in channels:
            meta.pop("lark_chat_id", None)
            meta.pop("lark_user_id", None)
            meta["_skip_lark"] = True
        await self._emit_notification(
            type=NotificationType.TOKEN_WARNING,
            title=title,
            body=body,
            session_id=None,
            metadata=meta,
        )

    async def _route(self, event: Event) -> None:
        # Domain-specific notification sources — each producer emits a
        # dedicated EventType (see `EventType.TOKEN_ALERT_TRIGGERED` etc)
        # so this switch is `grep`-able. The old `API_ERROR + _marker`
        # pattern was retired in this refactor.
        if event.type == EventType.TOKEN_ALERT_TRIGGERED:
            await self._route_token_alert(event)
            return
        if event.type == EventType.BUDGET_BREACHED:
            await self._route_budget_breached(event)
            return
        if event.type == EventType.PORT_CONFLICT_DETECTED:
            await self._emit_notification(
                type=NotificationType.PORT_CONFLICT,
                title=f"Port conflict on :{event.payload.get('port')}",
                body=f"new pid={event.payload.get('new_pid')} cmd={event.payload.get('new_cmd')}",
                session_id=None,
                metadata=dict(event.payload),
            )
            return
        if event.type == EventType.MISSION_ENDED:
            await self._route_mission_ended(event)
            return
        if event.type == EventType.SUPERVISOR_REVIEW_REQUESTED:
            csm_sid = event.payload.get("csm_session_id")
            label = await self._session_label(csm_sid) or event.payload.get("session_title")
            meta = dict(event.payload)
            if label:
                meta.setdefault("session_title", label)
            await self._emit_notification(
                type=NotificationType.AUTO_NEEDS_REVIEW,
                title=f"Needs review: {label or (csm_sid or '')[:8]}",
                body=f"[{event.payload.get('category', '?')}] {event.payload.get('reason', '(no reason)')}",
                session_id=csm_sid,
                metadata=meta,
            )
            return

        # 3) Session-level events use our internal csm_session_id stashed in payload
        csm_sid = (event.payload or {}).get("csm_session_id")

        # Any hook-side "session moved forward" signal (tool progressed,
        # session waiting for user input, session went idle) means whatever
        # H5 "Permission required" notification was pending for this sid is
        # now stale — reap it before doing anything else so the bell
        # decrements even if the current event doesn't produce a notif.
        if csm_sid and event.type in (
            EventType.SESSION_TOOL_PROGRESS,
            EventType.SESSION_WAITING_INPUT,
            EventType.SESSION_IDLE,
        ):
            await self._clear_pending_permission_notif(csm_sid)
            # Fall through — the tool-progress / waiting-input / idle
            # events don't themselves produce a notification in v1.

        # H5: claude hook says we're waiting for user permission → high-priority notif.
        if event.type == EventType.SESSION_WAITING_AUTH and csm_sid:
            label = await self._session_label(csm_sid)
            meta = dict(event.payload)
            if label:
                meta.setdefault("session_title", label)
            await self._emit_notification(
                type=NotificationType.AUTO_NEEDS_REVIEW,
                title="Permission required",
                body="Claude is waiting for your approval to use a tool.",
                session_id=csm_sid,
                metadata=meta,
            )
            # Remember this sid has a pending permission notif so the cheap
            # in-memory guard in _clear_pending_permission_notif can skip the
            # DB round-trip for every other session's tool events.
            self._sids_pending_permission.add(csm_sid)
            return

        if event.type == EventType.SESSION_CRASHED:
            label = await self._session_label(csm_sid) if csm_sid else None
            meta = dict(event.payload)
            if label:
                meta.setdefault("session_title", label)
            await self._emit_notification(
                type=NotificationType.SESSION_CRASHED,
                title="Session crashed",
                body=f"exit_code={event.payload.get('exit_code')}",
                session_id=csm_sid,
                metadata=meta,
            )
            return
        if event.type == EventType.SESSION_ENDED:
            # Auto session with non-zero exit → auto_run_failed
            exit_code = event.payload.get("exit_code")
            if csm_sid and exit_code not in (None, 0):
                async with self._sm() as db:
                    sess = await db.get(Session, csm_sid)
                    if sess is not None and sess.type == SessionType.AUTO:
                        label = sess.title or sess.id[:8]
                        meta = dict(event.payload)
                        meta.setdefault("session_title", label)
                        await self._emit_notification(
                            type=NotificationType.AUTO_RUN_FAILED,
                            title=f"Automation run failed: {label}",
                            body=f"exit_code={exit_code}",
                            session_id=csm_sid,
                            metadata=meta,
                        )
            # Auto-mark NEW_MESSAGE notifications for a just-ended session
            # as read so the bell red-pip disappears (user feedback: "click
            # notification → jump → session already exited, weird"). The
            # message did arrive, but there's nothing left to act on once
            # the session has closed; keeping it unread just creates a
            # phantom to-do. Applies to any session type (interactive /
            # auto / agent) with pending NEW_MESSAGE rows. Also purge the
            # in-memory dedup pointer so a lingering event can't merge
            # into a stale row.
            if csm_sid:
                try:
                    async with self._sm() as db:
                        await db.execute(
                            update(Notification)
                            .where(
                                Notification.session_id == csm_sid,
                                Notification.type == NotificationType.NEW_MESSAGE,
                                Notification.read_at.is_(None),
                            )
                            .values(read_at=now_utc_naive())
                        )
                        await db.commit()
                    self._last_new_msg.pop(csm_sid, None)
                    self._last_assistant_done_bump.pop(csm_sid, None)
                except Exception:
                    log.exception("SESSION_ENDED auto-mark-read failed sid=%s", csm_sid)
            return

        # 4) Assistant done in an interactive session → bump unread, write/merge new_message
        if event.type == EventType.MESSAGE_ASSISTANT_DONE:
            await self._on_assistant_done(event)
            return

    async def _on_assistant_done(self, event: Event) -> None:
        # The event uses claude session_id (from JSONL). We need to find the csm Session
        # row by external_session_id. If not yet bound (Session Manager hasn't reconciled),
        # silently skip.
        if not event.session_id:
            return
        payload = event.payload or {}
        # An aborted turn reuses this event type purely to release RUNNING
        # status (codex has no Stop hook, so the rollout's `turn_aborted` is
        # the only signal). It produced no reply, so notifying "new message"
        # and bumping unread would be a lie the user has to go clear.
        if payload.get("aborted"):
            return
        event_agent = payload.get("backend")
        # Resolve the csm session id without holding the session-lock.
        async with self._sm() as db:
            direct_stmt = select(Session).where(
                Session.external_session_id == event.session_id,
                Session.superseded_by.is_(None),
                Session.status.in_(_LIVE_SESSION_STATUSES),
            )
            if event_agent:
                direct_stmt = direct_stmt.where(Session.agent == event_agent)
            res = await db.execute(direct_stmt)
            # The partial unique index guarantees at most one live owner.
            # Filtering terminal history here also avoids MultipleResultsFound
            # for resumed sessions that legitimately reuse an external id.
            sess = res.scalar_one_or_none()
            # Fallback for claude session-id rotation (feedback 2026-07-14
            # "PG优化 有新消息但没红点"): if the direct lookup misses, claude
            # rotated its JSONL (compact / clear / auto-fork) and the
            # SessionStart hook that would normally rebind DB.external_session_id
            # didn't fire (or hasn't fired yet). Try to recover by matching
            # the newest live INTERACTIVE session in the same cwd, then rebind
            # so future events for this new claude_sid take the fast path.
            #
            # Ambiguity guard: if multiple live interactive sessions share the
            # cwd, we pick the most-recently-active one and log a warning —
            # otherwise a spurious rebind could steal notifications from the
            # wrong session. In practice users rarely run two claude sessions
            # in the same directory; when they do, the newest is the one the
            # user is actively using.
            if sess is None and event.project_path:
                fallback_conditions = [
                    Session.cwd == event.project_path,
                    Session.type == SessionType.INTERACTIVE,
                    Session.superseded_by.is_(None),
                    Session.status.in_(_LIVE_SESSION_STATUSES),
                ]
                if event_agent:
                    fallback_conditions.append(Session.agent == event_agent)
                fallback_stmt = (
                    select(Session)
                    .where(*fallback_conditions)
                    .order_by(Session.last_activity_ts.desc().nullslast())
                    .limit(2)
                )
                res2 = await db.execute(fallback_stmt)
                candidates = list(res2.scalars().all())
                if candidates:
                    if len(candidates) > 1:
                        log.warning(
                            "[assistant_done_rebind] multiple live interactive "
                            "sessions in cwd=%s; picking newest sid=%s (others: %s)",
                            event.project_path,
                            candidates[0].id,
                            [c.id for c in candidates[1:]],
                        )
                    picked = candidates[0]
                    # HIJACK guard: only recover a genuine in-session rotation
                    # of THIS session's process. If an unrelated claude is just
                    # sharing the cwd, skip — rebinding would steal a correctly
                    # pre-bound (`--session-id`) row's external_session_id.
                    if not _is_rotation_not_hijack(
                        picked.pid,
                        picked.external_session_id,
                        event.session_id,
                        picked.cwd,
                    ):
                        log.info(
                            "[assistant_done_rebind] skip hijack: sid=%s stays on "
                            "%s; %s belongs to another claude in cwd=%s",
                            picked.id,
                            picked.external_session_id,
                            event.session_id,
                            event.project_path,
                        )
                        return
                    old_cs = picked.external_session_id
                    picked.external_session_id = event.session_id
                    rollout_path = (event.payload or {}).get("rollout_path")
                    if rollout_path and not picked.rollout_path:
                        picked.rollout_path = str(rollout_path)
                    try:
                        await db.commit()
                    except Exception:
                        # Unique-constraint on ux_session_claude_sid_active can
                        # fire if another live row already claims this claude_sid.
                        # Roll back and drop the event — better to lose one
                        # notification than to corrupt cross-session state.
                        await db.rollback()
                        log.warning(
                            "[assistant_done_rebind] commit failed sid=%s "
                            "old=%s new=%s (unique-constraint?); event dropped",
                            picked.id, old_cs, event.session_id,
                        )
                        return
                    log.info(
                        "[assistant_done_rebind] sid=%s external_session_id %s → %s "
                        "(recovered from rotation via cwd=%s)",
                        picked.id, old_cs, event.session_id, event.project_path,
                    )
                    sess = picked
                    # Evict stale dedup state keyed by the old claude_sid so the
                    # first post-rebind event lands as a fresh NEW_MESSAGE row.
                    if old_cs:
                        self._last_assistant_done_bump.pop(picked.id, None)
                        self._last_new_msg.pop(picked.id, None)
            if sess is None or sess.type != SessionType.INTERACTIVE:
                return
            # Root fix for "点击消息跳转到 exited session" (2026-07-24):
            # if the matched row is in a terminal state (EXITED / CRASHED /
            # ORPHANED), the JSONL activity is coming from OUTSIDE CSM —
            # user probably ran `claude --resume <sid>` in their shell, or
            # some other tool re-opened the transcript. CSM has no way to
            # safely notify the user via a dead row: the deep-link would
            # take them to the ended session UI, which is exactly what the
            # user complained about. Drop the event silently; log so drift
            # is spottable in csm.log without an operator having to hunt.
            if sess.status not in _LIVE_SESSION_STATUSES:
                log.info(
                    "[assistant_done_drop] target sid=%s csid=%s status=%s cwd=%s "
                    "(JSONL activity for a terminal-state row, likely external "
                    "--resume or state drift); notification suppressed",
                    sess.id,
                    event.session_id,
                    sess.status.value if hasattr(sess.status, "value") else sess.status,
                    sess.cwd,
                )
                return
            sid = sess.id

        # Assistant just produced output — any pending "Permission required"
        # notif for this sid is by definition resolved. Reap it here in
        # addition to the hook-side sweep in `_route`, because the hook
        # path can drop the tool-progress event under load, but the JSONL
        # tail always eventually emits MESSAGE_ASSISTANT_DONE.
        await self._clear_pending_permission_notif(sid)

        # Serialize per-session dedup work so two concurrent events don't
        # both insert a fresh row.
        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            # Cross-source dedup: swallow the second MESSAGE_ASSISTANT_DONE
            # for a given turn. Both the hook Stop path AND the JSONL tail
            # emit this event per turn; without this guard, unread_count
            # bumps by 2 per turn.
            now_pre = now_utc_naive()
            # Which producer is this? The hook path stamps `hook_event_name`
            # into the payload (api/hooks.py); the JSONL tail never does.
            source = "hook" if (event.payload or {}).get("hook_event_name") else "jsonl"
            prev_bump = self._last_assistant_done_bump.get(sid)
            if prev_bump is not None:
                prev_ts, prev_source = prev_bump
                since_prev = (now_pre - prev_ts).total_seconds()
                # The other producer reporting the SAME turn. Don't create or
                # merge a second row — just let it improve what we already
                # showed (the JSONL event carries the authoritative text; the
                # hook one often has none and falls back to the *previous*
                # turn's message, which is why the first of each pair used to
                # show stale body text).
                if (
                    prev_source not in ("both", source)
                    and since_prev < self._cross_source_window_sec
                ):
                    # "both" = this turn has now been seen by both producers,
                    # so the next event — whatever its source — is a new turn
                    # and must not be swallowed by this same branch.
                    self._last_assistant_done_bump[sid] = (now_pre, "both")
                    log.info(
                        "[assistant_done_dedup] cross-source sid=%s %s→%s "
                        "since_prev=%.3fs window=%.1fs",
                        sid, prev_source, source, since_prev,
                        self._cross_source_window_sec,
                    )
                    await self._absorb_duplicate_assistant_done(sid, event)
                    return
                # Rapid repeat from the same producer (or a third event for a
                # turn both have already reported).
                #
                # Diagnostic breadcrumb for local:26deb045 — the user reports
                # occasionally missing "new message" red-dot even though the
                # terminal clearly finished output. This log lets us tell
                # whether the miss is caused by the 2s dedup window vs the
                # bump getting suppressed for a different reason. Keep the
                # log at info level so it's visible in csm.log without needing
                # to raise the whole logger.
                if since_prev < self._assistant_done_dedup_sec:
                    log.info(
                        "[assistant_done_dedup] suppressed sid=%s since_prev=%.3fs window=%.1fs",
                        sid,
                        since_prev,
                        self._assistant_done_dedup_sec,
                    )
                    return
            self._last_assistant_done_bump[sid] = (now_pre, source)
            # Notifications are decoupled from Session state — we do NOT touch
            # `Session.unread_count` here anymore. The Sessions page no longer
            # renders any "new message" dot; the notification channel (bell +
            # NotificationPanel) is the single source of truth for what's
            # unread. Merge counting for the "N new messages" title lives in
            # memory (`_last_new_msg[sid][2]`).
            async with self._sm() as db:
                sess = await db.get(Session, sid)
                if sess is None:
                    return
                # Feedback 5de334d5: reliably populate Session.last_assistant_msg
                # from the JSONL-derived event payload. The Stop hook path in
                # api/hooks.py only landed in ~25% of sessions (transcript path
                # resolution flaky in some claude versions); EventStream is the
                # authoritative source and always has the text. The eventual
                # `await db.commit()` below (merge or insert branch) persists it.
                assistant_text = event.payload.get("assistant_text") if event.payload else None
                if isinstance(assistant_text, str) and assistant_text:
                    sess.last_assistant_msg = assistant_text
                # Preview snippet for the notification body — same value
                # feeds the in-app panel's clamped subtitle and the Lark
                # push body. Fallback to sess.last_assistant_msg when the
                # current event has no text (tool-only turn, or adapter
                # that emits MESSAGE_ASSISTANT_DONE without assistant_text)
                # so the notification always carries a preview.
                snippet_source = (
                    assistant_text if isinstance(assistant_text, str) and assistant_text
                    else (sess.last_assistant_msg or "")
                )
                snippet = _snippet(snippet_source) if snippet_source else ""
                now = now_utc_naive()
                last = self._last_new_msg.get(sid)
                # Opportunistic prune of expired entries while we're here.
                if last is not None and now - last[1] >= self._dedup_window:
                    self._last_new_msg.pop(sid, None)
                    last = None

                # Snapshot user-provided title (if any) so the notification row
                # is self-describing — the panel shouldn't need to cross-join
                # against the session table to show "which session" this
                # message came from. Session titles are stable; if the user
                # renames later, existing notifs keep the old title which is
                # fine (points to what they saw at the time).
                session_label = sess.title or sid[:8]

                if last is not None:
                    last_id, _last_ts, prev_count = last
                    existing = await db.get(Notification, last_id)
                    if existing is not None:
                        # Same session within window → merge.
                        new_count = prev_count + 1
                        # Re-open it (in case mark_read happened in between).
                        existing.read_at = None
                        plural = "messages" if new_count > 1 else "message"
                        existing.title = f"{new_count} new {plural}"
                        existing.created_at = now
                        # Replace the body with the latest snippet — users
                        # want to see the *most recent* message, not the
                        # first one from N minutes ago. Skip if we didn't
                        # get one this event (rare — hook fired without text).
                        if snippet:
                            existing.body = snippet
                        # Refresh session_title in case the row pre-dates the
                        # metadata field or the session was renamed.
                        meta = dict(existing.notif_metadata or {})
                        meta["session_title"] = session_label
                        meta["agent"] = sess.agent
                        existing.notif_metadata = meta
                        self._last_new_msg[sid] = (last_id, now, new_count)
                        await db.commit()
                        await self._push_inapp(existing)
                        return
                    # Stale pointer: the row we were merging into has been
                    # hard-deleted (session purge cascade, manual DB edit).
                    # Drop the pointer and fall through to the INSERT branch
                    # so this event still surfaces — otherwise NEW_MESSAGE
                    # events for this session silently vanish until the
                    # 5s dedup window expires each time (feedback bug P0).
                    self._last_new_msg.pop(sid, None)

                n = Notification(
                    type=NotificationType.NEW_MESSAGE,
                    session_id=sid,
                    title="1 new message",
                    body=snippet or None,
                    notif_metadata={
                        "external_session_id": event.session_id,
                        "session_title": session_label,
                        "agent": sess.agent,
                    },
                )
                db.add(n)
                await db.commit()
                await db.refresh(n)
                self._last_new_msg[sid] = (n.id, now, 1)
                await self._push_inapp(n)

    async def _absorb_duplicate_assistant_done(self, sid: str, event: Event) -> None:
        """Fold the second producer's report of a turn into the row we already
        pushed, instead of raising another notification.

        Only the text is taken: the JSONL payload is authoritative, while the
        hook path frequently has no `assistant_text` at all and falls back to
        `Session.last_assistant_msg` — which at that moment still holds the
        PREVIOUS turn's reply. That is why the first row of every duplicate
        pair carried stale body text.

        In-app push only. Going through the Lark fan-out here would deliver the
        same turn to Lark twice, which is half of what this dedup exists to
        stop.
        """
        text = (event.payload or {}).get("assistant_text")
        if not isinstance(text, str) or not text:
            return
        snippet = _snippet(text)
        if not snippet:
            return
        async with self._sm() as db:
            sess = await db.get(Session, sid)
            if sess is not None:
                sess.last_assistant_msg = text
            row: Notification | None = None
            last = self._last_new_msg.get(sid)
            if last is not None:
                row = await db.get(Notification, last[0])
            if row is None:
                # Merge pointer already expired (its window is shorter than the
                # cross-source one) — fall back to the newest row for this sid.
                row = (
                    await db.execute(
                        select(Notification)
                        .where(
                            Notification.session_id == sid,
                            Notification.type == NotificationType.NEW_MESSAGE,
                        )
                        .order_by(Notification.created_at.desc())
                        .limit(1)
                    )
                ).scalars().first()
            changed = row is not None and row.body != snippet
            if changed:
                row.body = snippet
            await db.commit()
            if changed:
                await self._push_inapp(row, lark=False)

    async def _session_label(self, sid: str | None) -> str | None:
        """Best-effort friendly name for a CSM session id: `Session.title`
        if set, else the 8-char id prefix. None if sid is missing or the
        row is gone. Used to stamp `session_title` into notification
        metadata so the panel's session tag is human-readable across all
        notification types (not just NEW_MESSAGE).
        """
        if not sid:
            return None
        try:
            async with self._sm() as db:
                sess = await db.get(Session, sid)
                if sess is None:
                    return sid[:8]
                return sess.title or sid[:8]
        except Exception:
            return sid[:8]

    async def _emit_notification(
        self,
        type: NotificationType,
        title: str,
        body: str | None,
        session_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> Notification:
        async with self._sm() as db:
            n = Notification(
                type=type,
                session_id=session_id,
                title=title,
                body=body,
                notif_metadata=metadata or {},
            )
            db.add(n)
            await db.commit()
            await db.refresh(n)
        await self._push_inapp(n)
        return n

    async def _push_inapp(self, n: Notification, *, lark: bool = True) -> None:
        # `read_at` / `dismissed_at` included so that in-place UPDATE pushes
        # (permission auto-clear, future state-transition pushes) reach the
        # frontend WS store's upsert-by-id path and clear the bell badge
        # without a full page reload. On creation these are null, which is
        # what the frontend already assumes for fresh rows.
        payload = {
            "id": n.id,
            "type": n.type.value if hasattr(n.type, "value") else n.type,
            "session_id": n.session_id,
            "title": n.title,
            "body": n.body,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "read_at": n.read_at.isoformat() if n.read_at else None,
            "dismissed_at": n.dismissed_at.isoformat() if n.dismissed_at else None,
            "metadata": n.notif_metadata or {},
        }
        try:
            await self._inapp.send(payload)
        except Exception:
            log.exception("inapp push failed for notification %s", n.id)
        # Fan-out to LarkSink (F5) — fire-and-forget so a hung `lark-cli`
        # (10s timeout inside _shell_send) can't block the EventStream
        # subscriber chain. Any exception is logged on the background
        # task so failures remain observable; the caller is not affected.
        # We keep a strong reference in `_pending_lark_tasks` so Python
        # doesn't garbage-collect an in-flight task before it finishes.
        if self._lark is not None and lark:
            async def _lark_send_bg(payload=payload, nid=n.id) -> None:
                try:
                    await self._lark.send(payload)
                except Exception:
                    log.exception("lark sink push failed for notification %s", nid)
            t = asyncio.create_task(_lark_send_bg())
            self._pending_lark_tasks.add(t)
            t.add_done_callback(self._pending_lark_tasks.discard)
