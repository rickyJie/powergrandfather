"""Unit tests for csm.modules.workflow.schema (M8 / T4).

Coverage targets (per task spec):
1. PRD §3 sample_experiment YAML parses cleanly
2. Missing `stages` → WorkflowSchemaError
3. Stage `kind` not in {claude, poll} → WorkflowSchemaError
4. Unknown primitive in validation block → WorkflowSchemaError
5. depends_on cycle / forward-reference → WorkflowSchemaError

Bonus cases (small) for coverage of normalization branches.
"""
from __future__ import annotations

import pytest
from csm.modules.workflow.schema import (
    ContainsSubstringCheck,
    FileExistsCheck,
    JsonschemaCheck,
    MinCharsCheck,
    PollCheckBlock,
    RegexMatchCheck,
    RequiredSectionsCheck,
    WorkflowSchemaError,
    WorkflowSpec,
    load_workflow_spec,
)

# ---------------------------------------------------------------------------
# Fixture: PRD §3 sample_experiment YAML (verbatim copy)
# ---------------------------------------------------------------------------

PRD_POLAR_YAML = """
name: sample_experiment
description: |
  Run one sample pipeline experiment end-to-end given a topic.

parameters:
  - {name: topic, type: string, required: true,
     description: "本次实验主题（自由文本）"}
  - {name: baseline, type: string, default: "current_sota", required: false}

workspace: ".claude/missions/{mission_id}"

global_timeout: 604800

stages:
  - name: design
    kind: claude
    prompt: |
      按 .claude/skills/experiment-design/SKILL.md 出方案。
      Topic: {params.topic}. Baseline: {params.baseline}.
      产物写到 {ws}/01-design/design.md。
    outputs:
      - "{ws}/01-design/design.md"
    validation:
      - file: "{ws}/01-design/design.md"
        primitives:
          - file_exists
          - min_chars: 200
          - required_sections:
              - "## Baseline"
              - "## Changes"
              - "## Hypothesis"
              - "## Expected wauc"
          - regex_match:
              section: "## Expected wauc"
              pattern: '0\\.[0-9]{4}'
    time_budget: 900s

  - name: launch
    kind: claude
    prompt: |
      读 {stages.design.outputs[0]}，按 experiment-launch skill 起训练。
      记录到 {ws}/02-launch/meta.json。
    outputs:
      - "{ws}/02-launch/meta.json"
    validation:
      - file: "{ws}/02-launch/meta.json"
        primitives:
          - file_exists
          - jsonschema:
              type: object
              required: [exp_path, cluster_job_id, tmux_session, started_at]
              properties:
                exp_path: {type: string}
                cluster_job_id: {type: string, minLength: 1}
                tmux_session: {type: string}
                started_at: {type: string}
    time_budget: 600s

  - name: wait_train
    kind: poll
    poll_interval: 1800s
    timeout: 86400s
    check:
      - file: "{stages.launch.outputs[0]}"
        load_as: json
        extract_field: exp_path
        as: exp_path
      - file: "{exp_path}/train_log/report.json"
        primitives:
          - file_exists
          - jsonschema:
              type: object
              required: [current]
              properties:
                current: {type: object, required: [wauc],
                          properties: {wauc: {type: number, minimum: 0, maximum: 1}}}

  - name: eval_analysis
    kind: claude
    prompt: |
      训练已完成。按 experiment-eval + experiment-analysis skill 出最终报告。
      产物写到 {ws}/04-final/final_report.md。
    outputs:
      - "{ws}/04-final/final_report.md"
    validation:
      - file: "{ws}/04-final/final_report.md"
        primitives:
          - file_exists
          - required_sections: ["## Result", "## Verdict", "## Context", "## Risks"]
          - regex_match: {section: "## Result", pattern: 'wauc[:\\s]+0\\.[0-9]{4}'}
          - regex_match: {section: "## Verdict", pattern: '\\b(PROMOTE|REJECT|RERUN-WITH-SEED)\\b'}
          - contains_substring: {section: "## Context", text: "SOTA"}
          - min_chars: {section: "## Risks", count: 80}
    time_budget: 1800s

final_outputs:
  - "{ws}/04-final/final_report.md"
"""


# ---------------------------------------------------------------------------
# 1. Happy path: PRD §3 sample_experiment YAML
# ---------------------------------------------------------------------------


def test_parses_prd_sample_experiment_yaml():
    spec = load_workflow_spec(PRD_POLAR_YAML)

    assert isinstance(spec, WorkflowSpec)
    assert spec.name == "sample_experiment"
    assert spec.workspace == ".claude/missions/{mission_id}"
    assert spec.global_timeout == 604800
    assert spec.final_outputs == ["{ws}/04-final/final_report.md"]

    # Parameters
    assert [p.name for p in spec.parameters] == ["topic", "baseline"]
    assert spec.parameters[0].required is True
    assert spec.parameters[1].default == "current_sota"

    # Stage names + kinds in order
    assert [(s.name, s.kind) for s in spec.stages] == [
        ("design", "claude"),
        ("launch", "claude"),
        ("wait_train", "poll"),
        ("eval_analysis", "claude"),
    ]

    # Time fields coerced from "900s" → 900
    assert spec.stages[0].time_budget == 900
    assert spec.stages[1].time_budget == 600
    assert spec.stages[2].poll_interval == 1800
    assert spec.stages[2].timeout == 86400
    assert spec.stages[3].time_budget == 1800

    # design stage: shorthand primitives normalized into the right submodels
    design_prims = spec.stages[0].validation[0].primitives
    assert [type(p).__name__ for p in design_prims] == [
        "FileExistsCheck",
        "MinCharsCheck",
        "RequiredSectionsCheck",
        "RegexMatchCheck",
    ]
    assert isinstance(design_prims[1], MinCharsCheck)
    assert design_prims[1].count == 200
    assert design_prims[1].section is None  # PRD form `min_chars: 200` has no section
    assert isinstance(design_prims[2], RequiredSectionsCheck)
    assert design_prims[2].sections == [
        "## Baseline",
        "## Changes",
        "## Hypothesis",
        "## Expected wauc",
    ]
    assert isinstance(design_prims[3], RegexMatchCheck)
    assert design_prims[3].section == "## Expected wauc"
    assert design_prims[3].pattern == r"0\.[0-9]{4}"

    # launch stage: jsonschema body preserved as dict
    launch_prims = spec.stages[1].validation[0].primitives
    assert isinstance(launch_prims[1], JsonschemaCheck)
    assert launch_prims[1].json_schema["type"] == "object"
    assert "exp_path" in launch_prims[1].json_schema["required"]

    # wait_train stage: poll check has both load-binding form and validation form
    wt_check = spec.stages[2].check
    assert len(wt_check) == 2
    # entry 0: load-binding form
    assert isinstance(wt_check[0], PollCheckBlock)
    assert wt_check[0].load_as == "json"
    assert wt_check[0].extract_field == "exp_path"
    assert wt_check[0].bind_as == "exp_path"  # `as:` aliased
    assert wt_check[0].primitives is None
    # entry 1: validation form
    assert wt_check[1].primitives is not None
    assert wt_check[1].load_as is None
    assert isinstance(wt_check[1].primitives[0], FileExistsCheck)
    assert isinstance(wt_check[1].primitives[1], JsonschemaCheck)

    # eval_analysis stage: section-scoped variants of contains_substring + min_chars
    eval_prims = spec.stages[3].validation[0].primitives
    # 6 primitives per PRD eval section
    assert len(eval_prims) == 6
    # contains_substring with section + text
    contains = next(p for p in eval_prims if isinstance(p, ContainsSubstringCheck))
    assert contains.section == "## Context"
    assert contains.text == "SOTA"
    # min_chars with section + count (dict form)
    minchars = next(p for p in eval_prims if isinstance(p, MinCharsCheck))
    assert minchars.section == "## Risks"
    assert minchars.count == 80


# ---------------------------------------------------------------------------
# 2. Missing `stages` → error
# ---------------------------------------------------------------------------


def test_missing_stages_field_raises():
    yaml_text = """
name: x
description: missing stages on purpose
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    msg = str(ei.value)
    assert "stages" in msg
    assert "Field required" in msg or "field required" in msg.lower()


def test_empty_stages_list_raises():
    yaml_text = """
name: x
stages: []
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "stage" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# 3. Stage `kind` not in {claude, poll} → error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_kind", ["manual", "parallel", "conditional", "subworkflow", ""])
def test_unsupported_stage_kind_raises(bad_kind):
    yaml_text = f"""
name: x
stages:
  - name: a
    kind: {bad_kind or "''"}
    prompt: hi
    outputs:
      - "{{ws}}/a"
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    msg = str(ei.value)
    # pydantic Literal error message
    assert "claude" in msg and "poll" in msg


# ---------------------------------------------------------------------------
# 4. Unknown primitive → error
# ---------------------------------------------------------------------------


def test_unknown_primitive_raises():
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs:
      - "{ws}/a"
    validation:
      - file: "{ws}/a"
        primitives:
          - bogus_check: 42
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    msg = str(ei.value)
    assert "bogus_check" in msg
    assert "unknown primitive" in msg


def test_min_chars_without_count_raises():
    """Mapping-form min_chars must give either int (count) or dict with count."""
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs: ["{ws}/a"]
    validation:
      - file: "{ws}/a"
        primitives:
          - min_chars:
              section: "## X"
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    # pydantic complains about missing `count` field on MinCharsCheck
    assert "count" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# 5. depends_on cycle / forward-reference → error
# ---------------------------------------------------------------------------


def test_depends_on_forward_reference_raises():
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs: ["{ws}/a"]
    depends_on: [b]    # b appears later — forward ref
  - name: b
    kind: claude
    prompt: hi
    outputs: ["{ws}/b"]
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "depends_on" in str(ei.value)
    assert "'b'" in str(ei.value)


def test_depends_on_self_cycle_raises():
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs: ["{ws}/a"]
    depends_on: [a]    # self-cycle
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "cycle" in str(ei.value).lower() or "depends_on" in str(ei.value)


def test_depends_on_linear_chain_ok():
    """Legal backwards reference: stage b can depend_on stage a (declared first)."""
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs: ["{ws}/a"]
  - name: b
    kind: claude
    prompt: hi
    outputs: ["{ws}/b"]
    depends_on: [a]
"""
    spec = load_workflow_spec(yaml_text)
    assert spec.stages[1].depends_on == ["a"]


# ---------------------------------------------------------------------------
# Additional coverage: kind-conditional structural checks
# ---------------------------------------------------------------------------


def test_claude_stage_missing_outputs_raises():
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "outputs" in str(ei.value)


def test_poll_stage_missing_check_raises():
    yaml_text = """
name: x
stages:
  - name: w
    kind: poll
    poll_interval: 60s
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "check" in str(ei.value)


def test_poll_stage_missing_poll_interval_raises():
    yaml_text = """
name: x
stages:
  - name: w
    kind: poll
    check:
      - file: "{ws}/r.json"
        primitives: [file_exists]
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "poll_interval" in str(ei.value)


def test_poll_check_mixing_load_and_validation_raises():
    """A single poll check entry cannot have BOTH load_as+as AND primitives."""
    yaml_text = """
name: x
stages:
  - name: w
    kind: poll
    poll_interval: 60s
    check:
      - file: "{ws}/r.json"
        load_as: json
        extract_field: foo
        as: foo
        primitives: [file_exists]
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "load" in str(ei.value).lower() or "primitives" in str(ei.value)


def test_duration_suffixes_parsed():
    yaml_text = """
name: x
global_timeout: 7d
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs: ["{ws}/a"]
    time_budget: 30m
  - name: w
    kind: poll
    poll_interval: 5m
    timeout: 2h
    check:
      - file: "{ws}/r.json"
        primitives: [file_exists]
"""
    spec = load_workflow_spec(yaml_text)
    assert spec.global_timeout == 7 * 86400
    assert spec.stages[0].time_budget == 30 * 60
    assert spec.stages[1].poll_interval == 5 * 60
    assert spec.stages[1].timeout == 2 * 3600


def test_duplicate_stage_names_raises():
    yaml_text = """
name: x
stages:
  - {name: a, kind: claude, prompt: hi, outputs: ["{ws}/a"]}
  - {name: a, kind: claude, prompt: hi, outputs: ["{ws}/a2"]}
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "duplicate" in str(ei.value).lower()


def test_invalid_yaml_raises_with_helpful_message():
    """YAML syntax error → WorkflowSchemaError with 'YAML parse error' prefix."""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec("name: x\n  bad: : :\n")
    assert "YAML parse error" in str(ei.value)


def test_empty_yaml_raises():
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec("")
    assert "empty" in str(ei.value).lower()


def test_top_level_not_mapping_raises():
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec("- just\n- a\n- list\n")
    assert "mapping" in str(ei.value).lower()


def test_bad_regex_pattern_raises():
    yaml_text = """
name: x
stages:
  - name: a
    kind: claude
    prompt: hi
    outputs: ["{ws}/a"]
    validation:
      - file: "{ws}/a"
        primitives:
          - regex_match: {pattern: '['}    # unterminated character class
"""
    with pytest.raises(WorkflowSchemaError) as ei:
        load_workflow_spec(yaml_text)
    assert "regex" in str(ei.value).lower()
