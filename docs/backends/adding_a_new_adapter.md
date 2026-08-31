# Adding a new CLI-adapter to CSM

This guide walks through the steps to bring a third adapter (call it
"gemini" or whatever) online. The architecture is designed so that a
new adapter can be added by touching only files under
`backend/csm/backends/<name>/` and one import line in
`backend/csm/backends/__init__.py`. If you find yourself editing
`session_manager/manager.py`, `event_stream.py`, or any API router,
that's a sign the adapter shape is escaping its abstraction — file a
bug against the CLIAdapter Protocol instead.

**Scope of this doc: the session lifecycle** (argv construction,
tailer, event derivation, hooks). For teaching the Tokens page to show
your adapter's remaining-quota / plan info, see the companion doc
[`adding_a_usage_probe.md`](adding_a_usage_probe.md) — it covers the
`/status` (or equivalent) probe, schema mapping into the shared
`UsageSnapshot` columns, and the pitfalls we hit adding codex.

## 1. Skeleton package

```
backend/csm/backends/gemini/
├── __init__.py       # re-export GeminiAdapter
├── adapter.py        # implements CLIAdapter Protocol
├── tailer.py         # optional: incremental file tail if the CLI writes artifacts
├── events.py         # optional: raw-record → CSMEvent derivation
└── hooks.py          # optional: hook config writer, only if CLI supports hooks
```

## 2. Implement `CLIAdapter`

Copy `backends/claude/adapter.py` as a starting point. Fields you must
set on the class:

```python
class GeminiAdapter:
    name = "gemini"                  # short id, must be unique
    display_name = "Gemini CLI"      # UI label
    icon = "gemini"                  # frontend hint (any string)
    capabilities = frozenset({       # declares what the adapter can do
        Capability.INTERACTIVE_STREAM,
        # + PRE_SPAWN_SESSION_ID if your CLI accepts a session id flag
        # + POST_SPAWN_BIND if id is discovered from an artifact file
        # + HOOKS if you can write per-project hook config
    })
```

Then implement each Protocol method — see `csm/backends/base.py` for
the docstring on each. Minimum viable list:

- `home_dir()`, `default_home_name()`, `auth_file()`, `probe()`
- `pre_spawn_session_id()` / `post_spawn_bind()` (return None if
  capability not declared)
- `build_argv()` — critical: non-native `argv[0]` must be **strict
  pass-through**, no flag injection, no prompt appending. Test-mode
  overrides like `["bash", "-i"]` MUST work.
- `artifact_root()`, `artifact_glob()` — where per-session artifacts
  live under `home_dir()`
- `scan_events()`, `snapshot()`, `restore()`, `take_newly_seen()`
- `install_hooks()` — no-op if `HOOKS` not in capabilities

## 3. Register in the default registry

Edit `backend/csm/backends/__init__.py::build_default_registry`:

```python
def build_default_registry() -> AdapterRegistry:
    from csm.backends.claude.adapter import ClaudeAdapter
    from csm.backends.codex.adapter import CodexAdapter
    from csm.backends.gemini.adapter import GeminiAdapter   # NEW

    return AdapterRegistry([
        ClaudeAdapter(),
        CodexAdapter(),
        GeminiAdapter(),                                    # NEW
    ])
```

## 4. Sandbox env var

The dev sandbox script `scripts/csm-codex-dev-sandbox.sh` exports
`CSM_CLAUDE_HOME` and `CSM_CODEX_HOME`. Add a `CSM_GEMINI_HOME=...` line
pointing at a `/tmp/csm-.../fake_gemini/` and update `paths.py` /
`GeminiAdapter.home_dir()` to honour it.

The sandbox guard (`main.py::_require_sandbox_on_codex_branch`) iterates
`adapter_registry.all()` and checks each adapter's `home_dir()` against
the real one, so it picks up the new adapter automatically.

## 5. Tests

Add `tests/unit/backends/test_gemini_adapter.py` covering:

- Protocol conformance: `assert_conforms(GeminiAdapter())`
- Identity: `name / display_name / capabilities`
- `build_argv`:
  - Strict pass-through for non-native `argv[0]`
  - Injects any flags the CLI needs
  - Handles `initial_prompt` correctly
- Session-id lifecycle: pre_spawn / post_spawn
- `scan_events` fires each canonical event type this CLI can emit
- `probe()` handles missing binary + missing auth

Run:
```bash
PYTHONPATH=./backend pytest tests/unit/backends/test_gemini_adapter.py
```

Then the full suite to catch integration regressions:
```bash
PYTHONPATH=./backend pytest tests/unit/
```

## 6. Enable at runtime

Users control which adapters actually run via
`CSM_ENABLE_<NAME>=1` env vars. So users who want gemini would export
`CSM_ENABLE_GEMINI=1` before launching CSM. The registry loads all
adapters at boot; the `enabled()` filter picks who actually tails +
gets exposed as a valid `agent` option in POST /api/sessions.

## 7. Frontend

If your adapter's `display_name` / `icon` are set, the existing
components already handle it:

- `<AgentBadge :agent="gemini" />` renders correctly.
- `<AgentSelector />` includes it in the dropdown once it's registered.
- The FirstRunWizard picks it up automatically as a choice card.

The colour of the badge (see `frontend/src/components/AgentBadge.vue`
CSS) is a nice-to-have — add a `.agent-badge--gemini` rule if you want
a specific tint.

## What NOT to do

- **Do not** add `if agent == "gemini"` branches in `session_manager`,
  `event_stream`, `workflow`, `supervisor`, etc. If your adapter needs
  something the abstraction doesn't expose, propose a new
  `Capability` value and pass it through the Protocol.
- **Do not** add `Gemini`-specific columns to `Session` or
  `UserPreference`. The `agent` string is the only discriminator; all
  adapter-specific state lives inside the adapter's own module.
- **Do not** silently degrade a canonical Event when your CLI doesn't
  support it. Skip emission entirely. Consumers already handle
  "never emitted" cases.

## Verifying the abstraction still holds

Run the lint after adding your adapter:

```bash
./scripts/lint-agent-abstraction.sh
```

Exit code 0 = clean. Any LEAK line pointing at your new code is a
signal you special-cased the adapter name somewhere it shouldn't have.
