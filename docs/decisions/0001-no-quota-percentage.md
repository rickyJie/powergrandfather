# ADR 0001 — No quota% estimation in v1

**Date**: 2026-06-20
**Status**: Accepted
**Phase**: design (pre-P0)

## Context

Anthropic does not publish exact subscription quota limits. We can measure usage precisely from JSONL files but the denominator (max tokens per 5h window) is opaque. Three signals are available to estimate the limit:

1. Historical rate-limit hits ("you've hit your limit" text) — gives observed upper bound
2. Public/community estimates of plan limits — known imprecise, change without notice
3. User-configured manual override — accurate but high friction

## Decision

**v1 ships without a quota% estimate.** The token dashboard shows:
- Absolute current 5h window usage (messages, cache_creation, output)
- 24h historical trend
- Top consumers by session/project/model
- Recorded hit observations (each with full 5h snapshot)
- User-configurable absolute-threshold alerts

## Why

Statistical analysis of n=9 historical hit observations across 6 days showed:
- Message count is the most stable signal at hit time (CV 13.5%, range 991-1628)
- cache_creation_tokens next-best (CV 20.3%, range 9.2M-17.8M)
- Total tokens CV is 59% — too noisy to use directly
- Common assumptions ("Opus 5x") are falsified by the data — model-weighted CV is *worse*

n=9 is too sparse for a confident point estimate. Bootstrap 90% CI for the message-count median spans [1178, 1411] — ±15% just from sampling. Quoting "you've used 67% of quota" with that uncertainty would mislead.

## Consequences

- (-) Users have to know their own consumption pattern to set thresholds; UI cannot proactively say "you'll hit at 17:30".
- (-) Predictive alerts ("at current rate, will hit 80% in 30 min") deferred to v2.
- (+) No misleading precision; what we show is grounded in actual data.
- (+) When ≥30 observations accumulate, we can revisit and add quota% estimation with bootstrap confidence intervals shown to the user.

## Future direction

v2 plan: once HitObservation table has ≥30 rows across ≥30 days, fit a weighted regression `α·input + β·cache_creation + γ·cache_read + δ·output ≈ L` per the prior analysis and report `usage_pct ± CI`. The analysis script lives in conversation history (statistical/methods/weighted-regression).
