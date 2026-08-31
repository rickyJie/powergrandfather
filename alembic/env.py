"""Alembic environment for async SQLite (sync url for autogenerate, sync upgrade)."""
from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make sure backend/ is on sys.path so we can import csm.*
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from csm.config import settings as csm_settings  # noqa: E402
from csm.models import Base  # noqa: E402  (loads all models)

config = context.config

# Override DB URL with our settings, UNLESS a caller (e.g. tests using
# `Config(...).set_main_option("sqlalchemy.url", ...)`) has already set
# a real URL. This lets migration tests point at a throwaway sqlite file
# without having to reload the csm.config module.
_url = config.get_main_option("sqlalchemy.url")
_PLACEHOLDER = "driver://user:pass@localhost/dbname"
if not _url or _url == _PLACEHOLDER:
    db_path = csm_settings.db_path
    if not db_path.is_absolute():
        db_path = csm_settings.project_root / db_path
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
db_path = None  # avoid stale reference in run_migrations_online chmod

if config.config_file_name is not None:
    # `disable_existing_loggers=False` prevents fileConfig from silencing
    # every csm.* logger not listed in alembic.ini — the default True
    # sets `disabled=True` on all pre-existing loggers which then silently
    # swallows their messages. That broke caplog-based tests running
    # after any migration test in the same pytest session.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch for ALTER
        )
        with context.begin_transaction():
            context.run_migrations()
    # Best-effort chmod to 0600 for the sqlite file. Parse the resolved
    # URL rather than using the module-level `db_path` variable (which
    # may be None when the caller supplied their own URL).
    try:
        from sqlalchemy.engine.url import make_url as _make_url
        _resolved_url = _make_url(config.get_main_option("sqlalchemy.url"))
        if _resolved_url.database and Path(_resolved_url.database).exists():
            os.chmod(_resolved_url.database, 0o600)
    except OSError as exc:
        logging.getLogger(__name__).warning("chmod 0600 failed: %s", exc)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
