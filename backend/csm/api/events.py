"""SSE endpoint fan-out for the in-memory EventStream.

Subscribes each connected client to `app.state.event_stream` and forwards
domain events as `text/event-stream` frames. Bounded per-client queue
(maxsize=`_QUEUE_MAX`); when a slow consumer fills it, we disconnect that
client rather than trying to drop-old-events (simpler + no silent gaps —
the client's reconnect path will `refresh()` back to a consistent state).

Wire format: one `data: <json>\\n\\n` frame per event. The JSON matches
`/api/events/recent` item shape (id/type/ts/session_id/project_path/payload)
so the frontend can reuse the same parser.

Heartbeats: a `: heartbeat\\n\\n` comment line every `_HEARTBEAT_SEC` keeps
proxies from idle-timing-out the connection and lets the client detect a
dead connection quickly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from csm.api._serialize import iso_utc
from csm.core.event_stream import EventStream
from csm.core.events import Event

router = APIRouter(prefix="/api/events", tags=["events"])
log = logging.getLogger(__name__)

_QUEUE_MAX = 512
_HEARTBEAT_SEC = 15.0

# Sentinel used to signal "queue overflowed — drop this client".
_DISCONNECT: dict[str, Any] = {"__disconnect__": True}


def _stream(request: Request) -> EventStream:
    es = getattr(request.app.state, "event_stream", None)
    if es is None:
        raise HTTPException(status_code=503, detail="event stream not initialized")
    return es


def _event_to_json(event: Event) -> str:
    """Serialize an Event to the wire dict used by `/api/events/recent`."""
    return json.dumps({
        "id": event.id,
        "type": event.type.value,
        "ts": iso_utc(event.ts),
        "session_id": event.session_id,
        "project_path": event.project_path,
        "payload": event.payload or {},
    }, default=str)


def _enqueue_or_sentinel(queue: asyncio.Queue, event: Event) -> None:
    """Non-blocking put with graceful slow-consumer handling.

    Normal path: `queue.put_nowait(event)`. If the queue is full the
    client is too slow — we evict the oldest queued event to make room
    and insert `_DISCONNECT` in its place so the producer wakes up on
    its next `get()` and tears the client down. The dropped event and
    the evicted one are both lost; the client's reconnect will bootstrap
    back to a consistent state via a full refresh.

    Extracted from `stream()`'s closure so the queue-full → sentinel
    transition has a testable seam. The queue is bounded (see
    `_QUEUE_MAX`), and `asyncio.Queue.put_nowait` is synchronous, so
    without the explicit evict step the second put would fail exactly
    like the first — sentinel would never actually land.
    """
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
        queue.put_nowait(_DISCONNECT)  # type: ignore[arg-type]
    except (asyncio.QueueEmpty, asyncio.QueueFull):
        # Only reachable under a concurrent drain we didn't expect; the
        # producer's heartbeat timeout will still notice `is_disconnected`
        # and unwind eventually.
        pass
    log.warning(
        "sse queue overflow: dropped event type=%s sid=%s, client will be torn down",
        event.type.value,
        event.session_id,
    )


async def _event_producer(
    request: Request,
    es: EventStream,
    queue: asyncio.Queue,
    sub_id: str,
) -> AsyncIterator[bytes]:
    """Read from the per-client queue and yield SSE-framed bytes.

    Emits a heartbeat comment every `_HEARTBEAT_SEC` of quiet so proxies
    and clients can tell the pipe is alive. Tears down cleanly on client
    disconnect, queue overflow sentinel, or CancelledError.
    """
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SEC)
            except TimeoutError:
                yield b": heartbeat\n\n"
                continue
            if isinstance(item, dict) and item.get("__disconnect__"):
                log.warning(
                    "sse client dropped: per-client queue full (maxsize=%d)",
                    _QUEUE_MAX,
                )
                return
            payload = _event_to_json(item)
            yield f"data: {payload}\n\n".encode()
    except asyncio.CancelledError:
        return
    finally:
        es.unsubscribe(sub_id)


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE stream of all EventStream domain events.

    Client contract:
      - Reconnect on any disconnect (browser EventSource does this by
        default). The server does not replay missed events; the client
        must trigger a full refresh on `open` to bootstrap.
      - Overflow policy: if the server-side per-client queue overflows
        (slow consumer), the client is disconnected. Reconnect will
        rebuild state via the client's onopen refresh.
    """
    es = _stream(request)
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)

    async def _on_event(event: Event) -> None:
        _enqueue_or_sentinel(queue, event)

    sub_id = es.subscribe(None, _on_event)  # None = subscribe all

    # `X-Accel-Buffering: no` disables nginx buffering if fronted by one.
    return StreamingResponse(
        _event_producer(request, es, queue, sub_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
