"""Fixed-capacity byte ring buffer for replaying recent PTY output."""
from __future__ import annotations

from collections import deque
from threading import Lock


class RingBuffer:
    """Append-only ring of bytes with a hard cap on stored size.

    Stores chunks in a deque so we don't keep copying; `snapshot()` returns
    a single bytes payload of the most recent ≤ capacity bytes.
    """

    def __init__(self, capacity: int = 1024 * 1024):
        self.capacity = int(capacity)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = Lock()

    def write(self, data: bytes) -> None:
        if not data:
            return
        # If the single payload exceeds capacity, only keep the tail.
        if len(data) >= self.capacity:
            data = data[-self.capacity:]
            with self._lock:
                self._chunks.clear()
                self._chunks.append(data)
                self._size = len(data)
                return
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            while self._size > self.capacity and self._chunks:
                head = self._chunks.popleft()
                self._size -= len(head)
                if self._size < 0:
                    # Should never happen, but stay defensive.
                    self._size = 0
                    self._chunks.clear()
                    break

    def snapshot(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)

    def size(self) -> int:
        return self._size

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
