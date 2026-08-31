"""Regression tests for `CodexRolloutTailer`.

Focus on the two silent-data-loss bugs caught in backend review:

- M1: previously, a `session_meta` record with an empty `session_id`
  caused every subsequent record in the file to be silently dropped
  with no log. The fix logs a warning ONCE per file so the black hole
  is observable.
- M2: on file truncation, offset+line_no were reset but `codex_session_id`
  and `project_path` were kept from the pre-truncation session. Codex
  writing a new session into the same path would then get its events
  mis-attributed to the old session_id. The fix clears the ids too.

Plus baseline coverage: happy-path scan, snapshot/restore, partial-line
handling — none of which had unit tests before this change.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from csm.adapters.jsonl_tail import CodexRolloutTailer


def _rollout_dir(tmp_path: Path) -> Path:
    """Create an isolated sessions/YYYY/MM/DD-style tree under tmp_path."""
    d = tmp_path / "sessions" / "2026" / "07" / "25"
    d.mkdir(parents=True)
    return d


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _append_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Baseline: happy path
# ---------------------------------------------------------------------------


def test_scan_bootstraps_ids_from_session_meta(tmp_path):
    """First scan pulls session_id + cwd out of session_meta and attaches
    them to every subsequent record."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-abcdef.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "timestamp": "2026-07-25T10:00:00Z",
         "payload": {"session_id": "sess-real-uuid", "cwd": "/data/project"}},
        {"type": "event_msg", "timestamp": "2026-07-25T10:00:01Z",
         "payload": {"type": "user_message", "message": "hi"}},
    ])

    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    records = tailer.scan_once()

    assert len(records) == 2
    assert all(r.codex_session_id == "sess-real-uuid" for r in records)
    assert all(r.project_path == "/data/project" for r in records)
    assert records[0].line_no == 1
    assert records[1].line_no == 2


def test_scan_incremental_only_reads_new_bytes(tmp_path):
    """Second scan_once() only returns records appended since the first."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-inc.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "s1", "cwd": "/x"}},
    ])
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    first = tailer.scan_once()
    assert len(first) == 1

    _append_jsonl(rollout, [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "yo"}},
    ])
    second = tailer.scan_once()
    assert len(second) == 1
    assert second[0].codex_session_id == "s1"


def test_partial_line_not_consumed(tmp_path):
    """A trailing line without \\n is left for the next scan."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-partial.jsonl"
    complete = json.dumps({"type": "session_meta", "payload": {"session_id": "s", "cwd": "/x"}})
    partial = '{"type": "event_msg", "payload":'
    rollout.write_text(complete + "\n" + partial)

    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    first = tailer.scan_once()
    assert len(first) == 1  # only the terminated line

    # Now complete the partial line — should show up in second scan.
    with open(rollout, "a") as f:
        f.write(' {"type": "user_message", "message": "x"}}\n')
    second = tailer.scan_once()
    assert len(second) == 1


def test_snapshot_restore_roundtrips_bootstrap_ids(tmp_path):
    """Persisted state must include codex_session_id / project_path so a
    restart doesn't re-emit session_meta OR lose the id binding."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-snap.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "snap-id", "cwd": "/w"}},
    ])
    t1 = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    t1.scan_once()
    snap = t1.snapshot()

    t2 = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    t2.restore(snap)
    # Append a new record — it should attach to the restored id.
    _append_jsonl(rollout, [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "post-restart"}},
    ])
    records = t2.scan_once()
    assert len(records) == 1
    assert records[0].codex_session_id == "snap-id"


# ---------------------------------------------------------------------------
# M1: empty session_id must emit a warning, not silently blackhole
# ---------------------------------------------------------------------------


def test_empty_session_id_emits_warning_once_and_skips(tmp_path, caplog):
    """session_meta with session_id='' → warning logged ONCE per file, all
    records skipped (not emitted with empty id)."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-empty-id.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "", "cwd": "/x"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "lost1"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "lost2"}},
    ])

    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    with caplog.at_level(logging.WARNING, logger="csm.adapters.jsonl_tail"):
        records = tailer.scan_once()

    # All three records were dropped (session_meta doesn't emit if id blank).
    assert records == []
    # Exactly one warning per file.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no session_id" in warnings[0].getMessage()
    assert "rollout-empty-id" in warnings[0].getMessage()


def test_empty_session_id_does_not_re_warn_on_subsequent_scans(tmp_path, caplog):
    """Even if more records are appended, warning must not repeat."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-empty-persist.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "", "cwd": "/x"}},
    ])
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    with caplog.at_level(logging.WARNING, logger="csm.adapters.jsonl_tail"):
        tailer.scan_once()
        _append_jsonl(rollout, [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "still-lost"}},
        ])
        tailer.scan_once()
        tailer.scan_once()
    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1


# ---------------------------------------------------------------------------
# M2: truncation must reset session id + project path, not just offset
# ---------------------------------------------------------------------------


def test_truncation_resets_bootstrap_ids(tmp_path):
    """When the tailer's `st.st_size < fs.offset` truncation guard fires,
    it MUST also drop the cached session_id and project_path — otherwise
    any new session_meta written into the shrunken file gets ignored (the
    `if not fs.codex_session_id` bootstrap check is False) and the new
    session's records leak the previous session's id."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-rot.jsonl"

    # Session 1 — write LARGE payload so we can shrink the file and trigger
    # the size-based truncation detection.
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "OLD-id", "cwd": "/proj/old"}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "x" * 4000}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "y" * 4000}},
    ])
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    first = tailer.scan_once()
    assert all(r.codex_session_id == "OLD-id" for r in first)

    # Truncate + rewrite as Session 2 (same file path, SMALLER content so
    # `st_size < offset` fires and the truncation guard resets state).
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "NEW-id", "cwd": "/proj/new"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "new-msg"}},
    ])
    second = tailer.scan_once()

    # Both records from the new session must carry the NEW session id + cwd.
    assert len(second) == 2, f"expected 2 records after rotate, got {len(second)}: {second}"
    assert all(r.codex_session_id == "NEW-id" for r in second), \
        f"post-rotation records leaked OLD-id: {[r.codex_session_id for r in second]}"
    assert all(r.project_path == "/proj/new" for r in second)


def test_truncation_clears_warned_empty_id_flag(tmp_path, caplog):
    """After a detectable truncation, an empty-id file must re-warn
    (fresh session == fresh observability), not stay silent because we
    warned about the previous session."""
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-rot-empty.jsonl"

    # LARGE first write so the subsequent smaller write triggers the
    # size-shrink truncation guard.
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "", "cwd": "/x"}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "pad" * 2000}},
    ])
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    with caplog.at_level(logging.WARNING, logger="csm.adapters.jsonl_tail"):
        tailer.scan_once()
        _write_jsonl(rollout, [
            {"type": "session_meta", "payload": {"session_id": "", "cwd": "/y"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "z"}},
        ])
        tailer.scan_once()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, \
        "expected fresh warning after truncation, got: " + str([w.getMessage() for w in warnings])


# ---------------------------------------------------------------------------
# Robustness: malformed JSON lines are skipped, not fatal
# ---------------------------------------------------------------------------


def test_malformed_json_line_is_skipped(tmp_path):
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-bad.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"session_id": "s", "cwd": "/x"}}) + "\n"
        + "{this is not json\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "ok"}}) + "\n"
    )
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    records = tailer.scan_once()
    # session_meta + the recoverable event_msg, but NOT the malformed line.
    assert len(records) == 2
    assert records[-1].obj["payload"]["message"] == "ok"


def test_newly_seen_paths_reported_once(tmp_path):
    d = _rollout_dir(tmp_path)
    rollout = d / "rollout-newseen.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"session_id": "s", "cwd": "/x"}},
    ])
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    tailer.scan_once()
    seen1 = tailer.take_newly_seen()
    assert str(rollout) in seen1

    tailer.scan_once()
    seen2 = tailer.take_newly_seen()
    assert seen2 == set()  # already known, not re-reported


@pytest.mark.parametrize("bootstrap_key", ["session_id", "id"])
def test_bootstrap_accepts_either_session_id_or_id_field(tmp_path, bootstrap_key):
    """Codex has used both `session_id` and `id` in session_meta payloads
    across versions; the tailer accepts either."""
    d = _rollout_dir(tmp_path)
    rollout = d / f"rollout-{bootstrap_key}.jsonl"
    _write_jsonl(rollout, [
        {"type": "session_meta", "payload": {bootstrap_key: "either-works", "cwd": "/x"}},
    ])
    tailer = CodexRolloutTailer(sessions_root=tmp_path / "sessions")
    records = tailer.scan_once()
    assert len(records) == 1
    assert records[0].codex_session_id == "either-works"
