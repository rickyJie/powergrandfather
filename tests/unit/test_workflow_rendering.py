"""Tests for the workflow stage prompt renderer (M8 night2 T4).

The renderer must support five placeholder shapes — `{params.X}`, `{ws}`,
`{mission_id}`, `{workflow_name}`, `{stages.X.outputs[N]}` — and **fail
loudly** on any other `{...}` token, on missing params, and on
out-of-range stage output indices. T3's old behavior (leave unknown
tokens verbatim) was deliberately reversed for T4 because by T4 we no
longer have a downstream renderer to pick up the slack.
"""
from __future__ import annotations

import pytest
from csm.modules.workflow.rendering import PromptRenderError, render_prompt


def _common() -> dict[str, object]:
    """Default kwargs for render_prompt — overridden per-test."""
    return {
        "params": {"topic": "sample_pipeline", "rounds": 3},
        "workspace_path": "/tmp/csm-ws/mission-abc",
        "mission_id": "mission-abc",
        "workflow_name": "sample_experiment",
        "stage_outputs": {
            "design": ["/tmp/csm-ws/mission-abc/design.md"],
            "code": [
                "/tmp/csm-ws/mission-abc/impl.py",
                "/tmp/csm-ws/mission-abc/impl_test.py",
            ],
        },
    }


def test_render_params():
    """`{params.X}` resolves from the params dict (str-coerced for non-strs)."""
    out = render_prompt(
        "build {params.topic} for {params.rounds} rounds",
        **_common(),
    )
    assert out == "build sample_pipeline for 3 rounds"


def test_render_ws_mission_id_workflow_name():
    """`{ws}` / `{mission_id}` / `{workflow_name}` resolve to the named args."""
    out = render_prompt(
        "workspace={ws} mission={mission_id} wf={workflow_name}",
        **_common(),
    )
    assert (
        out
        == "workspace=/tmp/csm-ws/mission-abc mission=mission-abc wf=sample_experiment"
    )


def test_render_stage_outputs_index():
    """`{stages.X.outputs[N]}` returns the N-th output of stage X."""
    out = render_prompt(
        "design={stages.design.outputs[0]} impl={stages.code.outputs[1]}",
        **_common(),
    )
    assert (
        out
        == "design=/tmp/csm-ws/mission-abc/design.md impl=/tmp/csm-ws/mission-abc/impl_test.py"
    )


def test_render_missing_param_raises():
    """A `{params.X}` token whose key is not in params must raise."""
    common = _common()
    common["params"] = {}  # nothing supplied
    with pytest.raises(PromptRenderError) as ei:
        render_prompt("write {params.topic}", **common)
    msg = str(ei.value)
    assert "topic" in msg
    assert "{params.topic}" in msg


def test_render_unknown_token_raises():
    """Anything else in `{...}` (typos, half-templates) must raise loudly."""
    with pytest.raises(PromptRenderError) as ei:
        render_prompt("hello {cluster}", **_common())
    assert "{cluster}" in str(ei.value)

    # Also: `{params.}` (empty name) is structurally invalid → unknown token.
    with pytest.raises(PromptRenderError):
        render_prompt("oops {params.}", **_common())


def test_render_stage_outputs_out_of_range_raises():
    """Index past the stage's outputs list must raise (not silently empty)."""
    with pytest.raises(PromptRenderError) as ei:
        # `design` has 1 output, index 1 is out of range.
        render_prompt("oops {stages.design.outputs[1]}", **_common())
    msg = str(ei.value)
    assert "design" in msg
    assert "out of range" in msg

    # Unknown stage name should also raise (different but related error).
    with pytest.raises(PromptRenderError) as ei2:
        render_prompt("oops {stages.never_ran.outputs[0]}", **_common())
    assert "never_ran" in str(ei2.value)


def test_render_escape_double_brace_produces_literal():
    """`{{X}}` renders as `{X}` literally — protects markers like `{ISO date}`."""
    out = render_prompt(
        "Generated {{ISO date}} for {params.topic}",
        **_common(),
    )
    assert out == "Generated {ISO date} for sample_pipeline"


def test_render_escape_with_placeholders_around():
    """Escaped braces coexist with real placeholders in one line."""
    out = render_prompt(
        "write to {ws}/report.md, header line `# Generated {{ISO date}}`",
        **_common(),
    )
    assert "/tmp/csm-ws/mission-abc/report.md" in out
    assert "{ISO date}" in out
    assert "{ws}" not in out  # ws was actually substituted


def test_render_unknown_token_error_suggests_escape():
    """Unknown-placeholder error message must mention `{{...}}` escape hint."""
    with pytest.raises(PromptRenderError) as ei:
        render_prompt("Generated {ISO date}", **_common())
    msg = str(ei.value)
    assert "{ISO date}" in msg
    assert "{{ISO date}}" in msg  # the suggested escape
