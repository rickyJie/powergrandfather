"""Unit tests for the session-changes JSONL parser.

Covers the three tool shapes (Edit / Write / MultiEdit), the aggregation
into per-file summaries, and the tolerance for malformed / partial JSONL
lines (a corrupt line must never crash the endpoint).
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from csm.modules.session_manager.changes import (
    EditRecord,
    filter_by_path,
    find_codex_rollout,
    parse_codex_edits_from_rollout,
    parse_edits_from_jsonl,
    summarize_by_file,
)


def _write_jsonl(records: list[dict]) -> Path:
    fh = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for r in records:
        fh.write(json.dumps(r) + "\n")
    fh.close()
    return Path(fh.name)


def _asst_toolcall(name: str, inp: dict, *, ts: str = "2026-07-25T10:00:00Z", tuid: str = "toolu_x") -> dict:
    """Build one assistant JSONL record that contains a single tool_use block."""
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "tool_use", "id": tuid, "name": name, "input": inp}
            ]
        },
    }


def _write_codex_rollout(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def _codex_patch_call(call_id: str, patch: str, *, ts: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "call_id": call_id,
            "input": patch,
        },
    }


def _codex_patch_output(call_id: str, *, exit_code: int) -> dict:
    output = (
        f"Exit code: {exit_code}\n"
        + (
            "Success. Updated the following files:\nM src/app.py\n"
            if exit_code == 0
            else "Output:\nFailed to find expected lines in src/app.py\n"
        )
    )
    return {
        "type": "response_item",
        "timestamp": "2026-07-30T10:00:01Z",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


def _codex_patch_end(changes: dict, *, success: bool = True) -> dict:
    return {
        "type": "event_msg",
        "timestamp": "2026-08-01T07:48:22Z",
        "payload": {
            "type": "patch_apply_end",
            "call_id": "exec-structured",
            "success": success,
            "changes": changes,
        },
    }


def test_edit_tool_extracted_with_old_and_new():
    """Straight Edit: pull old_string + new_string into an EditRecord."""
    p = _write_jsonl([
        _asst_toolcall("Edit", {
            "file_path": "/tmp/foo.py",
            "old_string": "print('old')",
            "new_string": "print('new')",
        }),
    ])
    rows = parse_edits_from_jsonl(p)
    assert len(rows) == 1
    assert rows[0].tool == "Edit"
    assert rows[0].file_path == "/tmp/foo.py"
    assert rows[0].old == "print('old')"
    assert rows[0].new == "print('new')"
    assert rows[0].sub_index == 0


def test_write_tool_has_none_old_and_full_content_new():
    """Write is full-file replace; old=None so the diff renderer can label
    it as new-file (or diff against a prior Read if it feels like it)."""
    p = _write_jsonl([
        _asst_toolcall("Write", {
            "file_path": "/tmp/new.md",
            "content": "# Hello\n\nBody.\n",
        }),
    ])
    rows = parse_edits_from_jsonl(p)
    assert len(rows) == 1
    assert rows[0].tool == "Write"
    assert rows[0].old is None
    assert rows[0].new == "# Hello\n\nBody.\n"


def test_multiedit_expands_to_one_record_per_sub_edit_with_index():
    """A MultiEdit with 3 sub-edits produces 3 EditRecords, indexed 0,1,2."""
    p = _write_jsonl([
        _asst_toolcall("MultiEdit", {
            "file_path": "/tmp/multi.py",
            "edits": [
                {"old_string": "a", "new_string": "A"},
                {"old_string": "b", "new_string": "B"},
                {"old_string": "c", "new_string": "C"},
            ],
        }),
    ])
    rows = parse_edits_from_jsonl(p)
    assert len(rows) == 3
    assert [r.sub_index for r in rows] == [0, 1, 2]
    assert [r.old for r in rows] == ["a", "b", "c"]
    assert [r.new for r in rows] == ["A", "B", "C"]
    assert all(r.tool == "MultiEdit-sub" for r in rows)
    # tool_use_id shared across sub-edits — they came from one tool call
    assert len({r.tool_use_id for r in rows}) == 1


def test_non_editing_tools_and_user_records_filtered_out():
    """Read, Bash, user turns, file-history frames — none of these are edits."""
    p = _write_jsonl([
        # user prompt
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        # assistant with Read tool (should skip)
        _asst_toolcall("Read", {"file_path": "/tmp/x.py"}),
        # assistant with Bash tool (should skip)
        _asst_toolcall("Bash", {"command": "ls -la"}),
        # assistant with pure text (no tool_use)
        {
            "type": "assistant",
            "timestamp": "2026-07-25T11:00:00Z",
            "message": {"content": [{"type": "text", "text": "OK"}]},
        },
        # meta records with non-standard shapes
        {"type": "summary", "summary": "..."},
    ])
    rows = parse_edits_from_jsonl(p)
    assert rows == []


def test_malformed_lines_skipped_without_crashing():
    """A corrupt / truncated line must not break the whole endpoint —
    that would turn "one bad byte" into "changes panel is permanently
    broken for this session"."""
    # Write a mix of good + malformed + good lines by hand
    fh = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    fh.write(json.dumps(_asst_toolcall("Edit", {
        "file_path": "/tmp/before.py", "old_string": "x", "new_string": "y",
    })) + "\n")
    fh.write("{not valid json at all\n")
    fh.write("\n")  # blank line
    fh.write("null\n")  # valid JSON but not a dict → passes json.loads but skipped downstream
    fh.write(json.dumps(_asst_toolcall("Edit", {
        "file_path": "/tmp/after.py", "old_string": "a", "new_string": "b",
    })) + "\n")
    fh.close()

    rows = parse_edits_from_jsonl(Path(fh.name))
    # Both real edits survive; malformed and null lines dropped silently
    assert len(rows) == 2
    assert rows[0].file_path == "/tmp/before.py"
    assert rows[1].file_path == "/tmp/after.py"


def test_missing_jsonl_file_returns_empty_list():
    """Called on a session with no transcript on disk (claude pruned it,
    or the sid was never real) → empty list, not exception."""
    assert parse_edits_from_jsonl(Path("/tmp/definitely-does-not-exist-hjk.jsonl")) == []


def test_summarize_by_file_ranks_most_recent_first():
    """Most-recently-touched file should sort to the top of the panel."""
    records = [
        EditRecord(ts="2026-07-25T09:00:00Z", tool="Edit", file_path="/a", old="", new="1"),
        EditRecord(ts="2026-07-25T10:00:00Z", tool="Write", file_path="/b", old=None, new="2"),
        EditRecord(ts="2026-07-25T11:00:00Z", tool="Edit", file_path="/a", old="1", new="3"),
    ]
    summaries = summarize_by_file(records)
    assert [s.path for s in summaries] == ["/a", "/b"]  # /a wins on last_ts
    a = summaries[0]
    assert a.edit_count == 2
    assert a.tools == {"Edit"}
    assert a.first_ts == "2026-07-25T09:00:00Z"
    assert a.last_ts == "2026-07-25T11:00:00Z"


def test_summarize_tracks_mixed_tool_names_per_file():
    """A file touched by Write then Edit should show both tools in its badge set."""
    records = [
        EditRecord(ts="t1", tool="Write", file_path="/x", old=None, new="v1"),
        EditRecord(ts="t2", tool="Edit", file_path="/x", old="v1", new="v2"),
        EditRecord(ts="t3", tool="MultiEdit-sub", file_path="/x", old="v2", new="v3"),
    ]
    s = summarize_by_file(records)[0]
    assert s.tools == {"Write", "Edit", "MultiEdit-sub"}
    assert s.edit_count == 3


def test_filter_by_path_preserves_order_and_isolation():
    """filter_by_path returns only rows for the requested file, in original order."""
    records = [
        EditRecord(ts="t1", tool="Edit", file_path="/a", old="", new="1"),
        EditRecord(ts="t2", tool="Edit", file_path="/b", old="", new="2"),
        EditRecord(ts="t3", tool="Edit", file_path="/a", old="1", new="3"),
    ]
    a = filter_by_path(records, "/a")
    assert len(a) == 2
    assert [r.new for r in a] == ["1", "3"]
    b = filter_by_path(records, "/b")
    assert [r.new for r in b] == ["2"]
    assert filter_by_path(records, "/nonexistent") == []


def test_codex_apply_patch_extracts_successful_update_and_add(tmp_path):
    rollout = _write_codex_rollout(
        tmp_path / "rollout-test.jsonl",
        [
            _codex_patch_call(
                "call_ok",
                """*** Begin Patch
*** Update File: src/app.py
@@
 keep
-old
+new
*** Add File: notes.md
+# Title
+
+Body
*** End Patch""",
                ts="2026-07-30T10:00:00Z",
            ),
            _codex_patch_output("call_ok", exit_code=0),
        ],
    )

    edits = parse_codex_edits_from_rollout(rollout, cwd="/workspace")

    assert len(edits) == 2
    update, added = edits
    assert update.tool == "ApplyPatch"
    assert update.file_path == "/workspace/src/app.py"
    assert update.old == "keep\nold"
    assert update.new == "keep\nnew"
    assert update.tool_use_id == "call_ok"
    assert added.tool == "ApplyPatchAdd"
    assert added.file_path == "/workspace/notes.md"
    assert added.old == ""
    assert added.new == "# Title\n\nBody\n"


def test_codex_apply_patch_excludes_failed_and_inflight_calls(tmp_path):
    failed_patch = """*** Begin Patch
*** Update File: /workspace/failed.py
@@
-old
+new
*** End Patch"""
    pending_patch = """*** Begin Patch
*** Add File: /workspace/pending.py
+pending
*** End Patch"""
    rollout = _write_codex_rollout(
        tmp_path / "rollout-test.jsonl",
        [
            _codex_patch_call("call_failed", failed_patch, ts="2026-07-30T10:00:00Z"),
            _codex_patch_output("call_failed", exit_code=1),
            _codex_patch_call("call_pending", pending_patch, ts="2026-07-30T10:01:00Z"),
        ],
    )

    assert parse_codex_edits_from_rollout(rollout, cwd="/workspace") == []


def test_codex_apply_patch_preserves_move_and_delete_operations(tmp_path):
    rollout = _write_codex_rollout(
        tmp_path / "rollout-test.jsonl",
        [
            _codex_patch_call(
                "call_ops",
                """*** Begin Patch
*** Update File: old_name.py
*** Move to: new_name.py
@@
-old
+new
*** Delete File: obsolete.py
*** End Patch""",
                ts="2026-07-30T10:00:00Z",
            ),
            _codex_patch_output("call_ops", exit_code=0),
        ],
    )

    edits = parse_codex_edits_from_rollout(rollout, cwd="/workspace")

    assert [edit.tool for edit in edits] == [
        "ApplyPatchMove",
        "ApplyPatch",
        "ApplyPatchDelete",
    ]
    moved, updated, deleted = edits
    assert moved.source_path == "/workspace/old_name.py"
    assert moved.file_path == "/workspace/new_name.py"
    assert updated.source_path == "/workspace/old_name.py"
    assert updated.file_path == "/workspace/new_name.py"
    assert deleted.file_path == "/workspace/obsolete.py"
    assert deleted.old is None


def test_codex_structured_patch_end_extracts_update_add_and_delete(tmp_path):
    rollout = _write_codex_rollout(
        tmp_path / "rollout-structured.jsonl",
        [_codex_patch_end({
            "/workspace/src/app.py": {
                "type": "update",
                "unified_diff": "@@ -1,2 +1,2 @@\n keep\n-old\n+new\n",
                "move_path": None,
            },
            "/workspace/notes.md": {
                "type": "add",
                "content": "# Notes\n",
            },
            "/workspace/obsolete.txt": {
                "type": "delete",
                "content": "old body\n",
            },
        })],
    )

    edits = parse_codex_edits_from_rollout(rollout, cwd="/workspace")

    assert [edit.tool for edit in edits] == [
        "ApplyPatch",
        "ApplyPatchAdd",
        "ApplyPatchDelete",
    ]
    assert edits[0].old == "keep\nold"
    assert edits[0].new == "keep\nnew"
    assert edits[1].new == "# Notes\n"
    assert edits[2].old == "old body\n"


def test_codex_structured_patch_end_prevents_legacy_duplicate(tmp_path):
    patch = """*** Begin Patch
*** Update File: src/app.py
@@
-old
+new
*** End Patch"""
    rollout = _write_codex_rollout(
        tmp_path / "rollout-mixed.jsonl",
        [
            _codex_patch_call("legacy-call", patch, ts="2026-08-01T07:48:21Z"),
            _codex_patch_output("legacy-call", exit_code=0),
            _codex_patch_end({
                "/workspace/src/app.py": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                    "move_path": None,
                },
            }),
        ],
    )

    edits = parse_codex_edits_from_rollout(rollout, cwd="/workspace")

    assert len(edits) == 1
    assert edits[0].old == "old"
    assert edits[0].new == "new"


def test_find_codex_rollout_by_external_id(tmp_path):
    root = tmp_path / "sessions"
    expected = _write_codex_rollout(
        root / "2026/07/30/rollout-target.jsonl",
        [{
            "type": "session_meta",
            "timestamp": "2026-07-30T10:00:00Z",
            "payload": {
                "session_id": "codex-target",
                "cwd": "/workspace",
                "timestamp": "2026-07-30T10:00:00Z",
            },
        }],
    )
    _write_codex_rollout(
        root / "2026/07/30/rollout-other.jsonl",
        [{
            "type": "session_meta",
            "payload": {
                "session_id": "codex-other",
                "cwd": "/workspace",
                "timestamp": "2026-07-30T10:00:00Z",
            },
        }],
    )

    found = find_codex_rollout(
        root,
        external_session_id="codex-target",
        cwd="/workspace",
    )
    assert found == expected


def test_find_codex_rollout_legacy_timestamp_match_is_conservative(tmp_path):
    root = tmp_path / "sessions"

    def plant(name: str, user_ts: str) -> Path:
        return _write_codex_rollout(
            root / f"2026/07/30/{name}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": name,
                        "cwd": "/workspace",
                        # Simulate a resumed rollout whose original creation
                        # time cannot identify the newer CSM row.
                        "timestamp": "2026-07-29T08:00:00Z",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": user_ts,
                    "payload": {"type": "user_message", "message": "redacted"},
                },
            ],
        )

    expected = plant("rollout-near", "2026-07-30T10:00:20Z")
    plant("rollout-far", "2026-07-30T10:03:00Z")

    found = find_codex_rollout(
        root,
        external_session_id=None,
        cwd="/workspace",
        started_at=datetime.fromisoformat("2026-07-30T10:00:00"),
    )
    assert found == expected

    plant("rollout-ambiguous", "2026-07-30T10:00:21Z")
    ambiguous = find_codex_rollout(
        root,
        external_session_id=None,
        cwd="/workspace",
        started_at=datetime.fromisoformat("2026-07-30T10:00:00"),
    )
    assert ambiguous is None


# ---------------------------------------------------------------------------
# find_codex_rollout — SQLite fast-path (codex 0.145.0+ state DB)
# ---------------------------------------------------------------------------


def _seed_codex_state_db(home: Path, threads: list[tuple[str, str, str, int]]):
    """Materialize a minimal `<home>/state_5.sqlite` for tests.

    `threads` entries are (id, cwd, rollout_path, created_at_unix).
    """
    import sqlite3
    conn = sqlite3.connect(str(home / "state_5.sqlite"))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads ("
            "id TEXT PRIMARY KEY, cwd TEXT NOT NULL, "
            "rollout_path TEXT, created_at INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO threads(id, cwd, rollout_path, "
            "created_at) VALUES(?, ?, ?, ?)",
            threads,
        )
        conn.commit()
    finally:
        conn.close()


def test_find_codex_rollout_state_db_by_external_id_recovers_missing_jsonl(
    tmp_path,
):
    """When the Session row has ``external_session_id`` but no rollout
    file was ever flushed to disk (codex died fast after registering the
    thread), the SQLite fast-path returns the file it eventually wrote
    on the NEXT tick — we only trust the row when the file exists."""
    home = tmp_path
    sessions_root = home / "sessions"
    (sessions_root / "2026" / "08" / "05").mkdir(parents=True)
    # File exists on disk — codex flushed it later.
    real_path = (
        sessions_root / "2026" / "08" / "05" / "rollout-recovered.jsonl"
    )
    real_path.write_text("")  # empty is fine; existence is all we check
    _seed_codex_state_db(
        home,
        threads=[
            ("019recover", "/workspace", str(real_path), 1785000000),
        ],
    )
    found = find_codex_rollout(
        sessions_root,
        external_session_id="019recover",
        cwd="/workspace",
    )
    assert found == real_path


def test_find_codex_rollout_state_db_returns_none_when_file_missing(tmp_path):
    """State DB says X.jsonl but disk has no such file → we do NOT return
    a fake Path. Fall through to filesystem walk (which also returns None
    here) so caller sees a truthful 'nothing found'."""
    home = tmp_path
    sessions_root = home / "sessions"
    sessions_root.mkdir()
    _seed_codex_state_db(
        home,
        threads=[
            ("019phantom", "/workspace",
             "/really/does/not/exist.jsonl", 1785000000),
        ],
    )
    assert find_codex_rollout(
        sessions_root,
        external_session_id="019phantom",
        cwd="/workspace",
    ) is None


def test_find_codex_rollout_state_db_by_cwd_and_started_at(tmp_path):
    """No ``external_session_id`` — match by cwd + created_at window."""
    home = tmp_path
    sessions_root = home / "sessions"
    (sessions_root / "2026" / "08" / "05").mkdir(parents=True)
    real = (sessions_root / "2026" / "08" / "05" / "rollout-w.jsonl")
    real.write_text("")
    _seed_codex_state_db(
        home,
        threads=[
            # Well within the ±5min window centered on started_at.
            ("019match", "/workspace", str(real), 1785000030),
            # Same cwd, minutes outside the window → ignored.
            ("019stale", "/workspace", str(real),
             1785000000 - 400),
        ],
    )
    from datetime import UTC
    from datetime import datetime as _dt
    started_at = _dt.fromtimestamp(1785000000, tz=UTC).replace(tzinfo=None)
    assert find_codex_rollout(
        sessions_root,
        external_session_id=None,
        cwd="/workspace",
        started_at=started_at,
    ) == real


def test_find_codex_rollout_state_db_rejects_two_close_threads(tmp_path):
    """Ambiguity guard: two threads landed <2s apart → refuse to guess."""
    home = tmp_path
    sessions_root = home / "sessions"
    (sessions_root / "2026" / "08" / "05").mkdir(parents=True)
    real_a = sessions_root / "2026" / "08" / "05" / "a.jsonl"
    real_b = sessions_root / "2026" / "08" / "05" / "b.jsonl"
    real_a.write_text("")
    real_b.write_text("")
    _seed_codex_state_db(
        home,
        threads=[
            ("019twin-a", "/workspace", str(real_a), 1785000010),
            ("019twin-b", "/workspace", str(real_b), 1785000011),
        ],
    )
    from datetime import UTC
    from datetime import datetime as _dt
    started_at = _dt.fromtimestamp(1785000000, tz=UTC).replace(tzinfo=None)
    assert find_codex_rollout(
        sessions_root,
        external_session_id=None,
        cwd="/workspace",
        started_at=started_at,
    ) is None
