"""Single-instance DB lock guard (ADR-0002).

Verifies a second owner of the same SQLite file is refused, the holder pid is
surfaced, distinct DB paths don't contend, and the lock frees on release so a
graceful restart can re-acquire.
"""
from __future__ import annotations

import os

import pytest
from csm.core.single_instance import DbInstanceLock, SingleInstanceError


def test_second_instance_refused_and_names_holder(tmp_path):
    db = tmp_path / "csm.db"
    first = DbInstanceLock(db)
    first.acquire()
    try:
        second = DbInstanceLock(db)
        with pytest.raises(SingleInstanceError) as ei:
            second.acquire(timeout=0.2, interval=0.02)
        # Holder pid (this process, since flock is per-open-description) is named.
        assert str(os.getpid()) == ei.value.holder
        assert ei.value.lock_path == db.with_name("csm.db.lock")
    finally:
        first.release()


def test_release_lets_a_new_instance_acquire(tmp_path):
    db = tmp_path / "csm.db"
    first = DbInstanceLock(db)
    first.acquire()
    first.release()
    # After release the lock is free — a fresh instance (e.g. post-restart) wins.
    second = DbInstanceLock(db)
    second.acquire(timeout=0.5, interval=0.02)
    second.release()


def test_distinct_db_paths_do_not_contend(tmp_path):
    a = DbInstanceLock(tmp_path / "desktop.db")
    b = DbInstanceLock(tmp_path / "mobile.db")
    a.acquire()
    b.acquire(timeout=0.2, interval=0.02)  # different lock file → no conflict
    a.release()
    b.release()


def test_release_is_idempotent(tmp_path):
    lock = DbInstanceLock(tmp_path / "csm.db")
    lock.acquire()
    lock.release()
    lock.release()  # no-op, must not raise


def test_lock_path_is_sibling_of_db(tmp_path):
    lock = DbInstanceLock(tmp_path / "sub" / "csm.db")
    assert lock.lock_path == tmp_path / "sub" / "csm.db.lock"
