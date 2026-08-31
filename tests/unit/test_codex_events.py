"""Unit tests for `derive_codex_events` — the codex-rollout → CSM-Event
translation layer used by the P4 multi-tailer path.

Coverage:
- Every mapping documented in docs/codex/rollout_schema.md:
    session_meta          → SESSION_STARTED
    event_msg::user_message   → MESSAGE_USER_SENT
    event_msg::task_complete  → MESSAGE_ASSISTANT_DONE
    event_msg::token_count    → USAGE_RECORDED
    response_item::function_call     → SESSION_TOOL_PROGRESS
    response_item::custom_tool_call  → SESSION_TOOL_PROGRESS
- Records that intentionally DON'T map (plain `response_item` messages,
  turn_context, world_state, task_started, etc.) yield nothing.
- Timestamp parsing / fallback.
- Robustness against missing / malformed payload sub-structures.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from csm.adapters.jsonl_tail import CodexRawRecord
from csm.core.codex_events import derive_codex_events, summarise_codex_events
from csm.core.events import EventType


def _rec(
    obj: dict,
    *,
    sid: str = "codex-sid",
    cwd: str = "/tmp/proj",
    model: str | None = None,
) -> CodexRawRecord:
    return CodexRawRecord(
        rollout_path="/fake/rollout.jsonl",
        codex_session_id=sid,
        project_path=cwd,
        model=model,
        line_no=1,
        byte_offset=0,
        obj=obj,
    )


# ---------------------------------------------------------------------------
# Mapping: session_meta
# ---------------------------------------------------------------------------


def test_session_meta_yields_session_started():
    r = _rec({
        "type": "session_meta",
        "timestamp": "2026-07-25T10:00:00Z",
        "payload": {"session_id": "codex-sid", "cwd": "/tmp/proj",
                    "cli_version": "0.145.0", "model_provider": "openai"},
    })
    events = list(derive_codex_events(r))
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.SESSION_STARTED
    assert ev.session_id == "codex-sid"
    assert ev.project_path == "/tmp/proj"
    assert ev.payload["backend"] == "codex"
    assert ev.payload["cli_version"] == "0.145.0"
    assert ev.payload["rollout_path"] == "/fake/rollout.jsonl"


def test_session_meta_falls_back_to_now_when_timestamp_missing():
    r = _rec({"type": "session_meta", "payload": {"session_id": "s", "cwd": "/x"}})
    before = datetime.now(UTC)
    events = list(derive_codex_events(r))
    after = datetime.now(UTC)
    assert len(events) == 1
    assert before <= events[0].ts <= after


def test_session_meta_falls_back_to_now_on_bad_iso():
    r = _rec({"type": "session_meta", "timestamp": "not-a-timestamp",
              "payload": {"session_id": "s", "cwd": "/x"}})
    events = list(derive_codex_events(r))
    assert len(events) == 1  # still emits, ts is now()


# ---------------------------------------------------------------------------
# Mapping: user_message
# ---------------------------------------------------------------------------


def test_user_message_yields_message_user_sent():
    r = _rec({
        "type": "event_msg",
        "timestamp": "2026-07-25T10:00:01Z",
        "payload": {"type": "user_message", "message": "hello codex"},
    })
    events = list(derive_codex_events(r))
    assert len(events) == 1
    assert events[0].type == EventType.MESSAGE_USER_SENT
    assert events[0].payload["text"] == "hello codex"


def test_user_message_empty_text_still_emits():
    """Empty user message is legal (client-side glitch) — still surface it."""
    r = _rec({"type": "event_msg", "payload": {"type": "user_message"}})
    events = list(derive_codex_events(r))
    assert len(events) == 1
    assert events[0].payload["text"] == ""


# ---------------------------------------------------------------------------
# Mapping: task_complete
# ---------------------------------------------------------------------------


def test_task_complete_yields_assistant_done():
    r = _rec({
        "type": "event_msg",
        "timestamp": "2026-07-25T10:00:02Z",
        "payload": {"type": "task_complete", "last_agent_message": "done",
                    "duration_ms": 2100, "turn_id": "turn-1"},
    })
    events = list(derive_codex_events(r))
    assert len(events) == 1
    assert events[0].type == EventType.MESSAGE_ASSISTANT_DONE
    assert events[0].payload["assistant_text"] == "done"
    assert events[0].payload["backend"] == "codex"
    assert events[0].payload["rollout_path"] == "/fake/rollout.jsonl"
    assert events[0].source_offset == 0
    assert events[0].payload["text"] == "done"
    assert events[0].payload["duration_ms"] == 2100
    assert events[0].payload["turn_id"] == "turn-1"


# ---------------------------------------------------------------------------
# Mapping: token_count
# ---------------------------------------------------------------------------


def test_token_count_yields_usage_recorded():
    r = _rec(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 500,
                        "cached_input_tokens": 200,
                        "cache_write_input_tokens": 50,
                        "output_tokens": 120,
                        "reasoning_output_tokens": 30,
                        "total_tokens": 620,
                    }
                },
                "rate_limits": {"rpm": 5000},
            },
        },
        model="gpt-4o",
    )
    events = list(derive_codex_events(r))
    assert len(events) == 1
    p = events[0].payload
    assert events[0].type == EventType.USAGE_RECORDED
    # M10.B: model comes from CodexRawRecord.model (bootstrapped from
    # session_meta by CodexRolloutTailer) so aggregator can price
    # codex spend correctly instead of falling through to Sonnet default.
    assert p["model"] == "gpt-4o"
    # Codex's input_tokens is inclusive of both cache detail counters.
    # CSM stores disjoint buckets because its totals sum all three.
    assert p["input_tokens"] == 250
    assert p["cache_read_input_tokens"] == 200
    assert p["cache_creation_input_tokens"] == 50
    assert p["output_tokens"] == 120
    assert p["_codex_input_tokens_inclusive"] == 500
    assert p["_codex_total_tokens"] == 620
    assert p["_codex_reasoning_output_tokens"] == 30


def test_token_count_caps_cache_details_to_inclusive_input():
    """Malformed detail counters must not make disjoint totals exceed input."""
    r = _rec({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "cache_write_input_tokens": 40,
                    "output_tokens": -1,
                }
            },
        },
    })

    payload = list(derive_codex_events(r))[0].payload

    assert payload["input_tokens"] == 0
    assert payload["cache_read_input_tokens"] == 80
    assert payload["cache_creation_input_tokens"] == 20
    assert payload["output_tokens"] == 0
    assert (
        payload["input_tokens"]
        + payload["cache_read_input_tokens"]
        + payload["cache_creation_input_tokens"]
    ) == 100


def test_token_count_missing_model_yields_none():
    """Rollout files predating session_meta.model support → payload.model=None."""
    r = _rec(
        {"type": "event_msg", "payload": {"type": "token_count"}},
        model=None,
    )
    events = list(derive_codex_events(r))
    assert len(events) == 1
    assert events[0].payload["model"] is None


def test_token_count_missing_info_defaults_to_zeros():
    r = _rec({"type": "event_msg", "payload": {"type": "token_count"}})
    events = list(derive_codex_events(r))
    assert len(events) == 1
    p = events[0].payload
    assert p["input_tokens"] == 0
    assert p["output_tokens"] == 0
    assert p["cache_read_input_tokens"] == 0


def test_successful_patch_end_yields_tool_progress_with_changed_files():
    r = _rec({
        "type": "event_msg",
        "timestamp": "2026-08-01T07:48:22Z",
        "payload": {
            "type": "patch_apply_end",
            "success": True,
            "changes": {
                "/tmp/proj/a.py": {"type": "update"},
                "/tmp/proj/b.py": {"type": "add"},
            },
        },
    })

    events = list(derive_codex_events(r))

    assert len(events) == 1
    assert events[0].type == EventType.SESSION_TOOL_PROGRESS
    assert events[0].payload["tool_name"] == "apply_patch"
    assert events[0].payload["changed_files"] == [
        "/tmp/proj/a.py",
        "/tmp/proj/b.py",
    ]


def test_failed_patch_end_yields_no_tool_progress():
    r = _rec({
        "type": "event_msg",
        "payload": {
            "type": "patch_apply_end",
            "success": False,
            "changes": {},
        },
    })

    assert list(derive_codex_events(r)) == []


def test_token_count_info_wrong_type_treated_as_missing():
    """A payload with info=<not a dict> shouldn't crash."""
    r = _rec({"type": "event_msg", "payload": {"type": "token_count", "info": "oops"}})
    events = list(derive_codex_events(r))
    assert len(events) == 1
    assert events[0].payload["input_tokens"] == 0


# ---------------------------------------------------------------------------
# Non-mapping: things we deliberately ignore
# ---------------------------------------------------------------------------


def test_response_item_message_yields_nothing():
    """A response_item that isn't a tool call still maps to nothing — only
    `function_call` / `custom_tool_call` became meaningful."""
    r = _rec({"type": "response_item", "payload": {"role": "assistant", "content": "..."}})
    assert list(derive_codex_events(r)) == []


# ---------------------------------------------------------------------------
# response_item tool calls → SESSION_TOOL_PROGRESS
#
# Codex emits no `event_msg` for an ordinary tool call, so this is the ONLY
# source of `current_tool` for a codex session. Measured on 30 real rollouts,
# wiring it drops "running but card says nothing" from 60% to 13% of RUNNING
# wall-clock (the remainder is the pre-first-tool thinking window, where
# claude's card is blank too).
# ---------------------------------------------------------------------------


def _tool_rec(payload: dict) -> CodexRawRecord:
    return _rec({"type": "response_item", "timestamp": "2026-08-25T03:34:12Z",
                 "payload": payload})


def test_function_call_shell_maps_to_bash_with_command_hint():
    r = _tool_rec({
        "type": "function_call",
        "name": "exec_command",
        "call_id": "call_1",
        "arguments": '{"cmd": "ls -la /tmp", "workdir": "/tmp", "yield_time_ms": 10000}',
    })

    events = list(derive_codex_events(r))

    assert len(events) == 1
    assert events[0].type == EventType.SESSION_TOOL_PROGRESS
    # `Bash` (not `exec_command`) so the card reads the same for both CLIs.
    # This mirrors codex's OWN normalisation: its PreToolUse hook payload
    # reports `tool_name: "Bash"` for exactly these calls.
    assert events[0].payload["tool_name"] == "Bash"
    assert events[0].payload["tool_hint"] == "ls -la /tmp"
    assert events[0].payload["call_id"] == "call_1"


def test_code_mode_exec_extracts_inner_tool_and_command():
    """`custom_tool_call` is always named `exec`; the real tool only exists
    inside the JavaScript body, so the outer name is useless on its own."""
    r = _tool_rec({
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "call_2",
        "input": 'const r = await tools.exec_command({"cmd":"pytest -q","workdir":"/x"});\ntext(r.output);\n',
    })

    events = list(derive_codex_events(r))

    assert events[0].payload["tool_name"] == "Bash"
    assert events[0].payload["tool_hint"] == "pytest -q"


def test_code_mode_command_escapes_are_decoded_for_display():
    """The command is lifted out of JS *source*, where `\\n` is still two
    literal characters. Without decoding, the card shows `foo\\nbar`."""
    r = _tool_rec({
        "type": "custom_tool_call",
        "name": "exec",
        "input": r'await tools.exec_command({"cmd":"echo one\nssh -V","workdir":"/x"});',
    })

    hint = list(derive_codex_events(r))[0].payload["tool_hint"]

    assert "\\n" not in hint
    assert hint == "echo one ssh -V"


def test_code_mode_non_shell_tool_keeps_its_own_name():
    r = _tool_rec({
        "type": "custom_tool_call",
        "name": "exec",
        "input": 'const r = await tools.web__run({search_query:[{q:"hello"}]}); text(r)',
    })

    assert list(derive_codex_events(r))[0].payload["tool_name"] == "web__run"


def test_internal_polling_calls_do_not_clobber_current_tool():
    """`wait` is codex polling a command it already started. Emitting progress
    for it would replace a real "Bash: pytest …" with a meaningless "wait"
    for the rest of the turn — strictly worse than staying put."""
    for name in ("wait", "wait_agent"):
        r = _tool_rec({
            "type": "function_call", "name": name,
            "arguments": '{"cell_id":"1","yield_time_ms":20000}',
        })
        assert list(derive_codex_events(r)) == []


def test_encrypted_agent_payloads_never_reach_the_hint():
    """`spawn_agent` / `send_message` / `followup_task` carry a multi-KB
    encrypted blob in `message`. A truncated blob is worse than no hint, so
    `message` is excluded from the hint keys entirely."""
    blob = "gAAAAABqffBTBt07" + "x" * 4000
    r = _tool_rec({
        "type": "function_call", "name": "spawn_agent",
        "arguments": json.dumps({"task_name": "product_manager", "message": blob}),
    })

    payload = list(derive_codex_events(r))[0].payload

    assert payload["tool_name"] == "spawn_agent"
    assert payload["tool_hint"] == "product_manager"
    assert "gAAAAA" not in payload["tool_hint"]


def test_malformed_tool_arguments_still_yield_progress():
    """Unparseable arguments must not cost us the "it's running a tool"
    signal — we just lose the hint."""
    r = _tool_rec({
        "type": "function_call", "name": "exec_command",
        "arguments": "{not json at all",
    })

    events = list(derive_codex_events(r))

    assert events[0].payload["tool_name"] == "Bash"
    assert events[0].payload["tool_hint"] == ""


def test_tool_call_without_a_name_yields_nothing():
    r = _tool_rec({"type": "function_call", "name": "", "arguments": "{}"})
    assert list(derive_codex_events(r)) == []


def test_turn_context_yields_nothing():
    r = _rec({"type": "turn_context", "payload": {"turn_id": "t1"}})
    assert list(derive_codex_events(r)) == []


def test_world_state_yields_nothing():
    r = _rec({"type": "world_state", "payload": {"cwd": "/tmp"}})
    assert list(derive_codex_events(r)) == []


def test_task_started_yields_nothing_for_now():
    """task_started is intentionally not surfaced yet — see codex_events.py."""
    r = _rec({"type": "event_msg", "payload": {"type": "task_started"}})
    assert list(derive_codex_events(r)) == []


def test_unknown_top_type_yields_nothing():
    r = _rec({"type": "future_event_type", "payload": {}})
    assert list(derive_codex_events(r)) == []


def test_missing_payload_dict_does_not_crash():
    r = _rec({"type": "event_msg"})  # payload absent
    assert list(derive_codex_events(r)) == []


# ---------------------------------------------------------------------------
# summarise helper (used by diagnostics)
# ---------------------------------------------------------------------------


def test_summarise_counts_by_type():
    records = [
        _rec({"type": "session_meta", "payload": {"session_id": "s", "cwd": "/x"}}),
        _rec({"type": "event_msg", "payload": {"type": "user_message", "message": "a"}}),
        _rec({"type": "event_msg", "payload": {"type": "user_message", "message": "b"}}),
        _rec({"type": "event_msg", "payload": {"type": "token_count"}}),
        _rec({"type": "response_item", "payload": {}}),  # not mapped
    ]
    summary = summarise_codex_events(records)
    assert summary[EventType.SESSION_STARTED.value] == 1
    assert summary[EventType.MESSAGE_USER_SENT.value] == 2
    assert summary[EventType.USAGE_RECORDED.value] == 1
    assert EventType.MESSAGE_ASSISTANT_DONE.value not in summary
