"""M11 fix: codex model comes from `turn_context.payload.model`, not
`session_meta`. This test verifies that the tailer's `_CodexFileState`
bootstraps model from any record carrying `payload.model` — matching
real codex 0.145+ rollouts where session_meta has NO model field.
"""
from __future__ import annotations

import json

from csm.adapters.jsonl_tail import CodexRolloutTailer


def _write(path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_model_bootstrapped_from_turn_context(tmp_path):
    """Codex 0.145+ shape: session_meta first (no model), then
    turn_context carries the model. Tailer should surface it on every
    subsequent record."""
    (tmp_path / "sessions" / "2026" / "07" / "26").mkdir(parents=True)
    p = tmp_path / "sessions" / "2026" / "07" / "26" / "rollout-x.jsonl"
    _write(p, [
        {"type": "session_meta",
         "payload": {"session_id": "sid-1", "cwd": "/tmp"}},
        {"type": "turn_context",
         "payload": {"turn_id": "t1", "model": "gpt-5-codex"}},
        {"type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {"input_tokens": 100}}}},
    ])

    tailer = CodexRolloutTailer(tmp_path / "sessions")
    records = tailer.scan_once()
    assert len(records) == 3
    # session_meta record has no model in state yet — model bootstrap
    # happens as records are processed, so the first record sees model=None
    assert records[0].model is None
    # turn_context bootstrapped the model → it's set for this record and after
    assert records[1].model == "gpt-5-codex"
    assert records[2].model == "gpt-5-codex"


def test_model_from_session_meta_still_works(tmp_path):
    """Regression: if a codex version DOES put model in session_meta
    (older, or a future format), the bootstrap still catches it."""
    (tmp_path / "sessions").mkdir()
    p = tmp_path / "sessions" / "rollout-y.jsonl"
    _write(p, [
        {"type": "session_meta",
         "payload": {"session_id": "sid-2", "cwd": "/tmp", "model": "gpt-4o"}},
        {"type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {"input_tokens": 50}}}},
    ])
    tailer = CodexRolloutTailer(tmp_path / "sessions")
    records = tailer.scan_once()
    # session_meta record is what carries the model — it should surface it
    assert records[0].model == "gpt-4o"
    assert records[1].model == "gpt-4o"


def test_first_model_wins(tmp_path):
    """If turn_context switches model mid-session, the first-seen model
    stays. Attribution is best-effort — a session that starts with
    gpt-5 and switches to gpt-4o mid-way still counts as gpt-5 in
    our books. Documented behavior; keeps things simple."""
    (tmp_path / "sessions").mkdir()
    p = tmp_path / "sessions" / "rollout-z.jsonl"
    _write(p, [
        {"type": "session_meta", "payload": {"session_id": "s", "cwd": "/x"}},
        {"type": "turn_context", "payload": {"model": "gpt-5"}},
        {"type": "turn_context", "payload": {"model": "gpt-4o"}},
    ])
    tailer = CodexRolloutTailer(tmp_path / "sessions")
    records = tailer.scan_once()
    assert records[1].model == "gpt-5"
    assert records[2].model == "gpt-5"   # first-model-wins
