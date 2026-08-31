"""Framing and delivery of one typed message on its way to a CLI's PTY.

Reported symptom that drove this: several long messages sent from the phone in
a row appeared to send — no error, no reply — and then a later short "hi" made
ALL of them appear at once. The text was reaching claude's composer and sitting
there unsubmitted, because a large burst trips claude's paste-detection and the
trailing CR is absorbed as literal content instead of acting as Enter.

Two separable things have to hold:
  * the adapter must SAY the message needs a paste + a separate submit
    (tests/unit/backends/test_claude_adapter.py), and
  * the manager must DELIVER those chunks as genuinely separate reads, without
    letting anything interleave between the paste and its Enter.

This file covers the second half, plus the short-write reporting that used to
turn a message that never submitted into an HTTP 200.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from csm.modules.session_manager.manager import SessionManager


class _RecordingPty:
    """Captures each write as its own entry, with the time it arrived."""

    def __init__(self, accept: int | None = None):
        self.writes: list[bytes] = []
        self.stamps: list[float] = []
        self._accept = accept  # cap per write, to simulate a full PTY buffer

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        self.stamps.append(time.monotonic())
        return len(data) if self._accept is None else min(self._accept, len(data))


def _manager_with(pty: _RecordingPty) -> tuple[SessionManager, str]:
    mgr = SessionManager.__new__(SessionManager)
    mgr._lock = asyncio.Lock()
    live = MagicMock()
    live.pty = pty
    live.write_lock = asyncio.Lock()
    mgr._live = {"s1": live}
    mgr._note_interrupt_bytes = lambda *_a, **_k: None
    return mgr, "s1"


@pytest.mark.asyncio
async def test_chunks_land_as_separate_writes():
    pty = _RecordingPty()
    mgr, sid = _manager_with(pty)

    written, total = await mgr.write_input_sequence(sid, [b"\x1b[200~hi\x1b[201~", b"\r"])

    assert pty.writes == [b"\x1b[200~hi\x1b[201~", b"\r"]
    assert written == total == 15


@pytest.mark.asyncio
async def test_a_gap_separates_them():
    """Two back-to-back os.write calls on a PTY are routinely coalesced into a
    single read if the CLI isn't scheduled in between — which would put the CR
    back inside the paste burst and reproduce the original bug exactly."""
    pty = _RecordingPty()
    mgr, sid = _manager_with(pty)

    await mgr.write_input_sequence(sid, [b"a", b"\r"])

    assert pty.stamps[1] - pty.stamps[0] >= SessionManager.PROSE_CHUNK_GAP_SEC * 0.8


@pytest.mark.asyncio
async def test_a_single_chunk_pays_no_gap():
    """Every codex send, and every short claude send, is one chunk. They must
    not get slower because long claude messages needed splitting."""
    pty = _RecordingPty()
    mgr, sid = _manager_with(pty)

    t0 = time.monotonic()
    await mgr.write_input_sequence(sid, [b"hello\r\n"])

    assert time.monotonic() - t0 < SessionManager.PROSE_CHUNK_GAP_SEC


@pytest.mark.asyncio
async def test_a_short_write_stops_the_sequence():
    """Once a chunk is truncated the CLI's input state is already wrong.
    Writing the CR on top of a half-delivered paste would submit mangled text —
    strictly worse than reporting the failure."""
    pty = _RecordingPty(accept=3)
    mgr, sid = _manager_with(pty)

    written, total = await mgr.write_input_sequence(sid, [b"abcdefgh", b"\r"])

    assert pty.writes == [b"abcdefgh"]      # the CR was never sent
    assert written == 3 and total == 9


@pytest.mark.asyncio
async def test_a_dead_session_reports_zero_of_the_real_total():
    """`written == 0` is what the endpoint turns into 409. `total` must still
    be the real size so the caller can tell "nothing" from "not everything"."""
    mgr = SessionManager.__new__(SessionManager)
    mgr._lock = asyncio.Lock()
    mgr._live = {}

    assert await mgr.write_input_sequence("gone", [b"abc", b"\r"]) == (0, 4)


@pytest.mark.asyncio
async def test_the_write_lock_is_held_across_the_whole_sequence():
    """A raw keystroke from the desktop terminal landing between the paste and
    its Enter would be swallowed into the paste."""
    pty = _RecordingPty()
    mgr, sid = _manager_with(pty)
    mgr.PROSE_CHUNK_GAP_SEC = 0.2  # widen the window this test inspects
    lock = mgr._live[sid].write_lock

    task = asyncio.create_task(mgr.write_input_sequence(sid, [b"a", b"\r"]))
    while len(pty.writes) < 1:  # first chunk is out, CR is not yet
        await asyncio.sleep(0.001)
    assert len(pty.writes) == 1
    assert lock.locked(), "lock released between the paste and its Enter"

    await task
    assert not lock.locked()


# ---- frame_prose_sequence: the registry lookup around the adapter ----

def _mgr_with_registry(registry) -> SessionManager:
    mgr = SessionManager.__new__(SessionManager)
    mgr._registry = registry
    return mgr


def test_frame_prose_sequence_uses_the_adapter():
    adapter = MagicMock()
    adapter.frame_pty_input_sequence.return_value = [b"paste", b"\r"]
    registry = MagicMock()
    registry.get.return_value = adapter

    assert _mgr_with_registry(registry).frame_prose_sequence("claude", "x") == [
        b"paste", b"\r",
    ]


@pytest.mark.parametrize(
    "broken",
    [Exception("boom"), "not-a-list", [], [b"ok", "not-bytes"]],
    ids=["raises", "wrong-type", "empty", "mixed-types"],
)
def test_frame_prose_sequence_falls_back_to_crlf(broken):
    """The fallback is the historical behaviour, so a lookup or contract
    failure can only leave a session exactly as broken as it was before —
    never worse, and never writing garbage bytes to a live PTY."""
    adapter = MagicMock()
    if isinstance(broken, Exception):
        adapter.frame_pty_input_sequence.side_effect = broken
    else:
        adapter.frame_pty_input_sequence.return_value = broken
    registry = MagicMock()
    registry.get.return_value = adapter

    assert _mgr_with_registry(registry).frame_prose_sequence("x", "hi") == [b"hi\r\n"]


def test_frame_prose_stays_the_flattened_view():
    adapter = MagicMock()
    adapter.frame_pty_input_sequence.return_value = [b"\x1b[200~hi\x1b[201~", b"\r"]
    registry = MagicMock()
    registry.get.return_value = adapter

    assert _mgr_with_registry(registry).frame_prose("claude", "hi") == (
        b"\x1b[200~hi\x1b[201~\r"
    )
