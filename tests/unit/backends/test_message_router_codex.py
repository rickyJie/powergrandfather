"""M10.C: message_router supports codex rollout records too.

Frontend AgentChat WebSocket history replay uses `route_record` to
convert a stored transcript line into displayable chat events. Before
M10.C the router only knew claude JSONL shape; codex records were
silently dropped and codex agent chats showed empty history.
"""
from __future__ import annotations

from csm.modules.agent.message_router import route_record


def test_codex_user_message():
    r = route_record({
        "type": "event_msg",
        "timestamp": "2026-07-26T10:00:00Z",
        "payload": {"type": "user_message", "message": "hello codex"},
    })
    assert r == [{
        "type": "user_message",
        "ts": "2026-07-26T10:00:00Z",
        "text": "hello codex",
    }]


def test_codex_task_complete_is_assistant_text():
    r = route_record({
        "type": "event_msg",
        "timestamp": "2026-07-26T10:00:01Z",
        "payload": {"type": "task_complete", "last_agent_message": "done"},
    })
    assert r == [{
        "type": "assistant_text",
        "ts": "2026-07-26T10:00:01Z",
        "text": "done",
    }]


def test_codex_session_meta_with_model_yields_system_note():
    r = route_record({
        "type": "session_meta",
        "timestamp": "2026-07-26T10:00:00Z",
        "payload": {"session_id": "s", "model": "gpt-4o", "cwd": "/x"},
    })
    assert len(r) == 1
    assert r[0]["type"] == "system_note"
    assert "gpt-4o" in r[0]["text"]


def test_codex_session_meta_without_model_yields_nothing():
    r = route_record({
        "type": "session_meta",
        "payload": {"session_id": "s", "cwd": "/x"},
    })
    assert r == []


def test_codex_token_count_yields_nothing():
    """Token counts belong on the Tokens page, not in the chat feed."""
    r = route_record({
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {}},
    })
    assert r == []


def test_codex_turn_context_yields_nothing():
    r = route_record({
        "type": "turn_context",
        "payload": {"turn_id": "t1"},
    })
    assert r == []


def test_codex_empty_user_message_dropped():
    r = route_record({
        "type": "event_msg",
        "payload": {"type": "user_message", "message": ""},
    })
    assert r == []


def test_claude_shape_still_works():
    """Regression: adding codex branch didn't break claude routing."""
    r = route_record({
        "timestamp": "2026-07-26T10:00:00Z",
        "message": {"role": "user", "content": "hi from claude"},
    })
    assert len(r) == 1
    assert r[0]["type"] == "user_message"
    assert r[0]["text"] == "hi from claude"


# ---------------------------------------------------------------------------
# Tool cards — response_item::{function_call,custom_tool_call}
#
# Codex puts the model's tool calls in `response_item`, NOT `event_msg`. While
# the router only read `event_msg`, a codex turn produced no chat events at all
# between the user's message and `task_complete`, so the mobile stream sat on
# "Thinking…" for the whole turn with nothing to show.
# ---------------------------------------------------------------------------


def _call(payload: dict) -> list[dict]:
    return route_record({
        "type": "response_item", "timestamp": "2026-08-25T03:34:12Z",
        "payload": payload,
    })


def test_function_call_opens_a_tool_card():
    r = _call({
        "type": "function_call", "name": "exec_command", "call_id": "call_1",
        "arguments": '{"cmd": "pytest -q", "workdir": "/x"}',
    })

    assert len(r) == 1
    assert r[0]["type"] == "tool_use_start"
    assert r[0]["tool_id"] == "call_1"
    # `Bash` + `command` is claude's tool_input shape, so the codex card
    # renders through the identical frontend path.
    assert r[0]["tool"] == "Bash"
    assert r[0]["input"]["command"] == "pytest -q"


def test_output_closes_the_card_it_belongs_to():
    r = _call({
        "type": "function_call_output", "call_id": "call_1",
        "output": [{"type": "input_text", "text": "42 passed"}],
    })

    assert len(r) == 1
    assert r[0] == {
        "type": "tool_use_result", "ts": "2026-08-25T03:34:12Z",
        "tool_id": "call_1", "ok": True, "preview": "42 passed",
    }


def test_output_is_dropped_without_a_call_id_to_pair_on():
    """An unpairable result renders as a stray empty row in the stream."""
    assert _call({"type": "custom_tool_call_output", "output": "orphan"}) == []


def test_code_mode_card_shows_the_real_tool_not_the_exec_wrapper():
    r = _call({
        "type": "custom_tool_call", "name": "exec", "call_id": "call_2",
        "input": 'const r = await tools.exec_command({"cmd":"ls -la","workdir":"/x"});',
    })

    assert r[0]["tool"] == "Bash"
    assert r[0]["input"]["command"] == "ls -la"


def test_apply_patch_card_names_the_files_before_the_write_lands():
    """The paths are only in the patch body at call time — codex's structured
    `patch_apply_end` doesn't arrive until afterwards.

    Note the `\\n`: inside the script the patch is a JS string literal, so its
    line breaks are the two characters backslash-n. Matching before decoding
    them makes the path capture run on into the diff — that produced garbage
    like `/tmp/a.md\\n+---` for 532 of 534 real patches.
    """
    r = _call({
        "type": "custom_tool_call", "name": "exec", "call_id": "call_3",
        "input": (
            r'const patch = "*** Begin Patch\n'
            r'*** Add File: /tmp/a.md\n+hello\n'
            r'*** Update File: /tmp/b.py\n-old\n+new\n";'
            'await tools.apply_patch({patch});'
        ),
    })

    assert r[0]["tool"] == "apply_patch"
    assert r[0]["input"]["files"] == ["/tmp/a.md", "/tmp/b.py"]


def test_script_is_shown_when_no_literal_argument_can_be_lifted():
    """Codex often builds its command list in JS and loops over it, so there
    is no `cmd: "…"` literal to find. An empty card is the one outcome that
    helps nobody."""
    r = _call({
        "type": "custom_tool_call", "name": "exec", "call_id": "call_4",
        "input": (
            'const cmds = [["shape","rg --files | wc -l"],["tree","ls -R"]];\n'
            'for (const [label, cmd] of cmds) {\n'
            '  const r = await tools.exec_command({cmd, workdir:"/x"});\n}'
        ),
    })

    # The inner tool is still identified even though its argument is a
    # variable, so the card is labelled correctly...
    assert r[0]["tool"] == "Bash"
    # ...and falls back to the script rather than rendering blank.
    assert "const cmds" in r[0]["input"]["script"]
    assert "command" not in r[0]["input"]


def test_unrecognisable_script_keeps_the_outer_exec_name():
    """No `tools.*` call at all — we still say something ran."""
    r = _call({
        "type": "custom_tool_call", "name": "exec", "call_id": "call_6",
        "input": 'const x = 1 + 1;',
    })

    assert r[0]["tool"] == "exec"
    assert "const x" in r[0]["input"]["script"]


def test_internal_polling_calls_produce_no_card():
    """`wait` is codex polling a command it already started — a card per poll
    would bury the actual work."""
    assert _call({
        "type": "function_call", "name": "wait", "call_id": "c",
        "arguments": '{"cell_id":"1"}',
    }) == []


def test_encrypted_agent_payload_never_reaches_a_card():
    r = _call({
        "type": "function_call", "name": "spawn_agent", "call_id": "call_5",
        "arguments": '{"task_name":"pm","message":"gAAAAAB' + "x" * 3000 + '"}',
    })

    assert r[0]["input"] == {"task_name": "pm"}
    assert "gAAAAA" not in str(r[0]["input"])


def test_reasoning_and_plain_messages_stay_out_of_the_stream():
    assert _call({"type": "reasoning", "summary": []}) == []
    assert _call({"type": "message", "role": "assistant", "content": []}) == []


def test_inner_step_records_no_longer_double_render():
    """`patch_apply_end` / `web_search_end` describe steps INSIDE one model
    tool call (measured 1:1 with the wrapping `exec` across 30 real rollouts).
    Routing them too drew the same action twice, so the chat now takes only
    the `response_item` call. They remain live elsewhere: `patch_apply_end`
    still drives SESSION_TOOL_PROGRESS and the Changes panel."""
    assert route_record({
        "type": "event_msg",
        "payload": {"type": "patch_apply_end", "success": True,
                    "changes": {"/tmp/a.py": {}}, "call_id": "exec-1"},
    }) == []
    assert route_record({
        "type": "event_msg",
        "payload": {"type": "web_search_end", "query": "q", "call_id": "exec-2"},
    }) == []

