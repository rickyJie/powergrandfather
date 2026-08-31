"""Integration test for JsonlFastTail — verifies 200ms polling + routing."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from csm.modules.agent.jsonl_fast_tail import (
    JsonlFastTail,
    conversation_jsonl_path,
)


def _write_line(p: Path, obj) -> None:
    with open(p, "a") as f:
        f.write(json.dumps(obj) + "\n")


def test_conversation_jsonl_path():
    """cwd → project dir name: every `/` becomes `-`, leading slash included."""
    p = conversation_jsonl_path(
        Path("/home/u/.claude/projects"), "/srv/repos/foo", "abc-123"
    )
    assert str(p) == "/home/u/.claude/projects/-srv-repos-foo/abc-123.jsonl"


def test_conversation_jsonl_path_strips_trailing_slash():
    p = conversation_jsonl_path(
        Path("/home/u/.claude/projects"), "/srv/repos/foo/", "xyz"
    )
    assert str(p) == "/home/u/.claude/projects/-srv-repos-foo/xyz.jsonl"


async def test_fast_tail_replays_history_then_streams(tmp_path: Path):
    f = tmp_path / "sess.jsonl"
    _write_line(f, {
        "type": "user",
        "timestamp": "t1",
        "message": {"role": "user", "content": [{"type": "text", "text": "first"}]},
    })
    _write_line(f, {
        "type": "assistant",
        "timestamp": "t2",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
    })

    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    tail = JsonlFastTail(path=f, on_event=on_event, poll_interval_sec=0.05)
    await tail.start(replay_from_start=True)
    await asyncio.sleep(0.15)
    assert len(events) == 2
    assert events[0]["text"] == "first"
    assert events[1]["text"] == "second"

    _write_line(f, {
        "type": "assistant",
        "timestamp": "t3",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "third"}]},
    })
    await asyncio.sleep(0.15)
    assert len(events) == 3
    assert events[2]["text"] == "third"

    await tail.stop()


async def test_fast_tail_skip_history_with_replay_false(tmp_path: Path):
    f = tmp_path / "sess.jsonl"
    _write_line(f, {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "old"}]},
    })

    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    tail = JsonlFastTail(path=f, on_event=on_event, poll_interval_sec=0.05)
    await tail.start(replay_from_start=False)
    await asyncio.sleep(0.15)
    assert events == []  # historical line not replayed

    _write_line(f, {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "new"}]},
    })
    await asyncio.sleep(0.15)
    assert len(events) == 1
    assert events[0]["text"] == "new"

    await tail.stop()


async def test_fast_tail_handles_missing_file(tmp_path: Path):
    f = tmp_path / "not_yet.jsonl"
    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    tail = JsonlFastTail(path=f, on_event=on_event, poll_interval_sec=0.05)
    await tail.start(replay_from_start=True)
    await asyncio.sleep(0.15)
    assert events == []

    _write_line(f, {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "appeared"}]},
    })
    await asyncio.sleep(0.15)
    assert len(events) == 1

    await tail.stop()


async def test_fast_tail_partial_line_not_consumed(tmp_path: Path):
    f = tmp_path / "sess.jsonl"
    # Write a complete line + a partial.
    with open(f, "w") as fh:
        fh.write(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"ok"}]}}\n'
        )
        fh.write('{"type":"user","message":{"role":"user","co')  # partial
        fh.flush()

    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    tail = JsonlFastTail(path=f, on_event=on_event, poll_interval_sec=0.05)
    await tail.start(replay_from_start=True)
    await asyncio.sleep(0.15)
    assert len(events) == 1
    assert events[0]["text"] == "ok"

    # Complete the partial.
    with open(f, "a") as fh:
        fh.write('ntent":[{"type":"text","text":"finished"}]}}\n')
    await asyncio.sleep(0.15)
    assert len(events) == 2
    assert events[1]["text"] == "finished"

    await tail.stop()
