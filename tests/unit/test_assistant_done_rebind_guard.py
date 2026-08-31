"""Guard against the assistant_done_rebind HIJACK.

The cwd-fallback rebind in NotificationBus._on_assistant_done recovers a
claude session that ROTATED its JSONL uuid (compact/clear/fork). But it must
NOT steal the binding of a correctly pre-bound (`--session-id`) session when an
UNRELATED claude merely shares the cwd. `_is_rotation_not_hijack` is the
discriminator: True → allow rebind (genuine rotation / undeterminable),
False → skip (the picked session is demonstrably still on its own id).
"""

from __future__ import annotations

import json
import os
import time

from csm.core.notification_bus import _is_rotation_not_hijack


def _patch_paths(monkeypatch, own, new):
    monkeypatch.setattr(
        "csm.modules.agent.jsonl_fast_tail.conversation_jsonl_path",
        lambda projects, cwd, ext: own if ext == "OWN" else new,
    )


def test_trivial_cases_allow():
    # No own id yet, or the event is for our own id → nothing to protect.
    assert _is_rotation_not_hijack(123, None, "NEW", "/cwd") is True
    assert _is_rotation_not_hijack(123, "SAME", "SAME", "/cwd") is True


def test_hijack_skipped_when_own_jsonl_is_fresh(tmp_path, monkeypatch):
    own = tmp_path / "own.jsonl"
    own.write_text("x")  # just written → fresh mtime
    new = tmp_path / "new.jsonl"
    _patch_paths(monkeypatch, own, new)
    # pid=None → fd check skipped; own JSONL fresh → the session never left its
    # id → the event belongs to another claude → skip (False).
    assert _is_rotation_not_hijack(None, "OWN", "NEW", "/cwd") is False


def test_rotation_allowed_when_own_jsonl_is_stale(tmp_path, monkeypatch):
    own = tmp_path / "own.jsonl"
    own.write_text("x")
    old = time.time() - 3600
    os.utime(own, (old, old))  # own JSONL went silent an hour ago
    new = tmp_path / "new.jsonl"
    _patch_paths(monkeypatch, own, new)
    # Stale own JSONL + no fd evidence → treat as a genuine rotation → allow.
    assert _is_rotation_not_hijack(None, "OWN", "NEW", "/cwd") is True


def test_headless_transcript_never_adopted(tmp_path, monkeypatch):
    """A `claude -p` one-shot in the same cwd is never a rotation.

    This is the 2026-08-30 incident: CSM's own agent-alert escalation ran with
    the backend's cwd, so its transcript landed in the user's project folder.
    The session it got handed to had been idle for an hour, so both older arms
    read "rotated away" — fd evidence absent (not our child), own JSONL stale —
    and a token alert's conversation surfaced inside an unrelated chat.
    """
    own = tmp_path / "own.jsonl"
    own.write_text("x")
    old = time.time() - 3600
    os.utime(own, (old, old))  # the arm that used to say "allow"
    new = tmp_path / "new.jsonl"
    new.write_text(
        json.dumps({"type": "user", "entrypoint": "sdk-cli"}) + "\n"
    )
    _patch_paths(monkeypatch, own, new)
    assert _is_rotation_not_hijack(None, "OWN", "NEW", "/cwd") is False


def test_interactive_transcript_still_adopted(tmp_path, monkeypatch):
    """The headless guard must not cost us the rotation-recovery fix: a real
    `cli` transcript with the same stale-own-JSONL shape still rebinds."""
    own = tmp_path / "own.jsonl"
    own.write_text("x")
    old = time.time() - 3600
    os.utime(own, (old, old))
    new = tmp_path / "new.jsonl"
    new.write_text(json.dumps({"type": "user", "entrypoint": "cli"}) + "\n")
    _patch_paths(monkeypatch, own, new)
    assert _is_rotation_not_hijack(None, "OWN", "NEW", "/cwd") is True
