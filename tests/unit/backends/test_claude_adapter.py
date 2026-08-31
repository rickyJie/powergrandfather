"""Unit tests for `ClaudeAdapter`.

Covers:
- Protocol conformance
- Capabilities set correctly
- pre_spawn_session_id returns a valid uuid; post_spawn_bind is a no-op
- build_argv:
    - claude path: --session-id + --dangerously-skip-permissions injected
    - non-claude argv[0] (bash): strict pass-through
    - resume_from replaces --session-id with --resume
    - hooks_base_url injects --settings JSON
    - extra_args land before prompt
    - initial_prompt is the last positional
- scan_events: SESSION_STARTED for newly-seen files, gates on msg_count>0
- probe: returns installed=False when `claude` not on PATH (using monkeypatch)
"""
from __future__ import annotations

import json
import uuid

import pytest
from csm.backends.base import Capability
from csm.backends.claude.adapter import ClaudeAdapter
from csm.core.events import EventType

from tests.unit.backends._fake_adapter import assert_conforms

# ---------------------------------------------------------------------------
# Protocol + identity
# ---------------------------------------------------------------------------


def test_claude_adapter_conforms_to_protocol():
    assert_conforms(ClaudeAdapter())


def test_claude_adapter_identity_fields():
    a = ClaudeAdapter()
    assert a.name == "claude"
    assert a.display_name == "Claude Code"
    assert isinstance(a.icon, str) and a.icon
    assert Capability.PRE_SPAWN_SESSION_ID in a.capabilities
    assert Capability.HOOKS in a.capabilities
    assert Capability.INTERACTIVE_STREAM in a.capabilities
    assert Capability.RESUME_SESSION in a.capabilities
    assert Capability.POST_SPAWN_BIND not in a.capabilities


def test_default_home_name_is_dot_claude():
    assert ClaudeAdapter().default_home_name() == ".claude"


# ---------------------------------------------------------------------------
# Session-id lifecycle
# ---------------------------------------------------------------------------


def test_pre_spawn_session_id_returns_valid_uuid():
    sid = ClaudeAdapter().pre_spawn_session_id(cwd="/tmp")
    # Must parse as a uuid
    parsed = uuid.UUID(sid)
    assert str(parsed) == sid


def test_pre_spawn_session_id_is_unique_each_call():
    a = ClaudeAdapter()
    ids = {a.pre_spawn_session_id("/tmp") for _ in range(10)}
    assert len(ids) == 10


def test_post_spawn_bind_is_noop_returns_none():
    """Claude bakes id pre-spawn; post_spawn_bind must be a no-op."""
    assert ClaudeAdapter().post_spawn_bind("row-id", "/tmp") is None


# ---------------------------------------------------------------------------
# build_argv — claude path
# ---------------------------------------------------------------------------


def test_build_argv_injects_session_id_and_dangerous_skip():
    a = ClaudeAdapter()
    sid = a.pre_spawn_session_id("/tmp")
    result = a.build_argv(
        base_argv=["claude"],
        cwd="/tmp",
        session_id=sid,
    )
    assert "--session-id" in result.argv
    assert sid in result.argv
    assert "--dangerously-skip-permissions" in result.argv
    assert result.session_id == sid
    assert result.prompt_appended is False


def test_build_argv_appends_initial_prompt_positionally():
    a = ClaudeAdapter()
    result = a.build_argv(
        base_argv=["claude"],
        cwd="/tmp",
        session_id="fixed-sid",
        initial_prompt="review this PR",
    )
    assert result.argv[-1] == "review this PR"
    assert result.prompt_appended is True


def test_build_argv_extras_land_before_prompt():
    a = ClaudeAdapter()
    result = a.build_argv(
        base_argv=["claude"],
        cwd="/tmp",
        session_id="s",
        initial_prompt="do stuff",
        extra_args=["--disallowedTools", "Skill"],
    )
    argv = result.argv
    # prompt is last
    assert argv[-1] == "do stuff"
    # extras present, and before prompt
    assert "--disallowedTools" in argv
    assert argv.index("--disallowedTools") < argv.index("do stuff")


def test_build_argv_hooks_url_injects_settings():
    a = ClaudeAdapter()
    result = a.build_argv(
        base_argv=["claude"],
        cwd="/tmp",
        session_id="s",
        hooks_base_url="http://127.0.0.1:8000",
    )
    argv = result.argv
    assert "--settings" in argv
    idx = argv.index("--settings")
    # settings value is the next arg, and it's JSON with a `hooks` key
    settings_json = argv[idx + 1]
    parsed = json.loads(settings_json)
    assert "hooks" in parsed
    assert "SessionStart" in parsed["hooks"]


def test_build_argv_resume_from_uses_resume_flag():
    a = ClaudeAdapter()
    result = a.build_argv(
        base_argv=["claude"],
        cwd="/tmp",
        resume_from="old-sid-abc",
    )
    argv = result.argv
    assert "--resume" in argv
    assert "old-sid-abc" in argv
    # --session-id must NOT be injected on resume (mutually exclusive)
    assert "--session-id" not in argv
    assert result.session_id == "old-sid-abc"


def test_build_argv_respects_existing_user_resume_flags():
    """User already passed --resume; don't also inject --session-id."""
    a = ClaudeAdapter()
    result = a.build_argv(
        base_argv=["claude", "--resume", "user-sid"],
        cwd="/tmp",
        session_id="freshly-generated",  # should be ignored
    )
    assert "--session-id" not in result.argv


# ---------------------------------------------------------------------------
# build_argv — non-claude pass-through
# ---------------------------------------------------------------------------


def test_build_argv_bash_is_strict_passthrough():
    a = ClaudeAdapter()
    result = a.build_argv(
        base_argv=["bash", "-i"],
        cwd="/tmp",
        session_id="freshly-generated",
        initial_prompt="secret",
    )
    assert result.argv == ["bash", "-i"]
    assert result.prompt_appended is False
    # No claude-only flags leaked
    assert "--session-id" not in result.argv
    assert "--dangerously-skip-permissions" not in result.argv


def test_build_argv_empty_returns_empty():
    a = ClaudeAdapter()
    result = a.build_argv(base_argv=[], cwd="/tmp", session_id="s", initial_prompt="p")
    assert result.argv == []


# ---------------------------------------------------------------------------
# scan_events — SESSION_STARTED gating
# ---------------------------------------------------------------------------


def test_scan_events_no_files_yields_nothing(monkeypatch, tmp_path):
    """Empty projects dir → no events."""
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(tmp_path))
    (tmp_path / "projects").mkdir()
    a = ClaudeAdapter()
    assert a.scan_events() == []


def test_scan_events_emits_session_started_only_after_first_record(
    monkeypatch, tmp_path,
):
    """SESSION_STARTED must NOT fire for a freshly-touched empty JSONL.

    First scan sees the file but nothing parsed — no event. Second scan
    after a real record is written — SESSION_STARTED emitted, plus
    whatever events derive from the record.
    """
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(tmp_path))
    projects = tmp_path / "projects"
    projects.mkdir()
    proj_dir = projects / "-tmp-x"
    proj_dir.mkdir()
    jsonl = proj_dir / "test-sid.jsonl"
    jsonl.write_text("")  # empty

    a = ClaudeAdapter()
    events = a.scan_events()
    assert events == []  # file seen but no records

    # Now append a user message
    record = {
        "message": {"role": "user", "content": "hi"},
        "timestamp": "2026-07-25T10:00:00Z",
    }
    with jsonl.open("a") as f:
        f.write(json.dumps(record) + "\n")

    events = a.scan_events()
    types = [e.type for e in events]
    # SESSION_STARTED fires now that msg_count > 0
    assert EventType.SESSION_STARTED in types
    assert EventType.MESSAGE_USER_SENT in types
    started = next(e for e in events if e.type == EventType.SESSION_STARTED)
    assert started.session_id == "test-sid"
    assert started.payload.get("backend") == "claude"


def test_snapshot_restore_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(tmp_path))
    projects = tmp_path / "projects"
    (projects / "-p").mkdir(parents=True)
    jsonl = projects / "-p" / "sid.jsonl"
    jsonl.write_text(json.dumps(
        {"message": {"role": "user", "content": "hi"}}
    ) + "\n")

    a1 = ClaudeAdapter()
    _ = a1.scan_events()
    snap = a1.snapshot()
    assert str(jsonl) in snap

    a2 = ClaudeAdapter()
    a2.restore(snap)
    # After restore, another scan with no new bytes should be a no-op
    assert a2.scan_events() == []


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_returns_installed_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(
        "csm.backends.claude.adapter.shutil.which",
        lambda name: None,
    )
    st = ClaudeAdapter().probe()
    assert st.installed is False
    assert st.authenticated is False
    assert st.error and "PATH" in st.error


def test_probe_capabilities_exposed():
    """probe() must include the adapter's capabilities in the status."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            "csm.backends.claude.adapter.shutil.which",
            lambda name: None,
        )
        st = ClaudeAdapter().probe()
        assert Capability.HOOKS in st.capabilities
        assert Capability.PRE_SPAWN_SESSION_ID in st.capabilities
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# PTY input framing
#
# Reported symptom: several long messages sent from the phone in a row appeared
# to send — no error, no reply — and then a later short "hi" caused ALL of them
# to arrive at once. That is not a lost write: the text reached claude's
# composer and sat there unsubmitted. A large burst trips claude's own
# paste-detection, so the trailing CR is inserted as literal content instead of
# acting as Enter; "hi" was small enough to be read as typing, and its CR
# finally submitted the whole accumulated buffer.
# ---------------------------------------------------------------------------

def test_short_single_line_message_is_still_typed():
    """The path that works today must not change — its blast radius is every
    message, not just the long ones."""
    assert ClaudeAdapter().frame_pty_input_sequence("hello") == [b"hello\r\n"]


def test_a_long_message_is_pasted_and_submitted_separately():
    body = "x" * 500
    seq = ClaudeAdapter().frame_pty_input_sequence(body)

    # Bracketed paste — the literal bytes a terminal emulator sends on paste,
    # and what produces claude's "[Pasted text #1 +N lines]" chip.
    assert seq[0] == b"\x1b[200~" + body.encode() + b"\x1b[201~"
    # The submit rides in its OWN write so the CLI reads it alone: after
    # ESC[201~ the paste is unambiguously over and a lone CR can only be Enter.
    assert seq[1] == b"\r"
    assert len(seq) == 2


def test_a_multiline_message_is_pasted_however_short():
    """Length is not the only trigger — a newline inside a typed burst is
    itself a submit, which would chop one message into several."""
    seq = ClaudeAdapter().frame_pty_input_sequence("line one\nline two")

    assert seq[0].startswith(b"\x1b[200~")
    assert seq[-1] == b"\r"


def test_the_paste_envelope_never_wraps_the_submit():
    """If the CR landed inside the envelope it would be pasted as text — which
    is exactly the bug this fixes, just spelled differently."""
    seq = ClaudeAdapter().frame_pty_input_sequence("y" * 500)

    assert b"\r" not in seq[0]
    assert seq[0].endswith(b"\x1b[201~")


def test_frame_pty_input_stays_the_flattened_view():
    """Callers that only want bytes keep working; the split is additive."""
    a = ClaudeAdapter()
    for text in ("hi", "z" * 500, "a\nb"):
        assert a.frame_pty_input(text) == b"".join(a.frame_pty_input_sequence(text))


def test_utf8_is_measured_in_bytes_not_characters():
    """A CJK message is ~3 bytes per character, so a character-count threshold
    would let a burst three times the intended size through as typing."""
    text = "中" * 100          # 100 chars, 300 bytes
    seq = ClaudeAdapter().frame_pty_input_sequence(text)

    assert seq[0].startswith(b"\x1b[200~")
    assert seq[-1] == b"\r"
