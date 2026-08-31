"""Session Manager — owns PTY-backed Claude subprocesses and their lifecycle.

Per-session state held in memory; Session row in SQLite is the durable record.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.adapters import claude_subprocess
from csm.adapters.claude_subprocess import PtyHandle
from csm.config import settings as _settings
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Session
from csm.models.session import SessionStatus, SessionType
from csm.modules.session_manager.ring_buffer import RingBuffer
from csm.utils.time import now_utc_naive


class ClaudeSessionIdConflict(Exception):
    """Raised when two live sessions try to claim the same external_session_id.

    Enforced by the partial unique index `ux_session_external_sid_active`.
    API layer maps this to HTTP 409 Conflict.
    """

    def __init__(self, external_session_id: str) -> None:
        super().__init__(external_session_id)
        self.external_session_id = external_session_id

if TYPE_CHECKING:
    from fastapi import WebSocket

    from csm.backends.registry import AdapterRegistry

log = logging.getLogger(__name__)


@dataclass
class _WsChannel:
    """Per-websocket fan-out buffer.

    The reader loop appends bytes to `buf` and sets `event`. A dedicated
    `sender_task` awaits the event, drains `buf` into a single frame, and
    writes it to the socket. This decouples PTY read + append (O(1), never
    awaits) from the slow ws.send_bytes so keystroke handling (which runs
    in the same event loop as the reader) doesn't queue behind fan-out.

    `MAX_BUF` caps the growth; if a socket falls that far behind we mark it
    closed and let the sender break the loop — dropping the ws is safer than
    unbounded memory growth. A reconnect will replay from the ring snapshot.
    """
    buf: bytearray = field(default_factory=bytearray)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    sender_task: asyncio.Task | None = None
    # Serialises all writes to this websocket. The sender loop emits binary
    # PTY bursts; the receive loop emits text pong frames for the heartbeat
    # (A1). Starlette does not lock sends itself, so two coroutines calling
    # ws.send_* concurrently could interleave frames — this lock keeps each
    # frame atomic relative to the others.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _LiveSession:
    """In-memory state for a running session."""
    id: str
    pty: PtyHandle
    ring: RingBuffer
    channels: dict[Any, _WsChannel] = field(default_factory=dict)
    reader_task: asyncio.Task | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # True after the reader has been unregistered from the asyncio selector.
    # Used to make the unregister call idempotent so both the reader-loop
    # `finally` block and the `_terminate` path can call it — whichever fires
    # first wins, the other is a no-op. Critical: unregistering MUST happen
    # before `pty.close()` releases the fd number, otherwise the kernel can
    # reuse it for the next spawn and we'd tear down that session's monitor.
    reader_unregistered: bool = False
    # Wall-clock of the last PTY read. Reader loop assigns this in-memory
    # (no await, no DB) on every burst; the SessionManager's activity flush
    # task periodically batch-writes it to Session.last_activity_ts. Doing
    # the SQLite commit inline on the reader stalled keystroke echo by
    # 20-100ms under write-lock contention (once per second per session),
    # which the user perceived as intermittent typing lag.
    last_activity_at: datetime | None = None
    # Set to True after last_activity_at has been flushed at least once —
    # lets the flush loop skip unchanged sessions cheaply.
    activity_dirty: bool = False
    # Reader-loop wakeup event. Assigned by `_reader_loop` on entry; the
    # selector callback sets it on PTY readability. `_terminate` also sets
    # it so a killed session doesn't have to wait out the reader's 1s poll
    # before it notices the fd is gone and finalize can run. Without this
    # kick, once we removed the inline DB commit from the reader hot path
    # (which was an implicit yield / recheck), termination-to-finalize
    # latency ballooned to the full 1s poll boundary — visible in tests as
    # sessions still marked RUNNING 300ms after `stop_session` returned.
    data_ready: asyncio.Event | None = None


class SessionManager:
    """The one place that spawns / supervises / talks to Claude subprocesses."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        adapter_registry: AdapterRegistry,
        ring_buffer_bytes: int = 1024 * 1024,
        stop_grace_sec: int = 5,
        claude_argv: list[str] | None = None,
        hooks_base_url: str | None = None,
        proxy_env: dict[str, str] | None = None,
    ):
        self._sm = sessionmaker
        self._es = event_stream
        self._ring_capacity = ring_buffer_bytes
        self._grace = stop_grace_sec
        # `claude_argv` is exposed so tests can substitute (`['bash']` etc).
        self._claude_argv = claude_argv or ["claude"]
        # Proxy vars sniffed from the user's login shell (and/or
        # ~/.csm/proxy.env) at CSM boot. Layered into every session's spawn
        # env below `os.environ` and any explicit per-request env override,
        # so spawned claude/codex reach api.anthropic.com through the same
        # proxy the user configured in ~/.zshrc even when CSM was started
        # under a non-interactive bash. `set_proxy_env` lets the settings
        # API refresh this without a full restart.
        self._proxy_env: dict[str, str] = dict(proxy_env) if proxy_env else {}
        # H3: backend URL prefix the spawned claude will POST hook events to.
        # E.g. "http://127.0.0.1:8001" → settings JSON will use
        # "http://127.0.0.1:8001/api/hooks/<sid>" for every hook event.
        # If None, hook injection is skipped (claude runs without --settings).
        self._hooks_base_url = hooks_base_url
        # Multi-agent v2: `create_session` dispatches argv + session-id
        # lifecycle through the resolved adapter (`pre_spawn_session_id`,
        # `build_argv`, `post_spawn_bind`).
        #
        # Required, not optional. It was `=None` with a parallel legacy
        # claude/codex if-else as fallback, which production never took —
        # only tests that omitted the registry did, so ~270 lines of
        # argv-building and a second inline copy of `_commit_and_spawn`
        # stayed alive purely to serve them, and every reader of
        # `create_session` had to hold two mutually exclusive paths in
        # their head. Tests now build a registry like production does.
        self._registry = adapter_registry
        self._live: dict[str, _LiveSession] = {}
        self._lock = asyncio.Lock()
        self._stopping = False
        # Sessions the user asked us to stop (via stop_session / kill_session /
        # purge). We treat their non-zero exit_code as ENDED, not CRASHED, so
        # the UI doesn't fire a "Session crashed" notification for a manual
        # kill. Populated by `_terminate`, consumed + cleared in `_finalize`.
        self._user_stopped: set[str] = set()
        # Background task that periodically flushes each live session's
        # in-memory `last_activity_at` to `Session.last_activity_ts`. Started
        # in `start()`, cancelled in `shutdown()`. See _LiveSession comment.
        self._activity_flush_task: asyncio.Task | None = None
        # Companion loop that re-runs the orphan-pid check periodically.
        # `startup_reap_orphans` fires only on boot; without a periodic
        # partner an ORPHANED row whose pid dies mid-uptime stays
        # ORPHANED in the UI forever (2026-07-26 incident: user saw a
        # dead-pid row still showing the "CSM lost the handle" banner).
        self._orphan_reap_task: asyncio.Task | None = None
        # Per-sid locks that serialise concurrent /resume calls so double-clicks
        # (or two tabs hitting the same button) can't spawn two PTYs racing on
        # the same JSONL. Entries are popped in the resume handler's `finally`
        # so this dict does not grow unbounded on a long-lived process.
        self._resume_locks: dict[str, asyncio.Lock] = {}

    # ---- proxy env ----
    @property
    def proxy_env(self) -> dict[str, str]:
        """Snapshot of currently-cached proxy vars merged into spawn env."""
        return dict(self._proxy_env)

    def set_proxy_env(self, env: dict[str, str]) -> None:
        """Replace cached proxy env. New sessions spawned after this call
        will see the updated values; already-running children are unaffected
        (they inherited a frozen copy at fork time)."""
        self._proxy_env = dict(env)

    def _apply_proxy_env(self, spawn_env: dict[str, str]) -> None:
        """Layer cached proxy vars under whatever's already in spawn_env.

        Order of precedence at spawn time:
            explicit per-request env > uvicorn's os.environ > sniffed proxy
        `setdefault` gives us that with a single line — os.environ values
        (whether they were populated by a proxied startup shell or by an
        explicit `env` param) always beat the sniffed fallback."""
        for k, v in self._proxy_env.items():
            spawn_env.setdefault(k, v)

    # ---- lifecycle ----
    async def startup_reap_orphans(self) -> int:
        """On boot, mark previously-running sessions as orphaned/crashed.

        The status filter must include every state that means "the manager
        was actively tracking this session before we died" — otherwise a
        restart that catches a session mid-`waiting_input` / `waiting_auth`
        (both very common — that's the state claude sits in between
        assistant turns) leaves the row untouched, and the frontend keeps
        showing it in the Active tab even though `self._live` is empty on
        the fresh process. ORPHANED is also included so a re-check on each
        boot can downgrade to CRASHED once the reparented pid is finally
        gone, instead of it lingering as orphaned forever.
        """
        marked = 0
        async with self._sm() as db:
            res = await db.execute(
                select(Session).where(Session.status.in_(
                    [
                        SessionStatus.RUNNING,
                        SessionStatus.STARTING,
                        SessionStatus.IDLE,
                        SessionStatus.WAITING_INPUT,
                        SessionStatus.WAITING_AUTH,
                        SessionStatus.ORPHANED,
                    ]
                ))
            )
            for row in res.scalars().all():
                if row.pid is not None and _pid_alive(row.pid):
                    row.status = SessionStatus.ORPHANED
                else:
                    row.status = SessionStatus.CRASHED
                    row.ended_at = now_utc_naive()
                marked += 1
            await db.commit()
        return marked

    async def start(self) -> None:
        """Start background maintenance tasks.

        Currently: activity flush + orphan reap loops. Called from the
        FastAPI lifespan right after `startup_reap_orphans`. Idempotent —
        callable multiple times without spawning duplicate tasks.
        """
        if self._activity_flush_task is None or self._activity_flush_task.done():
            self._activity_flush_task = asyncio.create_task(
                self._activity_flush_loop()
            )
        if _settings.orphan_reap_interval_sec > 0 and (
            self._orphan_reap_task is None or self._orphan_reap_task.done()
        ):
            self._orphan_reap_task = asyncio.create_task(self._orphan_reap_loop())

    async def shutdown(self) -> None:
        """Stop accepting new sessions and best-effort SIGKILL every live PTY.

        Safe to call multiple times. Kicks off `kill_session` for every live
        PTY concurrently and gathers with a 3s cap so lifespan teardown does
        not race with in-flight finalizer DB writes (which would otherwise
        hit the disposed SQLite engine).
        """
        self._stopping = True
        # Cancel the activity flush task BEFORE tearing down sessions so any
        # in-flight SQLite commit finishes / is cancelled before lifespan's
        # DB engine dispose runs.
        if self._activity_flush_task is not None:
            self._activity_flush_task.cancel()
            try:
                await self._activity_flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._activity_flush_task = None
        if self._orphan_reap_task is not None:
            self._orphan_reap_task.cancel()
            try:
                await self._orphan_reap_task
            except (asyncio.CancelledError, Exception):
                pass
            self._orphan_reap_task = None
        finalizers: list[asyncio.Task] = []
        for sid in list(self._live.keys()):
            try:
                finalizers.append(asyncio.create_task(self.kill_session(sid)))
            except Exception:
                log.exception("kill during shutdown failed sid=%s", sid)
        if finalizers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*finalizers, return_exceptions=True),
                    timeout=3.0,
                )
            except TimeoutError:
                log.warning(
                    "session shutdown gather timed out; %d finalizers still pending",
                    len(finalizers),
                )

    # Kept as a class-level attribute (property-style) for back-compat with
    # tests / callers reading `SessionManager._ACTIVITY_FLUSH_INTERVAL_SEC`.
    # Source of truth is `settings.activity_flush_interval_sec` (env-overridable
    # via `CSM_ACTIVITY_FLUSH_INTERVAL_SEC`).
    @property
    def _ACTIVITY_FLUSH_INTERVAL_SEC(self) -> float:  # noqa: N802
        return _settings.activity_flush_interval_sec

    async def _activity_flush_loop(self) -> None:
        """Periodically batch-flush live sessions' last_activity_at to DB.

        Reader loops do NOT write to DB on the hot path (see the change
        history in _reader_loop). This coroutine picks up their in-memory
        stamps every ~15s and writes them all in one transaction — one
        SQLite commit per interval instead of one per session per second,
        and never in a position to stall keystroke echo because it runs
        as its own task.
        """
        try:
            while not self._stopping:
                await asyncio.sleep(self._ACTIVITY_FLUSH_INTERVAL_SEC)
                if self._stopping:
                    break
                # Snapshot the (sid, ts) pairs we intend to flush so a
                # concurrent finalize or new-read doesn't mutate the map
                # while we're iterating.
                pending: list[tuple[str, datetime]] = []
                async with self._lock:
                    for sid, live in self._live.items():
                        if live.activity_dirty and live.last_activity_at is not None:
                            pending.append((sid, live.last_activity_at))
                            live.activity_dirty = False
                if not pending:
                    continue
                try:
                    async with self._sm() as db:
                        for sid, ts in pending:
                            row = await db.get(Session, sid)
                            if row is not None:
                                row.last_activity_ts = ts
                        await db.commit()
                    # Checkpoint the replay tail alongside the activity stamp.
                    # This makes browser/backend reconnects and abrupt process
                    # exits recoverable without putting file IO on the PTY
                    # reader's latency-sensitive hot path.
                    snapshots: list[tuple[str, bytes]] = []
                    async with self._lock:
                        for sid, _ts in pending:
                            live = self._live.get(sid)
                            if live is not None:
                                snapshots.append((sid, live.ring.snapshot()))
                    if snapshots:
                        await asyncio.gather(*(
                            asyncio.to_thread(self._persist_output_snapshot, sid, data)
                            for sid, data in snapshots
                        ))
                except Exception:
                    log.exception("activity_flush_loop batch commit failed")
                    # Restore dirty flag so next tick retries — don't lose
                    # activity signal because of a transient DB hiccup.
                    async with self._lock:
                        for sid, _ts in pending:
                            live = self._live.get(sid)
                            if live is not None:
                                live.activity_dirty = True
        except asyncio.CancelledError:
            return

    async def _orphan_reap_tick(self) -> int:
        """One pass of the orphan reaper: downgrade every ORPHANED row
        whose pid is no longer alive to CRASHED + set ended_at. Returns
        the number of rows reaped. Extracted from `_orphan_reap_loop`
        so tests can exercise a single tick without sleeping through
        the 30s interval.
        """
        reaped = 0
        async with self._sm() as db:
            res = await db.execute(
                select(Session).where(Session.status == SessionStatus.ORPHANED)
            )
            for row in res.scalars().all():
                if row.id in self._live:
                    continue
                if row.pid is None or not _pid_alive(row.pid):
                    row.status = SessionStatus.CRASHED
                    row.ended_at = now_utc_naive()
                    reaped += 1
            if reaped:
                await db.commit()
        return reaped

    async def _orphan_reap_loop(self) -> None:
        """Periodically re-run the orphan-pid check.

        `startup_reap_orphans` runs only at boot. Without this partner
        loop, a session that got marked ORPHANED by the boot scan (and
        therefore whose pid was alive at that instant) stays ORPHANED in
        the UI even after the process later exits — the "CSM lost the
        handle" banner never clears. Users then think the row is stuck
        and either wait forever or purge it unnecessarily.

        Runs at `settings.orphan_reap_interval_sec` cadence (default 30s,
        0 disables). Cheap: one indexed SELECT + at most a handful of
        `os.kill(pid, 0)` probes per tick. Sessions the manager is still
        actively tracking (`self._live`) are skipped — those cannot be
        stale by definition.
        """
        interval = _settings.orphan_reap_interval_sec
        if interval <= 0:
            return
        try:
            while not self._stopping:
                await asyncio.sleep(interval)
                if self._stopping:
                    break
                try:
                    reaped = await self._orphan_reap_tick()
                    if reaped:
                        log.info(
                            "orphan_reap: downgraded %d orphaned rows "
                            "whose pid is no longer alive",
                            reaped,
                        )
                except Exception:
                    log.exception("orphan_reap tick failed")
        except asyncio.CancelledError:
            return

    # ---- core ops ----
    async def create_session(
        self,
        cwd: str,
        type: SessionType = SessionType.INTERACTIVE,
        title: str | None = None,
        initial_prompt: str | None = None,
        run_id: str | None = None,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        extra_claude_args: list[str] | None = None,
        resume_from: str | None = None,
        agent: str = "claude",
    ) -> Session:
        """Spawn a Claude (or `argv`-overridden) PTY child and persist a Session row.

        Args:
            cwd: working directory for the subprocess; also the project context
                EventStream uses to derive `project_path` for emitted events.
            type: interactive / auto / *_agent — only `type` distinguishes the
                lifecycle policy (interactive sessions accept WS attach + show
                up in unread badges; auto sessions are owned by AutomationRunner).
            title: optional human label shown in the Sessions list.
            initial_prompt: if non-empty, write `prompt + CRLF` into the PTY 3s
                after spawn. Used by AutomationRunner; ignored when None.
            run_id: when set, the Run row in the Automation module links back
                via this id (`Session.associated_run_id`).
            argv: process command line; defaults to the constructor's
                `claude_argv` (`["claude"]` in production, `["bash","-i"]` in tests).
            env: environment dict; inherits current process env when None.

        Returns:
            The persisted `Session` ORM row, with `status=RUNNING` and `pid`
            populated. The reader task is already running and the PTY is in
            `self._live[sid]`.

        Raises:
            Whatever `ptyprocess.PtyProcess.spawn` raises (PermissionError,
            FileNotFoundError, OSError). DB-insert failures cleanly tear down
            the just-spawned PTY before re-raising.
        """
        sid = str(uuid.uuid4())

        # LaTeX / global preamble follow-up: if the user set a
        # `default_session_prompt` (enabled + non-empty) in their
        # preferences AND the caller didn't already provide an
        # initial_prompt AND this is an INTERACTIVE session, fill in the
        # default so `_deliver_initial_prompt` writes it 3s after spawn.
        # AUTO / CHAT_AGENT are skipped deliberately: workflow-defined
        # or agent-definition-provided prompts must not be poisoned by
        # a global default. An explicit caller-provided initial_prompt
        # also wins over the default (respects "explicit beats implicit").
        if (
            initial_prompt is None
            and type == SessionType.INTERACTIVE
        ):
            try:
                from csm.models import UserPreference
                async with self._sm() as _pref_db:
                    _pref = await _pref_db.get(UserPreference, 1)
                    if (
                        _pref is not None
                        and _pref.default_session_prompt_enabled
                        and _pref.default_session_prompt
                        and _pref.default_session_prompt.strip()
                    ):
                        initial_prompt = _pref.default_session_prompt
                        # Optional supplementary note — appended only when its
                        # own toggle is on and the text is non-empty. Splicing
                        # here (not in `_deliver_initial_prompt`) keeps the note
                        # scoped to the global-default path; automation /
                        # agent-deck / caller-provided prompts never see it.
                        if (
                            _pref.default_session_prompt_note_enabled
                            and _pref.default_session_prompt_note
                            and _pref.default_session_prompt_note.strip()
                        ):
                            initial_prompt = (
                                f"{initial_prompt}\n\n"
                                f"{_pref.default_session_prompt_note.strip()}"
                            )
            except Exception:
                # Never let a pref-read failure block a session spawn — log
                # and fall through to the no-prompt path.
                log.exception("default_session_prompt lookup failed sid=%s", sid)

        # Default argv depends on adapter. Claude keeps the constructor-level
        # override (`self._claude_argv`, driven by `settings.claude_argv` for
        # test/dev bash substitution). Codex uses a fresh `["codex"]` unless
        # the caller passed an explicit argv (again: tests may substitute
        # `["bash", "-i"]`).
        if argv:
            argv = list(argv)
        elif agent == "codex":
            argv = ["codex"]
        else:
            argv = list(self._claude_argv)
        # Caller-supplied extras (e.g. AgentConversationManager passing
        # --disallowedTools Skill for skill-disabled agents) are appended ONLY
        # when the underlying binary is claude; test/bash mode silently drops
        # them so unit tests keep working.
        if extra_claude_args and argv and argv[0] == "claude":
            argv = argv + list(extra_claude_args)

        # Multi-agent v2 dispatch: argv / session-id / hooks work is delegated
        # to the CLIAdapter resolved for `agent`. An unknown agent raises out of
        # `registry.get` verbatim, so the API layer's 400 handler sees the real
        # message.
        adapter = self._registry.get(agent)
        # Starts as None, NOT `resume_from`: `argv_result.session_id` below is
        # the single source of truth, and every adapter reports the id it
        # actually used (claude mirrors `resume_from`, codex likewise). Seeding
        # it here left a value behind on the one path where `build_argv` is
        # strict pass-through — substituted argv (`["bash", "-i"]`) plus a
        # `resume_from` — so the new row claimed the id of the session it did
        # not in fact resume, and `ux_session_external_sid_active` answered 409.
        external_sid: str | None = None
        hooks_url = None
        if self._hooks_base_url:
            hooks_url = f"{self._hooks_base_url.rstrip('/')}/api/hooks/{sid}"
        # Session-id generation: per-adapter (claude returns uuid, codex
        # returns None). Adapters that don't declare the PRE_SPAWN_SESSION_ID
        # capability MUST return None here.
        pre_id = adapter.pre_spawn_session_id(cwd)

        # Only HOOKS-capable adapters get the Claude-specific callback URL;
        # every adapter gets the protocol-level `resume_from`.
        build_kwargs: dict[str, Any] = {
            "session_id": pre_id,
            "initial_prompt": initial_prompt,
            "extra_args": (
                list(extra_claude_args) if extra_claude_args else None
            ),
            "resume_from": resume_from,
        }
        from csm.backends.base import Capability
        if Capability.HOOKS in adapter.capabilities:
            build_kwargs["hooks_base_url"] = hooks_url
        # BEFORE build_argv consumes it: `argv` still carries the user's
        # opt-out marker at this point.
        self._pretrust_workspace(adapter, argv, cwd)
        argv_result = adapter.build_argv(
            base_argv=argv,
            cwd=cwd,
            **build_kwargs,
        )
        argv = argv_result.argv
        # Adopt the id the adapter actually put on the command line — never
        # `pre_id` directly. Test/dev argv (`["bash", "-i"]`) is strict
        # pass-through, so claiming `pre_id` there would stamp the row with a
        # claude session id no transcript will ever carry. Codex returns None
        # and `post_spawn_bind` writes the discovered id to `rollout_path`.
        if argv_result.session_id:
            external_sid = argv_result.session_id
        prompt_via_argv = argv_result.prompt_appended
        return await self._commit_and_spawn(
            sid=sid,
            cwd=cwd,
            type=type,
            title=title,
            initial_prompt=initial_prompt,
            run_id=run_id,
            env=env,
            agent=agent,
            argv=argv,
            external_sid=external_sid,
            prompt_via_argv=prompt_via_argv,
            adapter=adapter,
        )

    async def _commit_and_spawn(
        self,
        *,
        sid: str,
        cwd: str,
        type: SessionType,
        title: str | None,
        initial_prompt: str | None,
        run_id: str | None,
        env: dict[str, str] | None,
        agent: str,
        argv: list[str],
        external_sid: str | None,
        prompt_via_argv: bool,
        adapter=None,
    ) -> Session:
        """Commit the session row BEFORE spawning, then spawn.

        The order is the fix for the "zombie running" regression. It used to be
        spawn → register → commit; a claude subprocess that died within a few
        hundred ms (bad --resume sid, missing JSONL, argv typo, permission
        fail) fired its SessionStart / SessionEnd hooks before the row was
        durable. Those hooks hit `db.get(Session, sid) → None`, were dropped as
        "unknown session", and CSM never learned the child had died — the row
        stayed pinned at `running` forever, blocking every future Resume on it.

        Each step below is its own method so that order is enforced by the code
        rather than by a reader remembering a comment.

        `adapter` is optional; when it declares POST_SPAWN_BIND, a background
        task resolves the per-session artifact into `Session.rollout_path`.
        """
        spawn_env = self._build_spawn_env(env)
        await self._insert_placeholder_row(
            sid=sid, cwd=cwd, type=type, title=title, run_id=run_id,
            external_sid=external_sid, agent=agent,
        )
        await self._snapshot_adapter_artifacts(adapter, sid)
        pty = await self._spawn_or_drop_placeholder(sid, argv, cwd, spawn_env, adapter)
        await self._register_live_session(sid, pty)
        await self._stamp_pid_and_promote(sid, pty)
        self._schedule_post_spawn_bind(sid, cwd, adapter)

        # Initial prompt for non-claude argv (bash etc.) — for real claude
        # / codex the adapter's build_argv already appended it.
        if initial_prompt and not prompt_via_argv:
            asyncio.create_task(self._deliver_initial_prompt(sid, initial_prompt))

        return await self.get_session(sid)  # type: ignore[return-value]

    def _build_spawn_env(self, env: dict[str, str] | None) -> dict[str, str]:
        """Environment for the child process.

        Two non-obvious adjustments, both required for CSM to see the session
        at all:

        H3.5 — hook callbacks POST to 127.0.0.1. Every real user config we've
        seen sets HTTP_PROXY for VPN / corp / Clash, and forwarding a loopback
        POST through it yields 502, so claude's hooks silently drop and the
        session ends up with miscounted unread + stale "Stop hook error"
        banners. NO_PROXY gets the loopback hosts appended; external traffic
        (api.anthropic.com etc.) still goes through the configured proxy.

        Transcript persistence — if CSM itself was launched from inside a
        Claude Code session, CLAUDE_CODE_CHILD_SESSION is inherited and the
        spawned claude treats itself as a nested child, skipping the JSONL
        write that EventStream tails for ALL session detection. Strip the
        marker and force persistence so this holds however CSM was started.
        (No-op for codex, which ignores both vars.)
        """
        spawn_env = dict(env) if env is not None else dict(os.environ)
        # Sniffed proxy vars layer UNDER what we already have:
        # os.environ / explicit env win, the sniff only fills gaps.
        self._apply_proxy_env(spawn_env)
        existing_no_proxy = spawn_env.get("NO_PROXY") or spawn_env.get("no_proxy") or ""
        loopback = "127.0.0.1,localhost,::1"
        merged = f"{existing_no_proxy},{loopback}" if existing_no_proxy else loopback
        spawn_env["NO_PROXY"] = merged
        spawn_env["no_proxy"] = merged  # some HTTP clients only honour lowercase
        spawn_env.pop("CLAUDE_CODE_CHILD_SESSION", None)
        spawn_env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
        return spawn_env

    async def _insert_placeholder_row(
        self,
        *,
        sid: str,
        cwd: str,
        type: SessionType,
        title: str | None,
        run_id: str | None,
        external_sid: str | None,
        agent: str,
    ) -> None:
        """Step 1 — commit a STARTING row with no pid, before any subprocess.

        Any hook arriving in the race window that follows finds this row via
        `db.get`. `ClaudeSessionIdConflict` surfaces here, cheaply, while
        nothing external has been created yet.
        """
        # A caller-supplied title is user intent — same claim semantics as a UI
        # rename via PATCH — so later claude ai-title emissions don't overwrite
        # it. PATCH title="" releases the field back to external sync.
        title_manual = bool(title and title.strip())
        try:
            async with self._sm() as db:
                db.add(Session(
                    id=sid,
                    title=title,
                    title_manual=title_manual,
                    type=type,
                    cwd=cwd,
                    status=SessionStatus.STARTING,
                    pid=None,
                    associated_run_id=run_id,
                    external_session_id=external_sid,
                    agent=agent,
                ))
                try:
                    await db.commit()
                except IntegrityError as e:
                    await db.rollback()
                    raise ClaudeSessionIdConflict(external_sid or "") from e
        except ClaudeSessionIdConflict:
            raise
        except Exception:
            log.exception("create_session placeholder insert failed sid=%s", sid)
            raise

    async def _snapshot_adapter_artifacts(self, adapter, sid: str) -> None:
        """Baseline the adapter's artifact directory immediately before spawn.

        Must happen here and not inside the later background bind task: codex
        creates its rollout file so quickly that a baseline taken afterwards
        already contains it, and the new file gets misclassified as pre-existing.
        """
        if adapter is None:
            return
        prepare_bind = getattr(adapter, "prepare_post_spawn_bind", None)
        if not callable(prepare_bind):
            return
        try:
            await asyncio.to_thread(prepare_bind, sid)
        except Exception:
            log.exception("prepare_post_spawn_bind failed sid=%s", sid)

    async def _spawn_or_drop_placeholder(
        self, sid: str, argv: list[str], cwd: str, spawn_env: dict[str, str], adapter,
    ) -> PtyHandle:
        """Step 2 — spawn; on failure delete the placeholder and re-raise.

        Deleting is safe precisely because of the commit-first order: the row
        is the only thing that ever referenced this sid, and no external state
        has accumulated yet.
        """
        try:
            return await asyncio.to_thread(claude_subprocess.spawn, argv, cwd, spawn_env)
        except Exception:
            cancel_bind = getattr(adapter, "cancel_post_spawn_bind", None) if adapter else None
            if callable(cancel_bind):
                cancel_bind(sid)
            log.exception("create_session spawn failed sid=%s; removing placeholder", sid)
            try:
                async with self._sm() as db:
                    stale = await db.get(Session, sid)
                    if stale is not None:
                        await db.delete(stale)
                        await db.commit()
            except Exception:
                log.exception(
                    "failed to remove placeholder row sid=%s after spawn failure", sid,
                )
            raise

    async def _register_live_session(self, sid: str, pty: PtyHandle) -> None:
        """Step 3 — publish the in-memory session and start its reader loop."""
        live = _LiveSession(id=sid, pty=pty, ring=RingBuffer(self._ring_capacity))
        async with self._lock:
            self._live[sid] = live
        live.reader_task = asyncio.create_task(
            self._reader_loop(live), name=f"csm-session-reader-{sid}",
        )

    async def _stamp_pid_and_promote(self, sid: str, pty: PtyHandle) -> None:
        """Step 4 — record the pid and promote STARTING → IDLE.

        Only STARTING is promoted. A hook or the reader may already have moved
        the row on (RUNNING, EXITED, CRASHED, WAITING_*); whoever got there
        first is the source of truth and must not be clobbered.

        IDLE rather than RUNNING is deliberate: the process is alive but the
        agent has not started working yet.

        Failure is logged, not raised — the row stays STARTING and the reader /
        hooks will correct it.
        """
        try:
            async with self._sm() as db:
                row = await db.get(Session, sid)
                if row is not None:
                    row.pid = pty.pid
                    if row.status == SessionStatus.STARTING:
                        row.status = SessionStatus.IDLE
                    await db.commit()
                    await db.refresh(row)
        except Exception:
            log.exception(
                "create_session step 4 (pid+status stamp) failed sid=%s pid=%s — "
                "row remains STARTING; reader/hooks will retry",
                sid, pty.pid,
            )

    def _schedule_post_spawn_bind(self, sid: str, cwd: str, adapter) -> None:
        """Fire-and-forget discovery of the per-session artifact.

        Only for adapters declaring POST_SPAWN_BIND — claude's is a no-op
        because it baked the id in pre-spawn.
        """
        if adapter is None:
            return
        try:
            from csm.backends.base import Capability as _Cap
            if _Cap.POST_SPAWN_BIND in adapter.capabilities:
                asyncio.create_task(
                    self._post_spawn_bind(sid, cwd, adapter),
                    name=f"csm-post-spawn-bind-{sid}",
                )
        except Exception:
            log.exception("post_spawn_bind scheduling failed sid=%s", sid)

    async def _post_spawn_bind(self, sid: str, cwd: str, adapter) -> None:
        """Run adapter.post_spawn_bind, persist the discovered id + artifact path.

        Fixed 2026-07-26 (M10.A2). Previous version was a `pass` stub —
        codex Session rows landed with both `external_session_id` and
        `rollout_path` NULL, breaking every downstream lookup keyed off
        those columns (NotificationBus, TokenAggregator, WorkflowOrchestrator,
        AutomationRunner). Now writes:

          - `Session.external_session_id` ← result.external_session_id
          - `Session.rollout_path`         ← result.artifact_path

        Both writes are gated: if a concurrent tailer / hook already
        populated the field, we leave it alone (first-writer-wins).

        Silent timeout (adapter returns None) is acceptable — the tailer
        will backfill later on the next scan tick.
        """
        try:
            bound = await asyncio.to_thread(
                adapter.post_spawn_bind, sid, cwd
            )
        except Exception:
            log.exception("post_spawn_bind adapter call failed sid=%s", sid)
            return
        if bound is None:
            return
        # Extract the (optional) fields defensively — adapter API returns
        # PostSpawnBindResult but older/mock adapters might still return
        # a bare string; be tolerant.
        if isinstance(bound, str):
            external_id: str | None = bound
            artifact_path: str | None = None
        else:
            external_id = getattr(bound, "external_session_id", None)
            artifact_path = getattr(bound, "artifact_path", None)
        if not external_id and not artifact_path:
            return

        try:
            async with self._sm() as db:
                row = await db.get(Session, sid)
                if row is None:
                    return
                dirty = False
                if external_id and not row.external_session_id:
                    row.external_session_id = external_id
                    dirty = True
                if artifact_path and not row.rollout_path:
                    row.rollout_path = artifact_path
                    dirty = True
                if dirty:
                    try:
                        await db.commit()
                        log.info(
                            "post_spawn_bind sid=%s: external_id=%s rollout_path=%s",
                            sid, external_id, artifact_path,
                        )
                    except IntegrityError:
                        # Unique-index conflict — another Session already
                        # owns this external_session_id. Roll back and
                        # log; the tailer's fallback will handle it.
                        await db.rollback()
                        log.warning(
                            "post_spawn_bind sid=%s: external_id=%s conflicted with "
                            "existing live session; leaving row NULL",
                            sid, external_id,
                        )
                        return
                    # M10 fix for token-attribution race: codex's
                    # session_meta + first token_count often land in the
                    # tailer BEFORE post_spawn_bind's async task writes
                    # `Session.external_session_id`. The aggregator's
                    # `_lookup_attribution` then misses and writes
                    # `source="manual_external"` on those early rows.
                    # Now that we know the external_id, re-attribute any
                    # orphan raw_token_event rows for this session.
                    if external_id and row is not None:
                        try:
                            from csm.models import RawTokenEvent
                            src_value = row.type.value if hasattr(row.type, "value") else str(row.type)
                            await db.execute(
                                RawTokenEvent.__table__.update()
                                .where(
                                    RawTokenEvent.external_session_id == external_id,
                                    RawTokenEvent.source == "manual_external",
                                )
                                .values(
                                    source=src_value,
                                    csm_session_id=row.id,
                                    agent=row.agent,   # M12: also set agent scope
                                )
                            )
                            await db.commit()
                        except Exception:
                            log.exception(
                                "post_spawn_bind re-attribute failed sid=%s ext=%s",
                                sid, external_id,
                            )
        except Exception:
            log.exception("post_spawn_bind DB write failed sid=%s", sid)

    async def get_session(self, sid: str) -> Session | None:
        async with self._sm() as db:
            return await db.get(Session, sid)

    @staticmethod
    def _output_path(sid: str) -> Path:
        return _settings.resolved_session_output_dir / f"{sid}.ansi"

    @classmethod
    def _persist_output_snapshot(cls, sid: str, data: bytes) -> None:
        if not data:
            return
        root = _settings.resolved_session_output_dir
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        target = cls._output_path(sid)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    async def output_snapshot(self, sid: str) -> tuple[bytes, str]:
        """Return the latest PTY replay tail and its source.

        Live sessions read directly from memory. Ended sessions fall back to
        the periodically/finally persisted ANSI snapshot. Missing output is a
        valid empty payload, not an error.
        """
        async with self._lock:
            live = self._live.get(sid)
            if live is not None:
                return live.ring.snapshot(), "live"
        path = self._output_path(sid)
        try:
            return await asyncio.to_thread(path.read_bytes), "persisted"
        except OSError:
            return b"", "missing"

    async def list_sessions(
        self,
        status_in: Iterable[SessionStatus] | None = None,
        type_in: Iterable[SessionType] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Session]:
        async with self._sm() as db:
            stmt = (
                select(Session)
                .order_by(Session.started_at.desc(), Session.id.desc())
                .offset(max(0, offset))
                .limit(limit)
            )
            if status_in:
                stmt = stmt.where(Session.status.in_(list(status_in)))
            if type_in:
                stmt = stmt.where(Session.type.in_(list(type_in)))
            res = await db.execute(stmt)
            return list(res.scalars().all())

    async def count_sessions(
        self,
        status_in: Iterable[SessionStatus] | None = None,
        type_in: Iterable[SessionType] | None = None,
    ) -> int:
        async with self._sm() as db:
            stmt = select(func.count()).select_from(Session)
            if status_in:
                stmt = stmt.where(Session.status.in_(list(status_in)))
            if type_in:
                stmt = stmt.where(Session.type.in_(list(type_in)))
            return int((await db.execute(stmt)).scalar_one())

    async def stop_session(self, sid: str, graceful: bool = True) -> int | None:
        return await self._terminate(sid, graceful=graceful)

    async def kill_session(self, sid: str) -> int | None:
        return await self._terminate(sid, graceful=False)

    async def _terminate(self, sid: str, graceful: bool) -> int | None:
        # Mark this termination as user-initiated so _finalize (called from
        # the reader loop when it sees EOF) doesn't flag it as a crash.
        self._user_stopped.add(sid)
        async with self._lock:
            live = self._live.get(sid)
        if live is None:
            self._user_stopped.discard(sid)
            return None
        # P0 #1: unregister the fd from the selector BEFORE the pty close
        # releases it. If we do it after, kernel fd-reuse in a concurrent
        # spawn can hand the same fd number to the new session and our
        # (racing) `remove_reader` would silently tear down that session's
        # monitor. Idempotent — reader-loop finally also calls this.
        self._unregister_reader(live)
        exit_code = await asyncio.to_thread(
            live.pty.terminate, graceful, self._grace
        )
        # Kick the reader out of its `wait_for(data_ready.wait(), 1.0)` so
        # finalize can run within a few ms of terminate returning instead
        # of up to 1s later.
        if live.data_ready is not None:
            live.data_ready.set()
        # Reader loop will see EOF and finalize DB row.
        return exit_code

    def _unregister_reader(self, live: _LiveSession) -> None:
        """Idempotently detach the PTY fd from the running asyncio selector."""
        if live.reader_unregistered:
            return
        live.reader_unregistered = True
        try:
            fd = live.pty.fileno()
        except Exception:
            return
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except Exception:
            pass

    # ---- WebSocket attach ----
    async def attach_websocket(self, sid: str, ws: WebSocket) -> None:
        """Send the ring buffer to ws, then keep streaming new output + accept input."""
        async with self._lock:
            live = self._live.get(sid)
        if live is None:
            await ws.accept()
            try:
                await ws.send_json({"error": "session not live", "sid": sid})
            finally:
                await ws.close(code=4404)
            return

        await ws.accept()
        # Create channel BEFORE snapshot so any chunk written by the reader
        # between snapshot and broadcast lands in the channel buffer — never
        # lost. Sender task starts AFTER snapshot to avoid two coroutines
        # racing on `ws.send_bytes` (Starlette does not serialise frames
        # itself).
        channel = _WsChannel()
        live.channels[ws] = channel
        try:
            snap = live.ring.snapshot()
            if snap:
                await ws.send_bytes(snap)
            channel.sender_task = asyncio.create_task(
                self._ws_sender_loop(ws, channel)
            )
            # Accept both binary (PTY stdin) and text (control JSON: resize).
            # Switching from receive_bytes() to receive() so we can dispatch on
            # message type — clients send `{"type":"resize","cols":N,"rows":M}`
            # as text to tell the PTY about a new window size (TUI banners +
            # cursor positioning depend on this).
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if (data := msg.get("bytes")) is not None:
                    # Keystroke fast path: small writes on a non-blocking fd
                    # (spawn puts it in O_NONBLOCK) never EAGAIN in practice —
                    # skip the thread-pool hop to shave ~1ms of scheduling
                    # per keystroke. Anything that hits EAGAIN (large paste,
                    # bracketed-paste blob) falls back to the select-loop
                    # write via to_thread.
                    async with live.write_lock:
                        try:
                            fd = live.pty.fileno()
                            written = os.write(fd, data)
                        except (BlockingIOError, InterruptedError):
                            written = await asyncio.to_thread(live.pty.write, data)
                        except OSError:
                            written = 0
                        else:
                            if written < len(data):
                                # Kernel accepted a prefix — finish the tail
                                # through the blocking-safe path.
                                tail = data[written:]
                                more = await asyncio.to_thread(live.pty.write, tail)
                                written += more
                    if written < len(data):
                        log.warning(
                            "attach_websocket truncated write for %s: %d/%d",
                            sid, written, len(data),
                        )
                    # Ctrl-C in the terminal aborts the turn with no Stop hook —
                    # release the RUNNING status so the card doesn't stick on
                    # "agent working". No-op unless the byte is present.
                    self._note_interrupt_bytes(sid, data)
                elif (text := msg.get("text")) is not None:
                    try:
                        import json as _json
                        ctl = _json.loads(text)
                    except Exception:
                        continue
                    if ctl.get("type") == "resize":
                        cols = int(ctl.get("cols") or 80)
                        rows = int(ctl.get("rows") or 24)
                        try:
                            await asyncio.to_thread(live.pty.setwinsize, rows, cols)
                        except Exception:
                            pass
                    elif ctl.get("type") == "ping":
                        # Heartbeat (A1): the client sends {type:ping} while idle
                        # and closes the socket if no reply lands in time. Reply
                        # under the send lock so the pong can't interleave with a
                        # concurrent binary burst from the sender loop.
                        try:
                            async with channel.send_lock:
                                await ws.send_json({"type": "pong"})
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            ch = live.channels.pop(ws, None)
            if ch is not None:
                ch.closed = True
                ch.event.set()
                if ch.sender_task is not None and not ch.sender_task.done():
                    ch.sender_task.cancel()

    async def _ws_sender_loop(self, ws: WebSocket, ch: _WsChannel) -> None:
        """Drain `ch.buf` and push it to the socket as one frame per burst.

        This runs as its own task so a slow client can't slow down the PTY
        read path — the read path only appends bytes + sets an event, both
        O(1). Bursts naturally coalesce here because we grab the whole buf
        each iteration.
        """
        try:
            while not ch.closed:
                await ch.event.wait()
                if ch.closed:
                    return
                # Swap buffer atomically (no await between grab + clear).
                if not ch.buf:
                    ch.event.clear()
                    continue
                data = bytes(ch.buf)
                ch.buf.clear()
                ch.event.clear()
                try:
                    async with ch.send_lock:
                        await ws.send_bytes(data)
                except Exception:
                    ch.closed = True
                    return
        except asyncio.CancelledError:
            return

    # ---- input api (non-WS) ----
    def _pretrust_workspace(self, adapter: Any, argv: list[str], cwd: str) -> None:
        """Let the adapter make `cwd` usable before the CLI starts.

        Only codex implements this today: it refuses to run a turn in a
        directory absent from its trust table and opens a folder-trust modal
        instead — which the chat surfaces never render, so the session looks
        alive and silently eats the first message answering an invisible
        dialog.

        MUST be called from the registry dispatch path, not only the legacy
        per-agent branches: production returns early from the registry path,
        so a call placed in the legacy branch alone is dead code. That is
        exactly how the first version of this shipped and did nothing —
        `test_create_session_pretrusts_the_workspace` now pins the wiring.

        Duck-typed rather than a new Protocol member — one adapter needs it,
        and widening `CLIAdapter` would force three implementations of a
        no-op. Best effort: a failure here degrades to the trust prompt, and
        must never block session creation.
        """
        from csm.modules.session_manager.spawners import NO_TRUST_WORKSPACE_FLAG

        prepare = getattr(adapter, "ensure_workspace_trusted", None)
        if prepare is None or NO_TRUST_WORKSPACE_FLAG in argv:
            return
        try:
            prepare(cwd)
        except Exception:
            log.exception("workspace pre-trust failed cwd=%s", cwd)

    def frame_prose(self, agent: str | None, text: str) -> bytes:
        """PTY bytes for one typed message, framed the way `agent`'s CLI needs.

        Single source of truth for the two prose senders (the session message
        endpoint and AgentConversationManager). Both previously hard-coded
        `text + CRLF`, which codex's TUI silently discards — so a fix applied
        to only one of them would leave Agent Deck chats mysteriously dead
        while session chats worked.

        Falls back to CRLF whenever the adapter can't be reached (no registry
        wired in a test app, unknown agent). That fallback is the historical
        behaviour, so a lookup failure can only leave a session exactly as
        broken as it was before — never worse.
        """
        return b"".join(self.frame_prose_sequence(agent, text))

    def frame_prose_sequence(self, agent: str | None, text: str) -> list[bytes]:
        """`frame_prose`, but as the ordered writes the CLI actually needs.

        Claude splits a long message into a bracketed paste plus a separate CR
        so the TUI reads the submit on its own — see
        `ClaudeAdapter.frame_pty_input_sequence`. Everything else is one write.
        """
        fallback = [(text + "\r\n").encode("utf-8", errors="replace")]
        if self._registry is None or not agent:
            return fallback
        try:
            chunks = self._registry.get(agent).frame_pty_input_sequence(text)
        except Exception:
            log.exception("frame_pty_input failed for agent=%s; using CRLF", agent)
            return fallback
        if not isinstance(chunks, list) or not chunks:
            return fallback
        if not all(isinstance(c, bytes) for c in chunks):
            return fallback
        return chunks

    async def write_input(self, sid: str, data: bytes) -> int:
        async with self._lock:
            live = self._live.get(sid)
        if live is None:
            return 0
        async with live.write_lock:
            written = await asyncio.to_thread(live.pty.write, data)
        # PtyHandle.write already loops on BlockingIOError for up to ~5s, so
        # a short return means the fd hit EOF/EBADF or the write deadline —
        # not "buffer full, try again". Log so integrators noticing missing
        # bytes have a breadcrumb; the caller can still retry the tail.
        if written < len(data):
            log.warning(
                "write_input truncated for %s: %d/%d bytes written",
                sid, written, len(data),
            )
        # Covers the API interrupt endpoint (writes b"\x03") and any other
        # non-WS caller. The raw WS keystroke path writes via os.write and
        # calls _note_interrupt_bytes itself.
        self._note_interrupt_bytes(sid, data)
        return written

    # Gap between the chunks of one framed message. Two back-to-back os.write
    # calls on a PTY are routinely coalesced into a single read if the CLI
    # isn't scheduled in between — which would defeat the whole point of
    # splitting them. Long enough to land as separate reads, short enough that
    # nobody perceives it on a send.
    PROSE_CHUNK_GAP_SEC = 0.04

    async def write_input_sequence(
        self, sid: str, chunks: list[bytes],
    ) -> tuple[int, int]:
        """Write `chunks` in order under ONE hold of the write lock.

        Returns (bytes_written, bytes_total). Holding the lock across the whole
        sequence is the point: a raw keystroke from a desktop terminal landing
        between a bracketed paste and its Enter would be swallowed into the
        paste. Stops at the first short write — once a chunk is truncated the
        CLI's input state is already wrong, and writing the rest on top of it
        would turn a detectable failure into corrupted text.
        """
        total = sum(len(c) for c in chunks)
        async with self._lock:
            live = self._live.get(sid)
        if live is None:
            return 0, total
        written = 0
        async with live.write_lock:
            for i, chunk in enumerate(chunks):
                if i:
                    await asyncio.sleep(self.PROSE_CHUNK_GAP_SEC)
                n = await asyncio.to_thread(live.pty.write, chunk)
                written += max(0, n)
                if n < len(chunk):
                    log.warning(
                        "write_input_sequence truncated for %s: chunk %d/%d "
                        "wrote %d/%d bytes",
                        sid, i + 1, len(chunks), n, len(chunk),
                    )
                    break
        self._note_interrupt_bytes(sid, b"".join(chunks))
        return written, total

    # ---- interrupt handling ----
    def _note_interrupt_bytes(self, sid: str, data: bytes) -> None:
        """If `data` carries an ETX (Ctrl-C / SIGINT), schedule a status
        release. Cheap enough to call on the keystroke fast path — the actual
        DB work happens off the input coroutine in `_on_interrupt`."""
        if b"\x03" in data:
            asyncio.create_task(self._on_interrupt(sid))

    async def _on_interrupt(self, sid: str) -> None:
        """A user interrupted the current turn. The CLI aborts without a `Stop`
        hook / `end_turn`, so nothing else moves the session out of RUNNING —
        release it here. Only touches a RUNNING row (idempotent: repeated
        Ctrl-C, or Ctrl-C while already idle / waiting / terminal, is a no-op),
        then emits SESSION_INTERRUPTED so worktime closes the open interval and
        the frontend badge flips off."""
        external_sid: str | None = None
        try:
            async with self._sm() as db:
                row = await db.get(Session, sid)
                if row is None or row.status != SessionStatus.RUNNING:
                    return
                row.status = SessionStatus.IDLE
                row.current_tool = None
                external_sid = row.external_session_id
                await db.commit()
        except Exception:
            log.exception("_on_interrupt: DB update failed for %s", sid)
            return
        try:
            await self._es.emit(Event(
                type=EventType.SESSION_INTERRUPTED,
                ts=datetime.now(UTC),
                session_id=external_sid,
                project_path=None,
                payload={"csm_session_id": sid},
            ))
        except Exception:
            pass

    # ---- reader loop ----
    async def _reader_loop(self, live: _LiveSession) -> None:
        """Stream PTY output → ring buffer + attached WebSockets.

        Uses `loop.add_reader(fd, ...)` so the PTY fd is monitored *natively*
        by the asyncio selector (epoll on Linux) — no threads are consumed
        while the session is idle. Every idle session used to hold one worker
        in the default `asyncio.to_thread` pool via a blocking `pty.read`,
        which starved unrelated `to_thread` calls (notably PTY *writes* for
        user keystrokes) once idle sessions outnumbered pool workers. Direct
        `os.read` on the fd, driven by an asyncio.Event set by the selector,
        is O(1) memory per session and never contends with writes.
        """
        loop = asyncio.get_running_loop()
        try:
            fd = live.pty.fileno()
        except Exception:
            log.exception("reader_loop cannot obtain fd for %s", live.id)
            await self._finalize_session(live)
            return

        # Quick-exit guard: if the child died at exec (e.g. bad cwd), the
        # PTY may already be dead before we register — finalize instead
        # of looping forever waiting for a readable event that never fires.
        if not live.pty.is_alive():
            await self._finalize_session(live)
            return

        # Publish the event on `live` so `_terminate` can wake us out of
        # the 1s wait_for the moment the fd is unregistered — otherwise a
        # killed session sits in `wait_for` for up to a second before it
        # can notice `pty.is_alive() == False` and finalize.
        data_ready = asyncio.Event()
        live.data_ready = data_ready

        def _on_readable() -> None:
            data_ready.set()

        try:
            loop.add_reader(fd, _on_readable)
        except Exception:
            log.exception("reader_loop add_reader failed for %s", live.id)
            await self._finalize_session(live)
            return

        try:
            while not self._stopping:
                # Wait for the selector to say "readable", with a 1s wall so
                # we still notice a silently-dead child (e.g. SIGKILL by another
                # path) without depending on the selector to wake us.
                try:
                    await asyncio.wait_for(data_ready.wait(), timeout=1.0)
                except TimeoutError:
                    if not live.pty.is_alive():
                        break
                    continue
                data_ready.clear()

                # add_reader is level-triggered on epoll, so if the read below
                # doesn't drain everything the callback will fire again next
                # loop iteration. We drain up to `_read_coalesce_max` bytes into
                # a single chunk per wake so a burst of PTY output (claude
                # streaming) turns into one `ws.send_bytes` per burst instead
                # of one per 8KB. This reduces event-loop tick pressure and
                # keeps the input coroutine (ws.receive → pty.write) responsive
                # under heavy output — i.e. the user's typing shows up in the
                # terminal without seconds-long delays.
                _READ_CHUNK = 8192
                _READ_COALESCE_MAX = 65536
                try:
                    data = os.read(fd, _READ_CHUNK)
                except (BlockingIOError, InterruptedError):
                    # P1 #6: spurious wake or transient signal — sleep briefly
                    # so we don't spin on a hot fd if for some reason the
                    # selector is repeatedly firing without data. add_reader is
                    # level-triggered so it will re-fire the moment data lands.
                    await asyncio.sleep(0.02)
                    continue
                except OSError:
                    data = b""

                if not data:
                    if not live.pty.is_alive():
                        break
                    # Spurious wake (rare) — brief pause to avoid a busy loop.
                    await asyncio.sleep(0.02)
                    continue

                # Drain more data if the child produced a larger burst than
                # one 8KB read. Each subsequent `os.read` is non-blocking
                # (spawn set O_NONBLOCK); as soon as it raises BlockingIOError
                # we stop, or when we hit the coalesce cap so other sessions'
                # readers still get a fair turn.
                if len(data) == _READ_CHUNK:
                    chunks = [data]
                    total = len(data)
                    while total < _READ_COALESCE_MAX:
                        try:
                            more = os.read(fd, _READ_CHUNK)
                        except (BlockingIOError, InterruptedError):
                            break
                        except OSError:
                            more = b""
                        if not more:
                            break
                        chunks.append(more)
                        total += len(more)
                        if len(more) < _READ_CHUNK:
                            break
                    if len(chunks) > 1:
                        data = b"".join(chunks)

                try:
                    live.ring.write(data)
                    # Fan-out: append to each channel's buffer + wake sender.
                    # This is O(1) per channel and never awaits, so a slow
                    # client can't slow the read path. Sender tasks handle the
                    # actual ws.send_bytes concurrently.
                    #
                    # `_CHANNEL_BUF_CAP` (2 MiB) is the per-ws memory ceiling.
                    # If a client is that far behind, we mark the channel
                    # closed — the sender loop breaks and attach_websocket's
                    # finally cleans up. Reconnect will replay from the ring
                    # snapshot; dropping bytes mid-stream would scramble ANSI.
                    _CHANNEL_BUF_CAP = 2 * 1024 * 1024
                    for ch in live.channels.values():
                        if ch.closed:
                            continue
                        if len(ch.buf) + len(data) > _CHANNEL_BUF_CAP:
                            ch.closed = True
                            ch.event.set()
                            continue
                        ch.buf.extend(data)
                        ch.event.set()
                    # In-memory activity stamp only — the periodic flush
                    # task (`_activity_flush_loop`) batches SQLite writes
                    # off-path so a 20-100ms commit under write-lock
                    # contention can't stall keystroke echo.
                    live.last_activity_at = now_utc_naive()
                    live.activity_dirty = True
                except Exception:
                    log.exception("reader_loop fan-out failed for %s", live.id)
                    # Continue loop — do NOT silently mark session exited.
        finally:
            # Idempotent — no-op if `_terminate` already unregistered before
            # closing the fd.
            self._unregister_reader(live)
            await self._finalize_session(live)

    async def _finalize_session(self, live: _LiveSession) -> None:
        # If the PTY is still alive at this point, the reader is shutting down
        # because of an exception, not real EOF — terminate the child so we
        # don't leak it.
        if live.pty.is_alive():
            try:
                await asyncio.to_thread(live.pty.terminate, False, 1)
            except Exception:
                log.exception("finalize: pty.terminate failed for %s", live.id)
        exit_code = live.pty.exit_code
        crashed = exit_code is not None and exit_code != 0
        # User-initiated terminate (stop_session / kill_session / purge) exits
        # with a signal-negative code (SIGINT=-2, SIGTERM=-15, SIGKILL=-9);
        # `crashed=True` for these would mis-fire a SESSION_CRASHED
        # notification. Downgrade to ENDED so the bell stays quiet.
        if live.id in self._user_stopped:
            crashed = False
            self._user_stopped.discard(live.id)
        external_sid: str | None = None
        try:
            async with self._sm() as db:
                row = await db.get(Session, live.id)
                if row is not None:
                    row.status = SessionStatus.CRASHED if crashed else SessionStatus.EXITED
                    row.exit_code = exit_code
                    row.ended_at = now_utc_naive()
                    # Persist the last in-memory activity stamp in the same
                    # transaction as the finalize update, otherwise the
                    # periodic flush task races finalize and can lose the
                    # final burst's activity (session appears older than it
                    # actually was in the tree sort).
                    if live.last_activity_at is not None:
                        row.last_activity_ts = live.last_activity_at
                    external_sid = row.external_session_id
                    await db.commit()
        except Exception:
            log.exception("finalize: DB update failed for %s", live.id)
        # Emit domain event. `session_id` field carries the claude uuid (JSONL
        # basename) so EventStream can retire the corresponding entry from
        # its idle/ended dedup sets. `csm_session_id` stays in payload for
        # subscribers that key off the CSM row.
        try:
            await self._es.emit(Event(
                type=EventType.SESSION_CRASHED if crashed else EventType.SESSION_ENDED,
                ts=datetime.now(UTC),
                session_id=external_sid,
                project_path=None,
                payload={"csm_session_id": live.id, "exit_code": exit_code},
            ))
        except Exception:
            pass
        try:
            await asyncio.to_thread(
                self._persist_output_snapshot,
                live.id,
                live.ring.snapshot(),
            )
        except Exception:
            log.exception("finalize: output snapshot failed for %s", live.id)
        async with self._lock:
            self._live.pop(live.id, None)

    # ---- initial prompt ----
    async def _deliver_initial_prompt(self, sid: str, prompt: str) -> None:
        # "Brute-force wait + retry-by-observation" per design.
        # 1. Wait 3s for Claude REPL to be ready.
        # 2. Write prompt + CRLF, under the write_lock to avoid interleaving
        #    with any concurrent WS-attached user input.
        await asyncio.sleep(3.0)
        async with self._lock:
            live = self._live.get(sid)
        if live is None:
            return
        payload = prompt.encode("utf-8", errors="replace") + b"\r\n"
        try:
            async with live.write_lock:
                written = await asyncio.to_thread(live.pty.write, payload)
            if written < len(payload):
                log.error(
                    "initial prompt truncated for %s: %d/%d bytes — the "
                    "session may start with an incomplete first message",
                    sid, written, len(payload),
                )
        except Exception:
            log.exception("initial prompt write failed for %s", sid)


def _pid_alive(pid: int) -> bool:
    # ``kill(pid, 0)`` reports zombie processes as existing even though
    # their command has already exited and can never service the PTY again.
    # Treat Linux Z/X states as dead so a missed EOF cannot leave a Session
    # permanently displayed as running/orphaned.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close_paren = stat.rfind(")")
        if close_paren >= 0:
            state = stat[close_paren + 2 : close_paren + 3]
            if state in {"Z", "X"}:
                return False
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        # Non-Linux hosts or transient /proc races fall through to signal 0.
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
