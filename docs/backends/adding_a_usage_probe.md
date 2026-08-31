# Adding a usage-quota probe for a new CLI adapter

Companion to `adding_a_new_adapter.md` — that doc covers the **session
lifecycle** side (argv, tailer, events, hooks). This doc covers the
**quota-panel** side: how to teach the Token page to show remaining
quota / plan info for a new agent.

The claude → codex build taught us most of these lessons the hard way.
Follow this playbook and you avoid the schema drift and rework we ate
between M14 (`codex /usage`) and its /status replacement.

Table of contents:

1. [Is this adapter even worth a probe?](#1-decision-do-we-need-a-probe-at-all)
2. [Field research (capture before code)](#2-field-research--capture-before-code)
3. [Schema mapping (align with claude first)](#3-schema-mapping--align-with-claude-first)
4. [Field normalization at parse time](#4-field-normalization-at-parse-time-not-render-time)
5. [Implementation skeleton](#5-implementation-skeleton)
6. [Testing strategy](#6-testing-strategy)
7. [Frontend integration](#7-frontend-integration)
8. [Pitfalls we hit — read this before starting](#8-pitfalls-we-hit--read-this-before-starting)

---

## 1. Decision: do we need a probe at all?

Not every adapter needs a first-class quota probe. Gate on these:

| Signal | Verdict |
|---|---|
| CLI has a `/usage` or `/status` slash-command that shows remaining quota | ✅ Build the probe |
| CLI only shows lifetime tokens (no reset window) | ⚠ Probably not worth it — Token page shows historical rollups already |
| Quota info is web-panel only (no CLI path) | ❌ Skip. Point the user at the web panel in a placeholder card |
| CLI is offline / open-weights / has no quota concept | ❌ Skip. Register the adapter without a probe |

If you skip, register the adapter without a `usage_scheduler` branch — the Tokens page will render a placeholder ("Plan-quota tracking not available for {agent}") automatically.

## 2. Field research — capture BEFORE code

**Do not write the probe from documentation or memory.** TUI output
formats change per version, and even the same version renders
differently on different terminal widths. Capture real output first.

Recipe:

```python
# /tmp/status_capture.py — throwaway. Run once. Screenshot the output.
import os, re, time, pexpect

# Use the user's REAL home for whichever CLI you're probing so auth works.
env = {k: v for k, v in os.environ.items() if k != "GEMINI_HOME"}

child = pexpect.spawn(
    "gemini --whatever-flags",
    env=env, encoding="utf-8",
    dimensions=(50, 120), timeout=30,
)
time.sleep(8)  # boot

# Try the slash command you think shows quota.
child.send("/quota"); child.send("\r")
time.sleep(4)

# Read whatever came back, strip ANSI, print.
out = []
end = time.time() + 1.5
while time.time() < end:
    try:
        chunk = child.read_nonblocking(size=8192, timeout=0.3)
        if chunk: out.append(chunk)
    except pexpect.TIMEOUT: continue
    except pexpect.EOF: break
raw = "".join(out)
clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw).replace("\r", "")
print(clean)
```

**Look for these things and document them in the probe module's docstring:**

- Field names and their exact rendering (`Weekly limit: [bar] N% left` vs `Quota: N/M requests`)
- Semantic direction of the percentage (used-% or left-%?)
- Reset time format (raw display string, ISO datetime, relative "in 2h"?)
- Whether the numbers appear immediately or need a refresh (codex needs
  two `/status` calls — first triggers server refresh, second reads
  numbers)
- Whether the pane needs a specific terminal width to render properly
  (some CLIs word-wrap and the parser then breaks)

**Then** write the probe. The docstring should link the reader back to
the fixture so future-you can re-diff against a new CLI version.

## 3. Schema mapping — align with claude first

The `UsageSnapshot` model has these shared columns:

```
session_pct     session_reset     week_pct     week_reset
tier            subscription_type
```

**Rule: fit the new adapter's data INTO these columns. Do not add
adapter-specific columns unless the concept genuinely has no mapping.**

Concrete mappings we did:

| Claude field | Codex value | How |
|---|---|---|
| `session_pct` | `None` | Codex has no 5h window |
| `session_reset` | `None` | ditto |
| `week_pct` | `100 - "N% left"` | Codex displays remaining; convert to used-% |
| `week_reset` | `"14:11 on 2 Aug"` | Raw display string from CLI, no ISO conversion |
| `tier` | `"Plus"` / `"Pro"` | Parsed from Account line |
| `subscription_type` | same as `tier` | Codex has one tier concept, not two — duplicate |

**Why not add columns per agent?** We already made that mistake once —
the initial codex probe added `codex_lifetime_tokens`,
`codex_peak_daily_tokens`, `codex_streak_days`,
`codex_longest_task_sec` for the `/usage` panel. When we switched to
`/status`, those became dead weight (kept as legacy nullable columns —
see `usage_snapshot.py`). The lesson: **an adapter-shaped schema
compounds with every new adapter**. A claude-shaped schema stays flat.

If your adapter genuinely exposes a field that doesn't map (e.g. a
credit balance in dollars), open the discussion first:

- Is this displayable in the same bar layout? Then rescale into `week_pct`.
- Is it fundamentally different (e.g. a numeric credit count)? Then
  propose a new column that ALL adapters could plausibly fill later.
  Adding a `codex_credits` column repeats the anti-pattern.

## 4. Field normalization at parse time, not render time

Two normalizations we do at parse time so the frontend never branches:

**Percentage direction — always "used %".** Codex shows "N% left".
Claude shows "N% used". Codex probe does `100 - left` at parse time so
`week_pct` means the same thing (fill %) in both rows. The bar-fill CSS
then works uniformly:

```html
<div class="ul-bar-fill" :style="{ width: (usageLive.latest.week_pct ?? 0) + '%' }"/>
```

**Clamp obviously bogus values.** Codex has been seen to render 101%
during a mid-refresh frame — clamp to [0, 100] at parse time.

**Do NOT parse reset timestamps into ISO.** Keep the raw display
string. Reason: the UI only displays it back, and each CLI's rendering
already includes the user's timezone + convenient shorthand ("in 2h",
"Aug 1, 11:59pm"). Re-parsing gets you into DST bugs and locale bugs
for no user benefit.

## 5. Implementation skeleton

Six files change for a new probe. Names shown for a hypothetical `gemini`
adapter.

### 5.1 The probe itself

`backend/csm/modules/token/gemini_usage_probe.py` — parallel to
`codex_usage_probe.py`. Structure:

```python
@dataclass
class GeminiUsageProbeResult:
    """Aligned with UsageProbeResult (claude) so scheduler can persist
    through the same UsageSnapshot columns."""
    session_pct: int | None       # None if agent has no session window
    session_reset: str | None
    week_pct: int | None
    week_reset: str | None
    tier: str | None
    subscription_type: str | None
    raw_pane: str
    error: str | None
    duration_ms: int
    fetched_at: datetime


def _parse_pane(pane: str) -> tuple[...]:
    """Pure function on a stripped pane string. Unit-testable without spawn."""


class GeminiUsagePoller:
    """Async wrapper. Serialised via asyncio.Lock (two concurrent probes
    fight over stdin). Probe method runs in a worker thread via
    asyncio.to_thread to avoid blocking the event loop with pexpect."""

    async def probe(self) -> GeminiUsageProbeResult: ...
```

Key contracts:

- **Never raise from `.probe()`.** Return a result with `error=` set instead. Scheduler code assumes probe results are always well-formed.
- **Always fill `fetched_at`** so downstream can compute staleness.
- **Cap `raw_pane` at 4000 chars.** Only kept for debug on parse failure.
- **Use a stable trust-anchor cwd** under `$HOME/.csm/gemini_status_probe/` — not `/tmp/`, because `/tmp` cleanups re-trigger any first-run trust prompt.

### 5.2 Scheduler wiring

`backend/csm/modules/token/usage_scheduler.py`:

Add a `_probe_gemini_once(source)` mirror of `_probe_codex_once`.
Add its call site inside `_probe_all(source)`:

```python
async def _probe_all(self, source: str) -> None:
    await asyncio.gather(
        self._probe_claude_once(source),
        self._probe_codex_once(source),
        self._probe_gemini_once(source),   # NEW
        return_exceptions=True,
    )
```

`return_exceptions=True` is load-bearing — a slow / crashed probe must
not block the others.

Add the skip-reason check in the probe method:

```python
async def _probe_gemini_once(self, source: str) -> GeminiUsageProbeResult:
    skip = await _adapter_skip_reason(self._registry, "gemini")
    if skip:
        return _skip_result(skip)   # returns quickly, no subprocess spawn
    ...
```

The `_adapter_skip_reason` helper is shared — it fails fast if the
adapter isn't registered or its own `probe()` says it's not usable.
Prevents a 30-second timeout for users who don't have the CLI installed.

Add `run_now(source, agent='gemini')` branch so the API can force-run
this specific probe.

### 5.3 API endpoint

`backend/csm/api/tokens.py`:

The endpoints are already agent-parametric. `GET /api/tokens/usage-live?agent=gemini`
and `POST /api/tokens/usage-live/refresh?agent=gemini` will work as
soon as the scheduler branch exists.

Just verify `_serialize_usage_snapshot` doesn't need changes — it
should be fine as long as your probe writes into the shared columns.

**Rule: agent as query parameter, not as endpoint path.** Do not add
`/api/tokens/usage-live/gemini`. One endpoint per operation.

### 5.4 Migration

**Usually you don't need one.** The shared columns already exist. Only
add a migration if:
- You need a new column that ALL adapters could plausibly fill, or
- Your adapter's `week_reset` string is longer than 200 chars (unlikely)

Do NOT add a migration to remove the legacy codex_* columns. Schema
churn is expensive; unused nullable columns are free. Mark them legacy
in the model docstring and move on.

### 5.5 Frontend template

`frontend/src/views/Tokens.vue` — the template is already
adapter-agnostic. Just make sure your probe writes `week_pct` and (if
applicable) `session_pct` into the snapshot.

The template branches on `isCodexUsage` (should be renamed to
`hideSessionBar` once a third agent lands with the same "no 5h window"
property). Extend as needed:

```typescript
const hideSessionBar = computed(() => {
  const agent = usageLive.value?.latest?.agent
  return agent === 'codex' || agent === 'gemini'  // etc
})
```

The header derives from the snapshot:

```typescript
const usageLabel = computed(() => {
  const agentName = { claude: 'Claude', codex: 'Codex', gemini: 'Gemini' }[snapshotAgent] ?? 'Plan'
  const tier = _formatTier(...)
  return tier ? `${agentName} ${tier} usage` : `${agentName} plan usage`
})
```

### 5.6 Frontend fetch trigger

Also `Tokens.vue` — `probeAgent` computed decides which agent's
snapshot to fetch based on the current scope:

```typescript
const probeAgent = computed<'claude' | 'codex' | 'gemini' | null>(() => {
  const s = effectiveAgentScope.value
  if (s === 'codex') return 'codex'
  if (s === 'gemini') return 'gemini'
  if (s === null || s === 'claude') return 'claude'
  return null
})
```

## 6. Testing strategy

Three layers, in this order:

### 6.1 Parser unit tests — no subprocess

`tests/unit/test_gemini_usage_probe_parser.py`. Feed captured pane
strings to `_parse_pane` directly. Cases to cover (each is one test):

- Real captured pane from a live probe (paste it verbatim — this is
  your regression anchor when the CLI changes)
- Partial values (missing one field shouldn't kill the others)
- Edge percentages (0%, 100%, clamped >100%)
- Case-insensitive labels (some CLIs render `Weekly Limit` some
  `weekly limit`)
- Refresh-marker case (CLI says "loading…" — probe should return None
  fields not misparse)
- Empty pane (returns all Nones, no crash)

Run: `PYTHONPATH=./backend pytest tests/unit/test_gemini_usage_probe_parser.py -q`

### 6.2 Sandbox smoke — end-to-end via HTTP

After deploying to the codex sandbox:

```bash
curl -sS -X POST "http://127.0.0.1:8100/api/tokens/usage-live/refresh?agent=gemini" \
  -H "X-CSM-Client: 1" --max-time 90 | python3 -m json.tool
```

Assert on:
- `week_pct` is an int in [0, 100] (or explicitly null if refresh not
  ready)
- `tier` matches what you saw in the /status capture
- `duration_ms` is under your CLI's boot + roundtrip budget
- `error` is null

Then `GET` the same endpoint to confirm the persisted row deserializes
correctly.

### 6.3 Full regression

`PYTHONPATH=./backend pytest -q --deselect <known-broken>`

Any new failure is on you. Pre-existing failures should stay a stable
set — if the pre-existing list grows, you introduced something.

## 7. Frontend integration

Beyond the Tokens.vue changes in 5.5/5.6:

- `AgentBadge.vue` — inherited from `adding_a_new_adapter.md`. Same
  badge is reused on the Tokens hero label + snapshot rows.
- `AgentSelector.vue` — the agent-scope toggle picks up the new adapter
  automatically once it's in the registry.

No new Vue components should be needed. If you find yourself writing a
`GeminiUsagePanel.vue`, stop — the shared template should cover it.

## 8. Pitfalls we hit — read this BEFORE starting

Numbered in the order they burned us during the codex probe build:

### 8.1 Don't add agent-specific columns unless the concept genuinely doesn't map

We added 4 codex-specific columns for the `/usage` panel (lifetime,
peak, streak, longest task) because that panel showed history not
quota. When we switched to `/status` (which does show quota), those 4
columns became dead. Now they sit as legacy nullable columns forever.

Cost: one round of API + frontend + serializer rework, plus permanent
schema baggage.

Prevention: **ask "does this field mean something claude could also
theoretically have?" before adding it.** If the answer is yes, use the
shared column. If the answer is no, re-check whether you're using the
right CLI command in the first place.

### 8.2 Don't parse reset timestamps into ISO

We were tempted to parse `"14:11 on 2 Aug"` into a proper datetime.
Bad idea — codex's local timezone isn't in the string, the year is
implicit, and DST wrap-around is a footgun. We only display the string
back to the user, so keeping the raw text is both simpler AND more
correct.

Prevention: **only structure data if you have a reason to compute on
it.** For display-only fields, raw strings win.

### 8.3 Don't trust sandbox isolation for auth

The dev sandbox exports `CODEX_HOME=/tmp/.../fake_codex/`. We assumed
this fully isolated codex from the user's real account. It doesn't —
codex reads auth from `~/.codex/auth.json` regardless of `CODEX_HOME`.

Prevention: **before believing "this test uses a sandbox account",
explicitly probe:** log the account name, verify it matches what you
expected. Same applies to Gemini — check where its auth actually lives
before assuming the sandbox env var covers it.

### 8.4 Don't drop columns just because they're unused

We considered adding a downgrade migration for the 4 legacy codex_*
columns. Cost: one more migration to review, one more roll-forward
path for existing installs, and any test that inserted those columns
now needs updating. Benefit: 4 fewer nullable columns.

Prevention: **columns are cheap; migrations are load-bearing.** Mark
unused columns as legacy in the model docstring. Delete on the next
schema-cleanup pass (which happens ~yearly, not per-feature).

### 8.5 Don't v-if/v-else two separate template blocks

Initial codex probe had two branches: a `<div v-if="!isCodexUsage">`
claude-shape block and a `<div v-else>` codex-tiles block. When the
UX pivoted to unify them, we had to delete the entire tile block and
re-write. If we'd used shared markup with `v-if` gates from the start,
the pivot would have been a two-line edit.

Prevention: **shared shell + `v-if` gates > sibling branches** when
adding a variant. The extra 5 lines of markup upfront save a rewrite
later.

### 8.6 Capture the real pane before writing the parser

We wrote the first codex parser from the initial `/usage` docstring
sample. It worked. Then when we switched to `/status`, we did the same
— but this time nothing worked, because our assumed format didn't
match reality. Wasted 20 minutes debugging the parser before realising
we hadn't actually seen `/status`'s post-refresh output.

Prevention: **capture > guess.** Write the throwaway pexpect script
in §2, run it against real CLI, THEN write the parser. Non-negotiable.

### 8.7 CLI protocol quirks are invisible until they burn you

Codex `/status` needs TWO calls — first is a "please refresh" marker,
second is the real numbers. If you only send it once you get useless
data with no obvious error. Codex `/usage` opens a picker that needs
Enter to select. Claude `/usage` needs char-by-char typing because Ink
drops bulk sends.

Prevention: **document every protocol quirk in the probe module
docstring.** Future-you (and the next person to debug it) will thank
you. See `codex_usage_probe.py` header for the pattern.

---

## Appendix: reference implementation diff

The `/status` refactor commit (`f557327`) is the reference pattern for
"add a new probe that fits the shared shape". Files touched:

- `backend/csm/modules/token/codex_usage_probe.py` — new probe module
- `backend/csm/modules/token/usage_scheduler.py` — added `_probe_*_once` branch
- `backend/csm/models/usage_snapshot.py` — docstring only (columns already existed)
- `backend/csm/api/tokens.py` — serializer + docstring updates
- `frontend/src/views/Tokens.vue` — shared bar layout, `v-if` gate, header derivation
- `tests/unit/test_codex_usage_probe_parser.py` — parser fixtures

Total: ~350 LOC insertions, ~370 deletions (net negative because we
retired the tile branch). Adding a green-field probe (no retirement)
would be closer to ~250 insertions.

Bounded costs like this only hold if you follow the shared-shape rule
in §3. Adapter-specific columns compound quadratically with adapter
count.
