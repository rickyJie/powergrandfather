"""Unit tests for M9.1 flag-schema declarative UI contract.

Every adapter's `flags_schema()` must return a list of well-formed
FlagDescriptors; the API layer must serialize them via `flag_to_dict`
into a shape the frontend can consume without adapter-name branching.
"""
from __future__ import annotations

import pytest
from csm.backends.base import (
    CheckboxFlag,
    InfoBlock,
    ResumeFlag,
    SelectChoice,
    SelectFlag,
    flag_to_dict,
)
from csm.backends.claude.adapter import ClaudeAdapter
from csm.backends.codex.adapter import CodexAdapter

# ---------------------------------------------------------------------------
# flag_to_dict serialisation
# ---------------------------------------------------------------------------


def test_serialize_checkbox_flag():
    f = CheckboxFlag(
        kind="checkbox", name="skip", label="Skip perms",
        argv_flag="--dangerously-skip-permissions",
        hint="danger", default_on=True,
    )
    d = flag_to_dict(f)
    assert d["kind"] == "checkbox"
    assert d["name"] == "skip"
    assert d["argv_flag"] == "--dangerously-skip-permissions"
    assert d["default_on"] is True


def test_serialize_select_flag_with_choices():
    f = SelectFlag(
        kind="select", name="model", label="Model",
        argv_flag="--model",
        choices=(SelectChoice(value="s", label="Sonnet"),
                 SelectChoice(value="o", label="Opus")),
    )
    d = flag_to_dict(f)
    assert d["kind"] == "select"
    assert d["choices"] == [
        {"value": "s", "label": "Sonnet"},
        {"value": "o", "label": "Opus"},
    ]


def test_serialize_resume_flag():
    f = ResumeFlag(kind="resume", name="r", label="Resume", argv_flag="--resume")
    d = flag_to_dict(f)
    assert d["kind"] == "resume"
    assert d["argv_flag"] == "--resume"


def test_serialize_info_block():
    f = InfoBlock(kind="info", text="hello")
    d = flag_to_dict(f)
    assert d == {"kind": "info", "text": "hello"}


def test_serialize_unknown_type_raises():
    class NotADescriptor:
        pass
    with pytest.raises(TypeError):
        flag_to_dict(NotADescriptor())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ClaudeAdapter.flags_schema
# ---------------------------------------------------------------------------


def test_claude_default_argv():
    assert ClaudeAdapter().default_argv() == "claude --dangerously-skip-permissions"


def test_claude_has_color_and_icon():
    a = ClaudeAdapter()
    assert a.color.startswith("#")
    assert len(a.icon) == 1        # single glyph


def test_claude_flags_explain_fixed_permissions_and_include_model_and_resume():
    schema = ClaudeAdapter().flags_schema()
    kinds = {f.kind for f in schema}
    assert "checkbox" in kinds
    assert "select" in kinds
    assert "resume" in kinds
    names = {getattr(f, "name", None) for f in schema}
    assert any(f.kind == "info" and "permission" in f.text.lower() for f in schema)
    assert "model" in names
    assert "resume" in names


def test_claude_flag_argv_flags_are_valid():
    schema = ClaudeAdapter().flags_schema()
    for f in schema:
        if hasattr(f, "argv_flag"):
            assert f.argv_flag.startswith("--"), (
                f"argv_flag {f.argv_flag!r} must be a --long-flag"
            )


def test_claude_model_choices_include_defaults():
    schema = ClaudeAdapter().flags_schema()
    model = next(f for f in schema if getattr(f, "name", None) == "model")
    assert model.kind == "select"
    values = [c.value for c in model.choices]
    assert "" in values          # "default"
    assert "sonnet" in values
    assert "opus" in values
    assert "haiku" in values


# ---------------------------------------------------------------------------
# CodexAdapter.flags_schema
# ---------------------------------------------------------------------------


def test_codex_default_argv_includes_dangerous_flag():
    """Parity with Claude: codex spawns bypass the CLI's own approval
    prompt so CSM stays the single permission source of truth. The flag
    is surfaced in default_argv so the UI shows what will actually run."""
    argv = CodexAdapter().default_argv()
    assert "codex" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert 'model_reasoning_effort="xhigh"' in argv


def test_codex_has_color_and_icon():
    a = CodexAdapter()
    assert a.color.startswith("#")
    assert len(a.icon) == 1


def test_codex_flags_schema_explains_fixed_permissions_and_has_model_select():
    """The UI must not expose a checkbox that cannot remove a base argv flag.

    The enforced permission bypass is therefore an `info` block, not a
    toggle: `build_codex_argv` re-injects it, so a checkbox would lie.
    """
    schema = CodexAdapter().flags_schema()
    kinds = [f.kind for f in schema]
    assert "select" in kinds
    assert "info" in kinds

    checkboxes = [f for f in schema if f.kind == "checkbox"]
    forced = "--dangerously-bypass-approvals-and-sandbox"
    assert all(f.argv_flag != forced for f in checkboxes), (
        "a checkbox for the enforced bypass would not do anything"
    )

    select = next(f for f in schema if f.kind == "select")
    assert select.name == "model"
    assert select.argv_flag == "--model"
    values = [c.value for c in select.choices]
    assert "" in values                  # default (config.toml)
    assert "gpt-5-codex" in values       # named default
    assert any("gpt-4" in v for v in values)

    info_text = " ".join(f.text for f in schema if f.kind == "info")
    assert "approval bypass" in info_text.lower()


def test_codex_trust_checkbox_actually_changes_behaviour():
    """A checkbox is only honest if ticking it takes effect."""
    from csm.modules.session_manager.spawners import build_codex_argv

    box = next(
        f for f in CodexAdapter().flags_schema()
        if f.kind == "checkbox" and f.name == "no_trust_workspace"
    )
    assert box.default_on is False

    plain = CodexAdapter().default_argv().split()
    assert build_codex_argv(plain, "/tmp/x").trust_workspace is True

    opted_out = plain + [box.argv_flag]
    assert build_codex_argv(opted_out, "/tmp/x").trust_workspace is False


def test_a_client_that_sends_no_argv_still_gets_trusted():
    """THE case that matters most: the mobile New Session dialog posts only
    {cwd, agent, initial_prompt}, so the manager falls back to a bare
    `["codex"]`. Mobile is also the surface where the trust modal is
    invisible, so an opt-IN marker would have skipped exactly the users who
    need this. Trust must therefore be the default, not a flag."""
    from csm.modules.session_manager.spawners import build_codex_argv

    assert build_codex_argv(["codex"], "/tmp/x").trust_workspace is True


def test_csm_marker_never_reaches_the_codex_process():
    """codex's parser rejects unknown arguments, so leaking the marker would
    turn a UI toggle into a session that refuses to start."""
    from csm.modules.session_manager.spawners import build_codex_argv

    argv = build_codex_argv(CodexAdapter().default_argv().split(), "/tmp/x").argv

    assert not any(a.startswith("--csm-") for a in argv), argv


# ---------------------------------------------------------------------------
# Uniform serialisation — the /api/backends response for every adapter
# ---------------------------------------------------------------------------


def test_every_default_adapter_serialises_cleanly():
    """The frontend consumes `flag_to_dict` output verbatim. Make sure every
    adapter's schema round-trips through it without raising."""
    for adapter in (ClaudeAdapter(), CodexAdapter()):
        for f in adapter.flags_schema():
            d = flag_to_dict(f)
            assert "kind" in d
            assert d["kind"] in ("checkbox", "select", "resume", "info")
