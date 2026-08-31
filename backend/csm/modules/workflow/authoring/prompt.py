"""Server-side prompts for the two-round workflow authoring flow.

Round 1 (clarify): agent skims the repo + requirement, then emits a
short JSON block of boundary-condition questions the user should answer
before we commit to a YAML shape (empty inputs, dry-run, all-defer,
threshold values, etc.). Kept small — hard-capped at 5 questions.

Round 2 (generate): the original one-shot YAML writer, now optionally
receiving the user's answers to round-1 questions so it can bake the
right conditional / skip / threshold decisions into the YAML.

Kept as plain constants so both prompts are grep-friendly.
"""
from __future__ import annotations

from pathlib import Path

# Repo root of PowerGrandFather itself — derived from this file's location
# (`.../backend/csm/modules/workflow/authoring/prompt.py`) so the constants
# below stay correct regardless of where a checkout is placed.
_REPO_ROOT = Path(__file__).resolve().parents[5]

# Absolute path where every generated YAML MUST land — user's checkout
# of PowerGrandFather. Resolved at import time.
TASKS_DIR = str(_REPO_ROOT / "tasks")

# Authoring guide — the canonical source of truth for how to write a
# workflow YAML. Bundled with the repo; no remote fetch.
GUIDE_LOCAL_PATH = str(_REPO_ROOT / "docs" / "workflow_authoring_guide.md")

# HOT guide — a distilled ~10-15KB subset (essence of §0/§3/§4/§6/§7 + a real
# flagship example) that gets spliced into every generate/edit prompt so
# critical rules stay in front of the model instead of being buried in
# the 40KB full guide. Agent is told to Read the full guide only when hot
# doesn't answer.
GUIDE_HOT_LOCAL_PATH = str(_REPO_ROOT / "docs" / "workflow_authoring_guide_hot.md")


# =====================================================================
# Round 1 — Clarify
# =====================================================================

CLARIFY_SYSTEM_APPEND = (
    "You analyze a workflow requirement + target repo, then emit boundary "
    "clarification questions the user should answer before YAML generation. "
    "Skim the repo VERY briefly (max 3 tool calls: ls / git log / one file). "
    "Output ONE <clarifications>...</clarifications> block containing a "
    "single JSON object. Max 5 questions. If no meaningful boundary needs "
    "clarification, output an empty questions array. Be terse."
)


def build_clarify_prompt(*, requirement: str, workflow_name: str | None) -> str:
    """Prompt for round 1: probe the repo and propose boundary questions.

    Deliberately keeps the guide out — clarify doesn't need the full
    authoring manual, only enough repo context to write good questions.
    """
    name_hint = (
        f"The user suggests workflow_name = `{workflow_name}`."
        if workflow_name
        else "The user did not name it; infer a placeholder name from the "
             "requirement (the generate step decides the final one)."
    )
    return f"""# Task: propose boundary-clarification questions for a CSM workflow

You are CSM's clarify agent. Your cwd is **the repo the user wants to automate**.

## The user's one-sentence requirement

{requirement}

## workflow_name hint

{name_hint}

## What to do

Spend at most **3 tool calls** (say `ls`, `git log --oneline -5`, the first 20
lines of the README) working out what this repo does. Then, against the
requirement above, identify **edge cases / empty states / threshold choices** —
the "what if this isn't true in practice" questions.

Prioritise these kinds of question (1-2 of the most relevant per kind; don't
be greedy):

1. **Empty-data branch** — when an upstream stage returns an empty array or an
   empty file, should the downstream stage skip? emit an empty report anyway?
   fail?
2. **All-default / all-DEFER branch** — if the review turns up nothing to
   apply, should the remaining stages be skipped?
3. **Threshold choice** — do you want a `min_chars` / `minItems` floor in
   validation, and what floor is sensible? (Offer the agent's suggested value
   for the user to review.)
4. **Failure recovery** — when an external dependency fails (network, object
   store, git push): retry? fail? degrade to dry-run?
5. **dry-run semantics** — if the user mentioned a dry_run param, what should
   apply / push / notify each do when dry_run=true?

## F0 hard constraint: a workflow describes what/how, **never** when/how-often

A workflow is **the definition of a single run**. When it runs, how many times,
how often — that's the **Schedule layer** (cron / manual launch) and has
nothing to do with the YAML.

- Ignore every time or frequency trigger in the requirement — "daily",
  "weekly", "Sundays at 22:00", "hourly", "every N runs". **Do not** raise
  clarification questions about them ("which day of the week should it run?"
  is a **violation**), and don't mention trigger frequency in `stage_preview`.
- Data-range params (like `days_back` = take the last N days on each run) ARE
  fair game, but only as **parameters** — never tied to trigger frequency.

Before asking anything, ask yourself: "does this let the user decide **what one
run does** (ask it), or **how often it runs** (don't)?"

## **Hard constraint** on threshold questions (learned the hard way)

If you're about to ask about a `min_chars` / `minItems` threshold, you **must**
first consider whether that artefact gets written as a short placeholder in the
**empty-state branch** (something like "0 items this week"). The rule:

- **The stage has an empty-state branch that writes placeholder text** → the
  recommended default for `min_chars` **must be 0** (or skip min_chars entirely
  and rely on `required_sections` / `file_exists`).
  Real incident: a workflow wrote a 17-character "no new items this week"
  placeholder in the empty state while `min_chars=20` — because the clarify
  agent never considered that its own placeholder would be shorter than the
  threshold — and the mission failed against its own gate.
- **The stage has no empty-state branch / only produces output on non-empty
  data** → only then ask what floor they want (200 / 500 are reasonable),
  where the threshold genuinely guards against a half-hearted agent.

When you do ask about `min_chars`, say so **explicitly** in the question text:
"will you write a placeholder when upstream is empty, and roughly how long is
it?" — let the user derive a sane floor. Or just recommend 0 and explain in the
question that empty-state placeholders don't survive a hard min_chars gate.

## Output requirements (**strict**)

Your output **must** contain **one** `<clarifications>...</clarifications>` XML
block holding **a single JSON object**. Any other reasoning text is fine, but
that block appears exactly once.

JSON shape:

```json
{{
  "stage_preview": "one line on how you read this workflow (e.g. 'five stages: fetch → review → apply → push → notify')",
  "stages": [
    {{"name": "fetch_feedback", "kind": "claude", "purpose": "pull the last N days from the inbox, emit JSON"}},
    {{"name": "review_each",    "kind": "claude", "purpose": "review each item, emit reviews.md + action_plan.json"}},
    {{"name": "apply_changes",  "kind": "claude", "purpose": "apply action_plan to the code, emit patch + summary"}},
    {{"name": "commit_push",    "kind": "claude", "purpose": "commit + push (commit only under dry_run)"}},
    {{"name": "notify",         "kind": "claude", "purpose": "send the notification"}}
  ],
  "questions": [
    {{
      "id": "empty_fetch",
      "text": "If the fetch stage comes back empty, what should happen?",
      "options": [
        {{"value": "skip_notify", "label": "Skip to notify and send '0 items this week'", "recommended": true}},
        {{"value": "silent_success", "label": "Mark the whole mission successful and exit quietly"}},
        {{"value": "fail_alert", "label": "Fail and alert me to look at it"}}
      ]
    }},
    {{
      "id": "reviews_min_chars",
      "text": "What min_chars threshold should reviews.md have?",
      "options": [
        {{"value": "0", "label": "No floor (an empty review is allowed)", "recommended": true}},
        {{"value": "200", "label": "200 characters (force some content)"}},
        {{"value": "500", "label": "500 characters (force detail)"}}
      ]
    }}
  ]
}}
```

### The stages array is **required** — always present, never empty

`stages` is the **skeleton** of how you plan to decompose this workflow. The
frontend renders it as a preview card so the user can review the breakdown
before answering the boundary questions — if you got it wrong, they can
rename / delete / add / reorder in the UI before generation. So:

- every stage carries all three of `{{"name", "kind", "purpose"}}`
- `name`: snake_case, matching `^[a-z][a-z0-9_]*$`
- `kind`: only `"claude"` or `"poll"` (same as the workflow schema)
- `purpose`: **one short line**, ≤ 15 words, saying what the stage does and
  what it produces
- count: **3-8**. Fewer is meaningless; more is certainly over-decomposed
- **F0 reminder**: don't think about "how often it runs" while decomposing —
  that's the schedule layer's business

Constraints:
- At most **5** questions; err on the side of fewer.
- Every question has at least 2 options, exactly one of which **must** carry
  `recommended: true` — that is the target of the user's "accept defaults"
  button.
- `id` is snake_case, short, and reflects the intent.
- `text` is phrased as a question.
- `label` is ≤ 12 words and specific enough to need no follow-up.
- If you judge that this workflow has **no boundary worth clarifying** (a
  linear happy path like "cat a log file"), return `"questions": []` — but
  **still fill in the stages array**. The frontend then skips straight to
  letting the user review the skeleton before generating.

## Hard constraints

- **Do not** write a YAML file in this step. You only emit the question list.
- **Do not** explore the repo at length. 3 tool calls is the ceiling.
- **Do not** ask things you should decide yourself, like "how many stages do
  you want" — ask only about boundaries you cannot settle without the user.

Go."""


# =====================================================================
# Round 2 — Generate (existing flow, now with optional clarifications)
# =====================================================================

SYSTEM_APPEND = (
    "You are a CSM workflow YAML generator invoked non-interactively via "
    "`claude -p`. Your ONLY deliverable is a workflow YAML file written to "
    f"{TASKS_DIR}/<name>.workflow.yaml. Do not ask questions — infer from "
    "the cwd repo context and the user's clarifications block (if any). "
    "Be terse in your reasoning trace. Your final line MUST be exactly: "
    f"wrote {TASKS_DIR}/<name>.workflow.yaml"
)


def _render_clarifications(
    clarifications: list[dict] | None,
) -> str:
    """Render the Q&A block for the generate prompt.

    `clarifications` is a list of dicts, each shaped:
      {"question": str, "answer_label": str, "free_text": str | None}

    Returns a string that starts with a section header or empty string
    when nothing to show.
    """
    if not clarifications:
        return ""
    lines = [
        "## The user's clarification answers",
        "",
        "Before generating the YAML, the clarify agent asked the user the "
        "following boundary questions. Their answers:",
        "",
    ]
    for i, c in enumerate(clarifications, 1):
        q = c.get("question", "").strip()
        a = c.get("answer_label", "").strip()
        free = (c.get("free_text") or "").strip()
        lines.append(f"{i}. **Q**: {q}")
        lines.append(f"   **A**: {a}")
        if free:
            lines.append(f"   **Also**: {free}")
        lines.append("")
    lines.extend([
        "**Make sure** these decisions land in the YAML — for example:",
        "- add a conditional skip / empty-state branch to the relevant stage",
        "- set validation thresholds to the values the user chose",
        "- implement dry_run and failure-recovery behaviour as answered",
        "",
        "And add a `# clarifications:` comment block at the **top** of the "
        "YAML, copying the Q&A above verbatim, so a later reader can trace "
        "which boundary decisions were made and why:",
        "",
        "    # clarifications:",
        "    #   1. Q: If the fetch stage comes back empty …",
        "    #      A: Skip to notify and send '0 items this week'",
        "    #   2. ...",
        "",
    ])
    return "\n".join(lines)


def _render_confirmed_stages(stages: list[dict] | None) -> str:
    """Render the user-confirmed skeleton block.

    `stages` is a list of dicts like `{"name", "kind", "purpose"}` — the
    stages the clarify agent proposed, possibly edited by the user before
    generation. When present, the generate agent MUST use exactly these
    stage names in the emitted YAML and cannot invent new ones — the
    skeleton is locked at this point.
    """
    if not stages:
        return ""
    lines = [
        "## Stage breakdown locked by the user (**do not change it**)",
        "",
        "The user reviewed — and possibly edited — the skeleton the clarify "
        "agent proposed. You must use the stage array below **exactly**: no "
        "renaming, no reordering, no changing kinds. Your job is to fill in "
        "each stage's prompt / outputs / validation.",
        "",
    ]
    for i, s in enumerate(stages, 1):
        name = str(s.get("name", "")).strip()
        kind = str(s.get("kind", "")).strip()
        purpose = str(s.get("purpose", "")).strip()
        lines.append(f"{i}. `{name}` (kind: `{kind}`) — {purpose}")
    lines.extend([
        "",
        "**If you violate this** — renaming, adding, removing or reordering — "
        "the server rejects the upload.",
        "If the skeleton genuinely cannot satisfy the requirement (a necessary "
        "stage is missing), you may either:",
        "- fold that action into the prompt of the closest existing stage; or",
        "- emit a `<skeleton_disagreement>...</skeleton_disagreement>` block at "
        "  the end explaining why — but **still write the YAML using only the "
        "  given stage array**, so the user can see the disagreement and decide "
        "  whether to add a stage and re-run.",
        "",
    ])
    return "\n".join(lines)


def build_generate_prompt(
    *,
    requirement: str,
    workflow_name: str | None,
    guide_body: str,
    clarifications: list[dict] | None = None,
    confirmed_stages: list[dict] | None = None,
) -> str:
    """Round-2 prompt. If clarifications is set, splice the Q&A block in.
    If confirmed_stages is set, lock the stage skeleton (agent may not
    invent new stages) — this is the "user pre-approved the decomposition"
    contract from the clarify screen's stage preview card.
    """
    name_hint = (
        f"The user suggested workflow_name `{workflow_name}` — use it unless "
        f"it is invalid (does not match `^[a-z][a-z0-9_]*$`)."
        if workflow_name
        else "Pick a workflow_name yourself that reflects the intent; it must "
             "match `^[a-z][a-z0-9_]*$`."
    )
    clarifications_block = _render_clarifications(clarifications)
    confirmed_stages_block = _render_confirmed_stages(confirmed_stages)
    return f"""# Task: write a CSM workflow YAML for the repo at the current cwd

You are a backend agent, invoked by CSM for a one-shot workflow YAML
generation. Your cwd is **the repo the user wants to automate** — not the CSM
repo.

## The user's requirement (one sentence)

{requirement}

## workflow_name constraint

{name_hint}

{clarifications_block}
{confirmed_stages_block}
## Where the output goes (**mandatory**)

The write path must be:

    {TASKS_DIR}/<workflow_name>.workflow.yaml

You may **not** write into the current cwd's tasks directory — CSM reads YAML
only from the absolute path above.

## The guide (HOT edition — condensed to the critical rules plus one real example)

Below is the **condensed** CSM Workflow Authoring Guide (§0, §3, §4 template
skeletons, §6 F1-F9, §7 R9-R19 and Appendix A's real example). This is the core
you need **on every generation**.

If you need **the full methodology (§1)**, **the complete primitive table
(§5)** or **Appendix B/C**, `Read` the full guide at `{GUIDE_LOCAL_PATH}`.

<guide_hot>
{guide_body}
</guide_hot>

## Your procedure (do every step)

1. Run `ls`, `git log --oneline -5`, read the README/Makefile — work out what
   the repo at the current cwd does.
2. If there's a "stage breakdown locked by the user" block above, use that
   skeleton verbatim; otherwise decompose into stages per §1.
3. Pick T1 / T2 / T3 from the §4 template skeletons; fill in parameters and
   each stage's outputs.
4. Fill in the fields, staying clear of all nine §6 traps (F1-F9).
5. Use the Write tool to write the file **directly** to the mandatory path
   `{TASKS_DIR}/<workflow_name>.workflow.yaml`.
6. Self-check against §7 R9-R19 one rule at a time; on any finding, go back to
   step 5 and fix it, until the whole self-check passes.
7. Your **final line** must be exactly this — the backend greps for it to
   confirm the path:

       wrote {TASKS_DIR}/<workflow_name>.workflow.yaml

## Hard constraints, restated

- **F0: a workflow says what/how, never when/how-often.** Ignore every time or
  frequency trigger in the requirement — "daily", "weekly", "Sundays at
  22:00", "hourly", "every N runs". The YAML must **not** contain schedule-layer
  fields: `cron`, `schedule`, `interval` (except `poll_interval`, which is a
  poll's internal cadence, not a trigger frequency), `next_run_at`. Data-range
  params such as `days_back` are fine, but stay independent of trigger frequency.
- Use only the placeholders listed in guide §3; escape any other `{{...}}`
- A section arg must carry the `## ` prefix
- `regex_match` uses loose `.*` matching — never `[:\\s]+`
- A kind=claude stage must have non-empty outputs; a kind=poll stage must have none
- A poll check may only poll markers the underlying task **actually produces** —
  never a later stage's outputs

Go.
"""


# Backwards-compat alias — some tests/scripts may still call build_prompt.
def build_prompt(*, requirement: str, workflow_name: str | None, guide_body: str) -> str:
    """Deprecated: use build_generate_prompt directly."""
    return build_generate_prompt(
        requirement=requirement,
        workflow_name=workflow_name,
        guide_body=guide_body,
        clarifications=None,
    )


# =====================================================================
# Debug session prompt — seeds an INTERACTIVE claude session in the
# tasks/ dir so the user can iterate on a workflow YAML in real time
# (multi-turn, unlike edit-with-agent's one-shot).
# =====================================================================


def build_debug_session_prompt(*, workflow_name: str) -> str:
    """Initial-turn prompt for the interactive debug session.

    Kept intentionally short — the guide + YAML both live on disk, so
    the agent Reads them lazily instead of shipping them through the
    PTY. This keeps first-turn latency low.
    """
    yaml_path = f"{TASKS_DIR}/{workflow_name}.workflow.yaml"
    return f"""You are debugging a CSM workflow YAML interactively with the user.
This is a multi-turn session — they will keep proposing changes; you edit the
YAML as asked and explain what you did.

**Workflow name**: `{workflow_name}` (**the top-level name cannot change** — if
they want a rename, have them create a new workflow)
**YAML file path**: `{yaml_path}`
**Full authoring guide**: `{GUIDE_LOCAL_PATH}`

## First turn (**do this immediately, don't wait to be asked**)

1. Read the YAML file to see where things stand.
2. In 3-5 lines, tell the user the structure you see — how many stages, roughly
   what each does, which params exist.
3. Ask what they want to change.

## Every turn after that

When the user says what to change:
1. Work out which part it touches — stage name, field, threshold, prompt text,
   validation…
2. Read the relevant guide section if you need it (§1 decomposition, §4
   templates, §6 F1-F9 traps, §7 R9-R19 self-check).
3. Edit or Write the YAML file **at the same path** — never a new file.
4. Explain the change in one line, then wait for their next message.

## Hard constraints

- **The workflow's top-level name field cannot change.**
- Use only the placeholders listed in guide §3; escape any other `{{...}}`.
- A section arg must carry the `## ` prefix.
- A kind=claude stage must have non-empty outputs; a kind=poll stage must have none.
- If a requested change would break a cross-stage reference — deleting stage A
  while stage B still references `stages.A.outputs[0]` — say so and propose a
  fix (adjust B, or keep A).
- When the user says "stop" or "OK", stop and wait. Don't keep editing on your own.

Go — Read the YAML first, then give them the summary.
"""


# =====================================================================
# Round-3-style — Edit-with-agent (natural-language iterations on an
# existing YAML). Different flow from clarify+generate: the workflow
# already exists on disk + in DB, the user just wants to change parts
# of it via a sentence-level feedback ("make min_chars 0", "insert a
# dry-run branch before commit_push").
# =====================================================================

EDIT_SYSTEM_APPEND = (
    "You are editing an EXISTING CSM workflow YAML at "
    f"{TASKS_DIR}/<name>.workflow.yaml. Read the current version, apply "
    "the user's feedback, write the new version back to the SAME path. "
    "Preserve the workflow name — do not rename. Preserve unrelated "
    "stages / parameters — the user only asked about specific parts. "
    "Your final line MUST be exactly: wrote "
    f"{TASKS_DIR}/<name>.workflow.yaml"
)


def _render_last_failure(last_failure: dict | None) -> str:
    """Render the "last mission failure" signal block.

    `last_failure` is a dict `{failed_stage, failure_reason, stdout_tail,
    triggered_rule}` populated by the caller from Mission ORM row + review
    report. Empty return when no recent failure — most edits happen when
    the workflow still hasn't been Launched or the last run succeeded.
    """
    if not last_failure:
        return ""
    stage = str(last_failure.get("failed_stage") or "").strip()
    reason = str(last_failure.get("failure_reason") or "").strip()
    triggered = str(last_failure.get("triggered_rule") or "").strip()
    tail = str(last_failure.get("stdout_tail") or "").strip()
    lines = [
        "## Signal from the last failed run (attached automatically, for reference)",
        "",
        "This workflow's most recent mission **failed**. Below is where and why. "
        "If the user's feedback is aimed at fixing that failure, use this as "
        "context. If it's unrelated, ignore this block.",
        "",
    ]
    if stage:
        lines.append(f"- **failed stage**: `{stage}`")
    if triggered:
        lines.append(f"- **rule triggered**: `{triggered}`")
    if reason:
        lines.append(f"- **failure reason**: {reason[:500]}")
    if tail:
        # Cap tail at ~800 chars so it doesn't blow up the prompt
        capped = tail[-800:] if len(tail) > 800 else tail
        lines.append("- **tail of stdout**:")
        lines.append("")
        lines.append("```")
        lines.append(capped)
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


def build_edit_prompt(
    *,
    workflow_name: str,
    current_yaml: str,
    feedback: str,
    last_failure: dict | None = None,
) -> str:
    """Prompt for iterative edits driven by user feedback.

    The workflow already exists on disk at `{TASKS_DIR}/<workflow_name>.workflow.yaml`.
    The agent's job is to read it, apply `feedback`, write it back.

    The full authoring guide is NOT inlined here — the agent Reads the
    hot version on demand from `GUIDE_HOT_LOCAL_PATH` and the full version
    from `GUIDE_LOCAL_PATH` if it needs section-level detail. This keeps
    each edit call cheap (~20-30s cheaper than shipping 40KB every round)
    and matches the debug-session flow.

    `last_failure`, if set, describes the workflow's most recent failed
    mission so the agent can consult it when the feedback is a fix request.
    """
    last_failure_block = _render_last_failure(last_failure)
    return f"""# Task: edit a CSM workflow YAML according to user feedback

You are CSM's edit agent. The user already has a workflow YAML in service and
wants to adjust **part** of it. Read the existing YAML, apply the feedback,
write it back to the same path.

## The existing workflow

**name**: `{workflow_name}`
**file path**: `{TASKS_DIR}/{workflow_name}.workflow.yaml`

**Current YAML**:

<current_yaml>
{current_yaml}
</current_yaml>

## The user's feedback (what to change)

{feedback}

{last_failure_block}
## The guide (**read on demand** — don't pull the whole thing in up front)

If the change is only text, a threshold, or adding one primitive, you don't
need the guide at all. If the feedback introduces a new concept ("add a poll
stage", "switch from the T2 to the T3 template", "when does F1 apply"), `Read`
the **condensed** guide at `{GUIDE_HOT_LOCAL_PATH}`; if that isn't enough, read
the **full** one at `{GUIDE_LOCAL_PATH}`.

## Rules of engagement (strict)

1. **Keep the workflow name.** The top-level `name:` field stays
   `{workflow_name}`. Renaming is not allowed.
2. **Minimise the change.** Touch only what the feedback explicitly asks for —
   the relevant stage, field or threshold. Every other stage, parameter, output
   and prompt stays **exactly as it was**.
3. **Preserve the existing semantics.** If the feedback conflicts with the
   workflow's current logic — "delete the apply_changes stage" while downstream
   still references it — either fix the upstream/downstream references in the
   same write, or point out the conflict in your reasoning and propose a safe
   compromise.
4. **Don't drop the clarifications comment.** If the YAML starts with a
   `# clarifications:` block, keep it verbatim unless the user asks otherwise.
5. **The write path must be** `{TASKS_DIR}/{workflow_name}.workflow.yaml`
   (overwrite in place).
6. When you're done, self-check against guide §7 R9-R19 and fix violations in
   place until it reads 0 fail.
7. Your **final line** must be exactly:

       wrote {TASKS_DIR}/{workflow_name}.workflow.yaml

## Hard constraints, restated

- Use only the placeholders listed in guide §3; escape any other `{{...}}`
- A section arg must carry the `## ` prefix
- `regex_match` uses loose `.*` matching
- A kind=claude stage must have non-empty outputs; a kind=poll stage must have none
- A poll check may only poll markers the underlying task **actually produces**

Go.
"""

