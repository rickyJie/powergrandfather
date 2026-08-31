"""Event Stream unit tests — mock JSONL files, verify event emission + replay + subs."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from csm.adapters.jsonl_tail import JsonlTailer, project_dir_to_cwd
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType


# ---- helpers ----
def make_projects_root(tmp: Path, project_dirname: str = "-tmp-proj") -> Path:
    root = tmp / "projects"
    proj = root / project_dirname
    proj.mkdir(parents=True)
    return root


def append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def make_msg(role: str, text: str = "", usage: dict | None = None, model: str = "claude-sonnet-4-6", ts: datetime | None = None, stop_reason: str | None = None) -> dict:
    msg = {"role": role, "content": [{"type": "text", "text": text}], "model": model}
    if usage:
        msg["usage"] = usage
    if stop_reason is not None:
        msg["stop_reason"] = stop_reason
    return {
        "timestamp": (ts or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "uuid": str(uuid.uuid4()),
        "message": msg,
    }


def make_api_error(text: str, ts: datetime | None = None) -> dict:
    return {
        "timestamp": (ts or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "uuid": str(uuid.uuid4()),
        "isApiErrorMessage": True,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


# ---- jsonl_tail ----
def test_project_dir_decode():
    assert project_dir_to_cwd("-tmp-foo-bar") == "/tmp/foo/bar"


def test_tailer_incremental(tmp_path: Path):
    root = make_projects_root(tmp_path)
    jsonl = root / "-tmp-proj" / "session1.jsonl"
    jsonl.touch()

    tailer = JsonlTailer(root)
    assert tailer.scan_once() == []
    assert "session1.jsonl" in str(next(iter(tailer.take_newly_seen())))

    append_jsonl(jsonl, make_msg("user", "hi"))
    recs = tailer.scan_once()
    assert len(recs) == 1
    # RawRecord.claude_session_id is the tailer-local field (JSONL basename);
    # keep that adapter-specific name — the domain-side rename is in the
    # Session ORM column, not the raw tail record.
    assert recs[0].claude_session_id == "session1"

    # No re-read of consumed bytes.
    assert tailer.scan_once() == []

    # New line appended is picked up.
    append_jsonl(jsonl, make_msg("assistant", "hello", usage={"input_tokens": 5, "output_tokens": 3}))
    recs = tailer.scan_once()
    assert len(recs) == 1
    assert recs[0].obj["message"]["role"] == "assistant"


# ---- event_stream ----
@pytest.fixture
async def stream(tmp_path: Path):
    root = make_projects_root(tmp_path)
    s = EventStream(projects_root=root, poll_interval_sec=0.05,
                    watchdog_interval_sec=0.5, session_idle_minutes=0,
                    seed_stale_history_as_idle=False)
    yield s
    await s.stop()


async def test_subscribe_emit(stream: EventStream):
    received: list[Event] = []

    async def handler(e: Event):
        received.append(e)

    stream.subscribe([EventType.MESSAGE_USER_SENT], handler)
    await stream.emit(Event(type=EventType.MESSAGE_USER_SENT, ts=datetime.now(UTC),
                            session_id="s1", project_path="/tmp", payload={}))
    # Wrong type → not delivered.
    await stream.emit(Event(type=EventType.USAGE_RECORDED, ts=datetime.now(UTC),
                            session_id="s1", project_path="/tmp", payload={}))
    assert len(received) == 1


async def test_unsubscribe(stream: EventStream):
    received: list[Event] = []

    async def handler(e: Event):
        received.append(e)

    sub_id = stream.subscribe(None, handler)
    await stream.emit(Event(type=EventType.SESSION_STARTED, ts=datetime.now(UTC),
                            session_id="s1", project_path="/tmp", payload={}))
    stream.unsubscribe(sub_id)
    await stream.emit(Event(type=EventType.SESSION_STARTED, ts=datetime.now(UTC),
                            session_id="s1", project_path="/tmp", payload={}))
    assert len(received) == 1


async def test_replay_filters(stream: EventStream):
    base = datetime.now(UTC)
    for i, t in enumerate([EventType.SESSION_STARTED, EventType.USAGE_RECORDED, EventType.SESSION_ENDED]):
        await stream.emit(Event(type=t, ts=base + timedelta(seconds=i),
                                 session_id=f"s{i}", project_path="/tmp", payload={}))
    all_evt = stream.replay()
    assert len(all_evt) == 3
    only_s1 = stream.replay(session_id="s1")
    assert len(only_s1) == 1
    later = stream.replay(since=base + timedelta(seconds=1))
    assert len(later) == 2


async def test_tail_emits_events(stream: EventStream, tmp_path: Path):
    # Use the same root the stream was built with.
    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "abcdef.jsonl"

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    stream.subscribe(None, handler)

    await stream.start()

    append_jsonl(jsonl, make_msg("user", "hi"))
    append_jsonl(jsonl, make_msg("assistant", "hello", usage={"input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 3}, stop_reason="end_turn"))

    # Wait a tick or two for the loop to pick up.
    await asyncio.sleep(0.25)

    types = {e.type for e in received}
    assert EventType.SESSION_STARTED in types
    assert EventType.MESSAGE_USER_SENT in types
    assert EventType.MESSAGE_ASSISTANT_DONE in types
    assert EventType.USAGE_RECORDED in types


async def test_injected_user_record_does_not_emit_user_sent(stream: EventStream, tmp_path: Path):
    """Role "user" is not the same as "the human spoke".

    A subagent's task-notification and a headless `claude -p` prompt are both
    filed under role "user". Emitting MESSAGE_USER_SENT for them flipped the
    session to RUNNING and opened a WorktimeTracker interval for work nobody
    did. The typed message in the same file still reports, so the gate can't be
    passing by simply suppressing everything.
    """
    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "injected.jsonl"

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    stream.subscribe([EventType.MESSAGE_USER_SENT], handler)

    await stream.start()

    notification = make_msg("user", "<task-notification>…")
    notification["origin"] = {"kind": "task-notification"}
    notification["promptSource"] = "system"
    append_jsonl(jsonl, notification)

    sdk_prompt = make_msg("user", "Rule that fired: …")
    sdk_prompt["promptSource"] = "sdk"
    append_jsonl(jsonl, sdk_prompt)

    typed = make_msg("user", "hi")
    typed["origin"] = {"kind": "human"}
    typed["promptSource"] = "typed"
    append_jsonl(jsonl, typed)

    await asyncio.sleep(0.25)

    assert len(received) == 1


async def test_tool_use_turn_does_not_emit_assistant_done(stream: EventStream, tmp_path: Path):
    """Regression: a tool_use stop_reason on an assistant turn must NOT
    emit MESSAGE_ASSISTANT_DONE. The grace-timer in AutomationRunner relies
    on this event only firing at true end_turn — otherwise every mid-turn
    tool call would re-arm a 10s kill timer and busy sessions get murdered
    mid-task (the 2026-06-29 overnight regression)."""
    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "tooluse.jsonl"

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    stream.subscribe(None, handler)
    await stream.start()

    append_jsonl(jsonl, make_msg("user", "do something"))
    # Three intermediate tool_use turns, then a real end_turn.
    append_jsonl(jsonl, make_msg("assistant", "thinking", stop_reason="tool_use"))
    append_jsonl(jsonl, make_msg("assistant", "still thinking", stop_reason="tool_use"))
    append_jsonl(jsonl, make_msg("assistant", "almost done", stop_reason="tool_use"))
    append_jsonl(jsonl, make_msg("assistant", "done", stop_reason="end_turn"))
    await asyncio.sleep(0.25)

    done_events = [e for e in received if e.type == EventType.MESSAGE_ASSISTANT_DONE]
    # Exactly ONE assistant_done — the final end_turn — not four.
    assert len(done_events) == 1, f"expected 1 assistant_done, got {len(done_events)}"


async def test_rate_limit_hit(stream: EventStream):
    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "limithit.jsonl"

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    stream.subscribe([EventType.RATE_LIMIT_HIT, EventType.API_ERROR], handler)

    await stream.start()
    append_jsonl(jsonl, make_api_error("You've hit your limit · resets 8:30pm (Asia/Shanghai)"))
    await asyncio.sleep(0.25)

    types = {e.type for e in received}
    assert EventType.API_ERROR in types
    assert EventType.RATE_LIMIT_HIT in types
    hit = next(e for e in received if e.type == EventType.RATE_LIMIT_HIT)
    assert "Asia/Shanghai" in (hit.payload.get("reset_text") or "")


async def test_session_idle_via_watchdog(stream: EventStream):
    # session_idle_minutes=0 → any age > 0 triggers idle
    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "idlesess.jsonl"
    append_jsonl(jsonl, make_msg("user", "hello"))

    # Force file mtime old enough.
    old = (datetime.now() - timedelta(minutes=5)).timestamp()
    os.utime(jsonl, (old, old))

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    stream.subscribe([EventType.SESSION_IDLE], handler)

    await stream.start()
    # Trigger one tail + one watchdog cycle.
    await asyncio.sleep(0.8)

    idles = [e for e in received if e.type == EventType.SESSION_IDLE]
    assert len(idles) >= 1
    assert idles[0].session_id == "idlesess"


# ---- Finding-6 observability: watchdog and subscriber logging ----
async def test_watchdog_tick_logs_stats(stream: EventStream, caplog):
    """Every watchdog tick must log its per-tick counts so csm.log lets us
    distinguish "watchdog dead" from "watchdog running but nothing to emit"."""
    import logging
    caplog.set_level(logging.INFO, logger="csm.core.event_stream")

    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "watchsess.jsonl"
    append_jsonl(jsonl, make_msg("user", "hi"))
    old = (datetime.now() - timedelta(minutes=5)).timestamp()
    os.utime(jsonl, (old, old))

    await stream.start()
    await asyncio.sleep(0.8)

    tick_lines = [r.getMessage() for r in caplog.records if "watchdog tick" in r.getMessage()]
    assert tick_lines, "watchdog tick log line never appeared"
    assert any("idle_emitted=" in ln for ln in tick_lines)
    emit_lines = [r.getMessage() for r in caplog.records if "dispatched SESSION_IDLE" in r.getMessage()]
    assert emit_lines, "SESSION_IDLE dispatch log line never appeared"


async def test_session_ended_evicts_from_dedup_sets(stream: EventStream):
    """Finding-6d (TR3): SESSION_ENDED must retire the session id from
    `_idle_emitted` and add it to `_ended_emitted` so:
    (a) the watchdog stops walking its stale JSONL every tick, and
    (b) `_idle_emitted` doesn't grow unbounded on a long-running CSM."""
    stream._idle_emitted.add("finished-sess")
    await stream.emit(Event(
        type=EventType.SESSION_ENDED,
        ts=datetime.now(UTC),
        session_id="finished-sess",
        project_path="/tmp",
        payload={"csm_session_id": "csm-abc", "exit_code": 0},
    ))
    assert "finished-sess" not in stream._idle_emitted
    assert "finished-sess" in stream._ended_emitted


async def test_session_crashed_also_evicts(stream: EventStream):
    """SESSION_CRASHED has the same lifecycle semantics for dedup."""
    stream._idle_emitted.add("crashed-sess")
    await stream.emit(Event(
        type=EventType.SESSION_CRASHED,
        ts=datetime.now(UTC),
        session_id="crashed-sess",
        project_path="/tmp",
        payload={"csm_session_id": "csm-xyz", "exit_code": 137},
    ))
    assert "crashed-sess" not in stream._idle_emitted
    assert "crashed-sess" in stream._ended_emitted


async def test_seed_stale_history_prevents_boot_storm(tmp_path: Path):
    """Finding-6b (test-run-2 discovery): a workstation with thousands of
    stale historical JSONL files used to fire SESSION_IDLE for every one
    of them on the first watchdog tick. Boot-time seeding treats anything
    already past the idle threshold as "belongs to a previous life" so
    only fresh idle transitions signal."""
    root = tmp_path / "projects"
    proj = root / "-tmp-proj"
    proj.mkdir(parents=True)
    # Write a historical JSONL that would have blown up csm.log pre-fix.
    stale = proj / "ancient.jsonl"
    append_jsonl(stale, make_msg("user", "long ago"))
    old = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(stale, (old, old))

    s = EventStream(projects_root=root, poll_interval_sec=0.05,
                    watchdog_interval_sec=0.5, session_idle_minutes=0,
                    seed_stale_history_as_idle=True)

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    s.subscribe([EventType.SESSION_IDLE], handler)

    await s.start()
    await asyncio.sleep(1.2)
    await s.stop()

    stale_idles = [e for e in received if e.session_id == "ancient"]
    assert stale_idles == [], f"boot-seed should suppress stale history, got {stale_idles}"


async def test_idle_emitted_only_once_per_session(stream: EventStream):
    """Finding-6b (test-run-2 discovery): the watchdog used to re-fire
    SESSION_IDLE for every stale JSONL on every tick, drowning csm.log
    and any real idle signal in noise. After the fix a given session
    fires at most one SESSION_IDLE per EventStream lifetime."""
    root = stream._tailer.projects_root
    jsonl = root / "-tmp-proj" / "dedupsess.jsonl"
    append_jsonl(jsonl, make_msg("user", "hello"))
    old = (datetime.now() - timedelta(minutes=10)).timestamp()
    os.utime(jsonl, (old, old))

    received: list[Event] = []
    async def handler(e: Event):
        received.append(e)
    stream.subscribe([EventType.SESSION_IDLE], handler)

    await stream.start()
    # Two watchdog cycles (interval=0.5s). Pre-fix both would emit; post-fix
    # only the first, and the second logs `already_idle=1`.
    await asyncio.sleep(1.6)

    idles = [e for e in received if e.session_id == "dedupsess"]
    assert len(idles) == 1, f"expected 1 SESSION_IDLE per session, got {len(idles)}"


async def test_subscriber_exception_is_logged_not_swallowed(stream: EventStream, caplog):
    """Finding-6 root-cause candidate: a subscriber that raised would kill
    the SESSION_IDLE path silently. The stream keeps running, but the
    exception MUST land in the log so we can see it next time."""
    import logging
    caplog.set_level(logging.ERROR, logger="csm.core.event_stream")

    async def bad_handler(_e: Event) -> None:
        raise RuntimeError("boom from subscriber")

    stream.subscribe([EventType.SESSION_STARTED], bad_handler)
    await stream.emit(Event(
        type=EventType.SESSION_STARTED,
        ts=datetime.now(UTC),
        session_id="s1",
        project_path="/tmp",
        payload={},
    ))
    # stream survived — a second emit still delivers
    good_calls: list[Event] = []
    async def good_handler(e: Event):
        good_calls.append(e)
    stream.subscribe([EventType.SESSION_STARTED], good_handler)
    await stream.emit(Event(
        type=EventType.SESSION_STARTED,
        ts=datetime.now(UTC),
        session_id="s2",
        project_path="/tmp",
        payload={},
    ))
    assert good_calls, "stream should keep delivering after a subscriber raises"

    err_lines = [r.getMessage() for r in caplog.records if "subscriber failed" in r.getMessage()]
    assert err_lines, "subscriber exception must be logged (Finding-6)"


async def test_slow_subscriber_does_not_block_others(stream: EventStream):
    """E1 (2026-07-25): a slow subscriber (e.g. SupervisorAgent's `claude -p`)
    must not block other subscribers. Dispatch is now concurrent via
    asyncio.gather, so total wall-clock latency ~= slowest handler, not the
    sum of all handlers.
    """
    import time

    fast_called: list[float] = []
    slow_called: list[float] = []

    async def slow_handler(_e: Event) -> None:
        await asyncio.sleep(0.5)
        slow_called.append(time.monotonic())

    async def fast_handler(_e: Event) -> None:
        fast_called.append(time.monotonic())

    stream.subscribe([EventType.SESSION_STARTED], slow_handler)
    stream.subscribe([EventType.SESSION_STARTED], fast_handler)

    t0 = time.monotonic()
    await stream.emit(Event(
        type=EventType.SESSION_STARTED,
        ts=datetime.now(UTC),
        session_id="s-parallel",
        project_path="/tmp",
        payload={},
    ))
    elapsed = time.monotonic() - t0

    assert fast_called, "fast subscriber must be invoked"
    assert slow_called, "slow subscriber must be invoked"
    # Fast handler should fire well before slow handler's 0.5s sleep completes.
    assert fast_called[0] - t0 < 0.2, (
        f"fast handler was blocked by slow one; fired at {fast_called[0] - t0:.3f}s"
    )
    # Total emit time bounded by slowest handler + small overhead,
    # not by sum(handlers). Allow generous 0.9s ceiling for CI jitter.
    assert elapsed < 0.9, f"emit took {elapsed:.3f}s — dispatch appears serial"
