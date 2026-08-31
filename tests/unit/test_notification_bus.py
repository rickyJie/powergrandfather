"""NotificationBus tests — feed mock events and verify notifications + unread bookkeeping."""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from csm.adapters.inapp_sink import InAppSink
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.core.notification_bus import NotificationBus
from csm.models import Base, Notification, Session
from csm.models.notification import NotificationType
from csm.models.session import SessionStatus, SessionType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def setup():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    es = EventStream(projects_root=Path(tempfile.gettempdir()), poll_interval_sec=10.0, watchdog_interval_sec=10.0)
    sink = InAppSink()
    bus = NotificationBus(sessionmaker=sm, event_stream=es, in_app_sink=sink, dedup_window_sec=5)
    await bus.start()
    yield bus, sm, es, sink
    await bus.stop()
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def test_session_crashed_emits_notification(setup):
    bus, sm, es, _ = setup
    await es.emit(Event(
        type=EventType.SESSION_CRASHED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={"csm_session_id": "csm-1", "exit_code": 7},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.SESSION_CRASHED
        assert rows[0].session_id == "csm-1"


async def test_auto_run_failed_when_auto_session_exits_nonzero(setup):
    bus, sm, es, _ = setup
    # Insert an AUTO session row first.
    async with sm() as db:
        sess = Session(id="auto-1", cwd="/tmp", type=SessionType.AUTO, status=SessionStatus.RUNNING, title="my-auto")
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.SESSION_ENDED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={"csm_session_id": "auto-1", "exit_code": 2},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.AUTO_RUN_FAILED
        assert "my-auto" in (rows[0].title or "")


async def test_no_notif_for_clean_auto_exit(setup):
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="auto-ok", cwd="/tmp", type=SessionType.AUTO, status=SessionStatus.RUNNING)
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.SESSION_ENDED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={"csm_session_id": "auto-ok", "exit_code": 0},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert rows == []


async def test_token_alert_triggered_translates(setup):
    bus, sm, es, _ = setup
    await es.emit(Event(
        type=EventType.TOKEN_ALERT_TRIGGERED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={"alert_name": "msgs>1000", "metric": "msg_count", "actual": 1200, "threshold": 1000},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.TOKEN_WARNING


async def test_port_conflict_detected_translates(setup):
    bus, sm, es, _ = setup
    await es.emit(Event(
        type=EventType.PORT_CONFLICT_DETECTED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={"port": 8080, "new_pid": 555, "new_cmd": "nginx"},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.PORT_CONFLICT
        assert "8080" in (rows[0].title or "")


async def test_budget_breached_translates(setup):
    bus, sm, es, _ = setup
    await es.emit(Event(
        type=EventType.BUDGET_BREACHED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={
            "budget_id": "b1",
            "budget_name": "monthly",
            "state": "breached",
            "effective_pct": 105,
            "current_tokens": 5000000,
            "current_cost_usd": 42.50,
            "scope_type": "global",
            "period": "monthly",
        },
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.TOKEN_WARNING
        assert "BREACHED" in (rows[0].title or "")


async def test_assistant_done_merges_notifications(setup):
    """Three assistant-done events within the merge window collapse into a
    single "N new messages" notification.

    Post-decouple (2026-07-10): `Session.unread_count` is intentionally NOT
    bumped anymore — the Sessions module no longer tracks unread state, the
    Notification table alone is authoritative. The merge counter lives in the
    NotificationBus's in-memory `_last_new_msg[sid]` tuple.

    We also disable the cross-source dedup (`_assistant_done_dedup_sec = 0`)
    which normally swallows the 2nd/3rd emit here: that dedup exists so the
    JSONL tail + hook-Stop paths don't double-count a SINGLE turn, but in
    the test all 3 events come through the same channel and each represents
    a distinct turn.
    """
    bus, sm, es, _ = setup
    bus._assistant_done_dedup_sec = 0  # disable cross-source dedup for this test
    async with sm() as db:
        sess = Session(id="csm-int-1", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-xyz",
                       unread_count=0)
        db.add(sess)
        await db.commit()

    for _ in range(3):
        await es.emit(Event(
            type=EventType.MESSAGE_ASSISTANT_DONE,
            ts=datetime.now(UTC),
            session_id="claude-xyz",
            project_path="/tmp",
            payload={},
        ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert "3 new messages" in rows[0].title
        # Post-decouple: Session.unread_count MUST stay at 0. The Sessions
        # module is not supposed to know about NEW_MESSAGE bumps.
        sess_row = await db.get(Session, "csm-int-1")
        assert sess_row.unread_count == 0


async def test_hook_and_jsonl_report_of_one_turn_collapse_to_one_notification(setup):
    """One turn seen by BOTH producers must raise exactly one notification.

    The Stop hook (api/hooks.py) fires the instant claude finishes; the JSONL
    tail reports the same turn up to one poll (5s) later. The old 2s window
    never spanned that gap, so every turn produced two NEW_MESSAGE rows — two
    OS notifications, two Lark pushes, doubled unread (~40 pairs/day observed).

    The JSONL text must also win: the hook payload has no `assistant_text` and
    falls back to `Session.last_assistant_msg`, which still holds the PREVIOUS
    turn's reply at that moment.
    """
    bus, sm, es, _ = setup
    async with sm() as db:
        db.add(Session(id="csm-dup", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-dup",
                       last_assistant_msg="reply from the PREVIOUS turn"))
        await db.commit()

    # 1) hook Stop — instant, carries no assistant text.
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-dup", project_path="/tmp",
        payload={"hook_event_name": "Stop", "csm_session_id": "csm-dup"},
    ))
    # 2) JSONL tail — same turn, one poll later, authoritative text.
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-dup", project_path="/tmp",
        payload={"model": "claude-opus", "assistant_text": "reply from THIS turn"},
    ))

    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert "1 new message" in rows[0].title
        assert "THIS turn" in (rows[0].body or "")

    # The pair is closed ("both"), so the next turn is NOT swallowed by the
    # same window — otherwise the suppression would chain and eat real
    # messages. (The 2s same-source guard is a separate concern here.)
    bus._assistant_done_dedup_sec = 0
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-dup", project_path="/tmp",
        payload={"hook_event_name": "Stop", "csm_session_id": "csm-dup"},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert "2 new messages" in rows[0].title


async def test_unread_by_session_is_exact_and_ignores_other_notification_types(setup):
    bus, sm, _es, _sink = setup
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sm() as db:
        db.add_all([
            Notification(type=NotificationType.NEW_MESSAGE, session_id="s1", title="1"),
            Notification(type=NotificationType.NEW_MESSAGE, session_id="s1", title="2"),
            Notification(type=NotificationType.NEW_MESSAGE, session_id="s2", title="3"),
            Notification(
                type=NotificationType.NEW_MESSAGE,
                session_id="s2",
                title="read",
                read_at=now,
            ),
            Notification(
                type=NotificationType.NEW_MESSAGE,
                session_id="s2",
                title="dismissed",
                dismissed_at=now,
            ),
            Notification(type=NotificationType.SESSION_CRASHED, session_id="s1", title="crash"),
        ])
        await db.commit()

    assert await bus.unread_by_session() == {"s1": 2, "s2": 1}


async def test_assistant_done_ignores_terminal_duplicate_external_id(setup):
    """Historical rows may reuse an external id; only its live owner is valid."""
    _, sm, es, _ = setup
    async with sm() as db:
        db.add_all([
            Session(
                id="old-row", cwd="/tmp", agent="codex",
                type=SessionType.INTERACTIVE, status=SessionStatus.EXITED,
                external_session_id="shared-codex",
            ),
            Session(
                id="live-row", cwd="/tmp", agent="codex",
                type=SessionType.INTERACTIVE, status=SessionStatus.RUNNING,
                external_session_id="shared-codex",
            ),
        ])
        await db.commit()

    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="shared-codex",
        project_path="/tmp",
        payload={"backend": "codex", "assistant_text": "finished"},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].session_id == "live-row"
        live = await db.get(Session, "live-row")
        assert live.last_assistant_msg == "finished"


async def test_codex_fallback_never_rebinds_claude_in_same_cwd(setup):
    """Missing Codex binding must not steal a newer Claude session."""
    _, sm, es, _ = setup
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sm() as db:
        db.add_all([
            Session(
                id="claude-live", cwd="/same", agent="claude",
                type=SessionType.INTERACTIVE, status=SessionStatus.RUNNING,
                external_session_id="claude-id", last_activity_ts=now,
            ),
            Session(
                id="codex-live", cwd="/same", agent="codex",
                type=SessionType.INTERACTIVE, status=SessionStatus.RUNNING,
                last_activity_ts=now - timedelta(seconds=10),
            ),
        ])
        await db.commit()

    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="codex-id",
        project_path="/same",
        payload={
            "backend": "codex",
            "assistant_text": "codex result",
            "rollout_path": "/rollout/codex.jsonl",
        },
    ))
    async with sm() as db:
        claude = await db.get(Session, "claude-live")
        codex = await db.get(Session, "codex-live")
        assert claude.external_session_id == "claude-id"
        assert codex.external_session_id == "codex-id"
        assert codex.rollout_path == "/rollout/codex.jsonl"


async def test_mark_session_read(setup):
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="csm-int-2", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-abc",
                       unread_count=0)
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-abc",
        project_path="/tmp",
        payload={},
    ))
    await bus.mark_session_read("csm-int-2")
    async with sm() as db:
        sess_row = await db.get(Session, "csm-int-2")
        assert sess_row.unread_count == 0
        rows = (await db.execute(select(Notification))).scalars().all()
        assert all(r.read_at is not None for r in rows)


async def test_mark_read_invalidates_dedup_so_next_assistant_done_opens_fresh_row(setup):
    """Regression for the "red-dot comes right back after clicking" bug.

    Sequence:
      1. assistant.done → NEW_MESSAGE #1 (unread)
      2. user clicks it in the panel → mark_read(#1)
      3. another assistant.done within the dedup window
         → MUST open a NEW row (#2), NOT re-open #1

    Before the fix, `mark_read` gated `_last_new_msg.pop` on the legacy
    `sess.unread_count > 0` (which is never true post-decouple), so the
    dedup pointer survived; the next assistant.done hit the merge branch
    and set `existing.read_at = None`, resurrecting the just-read row.
    """
    bus, sm, es, _ = setup
    bus._assistant_done_dedup_sec = 0  # disable cross-source dedup for this test
    async with sm() as db:
        sess = Session(id="csm-red-dot", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-red",
                       unread_count=0)
        db.add(sess)
        await db.commit()

    # 1) First assistant.done → creates NEW_MESSAGE #1
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-red",
        project_path="/tmp",
        payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        first_id = rows[0].id
        assert rows[0].read_at is None

    # 2) User clicks it → mark_read
    assert await bus.mark_read(first_id) is True
    async with sm() as db:
        r = await db.get(Notification, first_id)
        assert r.read_at is not None

    # 3) Another assistant.done inside the dedup window — expected: NEW row,
    #    the read row stays read.
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-red",
        project_path="/tmp",
        payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification).order_by(Notification.created_at))).scalars().all()
        # Two distinct rows now — the second event opened a fresh notification.
        assert len(rows) == 2
        # The first (originally-read) row is untouched.
        first = next(r for r in rows if r.id == first_id)
        assert first.read_at is not None
        # And the new row is unread.
        second = next(r for r in rows if r.id != first_id)
        assert second.read_at is None


async def test_new_message_body_carries_assistant_text_snippet(setup):
    """NEW_MESSAGE.body was `None` — the panel and Lark push had nothing
    to render below the title. Now the body carries a truncated snippet
    of assistant_text so the user sees WHAT the assistant said without
    opening the session first."""
    from csm.core.notification_bus import _snippet
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="csm-body-1", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-body",
                       title="body test", unread_count=0)
        db.add(sess)
        await db.commit()
    long_reply = (
        "Here is a multi-line reply from the assistant.\n"
        "It has whitespace     and   multiple\n\n"
        "paragraphs with a code snippet:\n"
        "```python\ndef f(): return 1\n```\n"
        "And a trailing sentence that hopefully lands inside the 180-char window."
    )
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-body",
        project_path="/tmp",
        payload={"assistant_text": long_reply},
    ))
    async with sm() as db:
        row = (await db.execute(select(Notification))).scalar_one()
        assert row.session_id == "csm-body-1"
        # Snippet preserves paragraph structure via `⏎` markers and is
        # bounded (allow the trailing ellipsis).
        assert row.body is not None and len(row.body) <= 181
        assert row.body.startswith("Here is a multi-line reply")
        assert "⏎" in row.body  # paragraph marker present
        # Direct call to the helper on the same input must match — the
        # bus does not add framing around the snippet.
        assert row.body == _snippet(long_reply)


async def test_new_message_body_falls_back_to_session_last_assistant_msg(setup):
    """When MESSAGE_ASSISTANT_DONE arrives without an assistant_text
    payload (tool-only turn or adapter that doesn't populate the field),
    the notification body should fall back to Session.last_assistant_msg
    so Lark push and the in-app panel still carry a preview instead of
    just "1 new message" with no content."""
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(
            id="csm-fb-1", cwd="/tmp", type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING, external_session_id="claude-fb",
            title="fb test", unread_count=0,
            last_assistant_msg="prior turn text carried over",
        )
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-fb",
        project_path="/tmp",
        payload={"assistant_text": None},  # simulate tool-only / adapter miss
    ))
    async with sm() as db:
        row = (await db.execute(select(Notification))).scalar_one()
        assert row.session_id == "csm-fb-1"
        assert row.body == "prior turn text carried over"


async def test_new_message_body_updates_to_latest_snippet_on_merge(setup):
    """Merging consecutive messages should refresh body to the *latest*
    reply so the panel reflects the newest content, not the first."""
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="csm-merge-1", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-merge",
                       title=None, unread_count=0)
        db.add(sess)
        await db.commit()
    bus._assistant_done_dedup_sec = 0  # disable cross-source dedup
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-merge",
        project_path="/tmp",
        payload={"assistant_text": "first reply — stale"},
    ))
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-merge",
        project_path="/tmp",
        payload={"assistant_text": "second reply — latest and greatest"},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1  # merged
        assert rows[0].title == "2 new messages"
        assert rows[0].body == "second reply — latest and greatest"


async def test_snippet_helper_shape():
    """Direct coverage of the _snippet helper — flatten whitespace, hard
    cap length, prefer word boundary near the cap for readability."""
    from csm.core.notification_bus import _snippet
    assert _snippet("") == ""
    # Single-paragraph flatten
    assert _snippet("a  b   c") == "a b c"
    # Multi-paragraph → arrow marker
    assert "⏎" in _snippet("one\ntwo\nthree")
    # Truncation with word boundary + ellipsis
    long = "word " * 100
    out = _snippet(long, max_len=40)
    assert out.endswith("…")
    assert len(out) <= 41
    # Hard cap when no space available near the end (all one token).
    packed = "x" * 100
    out2 = _snippet(packed, max_len=40)
    assert len(out2) == 41  # 40 chars + ellipsis
    assert out2.endswith("…")


async def test_new_message_metadata_carries_session_title(setup):
    """Regression: notification metadata should include the user-visible
    session title so the panel can render "session my-cool-work" instead of
    "session 2da5e484". Falls back to id prefix if the session has no title."""
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="csm-title-A", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-A",
                       title="my-cool-work", unread_count=0)
        db.add(sess)
        sess2 = Session(id="csm-title-B", cwd="/tmp", type=SessionType.INTERACTIVE,
                        status=SessionStatus.RUNNING, external_session_id="claude-B",
                        title=None, unread_count=0)
        db.add(sess2)
        await db.commit()

    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-A",
        project_path="/tmp",
        payload={},
    ))
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC),
        session_id="claude-B",
        project_path="/tmp",
        payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        by_sid = {r.session_id: r for r in rows}
        assert by_sid["csm-title-A"].notif_metadata.get("session_title") == "my-cool-work"
        # No title on the source session → falls back to id prefix.
        assert by_sid["csm-title-B"].notif_metadata.get("session_title") == "csm-titl"


async def test_mark_all_read_prevents_concurrent_assistant_done_from_reopening(setup):
    """P1 race: `mark_all_read` used to clear `_last_new_msg` outside any
    per-session lock. A concurrent `_on_assistant_done` on a session that
    was in the tracking dict could hit the merge branch after the SQL
    update ran, mutate the just-dismissed row (`read_at = None`), and
    resurrect it in the panel.

    After the fix, `mark_all_read` acquires every currently-tracked
    session lock BEFORE the batch update, so a concurrent
    `_on_assistant_done` waiting on that lock resumes AFTER the clear
    sees the pointer already gone and falls into the INSERT branch —
    creating a distinct new row instead of reviving the dismissed one.
    """
    import asyncio as _asyncio
    bus, sm, es, _ = setup
    bus._assistant_done_dedup_sec = 0
    async with sm() as db:
        sess = Session(id="csm-mar-1", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-mar",
                       unread_count=0)
        db.add(sess)
        await db.commit()

    # Seed one notif so the session ends up in `_last_new_msg`.
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-mar",
        project_path="/tmp", payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        first_id = rows[0].id

    # Now fire mark_all_read and a second assistant.done concurrently.
    _, _ = await _asyncio.gather(
        bus.mark_all_read(),
        es.emit(Event(
            type=EventType.MESSAGE_ASSISTANT_DONE,
            ts=datetime.now(UTC), session_id="claude-mar",
            project_path="/tmp", payload={},
        )),
    )

    async with sm() as db:
        first = await db.get(Notification, first_id)
        # The seed row was dismissed by mark_all_read and MUST stay so —
        # the concurrent event must not revive it via the merge branch.
        assert first.dismissed_at is not None
        assert first.read_at is not None
        # The concurrent event landed either after our lock (fresh row)
        # or was serialised in ahead of us; either way the total should
        # be ≥ 1 and no row should be resurrected.
        rows = (await db.execute(select(Notification))).scalars().all()
        assert all(
            (r.id != first_id) or (r.dismissed_at is not None)
            for r in rows
        )


async def test_mark_session_read_bogus_sid_returns_zero(setup):
    """P1-8: `mark_session_read` on a session_id that has no matching
    Session row must return 0 so the API layer can 404 instead of
    silently answering 200. Regression for daily_review YAMLs that used
    to loop forever thinking they'd cleared something."""
    bus, sm, es, _ = setup
    cleared = await bus.mark_session_read("does-not-exist")
    assert cleared == 0


async def test_dismiss_new_message_pops_dedup_pointer(setup):
    """P1-3: `dismiss` on a NEW_MESSAGE row must invalidate `_last_new_msg`
    for that session — otherwise a follow-up assistant.done within the
    dedup window would revive the dismissed row via the merge branch
    (`existing.read_at = None`) and it would light the bell up again."""
    bus, sm, es, _ = setup
    bus._assistant_done_dedup_sec = 0
    async with sm() as db:
        sess = Session(id="csm-dsm", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-dsm",
                       unread_count=0)
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-dsm",
        project_path="/tmp", payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        first_id = rows[0].id
    assert await bus.dismiss(first_id) is True

    # Next assistant.done must open a NEW row, not resurrect the dismissed one.
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-dsm",
        project_path="/tmp", payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 2
        first = next(r for r in rows if r.id == first_id)
        assert first.dismissed_at is not None
        second = next(r for r in rows if r.id != first_id)
        assert second.dismissed_at is None
        assert second.read_at is None


async def test_stale_dedup_pointer_does_not_swallow_new_message(setup):
    """P0-2: if the row that `_last_new_msg[sid]` points at has been
    hard-deleted (e.g. session purge cascade), the next assistant.done
    for that session used to fall into the merge branch, find
    `existing is None`, silently return WITHOUT inserting, and set a
    dangling pointer that kept swallowing every subsequent event until
    the 5s window expired each time.

    After the fix, the merge branch pops the stale pointer and falls
    through to the INSERT branch — so the event surfaces as a fresh
    NEW_MESSAGE row.
    """
    bus, sm, es, _ = setup
    bus._assistant_done_dedup_sec = 0
    async with sm() as db:
        sess = Session(id="csm-stale", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-stale",
                       unread_count=0)
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-stale",
        project_path="/tmp", payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        first_id = rows[0].id
        # Simulate the purge cascade: hard-delete the notification row
        # WITHOUT touching `_last_new_msg` (the bug state).
        from sqlalchemy import delete as _delete
        await db.execute(_delete(Notification).where(Notification.id == first_id))
        await db.commit()
    assert bus._last_new_msg.get("csm-stale") is not None  # pointer is dangling

    # Now a fresh assistant.done — must surface as a new row.
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-stale",
        project_path="/tmp", payload={},
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id != first_id
        assert rows[0].read_at is None


async def test_supervisor_review_metadata_carries_session_title(setup):
    """P1-9: AUTO_NEEDS_REVIEW notifications (supervisor review, permission
    required, etc.) should stamp `session_title` into metadata so the
    panel's session tag shows the friendly name, not the 8-char id
    prefix — consistent with the NEW_MESSAGE fix."""
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="csm-sup", cwd="/tmp", type=SessionType.AUTO,
                       status=SessionStatus.RUNNING, title="mission-42")
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.SUPERVISOR_REVIEW_REQUESTED,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={
            "csm_session_id": "csm-sup",
            "category": "regression",
            "reason": "unit tests failing",
        },
    ))
    async with sm() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.AUTO_NEEDS_REVIEW
        assert rows[0].notif_metadata.get("session_title") == "mission-42"


async def test_permission_notif_auto_clears_on_tool_progress(setup):
    """H5 emits a 'Permission required' notif on SESSION_WAITING_AUTH; once
    the user answers y/n in the terminal, claude fires PostToolUse which
    surfaces as SESSION_TOOL_PROGRESS. The bus should mark the pending
    permission notif as read so the bell decrements without a click.
    Supervisor review notifs (also AUTO_NEEDS_REVIEW) must NOT be cleared."""
    bus, sm, es, _ = setup
    async with sm() as db:
        sess = Session(id="csm-p1", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.WAITING_AUTH, title="job")
        db.add(sess)
        # Plant a supervisor review notif to prove it stays unread.
        sup_sess = Session(id="csm-p2", cwd="/tmp", type=SessionType.AUTO,
                           status=SessionStatus.RUNNING, title="mission")
        db.add(sup_sess)
        await db.commit()

    # Fire H5 → permission notif exists, unread.
    await es.emit(Event(
        type=EventType.SESSION_WAITING_AUTH,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={"csm_session_id": "csm-p1"},
    ))
    # Fire supervisor review → separate AUTO_NEEDS_REVIEW that must NOT auto-clear.
    await es.emit(Event(
        type=EventType.SUPERVISOR_REVIEW_REQUESTED,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={"csm_session_id": "csm-p2", "category": "regression", "reason": "x"},
    ))

    async with sm() as db:
        rows = (await db.execute(select(Notification).order_by(Notification.session_id))).scalars().all()
        assert len(rows) == 2
        by_sid = {r.session_id: r for r in rows}
        assert by_sid["csm-p1"].title == "Permission required"
        assert by_sid["csm-p1"].read_at is None
        assert by_sid["csm-p2"].title.startswith("Needs review")
        assert by_sid["csm-p2"].read_at is None

    # User answers permission → tool progresses → clear.
    await es.emit(Event(
        type=EventType.SESSION_TOOL_PROGRESS,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={"csm_session_id": "csm-p1"},
    ))

    async with sm() as db:
        rows = (await db.execute(select(Notification).order_by(Notification.session_id))).scalars().all()
        by_sid = {r.session_id: r for r in rows}
        # Permission notif cleared…
        assert by_sid["csm-p1"].read_at is not None, "permission notif should auto-clear on tool progress"
        # …but supervisor review left alone.
        assert by_sid["csm-p2"].read_at is None, "supervisor review must NOT auto-clear"


async def test_permission_notif_auto_clear_is_surgical(setup):
    """`local:dc4dceec` — clear only the OLDEST pending permission notif per
    moved-forward event, not every unread one. Repro: two SESSION_WAITING_AUTH
    events land back-to-back (permission A then a fresh permission B before
    A's PostToolUse fires); the tool_progress signal that resolves A must not
    also silently reap B."""
    bus, sm, es, _ = setup
    from datetime import timedelta

    from csm.models.notification import Notification, NotificationType
    from csm.utils.time import now_utc_naive

    async with sm() as db:
        db.add(Session(id="csm-two-perm", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.WAITING_AUTH))
        await db.commit()

    # Plant two "Permission required" rows for the same session, offset so the
    # ORDER BY created_at ASC pick is deterministic.
    older = Notification(
        type=NotificationType.AUTO_NEEDS_REVIEW,
        session_id="csm-two-perm",
        title="Permission required",
        body="perm A",
        notif_metadata={},
    )
    async with sm() as db:
        db.add(older)
        await db.commit()
        await db.refresh(older)
        older.created_at = now_utc_naive() - timedelta(seconds=5)
        await db.commit()
    newer = Notification(
        type=NotificationType.AUTO_NEEDS_REVIEW,
        session_id="csm-two-perm",
        title="Permission required",
        body="perm B",
        notif_metadata={},
    )
    async with sm() as db:
        db.add(newer)
        await db.commit()
        await db.refresh(newer)

    # These rows were planted directly in the DB, bypassing the WAITING_AUTH
    # create path that populates the in-memory pending-permission guard. Mirror
    # that state so the fast-path guard in _clear_pending_permission_notif lets
    # the sweep reach the DB (in production the set is always populated when a
    # "Permission required" notif is emitted).
    bus._sids_pending_permission.add("csm-two-perm")

    # One moved-forward event → only the oldest clears.
    await es.emit(Event(
        type=EventType.SESSION_TOOL_PROGRESS,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={"csm_session_id": "csm-two-perm"},
    ))

    async with sm() as db:
        rows = (await db.execute(
            select(Notification)
            .where(Notification.session_id == "csm-two-perm")
            .order_by(Notification.created_at.asc())
        )).scalars().all()
    assert len(rows) == 2
    assert rows[0].body == "perm A"
    assert rows[0].read_at is not None, "older permission should have cleared"
    assert rows[1].body == "perm B"
    assert rows[1].read_at is None, "newer permission must NOT have been reaped by the same event"


async def test_permission_notif_auto_clears_on_waiting_input(setup):
    """Same auto-clear semantics via SESSION_WAITING_INPUT (the alternate
    hook path when claude proceeds to prompt the user for text input)."""
    bus, sm, es, _ = setup
    async with sm() as db:
        db.add(Session(id="csm-p3", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.WAITING_AUTH))
        await db.commit()
    await es.emit(Event(
        type=EventType.SESSION_WAITING_AUTH,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={"csm_session_id": "csm-p3"},
    ))
    await es.emit(Event(
        type=EventType.SESSION_WAITING_INPUT,
        ts=datetime.now(UTC), session_id=None, project_path=None,
        payload={"csm_session_id": "csm-p3"},
    ))
    async with sm() as db:
        row = (await db.execute(select(Notification).where(Notification.session_id == "csm-p3"))).scalar_one()
        assert row.read_at is not None


async def test_evict_session_lets_reused_id_fire_fresh(setup):
    """After `evict_session(sid)`, any lingering `_last_new_msg[sid]`
    state is gone — a subsequent assistant.done for a fresh session
    row that happens to share the same csm id opens a NEW notification
    instead of trying to merge into whatever the previous session left
    behind."""
    bus, sm, es, _ = setup
    bus._assistant_done_dedup_sec = 0
    async with sm() as db:
        sess = Session(id="csm-evict", cwd="/tmp", type=SessionType.INTERACTIVE,
                       status=SessionStatus.RUNNING, external_session_id="claude-evict")
        db.add(sess)
        await db.commit()
    await es.emit(Event(
        type=EventType.MESSAGE_ASSISTANT_DONE,
        ts=datetime.now(UTC), session_id="claude-evict",
        project_path="/tmp", payload={},
    ))
    assert "csm-evict" in bus._last_new_msg
    bus.evict_session("csm-evict")
    assert "csm-evict" not in bus._last_new_msg
    assert "csm-evict" not in bus._session_locks
    assert "csm-evict" not in bus._last_assistant_done_bump


async def test_inapp_sink_receives_push(setup):
    bus, sm, es, sink = setup

    class FakeWS:
        def __init__(self):
            self.received: list[str] = []
            self.accepted = False

        async def accept(self):
            self.accepted = True

        async def send_text(self, msg: str):
            self.received.append(msg)

    ws = FakeWS()
    await sink.attach(ws)
    assert ws.accepted is True

    await es.emit(Event(
        type=EventType.SESSION_CRASHED,
        ts=datetime.now(UTC),
        session_id=None,
        project_path=None,
        payload={"csm_session_id": "csm-9", "exit_code": 1},
    ))
    assert len(ws.received) == 1
    assert "session_crashed" in ws.received[0]


# ---- Retention (Patch A2) ----


async def _seed_notification(sm, *, kind, created_at, dismissed_at=None, read_at=None, session_id=None):
    async with sm() as db:
        n = Notification(
            type=kind,
            session_id=session_id,
            title="seed",
            body=None,
            notif_metadata={},
        )
        db.add(n)
        await db.commit()
        await db.refresh(n)
        # Datetimes on Notification are managed by SQLAlchemy defaults, so
        # patch them after the initial insert to hit the exact age we want.
        n.created_at = created_at
        if dismissed_at is not None:
            n.dismissed_at = dismissed_at
        if read_at is not None:
            n.read_at = read_at
        await db.commit()
        return n.id


async def test_retention_prunes_dismissed_older_than_cutoff(setup):
    bus, sm, _, _ = setup
    old_dismissed = datetime.utcnow() - timedelta(days=45)
    fresh_dismissed = datetime.utcnow() - timedelta(days=10)
    old_id = await _seed_notification(
        sm, kind=NotificationType.SESSION_CRASHED,
        created_at=old_dismissed, dismissed_at=old_dismissed,
    )
    fresh_id = await _seed_notification(
        sm, kind=NotificationType.SESSION_CRASHED,
        created_at=fresh_dismissed, dismissed_at=fresh_dismissed,
    )
    aged, capped = await bus._retention_tick()
    assert aged == 1
    assert capped == 0
    async with sm() as db:
        remaining = (await db.execute(select(Notification.id))).scalars().all()
    assert old_id not in remaining
    assert fresh_id in remaining


async def test_retention_keeps_unread_regardless_of_age(setup):
    bus, sm, _, _ = setup
    ancient = datetime.utcnow() - timedelta(days=365)
    unread_id = await _seed_notification(
        sm, kind=NotificationType.SESSION_CRASHED,
        created_at=ancient, dismissed_at=None,
    )
    aged, capped = await bus._retention_tick()
    assert aged == 0
    assert capped == 0
    async with sm() as db:
        remaining = (await db.execute(select(Notification.id))).scalars().all()
    assert unread_id in remaining


async def test_retention_per_type_cap_drops_oldest(sm=None):
    # Use a smaller cap so the test doesn't have to insert 1000 rows.
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    es = EventStream(projects_root=Path(tempfile.gettempdir()), poll_interval_sec=10.0, watchdog_interval_sec=10.0)
    sink = InAppSink()
    bus = NotificationBus(
        sessionmaker=sm, event_stream=es, in_app_sink=sink,
        dedup_window_sec=5, per_type_cap=3,
    )
    await bus.start()
    try:
        # Insert 5 dismissed rows of the same type at strictly increasing
        # ages — oldest 2 should be culled to fit cap=3.
        ids = []
        for i in range(5):
            nid = await _seed_notification(
                sm, kind=NotificationType.TOKEN_WARNING,
                created_at=datetime.utcnow() - timedelta(hours=i + 1),
            )
            ids.append(nid)
        # Rows are freshest → oldest by index [0,1,2,3,4]. Cap keeps
        # newest 3, so ids[3] and ids[4] should be dropped.
        aged, capped = await bus._retention_tick()
        assert aged == 0
        assert capped == 2
        async with sm() as db:
            remaining = set(
                (await db.execute(select(Notification.id))).scalars().all()
            )
        assert ids[0] in remaining
        assert ids[1] in remaining
        assert ids[2] in remaining
        assert ids[3] not in remaining
        assert ids[4] not in remaining
    finally:
        await bus.stop()
        await es.stop()
        await engine.dispose()
        os.unlink(db_path)


# ---- Lark fire-and-forget (Patch B1) ----


async def test_lark_send_does_not_block_dispatch():
    """Emit a session-crash event with a LarkSink that hangs 5s inside
    `send()`. The subscriber `_dispatch` should return promptly instead
    of waiting for Lark; the shell-out lives on a background task."""
    import asyncio as _aio
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    es = EventStream(projects_root=Path(tempfile.gettempdir()), poll_interval_sec=10.0, watchdog_interval_sec=10.0)
    sink = InAppSink()

    class HangingLark:
        async def send(self, payload):
            await _aio.sleep(5.0)
            return True

    bus = NotificationBus(
        sessionmaker=sm, event_stream=es, in_app_sink=sink,
        dedup_window_sec=5, lark_sink=HangingLark(),
    )
    await bus.start()
    try:
        loop = _aio.get_running_loop()
        t0 = loop.time()
        await es.emit(Event(
            type=EventType.SESSION_CRASHED,
            ts=datetime.now(UTC),
            session_id=None,
            project_path=None,
            payload={"csm_session_id": "sid-blocked", "exit_code": 1},
        ))
        elapsed = loop.time() - t0
        # If Lark were awaited inline this would be ~5s. Margin allows
        # for cold-start / DB commit noise on loaded CI runners.
        assert elapsed < 2.0, f"dispatch blocked for {elapsed:.2f}s"
    finally:
        await bus.stop()
        await es.stop()
        await engine.dispose()
        os.unlink(db_path)
