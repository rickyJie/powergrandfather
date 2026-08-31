"""Unit tests for the B1/B5 sync primitives (errors, env_expand, atomic_write)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from csm.modules.sync.atomic_write import atomic_write_with_hash_guard
from csm.modules.sync.env_expand import resolve_env_refs
from csm.modules.sync.errors import ConcurrentWriteDrift, SyncPreflightError

# ----------------------------------------------------------------- env_expand


def test_env_expand_passthrough_values_without_refs():
    """No ${VAR} → returned unchanged."""
    assert resolve_env_refs({"a": "plain", "b": ""}) == {"a": "plain", "b": ""}


def test_env_expand_resolves_defined_var(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cr3t")
    assert resolve_env_refs({"api_key": "${MY_TOKEN}"}) == {"api_key": "s3cr3t"}


def test_env_expand_multiple_refs_in_one_value(monkeypatch):
    monkeypatch.setenv("A", "x")
    monkeypatch.setenv("B", "y")
    assert resolve_env_refs({"k": "${A}-${B}"}) == {"k": "x-y"}


def test_env_expand_undefined_raises_with_names_only(monkeypatch):
    monkeypatch.delenv("NOPE_ONE", raising=False)
    monkeypatch.delenv("NOPE_TWO", raising=False)
    with pytest.raises(SyncPreflightError) as exc:
        resolve_env_refs({"a": "${NOPE_ONE}", "b": "${NOPE_TWO}-suffix"})
    assert exc.value.missing == ["NOPE_ONE", "NOPE_TWO"]
    # Message MUST NOT leak values (they're undefined anyway) but must
    # list names sorted (deterministic UX).
    assert "NOPE_ONE" in str(exc.value)
    assert "NOPE_TWO" in str(exc.value)


def test_env_expand_partial_undefined_still_raises(monkeypatch):
    """If ANY var is undefined, whole call aborts — no partial writes."""
    monkeypatch.setenv("HAVE", "hello")
    monkeypatch.delenv("DONT_HAVE", raising=False)
    with pytest.raises(SyncPreflightError) as exc:
        resolve_env_refs({"ok": "${HAVE}", "bad": "${DONT_HAVE}"})
    assert exc.value.missing == ["DONT_HAVE"]


def test_env_expand_returns_new_dict_not_alias(monkeypatch):
    monkeypatch.setenv("V", "1")
    src = {"k": "${V}"}
    out = resolve_env_refs(src)
    assert out is not src


# ---------------------------------------------------------------- atomic_write


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_atomic_write_creates_file(tmp_path: Path):
    target = tmp_path / "target.md"
    atomic_write_with_hash_guard(target, b"hello world")
    assert target.read_bytes() == b"hello world"


def test_atomic_write_overwrites_existing(tmp_path: Path):
    target = tmp_path / "target.md"
    target.write_bytes(b"old content")
    atomic_write_with_hash_guard(target, b"new content")
    assert target.read_bytes() == b"new content"


def test_atomic_write_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "sub" / "file.md"
    atomic_write_with_hash_guard(target, b"x")
    assert target.read_bytes() == b"x"


def test_atomic_write_detects_concurrent_drift(tmp_path: Path, monkeypatch):
    """Simulate a race: after os.replace, another writer overwrites the file
    before our verify-read. atomic_write_with_hash_guard must raise."""
    target = tmp_path / "raced.md"
    original_replace = os.replace

    def racing_replace(src, dst):
        original_replace(src, dst)
        # Racy writer clobbers the file BEFORE the verify read.
        Path(dst).write_bytes(b"clobbered by REPL")

    monkeypatch.setattr(os, "replace", racing_replace)

    with pytest.raises(ConcurrentWriteDrift) as exc:
        atomic_write_with_hash_guard(target, b"our content")

    assert exc.value.path == target
    assert exc.value.expected_hash == _sha256(b"our content")
    assert exc.value.actual_hash == _sha256(b"clobbered by REPL")


def test_atomic_write_cleans_tmp_on_failure(tmp_path: Path, monkeypatch):
    """If os.replace itself blows up, the .tmp scratch must be removed."""
    target = tmp_path / "fail.md"

    def boom(src, dst):
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_with_hash_guard(target, b"content")

    leftover = list(tmp_path.glob(".csm_sync_*.tmp"))
    assert leftover == []


def test_atomic_write_lock_uses_sibling_by_default(tmp_path: Path):
    """Default lock lives at <path>.<suffix>.lock — verify a write succeeds
    when no explicit lock_path is given (regression on lock resolution)."""
    target = tmp_path / "x.md"
    atomic_write_with_hash_guard(target, b"body")
    assert target.read_bytes() == b"body"
    # Lock artifact may or may not persist depending on filelock's cleanup;
    # only invariant we care about is: the write succeeded.
