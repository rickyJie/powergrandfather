"""Unit tests for the private helpers in ``csm.api.hooks``.

Covers ``_read_last_assistant_text`` edge cases that don't need the full
app fixture — the function is pure (path + filesystem) with a
belt-and-braces path guard: transcripts must live under
``settings.claude_projects_dir`` and every failure returns ``None`` so
the caller (``_dispatch`` under Stop) doesn't crash the hook and trip
the Finding-5 200-only contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from csm.api.hooks import _read_last_assistant_text
from csm.config import settings


@pytest.fixture
def under_projects_dir(tmp_path, monkeypatch):
    """Point ``settings.claude_projects_dir`` at ``tmp_path`` so we can create
    transcripts that pass the ``is_relative_to`` guard."""
    monkeypatch.setattr(settings, "claude_projects_dir", tmp_path)
    return tmp_path


def test_read_last_assistant_text_empty_file(under_projects_dir):
    """A zero-byte JSONL yields no lines → the loop leaves ``last_text`` at
    its ``None`` initial value → return None."""
    p = under_projects_dir / "empty.jsonl"
    p.write_bytes(b"")
    assert _read_last_assistant_text(str(p)) is None


def test_read_last_assistant_text_corrupt_jsonl(under_projects_dir):
    """Every ``json.loads`` in the scan is wrapped in try/except; a file that
    is entirely garbage produces no valid records → None."""
    p = under_projects_dir / "corrupt.jsonl"
    p.write_bytes(b"\x00\x01not valid json at all\xffnope\n\xde\xad")
    assert _read_last_assistant_text(str(p)) is None


def test_read_last_assistant_text_path_outside_root(tmp_path, monkeypatch):
    """Transcripts under an unrelated directory must be rejected by the
    ``is_relative_to`` guard — critical because ``transcript_path`` comes
    from the hook body (attacker-controlled if the loopback boundary is
    ever bypassed)."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    monkeypatch.setattr(settings, "claude_projects_dir", projects_root)

    attacker = tmp_path / "attacker.jsonl"
    attacker.write_text('{"message": {"role": "assistant", "content": "pwn"}}\n')
    # attacker.jsonl is a sibling of projects_root, not under it.
    assert _read_last_assistant_text(str(attacker)) is None


def test_read_last_assistant_text_vanishing_file(under_projects_dir, monkeypatch):
    """If ``p.open()`` raises after ``exists()`` returned True (races with an
    unlink, permission flip, or fs remount), the outer catch-all must
    swallow it and return None — bubbling would fail the hook."""
    p = under_projects_dir / "vanishing.jsonl"
    p.write_text('{"message": {"role": "assistant", "content": "hello"}}\n')

    # Monkeypatch Path.open to raise for THIS path only; other Path uses
    # (inside _read_last_assistant_text there aren't any post-guard, but
    # be defensive) are untouched.
    real_open = Path.open
    target = p.resolve()

    def exploding_open(self, *args, **kwargs):
        if self.resolve() == target:
            raise OSError("file vanished under us")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", exploding_open)

    assert _read_last_assistant_text(str(p)) is None


# ---------------------------------------------------------------------------
# Tail-bounded read (2026-08-27)
#
# The old implementation parsed the WHOLE transcript on every Stop hook — i.e.
# once per assistant turn — on the assumption that a transcript is "<1 MB per
# turn". It isn't: a transcript accumulates for the life of the session, and
# the largest ones in the live corpus are 19-25 MB, ~150-200 ms to parse. That
# ran inline on the event loop, so every turn paid it and so did every other
# request in flight. It now reads only the tail, off the loop.
# ---------------------------------------------------------------------------


def _turn(text: str) -> str:
    import json
    return json.dumps({"message": {"role": "assistant", "content": text}}) + "\n"


def test_reads_the_last_reply_from_a_transcript_far_past_the_tail_window(
    under_projects_dir, monkeypatch
):
    """The answer must come from the END of the file, not from wherever the
    read happens to start."""
    from csm.api import hooks

    monkeypatch.setattr(hooks, "_TRANSCRIPT_TAIL_BYTES", 4096)
    p = under_projects_dir / "long.jsonl"
    p.write_text(
        "".join(_turn(f"old reply {i} " + "x" * 200) for i in range(200))
        + _turn("the newest reply")
    )
    assert p.stat().st_size > 40_000  # many windows deep

    assert _read_last_assistant_text(str(p)) == "the newest reply"


def test_does_not_read_the_whole_file(under_projects_dir, monkeypatch):
    """The point of the change is the bytes NOT read. A 25 MB transcript used
    to be fully parsed on every turn."""
    from csm.api import hooks

    monkeypatch.setattr(hooks, "_TRANSCRIPT_TAIL_BYTES", 8192)
    p = under_projects_dir / "big.jsonl"
    p.write_text("".join(_turn("filler " + "y" * 500) for i in range(4000)) + _turn("last"))
    size = p.stat().st_size
    assert size > 1_000_000

    read_bytes = {"n": 0}
    real_open = Path.open
    target = p.resolve()

    class _Counting:
        def __init__(self, fh):
            self._fh = fh

        def __iter__(self):
            for line in self._fh:
                read_bytes["n"] += len(line)
                yield line

        def readline(self, *a):
            line = self._fh.readline(*a)
            read_bytes["n"] += len(line)
            return line

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    def counting_open(self, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)
        return _Counting(fh) if self.resolve() == target else fh

    monkeypatch.setattr(Path, "open", counting_open)

    assert _read_last_assistant_text(str(p)) == "last"
    assert read_bytes["n"] <= 8192 * 2, (
        f"read {read_bytes['n']} bytes of a {size}-byte transcript"
    )


def test_a_partial_line_at_the_window_edge_is_not_mistaken_for_a_record(
    under_projects_dir, monkeypatch
):
    """Seeking lands mid-line; that fragment must be dropped, not parsed.

    An earlier version of this test padded a record and checked a later, real
    message still won — which passes with or without the seek's `readline()`,
    because a mid-JSON fragment fails `json.loads` and gets skipped anyway. It
    asserted nothing.

    The fragment only does damage when it happens to BE valid JSON, so build
    exactly that: one line whose tail is a complete assistant record, with the
    window starting on its opening brace. Drop the fragment and the window
    holds no complete assistant record at all; keep it and a message the file
    never contained as a record is reported as the reply.
    """
    from csm.api import hooks

    ghost = '{"message":{"role":"assistant","content":"GHOST"}}'
    trailing = '{"message":{"role":"user","content":"after"}}\n'
    # Whole line is unparseable from its start (leading padding), so the ghost
    # is only reachable by seeking into the middle of it.
    line = "X" * 500 + ghost + "\n"

    p = under_projects_dir / "edge.jsonl"
    p.write_text(_turn("REAL") + line + trailing)
    # size - window lands precisely on the ghost's `{`.
    monkeypatch.setattr(hooks, "_TRANSCRIPT_TAIL_BYTES", len(ghost) + 1 + len(trailing))

    assert _read_last_assistant_text(str(p)) is None


def test_short_transcripts_are_still_read_whole(under_projects_dir):
    """Nothing changes for a file smaller than the window."""
    p = under_projects_dir / "short.jsonl"
    p.write_text(_turn("first") + _turn("second"))
    assert _read_last_assistant_text(str(p)) == "second"
