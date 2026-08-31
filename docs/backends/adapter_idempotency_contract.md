# Adapter Idempotency Contract (Sync v2 agent-driven)

**Introduced**: 2026-08-03 with sync v2 (design v6 §1 + v7 §3).
**Applies to**: every CLI adapter that declares `SYNC_MEMORY` /
`SYNC_MCP` / `SYNC_SKILLS` capability (see
`backend/csm/backends/base.py::Capability`).

## Why this exists

The sync v2 orchestrator applies each decision in three phases:

1. Phase 1 (DB tx): mutate CSM DB + insert a `fanout_ledger` row
   (`status='pending'`).
2. Phase 2 (no DB tx): fan out to agents via adapter methods.
3. Phase 3 (DB tx): update `last_synced_hashes` + close the ledger
   (`status='done'`).

Between any two phases, the process can crash. On the next tick — or
on `SyncOrchestrator.replay_pending_fanout_ledger()` at boot — the
`pending` row is re-driven from scratch, meaning **the same adapter
call is made twice for the same body**. If the second call has a
different observable effect than the first, the ledger's crash-recovery
guarantee falls apart.

## The contract

For every mutating adapter method that participates in sync fan-out —
concretely `write_memory_marker_block`, `mcp_add`, `mcp_remove`,
`write_skill_bundle`, `write_simple_skill`, `remove_skill` — the
following MUST hold:

> **Two consecutive calls with the same arguments produce the same
> observable end state as one call, with no error surfacing between
> them.**

Concretely:

- **Same identity + same body content** → second call is a no-op or an
  overwrite with byte-identical output. Never returns a "duplicate
  entry" error to the caller.
- **Same identity + different body content** → second call OVERWRITES
  the first. The union / concatenation / append semantics is FORBIDDEN.
- **Different identities** → each call is independent; the adapter
  MUST NOT batch or defer.

"Identity" here is the marker id (memory), the mcp `name`, or the
skill `name`.

## Capability-by-capability guidance

### `SYNC_MEMORY` — `write_memory_marker_block(path, marker_id, body)`

- Use `csm.modules.sync.marker_block.replace_or_append_marker_block` to
  build the target file text. It already dedupes on `marker_id` — the
  block is REPLACED in-place if present, APPENDED otherwise.
- Persist via `atomic_write_with_hash_guard(path, updated.encode())`.
  This surfaces concurrent REPL writes as `ConcurrentWriteDrift`
  (recorded by the caller, not retried automatically) — that's a
  drift signal, not an idempotency violation.
- **Never** manually splice / concatenate marker blocks; that opens the
  door to duplicated blocks when re-run.

### `SYNC_MCP` — `mcp_add`, `mcp_remove`, `mcp_list`

- `mcp_add`: MUST short-circuit if an entry with the same name already
  exists AND has the same shape. The reference implementations
  (`ClaudeAdapter.mcp_add`, `CodexAdapter.mcp_add`) first call
  `mcp_list()` and return a synthetic `CLIResult(returncode=0)`
  when the entry is already present. If the shape differs, the caller
  is responsible for `mcp_remove` + `mcp_add` — do NOT implicitly
  overwrite (some CLIs error on duplicate `add`).
- `mcp_remove`: MUST return `returncode=0` when the entry is already
  absent. The reference implementations pre-check via `mcp_list()`
  and synthesise a "not present" result rather than shelling out.
- `mcp_list`: MUST be side-effect-free. Read-only.

**v7.1 note**: The `raw` field returned by `mcp_list()` is
cross-version unstable (the plain-text `<cli> mcp list` output format
can drift between CLI upgrades). The sentinel-hash helper
(`csm.modules.sync.sentinels.STABLE_MCP_KEYS`) hashes only
`(name, transport)` from each entry. Adapter implementations MAY
include `raw` for downstream rendering / debugging but MUST NOT rely
on any other field being present.

### `SYNC_SKILLS` — `write_skill_bundle`, `remove_skill`, `list_skills`

A skill is a **directory**, not a file. `<skills_dir>/<name>/SKILL.md`
plus every sibling — `query.py`, `references/*.md`, `scripts/*.py`.
Until 2026-08-30 sync moved only SKILL.md, so any skill whose body said
"run ./query.py" arrived on the target unusable, and the drift poller
reported green because it only checked that the directory existed.

- `write_skill_bundle(spec)`: writes SKILL.md **and** every file in
  `spec["files"]`. Each file goes through
  `atomic_write_with_hash_guard`, which gives "last-writer-wins
  overwrite, no concatenation" for free.
  - **The chmod is load-bearing.** `atomic_write_with_hash_guard`
    creates its temp file 0600 and `os.replace`s it into position, so
    the adapter MUST `os.chmod(target, mode)` after every write — not
    just on create. A `query.py` that isn't executable is exactly as
    broken as one that isn't there.
  - **Pruning is scoped to `spec["prune"]`**, the manifest CSM last
    wrote to *this* agent (`Skill.last_synced_files[agent]`). Deleting
    "everything not in the new manifest" would take out files the user
    placed by hand. Files the adapter never wrote are not ours.
  - `rel_path` is re-validated at write time even though the walker
    already validated it at ingest — the DB is reachable through
    `PUT /api/sync/skills/{id}`.
  - Repeat calls with an identical spec produce a byte-identical tree,
    permission bits included (mtime may differ; that's fine).
- `write_simple_skill`: DEPRECATED. Delegates to `write_skill_bundle`
  with an empty bundle. Retained for out-of-tree adapters.
- `remove_skill`: `<skills_dir>/<name>/` recursive delete. MUST be a
  no-op if the dir doesn't exist. MUST refuse to descend outside
  `skills_dir()` (path-traversal guard — `guard_skill_name`).
- **Symlinked skill dirs are refused, not followed.** In a real setup
  most of `~/.claude/skills/*` are symlinks into a skill-book git repo.
  `os.replace()` and `shutil.rmtree()` both follow them, so writing or
  pruning through one silently edits the user's working tree. Both
  `write_skill_bundle` and `remove_skill` MUST raise
  `ExternalSkillSource`; the caller records a
  `DriftReason.EXTERNAL_SOURCE` row and returns `SyncStatus.SKIPPED`.
  Unlike other drift this never self-heals — that's deliberate, the
  user has to decide.
- `list_skills` / `list_skills_full` / `read_skill_bundle`:
  side-effect-free. `list_skills_full` returns `bundle_hash` (over
  SKILL.md plus each file's `(rel_path, mode, content)`) but never the
  bytes — it runs on every agent tick.

## Non-obvious pitfalls

- **Racing external edits**. `atomic_write_with_hash_guard` fails when
  the file's on-disk hash has changed between our pre-read and our
  write. That is NOT an idempotency violation — it's the caller's
  contract with the drift subsystem. `write_memory_marker_block` MUST
  raise `ConcurrentWriteDrift`, not retry silently.
- **Non-idempotent CLI subcommands**. Some CLIs append instead of
  overwrite when re-invoked (e.g. an mcp CLI that stacks duplicate
  entries). If the underlying CLI can't be made idempotent, the
  adapter's Python-side pre-check + short-circuit is the mitigation
  (see the `mcp_add` reference implementations).
- **Rendering identity**. For `mcp_add`, the CLI treats a
  case-preserved name as identity. Never lower-case the name in the
  adapter — the sentinel hash lookups and CLI list lookups would
  desync.

## CI check

Every adapter that opts into a `SYNC_*` capability MUST have a test in
`tests/unit/test_adapter_idempotency.py` proving:

- `write_memory_marker_block(p, "same-id", body)` called twice produces
  a file identical to one call.
- `mcp_add(name, ...)` called twice does not produce two entries in
  `mcp_list()`.
- `write_simple_skill({"name": n, "body_md": b, ...})` called twice
  produces byte-identical `SKILL.md`.
- `write_skill_bundle(spec)` called twice produces a byte-identical
  directory tree **including permission bits**, prunes only paths named
  in `spec["prune"]`, and raises on a symlinked skill dir or a
  traversing `rel_path`.

These are parametrised over both filesystem-convention adapters
(`_SKILL_ADAPTERS`), so adding a third is one line.

The `FakeSyncAdapter` in `tests/unit/test_sync_service.py` is the
reference: its `mcp_add` short-circuits on repeat, its
`write_simple_skill` is a plain `write_text` (byte-idempotent), its
`write_memory_marker_block` uses `replace_or_append_marker_block`.

## What breaks if you violate it

- `SyncOrchestrator.replay_pending_fanout_ledger()` will double-apply
  the same fanout on any crash → agent-side files gain duplicate
  entries or corrupt marker sequences.
- `DriftPoller` may detect the duplication and open a `drift_record`
  row — a symptom, not the cause.
- Users see "why are there two `no-sudo` blocks in my CLAUDE.md?"
  after a mid-tick server restart. That's the tell-tale sign the
  contract has been violated.
