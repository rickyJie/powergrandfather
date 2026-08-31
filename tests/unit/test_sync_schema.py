"""Unit tests for SyncAgent decision schema (Phase 2a).

Covers:
- 4 valid action shapes parse via discriminator
- unknown `action` string rejected
- marker-injection tokens rejected
- >200 KB body rejected
- name-regex enforced (rejects PATH TRAVERSAL, uppercase, empty)
- resource_type ↔ candidate mismatch rejected in AdoptToCsm
- SkillCandidate auto-prepends YAML frontmatter when missing
- decisions cap enforced (max_length=500)
- _ACTION_PRIORITY ordering + stable sort
"""
from __future__ import annotations

import pytest
from csm.modules.sync.schema import (
    _ACTION_PRIORITY,
    AdoptToCsm,
    InstructionCandidate,
    McpCandidate,
    ProposeConflict,
    SkillCandidate,
    Skip,
    SyncDecisionsPayload,
    sort_decisions_by_priority,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------


def test_all_4_action_shapes_parse():
    """Every valid action shape is discriminated correctly."""
    payload = SyncDecisionsPayload.model_validate({
        "decisions": [
            {"action": "skip", "rationale": "no-op"},
            {"action": "adopt_to_csm",
             "resource_type": "instruction",
             "candidate": {"name": "a", "title": "T", "body": "b"},
             "source_agent": "claude",
             "recommended_scope": ["claude"],
             "rationale": "r"},
            {"action": "propagate_to_agent",
             "resource_type": "instruction",
             "resource_id": 1,
             "target_agent": "codex",
             "rationale": "r"},
            {"action": "propose_conflict",
             "resource_type": "instruction",
             "candidates": {"claude": "v1", "codex": "v2"},
             "rationale": "r"},
        ],
        "summary": "ok",
    })
    types = [type(d).__name__ for d in payload.decisions]
    assert types == ["Skip", "AdoptToCsm", "PropagateToAgent", "ProposeConflict"]


def test_unknown_action_rejected():
    """`delete_from_csm` / `merge` / anything not in the 4 whitelist fails."""
    with pytest.raises(ValidationError):
        SyncDecisionsPayload.model_validate({
            "decisions": [{"action": "delete_from_csm", "rationale": "no"}],
            "summary": "x",
        })


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def test_marker_injection_rejected():
    """Instruction body containing 'csm:start' rejected."""
    with pytest.raises(ValidationError) as exc:
        InstructionCandidate.model_validate({
            "name": "attack", "title": "T",
            "body": "<!-- csm:start id=hack -->\nbad\n<!-- csm:end -->",
        })
    assert "marker" in str(exc.value).lower()


def test_body_over_200kb_rejected():
    """Instruction body >200KB fails."""
    with pytest.raises(ValidationError) as exc:
        InstructionCandidate.model_validate({
            "name": "big", "title": "T", "body": "x" * 200_001,
        })
    assert "byte cap" in str(exc.value).lower() or "kb" in str(exc.value).lower()


def test_body_at_200kb_boundary_accepted():
    """Exactly 200000 bytes should be accepted."""
    InstructionCandidate.model_validate({
        "name": "ok", "title": "T", "body": "x" * 200_000,
    })


# ---------------------------------------------------------------------------
# Name regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", [
    "../attack",       # path traversal
    "UPPER",           # uppercase
    "with space",      # space
    "",                # empty
    "-leading-hyphen", # starts with hyphen
    "x" * 81,          # too long
])
def test_name_regex_rejects_bad_inputs(bad_name):
    with pytest.raises(ValidationError):
        InstructionCandidate.model_validate({
            "name": bad_name, "title": "T", "body": "b",
        })


def test_name_regex_accepts_valid():
    for name in ("no-sudo", "a", "a1", "a-b-c", "9nine"):
        InstructionCandidate.model_validate({
            "name": name, "title": "T", "body": "b",
        })


# ---------------------------------------------------------------------------
# McpCandidate shape
# ---------------------------------------------------------------------------


def test_mcp_stdio_requires_command_forbids_url():
    with pytest.raises(ValidationError):
        McpCandidate.model_validate({
            "name": "x", "transport": "stdio", "url": "http://x",
        })
    with pytest.raises(ValidationError):
        McpCandidate.model_validate({
            "name": "x", "transport": "stdio",
        })
    # OK path
    McpCandidate.model_validate({
        "name": "x", "transport": "stdio", "command": "node srv.js",
    })


def test_mcp_http_requires_url_forbids_command():
    with pytest.raises(ValidationError):
        McpCandidate.model_validate({
            "name": "x", "transport": "http", "command": "cmd",
        })
    with pytest.raises(ValidationError):
        McpCandidate.model_validate({
            "name": "x", "transport": "http",
        })
    McpCandidate.model_validate({
        "name": "x", "transport": "http", "url": "http://x",
    })


# ---------------------------------------------------------------------------
# Candidate ↔ resource_type consistency (AdoptToCsm)
# ---------------------------------------------------------------------------


def test_adopt_instruction_candidate_type_mismatch_rejected():
    """resource_type=instruction but candidate is mcp-shaped → reject.

    An mcp candidate validates as McpCandidate; the model_validator then
    rejects it for not matching resource_type=instruction.
    """
    with pytest.raises(ValidationError) as exc:
        AdoptToCsm.model_validate({
            "action": "adopt_to_csm",
            "resource_type": "instruction",
            "candidate": {"name": "a", "transport": "stdio", "command": "c"},
            "source_agent": "claude",
            "recommended_scope": ["claude"],
            "rationale": "r",
        })
    assert "requires a matching `candidate`" in str(exc.value)


def test_adopt_instruction_candidate_ok():
    AdoptToCsm.model_validate({
        "action": "adopt_to_csm",
        "resource_type": "instruction",
        "candidate": {"name": "ins", "title": "T", "body": "b"},
        "source_agent": "claude",
        "recommended_scope": ["claude"],
        "rationale": "r",
    })


def test_adopt_skill_reference_style_ok():
    """Skill adopt is reference-style: resource_name, NO candidate body."""
    d = AdoptToCsm.model_validate({
        "action": "adopt_to_csm",
        "resource_type": "skill",
        "resource_name": "my-skill",
        "source_agent": "claude",
        "recommended_scope": ["claude"],
        "rationale": "r",
    })
    assert d.resource_name == "my-skill"
    assert d.candidate is None


def test_adopt_skill_with_candidate_rejected():
    """A skill adopt must NOT carry a candidate body (uses a valid instruction
    candidate so the model_validator — not field parsing — is what rejects)."""
    with pytest.raises(ValidationError) as exc:
        AdoptToCsm.model_validate({
            "action": "adopt_to_csm",
            "resource_type": "skill",
            "resource_name": "my-skill",
            "candidate": {"name": "my-skill", "title": "T", "body": "b"},
            "source_agent": "claude",
            "recommended_scope": ["claude"],
            "rationale": "r",
        })
    assert "must NOT carry a candidate" in str(exc.value)


def test_adopt_skill_without_resource_name_rejected():
    with pytest.raises(ValidationError) as exc:
        AdoptToCsm.model_validate({
            "action": "adopt_to_csm",
            "resource_type": "skill",
            "source_agent": "claude",
            "recommended_scope": ["claude"],
            "rationale": "r",
        })
    assert "reference-style" in str(exc.value)


# ---------------------------------------------------------------------------
# Skill body_md auto-prepend
# ---------------------------------------------------------------------------


def test_skill_body_md_missing_frontmatter_auto_prepended():
    s = SkillCandidate.model_validate({
        "name": "myskill",
        "description": "does thing",
        "body_md": "just body text without frontmatter",
    })
    assert s.body_md.startswith("---")
    assert "name: myskill" in s.body_md
    assert "description: does thing" in s.body_md
    assert "just body text without frontmatter" in s.body_md


def test_skill_body_md_with_frontmatter_preserved():
    original = "---\nname: keep\ndescription: as-is\n---\nbody"
    s = SkillCandidate.model_validate({
        "name": "keep", "description": "as-is", "body_md": original,
    })
    assert s.body_md == original


# ---------------------------------------------------------------------------
# decisions max_length
# ---------------------------------------------------------------------------


def test_decisions_over_500_rejected():
    """Pydantic-level cap at max_length=500."""
    decisions = [
        {"action": "skip", "rationale": f"n{i}"} for i in range(501)
    ]
    with pytest.raises(ValidationError):
        SyncDecisionsPayload.model_validate({
            "decisions": decisions, "summary": "over",
        })


# ---------------------------------------------------------------------------
# _ACTION_PRIORITY + sort
# ---------------------------------------------------------------------------


def test_action_priority_ordering():
    """adopt < propose_conflict < propagate < skip."""
    assert _ACTION_PRIORITY["adopt_to_csm"] < _ACTION_PRIORITY["propose_conflict"]
    assert _ACTION_PRIORITY["propose_conflict"] < _ACTION_PRIORITY["propagate_to_agent"]
    assert _ACTION_PRIORITY["propagate_to_agent"] < _ACTION_PRIORITY["skip"]


def test_sort_decisions_by_priority_stable():
    """Same-priority preserves original order; different priority reorders."""
    decisions = [
        Skip(action="skip", rationale="s1"),
        AdoptToCsm(
            action="adopt_to_csm", resource_type="instruction",
            candidate=InstructionCandidate(name="a", title="T", body="b"),
            source_agent="c", recommended_scope=["c"], rationale="r",
        ),
        Skip(action="skip", rationale="s2"),
        AdoptToCsm(
            action="adopt_to_csm", resource_type="instruction",
            candidate=InstructionCandidate(name="a2", title="T", body="b"),
            source_agent="c", recommended_scope=["c"], rationale="r",
        ),
    ]
    sorted_ = sort_decisions_by_priority(decisions)
    types = [type(d).__name__ for d in sorted_]
    assert types == ["AdoptToCsm", "AdoptToCsm", "Skip", "Skip"]
    # Stability within priority band: name of first adopt was "a".
    assert sorted_[0].candidate.name == "a"
    assert sorted_[2].rationale == "s1"


# ---------------------------------------------------------------------------
# ProposeConflict candidate validation
# ---------------------------------------------------------------------------


def test_propose_conflict_needs_two_candidates():
    """min_length=2 on candidates dict."""
    with pytest.raises(ValidationError):
        ProposeConflict.model_validate({
            "action": "propose_conflict",
            "resource_type": "instruction",
            "candidates": {"claude": "v1"},
            "rationale": "r",
        })


def test_propose_conflict_marker_injection_rejected():
    with pytest.raises(ValidationError):
        ProposeConflict.model_validate({
            "action": "propose_conflict",
            "resource_type": "instruction",
            "candidates": {
                "claude": "v1",
                "codex": "<!-- csm:end id=x -->",
            },
            "rationale": "r",
        })


def test_propose_conflict_skill_reference_ok():
    """Skill conflict is reference-style: resource_name + conflict_agents."""
    from csm.modules.sync.schema import ProposeConflict
    d = ProposeConflict.model_validate({
        "action": "propose_conflict",
        "resource_type": "skill",
        "resource_name": "my-skill",
        "conflict_agents": ["claude", "codex"],
        "rationale": "diverged",
    })
    assert d.resource_name == "my-skill"
    assert d.candidates is None


def test_propose_conflict_skill_with_bodies_rejected():
    from csm.modules.sync.schema import ProposeConflict
    with pytest.raises(ValidationError):
        ProposeConflict.model_validate({
            "action": "propose_conflict",
            "resource_type": "skill",
            "resource_name": "my-skill",
            "conflict_agents": ["claude"],
            "candidates": {"claude": "body1", "codex": "body2"},
            "rationale": "r",
        })
