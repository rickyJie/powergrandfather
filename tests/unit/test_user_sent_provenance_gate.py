"""`MESSAGE_USER_SENT` means the HUMAN spoke, not "a role-user record landed".

Claude files subagent task-notifications, skill preambles and headless
`claude -p` prompts under role "user". Emitting the event for those told every
consumer the user had just typed — the session flipped to RUNNING and
WorktimeTracker opened an interval for work nobody did (2026-08-30).

The claude adapter and EventStream's inline derivation each own a copy of this
branch, so both are asserted here — they must not drift.
"""

from __future__ import annotations

from csm.adapters.jsonl_tail import RawRecord
from csm.backends.claude.events import derive_claude_events
from csm.core.events import EventType


def _rec(obj: dict) -> RawRecord:
    return RawRecord(
        jsonl_path="/tmp/x/sid.jsonl",
        claude_session_id="sid",
        project_path="/tmp/x",
        line_no=1,
        byte_offset=0,
        obj=obj,
    )


def _types(obj: dict) -> list[EventType]:
    return [e.type for e in derive_claude_events(_rec(obj))]


def test_typed_message_emits_user_sent():
    types = _types({
        "message": {"role": "user", "content": "hi"},
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "timestamp": "2026-08-30T10:00:00Z",
    })
    assert EventType.MESSAGE_USER_SENT in types


def test_bare_record_still_emits_user_sent():
    # No provenance fields at all (CLI 2.1.112, and every tool-result record).
    types = _types({
        "message": {"role": "user", "content": "hi"},
        "timestamp": "2026-08-30T10:00:00Z",
    })
    assert EventType.MESSAGE_USER_SENT in types


def test_task_notification_does_not_emit_user_sent():
    types = _types({
        "message": {"role": "user", "content": "<task-notification>…"},
        "origin": {"kind": "task-notification"},
        "promptSource": "system",
        "timestamp": "2026-08-30T10:00:00Z",
    })
    assert EventType.MESSAGE_USER_SENT not in types


def test_sdk_prompt_does_not_emit_user_sent():
    types = _types({
        "message": {"role": "user", "content": "Rule that fired: …"},
        "promptSource": "sdk",
        "timestamp": "2026-08-30T10:00:00Z",
    })
    assert EventType.MESSAGE_USER_SENT not in types


def test_tool_results_survive_the_gate():
    """Turn accounting must not regress: a suppressed record's tool_result
    blocks still report, and an ordinary tool-result turn still counts as the
    user's side of the exchange."""
    events = derive_claude_events(_rec({
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ],
        },
        "promptSource": "sdk",
        "timestamp": "2026-08-30T10:00:00Z",
    }))
    types = [e.type for e in events]
    assert EventType.MESSAGE_USER_SENT not in types
    assert EventType.TOOL_COMPLETED in types
