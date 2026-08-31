"""Unit tests for message_router.route_record."""
from __future__ import annotations

from csm.modules.agent.message_router import route_record


def test_user_text_message():
    obj = {
        "type": "user",
        "timestamp": "2026-06-24T00:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }
    evs = route_record(obj)
    assert evs == [
        {"type": "user_message", "ts": "2026-06-24T00:00:00Z", "text": "hello"}
    ]


def test_assistant_mixed_text_and_tool_use():
    obj = {
        "type": "assistant",
        "timestamp": "2026-06-24T00:00:01Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking file"},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file": "x"}},
            ],
        },
    }
    evs = route_record(obj)
    assert len(evs) == 2
    assert evs[0]["type"] == "assistant_text"
    assert evs[0]["text"] == "checking file"
    assert evs[1]["type"] == "tool_use_start"
    assert evs[1]["tool"] == "Read"
    assert evs[1]["tool_id"] == "toolu_1"


def test_tool_result_in_user_message():
    obj = {
        "type": "user",
        "timestamp": "2026-06-24T00:00:02Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
            ],
        },
    }
    evs = route_record(obj)
    assert evs == [
        {
            "type": "tool_use_result",
            "ts": "2026-06-24T00:00:02Z",
            "tool_id": "toolu_1",
            "ok": True,
            "preview": "ok",
        }
    ]


def test_tool_result_error_flag():
    obj = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": "EISDIR",
                    "is_error": True,
                },
            ],
        },
    }
    evs = route_record(obj)
    assert evs[0]["ok"] is False
    assert evs[0]["preview"] == "EISDIR"


def test_string_content_treated_as_text():
    obj = {
        "type": "user",
        "message": {"role": "user", "content": "plain string"},
    }
    evs = route_record(obj)
    assert evs == [{"type": "user_message", "ts": "", "text": "plain string"}]


def test_empty_text_block_skipped():
    obj = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": ""}]},
    }
    assert route_record(obj) == []


def test_unknown_record_ignored():
    assert route_record({"type": "weird"}) == []
    assert route_record({}) == []
    assert route_record({"type": "user", "message": "not a dict"}) == []  # type: ignore


def test_summary_record_emits_system_note():
    obj = {"type": "summary", "timestamp": "t", "summary": "session compaction"}
    evs = route_record(obj)
    assert evs == [
        {"type": "system_note", "ts": "t", "text": "session compaction"}
    ]


def test_tool_result_list_content_joined():
    obj = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t",
                    "content": [
                        {"type": "text", "text": "line1"},
                        {"type": "text", "text": "line2"},
                    ],
                }
            ],
        },
    }
    evs = route_record(obj)
    assert evs[0]["preview"] == "line1\nline2"


# ---------------------------------------------------------------------------
# Machine-injected role-"user" records
#
# Claude files a skill's preamble, the post-compaction recap, the
# auto-continue nudge, a subagent's completion report and the Esc-interrupt
# marker under role "user". They are the user's ROLE but not the user's WORDS,
# so the mobile jump rail filled with dots pointing at text the user never
# wrote. All of it is flagged structurally, which is what we key on —
# string-matching the English would rot on the next CLI release.
#
# Replaying the 49 local transcripts: 846 records survived the rail's own
# filters, 88 of them (10%) were not typed by anyone — 73 task-notifications
# and 15 interrupt markers.
# ---------------------------------------------------------------------------


def _user(text: str, **top) -> dict:
    return {"timestamp": "2026-08-25T00:00:00Z", "type": "user",
            "message": {"role": "user", "content": text}, **top}


def test_is_meta_user_record_is_marked_injected():
    """`isMeta` covers a skill preamble and the auto-continue nudge."""
    out = route_record(_user("Continue from where you left off.", isMeta=True))

    assert len(out) == 1
    assert out[0]["type"] == "user_message"
    assert out[0]["injected"] is True


def test_compact_summary_is_marked_injected():
    out = route_record(_user(
        "This session is being continued from a previous conversation…",
        isCompactSummary=True, isVisibleInTranscriptOnly=True,
    ))

    assert out[0]["injected"] is True


def test_a_typed_message_carries_no_injected_key():
    """Absence, not `False` — every existing consumer reads this event shape
    and a new always-present key is a needless contract change."""
    out = route_record(_user("我们现在要进行agent infra的调优"))

    assert out[0]["type"] == "user_message"
    assert "injected" not in out[0]


def test_injected_text_is_still_rendered():
    """Marked, not dropped: it IS part of the transcript, and hiding the
    compaction recap would lose the only in-chat trace of a context reset."""
    out = route_record(_user("Base directory for this skill: /x", isMeta=True))

    assert out[0]["text"] == "Base directory for this skill: /x"


# ---------------------------------------------------------------------------
# `<task-notification>` — a subagent / background command reporting back
#
# Biggest single source of noise in the mobile chat: 390 in the local corpus,
# median 385 chars and up to 12KB of XML envelope, filed under role "user" and
# rendered verbatim as if the user had typed it. Collapsed to the one line that
# carries meaning, and re-attributed to the system.
# ---------------------------------------------------------------------------

def _task_notification(summary: str, status: str = "completed",
                       result: str = "…", **top) -> dict:
    return _user(
        f"<task-notification>\n"
        f"<task-id>ac68cd85f4463626b</task-id>\n"
        f"<tool-use-id>toolu_01QfKovNEt7P1p7XXn4kNDeV</tool-use-id>\n"
        f"<output-file>/tmp/claude-1001/x/tasks/ac68cd85f4463626b.output</output-file>\n"
        f"<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n"
        f"<note>A task-notification fires each time this agent stops…</note>\n"
        f"<result>{result}</result>\n"
        f"</task-notification>",
        origin={"kind": "task-notification"}, **top,
    )


def test_task_notification_collapses_to_a_one_line_system_note():
    """Shaped as CLI 2.1.112 writes it — `origin` and no promptSource, the only
    signal on 268 records. The envelope's task-id / output-file / boilerplate
    note / multi-KB result are all plumbing the user cannot act on."""
    out = route_record(_task_notification('Agent "Brain 代码架构设计" finished'))

    assert len(out) == 1
    assert out[0] == {
        "type": "system_note",
        "ts": "2026-08-25T00:00:00Z",
        "text": 'Agent "Brain 代码架构设计" finished',
    }


def test_task_notification_from_a_newer_cli_collapses_too():
    """2.1.233 adds promptSource alongside `origin`."""
    out = route_record(_task_notification(
        'Background command "Monitor" completed (exit code 0)',
        promptSource="system", permissionMode="bypassPermissions",
    ))

    assert out[0]["text"] == 'Background command "Monitor" completed (exit code 0)'


def test_a_task_that_did_not_complete_says_so():
    """`<summary>` reads like success regardless, so a subagent that was killed
    would otherwise be indistinguishable from one that finished — in the text
    AND in the styling. The frontend supplies its own `level` in tests, so
    without this the router could stop emitting it and the warning colour would
    just quietly never appear again."""
    out = route_record(_task_notification('Agent "審閱" finished', status="failed"))

    assert out[0]["text"] == 'Agent "審閱" finished [failed]'
    assert out[0]["level"] == "warning"


def test_a_completed_task_carries_no_level():
    """96% of these notes are routine. Absence is what keeps them quiet — if
    every note carried a level the rare real failure would stop standing out."""
    out = route_record(_task_notification('Agent "X" finished'))

    assert "level" not in out[0]


def test_task_notification_never_becomes_a_user_message():
    """The point of the change: not attributed to the user, so it renders in
    the muted system style and the jump rail skips it on role alone."""
    out = route_record(_task_notification("whatever"))

    assert [e["type"] for e in out] == ["system_note"]


def test_quoting_a_task_notification_is_still_my_message():
    """Recognition is by `origin.kind`, not by matching the text — otherwise
    asking about a notification would replace your question with its summary."""
    out = route_record(_user(
        "<task-notification><summary>Agent finished</summary></task-notification>"
        " 这个是怎么来的?"))

    assert out[0]["type"] == "user_message"
    assert "injected" not in out[0]


def test_an_unrecognised_origin_kind_defaults_to_not_mine():
    """Forward-looking, and labelled as such: "task-notification" is the only
    non-human kind that exists today and it is intercepted above, so this arm
    catches nothing in the current corpus. It stays because 2.1.112 proved a
    release can ship an `origin` with no `promptSource` beside it — a new kind
    arriving that way must default off the rail, not onto it."""
    out = route_record(_user("Some future injection.",
                             origin={"kind": "scheduled-reminder"}))

    assert out[0]["injected"] is True


def test_sdk_driven_prompt_is_marked_injected():
    """`claude -p` / agent-authored prompts arrive with promptSource="sdk" and
    no `origin` at all, so the promptSource arm has to stand on its own."""
    out = route_record(_user("Review the emitted YAML.", promptSource="sdk"))

    assert out[0]["injected"] is True


def test_queued_message_is_mine():
    """Typed while a turn was still running, so the CLI parks it as "queued".
    Still my words — an allowlist that only accepted "typed" would silently
    drop these from the rail."""
    out = route_record(_user("先别急，等一下", promptSource="queued"))

    assert "injected" not in out[0]


def test_interrupt_marker_is_not_a_message():
    """Pressing Esc is an action, not something to navigate to. Current CLI
    tags the record; pre-2.1.233 records carry nothing but the literal."""
    tagged = route_record(_user("[Request interrupted by user]",
                                interruptedMessageId="msg_011CeKboB4CX"))
    legacy = route_record(_user("[Request interrupted by user for tool use]"))

    assert tagged[0]["injected"] is True
    assert legacy[0]["injected"] is True


def test_quoting_the_interrupt_marker_is_still_my_message():
    """Full-string equality, not a prefix — otherwise asking about the marker
    costs you the rail node for the question."""
    out = route_record(_user(
        "[Request interrupted by user] 这条是怎么产生的？"))

    assert "injected" not in out[0]


def test_pre_provenance_records_are_still_mine():
    """CLI 2.1.112 wrote no `origin` / `promptSource` at all — 511 of the 1058
    role-user records in the local corpus. Requiring a positive
    promptSource=="typed" would have emptied the rail on all older history."""
    out = route_record(_user("你看看现在的mobile功能",
                             userType="external", entrypoint="cli"))

    assert "injected" not in out[0]
