"""Onboarding sanity check for newly-loaded TaskDef YAML files (F2 part 2).

Runs in TaskLoader after schema validation. Heuristic checks (no LLM needed for
v1) — pure inspection of the YAML data + cwd existence. Returns a list of
warnings; loader logs them. Does NOT block load on warnings; only schema errors
(handled separately) reject the file.
"""
from __future__ import annotations

import os
from typing import Any


def onboarding_warnings(data: dict[str, Any]) -> list[str]:
    """Heuristic checks. Returns human-readable warnings (empty if clean)."""
    warnings: list[str] = []

    # 1) cwd existence (only for absolute paths; templates with {var} can't be checked)
    cwd = data.get("cwd", "")
    if cwd and "{" not in cwd and os.path.isabs(cwd) and not os.path.isdir(cwd):
        warnings.append(f"cwd {cwd!r} does not exist on this host (template? mistyped?)")

    # 2) prompt looks substantive
    prompt = data.get("prompt", "").strip()
    if len(prompt) < 20:
        warnings.append(f"prompt is very short ({len(prompt)} chars); did you mean to include more context?")

    # 3) output_globs sanity
    globs = data.get("output_globs") or []
    if isinstance(globs, list):
        for g in globs:
            if isinstance(g, str):
                if g.startswith("/") or g.startswith(".."):
                    warnings.append(f"output_glob {g!r} is absolute or escapes cwd — will be rejected at run-time")
                if g.strip() in {"*", "**"}:
                    warnings.append(f"output_glob {g!r} matches everything — will be noisy")

    # 4) parameters referenced in prompt but undeclared
    if "{" in prompt:
        import re
        referenced = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", prompt))
        declared = {p.get("name") for p in (data.get("parameters") or [])}
        missing = referenced - declared
        if missing:
            warnings.append(f"prompt references {sorted(missing)} but none declared in parameters")

    # 5) cwd has {var} but parameter undeclared
    if "{" in cwd:
        import re
        referenced = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", cwd))
        declared = {p.get("name") for p in (data.get("parameters") or [])}
        missing = referenced - declared
        if missing:
            warnings.append(f"cwd references {sorted(missing)} but none declared in parameters")

    return warnings
