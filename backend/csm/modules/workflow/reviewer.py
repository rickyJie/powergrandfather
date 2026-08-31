"""Structural workflow review — R9-R12 hard rules (PRD §4.2 Pass 1).

This is the M8 counterpart of `csm.modules.automation.review.validator`:
deterministic, file-free, no-LLM verification that a parsed `WorkflowSpec`
conforms to the engine's runtime contract.

The schema layer (`csm.modules.workflow.schema`) already rejects many of
these violations at YAML parse time (e.g. unknown primitives, missing
poll-stage `check:` block). Reviewer is the redundant "compiled-rules
correctness" gate that runs on the *parsed* spec:

  - It catches violations a future code path that bypasses
    `load_workflow_spec` (e.g. constructing a spec via `model_construct`)
    might leak in.
  - It records a *structured report* on `WorkflowDefinition.review_status`
    + `review_report` so the API + UI can show per-rule pass/fail without
    re-parsing.
  - For poll stages whose `check:` block mixes load-binding and validation
    entries, R12 inspects the primitive vocabulary of each form so the
    runtime engine never sees an unknown check primitive.

This module **does not** implement the runtime primitives (T7) or render
placeholders (T8). It only verifies that the spec *looks* runnable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from csm.modules.workflow.schema import WorkflowSpec

# Allowed placeholder tokens inside an `outputs` path (R10).
#
# - `ws` / `mission_id` / `workflow_name` — workspace-level variables resolved
#   per Mission row at runtime.
# - `params.<param_name>` — must reference a `ParameterSpec` declared on the
#   workflow.
# - `stages.<stage_name>.outputs[<n>]` — must reference an earlier stage's
#   declared output index (validated against the parsed `stages` array).
#
# Anything else is a free-form expression / illegal token and fails R10.
_PLACEHOLDER_RE = re.compile(r"\{([^}]+)\}")
_LEGAL_TOKENS = {"ws", "mission_id", "workflow_name"}
_PARAM_RE = re.compile(r"^params\.([a-zA-Z_][a-zA-Z0-9_]*)$")
_STAGE_REF_RE = re.compile(r"^stages\.([a-z][a-z0-9_]*)\.outputs\[(\d+)\]$")

# Same six primitives as PRD §4.1 (kept duplicated here so the reviewer
# stays independent of schema-internal `_KNOWN_PRIMITIVES`).
_KNOWN_PRIMITIVES = {
    "file_exists",
    "min_chars",
    "min_size_bytes",
    "required_sections",
    "regex_match",
    "jsonschema",
    "contains_substring",
}


@dataclass
class RuleVerdict:
    rule_id: str         # "R9" / "R10" / ... / "R19"
    status: str          # "pass" / "warn" / "fail"
    reason: str = ""     # empty on pass; explanation on warn/fail

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "status": self.status, "reason": self.reason}


@dataclass
class ReviewResult:
    status: str                            # "passed" | "rejected"
    rules: list[RuleVerdict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "rules": [r.to_dict() for r in self.rules]}


def review_workflow(spec: WorkflowSpec) -> ReviewResult:
    """Run all structural rules against a parsed WorkflowSpec.

    Grade layers:
    - **P0 (fail)** rules block acceptance — R9-R13 and R18.
      Any `fail` verdict → top-level `status: rejected`.
    - **P1 (warn)** rules attach warnings without blocking — R14-R17, R19.
      A `warn` verdict is retained in the report (so the UI can
      display it) but does not by itself flip the top-level status.

    Each rule produces exactly one `RuleVerdict` (stable report shape).
    """
    rules = [
        _check_r9(spec),
        _check_r10(spec),
        _check_r11(spec),
        _check_r12(spec),
        _check_r13(spec),
        _check_r14(spec),
        _check_r15(spec),
        _check_r16(spec),
        _check_r17(spec),
        _check_r18(spec),
        _check_r19(spec),
    ]
    has_fail = any(r.status == "fail" for r in rules)
    status = "rejected" if has_fail else "passed"
    return ReviewResult(status=status, rules=rules)


# ----------------------------------------------------------------------------
# R9 — every (claude) stage has non-empty outputs
# ----------------------------------------------------------------------------


def _check_r9(spec: WorkflowSpec) -> RuleVerdict:
    """R9: every stage must declare a non-empty `outputs` list.

    PRD wording is "every stage". Poll stages produce no outputs by design
    (the schema rejects `outputs:` on a poll stage), so this rule is only
    meaningful for `kind: claude`. A claude stage with empty/None outputs
    has no observable contract — runtime would have nothing to validate.
    """
    fails: list[str] = []
    for s in spec.stages:
        if s.kind != "claude":
            continue
        if not s.outputs:
            fails.append(s.name)
    if fails:
        return RuleVerdict(
            "R9", "fail",
            f"stage(s) missing non-empty outputs: {', '.join(fails)}",
        )
    return RuleVerdict("R9", "pass")


# ----------------------------------------------------------------------------
# R10 — output paths use only legal placeholders, no absolute, no `..`
# ----------------------------------------------------------------------------


def _check_r10(spec: WorkflowSpec) -> RuleVerdict:
    """R10: outputs paths use only legal placeholders, are relative, no `..`."""
    param_names = {p.name for p in spec.parameters}
    fails: list[str] = []

    # Map of stage_name → number of outputs (for `stages.X.outputs[N]` index check).
    stage_output_counts: dict[str, int] = {}

    for s in spec.stages:
        outs = s.outputs or []
        # First, register this stage's output count so later stages can reference it.
        # We do this BEFORE checking the current stage's outputs because outputs
        # may not reference themselves anyway, and forward-references are caught
        # by the "stage name unknown / not yet declared" branch below.
        for path in outs:
            stripped = path.strip()
            # Absolute path → fail
            if stripped.startswith("/"):
                fails.append(f"{s.name}: absolute path {path!r}")
                continue
            # `..` segment → fail (path traversal escape)
            if any(seg == ".." for seg in stripped.split("/")):
                fails.append(f"{s.name}: '..' segment in {path!r}")
                continue
            # Placeholder vocabulary check
            for m in _PLACEHOLDER_RE.finditer(stripped):
                token = m.group(1).strip()
                if token in _LEGAL_TOKENS:
                    continue
                pm = _PARAM_RE.match(token)
                if pm:
                    if pm.group(1) not in param_names:
                        fails.append(
                            f"{s.name}: unknown parameter {{params.{pm.group(1)}}} in {path!r}"
                        )
                    continue
                sm = _STAGE_REF_RE.match(token)
                if sm:
                    ref_stage, ref_idx = sm.group(1), int(sm.group(2))
                    if ref_stage not in stage_output_counts:
                        fails.append(
                            f"{s.name}: forward / unknown stage reference "
                            f"{{stages.{ref_stage}.outputs[{ref_idx}]}} in {path!r}"
                        )
                    elif ref_idx >= stage_output_counts[ref_stage]:
                        fails.append(
                            f"{s.name}: out-of-range output index "
                            f"{{stages.{ref_stage}.outputs[{ref_idx}]}} in {path!r}"
                        )
                    continue
                fails.append(
                    f"{s.name}: illegal placeholder {{{token}}} in {path!r}"
                )
        stage_output_counts[s.name] = len(outs)

    if fails:
        return RuleVerdict("R10", "fail", "; ".join(fails))
    return RuleVerdict("R10", "pass")


# ----------------------------------------------------------------------------
# R11 — validation blocks only use the 6 primitives, no free-form text
# ----------------------------------------------------------------------------


def _check_r11(spec: WorkflowSpec) -> RuleVerdict:
    """R11: every `validation` block uses only PRD §4.1 primitives.

    Schema parsing rejects unknown primitive names via discriminated union,
    so this rule is reached only when a caller constructs the spec via
    `model_construct` (bypassing validators) or mutates a parsed spec.
    We still inspect each entry so the runtime can rely on the post-review
    invariant "all validation primitives are runnable".
    """
    fails: list[str] = []
    for s in spec.stages:
        if not s.validation:
            continue
        for vi, vb in enumerate(s.validation):
            prims = getattr(vb, "primitives", None) or []
            for pi, p in enumerate(prims):
                name = _primitive_name(p)
                if name is None:
                    fails.append(
                        f"{s.name}.validation[{vi}].primitives[{pi}]: "
                        f"not a primitive ({type(p).__name__}: {p!r:.60})"
                    )
                elif name not in _KNOWN_PRIMITIVES:
                    fails.append(
                        f"{s.name}.validation[{vi}].primitives[{pi}]: "
                        f"unknown primitive {name!r}"
                    )
    if fails:
        return RuleVerdict("R11", "fail", "; ".join(fails))
    return RuleVerdict("R11", "pass")


# ----------------------------------------------------------------------------
# R12 — poll stages require `check:` with only-primitive vocabulary
# ----------------------------------------------------------------------------


def _check_r12(spec: WorkflowSpec) -> RuleVerdict:
    """R12: poll stages MUST have non-empty `check:`; check primitives must be known.

    Claude stages MAY omit `validation` (means "no contract"), but if they
    declare one, every primitive there has already been checked by R11.
    """
    fails: list[str] = []
    for s in spec.stages:
        if s.kind != "poll":
            continue
        if not s.check:
            fails.append(f"{s.name}: poll stage missing non-empty `check:` block")
            continue
        for ci, cb in enumerate(s.check):
            prims = getattr(cb, "primitives", None)
            if prims is None:
                # Load-binding form — no primitives to check; legal per PRD.
                continue
            for pi, p in enumerate(prims):
                name = _primitive_name(p)
                if name is None:
                    fails.append(
                        f"{s.name}.check[{ci}].primitives[{pi}]: "
                        f"not a primitive ({type(p).__name__}: {p!r:.60})"
                    )
                elif name not in _KNOWN_PRIMITIVES:
                    fails.append(
                        f"{s.name}.check[{ci}].primitives[{pi}]: "
                        f"unknown primitive {name!r}"
                    )
    if fails:
        return RuleVerdict("R12", "fail", "; ".join(fails))
    return RuleVerdict("R12", "pass")


# ----------------------------------------------------------------------------
# R13 — section-scoped primitives require a markdown-header arg
# ----------------------------------------------------------------------------


def _check_r13(spec: WorkflowSpec) -> RuleVerdict:
    """R13: primitives that scope by `section:` require a markdown-header arg.

    `section_scope._parse_header_arg` matches `^(#+)\\s+(.+?)\\s*$`; a bare
    title without leading `#` will silently fail with `section not found`
    at runtime — an authoring footgun (P3 iter 1 real-run: authors wrote
    `section: "Expected wauc"` when they meant `section: "## Expected wauc"`).

    Applies to both `validation` blocks (claude stages) and `check` blocks
    (poll stages); the four primitives that accept `section:` are
    `min_chars`, `min_size_bytes`, `regex_match`, `contains_substring`.
    """
    fails: list[str] = []
    for s in spec.stages:
        blocks_by_source: list[tuple[str, list[Any]]] = [
            ("validation", list(s.validation or [])),
            ("check", list(s.check or [])),
        ]
        for source_name, blocks in blocks_by_source:
            for bi, block in enumerate(blocks):
                prims = getattr(block, "primitives", None) or []
                for pi, p in enumerate(prims):
                    section = getattr(p, "section", None)
                    if section is None:
                        continue
                    stripped = section.strip()
                    if not stripped.startswith("#"):
                        fails.append(
                            f"{s.name}.{source_name}[{bi}].primitives[{pi}]: "
                            f"section={section!r} must start with a markdown "
                            f"header marker (e.g. '## Result'); bare titles "
                            f"fail section_scope._parse_header_arg at runtime"
                        )
    if fails:
        return RuleVerdict("R13", "fail", "; ".join(fails))
    return RuleVerdict("R13", "pass")


# ----------------------------------------------------------------------------
# R14 — poll check must not forward-reference a later stage's outputs
# ----------------------------------------------------------------------------


# Matches `{stages.<name>.outputs[<index>]}` placeholder tokens.
_STAGE_OUTPUT_REF_RE = re.compile(r"\{stages\.([a-z][a-z0-9_]*)\.outputs\[(\d+)\]\}")


def _check_r14(spec: WorkflowSpec) -> RuleVerdict:
    """R14 (warn): poll `check` files must not reference a later stage's outputs.

    A poll stage that waits on a file the NEXT stage produces creates a
    deadlock — the next stage never runs because the poll never passes.
    Mission `da396925` wait_train (2026-07-05, commit 00707e4) was the
    root incident, but that specific bad path used an implicit
    subdirectory rather than a `{stages.X.outputs[N]}` placeholder, so
    the rule only catches the explicit-reference class of the bug.

    Downgraded to warn: implicit path references (like the real-world
    incident) can't be caught here without running the underlying skill,
    so the rule can only nudge — not gate.
    """
    warns: list[str] = []
    stage_index = {s.name: i for i, s in enumerate(spec.stages)}
    for i, s in enumerate(spec.stages):
        if s.kind != "poll":
            continue
        for ci, cb in enumerate(s.check or []):
            path_template = getattr(cb, "file", None) or ""
            for m in _STAGE_OUTPUT_REF_RE.finditer(path_template):
                referenced_stage = m.group(1)
                ref_idx = stage_index.get(referenced_stage)
                if ref_idx is not None and ref_idx > i:
                    warns.append(
                        f"{s.name}.check[{ci}].file references "
                        f"{{stages.{referenced_stage}.outputs[{m.group(2)}]}} "
                        f"which is declared by a LATER stage — poll would "
                        f"deadlock waiting for the next stage's artifact"
                    )
    if warns:
        return RuleVerdict("R14", "warn", "; ".join(warns))
    return RuleVerdict("R14", "pass")


# ----------------------------------------------------------------------------
# R15 — poll check with jsonschema is fragile; prefer file_exists
# ----------------------------------------------------------------------------


def _check_r15(spec: WorkflowSpec) -> RuleVerdict:
    """R15 (warn): poll `check` primitives with jsonschema often mis-match
    the real report shape.

    Mission `da396925` wait_eval (2026-07-05, commit e835955) had a
    jsonschema requiring `wauc` and `acc` at root, but the actual
    report shape was `{"config": {...}, "report": {S3_url: {...}}}`.
    The poll therefore never passed even after eval completed.

    Recommendation: start with `file_exists` only; add schema constraints
    after you've inspected a real production report.
    """
    warns: list[str] = []
    for s in spec.stages:
        if s.kind != "poll":
            continue
        for ci, cb in enumerate(s.check or []):
            prims = getattr(cb, "primitives", None) or []
            for pi, p in enumerate(prims):
                if _primitive_name(p) == "jsonschema":
                    warns.append(
                        f"{s.name}.check[{ci}].primitives[{pi}] uses "
                        f"jsonschema — verify the shape against a real "
                        f"report; consider file_exists-only until then"
                    )
    if warns:
        return RuleVerdict("R15", "warn", "; ".join(warns))
    return RuleVerdict("R15", "pass")


# ----------------------------------------------------------------------------
# R16 — required_sections entries with punctuation may miss real headers
# ----------------------------------------------------------------------------


# Punctuation likely to appear on a heading line as an author's parenthetical /
# separator, causing whole-line regex to reject genuine model output.
_UNSAFE_SECTION_CHARS = re.compile(r"[()\[\]{}|]")


def _check_r16(spec: WorkflowSpec) -> RuleVerdict:
    """R16 (warn): `required_sections` entries should not embed punctuation
    like `()`, `[]`, `{}`, `|` — real heading lines from claude will
    often lack these characters or include different ones.

    P3 iter 2 (2026-07-03, commit 64f6c82) was the incident: prompt
    allowed model to output `## Won't-try (do not repropose)` but
    `required_sections: ["Won't-try"]` demanded a bare heading.
    """
    warns: list[str] = []
    for s in spec.stages:
        for vi, vb in enumerate(s.validation or []):
            prims = getattr(vb, "primitives", None) or []
            for pi, p in enumerate(prims):
                if _primitive_name(p) != "required_sections":
                    continue
                sections = getattr(p, "sections", None) or []
                for si, sec in enumerate(sections):
                    if isinstance(sec, str) and _UNSAFE_SECTION_CHARS.search(sec):
                        warns.append(
                            f"{s.name}.validation[{vi}].primitives[{pi}]"
                            f".sections[{si}]={sec!r} contains punctuation "
                            f"that whole-line heading regex may reject — "
                            f"instruct prompt to keep headings bare and "
                            f"put qualifiers on the subtitle line"
                        )
    if warns:
        return RuleVerdict("R16", "warn", "; ".join(warns))
    return RuleVerdict("R16", "pass")


# ----------------------------------------------------------------------------
# R17 — regex_match with strict `[:\s]+` between name and value is fragile
# ----------------------------------------------------------------------------


# Substrings that indicate a strict delimiter between a metric name and its
# decimal value — the pattern will reject natural prose like `metric = 0.99`
# or markdown-table `| metric | 0.99 |`.
_STRICT_DELIM_MARKERS = (r"[:\s]+", r"[\s:]+", r":\s+", r"\s:\s")


def _check_r17(spec: WorkflowSpec) -> RuleVerdict:
    """R17 (warn): `regex_match` pattern with a `[:\\s]+` or `\\s+`
    immediately between a metric name and its value rejects natural
    model prose like `wauc = 0.99` or `wauc | 0.99` (markdown table).

    Mission `da396925` experiment_analysis (2026-07-05, commit 736035d)
    hit this — model wrote `wauc = 0.8854` but pattern demanded
    `wauc[:\\s]+0\\.`. Use `metric.*0\\.` instead; the surrounding
    `section:` scope keeps stray numbers from matching.
    """
    warns: list[str] = []
    for s in spec.stages:
        block_lists: list[tuple[str, list[Any]]] = [
            ("validation", list(s.validation or [])),
            ("check", list(s.check or [])),
        ]
        for source_name, blocks in block_lists:
            for bi, block in enumerate(blocks):
                prims = getattr(block, "primitives", None) or []
                for pi, p in enumerate(prims):
                    if _primitive_name(p) != "regex_match":
                        continue
                    pattern = getattr(p, "pattern", None)
                    if not isinstance(pattern, str):
                        continue
                    if any(m in pattern for m in _STRICT_DELIM_MARKERS):
                        warns.append(
                            f"{s.name}.{source_name}[{bi}].primitives[{pi}]"
                            f".pattern={pattern!r} enforces strict "
                            f"[:\\s]+ delimiter between metric and value — "
                            f"loosen to `<metric>.*0\\.` so `metric = 0.99` "
                            f"and markdown-table forms also match"
                        )
    if warns:
        return RuleVerdict("R17", "warn", "; ".join(warns))
    return RuleVerdict("R17", "pass")


# ----------------------------------------------------------------------------
# R18 — tmux naming must be enforced by jsonschema, not just prose in the prompt
# ----------------------------------------------------------------------------


def _prompt_mentions_tmux(prompt: str | None) -> bool:
    if not isinstance(prompt, str):
        return False
    return "tmux_session" in prompt or "tmux_socket" in prompt


def _jsonschema_enforces_csm_prefix(schema_obj: Any) -> bool:
    """Walk a jsonschema dict looking for tmux_session/tmux_socket properties
    whose `pattern` starts with `^csm-`. Returns True iff at least one such
    constraint exists anywhere in the schema tree.
    """
    if not isinstance(schema_obj, dict):
        return False
    props = schema_obj.get("properties")
    if isinstance(props, dict):
        for key in ("tmux_session", "tmux_socket"):
            entry = props.get(key)
            if isinstance(entry, dict):
                pattern = entry.get("pattern", "")
                if isinstance(pattern, str) and pattern.startswith("^csm-"):
                    return True
    # Recurse into nested subschemas
    for k, v in schema_obj.items():
        if k == "properties":
            continue
        if isinstance(v, dict) and _jsonschema_enforces_csm_prefix(v):
            return True
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and _jsonschema_enforces_csm_prefix(item):
                    return True
    return False


def _check_r18(spec: WorkflowSpec) -> RuleVerdict:
    """R18 (warn): if a claude stage's prompt mentions `tmux_session` or
    `tmux_socket`, at least one of its validation jsonschema blocks
    ideally enforces `pattern: '^csm-'` on those fields.

    P3 iter 3 (2026-07-03, commit 1be0cb9) was the incident: the prompt
    described the tmux naming loosely, model produced `sim` as
    tmux_session, and downstream guards that expected `^csm-` broke.

    Downgraded from fail → warn because not every tmux session referenced
    in a stage prompt is a CSM-owned session; e.g. `experiment_eval_dispatch`
    legitimately references an `eval-batch` tmux owned by an external
    orchestrator. The warn nudges authors to double-check without
    blocking valid workflows.
    """
    warns: list[str] = []
    for s in spec.stages:
        if s.kind != "claude":
            continue
        if not _prompt_mentions_tmux(s.prompt):
            continue
        enforced = False
        for vb in s.validation or []:
            for p in getattr(vb, "primitives", None) or []:
                if _primitive_name(p) != "jsonschema":
                    continue
                schema_obj = getattr(p, "json_schema", None)
                if _jsonschema_enforces_csm_prefix(schema_obj):
                    enforced = True
                    break
            if enforced:
                break
        if not enforced:
            warns.append(
                f"{s.name}: prompt mentions tmux_session/tmux_socket but "
                f"no validation jsonschema enforces `pattern: '^csm-'` "
                f"on those properties — if this is a CSM-owned tmux the "
                f"model may emit a short alias (P3 iter 3: model emitted "
                f"'sim' as tmux_session); if the referenced tmux is "
                f"external-owned (e.g. eval-batch), ignore this warning"
            )
    if warns:
        return RuleVerdict("R18", "warn", "; ".join(warns))
    return RuleVerdict("R18", "pass")


# ----------------------------------------------------------------------------
# R19 — poll_interval / timeout ratio should be plausible
# ----------------------------------------------------------------------------


# If max_attempts (= timeout / poll_interval) exceeds this bound, the poll
# will spend disproportionate time firing checks with no chance of catching
# an intermittent condition — usually a misconfigured pair.
_MAX_ATTEMPTS_SOFT_LIMIT = 200


def _check_r19(spec: WorkflowSpec) -> RuleVerdict:
    """R19 (warn): flag poll stages whose `timeout / poll_interval` exceeds
    ~200 attempts. Usually indicates an off-by-order-of-magnitude authoring
    error (e.g. poll_interval=30s + timeout=24h → 2880 attempts).
    """
    warns: list[str] = []
    for s in spec.stages:
        if s.kind != "poll":
            continue
        if not s.poll_interval or not s.timeout:
            continue
        attempts = s.timeout // s.poll_interval
        if attempts > _MAX_ATTEMPTS_SOFT_LIMIT:
            warns.append(
                f"{s.name}: timeout ({s.timeout}s) / poll_interval "
                f"({s.poll_interval}s) = {attempts} attempts, exceeds "
                f"soft limit {_MAX_ATTEMPTS_SOFT_LIMIT} — likely a "
                f"misconfigured pair; consider longer poll_interval"
            )
    if warns:
        return RuleVerdict("R19", "warn", "; ".join(warns))
    return RuleVerdict("R19", "pass")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _primitive_name(p: Any) -> str | None:
    """Return the discriminator value if `p` looks like a PrimitiveCheck submodel.

    A string / dict / int that slipped past schema (via `model_construct`) is
    treated as "not a primitive" → R11/R12 fail. We deliberately avoid
    `isinstance` against the union to keep reviewer decoupled from schema's
    private type alias.
    """
    name = getattr(p, "primitive", None)
    if isinstance(name, str):
        return name
    return None
