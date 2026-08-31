"""SQLAlchemy 2.x async engine and session factory."""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from csm.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = settings.resolved_db_url()
        kwargs: dict = {"echo": False, "future": True}
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            # Driver-level acquire timeout. Aligned with PRAGMA busy_timeout below
            # so the two layers don't disagree.
            kwargs["connect_args"] = {"timeout": 30.0}
            # NullPool (backend-review W1): SQLite is single-writer, so a
            # connection POOL buys nothing on the write path — every writer
            # serializes on the WAL lock regardless. Worse, the default
            # AsyncAdaptedQueuePool (5 + 10 overflow) adds a SECOND queueing
            # layer whose `pool_timeout` (30s) stacks on top of the driver's
            # 30s busy_timeout for a ~60s worst case, and async + pooled
            # connections risk cross-task connection reuse. A fresh connection
            # per checkout makes contention attributable to the one real lock
            # and sidesteps both hazards; the connect overhead is negligible at
            # a single-user local console's request rate.
            kwargs["poolclass"] = NullPool
        _engine = create_async_engine(url, **kwargs)
        if is_sqlite:
            @event.listens_for(_engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _conn_record):
                cursor = dbapi_conn.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    # SQLite leaves FK enforcement disabled per connection.
                    # Without this, ORM ondelete=CASCADE declarations are only
                    # documentation and session purges leave orphan rows.
                    cursor.execute("PRAGMA foreign_keys=ON")
                    # 30s busy_timeout: the RollupWorker's per-tick upsert
                    # transaction (modules/token/rollup.py:_rollup) iterates many
                    # ON-CONFLICT INSERTs in one transaction and can hold the
                    # WAL writer slot >5s. Bumped from 5s so concurrent writers
                    # (agent send_message, notifications, port scan, ...) wait
                    # instead of 500-ing with "database is locked".
                    cursor.execute("PRAGMA busy_timeout=30000")
                finally:
                    cursor.close()
            # Enforce 0600 on the SQLite DB file (contains prompts + tool inputs).
            db_path = settings.db_path
            if not db_path.is_absolute():
                db_path = settings.project_root / db_path
            try:
                os.chmod(db_path, 0o600)
            except OSError as exc:
                logging.getLogger(__name__).warning("chmod 0600 %s failed: %s", db_path, exc)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the module-level SQLAlchemy sessionmaker.

    Legacy: API routes should prefer ``request.app.state.sessionmaker``
    (wired in lifespan) via ``Depends(get_db_sessionmaker)`` from
    ``csm.api._deps``. This function stays for alembic env.py + tests +
    background workers that receive sessionmaker via constructor
    injection.
    """
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession, commit on success, rollback on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
