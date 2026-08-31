"""In-app notification sink — fan-out push to attached WebSocket clients."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Per-client send timeout. A wedged TCP client (network drop mid-send,
# receiver deadlocked) would otherwise stall the whole fan-out loop
# behind `await ws.send_text(...)`. 2s is generous for a healthy
# LAN client but short enough that a dead one is dropped promptly.
_SEND_TIMEOUT_SEC = 2.0


class InAppSink:
    """WebSocket fan-out for in-app notification push.

    Holds an in-memory set of attached `WebSocket` objects. The
    `/api/notifications/ws` endpoint calls `attach(ws)` on accept and
    `detach(ws)` on disconnect.

    `send(notification_dict)` serialises the payload once and writes it to
    every attached client. Failing clients — either raising an exception
    or exceeding `_SEND_TIMEOUT_SEC` — are dropped from the set so the
    next fan-out doesn't retry them.
    """

    def __init__(self) -> None:
        self._clients: set[Any] = set()

    async def attach(self, ws: Any) -> None:
        await ws.accept()
        self._clients.add(ws)

    def detach(self, ws: Any) -> None:
        self._clients.discard(ws)

    async def send(self, notification: dict[str, Any]) -> int:
        """Send the notification to every attached client; return reach count.

        Each per-client `send_text` is bounded by `_SEND_TIMEOUT_SEC`;
        clients that time out are treated the same as clients that
        raise — dropped from the set. That prevents a single stuck
        socket from blocking the loop for every other client.
        """
        if not self._clients:
            return 0
        payload = json.dumps(notification, default=str)
        dead: list[Any] = []
        sent = 0
        for ws in list(self._clients):
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=_SEND_TIMEOUT_SEC)
                sent += 1
            except TimeoutError:
                log.warning("inapp sink: dropping ws client after %ss send timeout", _SEND_TIMEOUT_SEC)
                dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
        return sent

    @property
    def client_count(self) -> int:
        return len(self._clients)
