"""inotify-driven tail of a single Claude JSONL transcript.

Unlike the global `JsonlTailer` (5s cadence, every project), this tails ONE
file with sub-100ms latency for the duration of a single WebSocket connection.
We use `watchfiles.awatch` (Rust + OS notify backend) on the file's parent
directory, fall back to polling if the watcher fails to start. Construct with
the resolved file path, call `start()` when the WS attaches, `stop()` on
disconnect.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path

from csm.core.jsonl_lines import parse_complete_json_lines
from csm.modules.agent.message_router import route_record

log = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


# ── Cross-connection history cache ───────────────────────────────────────────
# A parsed full-history snapshot keyed by (mtime_ns, size). On a flaky SSH
# tunnel the mobile client reconnects often, and re-parsing a 10-25MB transcript
# from offset 0 every single time is what turned "reconnect" into a re-freeze
# cascade. With this, repeat opens/reconnects of an unchanged file reuse the
# parse. Keyed by identity so a grown file (new size) misses and re-parses.
_HISTORY_CACHE: OrderedDict[str, tuple[tuple[int, int], list[dict], int]] = OrderedDict()
_HISTORY_CACHE_MAX = 8


def _parse_file_sync(path: Path, start: int = 0) -> tuple[list[dict], int]:
    """Parse a JSONL transcript from byte `start` to the last complete line.
    Pure blocking I/O + CPU — always call via `asyncio.to_thread`. Returns
    (events, end_offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(start)
            buf = f.read()
    except OSError:
        return [], start
    parsed = parse_complete_json_lines(buf, start)
    events: list[dict] = []
    for jl in parsed.lines:
        events.extend(route_record(jl.obj))
    return events, parsed.last_complete_end


async def get_history(path: Path) -> tuple[list[dict], int]:
    """Full parsed history for `path`, cached by (mtime_ns, size). Returns
    (events, parsed_offset) — start a live tail from `parsed_offset` to pick up
    only bytes written after this snapshot, avoiding any re-parse."""
    path = Path(path)
    try:
        st = os.stat(path)
    except OSError:
        return [], 0
    key = (st.st_mtime_ns, st.st_size)
    ck = str(path)
    hit = _HISTORY_CACHE.get(ck)
    if hit is not None and hit[0] == key:
        _HISTORY_CACHE.move_to_end(ck)
        return hit[1], hit[2]
    events, end_offset = await asyncio.to_thread(_parse_file_sync, path, 0)
    _HISTORY_CACHE[ck] = (key, events, end_offset)
    _HISTORY_CACHE.move_to_end(ck)
    while len(_HISTORY_CACHE) > _HISTORY_CACHE_MAX:
        _HISTORY_CACHE.popitem(last=False)
    return events, end_offset


class JsonlFastTail:
    def __init__(
        self,
        path: Path,
        on_event: EventHandler,
        poll_interval_sec: float = 0.2,
    ):
        self._path = Path(path)
        self._on_event = on_event
        self._poll = poll_interval_sec
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        # Optional batch handler for the initial replay scan. If set, the events
        # produced by the very first scan are buffered + delivered as one list
        # to `_on_replay`, and `_on_event` is bypassed for those events. After
        # the initial scan completes, live updates flow through `_on_event` as
        # usual.
        self._on_replay: Callable[[list[dict]], Awaitable[None]] | None = None
        self._replaying = False
        self._replay_buf: list[dict] = []

    async def start(
        self,
        replay_from_start: bool = True,
        on_replay: Callable[[list[dict]], Awaitable[None]] | None = None,
        start_offset: int | None = None,
    ) -> None:
        """Begin tailing. If `replay_from_start` is True, the first scan emits
        every line in the file so the WS client gets full history; otherwise
        only new bytes are emitted.

        `start_offset` (live mode only) resumes from a known byte offset instead
        of the current EOF — pass the `parsed_offset` from `get_history()` so the
        live tail picks up exactly the bytes written after that snapshot, with no
        gap and no re-parse of history already sent.

        If `on_replay` is supplied AND `replay_from_start` is True, the events
        from that first scan are batched and handed to `on_replay` as one list,
        so the WS can ship them in a single frame instead of N tiny frames.
        Subsequent live events still flow through the per-event `on_event`.
        """
        if self._task is not None:
            return
        if not replay_from_start:
            if start_offset is not None:
                self._offset = start_offset
            elif self._path.exists():
                try:
                    self._offset = self._path.stat().st_size
                except OSError:
                    self._offset = 0
        if replay_from_start and on_replay is not None:
            self._on_replay = on_replay
            self._replaying = True
            self._replay_buf = []
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._loop(), name=f"agent-fast-tail-{self._path.name}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_evt.set()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except TimeoutError:
            self._task.cancel()
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        # Initial scan: covers replay-from-start AND any bytes written between
        # offset capture and watcher start (race-free).
        try:
            await self._scan_once()
        except Exception:
            log.exception("fast-tail initial scan failed for %s", self._path)
        # Flush the replay batch (if requested) as one frame before going live.
        if self._replaying:
            self._replaying = False
            cb = self._on_replay
            buf = self._replay_buf
            self._replay_buf = []
            if cb is not None:
                try:
                    await cb(buf)
                except Exception:
                    log.exception("on_replay handler raised")

        try:
            await self._watch_loop()
        except Exception:
            log.exception("fast-tail watcher crashed for %s; falling back to poll", self._path)
            await self._poll_loop()

    async def _watch_loop(self) -> None:
        """inotify-driven loop via `watchfiles.awatch`. Wakes immediately on
        any change to the target file in its parent directory."""
        from watchfiles import awatch

        parent = self._path.parent
        if not parent.exists():
            # No directory to watch (e.g. claude hasn't created the project
            # folder yet) — fall back to poll until it appears.
            await self._poll_loop()
            return

        target_name = self._path.name
        stop_aw = asyncio.create_task(self._stop_evt.wait(), name="fast-tail-stop")

        async for changes in awatch(parent, stop_event=self._stop_evt, recursive=False):
            for _change, p in changes:
                if Path(p).name == target_name:
                    try:
                        await self._scan_once()
                    except Exception:
                        log.exception("fast-tail scan failed for %s", self._path)
                    break  # one scan per batch handles all change types
            if self._stop_evt.is_set():
                break

        if not stop_aw.done():
            stop_aw.cancel()

    async def _poll_loop(self) -> None:
        """Fallback poll loop (used when watchfiles fails or the parent dir
        doesn't exist yet)."""
        while not self._stop_evt.is_set():
            try:
                await self._scan_once()
            except Exception:
                log.exception("fast-tail scan failed for %s", self._path)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._poll)
            except TimeoutError:
                pass

    def _parse_range(self) -> tuple[list[dict], int] | None:
        """Read the new bytes since `self._offset` and parse them into events.

        Pure blocking I/O + CPU (file read, `json.loads` per line, routing) —
        this is the hot path that, on a multi-MB transcript, used to run inline
        on the event loop and freeze the whole process for 300-500ms (starving
        heartbeats → false disconnects → reconnect → re-replay avalanche). It is
        now called via `asyncio.to_thread` from `_scan_once`, so the loop stays
        responsive during a big replay. Returns `(events, new_offset)` or None
        when there's nothing new. Does NOT mutate state — the async caller owns
        `self._offset` so there's no cross-thread write.
        """
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            return None
        offset = self._offset
        # Truncation guard (file rotated / rewritten shorter).
        if st.st_size < offset:
            offset = 0
        if st.st_size == offset:
            return None
        try:
            with open(self._path, "rb") as f:
                f.seek(offset)
                buf = f.read()
        except OSError:
            return None
        parsed = parse_complete_json_lines(buf, offset)
        events: list[dict] = []
        for jl in parsed.lines:
            events.extend(route_record(jl.obj))
        return events, parsed.last_complete_end

    async def _scan_once(self) -> None:
        # Parse off the event loop — a large replay must not block the process.
        parsed = await asyncio.to_thread(self._parse_range)
        if parsed is None:
            return
        events, new_offset = parsed
        self._offset = new_offset
        for event in events:
            if self._replaying:
                self._replay_buf.append(event)
                continue
            try:
                await self._on_event(event)
            except Exception:
                log.exception("on_event handler raised")


def conversation_jsonl_path(
    projects_root: Path, cwd: str, external_session_id: str
) -> Path:
    """Compute the JSONL transcript path for a given (cwd, external_session_id).

    Claude's convention is `~/.claude/projects/<encoded_cwd>/<sid>.jsonl`,
    where encoded_cwd is the absolute path with `/` (and, empirically for
    some paths, `_`) replaced by `-`.

    Encoding is not fully deterministic (observed: `sample_pipeline` becomes
    `sample-pipeline` while `MyPipeline_auto` stays as-is), so we
    try the naive computation first and fall back to a glob-by-sid search
    across all project dirs. external_session_id is a UUID, so uniqueness
    across the projects tree is safe to rely on. Returns the naive
    computed path unchanged when nothing resolves — caller behavior
    (`.is_file()` check) is preserved.
    """
    cwd = cwd.rstrip("/")
    encoded = cwd.replace("/", "-")
    root = Path(projects_root)
    naive = root / encoded / f"{external_session_id}.jsonl"
    if naive.is_file():
        return naive
    try:
        matches = list(root.glob(f"*/{external_session_id}.jsonl"))
    except OSError:
        return naive
    if len(matches) == 1:
        return matches[0]
    return naive
