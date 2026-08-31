"""Workflow validation engine (M8 T8).

Two responsibilities:

1. **Compile** a parsed `WorkflowSpec` into a JSON-friendly `compiled_rules`
   blob stored on `workflow_definition.compiled_rules`. The runtime never
   re-reads the YAML — compiled rules are the source of truth (PRD §4.2
   Pass 2).
2. **Validate** a stage at runtime by rendering each entry's `path_template`
   against the live Mission's workspace / parameters / prior-stage outputs,
   then applying the T7 primitives to the resulting files. Returns a
   `{"pass": bool, "failed_checks": [...]}` report (PRD §4.3).

Section-scoping is handled here, not in the primitives: when an entry has
a `section` argument, the engine slices the file with `section_scope.extract_section`,
writes the slice to a temp file, then calls the same path-based primitive
on the slice. Keeps primitives pure and markdown-unaware.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from csm.modules.workflow import primitives as P
from csm.modules.workflow.schema import WorkflowSpec
from csm.modules.workflow.section_scope import extract_section

_HEADER_PREFIX_RE = re.compile(r"^#+\s+")
_PARAM_REF_RE = re.compile(r"\{params\.([a-zA-Z_][a-zA-Z0-9_]*)\}")
_STAGE_REF_RE = re.compile(r"\{stages\.([a-z][a-z0-9_]*)\.outputs\[(\d+)\]\}")
_WORKSPACE_TOKEN = "{ws}"


# Map primitive name → (callable, list of extra arg keys to pull from entry).
# Section is handled by the engine (slicing) before calling — the primitive
# itself does not need the section argument at runtime even though T7
# accepts it for API stability.
_PRIMITIVE_DISPATCH: dict[str, tuple[Any, tuple[str, ...]]] = {
    "file_exists": (P.check_file_exists, ()),
    "min_chars": (P.check_min_chars, ("count",)),
    "required_sections": (P.check_required_sections, ("sections",)),
    "regex_match": (P.check_regex_match, ("pattern",)),
    "jsonschema": (P.check_jsonschema, ("json_schema",)),
    "contains_substring": (P.check_contains_substring, ("text",)),
}


COMPILED_RULES_VERSION = 1


# ----------------------------------------------------------------------------
# Compilation (definition-time)
# ----------------------------------------------------------------------------


def compile_workflow(spec: WorkflowSpec) -> dict[str, Any]:
    """Translate a `WorkflowSpec` into a JSON-friendly `compiled_rules` blob.

    Output shape:

        {
          "version": 1,
          "workflow_name": "...",
          "workspace_template": ".workflow/missions/{mission_id}",
          "stages": {
            "<stage_name>": {
              "kind": "claude" | "poll",
              "outputs": [...],                 # claude only
              "validation": [<entry>, ...],     # claude only
              "poll_interval": int,             # poll only
              "timeout": int | None,            # poll only
              "check": [<entry>, ...]           # poll only
            }
          },
          "final_outputs": [...]
        }

    Each `validation` entry is `{"primitive": str, "path_template": str, ...args}`.
    Each `check` entry is the same shape with `"kind": "primitive"` OR a
    load-binding entry `{"kind": "bind", "path_template": str, "load_as": ..., ...}`
    preserved so the poll engine can resolve placeholders later.
    """
    stages_compiled: dict[str, Any] = {}
    for stage in spec.stages:
        entry: dict[str, Any] = {"kind": stage.kind}
        if stage.kind == "claude":
            entry["outputs"] = list(stage.outputs or [])
            entry["validation"] = _compile_validation_blocks(stage.validation or [])
        else:  # poll
            entry["poll_interval"] = stage.poll_interval
            entry["timeout"] = stage.timeout
            entry["check"] = _compile_check_blocks(stage.check or [])
        if stage.time_budget is not None:
            entry["time_budget"] = stage.time_budget
        stages_compiled[stage.name] = entry

    return {
        "version": COMPILED_RULES_VERSION,
        "workflow_name": spec.name,
        "workspace_template": spec.workspace,
        "stages": stages_compiled,
        "final_outputs": list(spec.final_outputs),
    }


def _compile_validation_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        for prim in block.primitives:
            out.append(_compile_primitive_entry(block.file, prim))
    return out


def _compile_check_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if block.command is not None:
            out.append({"kind": "command", "command": list(block.command)})
        elif block.primitives is not None:
            for prim in block.primitives:
                entry = _compile_primitive_entry(block.file, prim)
                entry["kind"] = "primitive"
                out.append(entry)
        else:
            out.append(
                {
                    "kind": "bind",
                    "path_template": block.file,
                    "load_as": block.load_as,
                    "extract_field": block.extract_field,
                    "bind_as": block.bind_as,
                }
            )
    return out


def _compile_primitive_entry(file_template: str, prim: Any) -> dict[str, Any]:
    """Project a pydantic primitive model into a JSON-serializable dict."""
    entry: dict[str, Any] = {
        "primitive": prim.primitive,
        "path_template": file_template,
    }
    for attr in ("count", "sections", "pattern", "json_schema", "text", "section"):
        if hasattr(prim, attr):
            value = getattr(prim, attr)
            if value is None:
                continue
            if isinstance(value, list):
                value = list(value)
            entry[attr] = value
    return entry


# ----------------------------------------------------------------------------
# Path template rendering
# ----------------------------------------------------------------------------


class TemplateRenderError(Exception):
    """Raised when a path_template references an undeclared param or stage output.

    Surfaced as a failed_check at validate_stage so the Mission pauses with a
    clear reason rather than crashing the orchestrator.
    """


def _render_template(
    template: str,
    workspace_path: str,
    params: dict[str, Any],
    prior_outputs: dict[str, list[str]],
) -> str:
    out = template.replace(_WORKSPACE_TOKEN, str(workspace_path))

    def _param_repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in params:
            raise TemplateRenderError(f"missing param {key!r} for template {template!r}")
        return str(params[key])

    out = _PARAM_REF_RE.sub(_param_repl, out)

    def _stage_repl(m: re.Match[str]) -> str:
        stage_name = m.group(1)
        idx = int(m.group(2))
        if stage_name not in prior_outputs:
            raise TemplateRenderError(
                f"template {template!r} references stage {stage_name!r} but no prior outputs supplied"
            )
        outs = prior_outputs[stage_name]
        if idx < 0 or idx >= len(outs):
            raise TemplateRenderError(
                f"template {template!r}: stage {stage_name!r} has {len(outs)} outputs, "
                f"requested index {idx}"
            )
        return str(outs[idx])

    out = _STAGE_REF_RE.sub(_stage_repl, out)
    return out


# ----------------------------------------------------------------------------
# Runtime validation (per stage)
# ----------------------------------------------------------------------------


def validate_stage(
    stage_compiled: dict[str, Any],
    workspace_path: str,
    params: dict[str, Any],
    prior_outputs: dict[str, list[str]],
) -> dict[str, Any]:
    """Apply a compiled stage's checks to the live workspace.

    Returns `{"pass": bool, "failed_checks": [{primitive, path, reason}, ...]}`.
    Never raises — template / IO / unknown-primitive errors all surface as
    failed_checks so a Mission can pause with a structured reason instead of
    bringing down the orchestrator.
    """
    kind = stage_compiled.get("kind")
    if kind == "claude":
        entries: list[dict[str, Any]] = list(stage_compiled.get("validation") or [])
    elif kind == "poll":
        entries = [
            e for e in (stage_compiled.get("check") or []) if e.get("kind") == "primitive"
        ]
    else:
        return {
            "pass": False,
            "failed_checks": [
                {
                    "primitive": "<engine>",
                    "path": "",
                    "reason": f"unknown stage kind: {kind!r}",
                }
            ],
        }

    failed: list[dict[str, Any]] = []
    for entry in entries:
        primitive = entry.get("primitive", "<unknown>")
        template = entry.get("path_template", "")
        try:
            rendered = _render_template(template, workspace_path, params, prior_outputs)
        except TemplateRenderError as e:
            failed.append({"primitive": primitive, "path": template, "reason": str(e)})
            continue

        result = _run_primitive(primitive, rendered, entry)
        if not result.passed:
            failed.append({"primitive": primitive, "path": rendered, "reason": result.reason})

    return {"pass": not failed, "failed_checks": failed}


def _run_primitive(name: str, path: str, entry: dict[str, Any]) -> P.CheckResult:
    """Dispatch one compiled entry to its primitive, slicing the file first
    if the entry is section-scoped."""
    if name not in _PRIMITIVE_DISPATCH:
        return P.CheckResult(False, f"primitive {name!r} not implemented in T8 engine")
    func, arg_keys = _PRIMITIVE_DISPATCH[name]

    section = entry.get("section")
    target_path = path
    tmp_path: str | None = None
    if section:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return P.CheckResult(False, f"file does not exist: {path}")
        except OSError as e:
            return P.CheckResult(False, f"cannot read {path}: {e}")
        except UnicodeDecodeError as e:
            return P.CheckResult(False, f"not utf-8 text: {path}: {e}")
        slice_ = extract_section(content, section)
        if slice_ is None:
            return P.CheckResult(False, f"{path}: section {section!r} not found")
        # NamedTemporaryFile with delete=False so the path is reusable across
        # the call; we unlink in the `finally` below regardless of outcome.
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".section.md", delete=False
        )
        try:
            tmp.write(slice_)
        finally:
            tmp.close()
        target_path = tmp.name
        tmp_path = tmp.name

    try:
        args = [entry[k] for k in arg_keys]
        # Normalize required_sections inputs: PRD §3 YAML conventionally uses
        # full header form (`"## Goal"`) but the T7 primitive expects bare
        # titles (`"Goal"`) and prepends `^#+\s+` itself. Strip the prefix
        # here so both YAML styles work without touching T7.
        if name == "required_sections":
            args[0] = [_HEADER_PREFIX_RE.sub("", s).strip() for s in args[0]]
        return func(target_path, *args)
    except KeyError as e:
        return P.CheckResult(False, f"compiled entry missing required arg {e!s} for {name!r}")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
