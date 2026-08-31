"""SSE endpoint tests — verify per-client queue fan-out, heartbeat, and
graceful teardown on queue overflow and client disconnect.

Focus is on the internal `_event_producer` async generator (unit-level)
rather than spinning up the full FastAPI app with a real EventStream tail
loop, which would slow the suite and add flake surface. The producer's
contract with the outside world is entirely:
  1. read Event/sentinel from `queue`
  2. yield SSE bytes framing
  3. call `es.unsubscribe(sub_id)` in `finally`
so unit-testing it directly gives us all the coverage that matters.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from csm.api import events as events_mod
from csm.api.events import (
    _DISCONNECT,
    _enqueue_or_sentinel,
    _event_producer,
    _event_to_json,
)
from csm.core.events import Event, EventType


def _make_event(session_id: str = "abc") -> Event:
    return Event(
        type=EventType.SESSION_STARTED,
        ts=datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC),
        session_id=session_id,
        project_path="/tmp/proj",
        payload={"hello": "world"},
    )


class _FakeRequest:
    """Minimal Request stub — only `is_disconnected()` is used by the producer."""

    def __init__(self):
        self._disconnected = False

    async def is_disconnected(self) -> bool:
        return self._disconnected

    def disconnect(self) -> None:
        self._disconnected = True


def _fake_stream() -> MagicMock:
    es = MagicMock()
    es.unsubscribe = MagicMock()
    return es


async def _collect(agen, count: int, timeout: float = 1.0) -> list[bytes]:
    """Pull `count` frames from an async generator with a per-frame timeout."""
    out: list[bytes] = []
    for _ in range(count):
        out.append(await asyncio.wait_for(agen.__anext__(), timeout=timeout))
    return out


async def test_event_producer_yields_sse_frame_for_event():
    req = _FakeRequest()
    es = _fake_stream()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    ev = _make_event("s1")
    await queue.put(ev)

    agen = _event_producer(req, es, queue, sub_id="sub-1")
    frames = await _collect(agen, 1)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.startswith(b"data: ")
    assert frame.endswith(b"\n\n")
    body = frame[len(b"data: "):-2]
    obj = json.loads(body)
    assert obj["type"] == EventType.SESSION_STARTED.value
    assert obj["session_id"] == "s1"
    assert obj["payload"] == {"hello": "world"}

    req.disconnect()
    # Drain to trigger finally.
    with pytest.raises((StopAsyncIteration, asyncio.TimeoutError)):
        await asyncio.wait_for(agen.__anext__(), timeout=0.5)
    es.unsubscribe.assert_called_once_with("sub-1")


async def test_event_producer_emits_heartbeat_when_idle(monkeypatch):
    # Shrink the heartbeat interval so the test doesn't wait 15s.
    monkeypatch.setattr(events_mod, "_HEARTBEAT_SEC", 0.05)
    req = _FakeRequest()
    es = _fake_stream()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)

    agen = _event_producer(req, es, queue, sub_id="sub-2")
    frame = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
    assert frame == b": heartbeat\n\n"

    req.disconnect()
    with pytest.raises((StopAsyncIteration, asyncio.TimeoutError)):
        await asyncio.wait_for(agen.__anext__(), timeout=1.0)
    es.unsubscribe.assert_called_once_with("sub-2")


async def test_event_producer_stops_on_disconnect_sentinel():
    req = _FakeRequest()
    es = _fake_stream()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    await queue.put(_DISCONNECT)

    agen = _event_producer(req, es, queue, sub_id="sub-3")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=1.0)
    es.unsubscribe.assert_called_once_with("sub-3")


async def test_event_producer_stops_on_client_disconnect():
    req = _FakeRequest()
    es = _fake_stream()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    req.disconnect()  # already gone

    agen = _event_producer(req, es, queue, sub_id="sub-4")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=1.0)
    es.unsubscribe.assert_called_once_with("sub-4")


async def test_event_producer_preserves_event_order():
    req = _FakeRequest()
    es = _fake_stream()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    for sid in ("a", "b", "c"):
        await queue.put(_make_event(sid))

    agen = _event_producer(req, es, queue, sub_id="sub-5")
    frames = await _collect(agen, 3)
    ids = [json.loads(f[len(b"data: "):-2])["session_id"] for f in frames]
    assert ids == ["a", "b", "c"]

    req.disconnect()
    with pytest.raises((StopAsyncIteration, asyncio.TimeoutError)):
        await asyncio.wait_for(agen.__anext__(), timeout=0.5)


def test_event_to_json_shape():
    """Wire shape must match `/api/events/recent` items so the frontend
    can reuse one parser for both."""
    ev = _make_event()
    obj = json.loads(_event_to_json(ev))
    # Required keys per contract.
    for k in ("id", "type", "ts", "session_id", "project_path", "payload"):
        assert k in obj
    # `ts` must be ISO-formatted (iso_utc).
    assert "2026-07-11" in obj["ts"]


def test_enqueue_or_sentinel_normal_put():
    """Happy path: room in queue → event lands, no sentinel involved."""
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    ev = _make_event()
    _enqueue_or_sentinel(q, ev)
    assert q.qsize() == 1
    assert q.get_nowait() is ev


def test_enqueue_or_sentinel_full_evicts_and_pushes_sentinel(caplog):
    """Queue full → oldest event evicted, sentinel takes its slot so
    the producer wakes up and tears the connection down. Warning
    logged so ops sees the drop."""
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    old = _make_event("old")
    newer = _make_event("newer")
    q.put_nowait(old)
    q.put_nowait(newer)
    assert q.full()
    with caplog.at_level("WARNING", logger=events_mod.log.name):
        _enqueue_or_sentinel(q, _make_event("dropped"))
    # Log fired.
    assert any("overflow" in rec.message for rec in caplog.records)
    # Queue still full (2 slots) — oldest was evicted, sentinel took
    # its place, the incoming event was intentionally dropped.
    items = [q.get_nowait(), q.get_nowait()]
    # `newer` survived; sentinel appended at the tail.
    assert items[0] is newer
    assert items[1] is _DISCONNECT


def test_enqueue_or_sentinel_maxsize_one_still_delivers_sentinel(caplog):
    """Tightest bound: maxsize=1. Evict-and-replace pattern still works
    — the one occupied slot gets swapped for the sentinel."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait(_make_event("a"))
    with caplog.at_level("WARNING", logger=events_mod.log.name):
        _enqueue_or_sentinel(q, _make_event("b"))
    assert q.qsize() == 1
    assert q.get_nowait() is _DISCONNECT
