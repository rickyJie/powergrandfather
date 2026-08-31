"""Pydantic schema for workflow YAML (PRD §3).

This is the **schema layer only** — it parses + structurally validates a
workflow YAML document. No file I/O, no rendering of placeholders, no
runtime execution. Those live in later M8 tasks:

- T5 will use `load_workflow_spec` from a loader that reads `tasks/*.workflow.yaml`,
  upserts `WorkflowDefinition` rows, and triggers reviewer/compilation.
- T6 (reviewer R9-R12) inspects the resulting `WorkflowSpec` and emits compiled rules.
- T7 implements the 6 validation primitives as runtime pure functions.
- T8 (engine) renders placeholders + applies primitives at stage finalization.

PRD §3 YAML supports several **shorthand forms** for primitives inside a
validation block, e.g.:

    primitives:
      - file_exists                          # bare string
      - min_chars: 200                       # single-key mapping with scalar
      - required_sections: ["## A", "## B"]  # single-key mapping with list
      - regex_match: {section: "## X", pattern: 'foo'}   # single-key mapping with dict

We normalize every form to `{primitive: <name>, ...args}` BEFORE pydantic
runs the discriminated union — that keeps the discriminator field simple
and gives clean errors for unknown primitives.

Design choices worth knowing:

1. Discriminated union via `Annotated[Union[...], Field(discriminator="primitive")]`
   — the pydantic v2 canonical form. Each primitive submodel has
   `primitive: Literal["..."]` as its tag.
2. Time fields (`time_budget`, `poll_interval`, `timeout`, `global_timeout`)
   accept the PRD's `"900s"` / `"30m"` / `"24h"` / `"7d"` suffix syntax
   AND plain ints. They are coerced to int-seconds at parse time.
3. The JSON schema body for `jsonschema:` is stored on the model under the
   field name `json_schema` (not `schema`) to avoid colliding with
   pydantic's `BaseModel.model_json_schema()` method name in introspection.
4. `depends_on` is forward-compat per PRD §3 "Schema notes": we accept it,
   reject any value that references a stage not declared earlier in the array
   (covers both cycles AND forward references — sufficient for MVP linear).
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class WorkflowSchemaError(Exception):
    """Raised when workflow YAML fails parsing or structural validation.

    Message always includes a human-readable location pointer (which field,
    which stage, which validation block) so the user can fix the YAML
    without re-running the loader to bisect.
    """


# ----------------------------------------------------------------------------
# Duration parsing
# ----------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])?$")


def _parse_duration(v: Any) -> int | None:
    """Parse '900s' / '30m' / '24h' / '7d' / bare int → seconds.

    Returns None for None input (so optional fields stay None).
    Raises ValueError on malformed input — pydantic wraps this into a
    ValidationError with a useful loc.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"duration cannot be bool: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        m = _DURATION_RE.match(v.strip())
        if not m:
            raise ValueError(
                f"invalid duration {v!r}: expected '<int>[s|m|h|d]' (e.g. '900s', '30m', '24h', '7d')"
            )
        n = int(m.group(1))
        unit = m.group(2) or "s"
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return n * mult
    raise ValueError(f"duration must be int or str, got {type(v).__name__}: {v!r}")


# ----------------------------------------------------------------------------
# Primitive checks (discriminated union by `primitive` field)
# ----------------------------------------------------------------------------


class FileExistsCheck(BaseModel):
    """`file_exists` — file present and non-empty (PRD §4.1)."""

    primitive: Literal["file_exists"]


class MinCharsCheck(BaseModel):
    """`min_chars` — character count ≥ count, optionally section-scoped."""

    primitive: Literal["min_chars"]
    count: int = Field(ge=0)
    section: str | None = None


class MinSizeBytesCheck(BaseModel):
    """`min_size_bytes` — byte count ≥ count, optionally section-scoped."""

    primitive: Literal["min_size_bytes"]
    count: int = Field(ge=0)
    section: str | None = None


class RequiredSectionsCheck(BaseModel):
    """`required_sections` — all listed markdown headers present."""

    primitive: Literal["required_sections"]
    sections: list[str] = Field(min_length=1)


class RegexMatchCheck(BaseModel):
    """`regex_match` — `re.search(pattern, content)` truthy, optionally section-scoped."""

    primitive: Literal["regex_match"]
    pattern: str
    section: str | None = None

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex {v!r}: {e}") from e
        return v


class JsonschemaCheck(BaseModel):
    """`jsonschema` — validate file (as JSON) against the given schema.

    Stored as `json_schema` to avoid confusion with `BaseModel.model_json_schema()`.
    On YAML the user writes `jsonschema: {...}`; the normalizer maps the dict to
    this field.
    """

    primitive: Literal["jsonschema"]
    json_schema: dict[str, Any]


class ContainsSubstringCheck(BaseModel):
    """`contains_substring` — `text` is a substring of content, optionally section-scoped."""

    primitive: Literal["contains_substring"]
    text: str = Field(min_length=1)
    section: str | None = None


PrimitiveCheck = Annotated[
    FileExistsCheck
    | MinCharsCheck
    | MinSizeBytesCheck
    | RequiredSectionsCheck
    | RegexMatchCheck
    | JsonschemaCheck
    | ContainsSubstringCheck,
    Field(discriminator="primitive"),
]


_KNOWN_PRIMITIVES = {
    "file_exists",
    "min_chars",
    "min_size_bytes",
    "required_sections",
    "regex_match",
    "jsonschema",
    "contains_substring",
}


def _normalize_primitive(p: Any) -> dict[str, Any]:
    """Convert PRD shorthand → `{primitive: <name>, ...args}`.

    Raises ValueError with a clear message on unknown primitive or malformed
    entry; pydantic wraps with a loc pointing back to the offending list index.
    """
    if isinstance(p, str):
        if p not in _KNOWN_PRIMITIVES:
            raise ValueError(
                f"unknown primitive {p!r}; must be one of: {sorted(_KNOWN_PRIMITIVES)}"
            )
        # Bare-string form only legal for argless primitives.
        if p != "file_exists":
            raise ValueError(
                f"primitive {p!r} requires arguments; use mapping form (e.g. '{p}: ...')"
            )
        return {"primitive": p}

    if isinstance(p, dict):
        # Already-normalized dict (has explicit `primitive` key): pass through.
        if "primitive" in p:
            name = p["primitive"]
            if name not in _KNOWN_PRIMITIVES:
                raise ValueError(
                    f"unknown primitive {name!r}; must be one of: {sorted(_KNOWN_PRIMITIVES)}"
                )
            return p

        # PRD shorthand form: single-key dict.
        if len(p) != 1:
            raise ValueError(
                f"primitive entry must be a single-key mapping or bare string; got keys {list(p.keys())!r}"
            )
        key, value = next(iter(p.items()))

        if key not in _KNOWN_PRIMITIVES:
            raise ValueError(
                f"unknown primitive {key!r}; must be one of: {sorted(_KNOWN_PRIMITIVES)}"
            )

        if key == "file_exists":
            # Mapping form for an argless primitive is tolerated only if value is empty/None.
            if value not in (None, {}, []):
                raise ValueError("primitive 'file_exists' takes no arguments")
            return {"primitive": "file_exists"}

        if key in ("min_chars", "min_size_bytes"):
            if isinstance(value, int) and not isinstance(value, bool):
                return {"primitive": key, "count": value}
            if isinstance(value, dict):
                return {"primitive": key, **value}
            raise ValueError(
                f"primitive {key!r} expects int (e.g. '{key}: 200') or mapping "
                f"(e.g. '{key}: {{count: 80, section: \"## X\"}}'); got {type(value).__name__}"
            )

        if key == "required_sections":
            if not isinstance(value, list):
                raise ValueError(
                    f"primitive 'required_sections' expects a list of headers, got {type(value).__name__}"
                )
            return {"primitive": "required_sections", "sections": value}

        if key == "regex_match":
            if not isinstance(value, dict):
                raise ValueError(
                    "primitive 'regex_match' expects a mapping with at least 'pattern'"
                )
            return {"primitive": "regex_match", **value}

        if key == "jsonschema":
            if not isinstance(value, dict):
                raise ValueError("primitive 'jsonschema' expects a mapping (the JSON schema)")
            return {"primitive": "jsonschema", "json_schema": value}

        if key == "contains_substring":
            if not isinstance(value, dict):
                raise ValueError(
                    "primitive 'contains_substring' expects a mapping with at least 'text'"
                )
            return {"primitive": "contains_substring", **value}

    raise ValueError(f"invalid primitive entry: {p!r}")


# ----------------------------------------------------------------------------
# Validation block
# ----------------------------------------------------------------------------


class ValidationBlock(BaseModel):
    """One entry under a stage's `validation:` list.

    Each block targets ONE file and lists 1..N primitive checks applied in order.
    """

    file: str = Field(min_length=1)
    primitives: list[PrimitiveCheck] = Field(min_length=1)

    @field_validator("primitives", mode="before")
    @classmethod
    def _normalize_primitives(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        return [_normalize_primitive(item) for item in v]


# ----------------------------------------------------------------------------
# Poll-stage check block
# ----------------------------------------------------------------------------


class PollCheckBlock(BaseModel):
    """One entry under a `poll` stage's `check:` list.

    Three mutually-exclusive forms:

    1. **Load-binding form** (PRD §3 wait_train example) — read a file,
       extract a JSON field, bind it as a placeholder for later entries:
           {file: ..., load_as: json, extract_field: ..., as: ...}

    2. **Validation form** — apply primitives to a file (same vocabulary as
       a `claude` stage's `validation` block):
           {file: ..., primitives: [...]}

    3. **Shell-exec form** (M8 night2 T5) — run an external command; the
       check passes iff exit code is 0. Useful for `squeue` / `kubectl get`
       / `cluster job status` style polling that can't be reduced to a file
       primitive:
           {command: ["squeue", "-j", "12345", "-h"]}
    """

    model_config = ConfigDict(populate_by_name=True)

    # Optional now: shell-exec form has no target file.
    file: str | None = Field(default=None, min_length=1)

    # Load-binding form
    load_as: Literal["json", "text"] | None = None
    extract_field: str | None = None
    bind_as: str | None = Field(default=None, alias="as")

    # Validation form
    primitives: list[PrimitiveCheck] | None = None

    # Shell-exec form
    command: list[str] | None = None

    @field_validator("primitives", mode="before")
    @classmethod
    def _normalize_primitives(cls, v: Any) -> Any:
        if v is None:
            return None
        if not isinstance(v, list):
            return v
        return [_normalize_primitive(item) for item in v]

    @field_validator("command", mode="before")
    @classmethod
    def _check_command(cls, v: Any) -> Any:
        if v is None:
            return None
        if not isinstance(v, list) or not v:
            raise ValueError(
                "poll check `command` must be a non-empty list of strings"
            )
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item:
                raise ValueError(
                    f"poll check `command[{i}]` must be a non-empty string, got {item!r}"
                )
        return list(v)

    @model_validator(mode="after")
    def _check_form(self) -> PollCheckBlock:
        is_load = any(x is not None for x in (self.load_as, self.extract_field, self.bind_as))
        is_validation = self.primitives is not None
        is_command = self.command is not None
        chosen = [name for name, flag in (
            ("load-binding", is_load),
            ("validation", is_validation),
            ("shell-exec", is_command),
        ) if flag]
        if len(chosen) > 1:
            raise ValueError(
                f"poll check entry must use exactly one form; got: {chosen}"
            )
        if not chosen:
            raise ValueError(
                "poll check entry must be one of: load-binding "
                "(load_as + extract_field + as), validation (primitives), "
                "or shell-exec (command)"
            )
        if is_load:
            missing = [
                name for name, val in (
                    ("load_as", self.load_as),
                    ("extract_field", self.extract_field),
                    ("as", self.bind_as),
                ) if val is None
            ]
            if missing:
                raise ValueError(
                    f"poll load-binding form requires all of (load_as, extract_field, as); missing: {missing}"
                )
            if self.file is None:
                raise ValueError("poll load-binding form requires `file`")
        if is_validation and self.file is None:
            raise ValueError("poll validation form (primitives) requires `file`")
        if is_command and self.file is not None:
            raise ValueError(
                "poll shell-exec form (command) must not declare `file`"
            )
        return self


# ----------------------------------------------------------------------------
# Stage
# ----------------------------------------------------------------------------


_STAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class StageSpec(BaseModel):
    """One entry under workflow `stages:`. See PRD §3 / §5."""

    name: str
    # What the stage DOES, not which CLI runs it. "claude" means "hand a
    # prompt to an agent and wait"; "poll" means "re-run a shell check until
    # it passes". The name is historical — it predates multi-agent support —
    # and every `kind == "claude"` test in this package means "is this an
    # agent stage?". Which adapter actually runs it comes from `agent` below,
    # falling back to the workflow's `default_agent` then the user's
    # preference, so an agent stage happily runs codex. Renaming the literal
    # would invalidate every workflow YAML on disk, so it stays.
    kind: Literal["claude", "poll"]
    prompt: str | None = None
    outputs: list[str] | None = None
    validation: list[ValidationBlock] | None = None

    # Poll-only
    poll_interval: int | None = None
    timeout: int | None = None
    check: list[PollCheckBlock] | None = None

    # Shared
    time_budget: int | None = None

    # Forward-compat (PRD §3 "Schema notes" — arrays are linear in MVP)
    depends_on: list[str] | None = None

    # Multi-agent v2: which CLI-adapter to run this stage with.
    # None = follow the workflow-level `default_agent`, which itself
    # falls back to the user's UserPreference.default_agent.
    # Kept optional so existing workflow YAMLs (which don't mention
    # agents at all) still validate and route to claude via the resolver.
    agent: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _STAGE_NAME_RE.match(v):
            raise ValueError(
                f"stage name {v!r} must match [a-z][a-z0-9_]* (lowercase + underscore)"
            )
        return v

    @field_validator("time_budget", "poll_interval", "timeout", mode="before")
    @classmethod
    def _coerce_duration(cls, v: Any) -> Any:
        return _parse_duration(v)

    @model_validator(mode="after")
    def _check_kind(self) -> StageSpec:
        if self.kind == "claude":
            if not self.prompt or not self.prompt.strip():
                raise ValueError(f"stage {self.name!r} (kind=claude) requires non-empty `prompt`")
            if not self.outputs:
                raise ValueError(
                    f"stage {self.name!r} (kind=claude) requires non-empty `outputs` list"
                )
            # claude stages MAY omit `validation` ("no contract" — see PRD R12).
            if self.check is not None:
                raise ValueError(
                    f"stage {self.name!r} (kind=claude) cannot have `check:` "
                    "(that field is poll-only)"
                )
        elif self.kind == "poll":
            if self.check is None or not self.check:
                raise ValueError(
                    f"stage {self.name!r} (kind=poll) requires `check:` block (PRD R12)"
                )
            if self.poll_interval is None:
                raise ValueError(
                    f"stage {self.name!r} (kind=poll) requires `poll_interval`"
                )
            if self.validation is not None:
                raise ValueError(
                    f"stage {self.name!r} (kind=poll) cannot have `validation:` "
                    "(use `check:` instead)"
                )
            if self.prompt is not None or self.outputs is not None:
                raise ValueError(
                    f"stage {self.name!r} (kind=poll) does not take `prompt`/`outputs` "
                    "(poll stages run no claude session)"
                )
        return self


# ----------------------------------------------------------------------------
# Parameter
# ----------------------------------------------------------------------------


_PARAM_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class ParameterSpec(BaseModel):
    """One entry under workflow `parameters:`. PRD §3 example uses {name, type, required, default, description}."""

    name: str
    type: Literal["string", "int", "float", "bool"] = "string"
    required: bool = True
    default: Any = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _PARAM_NAME_RE.match(v):
            raise ValueError(
                f"parameter name {v!r} must match [a-zA-Z_][a-zA-Z0-9_]*"
            )
        return v


# ----------------------------------------------------------------------------
# Workflow (top-level)
# ----------------------------------------------------------------------------


_WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class WorkflowSpec(BaseModel):
    """Top-level workflow YAML model. PRD §3."""

    name: str
    description: str | None = None
    parameters: list[ParameterSpec] = Field(default_factory=list)
    # NOTE: must NOT sit under a `.claude/` prefix — claude CLI treats any
    # path containing `.claude/` as sensitive and prompts for permission
    # even with `--dangerously-skip-permissions` set. Keep this under
    # `.workflow/` (or another neutral root).
    workspace: str = ".workflow/missions/{mission_id}"
    global_timeout: int = 604800  # 7 days, per PRD §3
    stages: list[StageSpec]
    final_outputs: list[str] = Field(default_factory=list)
    # Multi-agent v2: workflow-level fallback for `stage.agent`. Optional;
    # None means "each stage falls back to the user's default agent".
    default_agent: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _WORKFLOW_NAME_RE.match(v):
            raise ValueError(
                f"workflow name {v!r} must be lowercase + underscore (e.g. 'sample_experiment')"
            )
        return v

    @field_validator("global_timeout", mode="before")
    @classmethod
    def _coerce_duration(cls, v: Any) -> Any:
        return _parse_duration(v)

    @model_validator(mode="after")
    def _check_stages(self) -> WorkflowSpec:
        if not self.stages:
            raise ValueError("workflow must declare at least one stage")
        # Unique stage names
        names: list[str] = []
        for s in self.stages:
            if s.name in names:
                raise ValueError(f"duplicate stage name {s.name!r}")
            names.append(s.name)
        # depends_on linearity — every dep must reference a stage that already
        # appeared earlier in the array. Catches both cycles and forward refs.
        seen: set[str] = set()
        for s in self.stages:
            if s.depends_on:
                for dep in s.depends_on:
                    if dep == s.name:
                        raise ValueError(
                            f"stage {s.name!r} depends_on itself (cycle)"
                        )
                    if dep not in seen:
                        raise ValueError(
                            f"stage {s.name!r} depends_on {dep!r} which does not appear earlier "
                            "in the stages array (cycle or forward reference; MVP is linear)"
                        )
            seen.add(s.name)
        # Duplicate parameter names
        pnames: set[str] = set()
        for p in self.parameters:
            if p.name in pnames:
                raise ValueError(f"duplicate parameter name {p.name!r}")
            pnames.add(p.name)
        return self


# ----------------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------------


def load_workflow_spec(yaml_text: str) -> WorkflowSpec:
    """Parse + structurally validate a workflow YAML string.

    Returns a `WorkflowSpec`. Raises `WorkflowSchemaError` on any failure:
    YAML syntax, wrong top-level type, missing required fields, bad enum
    values, unknown primitives, cyclic depends_on, etc.

    The error message always includes a human-readable location
    (`stages[2].validation[0].primitives[1]: unknown primitive 'foo'`).
    """
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise WorkflowSchemaError(f"YAML parse error: {e}") from e

    if data is None:
        raise WorkflowSchemaError("workflow YAML is empty")
    if not isinstance(data, dict):
        raise WorkflowSchemaError(
            f"workflow YAML top-level must be a mapping, got {type(data).__name__}"
        )

    try:
        return WorkflowSpec.model_validate(data)
    except ValidationError as e:
        raise WorkflowSchemaError(_format_validation_error(e)) from e


def _format_validation_error(e: ValidationError) -> str:
    """Convert a pydantic ValidationError into a flat, human-readable message.

    Each error becomes a line `<loc>: <message>`, where <loc> is dotted
    (e.g. `stages[0].validation[0].primitives[1]`).
    """
    lines: list[str] = []
    for err in e.errors():
        loc = _format_loc(err.get("loc", ()))
        msg = err.get("msg", "validation error")
        lines.append(f"{loc}: {msg}" if loc else msg)
    return "workflow schema validation failed:\n  " + "\n  ".join(lines)


def _format_loc(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for piece in loc:
        if isinstance(piece, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{piece}]"
            else:
                parts.append(f"[{piece}]")
        else:
            parts.append(str(piece))
    return ".".join(parts)
