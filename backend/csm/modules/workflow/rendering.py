"""Prompt template rendering for workflow stages (M8 night2 T4).

T3 shipped a one-trick renderer that only substituted `{params.X}` and left
every other `{...}` token verbatim. T4 implements the full placeholder
vocabulary the PRD defines (§5.2) and switches to **strict** failure:
unknown tokens or unresolvable references raise `PromptRenderError`
rather than silently leaking braces into the live claude prompt.

Supported placeholders:

| Token                              | Resolves to                                    |
|------------------------------------|------------------------------------------------|
| `{params.NAME}`                    | `params[NAME]`                                 |
| `{ws}`                             | `workspace_path`                               |
| `{mission_id}`                     | `mission_id`                                   |
| `{workflow_name}`                  | `workflow_name`                                |
| `{stages.STAGE.outputs[N]}`        | `stage_outputs[STAGE][N]` (0-indexed)          |
| `{{...}}`                          | literal `{...}` (escape, added post-Phase 1)   |

Anything else inside single `{...}` → `PromptRenderError` with an
actionable message suggesting `{{...}}` when the token doesn't match
any known shape. That is deliberate: leaving unknown tokens for "the
next runner" masks typos and, worse, silently ships bogus placeholders
to claude.
"""
from __future__ import annotations

import re
from typing import Any

# Escape sentinels used internally to pass `{{`/`}}` through the placeholder
# regex without triggering the "unknown token" branch. Chosen to be strings
# a normal user template would not contain by accident.
_ESCAPE_OPEN = "\x00CSM_ESCAPE_OPEN\x00"
_ESCAPE_CLOSE = "\x00CSM_ESCAPE_CLOSE\x00"

# Single regex captures every `{...}` token in one pass. The renderer then
# classifies each capture by shape and either substitutes or raises.
_TOKEN_RE = re.compile(r"\{([^{}]+)\}")

# Sub-patterns for the structured token shapes. Anchored on both ends so a
# stray suffix (e.g. `{params.foo bar}`) is treated as unknown rather than
# silently matching the prefix.
_PARAM_RE = re.compile(r"^params\.([a-zA-Z_][a-zA-Z0-9_]*)$")
_STAGE_OUTPUT_RE = re.compile(
    r"^stages\.([a-z][a-z0-9_]*)\.outputs\[(\d+)\]$"
)

# The simple no-arg tokens. Anything else is an error.
_LITERAL_TOKENS = frozenset({"ws", "mission_id", "workflow_name"})


class PromptRenderError(Exception):
    """Raised when a prompt template cannot be fully rendered.

    Covers all three failure modes the renderer recognises:
      - unknown placeholder syntax (e.g. `{cluster}`, `{params.}`,
        `{stages.foo.outputs[-1]}`)
      - missing `params.X` key
      - `stages.X.outputs[N]` where stage X has not produced N+1 outputs
        (either the stage is not in `stage_outputs` or the index is out
        of range)
    """


def render_prompt(
    template: str,
    *,
    params: dict[str, Any],
    workspace_path: str,
    mission_id: str,
    workflow_name: str,
    stage_outputs: dict[str, list[str]],
) -> str:
    """Render a stage's prompt template into a concrete claude prompt.

    `stage_outputs` is a mapping from already-completed stage name to its
    declared outputs **in spec order**. Stages that have not yet run must
    not be present (or pointing at an empty list) — referencing them
    raises.

    All placeholders are case-sensitive. Use `{{` / `}}` if the final
    prompt must contain literal curly braces (e.g. a JSON example, or
    the string `{ISO date}` as a fill-me-in marker for claude). The
    escape rule is single-level: `{{{{` renders as `{{`, but nested
    escaping beyond two levels isn't supported (no real use case).
    """
    errors: list[str] = []

    # Step 1 — mask `{{` and `}}` so the placeholder regex doesn't treat
    # them as unknown tokens. Restored to literal `{` / `}` at the very end.
    protected = template.replace("{{", _ESCAPE_OPEN).replace("}}", _ESCAPE_CLOSE)

    def _repl(m: re.Match[str]) -> str:
        token = m.group(1).strip()

        if token in _LITERAL_TOKENS:
            return {
                "ws": workspace_path,
                "mission_id": mission_id,
                "workflow_name": workflow_name,
            }[token]

        p_match = _PARAM_RE.match(token)
        if p_match:
            key = p_match.group(1)
            if key not in params:
                errors.append(
                    f"missing parameter {key!r} for token {{params.{key}}} "
                    f"(supplied params: {sorted(params.keys())})"
                )
                return m.group(0)
            return str(params[key])

        s_match = _STAGE_OUTPUT_RE.match(token)
        if s_match:
            stage_name = s_match.group(1)
            idx = int(s_match.group(2))
            outputs = stage_outputs.get(stage_name)
            if outputs is None:
                errors.append(
                    f"unknown stage {stage_name!r} in token {{stages.{stage_name}.outputs[{idx}]}} "
                    f"(known stages with outputs: {sorted(stage_outputs.keys())})"
                )
                return m.group(0)
            if idx >= len(outputs) or idx < 0:
                errors.append(
                    f"stage {stage_name!r} output index {idx} out of range "
                    f"(stage has {len(outputs)} output{'s' if len(outputs) != 1 else ''})"
                )
                return m.group(0)
            return outputs[idx]

        errors.append(
            f"unknown placeholder {{{token}}} — did you mean to write a "
            f"literal {{{token}}} in the final prompt? If so, escape it as "
            f"`{{{{{token}}}}}`. Known placeholders: {{ws}}, {{mission_id}}, "
            f"{{workflow_name}}, {{params.X}}, {{stages.NAME.outputs[N]}}."
        )
        return m.group(0)

    rendered = _TOKEN_RE.sub(_repl, protected)
    if errors:
        raise PromptRenderError(
            "prompt rendering failed:\n  " + "\n  ".join(errors)
        )
    # Step 3 — unmask the escapes back to single `{` / `}`.
    return rendered.replace(_ESCAPE_OPEN, "{").replace(_ESCAPE_CLOSE, "}")
