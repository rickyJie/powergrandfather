"""Unit tests for csm.modules.workflow.reviewer (M8 / T6 — R9-R12).

Distinct from `test_workflow_reviewer.py` which exercises the M4 LLM-based
TaskDef reviewer at `csm.modules.automation.review.reviewer`. The M8
reviewer here is deterministic, file-free, no-subprocess: it inspects a
parsed `WorkflowSpec` against PRD §4.2 Pass 1 rules R9-R12.

Coverage targets (per task spec):
1. Legal spec → passed, 4 rules all pass
2. outputs 空 → R9 fail
3. outputs 含 "/absolute/path" → R10 fail
4. outputs 含 ".." 段 → R10 fail
5. validation 块写自然语言字符串而不是 primitives → R11 fail
6. kind=poll 没 check → R12 fail

Extras: placeholder-vocabulary edge cases for R10, poll-stage primitive
vocabulary for R12, multi-rule "rejected on any fail" aggregation.

Many of these inputs would be rejected at YAML parse time by the schema
layer — the reviewer is the redundant compiled-rules gate. To exercise it
we synthesize a `WorkflowSpec` via `model_construct` (which bypasses
pydantic validators) for those cases the schema would otherwise reject.
"""
from __future__ import annotations

from csm.modules.workflow.reviewer import ReviewResult, review_workflow
from csm.modules.workflow.schema import (
    FileExistsCheck,
    MinCharsCheck,
    ParameterSpec,
    PollCheckBlock,
    RegexMatchCheck,
    StageSpec,
    ValidationBlock,
    WorkflowSpec,
    load_workflow_spec,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LEGAL_YAML = """
name: t6_legal
description: minimal workflow that should pass R9-R12
parameters:
  - {name: topic, type: string, required: true}
stages:
  - name: design
    kind: claude
    prompt: hi {params.topic}
    outputs:
      - "{ws}/01-design/design.md"
    validation:
      - file: "{ws}/01-design/design.md"
        primitives:
          - file_exists
          - min_chars: 200
  - name: wait
    kind: poll
    poll_interval: 60s
    check:
      - file: "{ws}/01-design/design.md"
        primitives: [file_exists]
  - name: ship
    kind: claude
    prompt: read {stages.design.outputs[0]}
    outputs:
      - "{ws}/02-ship/report.md"
"""


def _by_rule(result: ReviewResult, rule_id: str):
    for r in result.rules:
        if r.rule_id == rule_id:
            return r
    raise AssertionError(f"rule {rule_id!r} missing from result")


# ---------------------------------------------------------------------------
# 1. Legal spec → passed, all four rules pass
# ---------------------------------------------------------------------------


def test_legal_spec_passes_all_rules():
    spec = load_workflow_spec(LEGAL_YAML)
    result = review_workflow(spec)

    assert result.status == "passed"
    rule_ids = [r.rule_id for r in result.rules]
    assert rule_ids == ["R9", "R10", "R11", "R12", "R13",
                        "R14", "R15", "R16", "R17", "R18", "R19"]
    for r in result.rules:
        assert r.status == "pass", f"{r.rule_id} unexpectedly failed/warn: {r.reason}"
        assert r.reason == ""

    d = result.to_dict()
    assert d["status"] == "passed"
    assert {r["rule_id"] for r in d["rules"]} == {
        "R9", "R10", "R11", "R12", "R13",
        "R14", "R15", "R16", "R17", "R18", "R19",
    }


# ---------------------------------------------------------------------------
# 2. outputs empty → R9 fail
# ---------------------------------------------------------------------------


def test_r9_fails_when_claude_stage_outputs_empty():
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=[],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r9",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r9 = _by_rule(result, "R9")
    assert r9.status == "fail"
    assert "design" in r9.reason
    assert _by_rule(result, "R10").status == "pass"
    assert _by_rule(result, "R11").status == "pass"
    assert _by_rule(result, "R12").status == "pass"


# ---------------------------------------------------------------------------
# 3. outputs absolute path → R10 fail
# ---------------------------------------------------------------------------


def test_r10_fails_on_absolute_output_path():
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["/absolute/path/design.md"],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r10_abs",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r10 = _by_rule(result, "R10")
    assert r10.status == "fail"
    assert "absolute path" in r10.reason
    assert "/absolute/path/design.md" in r10.reason


# ---------------------------------------------------------------------------
# 4. outputs `..` segment → R10 fail
# ---------------------------------------------------------------------------


def test_r10_fails_on_parent_dir_traversal():
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/../escape/design.md"],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r10_dotdot",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r10 = _by_rule(result, "R10")
    assert r10.status == "fail"
    assert ".." in r10.reason


def test_r10_fails_on_illegal_placeholder():
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/{secrets.api_key}/out.md"],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r10_illegal_token",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r10 = _by_rule(result, "R10")
    assert r10.status == "fail"
    assert "secrets.api_key" in r10.reason


def test_r10_fails_on_unknown_param_reference():
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/{params.nope}/out.md"],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r10_unknown_param",
        description=None,
        parameters=[ParameterSpec(name="topic", type="string", required=True)],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r10 = _by_rule(result, "R10")
    assert "unknown parameter" in r10.reason
    assert "params.nope" in r10.reason


def test_r10_fails_on_forward_stage_reference():
    s1 = StageSpec.model_construct(
        name="early",
        kind="claude",
        prompt="hi",
        outputs=["{stages.later.outputs[0]}/derived.md"],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    s2 = StageSpec.model_construct(
        name="later",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/later.md"],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r10_forward",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[s1, s2],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r10 = _by_rule(result, "R10")
    assert "forward" in r10.reason or "unknown stage" in r10.reason


# ---------------------------------------------------------------------------
# 5. validation block uses NL string instead of primitives → R11 fail
# ---------------------------------------------------------------------------


def test_r11_fails_on_natural_language_validation():
    bogus_block = ValidationBlock.model_construct(
        file="{ws}/out.md",
        primitives=["the file should look ok"],
    )
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/out.md"],
        validation=[bogus_block],
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r11",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r11 = _by_rule(result, "R11")
    assert r11.status == "fail"
    assert "not a primitive" in r11.reason
    assert "design.validation[0].primitives[0]" in r11.reason
    assert _by_rule(result, "R9").status == "pass"
    assert _by_rule(result, "R10").status == "pass"


# ---------------------------------------------------------------------------
# 6. kind=poll missing check → R12 fail
# ---------------------------------------------------------------------------


def test_r12_fails_on_poll_stage_without_check():
    bad_stage = StageSpec.model_construct(
        name="wait",
        kind="poll",
        prompt=None,
        outputs=None,
        validation=None,
        check=None,
        poll_interval=60,
        timeout=3600,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r12_missing",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r12 = _by_rule(result, "R12")
    assert r12.status == "fail"
    assert "wait" in r12.reason
    assert "missing non-empty `check:`" in r12.reason
    assert _by_rule(result, "R9").status == "pass"


def test_r12_fails_on_poll_check_unknown_primitive():
    bad_check = PollCheckBlock.model_construct(
        file="{ws}/report.json",
        load_as=None,
        extract_field=None,
        bind_as=None,
        primitives=["please verify this manually"],
    )
    bad_stage = StageSpec.model_construct(
        name="wait",
        kind="poll",
        prompt=None,
        outputs=None,
        validation=None,
        check=[bad_check],
        poll_interval=60,
        timeout=3600,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r12_unknown_prim",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r12 = _by_rule(result, "R12")
    assert r12.status == "fail"
    assert "wait.check[0].primitives[0]" in r12.reason
    assert "not a primitive" in r12.reason


def test_r12_allows_poll_load_binding_form():
    load_check = PollCheckBlock.model_construct(
        file="{ws}/meta.json",
        load_as="json",
        extract_field="exp_path",
        bind_as="exp_path",
        primitives=None,
    )
    val_check = PollCheckBlock.model_construct(
        file="{exp_path}/report.json",
        load_as=None,
        extract_field=None,
        bind_as=None,
        primitives=[FileExistsCheck(primitive="file_exists")],
    )
    stage = StageSpec.model_construct(
        name="wait",
        kind="poll",
        prompt=None,
        outputs=None,
        validation=None,
        check=[load_check, val_check],
        poll_interval=60,
        timeout=3600,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r12_load_binding",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    r12 = _by_rule(result, "R12")
    assert r12.status == "pass", f"unexpected R12 failure: {r12.reason}"


# ---------------------------------------------------------------------------
# Cross-cutting: multiple simultaneous failures roll up to one `rejected`
# ---------------------------------------------------------------------------


def test_multiple_failures_aggregate_as_rejected():
    bad_stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=[],
        validation=None,
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    bad_poll = StageSpec.model_construct(
        name="wait",
        kind="poll",
        prompt=None,
        outputs=None,
        validation=None,
        check=None,
        poll_interval=60,
        timeout=3600,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_multi_fail",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[bad_stage, bad_poll],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    assert _by_rule(result, "R9").status == "fail"
    assert _by_rule(result, "R10").status == "pass"
    assert _by_rule(result, "R11").status == "pass"
    assert _by_rule(result, "R12").status == "fail"


# ---------------------------------------------------------------------------
# 7. R13 — section: arg on primitives must be a markdown header
# ---------------------------------------------------------------------------


def test_r13_passes_on_markdown_header_section_arg():
    """`section: '## Result'` is legal — R13 only rejects bare titles."""
    prim = MinCharsCheck(primitive="min_chars", count=100, section="## Result")
    block = ValidationBlock.model_construct(file="{ws}/out.md", primitives=[prim])
    stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/out.md"],
        validation=[block],
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r13_pass",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "passed"
    assert _by_rule(result, "R13").status == "pass"


def test_r13_fails_on_bare_title_section_arg():
    """`section: 'Result'` without `#` prefix fails at runtime — R13 catches it."""
    prim = MinCharsCheck(primitive="min_chars", count=100, section="Result")
    block = ValidationBlock.model_construct(file="{ws}/out.md", primitives=[prim])
    stage = StageSpec.model_construct(
        name="design",
        kind="claude",
        prompt="hi",
        outputs=["{ws}/out.md"],
        validation=[block],
        check=None,
        poll_interval=None,
        timeout=None,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r13_bare",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r13 = _by_rule(result, "R13")
    assert r13.status == "fail"
    assert "design.validation[0].primitives[0]" in r13.reason
    assert "'Result'" in r13.reason
    assert "markdown header" in r13.reason
    # Other rules unaffected.
    assert _by_rule(result, "R9").status == "pass"
    assert _by_rule(result, "R11").status == "pass"


def test_r13_fails_on_bare_title_in_poll_check_block():
    """R13 also gates poll-stage `check:` blocks, mirroring R11/R12."""
    prim = RegexMatchCheck(primitive="regex_match", pattern="wauc", section="Result")
    check = PollCheckBlock.model_construct(file="{ws}/train_log.txt", primitives=[prim])
    stage = StageSpec.model_construct(
        name="wait_train",
        kind="poll",
        prompt=None,
        outputs=None,
        validation=None,
        check=[check],
        poll_interval=60,
        timeout=3600,
        time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r13_poll",
        description=None,
        parameters=[],
        workspace=".claude/missions/{mission_id}",
        global_timeout=604800,
        stages=[stage],
        final_outputs=[],
    )

    result = review_workflow(spec)
    assert result.status == "rejected"
    r13 = _by_rule(result, "R13")
    assert r13.status == "fail"
    assert "wait_train.check[0].primitives[0]" in r13.reason
    assert "'Result'" in r13.reason


# ---------------------------------------------------------------------------
# 8. R14 — poll check forward-references a later stage's outputs → warn
# ---------------------------------------------------------------------------


def test_r14_warns_on_forward_stage_reference_in_poll_check():
    """Poll checks that reference a LATER stage's outputs deadlock at runtime.
    R14 catches the explicit-placeholder form (implicit path deadlocks
    remain uncatchable at spec review time).

    Uses model_construct so we can bypass schema-level forward-reference
    rejection (R10 covers `outputs`, but not poll `check.file` — that's
    exactly the R14 gap this test locks down).
    """
    from csm.modules.workflow.schema import PollCheckBlock, StageSpec

    launch = StageSpec.model_construct(
        name="launch", kind="claude", prompt="hi",
        outputs=["{ws}/launch/meta.json"],
        validation=None, check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    wait_check = PollCheckBlock.model_construct(
        file="{stages.analyze.outputs[0]}",
        primitives=[FileExistsCheck(primitive="file_exists")],
    )
    wait = StageSpec.model_construct(
        name="wait", kind="poll", prompt=None, outputs=None, validation=None,
        check=[wait_check], poll_interval=60, timeout=None,
        time_budget=None, depends_on=None,
    )
    analyze = StageSpec.model_construct(
        name="analyze", kind="claude", prompt="read", outputs=["{ws}/x.md"],
        validation=None, check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r14", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[launch, wait, analyze], final_outputs=[],
    )

    result = review_workflow(spec)
    # warn does not flip status to rejected
    assert result.status == "passed"
    r14 = _by_rule(result, "R14")
    assert r14.status == "warn"
    assert "wait.check[0].file" in r14.reason
    assert "analyze" in r14.reason


def test_r14_passes_on_backward_or_absent_stage_reference():
    """Referencing an EARLIER stage's outputs is legal (that's the point)."""
    spec = load_workflow_spec(LEGAL_YAML)
    result = review_workflow(spec)
    assert _by_rule(result, "R14").status == "pass"


# ---------------------------------------------------------------------------
# 9. R15 — poll check jsonschema is fragile → warn
# ---------------------------------------------------------------------------


def test_r15_warns_on_poll_check_jsonschema():
    """Any jsonschema on a poll stage's check → warn (per da396925 wait_eval)."""
    from csm.modules.workflow.schema import JsonschemaCheck, PollCheckBlock

    schema_prim = JsonschemaCheck(
        primitive="jsonschema",
        json_schema={"type": "object", "required": ["wauc"]},
    )
    check = PollCheckBlock.model_construct(
        file="{ws}/report.json",
        primitives=[FileExistsCheck(primitive="file_exists"), schema_prim],
    )
    wait = StageSpec.model_construct(
        name="wait", kind="poll", prompt=None, outputs=None, validation=None,
        check=[check], poll_interval=60, timeout=None, time_budget=None,
        depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r15", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[wait], final_outputs=[],
    )
    result = review_workflow(spec)
    assert result.status == "passed"  # warn does not reject
    r15 = _by_rule(result, "R15")
    assert r15.status == "warn"
    assert "wait.check[0].primitives[1]" in r15.reason


# ---------------------------------------------------------------------------
# 10. R16 — required_sections with punctuation → warn
# ---------------------------------------------------------------------------


def test_r16_warns_on_parenthetical_section_name():
    """P3 iter 2 pattern: `required_sections: ["Won't-try (do not repropose)"]`
    contains `()` — likely won't match plain heading text the model emits."""
    from csm.modules.workflow.schema import RequiredSectionsCheck, ValidationBlock

    prim = RequiredSectionsCheck(
        primitive="required_sections",
        sections=["Won't-try (do not repropose)", "Related lessons"],
    )
    block = ValidationBlock.model_construct(file="{ws}/x.md", primitives=[prim])
    stage = StageSpec.model_construct(
        name="design", kind="claude", prompt="hi", outputs=["{ws}/x.md"],
        validation=[block], check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r16", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[stage], final_outputs=[],
    )
    result = review_workflow(spec)
    assert result.status == "passed"
    r16 = _by_rule(result, "R16")
    assert r16.status == "warn"
    assert "design.validation[0].primitives[0].sections[0]" in r16.reason
    assert "Won't-try (do not repropose)" in r16.reason


# ---------------------------------------------------------------------------
# 11. R17 — regex_match with strict `[:\s]+` delimiter → warn
# ---------------------------------------------------------------------------


def test_r17_warns_on_strict_delimiter_regex():
    """`wauc[:\\s]+0\\.` rejects `wauc = 0.99` model prose (da396925 issue)."""
    prim = RegexMatchCheck(
        primitive="regex_match",
        pattern="wauc[:\\s]+0\\.[0-9]{4}",
        section="## Result",
    )
    block = ValidationBlock.model_construct(file="{ws}/x.md", primitives=[prim])
    stage = StageSpec.model_construct(
        name="analyze", kind="claude", prompt="hi", outputs=["{ws}/x.md"],
        validation=[block], check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r17", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[stage], final_outputs=[],
    )
    result = review_workflow(spec)
    assert result.status == "passed"
    r17 = _by_rule(result, "R17")
    assert r17.status == "warn"
    assert "analyze.validation[0].primitives[0]" in r17.reason


def test_r17_passes_on_looser_regex():
    """`metric.*0\\.` is the recommended form."""
    prim = RegexMatchCheck(
        primitive="regex_match", pattern="wauc.*0\\.[0-9]{4}", section="## Result",
    )
    block = ValidationBlock.model_construct(file="{ws}/x.md", primitives=[prim])
    stage = StageSpec.model_construct(
        name="analyze", kind="claude", prompt="hi", outputs=["{ws}/x.md"],
        validation=[block], check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r17_ok", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[stage], final_outputs=[],
    )
    result = review_workflow(spec)
    assert _by_rule(result, "R17").status == "pass"


# ---------------------------------------------------------------------------
# 12. R18 — tmux prose without ^csm- schema constraint → fail (P0)
# ---------------------------------------------------------------------------


def test_r18_warns_when_prompt_mentions_tmux_but_no_pattern_enforced():
    """P3 iter 3: prompt talked about tmux_session, model returned 'sim'.

    R18 is warn not fail — not every tmux mention is a CSM-owned session
    (e.g. eval-batch is external). Author should double-check but not
    be blocked.
    """
    from csm.modules.workflow.schema import ValidationBlock

    stage = StageSpec.model_construct(
        name="launch", kind="claude",
        prompt="Start a tmux_session for the training run.",
        outputs=["{ws}/meta.json"],
        validation=[
            ValidationBlock.model_construct(
                file="{ws}/meta.json",
                primitives=[FileExistsCheck(primitive="file_exists")],
            )
        ],
        check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r18", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[stage], final_outputs=[],
    )
    result = review_workflow(spec)
    assert result.status == "passed"  # warn does not reject
    r18 = _by_rule(result, "R18")
    assert r18.status == "warn"
    assert "launch" in r18.reason
    assert "^csm-" in r18.reason


def test_r18_passes_when_jsonschema_enforces_csm_prefix():
    from csm.modules.workflow.schema import JsonschemaCheck, ValidationBlock

    schema_prim = JsonschemaCheck(
        primitive="jsonschema",
        json_schema={
            "type": "object",
            "properties": {
                "tmux_session": {"type": "string", "pattern": "^csm-"},
                "tmux_socket":  {"type": "string", "pattern": "^csm-"},
            },
        },
    )
    stage = StageSpec.model_construct(
        name="launch", kind="claude",
        prompt="Start a tmux_session and tmux_socket. Both must be exact.",
        outputs=["{ws}/meta.json"],
        validation=[ValidationBlock.model_construct(
            file="{ws}/meta.json",
            primitives=[FileExistsCheck(primitive="file_exists"), schema_prim],
        )],
        check=None, poll_interval=None, timeout=None,
        time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r18_ok", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[stage], final_outputs=[],
    )
    result = review_workflow(spec)
    assert _by_rule(result, "R18").status == "pass"


def test_r18_passes_when_prompt_has_no_tmux_mention():
    """No tmux mention → rule does not apply."""
    spec = load_workflow_spec(LEGAL_YAML)
    result = review_workflow(spec)
    assert _by_rule(result, "R18").status == "pass"


# ---------------------------------------------------------------------------
# 13. R19 — implausible timeout / poll_interval ratio → warn
# ---------------------------------------------------------------------------


def test_r19_warns_on_high_attempt_count():
    """poll_interval=30s + timeout=24h → 2880 attempts (way too many)."""
    wait = StageSpec.model_construct(
        name="wait", kind="poll", prompt=None, outputs=None, validation=None,
        check=[PollCheckBlock.model_construct(
            file="{ws}/x", primitives=[FileExistsCheck(primitive="file_exists")],
        )],
        poll_interval=30, timeout=86400, time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r19", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[wait], final_outputs=[],
    )
    result = review_workflow(spec)
    assert result.status == "passed"
    r19 = _by_rule(result, "R19")
    assert r19.status == "warn"
    assert "2880 attempts" in r19.reason


def test_r19_passes_on_reasonable_ratio():
    """poll_interval=300s + timeout=7200s → 24 attempts (fine)."""
    wait = StageSpec.model_construct(
        name="wait", kind="poll", prompt=None, outputs=None, validation=None,
        check=[PollCheckBlock.model_construct(
            file="{ws}/x", primitives=[FileExistsCheck(primitive="file_exists")],
        )],
        poll_interval=300, timeout=7200, time_budget=None, depends_on=None,
    )
    spec = WorkflowSpec.model_construct(
        name="t6_r19_ok", description=None, parameters=[],
        workspace=".claude/missions/{mission_id}", global_timeout=604800,
        stages=[wait], final_outputs=[],
    )
    result = review_workflow(spec)
    assert _by_rule(result, "R19").status == "pass"
