"""Unit tests for ``csm.api.sessions._jsonl_present``.

Regression coverage for the openpi incident: before this fix,
``_jsonl_present`` returned True whenever the JSONL file existed on
disk, without inspecting its contents. A JSONL containing only
meta lines (``permission-mode``, ``file-history-snapshot``) — which is
exactly what claude writes when it crashes within a few hundred ms of
spawn — passed the check, so ``canResume`` (frontend) and the /resume
preflight (backend) both green-lit spawns of subprocesses that were
guaranteed to die immediately, producing a chain of six crashed rows
in 3.5 minutes.

The fix layers a content check on top of the existence check. These
tests pin the four semantically interesting cases so the regression
can't sneak back in.
"""
from __future__ import annotations

import json

import pytest
from csm.api.sessions import _jsonl_has_history, _jsonl_present
from csm.config import settings


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "claude_projects_dir", tmp_path)
    return tmp_path


def _plant(projects_root, cwd: str, sid: str, lines: list[dict]) -> None:
    encoded = cwd.rstrip("/").replace("/", "-")
    d = projects_root / encoded
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        "".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8",
    )


def test_missing_file_is_not_present(projects_root):
    """Baseline: no JSONL on disk → False, same as pre-fix behaviour."""
    assert _jsonl_present("/tmp/nope", "does-not-exist") is False


def test_empty_meta_only_jsonl_is_not_present(projects_root):
    """The openpi failure signature: a 115-byte file with only
    permission-mode + file-history-snapshot lines. Must NOT report
    present — Resume against this file would crash immediately."""
    _plant(
        projects_root, "/tmp/x", "half-born",
        [{"type": "permission-mode"}, {"type": "file-history-snapshot"}],
    )
    assert _jsonl_present("/tmp/x", "half-born") is False


def test_jsonl_with_user_message_is_present(projects_root):
    """A transcript with even one real user message is legitimately
    resumable — return True."""
    _plant(
        projects_root, "/tmp/x", "real-sid",
        [
            {"type": "permission-mode"},
            {"type": "user", "message": {"content": "hi"}},
        ],
    )
    assert _jsonl_present("/tmp/x", "real-sid") is True


def test_jsonl_with_assistant_message_is_present(projects_root):
    """Symmetry: an assistant-first transcript (rare but possible for
    scripted spawns) also counts as resumable content."""
    _plant(
        projects_root, "/tmp/x", "sid-asst",
        [{"type": "assistant", "message": {"content": "ok"}}],
    )
    assert _jsonl_present("/tmp/x", "sid-asst") is True


def test_none_or_empty_sid_is_not_present(projects_root):
    """Guard against callers that forget to null-check."""
    assert _jsonl_present("/tmp/x", None) is False
    assert _jsonl_present("/tmp/x", "") is False
    assert _jsonl_present("", "some-sid") is False


def test_malformed_lines_treated_as_meta(projects_root, tmp_path):
    """A JSONL that is complete garbage (non-JSON lines) has no real
    messages by definition — return False, don't crash."""
    encoded = "-tmp-x"
    d = tmp_path / encoded
    d.mkdir(parents=True, exist_ok=True)
    (d / "garbage.jsonl").write_text("not json\n{also not json\n", encoding="utf-8")
    assert _jsonl_present("/tmp/x", "garbage") is False


def test_has_history_short_circuits_on_first_real_message(tmp_path):
    """Direct unit test on the helper: it should short-circuit — a huge
    file with a real message on line 1 doesn't need to read the rest."""
    p = tmp_path / "big.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": "first"}})]
    lines.extend(["x" * 1000 for _ in range(10_000)])  # nonsense filler
    p.write_text("\n".join(lines), encoding="utf-8")
    assert _jsonl_has_history(p) is True


def test_has_history_cache_invalidates_when_transcript_grows(tmp_path):
    """A cached empty transcript must become resumable after the first message."""
    p = tmp_path / "growing.jsonl"
    p.write_text(json.dumps({"type": "permission-mode"}) + "\n", encoding="utf-8")
    assert _jsonl_has_history(p) is False

    with p.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")

    assert _jsonl_has_history(p) is True
