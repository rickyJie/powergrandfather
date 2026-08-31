"""Unit tests for `build_codex_argv`.

Focus on the M3 contract fix: non-codex `base_argv` must be a STRICT
pass-through — the previous version silently appended `initial_prompt`
to bash-style test overrides, which turned `["bash", "-i"]` into
`["bash", "-i", "<prompt>"]` (bash interprets the trailing arg as a
script path) and leaked the prompt to `/proc/<pid>/cmdline`.

Plus baseline coverage of the real-codex argv construction (`-C`, `-s`,
`--dangerously-*`, positional prompt).
"""
from __future__ import annotations

from csm.modules.session_manager.spawners import build_codex_argv

# ---------------------------------------------------------------------------
# M3: strict pass-through for non-codex argv[0]
# ---------------------------------------------------------------------------


def test_non_codex_argv_is_strict_passthrough_no_prompt_leak():
    result = build_codex_argv(
        base_argv=["bash", "-i"],
        cwd="/tmp/proj",
        initial_prompt="secret user prompt",
    )
    # Prompt must NOT be appended — old bug leaked it to /proc/<pid>/cmdline.
    assert result.argv == ["bash", "-i"]
    assert result.prompt_appended is False


def test_non_codex_argv_no_codex_only_flags_injected():
    """Test override argv must not be polluted with -C/-s/--dangerously-*."""
    result = build_codex_argv(
        base_argv=["/bin/sh", "-c", "echo hi"],
        cwd="/anywhere",
    )
    assert result.argv == ["/bin/sh", "-c", "echo hi"]
    assert result.prompt_appended is False


def test_empty_argv_returns_empty():
    result = build_codex_argv(base_argv=[], cwd="/x", initial_prompt="p")
    assert result.argv == []
    assert result.prompt_appended is False


# ---------------------------------------------------------------------------
# Real codex path: builds the expected invocation
# ---------------------------------------------------------------------------


def test_codex_argv_injects_cwd_sandbox_and_dangerous_flag():
    result = build_codex_argv(base_argv=["codex"], cwd="/data/proj")
    argv = result.argv
    assert argv[0] == "codex"
    assert "-C" in argv and argv[argv.index("-C") + 1] == "/data/proj"
    assert "-s" in argv and argv[argv.index("-s") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="xhigh"'


def test_codex_argv_preserves_explicit_reasoning_effort():
    result = build_codex_argv(
        base_argv=["codex", "-c", 'model_reasoning_effort="medium"'],
        cwd="/x",
    )
    argv = result.argv
    assert argv.count("-c") == 1
    assert 'model_reasoning_effort="medium"' in argv
    assert 'model_reasoning_effort="xhigh"' not in argv


def test_codex_argv_preserves_equals_form_reasoning_effort():
    result = build_codex_argv(
        base_argv=["codex", '--config=model_reasoning_effort="high"'],
        cwd="/x",
    )
    assert 'model_reasoning_effort="xhigh"' not in result.argv


def test_codex_argv_appends_prompt_positionally():
    result = build_codex_argv(
        base_argv=["codex"],
        cwd="/x",
        initial_prompt="review this PR",
    )
    assert result.argv[-1] == "review this PR"
    assert result.prompt_appended is True


def test_codex_argv_no_prompt_means_no_append():
    result = build_codex_argv(base_argv=["codex"], cwd="/x")
    assert "review this PR" not in result.argv
    assert result.prompt_appended is False


def test_codex_argv_respects_existing_cwd_flag_dash_C():
    """If caller already passed -C, don't inject a second one."""
    result = build_codex_argv(
        base_argv=["codex", "-C", "/manual"],
        cwd="/should-be-ignored",
    )
    # Only one -C, and it's the caller's.
    assert result.argv.count("-C") == 1
    assert "/should-be-ignored" not in result.argv


def test_codex_argv_respects_existing_cwd_flag_double_dash_cd():
    """--cd is the long form of -C."""
    result = build_codex_argv(
        base_argv=["codex", "--cd", "/manual"],
        cwd="/should-be-ignored",
    )
    assert "-C" not in result.argv  # long form was already present, don't add short
    assert "/should-be-ignored" not in result.argv


def test_codex_argv_respects_existing_sandbox_flag():
    result = build_codex_argv(
        base_argv=["codex", "-s", "read-only"],
        cwd="/x",
    )
    # -s appears once with caller's value
    assert result.argv.count("-s") == 1
    assert "workspace-write" not in result.argv


def test_codex_argv_can_opt_out_of_dangerous_flag():
    result = build_codex_argv(
        base_argv=["codex"],
        cwd="/x",
        allow_dangerous=False,
    )
    assert "--dangerously-bypass-approvals-and-sandbox" not in result.argv


def test_codex_argv_extra_args_appended_before_prompt():
    result = build_codex_argv(
        base_argv=["codex"],
        cwd="/x",
        initial_prompt="do stuff",
        extra_args=["--model", "gpt-5"],
    )
    argv = result.argv
    # Prompt is the LAST arg
    assert argv[-1] == "do stuff"
    # extra_args land before it
    assert "--model" in argv and "gpt-5" in argv
    assert argv.index("--model") < argv.index("do stuff")


def test_codex_argv_custom_sandbox_mode():
    result = build_codex_argv(
        base_argv=["codex"],
        cwd="/x",
        sandbox_mode="read-only",
    )
    assert result.argv[result.argv.index("-s") + 1] == "read-only"


def test_codex_argv_result_has_no_session_id():
    """CodexArgvResult.codex_session_id is always None at build time —
    codex has no --session-id, id comes from the rollout file post-hoc."""
    result = build_codex_argv(base_argv=["codex"], cwd="/x")
    assert result.codex_session_id is None


def test_codex_resume_places_id_before_optional_prompt():
    result = build_codex_argv(
        base_argv=["codex"],
        cwd="/workspace",
        resume_from="019c-session-id",
        initial_prompt="continue the review",
    )
    argv = result.argv
    assert argv[0] == "codex"
    assert "resume" in argv
    assert argv[-2:] == ["019c-session-id", "continue the review"]
    assert argv.index("-s") > argv.index("resume")


def test_non_codex_resume_is_still_strict_passthrough():
    result = build_codex_argv(
        base_argv=["bash", "-i"],
        cwd="/workspace",
        resume_from="must-not-leak",
    )
    assert result.argv == ["bash", "-i"]
