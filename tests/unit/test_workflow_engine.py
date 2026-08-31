"""Unit tests for the M8 workflow validation engine (T8).

Covers three layers in one module so the dataflow stays visible:

1. `section_scope.extract_section` — markdown slicing rules.
2. `engine.compile_workflow` — spec → JSON-friendly compiled_rules shape.
3. `engine.validate_stage` — render path templates + run T7 primitives,
   including section-scoped primitives that go through extract_section.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from csm.modules.workflow.engine import (
    COMPILED_RULES_VERSION,
    compile_workflow,
    validate_stage,
)
from csm.modules.workflow.schema import load_workflow_spec
from csm.modules.workflow.section_scope import extract_section

# ---------------------------------------------------------------------------
# 1. section_scope.extract_section
# ---------------------------------------------------------------------------


def test_section_basic_slice_to_next_same_level():
    md = "## A\nfoo line\n## B\nbar\n"
    out = extract_section(md, "## A")
    assert out == "## A\nfoo line\n"


def test_section_keeps_deeper_subsections_inside():
    """`### Sub` is deeper than `## A`, so it must stay INSIDE A's slice.

    Only headers with depth ≤ start depth terminate the slice.
    """
    md = (
        "## A\n"
        "intro\n"
        "### Sub\n"
        "sub content\n"
        "## B\n"
        "B body\n"
    )
    out = extract_section(md, "## A")
    assert out == "## A\nintro\n### Sub\nsub content\n"


def test_section_higher_level_header_terminates():
    """A shallower header (fewer #s) ALSO terminates a section."""
    md = "## A\ntext\n# Top\nbeyond\n"
    out = extract_section(md, "## A")
    assert out == "## A\ntext\n"


def test_section_last_runs_to_eof():
    md = "## A\nfirst\n## B\nlast section no trailing header"
    out = extract_section(md, "## B")
    assert out == "## B\nlast section no trailing header"


def test_section_missing_returns_none():
    md = "## A\nbody\n"
    assert extract_section(md, "## Nope") is None


def test_section_is_case_sensitive():
    md = "## Result\nbody\n"
    assert extract_section(md, "## result") is None
    assert extract_section(md, "## Result") == "## Result\nbody\n"


def test_section_malformed_header_arg_returns_none():
    md = "## A\nbody\n"
    assert extract_section(md, "no-hash-prefix") is None
    assert extract_section(md, "") is None


# ---------------------------------------------------------------------------
# 2. compile_workflow
# ---------------------------------------------------------------------------


SAMPLE_YAML = textwrap.dedent(
    """
    name: sample_wf
    description: fixture for engine tests
    parameters:
      - name: topic
        type: string
    stages:
      - name: design
        kind: claude
        prompt: "design {params.topic}"
        outputs:
          - "{ws}/01-design/design.md"
        validation:
          - file: "{ws}/01-design/design.md"
            primitives:
              - file_exists
              - min_chars: 50
              - required_sections: ["## Goal", "## Approach"]
              - regex_match: {section: "## Goal", pattern: "wauc"}
      - name: wait_train
        kind: poll
        poll_interval: 30s
        timeout: 24h
        check:
          - file: "{ws}/02-launch/STATE.yaml"
            load_as: json
            extract_field: train_log_path
            as: train_log
          - file: "{ws}/02-launch/STATE.yaml"
            primitives:
              - file_exists
    final_outputs:
      - "{ws}/final.md"
    """
).strip()


def _spec():
    return load_workflow_spec(SAMPLE_YAML)


def test_compile_workflow_shape():
    compiled = compile_workflow(_spec())

    assert compiled["version"] == COMPILED_RULES_VERSION
    assert compiled["workflow_name"] == "sample_wf"
    assert compiled["workspace_template"] == ".workflow/missions/{mission_id}"
    assert compiled["final_outputs"] == ["{ws}/final.md"]

    stages = compiled["stages"]
    assert set(stages.keys()) == {"design", "wait_train"}

    design = stages["design"]
    assert design["kind"] == "claude"
    assert design["outputs"] == ["{ws}/01-design/design.md"]
    val = design["validation"]
    # 4 primitives in the single block → 4 compiled entries
    assert [e["primitive"] for e in val] == [
        "file_exists",
        "min_chars",
        "required_sections",
        "regex_match",
    ]
    assert all(e["path_template"] == "{ws}/01-design/design.md" for e in val)
    assert val[1]["count"] == 50
    assert val[2]["sections"] == ["## Goal", "## Approach"]
    assert val[3]["section"] == "## Goal"
    assert val[3]["pattern"] == "wauc"

    poll = stages["wait_train"]
    assert poll["kind"] == "poll"
    assert poll["poll_interval"] == 30
    assert poll["timeout"] == 86400
    check = poll["check"]
    assert len(check) == 2
    assert check[0]["kind"] == "bind"
    assert check[0]["load_as"] == "json"
    assert check[0]["bind_as"] == "train_log"
    assert check[1]["kind"] == "primitive"
    assert check[1]["primitive"] == "file_exists"


def test_compile_workflow_is_json_serializable():
    import json

    compiled = compile_workflow(_spec())
    serialized = json.dumps(compiled)
    # round-trip survives
    assert json.loads(serialized) == compiled


# ---------------------------------------------------------------------------
# 3. validate_stage
# ---------------------------------------------------------------------------


def _write_design(ws: Path, *, with_wauc: bool, with_sections: bool, body_len: int = 200) -> Path:
    (ws / "01-design").mkdir(parents=True, exist_ok=True)
    f = ws / "01-design" / "design.md"
    sections = ""
    if with_sections:
        goal_body = "wauc target 0.95\n" if with_wauc else "no metric yet\n"
        sections = (
            "## Goal\n" + goal_body + "x" * max(0, body_len - 30) + "\n"
            "## Approach\nuse the plan\n"
        )
    f.write_text("# Design doc\n" + sections, encoding="utf-8")
    return f


def test_validate_stage_pass(tmp_path: Path):
    compiled = compile_workflow(_spec())
    ws = tmp_path / "mission_ws"
    ws.mkdir()
    _write_design(ws, with_wauc=True, with_sections=True, body_len=200)

    result = validate_stage(
        compiled["stages"]["design"],
        workspace_path=str(ws),
        params={"topic": "test"},
        prior_outputs={},
    )
    assert result["pass"] is True
    assert result["failed_checks"] == []


def test_validate_stage_fail_missing_section(tmp_path: Path):
    compiled = compile_workflow(_spec())
    ws = tmp_path / "mission_ws"
    ws.mkdir()
    # Only the H1 title, no ## sections at all → required_sections + section-scoped
    # regex_match both fail; file_exists + min_chars still pass on a long body.
    f = ws / "01-design" / "design.md"
    f.parent.mkdir(parents=True)
    f.write_text("# Design doc\n" + "x" * 200, encoding="utf-8")

    result = validate_stage(
        compiled["stages"]["design"],
        workspace_path=str(ws),
        params={"topic": "test"},
        prior_outputs={},
    )
    assert result["pass"] is False
    failed_prims = [c["primitive"] for c in result["failed_checks"]]
    assert "required_sections" in failed_prims
    assert "regex_match" in failed_prims  # section-scoped: slice not found
    assert "file_exists" not in failed_prims
    assert "min_chars" not in failed_prims


def test_validate_stage_section_scoped_regex_fail_when_pattern_absent(tmp_path: Path):
    """Section exists but the regex pattern is NOT inside it → fail with a
    reason that points at the rendered file path, not the slice tmp path."""
    compiled = compile_workflow(_spec())
    ws = tmp_path / "mission_ws"
    ws.mkdir()
    _write_design(ws, with_wauc=False, with_sections=True, body_len=200)

    result = validate_stage(
        compiled["stages"]["design"],
        workspace_path=str(ws),
        params={"topic": "test"},
        prior_outputs={},
    )
    assert result["pass"] is False
    regex_fail = next(c for c in result["failed_checks"] if c["primitive"] == "regex_match")
    # Path in the report is the rendered application-level path, not a tmp slice.
    assert regex_fail["path"].endswith("01-design/design.md")


def test_validate_stage_template_rendering_uses_prior_outputs(tmp_path: Path):
    """Compile a workflow whose stage validation file references a prior stage's
    output via `{stages.X.outputs[N]}` and verify the renderer wires it."""
    yaml_text = textwrap.dedent(
        """
        name: chain_wf
        stages:
          - name: first
            kind: claude
            prompt: write
            outputs: ["{ws}/first.md"]
          - name: second
            kind: claude
            prompt: review
            outputs: ["{ws}/second.md"]
            validation:
              - file: "{stages.first.outputs[0]}"
                primitives:
                  - file_exists
        """
    ).strip()
    spec = load_workflow_spec(yaml_text)
    compiled = compile_workflow(spec)

    ws = tmp_path / "ws"
    ws.mkdir()
    first_out = ws / "first.md"
    first_out.write_text("content\n", encoding="utf-8")

    result = validate_stage(
        compiled["stages"]["second"],
        workspace_path=str(ws),
        params={},
        prior_outputs={"first": [str(first_out)]},
    )
    assert result["pass"] is True


def test_validate_stage_template_missing_param_surfaces_as_failed_check(tmp_path: Path):
    """A missing param must NOT raise — it surfaces as a failed_check so the
    orchestrator can pause the Mission with a clear reason."""
    yaml_text = textwrap.dedent(
        """
        name: pwf
        parameters:
          - name: subdir
            type: string
        stages:
          - name: s
            kind: claude
            prompt: x
            outputs: ["{ws}/{params.subdir}/out.md"]
            validation:
              - file: "{ws}/{params.subdir}/out.md"
                primitives:
                  - file_exists
        """
    ).strip()
    spec = load_workflow_spec(yaml_text)
    compiled = compile_workflow(spec)

    result = validate_stage(
        compiled["stages"]["s"],
        workspace_path=str(tmp_path),
        params={},  # forgot to pass subdir
        prior_outputs={},
    )
    assert result["pass"] is False
    assert "missing param 'subdir'" in result["failed_checks"][0]["reason"]


def test_validate_stage_poll_runs_check_primitives_only(tmp_path: Path):
    """For poll stages, validate_stage runs primitive entries from `check`
    and ignores load-binding entries (those are the poll engine's job)."""
    compiled = compile_workflow(_spec())
    ws = tmp_path / "ws"
    (ws / "02-launch").mkdir(parents=True)
    (ws / "02-launch" / "STATE.yaml").write_text("ok\n", encoding="utf-8")

    result = validate_stage(
        compiled["stages"]["wait_train"],
        workspace_path=str(ws),
        params={},
        prior_outputs={},
    )
    assert result["pass"] is True


def test_validate_stage_claude_no_validation_passes(tmp_path: Path):
    """A claude stage MAY omit `validation` (PRD §3 R12). validate_stage must
    return pass=True / empty failed_checks for such a stage."""
    yaml_text = textwrap.dedent(
        """
        name: no_val_wf
        stages:
          - name: only
            kind: claude
            prompt: x
            outputs: ["{ws}/o.md"]
        """
    ).strip()
    spec = load_workflow_spec(yaml_text)
    compiled = compile_workflow(spec)
    result = validate_stage(
        compiled["stages"]["only"],
        workspace_path=str(tmp_path),
        params={},
        prior_outputs={},
    )
    assert result == {"pass": True, "failed_checks": []}


def test_validate_stage_unknown_stage_kind_fails(tmp_path: Path):
    """If a hand-constructed compiled blob has an unknown stage kind, fail
    gracefully rather than raise (engine never crashes the orchestrator)."""
    result = validate_stage(
        {"kind": "manual", "validation": []},
        workspace_path=str(tmp_path),
        params={},
        prior_outputs={},
    )
    assert result["pass"] is False
    assert "unknown stage kind" in result["failed_checks"][0]["reason"]


# ---------------------------------------------------------------------------
# 4. End-to-end thread: compile → validate (defensive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section_arg",
    ["## Goal", "## Approach"],
)
def test_compile_preserves_section_arg(section_arg: str):
    yaml_text = textwrap.dedent(
        f"""
        name: sec_wf
        stages:
          - name: s
            kind: claude
            prompt: x
            outputs: ["{{ws}}/o.md"]
            validation:
              - file: "{{ws}}/o.md"
                primitives:
                  - min_chars: {{count: 10, section: "{section_arg}"}}
        """
    ).strip()
    spec = load_workflow_spec(yaml_text)
    compiled = compile_workflow(spec)
    entry = compiled["stages"]["s"]["validation"][0]
    assert entry["section"] == section_arg
    assert entry["count"] == 10
