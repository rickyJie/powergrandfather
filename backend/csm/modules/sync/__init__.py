"""Multi-agent config sync subsystem (P0 v3).

Package layout:

- `cli_runner`   — subprocess wrapper + CLIResult dataclass (B3).
- `errors`       — SyncPreflightError / ConcurrentWriteDrift (B1, B5).
- `atomic_write` — write-hash-compare guard (B1).
- `env_expand`   — resolve_env_refs (B5).
- `service`      — SyncService + DriftPoller (Phase 2.6).

See docs/backends/multi_agent_sync_spec.md for the full design.
"""
