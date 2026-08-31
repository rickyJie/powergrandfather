# ADR 0003 — Retire hardcoded AlertRule; adopt agent-authored check scripts

**Date**: 2026-07-10
**Status**: Accepted
**Phase**: post-P4

## Context

The v1 token alert subsystem (M5) shipped with `AlertRule`, three fixed
`condition_type`s (`absolute` / `percentage` / `predictive`), and a fixed
metric list from `TrendQueries.current_window()`. In practice this had three
limits:

1. **Rigid metric set** — only `msg_count`, `input_tokens`, `cache_*`,
   `output_tokens`, `total_tokens`, `estimated_cost_usd`. Users asking "fire
   when the cache-hit ratio drops below 30% AND cost > $50" had no path except
   filing a feature request.
2. **Rigid comparison shape** — `actual >= threshold` for absolute,
   `pct >= threshold` for percentage. Compound conditions and rate-of-change
   comparisons weren't expressible.
3. **`condition_type` semantics diverged from code** — the `alert.py` docstring
   said "v1 only supports ABSOLUTE_THRESHOLD" but percentage + predictive
   branches had been implemented and shipped anyway. Confusing for both users
   and maintainers.

The `scope` field on `AlertRule` was stored but never consumed by the
evaluator, further muddling the contract.

## Decision

Replace the entire subsystem with **agent-authored Python check scripts**:

- User describes the rule in natural language (`nl_description`) and provides
  a rough `threshold_spec` (JSON, informational).
- At rule-creation time, CSM spawns `claude -p` once to generate a Python
  function:
  ```python
  def check(window: dict) -> tuple[bool, dict]:
      ...
  ```
  The generated script is dry-run against the current 5h window and shown to
  the user as a preview. Only after user confirmation is it persisted.
- Each rule has its own `poll_interval_sec` asyncio task. On tick: snapshot
  window → run the script in an isolated `python -c` subprocess (10 s hard
  timeout) → if `fired=True` and `cooldown_sec` elapsed, emit an alert event.
- Per-rule `channels: list[str]` (subset of `{"inapp", "lark"}`) controls
  routing. LarkSink honors an `_skip_lark` marker so per-rule opt-out works
  even when a default Lark target is configured in env.
- Optional `escalate: bool` — when `True`, on fire we build a rich context
  blob (top-N sessions with tool distribution, model split, cache-hit ratio,
  30-minute per-minute curve) and call `claude -p` again to synthesize a
  root-cause + recommendations notification body. Failures are degraded to
  the plain "threshold crossed" line.

Old `AlertRule` model, `AlertEvaluator`, `alert_rule` table, and
`/api/tokens/alert-rules/*` endpoints are all removed in the same commit.
Migration `j2e5f6a7b8c9_replace_alert_rule_with_agent_alert_rule.py` drops
the old table and creates `agent_alert_rule`.

## Trade-offs

**What we lost**
- Legibility of the rule in the DB — `check_script` is opaque Python source
  rather than three tidy columns. Diff'ing rule intent now means diff'ing
  code.
- Determinism of rule generation — the same NL description can yield
  different scripts across runs. Users should always dry-run + save the
  preview; the persisted script becomes the source of truth from then on.

**What we gained**
- Any check expressible in stdlib Python against the window dict works —
  compound conditions, rate-of-change, arbitrary arithmetic.
- Escalation flow gives users actionable notifications ("Session ef8f105f
  ran Bash in a loop") instead of "metric=msg_count actual=X threshold=Y".
- Contract with the check function is exactly one Python signature. No enum
  drift, no `scope` field that isn't wired, no docstring/code mismatch.

## Non-goals for v1

- **No sandbox against adversarial scripts.** The subprocess protects
  against accidental damage (bad SQL, thread leaks, imports of forbidden
  libraries by mistake) but not intentional escape. This is a single-user
  local tool; the trust model is "the user reviews the generated script
  before saving".
- **No rate limit on agent escalation.** We ship with escalation opt-in
  per-rule and no global cap, and will observe real usage before deciding
  whether a global per-hour cap is needed.
- **No batch generation.** The two-step flow (generate → preview → save) is
  interactive by design — batch would remove the review step that keeps
  agent output trustworthy.

## Alternatives considered

- **Keep AlertRule, add a `custom_expression` field.** Rejected: still
  requires us to design an expression language (or embed one). Python is
  already the lingua franca of the project.
- **MCP tool access at check-time.** Rejected for v1: complexity + token
  cost + non-deterministic tick latency (agent could hit rate limits during
  a burst window, missing the alerts we care about most).
- **Escalation on every fire (no `escalate` flag).** Rejected because a
  60-second poll × N rules × claude call would meaningfully raise the
  monitoring tool's own token burn — precisely the thing users install
  alerts to prevent.
