"""TaskDef parameter schema + safe `{var}` substitution (F1).

Parameter spec — list of dicts in TaskDef.parameters:
    [
      {"name": "group_id", "type": "str", "required": true, "description": "experiment group folder name"},
      {"name": "iters",    "type": "int", "default": 1, "description": "how many iters this run"},
    ]

Supported types: str, int, float, bool (used for coercion + type-check).
Substitution: `{name}` in prompt_template / cwd. Unknown placeholders left literal
(rather than raising — keeps templates resilient to renamed params).
"""
from __future__ import annotations

import re
from typing import Any

_PARAM_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_VALID_TYPES = {"str", "int", "float", "bool"}


class ParamValidationError(ValueError):
    pass


def validate_param_spec(spec: list[dict[str, Any]]) -> None:
    """Raise on malformed parameter list (called by TaskLoader)."""
    if not isinstance(spec, list):
        raise ParamValidationError(f"parameters must be a list, got {type(spec).__name__}")
    seen: set[str] = set()
    for i, p in enumerate(spec):
        if not isinstance(p, dict):
            raise ParamValidationError(f"parameters[{i}] must be a mapping")
        name = p.get("name")
        if not isinstance(name, str) or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
            raise ParamValidationError(f"parameters[{i}].name invalid: {name!r}")
        if name in seen:
            raise ParamValidationError(f"duplicate parameter name {name!r}")
        seen.add(name)
        t = p.get("type", "str")
        if t not in _VALID_TYPES:
            raise ParamValidationError(
                f"parameters[{name}].type={t!r} — must be one of {sorted(_VALID_TYPES)}"
            )


def coerce_and_check(spec: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    """Apply defaults, coerce types, enforce required. Returns the resolved dict.

    Behavior:
    - For each declared param: coerce supplied value or fill default; raise if missing+required.
    - Extra keys in `values` not declared in `spec` are passed through unchanged
      (so legacy task launches that pre-date the parameter system keep working).
    """
    by_name = {p["name"]: p for p in spec}
    # start with extras (anything not declared), then layer declared on top
    out: dict[str, Any] = {k: v for k, v in values.items() if k not in by_name}
    for name, p in by_name.items():
        t = p.get("type", "str")
        if name in values:
            try:
                out[name] = _coerce(values[name], t)
            except (TypeError, ValueError) as e:
                raise ParamValidationError(f"parameter {name!r}: {e}") from e
        elif "default" in p:
            out[name] = p["default"]
        elif p.get("required", True):
            raise ParamValidationError(f"missing required parameter {name!r}")
    return out


def _coerce(v: Any, t: str) -> Any:
    if t == "str":
        return str(v)
    if t == "int":
        return int(v)
    if t == "float":
        return float(v)
    if t == "bool":
        if isinstance(v, bool):
            return v
        s = str(v).lower()
        if s in {"true", "yes", "1", "on"}:
            return True
        if s in {"false", "no", "0", "off"}:
            return False
        raise ValueError(f"can't coerce {v!r} to bool")
    raise ValueError(f"unknown type {t!r}")


def substitute(template: str, values: dict[str, Any]) -> str:
    """Replace `{name}` in template with values[name]. Unknown placeholders kept literal."""
    def _repl(m):
        key = m.group(1)
        return str(values.get(key, m.group(0)))
    return _PARAM_PATTERN.sub(_repl, template)
