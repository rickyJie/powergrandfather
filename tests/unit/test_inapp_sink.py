"""InAppSink tests — WS fan-out, stuck-client timeout, dead-client eviction."""
from __future__ import annotations

import asyncio

import pytest
from csm.adapters.inapp_sink import _SEND_TIMEOUT_SEC, InAppSink


class _FakeWS:
    """Minimal WebSocket stub. `hang=True` makes `send_text` await
    forever so we can exercise the timeout branch."""

    def __init__(self, *, hang: bool = False, raise_on_send: bool = False):
        self.accepted = False
        self.received: list[str] = []
        self._hang = hang
        self._raise_on_send = raise_on_send

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, msg: str) -> None:
        if self._raise_on_send:
            raise ConnectionError("simulated broken pipe")
        if self._hang:
            # Wait on an event that never gets set. If the timeout
            # branch under test breaks and this call is not cancelled,
            # the test fails fast via pytest's step timeout instead
            # of pinning a CI worker for an hour.
            await asyncio.Event().wait()
        self.received.append(msg)


@pytest.mark.asyncio
async def test_send_fans_out_to_all_clients():
    sink = InAppSink()
    a, b = _FakeWS(), _FakeWS()
    await sink.attach(a)
    await sink.attach(b)
    reach = await sink.send({"id": "n1", "type": "session_crashed"})
    assert reach == 2
    assert a.received and b.received


@pytest.mark.asyncio
async def test_send_drops_client_that_raises():
    sink = InAppSink()
    good = _FakeWS()
    bad = _FakeWS(raise_on_send=True)
    await sink.attach(good)
    await sink.attach(bad)
    reach = await sink.send({"id": "n1"})
    assert reach == 1
    # Bad client evicted; next send doesn't retry it.
    reach2 = await sink.send({"id": "n2"})
    assert reach2 == 1
    assert sink.client_count == 1


@pytest.mark.asyncio
async def test_send_drops_client_that_times_out(monkeypatch, caplog):
    """A hung TCP client shouldn't block the fan-out or stay in the
    set forever. We shrink the send timeout so the test runs fast."""
    from csm.adapters import inapp_sink as mod
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.1)
    sink = InAppSink()
    hung = _FakeWS(hang=True)
    good = _FakeWS()
    await sink.attach(hung)
    await sink.attach(good)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with caplog.at_level("WARNING"):
        reach = await sink.send({"id": "n1"})
    elapsed = loop.time() - t0
    # The good client got the message; the hung one was dropped.
    assert reach == 1
    assert good.received
    assert hung not in sink._clients
    # Loop finished promptly — didn't wait 3600s for the hung client.
    assert elapsed < 1.0, f"fan-out blocked for {elapsed:.2f}s"
    assert any("send timeout" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_empty_client_set_returns_zero_without_serialising():
    """Fast-path: no clients means no JSON encode, no iteration."""
    sink = InAppSink()
    assert await sink.send({"id": "whatever"}) == 0


def test_send_timeout_constant_is_conservative():
    """Sanity: the default timeout should be short enough that a stuck
    client is dropped promptly, but not so short that healthy LAN
    clients are false-positived."""
    assert 0.5 <= _SEND_TIMEOUT_SEC <= 10.0
