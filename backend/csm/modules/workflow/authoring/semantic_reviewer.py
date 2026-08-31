"""Pass-2 semantic reviewer for generated workflow YAMLs.

R9-R19 (see `csm.modules.workflow.reviewer`) verify STRUCTURAL correctness:
placeholder legality, poll wiring, section markers, primitive names. Those
pass cleanly on many YAMLs that still need multiple manual iterations
because the SEMANTIC quality (stage decomposition sensible? outputs named
well? prompt complete? primitive matching data shape? edge-case branches
baked in?) is invisible to a rule-based reviewer.

This module adds a lightweight LLM-based pass that runs AFTER R9-R19 and
emits 5 bounded verdicts:

  - stage_decomposition   — does the stage split match the requirement?
  - output_naming         — do outputs paths convey their content?
  - prompt_completeness   — do stage prompts spell out enough for claude?
  - primitive_choice      — do validation primitives match the data shape?
  - branch_coverage       — do clarifications' edge branches show up in YAML?

Each verdict is `pass | warn | fail` with a one-sentence explanation.

Cost: one extra `claude -p` per generation (~30-60s). Runs even when R9-R19
verdict is "rejected" — semantic problems are worth surfacing regardless
so the user can fix multiple layers in a single edit round.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The 5 semantic verdict categories the pass-2 reviewer must return.
# Order matters — used as the canonical display order in the UI.
SEMANTIC_CATEGORIES = (
    "stage_decomposition",
    "output_naming",
    "prompt_completeness",
    "primitive_choice",
    "branch_coverage",
)

# Timeout — same shape as clarify (~30-60s). Bumped a bit for longer YAMLs.
_SEMANTIC_TIMEOUT_SEC = 120

# Extract the JSON block from stdout, same convention as clarify.
_VERDICT_BLOCK_RE = re.compile(
    r"<semantic_review>\s*(.+?)\s*</semantic_review>", re.DOTALL
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


SEMANTIC_SYSTEM_APPEND = (
    "You are a bounded semantic reviewer for a CSM workflow YAML. Given "
    "the original requirement, the user's clarification answers, and the "
    "generated YAML, emit ONE <semantic_review>...</semantic_review> block "
    "containing a single JSON object with exactly 5 verdicts. Be blunt — "
    "your value is calling out mismatches the structural reviewer can't see."
)


@dataclass
class SemanticVerdict:
    """One category's verdict."""
    category: str        # one of SEMANTIC_CATEGORIES
    status: str          # "pass" | "warn" | "fail"
    reason: str          # ≤ 200 chars

    def to_dict(self) -> dict[str, str]:
        return {"category": self.category, "status": self.status, "reason": self.reason}


@dataclass
class SemanticReviewResult:
    """Full pass-2 output."""
    verdicts: list[SemanticVerdict] = field(default_factory=list)
    error: str | None = None    # populated on failure (timeout, parse fail, ...)
    duration_sec: float = 0.0
    stdout_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdicts": [v.to_dict() for v in self.verdicts],
            "error": self.error,
            "duration_sec": round(self.duration_sec, 2),
        }


def build_semantic_review_prompt(
    *,
    requirement: str,
    clarifications: list[dict] | None,
    yaml_text: str,
) -> str:
    """Prompt for pass-2 semantic review.

    Kept short — the reviewer is a bounded critic, not a re-implementer.
    Gives the agent all three inputs (requirement / clarifications / YAML)
    and asks for exactly 5 category verdicts.
    """
    clar_lines: list[str] = []
    if clarifications:
        clar_lines.append("### The user's clarification answers")
        for i, c in enumerate(clarifications, 1):
            q = str(c.get("question", "")).strip()
            a = str(c.get("answer_label", "")).strip()
            free = str(c.get("free_text") or "").strip()
            clar_lines.append(f"{i}. Q: {q}")
            clar_lines.append(f"   A: {a}")
            if free:
                clar_lines.append(f"   Also: {free}")
        clar_lines.append("")
    else:
        clar_lines.append(
            "### The user's clarification answers\n\n"
            "(None — they used one-shot generation, so there is no extra "
            "boundary information.)\n"
        )
    clarifications_block = "\n".join(clar_lines)

    return f"""# Task: Pass-2 semantic review of a CSM workflow YAML

The R9-R19 structural review only checks syntax — placeholders are legal,
primitives exist, sections carry `##`. Your job is the **semantic** layer:
find the gaps where requirement, clarifications and YAML fail to line up.

## The user's original requirement

{requirement}

{clarifications_block}
## The generated YAML

<yaml>
{yaml_text}
</yaml>

## What to produce

Emit **one** `<semantic_review>...</semantic_review>` block containing **a
single JSON object** with a verdict for **exactly these 5 categories** (any
order, but all of them):

- **stage_decomposition** — do the stages map onto the verbs and steps in the
  requirement? Too coarse (one stage doing too much) and too fine (one step
  split across three claude sessions) are both problems.
- **output_naming** — can you tell what a file is for from its output path?
  Storing JSON in `{{ws}}/02-review/actions.md` is a name/content mismatch;
  `{{ws}}/02-review/reviews.md` + `{{ws}}/02-review/action_plan.json` is good.
- **prompt_completeness** — does each stage's prompt state the artefact path
  AND the content contract (H2 headings / JSON shape / what to write in the
  empty state)? A vague prompt lets claude improvise, and downstream
  validation fails.
- **primitive_choice** — does each stage's validation primitive match the
  shape of the data? `min_chars` on JSON is wrong (use jsonschema); a
  `min_chars: 200` on something that may be an empty-state placeholder is a trap.
- **branch_coverage** — are the boundaries agreed in the clarifications (empty
  state, all-DEFER, dry_run branches) actually reflected in the corresponding
  stage prompt or validation? Missing means the user said it explicitly and
  the agent dropped it — a high-priority warn or fail.

## JSON shape

```json
{{
  "verdicts": [
    {{
      "category": "stage_decomposition",
      "status": "pass" | "warn" | "fail",
      "reason": "one sentence, specific to a stage or field (<= 30 words)"
    }},
    ... the other 4
  ]
}}
```

## Constraints

- `status`: `pass` only when there is **nothing** left to improve. If you can
  think of a change, it is at least a `warn`.
- `reason`: must be **specific**, naming a stage or a field. No "looks good",
  no "seems fine overall".
  A good reason reads: "`review_each`'s prompt doesn't say whether
  action_plan.json holds [] or null in the empty state, so the downstream
  apply_changes may read None and crash."
- **Do not** fix the YAML — you are a reviewer, not an implementer.
- **Do not** report structural findings; R9-R19 already covered those.
- Mark anything you're unsure about `warn`. Don't pass everything to look tidy.

Go."""


def _extract_semantic_json(stdout: str) -> dict[str, Any] | None:
    """Pull the JSON payload out of a <semantic_review>...</semantic_review> block."""
    m = _VERDICT_BLOCK_RE.search(stdout)
    if not m:
        return None
    body = m.group(1).strip()
    fence = _JSON_FENCE_RE.search(body)
    if fence:
        body = fence.group(1).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalize_verdicts(payload: dict[str, Any]) -> list[SemanticVerdict]:
    """Normalize the parsed JSON into 5 canonical verdicts.

    Missing categories default to a `warn` verdict with reason "agent
    omitted this category" — surfaces the omission to the UI instead of
    silently hiding it.
    """
    raw = payload.get("verdicts") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    seen: dict[str, SemanticVerdict] = {}
    for v in raw:
        if not isinstance(v, dict):
            continue
        cat = str(v.get("category") or "").strip()
        if cat not in SEMANTIC_CATEGORIES:
            continue
        status = str(v.get("status") or "").strip().lower()
        if status not in ("pass", "warn", "fail"):
            status = "warn"
        reason = str(v.get("reason") or "").strip()
        # Cap reason so it fits in a UI card and doesn't blow up the JSON column.
        if len(reason) > 400:
            reason = reason[:397] + "..."
        seen[cat] = SemanticVerdict(category=cat, status=status, reason=reason)
    # Fill in missing categories as warn.
    return [
        seen.get(c) or SemanticVerdict(
            category=c, status="warn",
            reason="agent omitted this category — treat as needs-attention",
        )
        for c in SEMANTIC_CATEGORIES
    ]


async def semantic_review_workflow(
    *,
    requirement: str,
    clarifications: list[dict] | None,
    yaml_text: str,
    cwd: Path,
    timeout_sec: int = _SEMANTIC_TIMEOUT_SEC,
) -> SemanticReviewResult:
    """Spawn claude for a pass-2 semantic review.

    `cwd` should be the target repo (same as generate) so the reviewer can
    glance at project context if needed. Never writes files — read-only
    critic. Falls back to `error` + empty verdicts on any subprocess or
    parse failure; the caller decides whether to skip or surface.
    """
    t0 = time.monotonic()

    if not yaml_text.strip():
        return SemanticReviewResult(
            verdicts=[], error="yaml_text is empty",
            duration_sec=0.0, stdout_tail="",
        )

    prompt = build_semantic_review_prompt(
        requirement=requirement,
        clarifications=clarifications,
        yaml_text=yaml_text,
    )

    argv = [
        "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--append-system-prompt", SEMANTIC_SYSTEM_APPEND,
        "--output-format", "text",
    ]
    log.info("workflow-semantic-review: spawning claude in %s (yaml=%d bytes)",
             cwd, len(yaml_text))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as e:
        return SemanticReviewResult(
            verdicts=[], error=f"cannot spawn claude for semantic review: {e}",
            duration_sec=time.monotonic() - t0, stdout_tail="",
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec,
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return SemanticReviewResult(
            verdicts=[],
            error=f"semantic review timed out after {timeout_sec}s",
            duration_sec=time.monotonic() - t0, stdout_tail="",
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    stdout_tail = stdout[-2048:] if stdout else ""

    if proc.returncode != 0:
        log.warning(
            "workflow-semantic-review: claude exit=%d after %.1fs; stderr tail=%r",
            proc.returncode, time.monotonic() - t0, stderr[-256:],
        )
        return SemanticReviewResult(
            verdicts=[],
            error=f"semantic review claude exit code {proc.returncode}",
            duration_sec=time.monotonic() - t0, stdout_tail=stdout_tail,
        )

    payload = _extract_semantic_json(stdout)
    if payload is None:
        log.warning(
            "workflow-semantic-review: no <semantic_review> JSON block found (%.1fs)",
            time.monotonic() - t0,
        )
        return SemanticReviewResult(
            verdicts=[],
            error="no <semantic_review> JSON block in stdout",
            duration_sec=time.monotonic() - t0, stdout_tail=stdout_tail,
        )

    verdicts = _normalize_verdicts(payload)
    log.info(
        "workflow-semantic-review: OK — %d verdict(s), pass=%d warn=%d fail=%d, %.1fs",
        len(verdicts),
        sum(1 for v in verdicts if v.status == "pass"),
        sum(1 for v in verdicts if v.status == "warn"),
        sum(1 for v in verdicts if v.status == "fail"),
        time.monotonic() - t0,
    )
    return SemanticReviewResult(
        verdicts=verdicts, error=None,
        duration_sec=time.monotonic() - t0, stdout_tail=stdout_tail,
    )
