"""Unit tests for LarkSink — the DB-backed variant.

Fixture strategy: use an aiosqlite in-memory engine (URL
`sqlite+aiosqlite:///:memory:`) and create just the `lark_settings`
table via `LarkSettings.__table__.create(...)`. We deliberately do NOT
run the full Alembic migration here — the goal is to exercise
`LarkSink` in isolation, and running the whole migration chain against
in-memory SQLite is slow and requires a config override that's easy to
get wrong (see f-16 QA feedback on assumption-of-fixture-isolation).
"""
from __future__ import annotations

import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from csm.adapters.lark_sink import LarkSink
from csm.models.lark_settings import LarkSettings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# ---- fixtures ----
@pytest.fixture
async def lark_db_sm():
    """Return an async_sessionmaker bound to a fresh in-memory DB with
    just the lark_settings table created. No seed row inserted — each
    test seeds what it needs (or leaves empty to test the "no row" path)."""
    # Use StaticPool so all connections see the same in-memory DB.
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(LarkSettings.__table__.create)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _seed(sm, **overrides):
    defaults = dict(
        id=1,
        enabled=True,
        chat_id="oc_test",
        user_id=None,
        dedup_window_sec=60,
        dnd_hours=[],
        tz=None,
        enabled_types={
            "session_crashed": True,
            "auto_run_failed": True,
            "token_warning": True,
            "port_conflict": True,
        },
    )
    defaults.update(overrides)
    async with sm() as db:
        row = await db.get(LarkSettings, 1)
        if row is None:
            db.add(LarkSettings(**defaults))
        else:
            for k, v in defaults.items():
                if k != "id":
                    setattr(row, k, v)
        await db.commit()


def _notif(notif_type: str, session_id: str | None = "s1") -> dict:
    return {
        "id": "n1",
        "type": notif_type,
        "session_id": session_id,
        "title": "T",
        "body": "B",
        "created_at": "2026-06-21T13:00:00+00:00",
        "metadata": {},
    }


# ---- no-op cases (config missing / disabled / no target) ----
@pytest.mark.asyncio
async def test_no_row_send_returns_false(lark_db_sm):
    # No seed → id=1 row doesn't exist → sink no-op
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed")) is False
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_row_send_returns_false(lark_db_sm):
    await _seed(lark_db_sm, enabled=False)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed")) is False
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_no_target_send_returns_false(lark_db_sm):
    await _seed(lark_db_sm, chat_id=None, user_id=None)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed")) is False
        mock.assert_not_called()


# ---- type filter (conservative default) ----
@pytest.mark.asyncio
async def test_enabled_types_missing_key_defaults_false(lark_db_sm):
    """Conservative: a NotificationType absent from enabled_types → skip."""
    await _seed(lark_db_sm, enabled_types={"session_crashed": True})  # only 1
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed")) is True
        assert await sink.send(_notif("token_warning")) is False  # missing key
        assert await sink.send(_notif("port_conflict")) is False  # missing key
        assert mock.call_count == 1


@pytest.mark.asyncio
async def test_enabled_types_explicit_false_skips(lark_db_sm):
    await _seed(lark_db_sm, enabled_types={"session_crashed": False, "token_warning": True})
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed")) is False
        assert await sink.send(_notif("token_warning")) is True
        assert mock.call_count == 1


# ---- dedup ----
@pytest.mark.asyncio
async def test_dedup_uses_db_window(lark_db_sm):
    await _seed(lark_db_sm, dedup_window_sec=60)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed", "sX")) is True
        assert await sink.send(_notif("session_crashed", "sX")) is False  # deduped
        assert await sink.send(_notif("session_crashed", "sY")) is True
        assert mock.call_count == 2


@pytest.mark.asyncio
async def test_dedup_window_change_takes_effect_after_flush(lark_db_sm):
    """Changing dedup_window_sec in DB doesn't clear the in-mem cache;
    the API PUT handler must call flush_dedup_cache(). Simulate that."""
    await _seed(lark_db_sm, dedup_window_sec=60)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send"):
        assert await sink.send(_notif("session_crashed", "sX")) is True
        # New config in DB, same key still cached in memory
        await _seed(lark_db_sm, dedup_window_sec=1)
        assert await sink.send(_notif("session_crashed", "sX")) is False  # still cached
        # Simulate PUT handler flush
        cleared = sink.flush_dedup_cache()
        assert cleared == 1
        assert await sink.send(_notif("session_crashed", "sX")) is True  # released


@pytest.mark.asyncio
async def test_bypass_dedup_metadata(lark_db_sm):
    await _seed(lark_db_sm)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send"):
        n = _notif("token_warning", "sX")
        n["metadata"] = {"_bypass_dedup": True}
        assert await sink.send(n) is True
        assert await sink.send(n) is True  # bypass wins


@pytest.mark.asyncio
async def test_dedup_key_in_metadata_used(lark_db_sm):
    await _seed(lark_db_sm)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        n1 = _notif("token_warning", "sX"); n1["metadata"] = {"_dedup_key": "alert:r1"}
        n2 = _notif("token_warning", "sX"); n2["metadata"] = {"_dedup_key": "alert:r2"}
        assert await sink.send(n1) is True
        assert await sink.send(n2) is True  # different key → not deduped
        assert mock.call_count == 2


# ---- DnD ----
@pytest.mark.asyncio
async def test_dnd_blocks_send(lark_db_sm, monkeypatch):
    await _seed(lark_db_sm, dnd_hours=[23, 0, 1, 2])
    sink = LarkSink(sessionmaker=lark_db_sm)

    import csm.adapters.lark_sink as sink_mod

    class FakeDT(sink_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 21, 1, 30)  # 01:30 - inside DnD

    monkeypatch.setattr(sink_mod, "datetime", FakeDT)
    with patch.object(sink, "_shell_send") as mock:
        assert await sink.send(_notif("session_crashed", "sZ")) is False
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_bypass_dnd_independent_of_bypass_dedup(lark_db_sm, monkeypatch):
    """v2 split _bypass_dedup / _bypass_dnd. Verify each works alone."""
    await _seed(lark_db_sm, dnd_hours=[1])
    sink = LarkSink(sessionmaker=lark_db_sm)

    import csm.adapters.lark_sink as sink_mod

    class FakeDT(sink_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 21, 1, 30)

    monkeypatch.setattr(sink_mod, "datetime", FakeDT)

    with patch.object(sink, "_shell_send"):
        # _bypass_dedup alone doesn't bypass DnD.
        n1 = _notif("session_crashed", "s1"); n1["metadata"] = {"_bypass_dedup": True}
        assert await sink.send(n1) is False
        # _bypass_dnd bypasses DnD.
        n2 = _notif("session_crashed", "s2"); n2["metadata"] = {"_bypass_dnd": True}
        assert await sink.send(n2) is True


# ---- tz robustness ----
@pytest.mark.asyncio
async def test_invalid_tz_falls_back_to_local(lark_db_sm):
    """Bad tz in DB doesn't kill the push path — logs warn, uses local."""
    await _seed(lark_db_sm, tz="Not/A/Real/Zone")
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send"):
        # Should not raise despite bad tz.
        result = await sink.send(_notif("session_crashed"))
        assert result is True


@pytest.mark.asyncio
async def test_dnd_hours_string_element_defensively_coerced(lark_db_sm):
    """If someone hand-edits DB and stores dnd_hours as ["1","2"] (strs),
    int() defense keeps DnD working instead of silently failing."""
    await _seed(lark_db_sm)
    async with lark_db_sm() as db:
        row = await db.get(LarkSettings, 1)
        # Bypass the ORM type coercion by writing raw JSON-ish list.
        row.dnd_hours = ["1", "2"]
        await db.commit()

    sink = LarkSink(sessionmaker=lark_db_sm)
    cfg = await sink._load_config()
    assert cfg is not None
    assert 1 in cfg.dnd_hours
    assert 2 in cfg.dnd_hours


# ---- per-event override ----
@pytest.mark.asyncio
async def test_per_event_chat_id_override(lark_db_sm):
    await _seed(lark_db_sm, chat_id="oc_default")
    sink = LarkSink(sessionmaker=lark_db_sm)
    captured: dict = {}

    async def fake(text, chat_id=None, user_id=None, **_):
        captured["chat_id"] = chat_id

    with patch.object(sink, "_shell_send", side_effect=fake):
        n = _notif("token_warning", "sX")
        n["metadata"] = {"lark_chat_id": "oc_override"}
        assert await sink.send(n) is True
    assert captured["chat_id"] == "oc_override"


@pytest.mark.asyncio
async def test_skip_lark_metadata(lark_db_sm):
    await _seed(lark_db_sm)
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        n = _notif("session_crashed", "sX")
        n["metadata"] = {"_skip_lark": True}
        assert await sink.send(n) is False
        mock.assert_not_called()


# ---- send_test / _force_type_pass gate ----
@pytest.mark.asyncio
async def test_send_test_shortcircuits_all_gates(lark_db_sm, monkeypatch):
    """test push must succeed even when the caller's config would block
    a normal push (DnD active, dedup_window active, no enabled_types)."""
    await _seed(lark_db_sm, enabled_types={}, dnd_hours=list(range(24)))
    sink = LarkSink(sessionmaker=lark_db_sm)

    import csm.adapters.lark_sink as sink_mod

    class FakeDT(sink_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 21, 3, 0)  # always DnD

    monkeypatch.setattr(sink_mod, "datetime", FakeDT)

    with patch.object(sink, "_shell_send"):
        ok, err, dur = await sink.send_test("hello")
        assert ok is True
        assert err is None
        assert dur >= 0


@pytest.mark.asyncio
async def test_force_type_pass_ignored_for_non_test_notifications(lark_db_sm):
    """`_force_type_pass` is scoped to type=="test" so a copy-paste of
    test metadata can't accidentally push a type the user didn't enable."""
    await _seed(lark_db_sm, enabled_types={})  # NOTHING enabled
    sink = LarkSink(sessionmaker=lark_db_sm)
    with patch.object(sink, "_shell_send") as mock:
        n = _notif("session_crashed", "sX")
        n["metadata"] = {"_force_type_pass": True}
        assert await sink.send(n) is False
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_test_reports_shell_error(lark_db_sm):
    """Post-review: send_test calls _shell_send DIRECTLY (not through
    send()) so lark-cli stderr surfaces to the operator instead of
    being swallowed. A failure like 'chat_id not found' must land in
    the returned err string verbatim."""
    await _seed(lark_db_sm)
    sink = LarkSink(sessionmaker=lark_db_sm)

    async def boom(*a, **kw):
        raise RuntimeError("lark-cli exit 1: chat_id not found")

    with patch.object(sink, "_shell_send", side_effect=boom):
        ok, err, dur = await sink.send_test()
    assert ok is False
    assert err is not None
    assert "chat_id not found" in err


@pytest.mark.asyncio
async def test_send_test_reports_no_target_when_disabled(lark_db_sm):
    """Sink self-skip (disabled row) must surface a config-oriented
    error, not None — otherwise the UI can't tell the two failure
    modes apart."""
    await _seed(lark_db_sm, enabled=False)
    sink = LarkSink(sessionmaker=lark_db_sm)
    ok, err, dur = await sink.send_test()
    assert ok is False
    assert err is not None
    assert "disabled" in err.lower() or "chat_id" in err.lower()


# ---- transport: subprocess kill on timeout ----
@pytest.mark.asyncio
async def test_shell_send_kills_subprocess_on_timeout(lark_db_sm, monkeypatch):
    """A hung lark-cli must be killed when _shell_send hits its timeout
    — otherwise the outer wait_for cancellation from the API (8s)
    leaves an orphan process forever."""
    await _seed(lark_db_sm)
    sink = LarkSink(sessionmaker=lark_db_sm)

    kill_called = {"n": 0}
    reap_after_kill = {"done": False}

    class FakeProc:
        returncode = None

        def kill(self):
            kill_called["n"] += 1

        async def communicate(self):
            # First call blocks past the timeout; second (post-kill
            # reap) returns immediately.
            if kill_called["n"] == 0:
                import asyncio as _a
                await _a.sleep(30)
            else:
                reap_after_kill["done"] = True
            return (b"", b"")

    async def fake_exec(*a, **kw):
        return FakeProc()

    import csm.adapters.lark_sink as sink_mod
    monkeypatch.setattr(sink_mod.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="timed out"):
        # Use the injected-timeout param so the test runs in ~0.1s
        # rather than the production 10s default.
        await sink._shell_send("hi", chat_id="oc_a", timeout_sec=0.1)

    assert kill_called["n"] == 1, "kill() must be called on timeout"
    assert reap_after_kill["done"], "post-kill communicate() must be awaited"


# ---- cache pruning (long-lived process) ----
@pytest.mark.asyncio
async def test_dedup_ttl_prunes_stale_entries(lark_db_sm):
    await _seed(lark_db_sm, dedup_window_sec=60)
    sink = LarkSink(sessionmaker=lark_db_sm)
    now = time.time()
    sink._last_sent["stale_1"] = now - 500
    sink._last_sent["stale_2"] = now - 300
    sink._last_sent["fresh"] = now - 5
    removed = sink._prune_dedup(now, 60)
    assert removed == 2
    assert "fresh" in sink._last_sent


# ---- _format per-type golden layouts ----
#
# Each type has a distinctive shape so recipients can visually pattern-
# match the notification kind at a glance. These tests lock the layout
# down — if one changes, update the golden here so the intent stays
# documented alongside the change.

_ZH = ZoneInfo("Asia/Shanghai")


@pytest.mark.asyncio
async def test_format_new_message_layout(lark_db_sm, monkeypatch):
    """NEW_MESSAGE: header = session_title · @agent · N new; body is
    the salient block; footer = time + link."""
    from csm.config import settings as app_settings
    monkeypatch.setattr(app_settings, "public_base_url", "https://csm.example.com:8000")
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "new_message",
        "session_id": "abcdef1234567890",
        "title": "3 new messages",
        "body": "Sure — I'll refactor the auth layer first. Let me start…",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "sample exp Y", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    expected = (
        "【PowerGrandFather】💬 sample exp Y · @claude · 3 new\n"
        "\n"
        "Sure — I'll refactor the auth layer first. Let me start…\n"
        "\n"
        "🕐 2026-08-02 14:23 CST  🔗 https://csm.example.com:8000/sessions/abcdef1234567890"
    )
    assert out == expected


@pytest.mark.asyncio
async def test_format_new_message_singular_omits_count_suffix(lark_db_sm):
    """N=1 case: no ` · 1 new` tail — reads cleaner."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "new_message",
        "session_id": "s1",
        "title": "1 new message",
        "body": "hi",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "sync优化", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    assert "💬 sync优化 · @claude\n" in out
    assert " · 1 new" not in out


@pytest.mark.asyncio
async def test_format_auto_needs_review_layout(lark_db_sm, monkeypatch):
    """AUTO_NEEDS_REVIEW: header = fixed label; body prefixed with
    `Verdict:` so the rationale stands out."""
    from csm.config import settings as app_settings
    monkeypatch.setattr(app_settings, "public_base_url", "https://csm.example.com:8000")
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "auto_needs_review",
        "session_id": "sid-xyz",
        "title": "Needs review: refactor-M4",
        "body": "[stage_3] output diverges from spec",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "refactor-M4", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    expected = (
        "【PowerGrandFather】🔍 Needs a human\n"
        "\n"
        "📍 refactor-M4 · @claude\n"
        "Verdict: [stage_3] output diverges from spec\n"
        "\n"
        "🕐 2026-08-02 14:23 CST  🔗 https://csm.example.com:8000/sessions/sid-xyz"
    )
    assert out == expected


@pytest.mark.asyncio
async def test_format_auto_needs_review_permission_variant(lark_db_sm):
    """The H5 waiting-auth title lands under the same type; header
    swaps to 'Waiting for permission' so the recipient knows it's a permission
    prompt, not a supervisor verdict."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "auto_needs_review",
        "session_id": "sid-1",
        "title": "Permission required",
        "body": "Claude is waiting for your approval to use a tool.",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "sample exp", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    assert "🔍 Waiting for permission" in out


@pytest.mark.asyncio
async def test_format_session_crashed_hoists_exit_code(lark_db_sm, monkeypatch):
    """SESSION_CRASHED: body=exit_code=N gets reformatted to
    'exit N (signal-hint)'. 137 → SIGKILL/OOM hint."""
    from csm.config import settings as app_settings
    monkeypatch.setattr(app_settings, "public_base_url", "https://csm.example.com:8000")
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "session_crashed",
        "session_id": "d7b44c52",
        "title": "Session crashed",
        "body": "exit_code=137",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "openpi学习", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    expected = (
        "【PowerGrandFather】🚨 Session crashed\n"
        "\n"
        "📍 openpi学习 · @claude\n"
        "exit 137 (SIGKILL (possibly OOM))\n"
        "\n"
        "🕐 2026-08-02 14:23 CST  🔗 https://csm.example.com:8000/sessions/d7b44c52"
    )
    assert out == expected


@pytest.mark.asyncio
async def test_format_auto_run_failed_layout(lark_db_sm, monkeypatch):
    """AUTO_RUN_FAILED shares the crash body-reformat but with the
    ⚠️ icon + Chinese label — visually distinct from a real crash."""
    from csm.config import settings as app_settings
    monkeypatch.setattr(app_settings, "public_base_url", "https://csm.example.com:8000")
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "auto_run_failed",
        "session_id": "auto-1",
        "title": "Automation run failed: sync-workflow",
        "body": "exit_code=1",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "sync-workflow", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    assert "⚠️ Automation failed" in out
    assert "exit 1" in out
    assert "📍 sync-workflow · @claude" in out


@pytest.mark.asyncio
async def test_format_token_warning_layout(lark_db_sm):
    """TOKEN_WARNING: title is already info-rich (may carry its own
    emoji marker) — the formatter strips the leading emoji to avoid a
    double-icon render and shows title as a distinct line above body."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "token_warning",
        "session_id": None,
        "title": "🚨 Budget BREACHED: ops (105%)",
        "body": "4,200,000 tokens · $8.40 · scope=global · period=daily",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {},
    }
    out = sink._format(n, tz=_ZH)
    lines = out.split("\n")
    assert lines[0] == "【PowerGrandFather】📊 Token alert"
    # Leading emoji stripped from the original title:
    assert "Budget BREACHED: ops (105%)" in out
    assert "🚨 Budget BREACHED" not in out  # not double-emoji
    assert "4,200,000 tokens" in out
    # No session context: metadata is empty
    assert "📍" not in out


@pytest.mark.asyncio
async def test_format_port_conflict_layout(lark_db_sm):
    """PORT_CONFLICT: port number hoisted into header; no deep link
    (no port-detail page)."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "port_conflict",
        "session_id": None,
        "title": "Port conflict on :8000",
        "body": "new pid=1234 cmd=uvicorn",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"port": 8000},
    }
    out = sink._format(n, tz=_ZH)
    assert "🔌 Port conflict :8000" in out
    assert "new pid=1234 cmd=uvicorn" in out
    assert "🔗" not in out  # no link for port events


@pytest.mark.asyncio
async def test_format_mission_done_success_layout(lark_db_sm):
    """MISSION_DONE succeeded → ✅ badge; failed → ❌ badge. Header
    icon overrides the fixed type icon so the outcome reads at a glance."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "mission_done",
        "session_id": None,
        "title": "✅ Mission succeeded: sdlc-migrate",
        "body": "mission abc123",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"status": "succeeded", "workflow_name": "sdlc-migrate", "mission_id": "abc12345678"},
    }
    out = sink._format(n, tz=_ZH)
    assert out.startswith("【PowerGrandFather】✅ Mission succeeded")
    assert "workflow: sdlc-migrate" in out
    assert "mission #abc12345" in out


@pytest.mark.asyncio
async def test_format_mission_done_failure_layout(lark_db_sm):
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "mission_done",
        "session_id": None,
        "title": "❌ Mission failed: sdlc-migrate",
        "body": "mission abc · reason",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {
            "status": "failed",
            "workflow_name": "sdlc-migrate",
            "mission_id": "abc12345",
            "failure_reason": "stage 3 validation failed",
        },
    }
    out = sink._format(n, tz=_ZH)
    assert out.startswith("【PowerGrandFather】❌ Mission failed")
    assert "Reason: stage 3 validation failed" in out


# ---- cross-cutting: tz, markdown strip, base-url fallback ----
@pytest.mark.asyncio
async def test_format_localizes_time_to_configured_tz(lark_db_sm):
    """UTC input converts to configured tz with proper abbreviation."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "new_message", "session_id": "s1",
        "title": "1 new message", "body": "hi",
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "t", "agent": "claude"},
    }
    out_zh = sink._format(n, tz=_ZH)
    assert "🕐 2026-08-02 14:23 CST" in out_zh
    # UTC fallback when tz unset
    out_utc = sink._format(n, tz=None)
    assert "🕐 2026-08-02 06:23 UTC" in out_utc


@pytest.mark.asyncio
async def test_format_strips_markdown_noise_in_new_message_body(lark_db_sm):
    """Bold/heading/code markers should not leak into the Lark plain-text
    render — they show as literal `**` / `##` and clutter the preview."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    body = (
        "我在 PM 建议和你需求之间取中间方案。**PM 完全砍到 2 天只做 S1，但你明说要 S2 双活**，"
        "所以我把 S2 的**最小实现**加回来。\n"
        "## 要做的\n"
        "### F1. Bootstrap import\n"
        "- `POST /api/x`"
    )
    n = {
        "type": "new_message", "session_id": "s1",
        "title": "1 new message", "body": body,
        "created_at": "2026-08-02T06:23:00+00:00",
        "metadata": {"session_title": "t", "agent": "claude"},
    }
    out = sink._format(n, tz=_ZH)
    assert "**" not in out
    assert "## " not in out
    assert "### " not in out
    # Inline backticks stripped, text preserved
    assert "POST /api/x" in out
    # Bold text content preserved (just markers removed)
    assert "PM 完全砍到 2 天" in out


@pytest.mark.asyncio
async def test_format_deep_link_falls_back_to_localhost_when_no_base_url(lark_db_sm, monkeypatch):
    """Empty public_base_url → localhost + configured port fallback so
    the message still carries a clickable URL for the local user."""
    from csm.config import settings as app_settings
    monkeypatch.setattr(app_settings, "public_base_url", "")
    monkeypatch.setattr(app_settings, "port", 8000)
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = _notif("new_message", "s1")
    n["metadata"] = {"session_title": "t", "agent": "claude"}
    out = sink._format(n, tz=_ZH)
    assert "🔗 https://localhost:8000/sessions/s1" in out


@pytest.mark.asyncio
async def test_format_default_handles_test_ping(lark_db_sm):
    """send_test uses type='test' → default formatter; still produces a
    branded, timestamped payload."""
    sink = LarkSink(sessionmaker=lark_db_sm)
    n = {
        "type": "test", "title": "PowerGrandFather test ping",
        "body": "hello", "session_id": None,
        "created_at": "2026-08-02T06:23:00+00:00",
    }
    out = sink._format(n, tz=_ZH)
    assert out.startswith("【PowerGrandFather】🧪 PowerGrandFather test ping")
    assert "hello" in out
    assert "🕐 2026-08-02 14:23 CST" in out
