"""Unit tests for sync state helpers (Phase 2d).

Covers:
- redact_for_snapshot: bodies → sha256:16, mcp env → <redacted>
- compute_input_state_hash: deterministic + stable across reordering
- build_input_payload: shape check
"""
from __future__ import annotations

from csm.modules.sync.state import (
    build_input_payload,
    compute_input_state_hash,
    redact_for_snapshot,
)


def test_redact_replaces_agent_memory_full_with_fingerprint():
    state = {
        "agents": {
            "claude": {"memory_full": "user secret memory"},
        },
    }
    red = redact_for_snapshot(state)
    val = red["agents"]["claude"]["memory_full"]
    assert val.startswith("<sha256:")
    assert val.endswith(">")
    assert "user secret memory" not in val


def test_redact_replaces_skill_body_md_with_fingerprint():
    state = {
        "agents": {
            "claude": {"skills": [
                {"name": "sk", "body_md": "secret skill body"},
            ]},
        },
    }
    red = redact_for_snapshot(state)
    sk_body = red["agents"]["claude"]["skills"][0]["body_md"]
    assert sk_body.startswith("<sha256:")
    assert "secret" not in sk_body


def test_redact_replaces_mcp_env_values_with_redacted_marker():
    state = {
        "agents": {
            "claude": {"mcp_servers": [
                {"name": "srv", "env": {"KEY": "sk-secret-123",
                                        "OTHER": "leak-me"}},
            ]},
        },
    }
    red = redact_for_snapshot(state)
    env = red["agents"]["claude"]["mcp_servers"][0]["env"]
    assert env == {"KEY": "<redacted>", "OTHER": "<redacted>"}


def test_redact_csm_instruction_body_fingerprinted():
    state = {"csm_resources": {"instructions": [
        {"name": "no-sudo", "body": "Do not use sudo."},
    ]}}
    red = redact_for_snapshot(state)
    assert red["csm_resources"]["instructions"][0]["body"].startswith("<sha256:")


def test_redact_csm_mcp_env_json_replaced():
    state = {"csm_resources": {"mcp_servers": [
        {"name": "s", "env_json": {"X": "secret"}},
    ]}}
    red = redact_for_snapshot(state)
    assert red["csm_resources"]["mcp_servers"][0]["env_json"] == {"X": "<redacted>"}


def test_redact_deep_copy_no_mutation_of_input():
    original = {
        "agents": {
            "claude": {"memory_full": "keep me"},
        },
    }
    redact_for_snapshot(original)
    assert original["agents"]["claude"]["memory_full"] == "keep me"


def test_redact_survives_missing_keys():
    """No agents, no csm_resources, unusual shape — never raises."""
    assert redact_for_snapshot({}) == {}
    assert redact_for_snapshot({"other": 1}) == {"other": 1}
    assert redact_for_snapshot({"agents": {"claude": {}}}) == {
        "agents": {"claude": {}},
    }


def test_compute_input_state_hash_deterministic():
    payload = {"a": 1, "b": {"c": 2, "d": 3}}
    h1 = compute_input_state_hash(payload)
    h2 = compute_input_state_hash(payload)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_compute_input_state_hash_sort_keys_makes_order_irrelevant():
    p1 = {"a": 1, "b": 2}
    p2 = {"b": 2, "a": 1}
    assert compute_input_state_hash(p1) == compute_input_state_hash(p2)


def test_compute_input_state_hash_changes_on_content_change():
    p1 = {"a": 1}
    p2 = {"a": 2}
    assert compute_input_state_hash(p1) != compute_input_state_hash(p2)


def test_build_input_payload_shape():
    p = build_input_payload(
        csm_resources={"instructions": []},
        agent_states={"claude": {}},
        pending_recent=[{"id": 1}],
        meta={"trigger": "manual"},
    )
    assert set(p.keys()) == {
        "csm_resources", "agents", "pending_decisions_recent", "meta",
    }
    assert p["pending_decisions_recent"] == [{"id": 1}]


def test_build_input_payload_meta_defaults_to_empty_dict():
    p = build_input_payload({}, {}, [])
    assert p["meta"] == {}
