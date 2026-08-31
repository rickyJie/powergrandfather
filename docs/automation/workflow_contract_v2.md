# M8 Workflow Contract v2

**Who this is for**: someone who wants to model a multi-step process as a
CSM Mission and needs to write the YAML from scratch. Covers the shape of
the file, every placeholder you can use, all 6 validation primitives, the
three poll-check forms, and — importantly — every trap the CSM team hit
while writing their own test suite.

Read alongside:
- `tasks/sample_experiment.workflow.yaml` — canonical linear-pipeline example
- `docs/automation/workflow_contract.md` — the v1 TaskDef contract (single-task)

---

## 1. When to use a Workflow vs a TaskDef

| Use TaskDef when | Use Workflow when |
|---|---|
| One-shot: read, transform, write, exit | Multi-step process with clear phase boundaries |
| No cross-step data flow | Later steps read earlier steps' outputs |
| No "wait for external event" | Poll for cluster job / external file / etc. |
| Doesn't need retry-from-a-specific-stage | Retry semantics matter |
| Runs on cron | (Currently: still wrap in TaskDef because scheduler fires TaskDefs only. Recurring Mission is on the roadmap.) |

**Rule of thumb**: if the story is "step 1 produces X, step 2 needs X, step 3 waits for Y", it's a Workflow.

---

## 2. Anatomy

```yaml
name: <lowercase_underscore>                      # unique within the DB
description: |                                    # human-readable, multi-line ok
  Two-line summary of what the workflow does.

parameters:                                       # values callers can pass at launch
  - name: topic
    type: string
    required: true
    description: "Free-text subject of this run"
  - name: baseline
    type: string
    default: "current_sota"                       # optional; auto-populated if caller omits

workspace: ".workflow/reports/topic_x/{mission_id}"  # relative to project_root; supports {mission_id} / {workflow_name}

global_timeout: 604800                            # seconds; mission fails if it runs longer

stages:
  - name: <stage_name>                            # lowercase_underscore, unique in workflow
    kind: claude | poll
    # ...stage-specific fields...

final_outputs:                                    # files the API surfaces as "the deliverables"
  - "{ws}/final/report.md"
```

**Sanity check before you commit**: `POST /api/workflows/{name}/preview` (see §7).
It renders every prompt against a synthetic workspace, runs the structural
reviewer, and reports every issue at $0 cost.

---

## 3. Placeholder vocabulary

CSM parses `{...}` in every string in the YAML (prompts, outputs paths,
poll check file paths). **Anything not on this list is a hard error**.

| Placeholder | Resolves to | Example |
|---|---|---|
| `{ws}` | Mission's workspace_path (absolute) | `{ws}/design.md` |
| `{mission_id}` | UUID of this Mission | `{ws}/logs/{mission_id}.log` |
| `{workflow_name}` | The workflow's `name` field | `{workflow_name}_run.log` |
| `{params.NAME}` | `mission.parameters[NAME]` (str-coerced) | `for {params.topic}` |
| `{stages.STAGE.outputs[N]}` | Nth output of a completed prior stage | `read {stages.design.outputs[0]}` |
| `{{...}}` | Literal `{...}` (escape) | `Generated {{ISO date}}` — passes literal `{ISO date}` to claude |

### 🚨 Gotcha #1 — Literal braces need escaping

**Symptom**: You write `Generated {ISO date}` as a fill-in marker for claude.
Mission fails immediately with `unknown placeholder {ISO date}`.

**Cause**: The renderer parses every `{...}`. `{ISO date}` doesn't match any
known shape.

**Fix**: Escape with `{{...}}`. Example:

```yaml
prompt: |
  Write a report.
  Header: `# Report — Generated {{ISO date}}`   # ← escaped, claude sees {ISO date}
  Body: "for {params.topic}"                     # ← real placeholder
```

Applies to JSON literals too — `{"done": true}` must be written as `{{"done": true}}`.

### 🚨 Gotcha #2 — `{params.X}` for load-bindings

Load-binding poll stages inject a value into `mission.parameters` at runtime.
Subsequent stages reference it as `{params.X}`, not `{X}`. Preview will
FLAG this as missing (it doesn't simulate load-bindings) but runtime will
work. See §6.

---

## 4. Claude stages

```yaml
- name: design
  kind: claude
  prompt: |
    Fill in the story. Reference {params.topic} and use {ws} for output paths.
  outputs:
    - "{ws}/01-design/design.md"                # every path claude produces
  validation:
    - file: "{ws}/01-design/design.md"
      primitives:
        - file_exists
        - min_chars: 200
        - required_sections:
            - "Baseline"                        # `#` prefix stripped internally
            - "Changes"
            - "Hypothesis"
        - regex_match:
            pattern: '0\.[0-9]{4}'
            section: "Expected wauc"            # scope pattern to a header block
  time_budget: 900s                             # optional; documentary, not enforced yet
```

### 🚨 Gotcha #3 — Section header syntax

`required_sections` matches **whole-line** headers with `^#+\s+<name>\s*$`
after stripping the caller-supplied `#` prefix. Practically:

| YAML entry | Claude wrote | Matches? |
|---|---|---|
| `"## Result"` | `## Result` | ✓ |
| `"## Result"` | `# Result` | ✓ (any `#` count) |
| `"## Result"` | `## Result — final` | ✗ (trailing text) |
| `"## Result"` | `## result` | ✗ (case) |
| `"Result"` | `## Result` | ✓ (prefix stripped) |

**Fix for trailing text**: instruct claude explicitly not to append.
See G4 fixture in `tasks/test_g4_revalidate.workflow.yaml`.

### 🚨 Gotcha #4 — Output rows carry over

When a claude stage's validation passes, CSM writes one `Output` row per
declared `outputs:` entry. Downstream stages see them via
`{stages.X.outputs[N]}`. **Only paths on the `outputs:` list become
addressable.** Files produced by claude that you didn't declare are
invisible to later stages.

### 🚨 Gotcha #5 — Prompt renders BEFORE claude runs

CSM substitutes placeholders in the prompt template, then hands the
resulting string to claude. So `{ws}` in your prompt gets replaced with
`/data/research/xyz/.workflow/reports/.../mission-id/`. That final string
is what claude sees.

Consequence: if your prompt has bash examples with `{ws}/foo`, escape or
render-time expansion is fine, but don't accidentally use `{X}` for
things you meant to be literal shell variables.

---

## 5. Poll stages

```yaml
- name: wait_train
  kind: poll
  poll_interval: 30s
  timeout: 86400s
  check:
    # Form 1: shell-exec — passes iff exit code 0.
    - command: ["squeue", "-j", "12345", "-h"]

    # Form 2: primitive — apply primitives to a file.
    - file: "{stages.launch.outputs[0]}"
      primitives:
        - file_exists

    # Form 3: load-binding — read a file, extract a field, bind for later.
    - file: "{stages.launch.outputs[0]}"
      load_as: json                             # or 'text'
      extract_field: metrics.exp_path           # dotted path (json) or regex (text)
      as: exp_path                              # binds mission.parameters['exp_path']

    # Form 4: after binding, use it downstream in same iteration.
    - file: "{params.exp_path}/train_log/report.json"
      primitives:
        - file_exists
        - jsonschema:
            type: object
            required: [current]
```

Iteration passes iff **every** check entry passes. On pass → orchestrator
calls `on_pass` → advance. Load-bindings are persisted to
`mission.parameters` only on successful iterations (partial-iteration
bindings are discarded).

### 🚨 Gotcha #6 — Load-binding vocabulary

- `load_as: json` — `extract_field` is a dotted path (`a.b.c`); missing
  key or non-dict intermediate → check fails.
- `load_as: text` — `extract_field` is a regex applied with `re.search`.
  First capture group used; if no groups, whole match.
- If `extract_field` is omitted with `load_as: text`, the binding is the
  entire file contents stripped.
- Bracket indexing (`a.b[0]`) in dotted paths is **not** supported — use
  `load_as: text` + regex, or add the value to top-level of the JSON.

---

## 6. Validation primitives

All 6 primitives take a rendered file path as the first arg. Each returns
`(passed, reason)`.

| Primitive | Args | Passes when |
|---|---|---|
| `file_exists` | — | Path exists and is a regular file (not dir) |
| `min_chars` | `count: int` (top-level) or `{count, section}` (section-scoped) | Content ≥ count chars |
| `min_size_bytes` | `count: int` | File size ≥ count bytes |
| `required_sections` | `sections: list[str]` | Every name appears as `^#+\s+<name>\s*$` (case-sensitive) |
| `regex_match` | `pattern: str`, optional `section: str` | `re.search(pattern, content_or_section)` returns a match |
| `jsonschema` | inline JSON schema | File parses as JSON and validates against schema |
| `contains_substring` | `text: str`, optional `section: str` | Substring appears |

### Section scoping

`regex_match`, `min_chars`, and `contains_substring` accept a
`section:` field. When set, the primitive runs against the content
between that header and the next same-or-higher-level header.

```yaml
- regex_match:
    pattern: 'wauc[:\s]+0\.[0-9]{4}'
    section: "Result"                     # only search within the `# Result` block
```

---

## 6.5 How long does a claude stage actually take?

Real numbers from the test-run-1 corpus (v1 baseline; updated as more data
comes in). **Use these to calibrate your `global_timeout` before launch;
guessing based on "how long would I take" is off by 5-10×.**

| Stage shape | Real duration | What made it slow / fast |
|---|---|---|
| Write ONE short markdown file, no tool_use (`test_a_changelog`) | ~30 min | Claude "thinks aloud" on structure even for trivial writes |
| Scan + write, tool_use dense (`test_b_code_quality`) | ~2 min | rg + Read tools return fast; formatting is minimal |
| Run one shell command + write structured report (`test_c ruff_check` stage) | >30 min | Wall-clock includes structuring output into named sections |
| Design a 4-section markdown from prompt (`test_e design`) | ~14 min | Writing multi-section markdown with anchors is the slow path |
| Launcher curl-and-log (`test_c launcher`, `test_e launcher`) | ~30s | Bash-only, minimal claude reasoning |

**Rule of thumb**:

```
budget per stage ≈
    30s                          # if it's a bash-only launcher
  + 3 min per shell tool_use     # ruff / pytest / rg
  + 5 min per validation section # requires structured output
  + 5 min per output file        # each file needs "review before write"
  × 1.5 safety margin
```

Then `global_timeout = sum(stage budgets) × 1.3`.

For a 3-stage workflow where each stage writes a 4-section report and
runs 1 shell command, the honest budget is:
`(30s + 3min + 4×5min + 5min) × 1.5 × 3 × 1.3 ≈ 5900s ≈ 100 min`.

**Undersizing is the #1 first-time failure mode** — see the test-run-1
findings for two missions that failed on this alone.

The preview endpoint returns an `estimated_duration_sec` per stage +
`suggested_global_timeout` at the top level so you don't have to do this
math by hand. If your YAML's `global_timeout` is lower than
`suggested_global_timeout`, the mission is likely to time out.

## 7. The preview endpoint — use this before you launch

`POST /api/workflows/{name}/preview` — free dry-run. Body optional:

```json
{"params": {"topic": "signal review"}}
```

Returns:

```json
{
  "workflow_name": "signal_review",
  "params_supplied": {"topic": "signal review"},
  "params_effective": {"topic": "signal review", "baseline": "current_sota"},
  "required_params": ["topic"],
  "structural_review": {"status": "passed", "rules": [...]},
  "stages": [
    {
      "name": "design",
      "kind": "claude",
      "prompt_rendered": "Write a design for signal review ...",
      "prompt_render_errors": [],
      "outputs_resolved": ["<preview-workspace>/signal_review/01-design/design.md"],
      "outputs_render_errors": [],
      "validation_summary": [{"file": "...", "primitives": ["file_exists", "min_chars"]}]
    }
  ]
}
```

**Every error you see here you'd have seen at real launch — but for $0.**

Known limitation: preview doesn't simulate poll load-bindings. A downstream
stage that references `{params.hint1}` set by a poll load-binding will
show a `missing parameter` error in preview; at runtime it works.

---

## 8. Retry semantics

`POST /api/missions/{id}/retry?stage=<name>&mode=<mode>`

Two modes:

| `mode` | Behavior | Use when |
|---|---|---|
| `rerun` (default) | Spawn a fresh AUTO session (claude) or new poll loop (poll). Overwrites files. | Stage crashed / claude gave up |
| `revalidate` | Skip spawn; re-run validation against **current** workspace state. Pass → advance; fail → stay failed with new reason. | You manually fixed the output file and want to unstick |

### 🚨 Gotcha #7 — Rerun with deterministic prompt loops

If a stage's prompt deterministically produces content that fails
validation, `rerun` will infinite-loop-fail: fresh session → same
output → same validation failure. Two fixes:

1. Change the prompt to read external hint state (e.g.
   `if {ws}/hint.txt exists, use its content; else default`).
2. Use `mode=revalidate` after manually editing the output.

The `test_f_retry` fixture uses pattern #1; `test_g4_revalidate` uses #2.

---

## 9. Idle-hang backstop

An AUTO session that neither completes (no `end_turn`) nor crashes (no
PTY EOF) — for example, claude waiting silently in its REPL — used to
hang until manually killed. Since Phase 1 post-mortem this is caught by
`SESSION_IDLE`: 30 min of no JSONL activity on an AUTO session → runner
force-stops.

You don't need to do anything to opt in. The threshold is
`settings.session_idle_minutes` (default 30). Set a lower value if your
stages are typically shorter than 30 min and a stuck one should surface
faster.

---

## 10. Failure playbook

| Symptom | Cause | Fix |
|---|---|---|
| `unknown placeholder {X}` | Literal brace | Escape as `{{X}}` |
| `missing parameter 'X'` | `params.X` referenced but caller didn't supply and no default | Add `default:` on param spec, or supply at launch |
| `unknown stage 'X'` | `{stages.X.outputs[N]}` refers to a stage not yet run or misspelt | Check spec + order |
| `output index N out of range` | Stage produced fewer outputs than referenced | Adjust index or add output |
| `validation failed: ['file_exists...']` | Claude didn't produce the declared output | Check prompt; run preview to see rendered prompt |
| `validation failed: ['required_sections...']` | Header case / trailing text mismatch | See Gotcha #3 |
| `stage 'X' revalidation failed` | You called `mode=revalidate` but current file still fails | Look at the specific `failed_checks` list; edit file |
| Mission `succeeded` but nothing useful | Validation passed too liberally | Tighten primitives (add `min_chars`, `contains_substring`) |
| Stage runs 3h+ | Was fixed in Phase 1 post-mortem — idle backstop now catches this | Verify `session_idle_minutes` config |

---

## 11. What's not supported yet

If your workflow needs any of these, you'll have to fake it at the
launcher level today. On the roadmap:

- **Parallel stages** — e.g. try 3 seeds concurrently. Today: sequential only.
- **Conditional stages** — `when: params.mode == 'strict'`. Today: all stages run.
- **Manual approval gate** — `kind: manual` blocking on API-driven approval. Today: paused state exists but no gate primitive.
- **Recurring Missions** — schedule a workflow directly (not via a wrapper TaskDef).
- **Mission-to-Mission chain** — A launches B on success.
- **Nested workflows / sub-missions** — reuse a workflow inside another.
- **Retry-with-different-params** — parameter sweep.
- **Cross-mission shared workspace** — knowledge base pattern.

Every one of these is tracked as a follow-up.
