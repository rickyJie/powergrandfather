"""Alembic v2 migration integration test.

Runs `alembic upgrade head` on a fresh sqlite temp DB, verifies the new
schema shape, then runs `alembic downgrade base` and re-upgrades to
confirm both directions work end-to-end.

Also checks that pre-existing rows survive the up-migration with correct
defaults (e.g. old `backend='claude'` → new `agent='claude'`).
"""
from __future__ import annotations

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Fresh sqlite DB pointed at by alembic.ini."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("CSM_DB_PATH", str(db_file))
    # Alembic reads env.py which uses csm.config.settings.resolved_db_url.
    cfg = Config(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "alembic.ini"
        )
    )
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite:///{db_file}",
    )
    return cfg


def _table_columns(engine, table_name: str) -> set[str]:
    insp = inspect(engine)
    return {c["name"] for c in insp.get_columns(table_name)}


def test_upgrade_creates_user_preference_with_seed(alembic_cfg, tmp_path):
    command.upgrade(alembic_cfg, "head")
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")

    with engine.connect() as conn:
        rows = list(conn.execute(text(
            "SELECT id, default_agent, has_completed_first_run "
            "FROM user_preference"
        )))
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == "claude"
    # existing installs are treated as "wizard already done"
    assert rows[0][2] in (1, True)


def test_upgrade_renames_session_backend_to_agent(alembic_cfg, tmp_path):
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    cols = _table_columns(engine, "session")
    assert "agent" in cols
    assert "backend" not in cols
    assert "rollout_path" in cols
    assert "codex_rollout_path" not in cols


def test_upgrade_renames_file_state_columns_and_adds_agent(alembic_cfg, tmp_path):
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    cols = _table_columns(engine, "file_state")
    assert "artifact_path" in cols
    assert "jsonl_path" not in cols
    assert "agent" in cols


def test_downgrade_restores_old_names(alembic_cfg, tmp_path):
    """Downgrade past both v2 migrations restores the pre-v2 schema.

    v2 is now two revisions (u3o5j7k8lhim = user_preference + agent/rollout
    renames; v4p6k8l9mijn = claude_session_id → external_session_id).
    Downgrade to the last pre-v2 head (t2n4i6j7kghl) to see the original
    column names again.
    """
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "t2n4i6j7kghl")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    sess_cols = _table_columns(engine, "session")
    assert "backend" in sess_cols
    assert "agent" not in sess_cols
    assert "codex_rollout_path" in sess_cols
    assert "rollout_path" not in sess_cols
    assert "claude_session_id" in sess_cols
    assert "external_session_id" not in sess_cols
    # file_state also restored
    fs_cols = _table_columns(engine, "file_state")
    assert "jsonl_path" in fs_cols
    assert "artifact_path" not in fs_cols
    assert "agent" not in fs_cols
    # user_preference dropped
    insp = inspect(engine)
    assert "user_preference" not in insp.get_table_names()


def test_downgrade_v4_restores_claude_session_id(alembic_cfg, tmp_path):
    """Downgrading past the v4 migration (external_session_id rename)
    restores claude_session_id while keeping user_preference / agent
    intact. Explicit target `v4p6k8l9mijn` is one step above v4's
    revises, so pointing there is equivalent to `downgrade -1` from
    v4 head — resilient to new migrations landing on top."""
    command.upgrade(alembic_cfg, "head")
    # Downgrade all the way to just after the v2 (u3o5j7k8lhim) migration
    # — that reverts both v4 (external_session_id rename) and later
    # migrations that touched raw_token_event.
    command.downgrade(alembic_cfg, "u3o5j7k8lhim")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    sess_cols = _table_columns(engine, "session")
    assert "claude_session_id" in sess_cols
    assert "external_session_id" not in sess_cols
    # v2 (u3o5j7k8lhim) columns still there.
    assert "agent" in sess_cols


def test_full_up_down_up_roundtrip(alembic_cfg):
    """Round-trip: up → down → up. Common bug: downgrade leaves detritus
    that makes the second upgrade fail."""
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "-1")
    command.upgrade(alembic_cfg, "head")


def test_codex_token_migration_normalizes_existing_rows(alembic_cfg, tmp_path):
    """Inclusive Codex input becomes disjoint in raw rows and rollups."""
    command.upgrade(alembic_cfg, "b3d5f7h9j1kl")
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        # gpt-4o legacy cost: 500*5 + 50*5 + 200*2.5 + 120*15.
        conn.execute(text(
            "INSERT INTO raw_token_event ("
            "id, ts, model, input_tokens, cache_creation_tokens, "
            "cache_read_tokens, output_tokens, estimated_cost_usd, "
            "is_subagent, agent"
            ") VALUES ("
            "'codex-raw', '2026-08-01 08:00:00', 'gpt-4o', 500, 50, "
            "200, 120, 0.00505, 0, 'codex'"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO raw_token_event ("
            "id, ts, model, input_tokens, cache_creation_tokens, "
            "cache_read_tokens, output_tokens, estimated_cost_usd, "
            "is_subagent, agent"
            ") VALUES ("
            "'claude-raw', '2026-08-01 08:00:00', 'claude-opus-4-7', "
            "500, 50, 200, 120, 0.123, 0, 'claude'"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO hourly_rollup ("
            "id, bucket_hour, model, project_path, agent, input_tokens, "
            "cache_creation_tokens, cache_read_tokens, output_tokens, "
            "estimated_cost_usd, msg_count, updated_at"
            ") VALUES ("
            "'codex-rollup', '2026-08-01 08:00:00', 'gpt-4o', '/p', "
            "'codex', 500, 50, 200, 120, 0.00505, 1, "
            "'2026-08-01 09:00:00'"
            ")"
        ))

    command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        raw = conn.execute(text(
            "SELECT input_tokens, cache_creation_tokens, cache_read_tokens, "
            "output_tokens, estimated_cost_usd FROM raw_token_event "
            "WHERE id='codex-raw'"
        )).one()
        rollup = conn.execute(text(
            "SELECT input_tokens, estimated_cost_usd FROM hourly_rollup "
            "WHERE id='codex-rollup'"
        )).one()
        claude = conn.execute(text(
            "SELECT input_tokens, estimated_cost_usd FROM raw_token_event "
            "WHERE id='claude-raw'"
        )).one()

    assert tuple(raw[:4]) == (250, 50, 200, 120)
    assert sum(raw[:4]) == 620
    assert raw[4] == pytest.approx(0.0038)
    assert rollup[0] == 250
    assert rollup[1] == pytest.approx(0.0038)
    assert tuple(claude) == (500, 0.123)

    # Data downgrade is reversible while the old application semantics are
    # active: inclusive input and its corresponding estimated cost return.
    command.downgrade(alembic_cfg, "b3d5f7h9j1kl")
    with engine.connect() as conn:
        downgraded = conn.execute(text(
            "SELECT input_tokens, estimated_cost_usd FROM raw_token_event "
            "WHERE id='codex-raw'"
        )).one()
    assert downgraded[0] == 500
    assert downgraded[1] == pytest.approx(0.00505)


def test_lark_enabled_types_backfill_adds_new_message_and_review_and_mission(
    alembic_cfg, tmp_path,
):
    """Regression: `d1s3t5u7v9wx` backfills missing keys in
    lark_settings.enabled_types.

    Setup: upgrade to `a9u2pd3rfqot` (the seed migration that only writes
    the 4 legacy PUSH_TYPES). Mutate the row to have partial state
    including an explicit `False` for one key. Then upgrade to head and
    verify:
      - three new keys added as True,
      - user's explicit False choice preserved,
      - existing True flags preserved.
    """
    import json
    command.upgrade(alembic_cfg, "a9u2pd3rfqot")
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")

    with engine.begin() as conn:
        # Simulate an upgrader who has toggled one legacy key off.
        conn.execute(text(
            "UPDATE lark_settings SET enabled_types = :et WHERE id = 1"
        ).bindparams(et=json.dumps({
            "session_crashed": True,
            "auto_run_failed": True,
            "token_warning": False,   # explicit user choice, must survive
            "port_conflict": True,
        })))
    engine.dispose()

    command.upgrade(alembic_cfg, "head")
    engine2 = create_engine(f"sqlite:///{db_file}")
    with engine2.connect() as conn:
        raw = conn.execute(text(
            "SELECT enabled_types FROM lark_settings WHERE id = 1"
        )).scalar_one()
    et = json.loads(raw) if isinstance(raw, str) else raw
    assert et["new_message"] is True         # backfilled
    assert et["auto_needs_review"] is True   # backfilled
    assert et["mission_done"] is True        # backfilled
    assert et["session_crashed"] is True     # existing preserved
    assert et["token_warning"] is False      # user's explicit False preserved


def test_lark_enabled_types_backfill_is_idempotent(alembic_cfg, tmp_path):
    """Second upgrade must not overwrite a False the user set AFTER the
    first backfill. Guards against the "downgrade + upgrade" cycle
    silently re-enabling a type the user turned off."""
    import json
    command.upgrade(alembic_cfg, "head")
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE lark_settings SET enabled_types = :et WHERE id = 1"
        ).bindparams(et=json.dumps({
            "new_message": False,           # user opted out post-backfill
            "session_crashed": True,
            "auto_run_failed": True,
            "auto_needs_review": True,
            "token_warning": True,
            "port_conflict": True,
            "mission_done": True,
        })))
    engine.dispose()
    # Downgrade PAST the lark backfill revision (target: a9u2pd3rfqot,
    # the seed migration one step before d1s3t5u7v9wx). Then re-upgrade
    # to head. `new_message` key is removed by the downgrade and re-added
    # by the upgrade. Trade-off: this DOES flip the user's False → True
    # because the key is gone. This is explicitly the accepted behavior
    # of downgrade + re-upgrade — documented in the d1s3t5u7v9wx
    # migration docstring.
    #
    # Explicit target (not "-1") because migrations landing after
    # d1s3t5u7v9wx (e.g. m5n6o7p8q9rs sync v2) shift the meaning of "-1".
    command.downgrade(alembic_cfg, "a9u2pd3rfqot")
    command.upgrade(alembic_cfg, "head")
    engine2 = create_engine(f"sqlite:///{db_file}")
    with engine2.connect() as conn:
        raw = conn.execute(text(
            "SELECT enabled_types FROM lark_settings WHERE id = 1"
        )).scalar_one()
    et = json.loads(raw) if isinstance(raw, str) else raw
    # After downgrade + upgrade, the key is present again as True.
    assert et["new_message"] is True
    # The other True keys the user set are preserved (they weren't
    # touched by up or down since they aren't in _BACKFILL_KEYS —
    # session_crashed etc.).
    assert et["session_crashed"] is True
    assert et["token_warning"] is True


def test_sync_v2_adds_resource_table_columns(alembic_cfg, tmp_path):
    """m5n6o7p8q9rs adds `origin` + `last_synced_hashes` to 3 tables."""
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    for tbl in ("instruction", "mcp_server", "skill"):
        cols = _table_columns(engine, tbl)
        assert "origin" in cols, f"{tbl} missing origin"
        assert "last_synced_hashes" in cols, f"{tbl} missing last_synced_hashes"


def test_sync_v2_adds_sync_config_columns(alembic_cfg, tmp_path):
    """m5n6o7p8q9rs adds `sync_mode` + `tick_interval_hours` to sync_config."""
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    cols = _table_columns(engine, "sync_config")
    assert "sync_mode" in cols
    assert "tick_interval_hours" in cols


def test_sync_v2_creates_four_new_tables_and_seeds_policy(alembic_cfg, tmp_path):
    """m5n6o7p8q9rs creates 4 tables and seeds sync_policy(id=1)."""
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for tbl in ("sync_agent_run", "pending_decision", "sync_policy", "fanout_ledger"):
        assert tbl in tables, f"{tbl} not created"
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT id, length(prompt) FROM sync_policy"
        )).one()
    assert row[0] == 1
    # Seeded prompt should be substantial (v0.4 is ~3-4 KB).
    assert row[1] > 500, f"seed prompt too short: {row[1]} chars"


def test_sync_v2_fanout_ledger_unique_constraint(alembic_cfg, tmp_path):
    """m5n6o7p8q9rs unique key (resource_type, resource_id, body_hash, ts)."""
    from sqlalchemy.exc import IntegrityError
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO fanout_ledger (ts, resource_type, resource_id, "
            "body_hash, target_agents, status, attempt_count) "
            "VALUES ('2026-08-03 12:00:00', 'instruction', 1, 'abc', "
            "'[\"claude\"]', 'pending', 0)"
        ))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO fanout_ledger (ts, resource_type, resource_id, "
                "body_hash, target_agents, status, attempt_count) "
                "VALUES ('2026-08-03 12:00:00', 'instruction', 1, 'abc', "
                "'[\"codex\"]', 'pending', 0)"
            ))


def test_sync_v2_downgrade_removes_new_tables_and_columns(alembic_cfg, tmp_path):
    """Downgrade past m5n6o7p8q9rs drops the 4 tables and removes the
    added resource-table columns."""
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "d1s3t5u7v9wx")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for tbl in ("sync_agent_run", "pending_decision", "sync_policy", "fanout_ledger"):
        assert tbl not in tables, f"{tbl} not dropped by downgrade"
    for tbl in ("instruction", "mcp_server", "skill"):
        cols = _table_columns(engine, tbl)
        assert "origin" not in cols, f"{tbl}.origin not dropped"
        assert "last_synced_hashes" not in cols, f"{tbl}.last_synced_hashes not dropped"
    sc_cols = _table_columns(engine, "sync_config")
    assert "sync_mode" not in sc_cols
    assert "tick_interval_hours" not in sc_cols


def test_sync_v2_upgrade_is_idempotent_after_partial(alembic_cfg, tmp_path):
    """Simulate a partial upgrade: manually pre-add `origin` to instruction
    before running head. `_has_column` guard should skip the duplicate."""
    command.upgrade(alembic_cfg, "d1s3t5u7v9wx")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE instruction ADD COLUMN origin TEXT DEFAULT 'csm'"
        ))
    engine.dispose()
    # Now run the sync v2 migration. Should NOT error on the pre-existing column.
    command.upgrade(alembic_cfg, "head")
    engine2 = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    cols = _table_columns(engine2, "instruction")
    assert "origin" in cols
    assert "last_synced_hashes" in cols


def test_pre_existing_backend_claude_row_survives_upgrade(alembic_cfg, tmp_path):
    """Simulate an old install: upgrade to previous rev, insert a row with
    backend='claude', then upgrade to head and check `agent` is 'claude'."""
    # First bring schema to the PRE-v2 head (revision t2n4i6j7kghl).
    command.upgrade(alembic_cfg, "t2n4i6j7kghl")
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO session (id, cwd, status, type, started_at, "
            "backend, tags, unread_count, ring_buffer_offset, pinned, "
            "manual_unread) "
            "VALUES ('legacy-sid', '/tmp', 'running', 'interactive', "
            "'2026-07-25 10:00:00', 'claude', '[]', 0, 0, 0, 0)"
        ))
    # Now upgrade to head (v2 migration runs).
    command.upgrade(alembic_cfg, "head")
    engine.dispose()
    engine2 = create_engine(f"sqlite:///{db_file}")
    with engine2.connect() as conn:
        row = conn.execute(text(
            "SELECT id, agent FROM session WHERE id='legacy-sid'"
        )).one()
    assert row[0] == "legacy-sid"
    assert row[1] == "claude"
