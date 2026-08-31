"""Unit tests for `CodexAdapter`.

Covers:
- Protocol conformance + identity + capabilities
- pre_spawn_session_id is None; post_spawn_bind reads session_meta from rollout
- build_argv delegates to spawners.build_codex_argv (via composition)
- scan_events wraps CodexRolloutTailer + derive_codex_events
- probe detects missing binary / auth
"""
from __future__ import annotations

import json

from csm.backends.base import Capability
from csm.backends.codex.adapter import CodexAdapter, _read_first_session_meta
from csm.core.events import EventType

from tests.unit.backends._fake_adapter import assert_conforms

# ---------------------------------------------------------------------------
# Protocol + identity
# ---------------------------------------------------------------------------


def test_codex_adapter_conforms_to_protocol():
    assert_conforms(CodexAdapter())


def test_codex_adapter_identity_fields():
    a = CodexAdapter()
    assert a.name == "codex"
    assert a.display_name == "Codex CLI"
    assert Capability.POST_SPAWN_BIND in a.capabilities
    assert Capability.INTERACTIVE_STREAM in a.capabilities
    assert Capability.RESUME_SESSION in a.capabilities
    # Explicitly NOT PRE_SPAWN_SESSION_ID and NOT HOOKS yet
    assert Capability.PRE_SPAWN_SESSION_ID not in a.capabilities
    assert Capability.HOOKS not in a.capabilities


def test_default_home_name_is_dot_codex():
    assert CodexAdapter().default_home_name() == ".codex"


# ---------------------------------------------------------------------------
# Session-id lifecycle
# ---------------------------------------------------------------------------


def test_pre_spawn_session_id_returns_none():
    """Codex has no --session-id; pre_spawn hook must return None so
    SessionManager knows to skip argv injection."""
    assert CodexAdapter().pre_spawn_session_id(cwd="/tmp") is None


def test_post_spawn_bind_finds_session_id_from_new_rollout(
    monkeypatch, tmp_path,
):
    """A rollout file that appears after spawn with matching cwd → its
    session_id is returned."""
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions" / "2026" / "07" / "25"
    sessions.mkdir(parents=True)

    a = CodexAdapter()
    # Shorten timeout so the test finishes fast
    a._BIND_TIMEOUT_SEC = 1.0
    a._BIND_POLL_SEC = 0.05

    # Write the rollout file BEFORE calling bind (so it appears within
    # the poll window without threading — bind seeds pre-existing files
    # as "seen", so the file we write here is NOT seen. But the loop only
    # ever sees "new files vs seed". We need to write AFTER the seed pass.
    # Simpler: patch _read_first_session_meta directly and confirm the
    # scan detects new files.
    #
    # Actually the cleanest approach: call bind with the file NOT yet
    # present, spawn a Timer thread that writes it. Let's do that.
    import threading
    def _write_late():
        import time as _t
        _t.sleep(0.1)
        (sessions / "rollout-x.jsonl").write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"session_id": "codex-sid-abc", "cwd": "/tmp/proj"},
            }) + "\n"
        )
    threading.Timer(0.05, _write_late).start()

    result = a.post_spawn_bind(session_row_id="row-1", cwd="/tmp/proj")
    # M10.A2: post_spawn_bind now returns a PostSpawnBindResult with both
    # external_session_id and artifact_path so SessionManager can persist
    # both. Previously returned just the id string.
    assert result is not None
    assert result.external_session_id == "codex-sid-abc"
    assert result.artifact_path is not None
    assert result.artifact_path.endswith("rollout-x.jsonl")


def test_post_spawn_bind_finds_rollout_created_before_background_task(
    monkeypatch, tmp_path,
):
    """Regression: baseline is captured before spawn, not when bind starts."""
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.3
    a._BIND_POLL_SEC = 0.01

    a.prepare_post_spawn_bind("row-fast")
    rollout = sessions / "rollout-fast.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"session_id": "codex-fast", "cwd": "/tmp/proj"},
    }) + "\n")

    result = a.post_spawn_bind("row-fast", "/tmp/proj")
    assert result is not None
    assert result.external_session_id == "codex-fast"
    assert result.artifact_path == str(rollout)


def test_post_spawn_bind_finds_existing_rollout_that_grows(
    monkeypatch, tmp_path,
):
    """Regression: Codex may continue a legacy thread in an old rollout.

    The old binder remembered only path names and therefore ignored the
    reused file forever. Size/mtime baselines must treat post-spawn growth as
    the active artifact.
    """
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-existing.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "session_id": "codex-existing",
            "cwd": "/tmp/proj",
        },
    }) + "\n")

    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.3
    a._BIND_POLL_SEC = 0.01
    a.prepare_post_spawn_bind("row-existing")

    # Simulate the freshly spawned Codex process reopening and appending to
    # the pre-existing rollout.
    with rollout.open("a") as f:
        f.write(json.dumps({
            "type": "event_msg",
            "payload": {"type": "task_started"},
        }) + "\n")

    result = a.post_spawn_bind("row-existing", "/tmp/proj")
    assert result is not None
    assert result.external_session_id == "codex-existing"
    assert result.artifact_path == str(rollout)


def test_post_spawn_bind_ignores_wrong_cwd(monkeypatch, tmp_path):
    """A rollout with the wrong cwd is not our session — return None (timeout)."""
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.3
    a._BIND_POLL_SEC = 0.05

    # Pre-existing rollout is skipped (seed).
    (sessions / "rollout-old.jsonl").write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"session_id": "old-sid", "cwd": "/other/proj"},
        }) + "\n"
    )
    result = a.post_spawn_bind(session_row_id="row-1", cwd="/tmp/proj")
    # Timeout → None
    assert result is None


def test_post_spawn_bind_times_out_when_no_rollout(monkeypatch, tmp_path):
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.2
    a._BIND_POLL_SEC = 0.05
    assert a.post_spawn_bind(session_row_id="row-1", cwd="/x") is None


# ---------------------------------------------------------------------------
# SQLite fast-path (codex 0.145.0+ writes threads/rollout LAZILY — the
# state DB row usually appears before the jsonl gets flushed to disk).
# ---------------------------------------------------------------------------


def _seed_state_db(home, *, threads: list[tuple[str, str, str, int]]):
    """Populate a minimal `~/.codex/state_5.sqlite` for tests.

    `threads` items are (id, cwd, rollout_path, created_at_unix). Only the
    columns actually queried by our fast-path are required — codex's real
    schema has many more. Callable repeatedly on the same tmp_path (mimics
    codex incrementally inserting rows during a bind race).
    """
    import sqlite3
    db = home / "state_5.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads ("
            "id TEXT PRIMARY KEY, cwd TEXT NOT NULL, "
            "rollout_path TEXT, created_at INTEGER NOT NULL)"
        )
        for tid, cwd, rpath, created in threads:
            conn.execute(
                "INSERT OR REPLACE INTO threads(id, cwd, rollout_path, "
                "created_at) VALUES(?, ?, ?, ?)",
                (tid, cwd, rpath, created),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def test_post_spawn_bind_sqlite_fastpath_hits_new_thread(monkeypatch, tmp_path):
    """State-DB row inserted AFTER baseline is captured wins without any
    rollout jsonl on disk — this is the whole point of the fix."""
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()

    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 1.0
    a._BIND_POLL_SEC = 0.05

    # Baseline captured with an empty state DB.
    _seed_state_db(tmp_path, threads=[])
    a.prepare_post_spawn_bind("row-sql")

    # Simulate codex inserting a `threads` row shortly after spawn. Note
    # the rollout_path points to a file that DOES NOT exist on disk —
    # exactly the situation post_spawn_bind used to fail on.
    import threading
    import time as _time
    def _insert_late():
        _time.sleep(0.1)
        # Recreate the DB with one new row (mimics codex writing).
        _seed_state_db(
            tmp_path,
            threads=[("019new-thread-id", "/tmp/proj",
                      "/nonexistent/rollout-late.jsonl", 1785000000)],
        )
    threading.Timer(0.05, _insert_late).start()

    result = a.post_spawn_bind("row-sql", "/tmp/proj")
    assert result is not None
    assert result.external_session_id == "019new-thread-id"
    assert result.artifact_path == "/nonexistent/rollout-late.jsonl"


def test_post_spawn_bind_sqlite_baseline_excludes_prior_thread(
    monkeypatch, tmp_path,
):
    """Rows that already existed at baseline time must be ignored even if
    they match cwd — otherwise we'd bind to a totally unrelated session."""
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()

    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.3
    a._BIND_POLL_SEC = 0.05

    _seed_state_db(
        tmp_path,
        threads=[
            ("019old-thread", "/tmp/proj", "/tmp/proj/old.jsonl", 1780000000),
        ],
    )
    a.prepare_post_spawn_bind("row-nothing-new")

    # No new row is ever inserted — bind must time out (return None) rather
    # than latch on to 019old-thread.
    assert a.post_spawn_bind("row-nothing-new", "/tmp/proj") is None


def test_post_spawn_bind_sqlite_ignores_wrong_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.3
    a._BIND_POLL_SEC = 0.05

    a.prepare_post_spawn_bind("row-1")
    # New thread inserted but under a different cwd — must NOT match.
    _seed_state_db(
        tmp_path,
        threads=[("019elsewhere", "/other/proj",
                  "/other/proj/r.jsonl", 1785000000)],
    )
    assert a.post_spawn_bind("row-1", "/tmp/proj") is None


def test_codex_state_db_paths_prefers_highest_version(tmp_path):
    """When multiple `state_N.sqlite` files exist (mid-migration), the
    highest N wins — that's the schema the current codex build writes."""
    from csm.backends.codex.adapter import _codex_state_db_paths
    (tmp_path / "state_3.sqlite").write_bytes(b"")
    (tmp_path / "state_5.sqlite").write_bytes(b"")
    (tmp_path / "state_10.sqlite").write_bytes(b"")
    (tmp_path / "state_x.sqlite").write_bytes(b"")  # not a version match
    (tmp_path / "unrelated.sqlite").write_bytes(b"")
    ordered = _codex_state_db_paths(tmp_path)
    assert [p.name for p in ordered] == [
        "state_10.sqlite", "state_5.sqlite", "state_3.sqlite",
    ]


def test_codex_state_db_paths_missing_home(tmp_path):
    """No DB → empty list, not an exception. Callers rely on this."""
    from csm.backends.codex.adapter import _codex_state_db_paths
    assert _codex_state_db_paths(tmp_path / "does-not-exist") == []
    assert _codex_state_db_paths(tmp_path) == []


def test_post_spawn_bind_sqlite_prefers_earliest_new_thread(
    monkeypatch, tmp_path,
):
    """Competing codex process starts AFTER ours — its row has a LATER
    created_at, so it sorts first in a DESC query. Our binder must still
    pick OUR thread (the earlier post-baseline row) rather than latching
    on to the newest visible row.
    """
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()

    a = CodexAdapter()
    a._BIND_TIMEOUT_SEC = 0.3
    a._BIND_POLL_SEC = 0.05

    _seed_state_db(tmp_path, threads=[])
    a.prepare_post_spawn_bind("row-race")

    # Two post-baseline rows: ours (t=100) and a later interloper (t=200).
    _seed_state_db(
        tmp_path,
        threads=[
            ("019ours", "/tmp/proj", "/x/ours.jsonl", 1785000100),
            ("019later-interloper", "/tmp/proj",
             "/x/other.jsonl", 1785000200),
        ],
    )

    result = a.post_spawn_bind("row-race", "/tmp/proj")
    assert result is not None
    assert result.external_session_id == "019ours"


# ---------------------------------------------------------------------------
# _read_first_session_meta helper
# ---------------------------------------------------------------------------


def test_read_first_session_meta_happy_path(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(json.dumps(
        {"type": "session_meta",
         "payload": {"session_id": "abc", "cwd": "/x"}}
    ) + "\n")
    assert _read_first_session_meta(f, expected_cwd="/x") == "abc"


def test_read_first_session_meta_wrong_cwd_returns_none(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(json.dumps(
        {"type": "session_meta",
         "payload": {"session_id": "abc", "cwd": "/other"}}
    ) + "\n")
    assert _read_first_session_meta(f, expected_cwd="/x") is None


def test_read_first_session_meta_partial_line_returns_none(tmp_path):
    """Writer mid-flush — no trailing \\n. Must not consume the partial line."""
    f = tmp_path / "r.jsonl"
    f.write_text('{"type":"session_meta","payload":{"session_id":"x"}')
    assert _read_first_session_meta(f) is None


def test_read_first_session_meta_wrong_top_type_returns_none(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(json.dumps({"type": "event_msg", "payload": {}}) + "\n")
    assert _read_first_session_meta(f) is None


def test_read_first_session_meta_bad_json_returns_none(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text("{not json\n")
    assert _read_first_session_meta(f) is None


def test_read_first_session_meta_accepts_id_field(tmp_path):
    """Some codex versions use `id` instead of `session_id`."""
    f = tmp_path / "r.jsonl"
    f.write_text(json.dumps(
        {"type": "session_meta", "payload": {"id": "via-id-field"}}
    ) + "\n")
    assert _read_first_session_meta(f) == "via-id-field"


# ---------------------------------------------------------------------------
# build_argv delegation
# ---------------------------------------------------------------------------


def test_build_argv_codex_injects_flags():
    a = CodexAdapter()
    result = a.build_argv(base_argv=["codex"], cwd="/data/proj")
    assert result.argv[0] == "codex"
    assert "-C" in result.argv
    assert "workspace-write" in result.argv
    assert result.session_id is None


def test_build_argv_appends_prompt_positionally():
    a = CodexAdapter()
    result = a.build_argv(
        base_argv=["codex"],
        cwd="/x",
        initial_prompt="hello",
    )
    assert result.argv[-1] == "hello"
    assert result.prompt_appended is True


def test_build_argv_resume_returns_durable_session_id():
    result = CodexAdapter().build_argv(
        base_argv=["codex"],
        cwd="/x",
        resume_from="019c-existing",
    )
    assert "resume" in result.argv
    assert result.argv[-1] == "019c-existing"
    assert result.session_id == "019c-existing"


def test_build_argv_bash_is_strict_passthrough():
    a = CodexAdapter()
    result = a.build_argv(
        base_argv=["bash", "-i"],
        cwd="/x",
        initial_prompt="leaked?",
    )
    assert result.argv == ["bash", "-i"]
    assert result.prompt_appended is False


# ---------------------------------------------------------------------------
# scan_events
# ---------------------------------------------------------------------------


def test_scan_events_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    a = CodexAdapter()
    assert a.scan_events() == []


def test_scan_events_yields_session_started_from_session_meta(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions" / "2026" / "07" / "25"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-abc.jsonl"
    rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "timestamp": "2026-07-25T10:00:00Z",
            "payload": {"session_id": "codex-sid-xyz", "cwd": "/tmp/proj"},
        }) + "\n"
    )

    a = CodexAdapter()
    events = a.scan_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.SESSION_STARTED
    assert ev.session_id == "codex-sid-xyz"
    assert ev.payload["backend"] == "codex"


def test_scan_events_derives_user_message_and_task_complete(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-1.jsonl"
    lines = [
        {"type": "session_meta",
         "payload": {"session_id": "s1", "cwd": "/x"}},
        {"type": "event_msg",
         "payload": {"type": "user_message", "message": "hi"}},
        {"type": "event_msg",
         "payload": {"type": "task_complete", "last_agent_message": "done"}},
    ]
    rollout.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    a = CodexAdapter()
    events = a.scan_events()
    types = [e.type for e in events]
    assert EventType.SESSION_STARTED in types
    assert EventType.MESSAGE_USER_SENT in types
    assert EventType.MESSAGE_ASSISTANT_DONE in types


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_binary_missing(monkeypatch):
    monkeypatch.setattr(
        "csm.backends.codex.adapter.shutil.which",
        lambda name: None,
    )
    st = CodexAdapter().probe()
    assert st.installed is False
    assert st.authenticated is False


def test_probe_binary_present_but_no_auth_file(monkeypatch, tmp_path):
    """Binary exists but auth.json missing → authenticated=False."""
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(
        "csm.backends.codex.adapter.shutil.which",
        lambda name: "/fake/bin/codex",
    )
    class _Res:
        returncode = 0
        stdout = "codex-cli 0.145.0\n"
        stderr = ""
    monkeypatch.setattr(
        "csm.backends.codex.adapter.subprocess.run",
        lambda *a, **kw: _Res(),
    )
    st = CodexAdapter().probe()
    assert st.installed is True
    assert st.authenticated is False
    assert "auth.json" in (st.error or "")
    assert st.version == "codex-cli 0.145.0"


# ---------------------------------------------------------------------------


def test_codex_frames_prose_as_a_bracketed_paste():
    """Codex's TUI DISCARDS a plain `text + CRLF` burst — verified against
    codex-cli 0.145.0 over a real PTY: the turn never starts and the text
    never reaches the composer. Typing the same bytes one at a time works,
    which is what makes this a framing bug rather than a readiness race."""
    from csm.backends.codex.adapter import CodexAdapter

    framed = CodexAdapter().frame_pty_input("hello")

    assert framed.startswith(b"\x1b[200~")
    assert b"hello" in framed
    # The trailing \r may ride in the SAME write (verified submitting), which
    # keeps the caller's single atomic PTY write and its idempotency lock.
    assert framed.endswith(b"\x1b[201~\r")


def test_claude_framing_is_unchanged():
    """Regression: claude's working path must not acquire a paste envelope."""
    from csm.backends.claude.adapter import ClaudeAdapter

    assert ClaudeAdapter().frame_pty_input("hello") == b"hello\r\n"


# ---------------------------------------------------------------------------
# Workspace pre-trust — it edits the USER's ~/.codex/config.toml, so the
# safety properties matter more than the happy path.
#
# Codex refuses to run a turn in a directory absent from its `[projects]`
# table; it opens a folder-trust modal the chat surfaces never render, so the
# session looks alive and silently eats the first message answering a dialog
# nobody can see. Verified on a fresh CODEX_HOME: same spawn, same new
# directory, submit=NO without the entry and submit=YES with it. Passing the
# same value via `-c` does NOT work — the check reads the on-disk config.
# ---------------------------------------------------------------------------


def _adapter_with_home(tmp_path, monkeypatch):
    from csm.backends.codex.adapter import CodexAdapter
    a = CodexAdapter()
    monkeypatch.setattr(a, "home_dir", lambda: tmp_path)
    return a


def test_trust_entry_is_appended_and_parses(tmp_path, monkeypatch):
    import tomllib
    a = _adapter_with_home(tmp_path, monkeypatch)

    assert a.ensure_workspace_trusted("/data/proj") is True

    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["projects"]["/data/proj"]["trust_level"] == "trusted"


def test_existing_user_config_is_preserved_verbatim(tmp_path, monkeypatch):
    """Round-tripping through a TOML writer would drop the user's comments
    and ordering — this appends instead."""
    cfg = tmp_path / "config.toml"
    original = (
        '# my notes\nmodel = "gpt-5.6-sol"\n\n'
        '[projects."/existing"]\ntrust_level = "trusted"\n'
    )
    cfg.write_text(original)
    a = _adapter_with_home(tmp_path, monkeypatch)

    a.ensure_workspace_trusted("/data/new")

    after = cfg.read_text()
    assert after.startswith(original)      # nothing rewritten or reordered
    assert "# my notes" in after
    assert '[projects."/data/new"]' in after


def test_already_trusted_is_a_no_op(tmp_path, monkeypatch):
    """Idempotent: spawning repeatedly in one directory must not append a
    duplicate table every time (codex would also reject the dupe key)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[projects."/data/proj"]\ntrust_level = "trusted"\n')
    a = _adapter_with_home(tmp_path, monkeypatch)
    before = cfg.read_text()

    assert a.ensure_workspace_trusted("/data/proj") is False
    assert cfg.read_text() == before


def test_untrusted_existing_entry_is_left_alone(tmp_path, monkeypatch):
    """The user explicitly marked this folder untrusted — do not overrule."""
    import tomllib
    cfg = tmp_path / "config.toml"
    cfg.write_text('[projects."/data/proj"]\ntrust_level = "untrusted"\n')
    a = _adapter_with_home(tmp_path, monkeypatch)

    assert a.ensure_workspace_trusted("/data/proj") is False
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["projects"]["/data/proj"]["trust_level"] == "untrusted"


def test_path_with_quotes_stays_valid_toml(tmp_path, monkeypatch):
    import tomllib
    a = _adapter_with_home(tmp_path, monkeypatch)

    a.ensure_workspace_trusted('/data/we"ird\\path')

    parsed = tomllib.loads((tmp_path / "config.toml").read_text())
    assert '/data/we"ird\\path' in parsed["projects"]


def test_missing_trailing_newline_does_not_glue_tables(tmp_path, monkeypatch):
    import tomllib
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5"')          # no trailing newline
    a = _adapter_with_home(tmp_path, monkeypatch)

    a.ensure_workspace_trusted("/data/proj")

    parsed = tomllib.loads(cfg.read_text())
    assert parsed["model"] == "gpt-5"
    assert "/data/proj" in parsed["projects"]


def test_unparseable_config_is_never_clobbered(tmp_path, monkeypatch):
    """Best effort: a broken config means we back off, not overwrite. Losing
    the user's codex config would be far worse than one trust prompt."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not [ valid toml")
    a = _adapter_with_home(tmp_path, monkeypatch)

    assert a.ensure_workspace_trusted("/data/proj") is False
    assert cfg.read_text() == "this is not [ valid toml"
