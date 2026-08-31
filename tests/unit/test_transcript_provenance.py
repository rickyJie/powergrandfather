"""Provenance predicates: who wrote this record, and who spawned this file.

Both answers drive refusals elsewhere (don't attribute text to the user, don't
adopt this transcript onto a session row), so the "can't tell" direction is
part of the contract and is asserted here explicitly.
"""

from __future__ import annotations

import json

from csm.core.transcript_provenance import is_headless_transcript, is_injected_user_record

# ── is_injected_user_record ─────────────────────────────────────────────────


def test_typed_message_is_not_injected():
    assert (
        is_injected_user_record(
            {"origin": {"kind": "human"}, "promptSource": "typed"}, "hi"
        )
        is False
    )


def test_queued_message_is_not_injected():
    # Text typed while a turn was still running — still the human's words.
    assert is_injected_user_record({"promptSource": "queued"}, "hi") is False


def test_record_with_no_provenance_is_not_injected():
    # CLI 2.1.112 wrote no provenance at all on ordinary typed messages, and
    # tool-result records carry none either. Treating "absent" as injected
    # would reclassify a whole release's history.
    assert is_injected_user_record({}, "hi") is False


def test_task_notification_is_injected():
    assert (
        is_injected_user_record(
            {"origin": {"kind": "task-notification"}, "promptSource": "system"}, "x"
        )
        is True
    )


def test_sdk_prompt_is_injected():
    # A headless `claude -p` prompt — CSM's own alert helper, a cron skill.
    assert is_injected_user_record({"promptSource": "sdk"}, "Rule that fired:") is True


def test_unknown_origin_kind_is_injected():
    # Default-deny: a kind that doesn't exist yet must not read as human.
    assert is_injected_user_record({"origin": {"kind": "future-thing"}}, "x") is True


def test_meta_and_compact_summary_are_injected():
    assert is_injected_user_record({"isMeta": True}, "skill preamble") is True
    assert is_injected_user_record({"isCompactSummary": True}, "recap") is True


def test_interrupt_marker_is_injected():
    assert is_injected_user_record({}, "[Request interrupted by user]") is True
    assert is_injected_user_record({"interruptedMessageId": "abc"}, "") is True


def test_quoting_the_interrupt_marker_stays_human():
    # Full-string equality, not a prefix — a real message that merely mentions
    # the marker is still something the user typed.
    assert (
        is_injected_user_record({}, "[Request interrupted by user] — why?") is False
    )


# ── is_headless_transcript ──────────────────────────────────────────────────


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_sdk_cli_transcript_is_headless(tmp_path):
    p = _write(
        tmp_path / "t.jsonl",
        [
            {"type": "queue-operation", "sessionId": "s"},
            {"type": "user", "entrypoint": "sdk-cli", "message": {"role": "user"}},
        ],
    )
    assert is_headless_transcript(p) is True


def test_interactive_cli_transcript_is_not_headless(tmp_path):
    p = _write(
        tmp_path / "t.jsonl",
        [{"type": "user", "entrypoint": "cli", "message": {"role": "user"}}],
    )
    assert is_headless_transcript(p) is False


def test_first_entrypoint_decides(tmp_path):
    # A `cli` session that later spawns something is still a cli session; the
    # first entrypoint seen is the one that owns the file.
    p = _write(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "entrypoint": "cli"},
            {"type": "user", "entrypoint": "sdk-cli"},
        ],
    )
    assert is_headless_transcript(p) is False


def test_missing_file_is_not_headless(tmp_path):
    assert is_headless_transcript(tmp_path / "nope.jsonl") is False


def test_transcript_without_entrypoint_is_not_headless(tmp_path):
    # Older records carry no entrypoint. "Don't know" must not read as headless
    # — callers use a True to REFUSE, and refusing on a guess would break
    # rotation recovery on old history.
    p = _write(tmp_path / "t.jsonl", [{"type": "user", "message": {"role": "user"}}])
    assert is_headless_transcript(p) is False


def test_truncated_leading_line_is_not_headless(tmp_path):
    # A half-written line at the head: nothing after it is trustworthy either.
    p = tmp_path / "t.jsonl"
    p.write_text('{"type": "user", "entry')
    assert is_headless_transcript(p) is False


def test_empty_file_is_not_headless(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("")
    assert is_headless_transcript(p) is False
