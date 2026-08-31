"""AgentAlertRule subsystem — sandbox, evaluator, context builder."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime

import pytest
from csm.core.events import Event
from csm.models import AgentAlertRule, Base, RawTokenEvent, ToolInvocation
from csm.modules.token.agent_alert.context_builder import build_context
from csm.modules.token.agent_alert.evaluator import AgentAlertEvaluator
from csm.modules.token.agent_alert.sandbox import run_check_script
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()
    os.unlink(path)


# ---------- sandbox ----------


async def test_sandbox_positive():
    script = "def check(w):\n    return (w['n'] >= 10, {'metric':'n','actual':w['n'],'threshold':10})\n"
    r = await run_check_script(script=script, window={"n": 42})
    assert r.ok
    assert r.fired is True
    assert r.payload == {"metric": "n", "actual": 42, "threshold": 10}


async def test_sandbox_no_fire():
    script = "def check(w):\n    return (False, {})\n"
    r = await run_check_script(script=script, window={})
    assert r.ok
    assert r.fired is False


async def test_sandbox_timeout():
    script = "def check(w):\n    import time; time.sleep(10)\n    return (True, {})\n"
    r = await run_check_script(script=script, window={}, timeout_sec=0.5)
    assert not r.ok
    assert "timed out" in (r.error or "")


async def test_sandbox_contract_violation_bad_return_shape():
    script = "def check(w):\n    return 'nope'\n"
    r = await run_check_script(script=script, window={})
    assert not r.ok
    assert "check()" in (r.error or "")


async def test_sandbox_no_check_defined():
    script = "def other(w):\n    return (True, {})\n"
    r = await run_check_script(script=script, window={})
    assert not r.ok
    assert "callable" in (r.error or "")


async def test_sandbox_syntax_error():
    script = "def check(w:\n    return True\n"
    r = await run_check_script(script=script, window={})
    assert not r.ok
    assert "compile" in (r.error or "").lower() or "never closed" in (r.error or "")


async def test_sandbox_payload_not_json_serializable():
    script = "def check(w):\n    class X: pass\n    return (True, {'x': X()})\n"
    r = await run_check_script(script=script, window={})
    assert not r.ok
    assert "serializ" in (r.error or "").lower() or "JSON" in (r.error or "")


# ---------- evaluator ----------


class FakeES:
    def __init__(self):
        self.emitted: list[Event] = []

    async def emit(self, event: Event):
        self.emitted.append(event)


async def _insert_events(sm, count: int):
    """Seed raw_token_event rows so current_window() returns non-trivial numbers."""
    now = datetime.utcnow()
    async with sm() as s:
        for i in range(count):
            s.add(RawTokenEvent(
                id=f"evt-{i}",
                ts=now,
                external_session_id=f"sess-{i % 3}",
                project_path="/tmp/p",
                model="claude-opus-4-7",
                source="interactive",
                input_tokens=100,
                cache_creation_tokens=50,
                cache_read_tokens=200,
                output_tokens=30,
                estimated_cost_usd=0.01,
            ))
        await s.commit()


async def test_evaluator_fires_and_persists(db):
    await _insert_events(db, 5)
    async with db() as s:
        r = AgentAlertRule(
            name="test-fire",
            nl_description="fire when msg_count >= 3",
            check_script="def check(w):\n    return (w['msg_count'] >= 3, {'metric':'msg_count','actual':w['msg_count'],'threshold':3})\n",
            poll_interval_sec=60,
            cooldown_sec=0,
            channels=["inapp"],
            escalate=False,
            enabled=True,
        )
        s.add(r); await s.commit(); rid = r.id

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es)
    await ev._tick_rule(rid)

    assert len(es.emitted) == 1
    from csm.core.events import EventType
    assert es.emitted[0].type == EventType.TOKEN_ALERT_TRIGGERED
    p = es.emitted[0].payload
    assert p["alert_rule_id"] == rid
    assert p["actual"] == 5
    assert p["threshold"] == 3
    assert p["channels"] == ["inapp"]
    assert p["escalate"] is False
    assert "agent_summary" not in p

    async with db() as s:
        row = await s.get(AgentAlertRule, rid)
        assert row.last_fired_at is not None
        assert row.last_error is None


async def test_evaluator_cooldown_blocks_refire(db):
    await _insert_events(db, 1)
    async with db() as s:
        r = AgentAlertRule(
            name="test-cd",
            nl_description="always",
            check_script="def check(w):\n    return (True, {})\n",
            poll_interval_sec=60,
            cooldown_sec=3600,
            channels=["inapp"],
            enabled=True,
        )
        s.add(r); await s.commit(); rid = r.id

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es)
    await ev._tick_rule(rid)
    assert len(es.emitted) == 1
    # Same tick again should be suppressed by cooldown.
    await ev._tick_rule(rid)
    assert len(es.emitted) == 1


async def test_evaluator_records_script_error(db):
    async with db() as s:
        r = AgentAlertRule(
            name="test-broken",
            nl_description="broken",
            check_script="def check(w:\n    return True\n",   # SyntaxError
            poll_interval_sec=60,
            cooldown_sec=0,
            channels=["inapp"],
            enabled=True,
        )
        s.add(r); await s.commit(); rid = r.id

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es)
    await ev._tick_rule(rid)
    assert len(es.emitted) == 0

    async with db() as s:
        row = await s.get(AgentAlertRule, rid)
        assert row.last_error is not None
        assert row.last_fired_at is None


async def test_evaluator_escalation_callback_wired(db):
    await _insert_events(db, 1)
    async with db() as s:
        r = AgentAlertRule(
            name="test-esc",
            nl_description="escalated",
            check_script="def check(w):\n    return (True, {'metric':'x','actual':1,'threshold':0})\n",
            poll_interval_sec=60,
            cooldown_sec=0,
            channels=["inapp", "lark"],
            escalate=True,
            enabled=True,
            lark_chat_id="oc_test",
        )
        s.add(r); await s.commit(); rid = r.id

    async def fake_escalate(rule, window, script_payload):
        return {"title": "root cause", "body": "recs here", "duration_sec": 0.1}

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es, escalation_callback=fake_escalate)
    await ev._tick_rule(rid)

    assert len(es.emitted) == 1
    p = es.emitted[0].payload
    assert p["agent_summary"]["title"] == "root cause"
    assert p["agent_summary"]["body"] == "recs here"
    assert p["channels"] == ["inapp", "lark"]
    assert p["lark_chat_id"] == "oc_test"


async def test_evaluator_escalation_failure_degrades(db):
    await _insert_events(db, 1)
    async with db() as s:
        r = AgentAlertRule(
            name="test-esc-fail",
            nl_description="e",
            check_script="def check(w):\n    return (True, {})\n",
            poll_interval_sec=60,
            cooldown_sec=0,
            channels=["inapp"],
            escalate=True,
            enabled=True,
        )
        s.add(r); await s.commit(); rid = r.id

    async def bad_escalate(rule, window, sp):
        raise RuntimeError("agent unavailable")

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es, escalation_callback=bad_escalate)
    await ev._tick_rule(rid)
    # Still fires — escalation failure is degraded to plain notification.
    assert len(es.emitted) == 1
    assert "agent_summary" not in es.emitted[0].payload


# -------- P0 regression tests (backend + qa review 2026-07-10) --------


async def test_sandbox_protocol_isolates_script_and_window():
    """P0-1: window content and script content share no delimiter.

    Before the fix, the shim's stdin was `<script>\\0<window_json>` — any
    ambiguity between the two channels could corrupt one or both. Now the
    script is stdin-only (raw bytes) and the window is a base64 argv arg.
    This test proves the two are fully independent by feeding both a
    reasonably tricky window (nulls, unicode, long strings) and a script
    that would have been unusual under the old delimiter protocol.
    """
    script = (
        "def check(w):\n"
        "    # docstring-like content that could confuse a naive parser\n"
        "    marker = w.get('marker', '')\n"
        "    return (marker == 'ok', {'marker_len': len(marker), 'total_keys': len(w)})\n"
    )
    window = {
        "marker": "ok",
        # These were the classes of content that could be misparsed under
        # the old NUL-delimited stdin blob.
        "with_null_char": "before\x00after",
        "with_unicode": "café • 中文 • 🎉",
        "big": "x" * 5000,
    }
    r = await run_check_script(script=script, window=window)
    assert r.ok, f"protocol failed: {r.error}"
    assert r.fired is True
    assert r.payload["marker_len"] == 2


async def test_sandbox_rejects_nul_in_script_cleanly():
    """A script containing a raw NUL byte should fail with a clear compile
    error (Python's compile() forbids NUL), not silently corrupt the
    protocol. Regression against the old NUL-delimiter design where a NUL
    in the script would have prematurely terminated the script part.
    """
    r = await run_check_script(
        script="def check(w):\n    x = 1\x00\n    return (True, {})\n",
        window={"n": 1},
    )
    assert not r.ok
    assert r.error is not None
    assert "null" in r.error.lower() or "nul" in r.error.lower()


async def test_sandbox_window_with_nan_returns_error():
    """P0-4: NaN in window must be caught before subprocess spawn, not raise."""
    r = await run_check_script(
        script="def check(w):\n    return (True, {})\n",
        window={"metric": float("nan")},
    )
    assert not r.ok
    assert r.error is not None
    assert "NaN" in r.error or "non-JSON-safe" in r.error


async def test_sandbox_window_with_inf_returns_error():
    r = await run_check_script(
        script="def check(w):\n    return (True, {})\n",
        window={"metric": float("inf")},
    )
    assert not r.ok
    assert r.error is not None


async def test_evaluator_concurrent_ticks_serialized(db):
    """P0-2: two ticks fired back-to-back on the same rule must not double-fire.

    Simulate the race: rule with cooldown_sec > 0, first tick starts a slow
    escalation while a second tick tries to run. The lock must serialize
    them, and the second tick — arriving *after* the first has marked
    last_fired_at — must be blocked by cooldown.
    """
    await _insert_events(db, 1)
    async with db() as s:
        r = AgentAlertRule(
            name="test-concurrent",
            nl_description="fire",
            check_script="def check(w):\n    return (True, {'metric':'x','actual':1,'threshold':0})\n",
            poll_interval_sec=60,
            cooldown_sec=300,   # non-zero so re-fire needs to pass the gate
            channels=["inapp"],
            escalate=True,
            enabled=True,
        )
        s.add(r); await s.commit(); rid = r.id

    escalation_hits = 0

    async def slow_escalate(rule, window, sp):
        nonlocal escalation_hits
        escalation_hits += 1
        # Simulate a slow claude -p — long enough that the second tick
        # would land inside this window if the lock didn't hold.
        await asyncio.sleep(0.5)
        return {"title": "root", "body": "recs"}

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es, escalation_callback=slow_escalate)

    # Kick off both ticks concurrently.
    await asyncio.gather(ev._tick_rule(rid), ev._tick_rule(rid))

    # Exactly one fire, one escalation call.
    assert len(es.emitted) == 1, f"expected 1 emit, got {len(es.emitted)}"
    assert escalation_hits == 1, f"expected 1 escalation call, got {escalation_hits}"


async def test_evaluator_reload_respects_interval_change(db):
    """P0-3: changing poll_interval_sec via API must cancel + respawn immediately."""
    async with db() as s:
        r = AgentAlertRule(
            name="test-interval",
            nl_description="x",
            check_script="def check(w):\n    return (False, {})\n",
            poll_interval_sec=60,
            cooldown_sec=0,
            channels=["inapp"],
            enabled=True,
        )
        s.add(r); await s.commit(); rid = r.id

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es)
    await ev.start()
    assert rid in ev._tasks
    task_1, interval_1 = ev._tasks[rid]
    assert interval_1 == 60

    # Change the interval in DB — the running task should be replaced.
    async with db() as s:
        row = await s.get(AgentAlertRule, rid)
        row.poll_interval_sec = 1800
        await s.commit()

    await ev.reload()
    task_2, interval_2 = ev._tasks[rid]
    assert interval_2 == 1800
    assert task_2 is not task_1, "task should have been respawned"
    # Give the event loop a beat to actually run the cancellation callback.
    try:
        await task_1
    except (asyncio.CancelledError, Exception):
        pass
    assert task_1.done(), "old task must be done after cancel"
    await ev.stop()


async def test_evaluator_reload_reconciles(db):
    async with db() as s:
        for i in range(3):
            s.add(AgentAlertRule(
                name=f"rule-{i}",
                nl_description="x",
                check_script="def check(w):\n    return (False, {})\n",
                poll_interval_sec=60,
                cooldown_sec=0,
                channels=["inapp"],
                enabled=True,
            ))
        await s.commit()

    es = FakeES()
    ev = AgentAlertEvaluator(sessionmaker=db, event_stream=es)
    await ev.start()
    assert len(ev._tasks) == 3

    # Disable one → reload drops that task.
    async with db() as s:
        rows = (await s.execute(select(AgentAlertRule))).scalars().all()
        rows[0].enabled = False
        await s.commit()

    await ev.reload()
    assert len(ev._tasks) == 2
    await ev.stop()


# ---------- context builder ----------


async def test_context_builder_top_sessions_and_curve(db):
    now = datetime.utcnow()
    async with db() as s:
        for i in range(10):
            s.add(RawTokenEvent(
                id=f"e{i}",
                ts=now,
                external_session_id="big-session" if i < 8 else "small-session",
                project_path="/data/foo",
                model="claude-opus-4-7" if i < 8 else "claude-haiku-4-5",
                source="interactive",
                input_tokens=1000 if i < 8 else 10,
                cache_creation_tokens=500,
                cache_read_tokens=2000,
                output_tokens=100,
                estimated_cost_usd=0.1,
            ))
        s.add(ToolInvocation(
            id="ti1",
            ts=now,
            external_session_id="big-session",
            project_path="/data/foo",
            source="interactive",
            tool_name="Bash",
            input_tokens=100, output_tokens=10,
            cache_creation_tokens=0, cache_read_tokens=0,
        ))
        await s.commit()

    snapshot = {
        "msg_count": 10,
        "input_tokens": 8010,
        "cache_creation_tokens": 5000,
        "cache_read_tokens": 20000,
        "output_tokens": 900,
        "total_tokens": 33910,
        "estimated_cost_usd": 1.0,
        "start_utc": "2026-07-10T00:00:00",
        "end_utc": "2026-07-10T05:00:00",
    }
    ctx = await build_context(db, window_snapshot=snapshot)
    assert len(ctx.top_sessions) >= 1
    # big-session should be first
    assert ctx.top_sessions[0].external_session_id == "big-session"
    assert ctx.top_sessions[0].total_tokens > ctx.top_sessions[-1].total_tokens
    assert any(m["model"] == "claude-opus-4-7" for m in ctx.model_split)
    # Cache hit ratio in this synthetic snapshot = 20000 / (20000+8010+900)
    assert 0.0 < ctx.cache_hit_ratio < 1.0
    md = ctx.to_markdown()
    assert "big-session"[:8] in md
    assert "Model split" in md
