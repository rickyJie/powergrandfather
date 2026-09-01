# PowerGrandFather dogfooding record

[中文版本](dogfooding.zh-CN.md)

PowerGrandFather was used on its maintainer's own development machine before
the public source snapshot was published. This page records the aggregate
figures cited by the README and explains their limits.

## Snapshot

The database snapshot covers 2026-06-24 through 2026-08-31.

| Metric | Aggregate result |
|---|---:|
| Managed sessions | 356 |
| Cumulative agent time | 354.5 hours |
| Cumulative human-interaction time | 54.0 hours |
| Workflow missions | 21 |
| Successful / failed / cancelled missions | 12 / 7 / 2 |
| Workflow stage executions | 103 |
| Successful / failed stages | 95 / 8 |
| Feedback items | 75 |
| Resolved / open feedback items | 59 / 16 |

The private development repository also accumulated 325 commits between
2026-06-28 and 2026-08-31 and was tagged from v0.1.0 through v0.9.3. The
public repository is a sanitized source snapshot of v0.9.3: internal task
definitions, private paths, transcripts and operational data are deliberately
not part of its Git history.

## Definitions and caveats

- **Managed session** means one row created for an interactive, automated or
  agent-deck session. It is not a unique user.
- **Agent time** sums every agent work interval. Concurrent agents accumulate
  time independently, so 354.5 agent-hours is not 354.5 hours of wall-clock
  time and must not be interpreted as a productivity multiplier.
- **Workflow stage execution** is one persisted stage attempt. A mission can
  contain several stages and fails when a required stage fails, so stage and
  mission success rates answer different questions.
- **Feedback item** is an issue recorded through the private instance's
  feedback workflow. “Resolved” means it was marked resolved there; it is not
  a public GitHub issue count.
- These figures describe the maintainer's private instance and changing
  pre-release builds. They are evidence of sustained dogfooding, not a
  benchmark or a reliability guarantee for another machine.

## Privacy boundary

No prompt, transcript, repository path, tester identity or raw database row
was copied into the public repository. Only the aggregate counts above were
carried over. PowerGrandFather does not send public-product telemetry by
default.
