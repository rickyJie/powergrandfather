"""Claude Event Stream — central in-memory pub/sub for derived domain events.

Subscribes nothing externally. Owns:
  - one tail loop polling ~/.claude/projects every N seconds
  - one watchdog loop deriving idle / crashed every M seconds
  - a 1000-event ring buffer for replay
  - subscription registry (sync set of (types, async_handler))

Translation from raw JSONL records to Events happens here so the adapter stays
content-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from csm.adapters.jsonl_tail import JsonlTailer, RawRecord
from csm.core.events import Event, EventType
from csm.core.transcript_provenance import is_headless_session, is_injected_user_record
from csm.models import FileState
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)
_LEGACY_AGENT_NAME = "claude"
# Rows per `file_state` write transaction. Only matters for the first flush
# after a fresh DB (steady state is a handful of rows); bounds how long that
# one pass can hold SQLite's single writer.
_FILE_STATE_FLUSH_CHUNK = 1000
# Floor on the tail loop's inter-tick sleep. A tick that overruns the whole
# period should run back-to-back to catch up; this only stops a degenerate
# no-op tick from spinning. Clamped to `poll_interval` so sub-second intervals
# (tests) still behave.
_MIN_TAIL_SLEEP_SEC = 0.05
# (agent, last_offset, last_mtime, session_id) — every column `_flush_file_state`
# writes, so "already durable" can be decided without touching the DB.
_DurableFileState = tuple[str, int, float, str | None]

# ---- helpers ----
_HIT_LIMIT_RE = re.compile(r"hit your limit", re.IGNORECASE)
_RESET_RE = re.compile(r"resets\s+(\d{1,2}:\d{2})\s*(am|pm)?\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_text(content: Any) -> str:
    """Pull all text segments from a message content (list of blocks or str)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and "text" in c and isinstance(c["text"], str):
                parts.append(c["text"])
        return "\n".join(parts)
    return ""


Handler = Callable[[Event], Awaitable[None]]


class _Subscription:
    __slots__ = ("id", "types", "handler")

    def __init__(self, types: set[EventType] | None, handler: Handler):
        self.id = str(uuid.uuid4())
        self.types = types        # None means "all"
        self.handler = handler


class EventStream:
    """Central in-mem pub/sub fed by an incremental tail of `~/.claude/projects/`.

    Lifecycle:
      - Constructed by `main.lifespan`, kept on `app.state.event_stream`,
        shared by every module that needs Claude activity awareness.
      - `start()` launches the tail loop (poll every `poll_interval_sec`) and
        a watchdog loop (every `watchdog_interval_sec`). `stop()` cancels both.

    Subscription contract:
      - `subscribe(types, handler)` returns a `sub_id`. `types=None` subscribes
        to all events. Handlers are async; they MUST NOT raise — exceptions
        are caught and swallowed to keep the stream alive (but observed via
        a logger if a handler logs them).
      - Multiple subscribers per type are allowed. Delivery order is
        registration order; delivery is awaited sequentially per event.

    Replay:
      - The last `ring_size` (default 1000) events are kept in memory for
        `replay(since=, session_id=)` so late-subscribing consumers can catch
        up without scanning JSONL themselves.

    Idle / crashed events:
      - The tail loop only emits "JSONL changed" type events. `session.idle`
        is emitted by the watchdog when a file has not been touched for
        `session_idle_minutes`. `session.ended` / `session.crashed` are NOT
        emitted by EventStream — Session Manager owns those because only it
        knows the PID.
    """

    def __init__(
        self,
        projects_root: Path,
        poll_interval_sec: float = 5.0,
        watchdog_interval_sec: float = 60.0,
        session_idle_minutes: int = 30,
        ring_size: int = 1000,
        sessionmaker=None,
        flush_interval_sec: float = 30.0,
        seed_stale_history_as_idle: bool = True,
        adapter_registry=None,   # AdapterRegistry | None
    ):
        # Legacy claude-only tailer path (kept during transition — tests
        # that don't wire a registry still work). When `adapter_registry`
        # is supplied, multi-adapter scan takes over in _tick_once and
        # this tailer is only used for the watchdog + newly-seen bookkeep.
        self._tailer = JsonlTailer(projects_root)
        # Multi-agent v2: when set, `_tick_once` fans out `scan_events()`
        # to every enabled adapter in parallel (asyncio.gather +
        # return_exceptions so one slow/failing adapter doesn't block
        # others). When None, we fall back to the legacy single-tailer
        # path — kept for tests that construct EventStream directly
        # without going through lifespan.
        self._registry = adapter_registry
        self._subs: dict[str, _Subscription] = {}
        self._recent: deque[Event] = deque(maxlen=ring_size)
        self._poll_interval = poll_interval_sec
        self._watchdog_interval = watchdog_interval_sec
        self._idle_threshold = session_idle_minutes * 60
        self._tail_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # per-session metadata accumulated from records (for derived events).
        self._session_meta: dict[str, dict[str, Any]] = {}
        # tracks which sessions we've already emitted ended/crashed for.
        self._ended_emitted: set[str] = set()
        # Finding-6b (test-run-2 discovery): the watchdog used to re-emit
        # SESSION_IDLE for every stale JSONL on every tick — with 10k+
        # historical transcripts under ~/.claude/projects this blew up
        # csm.log in minutes and drowned every legitimate idle signal.
        # Track per-session "already emitted idle" so we fire at most once
        # per (session, EventStream lifetime).
        self._idle_emitted: set[str] = set()
        # First watchdog tick after boot primes (does NOT emit) every
        # already-stale session: it belongs to a previous life and did not
        # "just" cross the idle threshold. This makes the watchdog self-priming
        # and independent of `_seed_idle_emitted_from_history`, which used a
        # different enumeration (artifact_glob vs tail_states) and under-covered
        # — priming ~3.2k of ~15.4k stale sessions, so the first tick emitted
        # 12k+ no-op SESSION_IDLE events (subs=0) that drowned csm.log.
        self._watchdog_first_tick: bool = True
        # Persistence: if a sessionmaker is provided, restore tail offsets from
        # `file_state` at start() and periodically flush back so a backend
        # restart doesn't re-read every JSONL from offset=0 (which would cause
        # subscribers like TokenAggregator to duplicate every historical row).
        self._sm = sessionmaker
        self._flush_interval = flush_interval_sec
        self._last_flush_ts: float = 0.0
        # What `file_state` already holds, so a flush can send only the rows
        # whose offset actually moved. None until seeded (lazily, or by
        # `_restore_file_state` which reads the same rows anyway). See
        # `_flush_file_state` for why writing all of them was so expensive.
        self._durable_file_state: dict[str, _DurableFileState] | None = None
        # Test hook: disable boot-time stale-history seeding for unit tests
        # that intentionally point EventStream at a synthetic old-mtime file
        # and want SESSION_IDLE to fire. Production defaults to True so a
        # workstation with 10k+ historical transcripts doesn't blow up csm.log.
        self._seed_stale_history_as_idle = seed_stale_history_as_idle

    # ---- subscriptions ----
    def subscribe(self, types: list[EventType] | None, handler: Handler) -> str:
        sub = _Subscription(set(types) if types else None, handler)
        self._subs[sub.id] = sub
        return sub.id

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    def replay(self, since: datetime | None = None, session_id: str | None = None) -> list[Event]:
        out = []
        for e in self._recent:
            if since and e.ts < since:
                continue
            if session_id and e.session_id != session_id:
                continue
            out.append(e)
        return out

    # ---- emission ----
    async def emit(self, event: Event) -> None:
        self._recent.append(event)
        # Finding-6d (TR3): when a session ends (or crashes) we know that
        # session id will never generate fresh activity again. Add it to
        # `_ended_emitted` so the watchdog stops walking its stale JSONL
        # each tick, and drop it from `_idle_emitted` so the set doesn't
        # grow unbounded across a long-running CSM. Requires event
        # `session_id` to be the claude uuid (JSONL basename); the
        # session_manager populates it there from `Session.external_session_id`.
        if event.type in (EventType.SESSION_ENDED, EventType.SESSION_CRASHED):
            sid = event.session_id
            if sid:
                self._ended_emitted.add(sid)
                self._idle_emitted.discard(sid)
        # Snapshot subs to avoid mutation during iteration.
        subs = list(self._subs.values())
        matched_subs = [sub for sub in subs if sub.types is None or event.type in sub.types]
        matched = len(matched_subs)

        async def _safe_dispatch(sub: _Subscription) -> None:
            try:
                await sub.handler(event)
            except Exception:
                # Finding-6: previously swallowed silently, which hid a real
                # bug where SESSION_IDLE never reached AutomationRunner
                # because the handler was raising on a code path we couldn't
                # see. Log now — subscribers still MUST NOT break the stream.
                log.exception(
                    "event_stream subscriber failed: type=%s sub_id=%s sid=%s",
                    event.type.value if hasattr(event.type, "value") else event.type,
                    sub.id,
                    event.session_id,
                )

        # E1 (2026-07-25): dispatch subscribers concurrently so a slow one
        # (e.g. SupervisorAgent's `claude -p` call, 10-60s) does not block
        # any other subscriber. Each handler is wrapped in `_safe_dispatch`
        # which catches and logs, so `asyncio.gather` will not raise.
        # Subscribers dispatched concurrently — do not rely on invocation order.
        if matched_subs:
            await asyncio.gather(
                *(_safe_dispatch(sub) for sub in matched_subs),
                return_exceptions=False,
            )
        if event.type == EventType.SESSION_IDLE:
            log.info(
                "event_stream dispatched SESSION_IDLE sid=%s idle=%ss subs=%d matched=%d",
                event.session_id,
                (event.payload or {}).get("idle_seconds"),
                len(subs),
                matched,
            )

    # ---- lifecycle ----
    async def start(self) -> None:
        await self._restore_file_state()
        if self._seed_stale_history_as_idle:
            self._seed_idle_emitted_from_history()
        if self._tail_task is None:
            self._tail_task = asyncio.create_task(self._tail_loop(), name="csm-event-tail")
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="csm-event-watchdog")

    async def stop(self) -> None:
        self._stopping.set()
        for t in (self._tail_task, self._watchdog_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._tail_task = None
        self._watchdog_task = None
        # Final best-effort flush.
        try:
            await self._flush_file_state()
        except Exception:
            pass

    def _seed_idle_emitted_from_history(self) -> None:
        """Prime `_idle_emitted` with every jsonl already stale at boot.

        Finding-6b (test-run-2): a workstation with 10k+ historical
        transcripts under `~/.claude/projects` would fire 10k+ SESSION_IDLE
        events on the first watchdog tick after boot, drowning csm.log
        and hiding every legitimate live-session idle. The semantics of
        SESSION_IDLE are "session just crossed the idle threshold" — a
        transcript that was already stale before EventStream started
        belongs to a previous life and should not trigger fresh signals.

        We glob the projects tree directly (no JSONL parse needed — just
        stat) so this runs before any subscribers are attached and
        without waiting for the tail loop's first pass.
        """
        try:
            now_ts = time.time()
            seeded = 0
            # Multi-agent v2: iterate every registered adapter's artifact
            # glob so a workstation with 10k+ historical codex rollouts
            # doesn't blow up SESSION_IDLE on the first tick either.
            # Falls back to the legacy claude-only path if no registry
            # is attached (test compat).
            if self._registry is not None:
                from glob import glob as _glob
                for adapter in self._registry.all():
                    for path in _glob(adapter.artifact_glob(), recursive=True):
                        try:
                            mtime = os.path.getmtime(path)
                        except OSError:
                            continue
                        if now_ts - mtime > self._idle_threshold:
                            self._idle_emitted.add(Path(path).stem)
                            seeded += 1
            else:
                root = self._tailer.projects_root
                for path in root.rglob("*.jsonl"):
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if now_ts - mtime > self._idle_threshold:
                        self._idle_emitted.add(path.stem)
                        seeded += 1
            if seeded:
                log.info(
                    "event_stream primed %d already-stale sessions to skip on first watchdog tick",
                    seeded,
                )
        except Exception:
            log.exception("event_stream _seed_idle_emitted_from_history failed; continuing")

    # ---- file_state persistence (B10 fix) ----
    async def _prune_missing_artifacts(self, rows: list[FileState]) -> set[str]:
        """Forget rows whose transcript no longer exists. Boot-time only.

        The table only ever grew. On this box 12,838 of 15,581 rows (82.4%)
        pointed at claude transcripts that had already been deleted — nothing
        read them, but every restart loaded them, the tailer kept them all in
        its map, and (before the incremental flush) every pass rewrote them.
        That volume is what made one flush long enough to matter, so making the
        write cheap without shrinking the set only treats the symptom.

        Boot-time only, deliberately: this is the one moment where nothing else
        is mid-flight, so the DB delete, the tailer seed and the mirror can be
        derived from a single filtered list. A periodic runtime prune would
        have to keep `_durable_file_state` in step under concurrency — see the
        INVARIANT note on `_flush_file_state` for why that is easy to get
        silently wrong.

        Guard: prune an agent's rows only if at least one of ITS artifacts
        still exists. A whole tree missing means an unmounted filesystem, not
        15k deletions, and forgetting those offsets would re-tail every
        transcript from byte 0 and replay every event in it.
        """
        if self._sm is None or not rows:
            return set()
        by_agent: dict[str, list[str]] = {}
        for r in rows:
            by_agent.setdefault(r.agent, []).append(r.artifact_path)

        def _scan() -> dict[str, list[str]]:
            found: dict[str, list[str]] = {}
            for agent, paths in by_agent.items():
                gone = [p for p in paths if not os.path.exists(p)]
                if not gone or len(gone) == len(paths):
                    # `== len(paths)` → nothing of this agent's is readable;
                    # assume the tree is temporarily away and keep the offsets.
                    continue
                found[agent] = gone
            return found

        # 15k stat() calls — off the loop, and only once per boot.
        missing = await asyncio.to_thread(_scan)
        if not missing:
            return set()
        from sqlalchemy import delete
        pruned: set[str] = set()
        for agent, gone in missing.items():
            for i in range(0, len(gone), _FILE_STATE_FLUSH_CHUNK):
                chunk = gone[i:i + _FILE_STATE_FLUSH_CHUNK]
                async with self._sm() as db:
                    await db.execute(
                        delete(FileState).where(FileState.artifact_path.in_(chunk))
                    )
                    await db.commit()
                pruned.update(chunk)
            log.info(
                "file_state: pruned %d/%d rows for agent=%s whose artifact is gone",
                len(gone), len(by_agent[agent]), agent,
            )
        return pruned

    async def _restore_file_state(self) -> None:
        if self._sm is None:
            return
        try:
            from sqlalchemy import select
            async with self._sm() as db:
                rows = (await db.execute(select(FileState))).scalars().all()
            # Drop rows whose transcript is gone BEFORE anything downstream is
            # derived from them, so the DB, the tailer seed and the in-memory
            # mirror are all built from the same surviving set.
            pruned = await self._prune_missing_artifacts(rows)
            if pruned:
                rows = [r for r in rows if r.artifact_path not in pruned]
            # Same rows the flush would otherwise have to re-read to work out
            # what is already durable — seed it here for free.
            self._durable_file_state = {
                r.artifact_path: (r.agent, r.last_offset, r.last_mtime, r.session_id)
                for r in rows
            }
            if self._registry is not None:
                rows_by_agent: dict[str, dict[str, dict[str, Any]]] = {}
                for r in rows:
                    rows_by_agent.setdefault(r.agent, {})[r.artifact_path] = {
                        "offset": r.last_offset,
                        "mtime": r.last_mtime,
                        "line_no": 0,
                        "session_id": r.session_id,
                        # CodexRolloutTailer accepts this adapter-specific key.
                        "codex_session_id": r.session_id or "",
                    }
                for adapter in self._registry.all():
                    adapter.restore(rows_by_agent.get(adapter.name, {}))
                return
            # Legacy test path: restore only Claude rows into the old tailer.
            rows = [r for r in rows if r.agent == _LEGACY_AGENT_NAME]
            snap = {
                r.artifact_path: {"offset": r.last_offset, "mtime": r.last_mtime, "line_no": 0}
                for r in rows
            }
            self._tailer.restore(snap)
        except Exception:
            log = __import__("logging").getLogger(__name__)
            log.exception("file_state restore failed; falling back to fresh tail")

    async def _sync_external_title(
        self,
        *,
        external_session_id: str,
        new_title: str,
        source: str,
    ) -> None:
        """Reflect a claude-side title (custom-title / ai-title) into
        `session.title`. Skips rows the user has locked via
        `title_manual=true`. Chain-tail only — if the session has been
        superseded (resumed), the tail row wins. Idempotent — a repeat
        record with the same string is a no-op.
        """
        if self._sm is None:
            return
        from sqlalchemy import select as _select

        from csm.models import Session
        try:
            async with self._sm() as db:
                stmt = (
                    _select(Session)
                    .where(
                        Session.external_session_id == external_session_id,
                        Session.superseded_by.is_(None),
                    )
                    .limit(1)
                )
                sess = (await db.execute(stmt)).scalar_one_or_none()
                if sess is None:
                    return
                if sess.title_manual:
                    return
                # For ai-title, defer to any existing title (user-typed via
                # UI OR previously synced custom-title) — ai is the weakest
                # source. For custom-title (user typed inside claude), take
                # precedence over any earlier ai-title.
                if source == "ai-title" and sess.title:
                    return
                if sess.title == new_title:
                    return
                sess.title = new_title
                await db.commit()
        except Exception:
            log = __import__("logging").getLogger(__name__)
            log.exception(
                "external title sync failed sid=%s source=%s",
                external_session_id, source,
            )

    @staticmethod
    def _file_state_upsert():
        """One compiled UPSERT that a whole batch of rows can be fed through.

        `excluded.*` (not literal values) is what makes it reusable — the same
        statement then serves an executemany instead of needing one compile +
        one round-trip per file.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(FileState)
        return stmt.on_conflict_do_update(
            index_elements=[FileState.artifact_path],
            set_={
                "agent": stmt.excluded.agent,
                "last_offset": stmt.excluded.last_offset,
                "last_mtime": stmt.excluded.last_mtime,
                "session_id": stmt.excluded.session_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )

    async def _load_durable_file_state(self) -> dict[str, _DurableFileState]:
        """What `file_state` already holds, keyed by artifact path.

        Carries every column the flush writes — not just the offset. A codex
        rollout gets its `session_id` bound post-hoc, which can land on a tick
        where the offset did NOT move; comparing on offset alone would skip
        that write and leave the binding unpersisted until the file next grew.
        """
        from sqlalchemy import select
        async with self._sm() as db:
            rows = (await db.execute(
                select(
                    FileState.artifact_path,
                    FileState.agent,
                    FileState.last_offset,
                    FileState.last_mtime,
                    FileState.session_id,
                )
            )).all()
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}

    async def _flush_file_state(self) -> None:
        """Persist the tail offsets that MOVED. Throttled by `_flush_interval`.

        This used to upsert *every* tracked artifact on every flush, one
        statement at a time, inside a single transaction. Each statement is its
        own aiosqlite thread round-trip, and SQLite takes the write lock on the
        first one and does not let go until the commit — so the pass became a
        multi-second exclusive hold that grew with the size of the transcript
        corpus.

        Measured on 2026-08-27 (15,581 tracked files): the write lock was
        unavailable **24% of the time**, in ~9-second blocks arriving every 30s,
        while inserting no rows at all — every row was an unchanged UPDATE.
        The table timestamps the hold for us: the old code stamped `updated_at`
        per row, so all 15,581 rows carry DISTINCT stamps marching monotonically
        from 04:59:14.607282 to 04:59:23.538832 — one pass, 8.93 seconds, ten
        one-second buckets. Everything else that writes queued behind it: the
        notification INSERT for a finished turn, claude's hooks, worktime
        heartbeats, mark-read. That queue is what made "new message" surface
        seconds after the reply was already on screen; reads were never affected
        (WAL readers don't block), which is exactly the asymmetry the request
        log showed — 1 slow read in 21,864 vs 24.3% of writes over a second.

        Offsets only move for files that were appended to since the last flush —
        a handful per tick — so diff against what is already durable and send
        just those, as one executemany.

        INVARIANT: this and `_prune_missing_artifacts` are the only writers of
        `file_state`. `_durable_file_state` mirrors the table in memory and is
        trusted without re-reading, so a new writer (or a runtime prune) MUST
        update the mirror in the same step. Getting that wrong doesn't raise —
        the row silently stops being persisted, and after a restart its
        transcript is re-tailed from byte 0 and every event in it replays.
        """
        if self._sm is None:
            return
        now = time.time()
        if now - self._last_flush_ts < self._flush_interval:
            return
        self._last_flush_ts = now
        try:
            if self._durable_file_state is None:
                self._durable_file_state = await self._load_durable_file_state()
            durable = self._durable_file_state
            snapshots = (
                [(a.name, a.snapshot()) for a in self._registry.enabled()]
                if self._registry is not None
                else [(_LEGACY_AGENT_NAME, self._tailer.snapshot())]
            )
            stamp = now_utc_naive()
            changed: list[dict[str, Any]] = []
            for agent, snap in snapshots:
                for path, st in snap.items():
                    external_id = st.get("codex_session_id") or st.get("session_id")
                    if external_id is None and self._registry is None:
                        external_id = Path(path).stem
                    offset, mtime = st["offset"], st["mtime"]
                    if durable.get(path) == (agent, offset, mtime, external_id):
                        continue
                    changed.append({
                        "artifact_path": path,
                        "agent": agent,
                        "last_offset": offset,
                        "last_mtime": mtime,
                        "session_id": external_id,
                        "updated_at": stamp,
                    })
            if not changed:
                # Nothing moved — don't open a write transaction at all. This
                # is the steady state on an idle console.
                return
            stmt = self._file_state_upsert()
            # Chunked so the one pass that IS large (first flush against a
            # fresh DB) still can't hold the writer for an unbounded stretch.
            for i in range(0, len(changed), _FILE_STATE_FLUSH_CHUNK):
                chunk = changed[i:i + _FILE_STATE_FLUSH_CHUNK]
                async with self._sm() as db:
                    await db.execute(stmt, chunk)
                    await db.commit()
                # Only after the commit is durable — a chunk that raised stays
                # out of the map and is retried on the next flush.
                for row in chunk:
                    durable[row["artifact_path"]] = (
                        row["agent"], row["last_offset"],
                        row["last_mtime"], row["session_id"],
                    )
        except Exception:
            log = __import__("logging").getLogger(__name__)
            log.exception("file_state flush failed")

    # ---- tail loop ----
    async def _tail_loop(self) -> None:
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                await self._tick_once()
            except Exception:
                log = __import__("logging").getLogger(__name__)
                log.exception("event_stream tail tick failed")
            # Periodic durability checkpoint (throttled inside _flush_file_state).
            try:
                await self._flush_file_state()
            except Exception:
                pass
            # Fixed cadence, not "work, then sleep poll_interval". The sleep
            # used to sit on TOP of the tick, so the real detection period was
            # poll_interval + however long the tick took — and the tick is what
            # gets slow under load, exactly when detection latency matters. A
            # turn that only the JSONL tail can see (no Stop hook: every codex
            # session, plus claude sessions whose hook didn't land) waits a
            # whole period to be noticed, so the drift lands straight on the
            # user as a late notification.
            floor = min(self._poll_interval, _MIN_TAIL_SLEEP_SEC)
            delay = max(floor, self._poll_interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except TimeoutError:
                continue
            else:
                break

    async def _tick_once(self) -> None:
        """Fan out `scan_events()` across every enabled adapter concurrently,
        then emit whatever they produced.

        Concurrency + isolation (backend-review P0): each adapter runs in
        its own thread via `asyncio.to_thread` and results are collected
        with `return_exceptions=True` so one slow/failing adapter can't
        block others. Legacy single-tailer path is preserved for tests
        that don't wire an adapter_registry.
        """
        # ---- multi-adapter path (production) ----
        if self._registry is not None:
            adapters = self._registry.enabled()
            if adapters:
                results = await asyncio.gather(
                    *(asyncio.to_thread(a.scan_events) for a in adapters),
                    return_exceptions=True,
                )
                for adapter, result in zip(adapters, results, strict=True):
                    if isinstance(result, BaseException):
                        log.exception(
                            "adapter %s scan_events raised — skipping tick",
                            adapter.name,
                            exc_info=result,
                        )
                        continue
                    events = list(result)
                    for ev in events:
                        ev.payload.setdefault("agent", adapter.name)
                    # Project the WHOLE tick's lifecycle state in ONE
                    # transaction before publishing anything (backend-review
                    # W1) — a burst of N events no longer means N serialized
                    # writes monopolizing the SQLite writer. Emitting only after
                    # the batch commit preserves the invariant that an SSE
                    # consumer reacting to message.assistant_done never reads a
                    # stale RUNNING (state is already committed for every event
                    # in the tick).
                    await self._project_adapter_events(events, adapter.name)
                    for ev in events:
                        await self.emit(ev)
            # Even in registry mode, the legacy tailer keeps running so
            # `_handle_record` can populate `_session_meta` (used by the
            # watchdog + introspection). Draining below is a no-op if
            # ClaudeAdapter is already handling the same files, but the
            # cost is bounded and the observability payoff is real. The
            # extra records don't double-emit user-visible events —
            # `_handle_record` produces derived events which the emit()
            # dedup ring naturally absorbs; adapters emit canonical
            # events which land on the same ring. NOT a correctness
            # issue for our subscribers (all consume idempotently by id).
            # M6 cleanup will remove the legacy tail path entirely once
            # session_meta consumers migrate to the adapter surface.
            return

        # ---- legacy single-tailer path (test compat only) ----
        records = await asyncio.to_thread(self._tailer.scan_once)
        for r in records:
            await self._handle_record(r)
        from csm.adapters.jsonl_tail import project_dir_to_cwd
        for path in self._tailer.take_newly_seen():
            sid = Path(path).stem
            meta = self._session_meta.get(sid, {})
            if meta.get("msg_count", 0) == 0:
                continue
            project_dir = Path(path).parent.name
            project_path = project_dir_to_cwd(project_dir)
            await self.emit(Event(
                type=EventType.SESSION_STARTED,
                ts=datetime.now(UTC),
                session_id=sid,
                project_path=project_path,
                payload={"jsonl_path": path},
            ))

    async def _project_adapter_events(
        self,
        events: list[Event],
        adapter_name: str,
    ) -> None:
        """Batch-project one tick's adapter events in a SINGLE transaction.

        Persists adapter-derived lifecycle state onto the matching live Session
        rows. Claude hooks already perform these transitions synchronously, but
        only land in ~25% of sessions (flaky transcript-path resolution), so
        this projection is the authoritative backstop; codex has no hooks at
        all. Transitions: user message -> RUNNING, assistant complete -> IDLE.
        Also lazily binds external_session_id / rollout_path (exact ids win; cwd
        fallback restricted to the newest live unbound row for the adapter).

        backend-review W1: each event used to open its own session + commit, so
        a burst of N events was N serialized SQLite writes that monopolized the
        single WAL writer and starved concurrent interactive writers (mark-read,
        heartbeat, hooks) on busy_timeout — the "一起卡、一起放行" signature.
        Now all projectable events in the tick share one session and ONE commit.
        Per-event IntegrityError (and any error) isolation is preserved with a
        SAVEPOINT (begin_nested + flush) around each event, so a rare
        external_session_id conflict rolls back only that event, not the tick.
        Slow codex title file-IO is deferred to after the commit, outside the
        txn. event.payload['csm_session_id'] is set only after the commit is
        durable, so SSE consumers never reconcile against an uncommitted row.
        """
        if self._sm is None:
            return
        projectable = [
            e for e in events
            if e.type in {
                EventType.SESSION_STARTED,
                EventType.MESSAGE_USER_SENT,
                EventType.MESSAGE_ASSISTANT_DONE,
                EventType.SESSION_TOOL_PROGRESS,
            }
        ]
        if not projectable:
            return

        from sqlalchemy.exc import IntegrityError

        # (row_id, (agent, external_id)) for adapters holding EXTERNAL_TITLE
        title_syncs: list[tuple[str, tuple[str, str]]] = []
        sid_payloads: list[tuple[Event, str]] = []   # (event, row_id) set post-commit
        try:
            async with self._sm() as db:
                for ev in projectable:
                    res: tuple[str, str | None] | None = None
                    try:
                        # SAVEPOINT per event: a flush here surfaces a
                        # unique-constraint conflict for THIS event and rolls
                        # back only it, leaving the rest of the tick intact.
                        async with db.begin_nested():
                            res = await self._stage_projection(db, ev, adapter_name)
                            await db.flush()
                    except IntegrityError:
                        log.warning(
                            "adapter event projection conflict: agent=%s external_id=%s",
                            adapter_name, ev.session_id,
                        )
                        continue
                    except Exception:
                        log.exception(
                            "adapter event projection failed: agent=%s type=%s ext=%s",
                            adapter_name, ev.type.value, ev.session_id,
                        )
                        continue
                    if res is None:
                        continue
                    row_id, title_src = res
                    sid_payloads.append((ev, row_id))
                    if title_src:
                        title_syncs.append((row_id, title_src))
                await db.commit()
        except Exception:
            log.exception(
                "adapter event batch projection failed: agent=%s count=%d",
                adapter_name, len(projectable),
            )
            return

        # Commit is durable → SSE consumers can reconcile these single rows
        # instead of refetching Active + Auto + History for every turn event.
        for ev, row_id in sid_payloads:
            ev.payload["csm_session_id"] = row_id
        # Adapter-held title file-IO OUTSIDE the txn (slow separate-store scan).
        for row_id, (agent, ext) in title_syncs:
            await self._sync_adapter_held_title(row_id, agent, ext)

    async def _stage_projection(
        self,
        db: Any,
        event: Event,
        adapter_name: str,
    ) -> tuple[str, tuple[str, str] | None] | None:
        """Stage one event's Session mutations on the shared ``db`` (NO commit).

        Returns ``(row_id, (agent, external_id) | None)`` — the second element
        is set only when a post-commit title sync is warranted — or ``None``
        when no matching live row was found.
        """
        from sqlalchemy import select

        from csm.models import Session
        from csm.models.session import SessionStatus

        live_statuses = (
            SessionStatus.STARTING,
            SessionStatus.RUNNING,
            SessionStatus.IDLE,
            SessionStatus.WAITING_INPUT,
            SessionStatus.WAITING_AUTH,
        )
        payload = event.payload or {}
        event_agent = str(
            payload.get("agent") or payload.get("backend") or adapter_name
        )
        rollout_path_raw = payload.get("rollout_path")
        rollout_path = (
            str(rollout_path_raw)
            if isinstance(rollout_path_raw, str) and rollout_path_raw
            else None
        )

        row = None
        # Fast path: post-spawn bind already persisted the CLI id.
        if event.session_id:
            stmt = select(Session).where(
                Session.external_session_id == event.session_id,
                Session.agent == event_agent,
                Session.superseded_by.is_(None),
                Session.status.in_(live_statuses),
            )
            row = (await db.execute(stmt)).scalar_one_or_none()

        # Second exact key: the adapter artifact path.
        if row is None and rollout_path:
            stmt = (
                select(Session)
                .where(
                    Session.rollout_path == rollout_path,
                    Session.agent == event_agent,
                    Session.superseded_by.is_(None),
                    Session.status.in_(live_statuses),
                )
                .order_by(Session.started_at.desc())
                .limit(1)
            )
            row = (await db.execute(stmt)).scalars().first()

        # Lazy bind for the post-spawn race / continued-rollout case. Never
        # steal a row that already belongs to a different external id — and
        # never bind a headless `claude -p` transcript at all. CSM spawns those
        # itself (agent-alert helpers, workflow authoring) and cron-driven
        # skills spawn more; they land in whatever project folder their cwd maps
        # to, where this cwd-keyed fallback would hand one to an unrelated
        # session row that merely hasn't bound yet. A row CSM spawned is
        # pre-bound via `--session-id`, so it takes the fast path above and
        # never needs this.
        if (
            row is None
            and event.project_path
            and not is_headless_session(
                self._tailer.projects_root, event.project_path, event.session_id or ""
            )
        ):
            stmt = (
                select(Session)
                .where(
                    Session.cwd == event.project_path,
                    Session.agent == event_agent,
                    Session.external_session_id.is_(None),
                    Session.superseded_by.is_(None),
                    Session.status.in_(live_statuses),
                )
                .order_by(
                    Session.last_activity_ts.desc().nullslast(),
                    Session.started_at.desc(),
                )
                .limit(2)
            )
            candidates = list((await db.execute(stmt)).scalars().all())
            if candidates:
                row = candidates[0]
                if len(candidates) > 1:
                    log.warning(
                        "adapter event lazy-bind is ambiguous: "
                        "agent=%s cwd=%s picked=%s other=%s",
                        event_agent, event.project_path, row.id, candidates[1].id,
                    )

        if row is None:
            return None

        if event.session_id and not row.external_session_id:
            row.external_session_id = event.session_id
        if rollout_path and not row.rollout_path:
            row.rollout_path = rollout_path

        if event.type == EventType.MESSAGE_USER_SENT:
            row.status = SessionStatus.RUNNING
            row.ended_at = None
        elif event.type == EventType.SESSION_TOOL_PROGRESS:
            row.status = SessionStatus.RUNNING
            tool_name = payload.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                # Match the claude hook's `"<Tool>: <arg head>"` shape
                # (api/hooks.py) so both CLIs render identically on the card.
                # `tool_hint` is optional — adapters that can't supply one
                # still get the bare tool name, exactly as before.
                hint = payload.get("tool_hint")
                label = tool_name
                if isinstance(hint, str) and hint.strip():
                    label = f"{tool_name}: {hint.strip()[:80]}"
                row.current_tool = label[:200]
            else:
                row.current_tool = None
        elif event.type == EventType.MESSAGE_ASSISTANT_DONE:
            row.status = SessionStatus.IDLE
            row.current_tool = None
            assistant_text = payload.get("assistant_text")
            if isinstance(assistant_text, str) and assistant_text:
                row.last_assistant_msg = assistant_text[:2000]
        elif (
            event.type == EventType.SESSION_STARTED
            and row.status == SessionStatus.STARTING
        ):
            row.status = SessionStatus.RUNNING

        # `local:7a422f9d` — an adapter that keeps titles in its own state
        # store (Capability.EXTERNAL_TITLE) is read AFTER the commit: that
        # store is a separate file and the scan is SLOW, so it must not run
        # inside this txn.
        title_src = (
            (event_agent, row.external_session_id)
            if (
                row.external_session_id
                and not row.title_manual
                and self._keeps_titles_externally(event_agent)
            )
            else None
        )
        return row.id, title_src

    def _keeps_titles_externally(self, agent: str) -> bool:
        """Does `agent`'s adapter hold titles in its own state store?

        False when no registry is wired (tests that construct EventStream
        directly) — those never see adapter events in the first place, so
        there is no title to sync.
        """
        from csm.backends.base import Capability
        from csm.backends.errors import UnknownAgentError

        if self._registry is None:
            return False
        try:
            adapter = self._registry.get(agent)
        except UnknownAgentError:
            return False
        return Capability.EXTERNAL_TITLE in adapter.capabilities

    async def _sync_adapter_held_title(
        self, row_id: str, agent: str, external_id: str
    ) -> None:
        """Reflect an adapter-held title into Session.title (post-commit, no txn).

        Distinct from `_sync_external_title`, which handles titles claude
        publishes as records inside the JSONL transcript. This one pulls from
        the adapter's own state store instead.

        For codex the title lives in its own sqlite state file; reading it is
        SLOW file IO, so it runs here — after the status write committed and
        outside any open transaction — then writes just the title in a short
        session. Best-effort; guarded by title_manual + "already matches".

        Only reached for adapters declaring Capability.EXTERNAL_TITLE, so
        `lookup_external_title` is safe to call.
        """
        from csm.models import Session

        if not self._keeps_titles_externally(agent):
            return
        try:
            adapter = self._registry.get(agent)
            external_title = await asyncio.to_thread(
                adapter.lookup_external_title, external_id
            )
        except Exception:
            log.exception(
                "external title query failed sid=%s agent=%s ext=%s",
                row_id, agent, external_id,
            )
            return
        if not external_title:
            return
        try:
            async with self._sm() as db:
                r2 = await db.get(Session, row_id)
                if r2 is not None and not r2.title_manual and external_title != r2.title:
                    r2.title = external_title
                    await db.commit()
        except Exception:
            log.exception(
                "external title write failed sid=%s agent=%s ext=%s",
                row_id, agent, external_id,
            )

    async def _handle_record(self, r: RawRecord) -> None:
        obj = r.obj
        ts = _parse_ts(obj.get("timestamp")) or datetime.now(UTC)
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        role = msg.get("role")

        # Update per-session metadata.
        meta = self._session_meta.setdefault(r.claude_session_id, {})
        meta["last_ts"] = ts
        meta["cwd"] = r.project_path
        meta.setdefault("first_ts", ts)
        meta["msg_count"] = meta.get("msg_count", 0) + 1

        # `local:7a422f9d` — claude persists user renames as
        # `{"type":"custom-title","customTitle":"…","sessionId":"…"}` and
        # AI-derived titles as `{"type":"ai-title","aiTitle":"…",…}` inside
        # the same JSONL transcript. Sync into `session.title` when the
        # user hasn't claimed the field via CSM's own rename (guarded by
        # `title_manual`). Best-effort — any DB blip is swallowed so a
        # single bad row can't kill the tailer subscriber.
        rec_type = obj.get("type")
        if rec_type in ("custom-title", "ai-title"):
            new_title = obj.get("customTitle") if rec_type == "custom-title" else obj.get("aiTitle")
            if isinstance(new_title, str) and new_title.strip() and self._sm is not None:
                await self._sync_external_title(
                    external_session_id=r.claude_session_id,
                    new_title=new_title.strip(),
                    source=rec_type,
                )
            return

        # Rate limit hit.
        if obj.get("isApiErrorMessage"):
            content = msg.get("content") if isinstance(msg, dict) else None
            text = _extract_text(content) if content is not None else ""
            await self.emit(Event(
                type=EventType.API_ERROR,
                ts=ts,
                session_id=r.claude_session_id,
                project_path=r.project_path,
                payload={"text": text[:500]},
                source_offset=r.byte_offset,
            ))
            if _HIT_LIMIT_RE.search(text):
                m = _RESET_RE.search(text)
                reset = f"{m.group(1)}{m.group(2) or ''} {m.group(3)}" if m else None
                await self.emit(Event(
                    type=EventType.RATE_LIMIT_HIT,
                    ts=ts,
                    session_id=r.claude_session_id,
                    project_path=r.project_path,
                    payload={"reset_text": reset, "raw": text[:500]},
                    source_offset=r.byte_offset,
                ))

        # User message — may also carry tool_result blocks from the previous turn.
        if role == "user":
            # Role "user" is not the same as "the human spoke": a subagent's
            # task-notification, a skill preamble and an SDK-driven prompt are
            # all filed under it. Emitting MESSAGE_USER_SENT for those told
            # every consumer the user had just typed — flipping the session to
            # RUNNING and opening a worktime interval for work nobody did.
            # Tool-result records stay in (no provenance fields, and they ARE
            # the turn progressing), so turn accounting is unchanged.
            if not is_injected_user_record(obj, _extract_text(msg.get("content"))):
                await self.emit(Event(
                    type=EventType.MESSAGE_USER_SENT,
                    ts=ts,
                    session_id=r.claude_session_id,
                    project_path=r.project_path,
                    payload={},
                    source_offset=r.byte_offset,
                ))
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        await self.emit(Event(
                            type=EventType.TOOL_COMPLETED,
                            ts=ts,
                            session_id=r.claude_session_id,
                            project_path=r.project_path,
                            payload={"tool_use_id": c.get("tool_use_id")},
                            source_offset=r.byte_offset,
                        ))
            return

        # Assistant message — usage + completion + any embedded tool_use blocks.
        # tool_use blocks live INSIDE the assistant message content, so we have
        # to scan it here before the early-return below; otherwise the dead
        # tool-detection code path further down never fires.
        if role == "assistant":
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if usage:
                meta["last_cr"] = usage.get("cache_read_input_tokens", 0)
                await self.emit(Event(
                    type=EventType.USAGE_RECORDED,
                    ts=ts,
                    session_id=r.claude_session_id,
                    project_path=r.project_path,
                    payload={
                        "model": msg.get("model"),
                        "input_tokens": usage.get("input_tokens", 0),
                        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "is_subagent": "/subagents/" in r.jsonl_path,
                    },
                    source_offset=r.byte_offset,
                ))
            # Collect tool_use blocks first so we can split usage N-ways.
            content = msg.get("content") if isinstance(msg, dict) else None
            tool_names: list[str] = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        nm = c.get("name")
                        if nm:
                            tool_names.append(str(nm))
            if tool_names:
                n = len(tool_names)
                u = usage or {}
                share = {
                    "input_tokens": int(u.get("input_tokens", 0) or 0) // n,
                    "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0) // n,
                    "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0) // n,
                    "output_tokens": int(u.get("output_tokens", 0) or 0) // n,
                }
                model = msg.get("model")
                for nm in tool_names:
                    await self.emit(Event(
                        type=EventType.TOOL_INVOKED,
                        ts=ts,
                        session_id=r.claude_session_id,
                        project_path=r.project_path,
                        payload={
                            "name": nm,
                            "jsonl_path": r.jsonl_path,
                            "model": model,
                            **share,
                        },
                        source_offset=r.byte_offset,
                    ))
            # Only emit MESSAGE_ASSISTANT_DONE when claude has actually
            # finished its turn (stop_reason=end_turn). Intermediate stops
            # like tool_use mean more work is coming — firing here would
            # falsely tell the AutomationRunner the session is done.
            stop_reason = msg.get("stop_reason") if isinstance(msg, dict) else None
            if stop_reason == "end_turn":
                # Extract the final assistant text so subscribers (notably
                # NotificationBus) can populate Session.last_assistant_msg
                # without a second JSONL scan and without depending on the
                # flaky Stop-hook path in api/hooks.py (which was populating
                # only ~25% of sessions in practice — feedback 5de334d5).
                # Cap at 2000 chars to bound event payload size.
                text_parts: list[str] = []
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text", "")
                            if isinstance(t, str) and t:
                                text_parts.append(t)
                assistant_text = ("".join(text_parts))[:2000] if text_parts else None
                await self.emit(Event(
                    type=EventType.MESSAGE_ASSISTANT_DONE,
                    ts=ts,
                    session_id=r.claude_session_id,
                    project_path=r.project_path,
                    payload={"model": msg.get("model"), "assistant_text": assistant_text},
                    source_offset=r.byte_offset,
                ))
            return

    # ---- watchdog loop ----
    async def _watchdog_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._watchdog_tick()
            except Exception:
                log.exception("event_stream watchdog tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._watchdog_interval)
            except TimeoutError:
                continue
            else:
                break

    def _collect_watchdog_states(self) -> list[dict[str, Any]]:
        """Uniform per-adapter view for `_watchdog_tick`.

        Multi-agent v2: iterates every enabled adapter's `tail_states()`
        so codex (and future adapters) get idle detection too — previously
        the watchdog only walked `self._tailer.file_states()` (claude
        only). Falls back to the legacy claude-only path when no
        adapter_registry is wired (test compat).
        """
        out: list[dict[str, Any]] = []
        if self._registry is not None:
            for adapter in self._registry.enabled():
                try:
                    out.extend(adapter.tail_states())
                except Exception:
                    log.exception(
                        "adapter %s tail_states raised — skipping this tick",
                        adapter.name,
                    )
            return out
        # Legacy path: claude-only, no adapter metadata.
        from csm.adapters.jsonl_tail import project_dir_to_cwd
        for path, state in self._tailer.file_states().items():
            p = Path(path)
            out.append({
                "path": path,
                "external_session_id": p.stem,
                "project_path": project_dir_to_cwd(p.parent.name),
                "mtime": state.mtime,
            })
        return out

    async def _watchdog_tick(self) -> None:
        now_ts = time.time()
        states = self._collect_watchdog_states()
        # Finding-6 observability: counts let us tell from csm.log whether
        # the watchdog is running at all, how many sessions it sees, and
        # why SESSION_IDLE wasn't emitted (ended-set skip vs age-below).
        n_total = len(states)
        n_ended_skip = 0
        n_already_idle = 0
        n_below_threshold = 0
        n_no_id = 0
        n_idle_emitted = 0
        for st in states:
            sid = st.get("external_session_id")
            if not sid:
                # Adapter saw the artifact but hasn't extracted its id
                # yet (codex before session_meta line is parsed). Skip
                # this tick — the tailer will backfill and we'll try
                # again next watchdog tick.
                n_no_id += 1
                continue
            if sid in self._ended_emitted:
                n_ended_skip += 1
                continue
            if sid in self._idle_emitted:
                n_already_idle += 1
                continue
            mtime = st.get("mtime") or 0
            age = now_ts - mtime if mtime else 0
            if age <= self._idle_threshold:
                n_below_threshold += 1
                continue
            if self._watchdog_first_tick and self._seed_stale_history_as_idle:
                # Already stale on the very first tick after boot → prime-only,
                # no emit (previous life, not a fresh idle crossing). Subsumes
                # the under-covering _seed_idle_emitted_from_history. Gated on
                # the same flag as that seeding so tests that opt out
                # (_seed_stale_history_as_idle=False) still get a real emit.
                self._idle_emitted.add(sid)
                n_already_idle += 1
                continue
            path = st.get("path")
            project_path = st.get("project_path")
            log.info(
                "event_stream watchdog emitting SESSION_IDLE sid=%s age=%ss threshold=%ss",
                sid,
                int(age),
                self._idle_threshold,
            )
            await self.emit(Event(
                type=EventType.SESSION_IDLE,
                ts=datetime.now(UTC),
                session_id=sid,
                project_path=project_path,
                payload={"idle_seconds": int(age), "artifact_path": path},
            ))
            self._idle_emitted.add(sid)
            n_idle_emitted += 1
            # We do not yet auto-emit session.ended/crashed from the watchdog —
            # Session Manager will own that signal because it knows the PID.
        log.info(
            "event_stream watchdog tick: total=%d ended_skip=%d already_idle=%d "
            "below_threshold=%d no_id=%d idle_emitted=%d subs=%d",
            n_total,
            n_ended_skip,
            n_already_idle,
            n_below_threshold,
            n_no_id,
            n_idle_emitted,
            len(self._subs),
        )
        # After the first tick, resume normal emit-on-cross behaviour.
        self._watchdog_first_tick = False

    # ---- introspection ----
    def session_meta_snapshot(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._session_meta.items()}
