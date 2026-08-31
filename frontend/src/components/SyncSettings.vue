<script setup lang="ts">
/**
 * Unified Multi-agent Sync console (v1 + v2 merged into one Settings panel).
 *
 * Historically there were two Sync UIs: `SyncSettings.vue` (v1, rule-driven
 * drift poller — memory/mcp/skills/drift/activity tabs) and a standalone
 * `/sync` route (v2, agent-driven — resources/pending/config/ledger tabs).
 * They covered the same domain from two lenses and were confusing. This
 * file replaces both.
 *
 * Four tabs, ordered by expected use frequency:
 *
 *   1. Resources — matrix of CSM rows × enrolled agents + hash cell.
 *                  Type filter (all / instruction / mcp / skill) replaces
 *                  the old memory / mcp / skills sub-tabs. Read-only:
 *                  no inline body editor (deferred — see known_issues).
 *   2. Pending   — merged queue of v1 DriftRecord (rule-detected) + v2
 *                  pending_decision (agent-detected). Source badge on
 *                  each row; user resolves with take_agent / keep_diverged
 *                  / dismiss (v2) or mark-resolved (v1).
 *   3. Config    — per-module sync_mode toggle (lock ↔ agent, direct
 *                  effect), tick_interval_hours, enrollment matrix,
 *                  Run sync now, policy prompt editor, recent runs table.
 *   4. Activity  — cross-source timeline (v1 activity + v2 agent_runs +
 *                  fanout_ledger non-done rows). Source badge + status
 *                  filter + inline retry/dismiss for ledger rows.
 *
 * Component is embedded inside Settings.vue at `section === 'sync'`.
 * `embedded` prop reserves the option for a standalone remount (not used
 * today; kept for shape compatibility with old callers). All state is
 * per-mount; no Pinia store — sync is not a hot-path feature.
 */
import { apiErrorMessage } from '../lib/apiError'
import { computed, onMounted, ref, watch } from 'vue'
import {
  syncApi,
  type ActivityRow,
  type AgentRunRow,
  type DriftRow,
  type FanoutLedgerRow,
  type Instruction,
  type McpServer,
  type PendingDecisionRow,
  type PolicyRow,
  type AvailableSkill,
  type Skill,
  type SyncConfigEntry,
} from '../api/sync'
import { listBackends } from '../api/backends'

withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })

type Tab = 'sync' | 'schedule' | 'conflicts' | 'log'
const tab = ref<Tab>('sync')

// Sync tab: direction-first migration. Pick source → target, then the lists
// show the SOURCE agent's resources for the chosen type.
type SyncType = 'memory' | 'skills' | 'mcp'
const syncType = ref<SyncType>('memory')
const syncSource = ref('')
const syncTarget = ref('')

// ---- shared state --------------------------------------------------------

const configs = ref<Array<{ module: string; entry: SyncConfigEntry | null }>>([])
const availableSkills = ref<AvailableSkill[]>([])
const knownAgents = ref<string[]>([])
const instructions = ref<Instruction[]>([])
const mcpServers = ref<McpServer[]>([])
const skills = ref<Skill[]>([])
const pendingV1 = ref<DriftRow[]>([])
const pendingV2 = ref<PendingDecisionRow[]>([])
const ledger = ref<FanoutLedgerRow[]>([])
const runs = ref<AgentRunRow[]>([])
const activity = ref<ActivityRow[]>([])
const policy = ref<PolicyRow | null>(null)

const loading = ref(false)
const runInFlight = ref(false)
const runPhase = ref<string>('')
const lastRunResult = ref<string>('')

// Friendly labels for the live tick phase. The decide step spawns a real
// agent session, so "deciding" is where most of the wall-clock goes.
const PHASE_LABELS: Record<string, string> = {
  collecting: 'collecting state…',
  deciding: 'agent deciding…',
  applying: 'applying decisions…',
  done: 'done',
}
const errorBanner = ref<string>('')

// Loading is split so entering the page only fetches what the visible tab
// needs — the old all-in-one refreshAll fired ~12 calls up front (slow over a
// tunnel). Core = config + agents + pending (for the conflict banner). Log data
// and the policy prompt load lazily when their surface is opened.

async function loadCore() {
  loading.value = true
  errorBanner.value = ''
  try {
    const [cfg, backends, pv2] = await Promise.all([
      syncApi.listConfig(),
      listBackends(),
      syncApi.listPendingDecisions('pending', 200),
    ])
    configs.value = cfg.config
    knownAgents.value = backends.map(b => b.name)
    if (!syncSource.value && knownAgents.value.length) syncSource.value = knownAgents.value[0]
    if (!syncTarget.value)
      syncTarget.value = knownAgents.value.find(a => a !== syncSource.value) || ''
    pendingV2.value = pv2
    loadAvailableSkills()  // non-blocking; names only
  } catch (e) {
    errorBanner.value = `load failed: ${apiErrorMessage(e)}`
  } finally {
    loading.value = false
  }
}

// Alias kept so post-action callers (migrate / resolve / toggle) reload core.
async function refreshAll() {
  await loadCore()
  if (tab.value === 'log') await loadLog()
}

const logLoaded = ref(false)
async function loadLog() {
  try {
    const [led, r, act] = await Promise.all([
      syncApi.listFanoutLedger('non_done', 200),
      syncApi.listAgentRuns(30),
      syncApi.listActivity({ limit: 200 }),
    ])
    ledger.value = led
    runs.value = r
    activity.value = act
    logLoaded.value = true
  } catch { /* leave stale; non-critical */ }
}

const policyLoaded = ref(false)
async function ensurePolicyLoaded() {
  if (policyLoaded.value) return
  try {
    policy.value = await syncApi.getPolicy()
    loadPolicyDraft()
    policyLoaded.value = true
  } catch { /* ignore */ }
}

// Lazy-load per tab: only fetch a tab's data the first time it's opened.
watch(tab, (t) => {
  if (t === 'log' && !logLoaded.value) loadLog()
})

// The union of every agent name we've seen: adapter registry ∪ agents already
// present in some module's enrolled list. Matters when an agent was
// unregistered but still holds hash entries on old rows.
const enrolledAgents = computed<string[]>(() => {
  const s = new Set<string>(knownAgents.value)
  for (const c of configs.value) {
    for (const a of c.entry?.enrolled_agents || []) s.add(a)
  }
  return Array.from(s).sort()
})

// ---- Conflicts tab (merged v1 drift + v2 pending) -----------------------

interface UnifiedPendingRow {
  key: string
  source: 'rule' | 'agent'
  id: number
  ts: string
  resource_type: string
  resource_id: number | null
  summary: string
  raw: DriftRow | PendingDecisionRow
}

const unifiedPending = computed<UnifiedPendingRow[]>(() => {
  // v1 `rule` drift is retired — conflicts come only from the sync agent now.
  const out: UnifiedPendingRow[] = []
  for (const p of pendingV2.value) {
    const agents = Object.keys(p.candidates_json || {}).filter(k => k !== 'csm').join(', ')
    out.push({
      key: `agent:${p.id}`, source: 'agent',
      id: p.id, ts: p.ts,
      resource_type: p.resource_type, resource_id: p.resource_id,
      summary: `agent proposed conflict · candidates: ${agents || '—'}`,
      raw: p,
    })
  }
  out.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''))
  return out
})

const totalPending = computed(() => unifiedPending.value.length)

const pendingBusy = ref<Record<string, boolean>>({})

async function resolveV2Pending(row: UnifiedPendingRow, resolution: string) {
  const p = row.raw as PendingDecisionRow
  pendingBusy.value[row.key] = true
  try {
    await syncApi.resolvePendingDecision(p.id, resolution)
    await refreshAll()
  } catch (e) {
    errorBanner.value = `resolve failed: ${apiErrorMessage(e)}`
  } finally {
    pendingBusy.value[row.key] = false
  }
}

// Per-candidate line diff: for each side, the non-blank lines that appear ONLY
// there (not on any other side). Turns two walls of text into "here's what
// actually differs". Includes the 'csm' side so agent-vs-CSM conflicts show.
interface ConflictSide { agent: string; only: string[]; total: number; body: string }
function conflictDiff(p: PendingDecisionRow): ConflictSide[] {
  const cj = p.candidates_json || {}
  const agents = Object.keys(cj)
  const lineSets = agents.map(a => new Set((cj[a] || '').split('\n')))
  return agents.map((a, i) => {
    const mine = (cj[a] || '').split('\n')
    const others = new Set<string>()
    lineSets.forEach((s, j) => { if (j !== i) s.forEach(l => others.add(l)) })
    return {
      agent: a,
      only: mine.filter(l => l.trim() && !others.has(l)),
      total: mine.length,
      body: cj[a] || '',
    }
  })
}

// ---- tab 3: Config -------------------------------------------------------

async function saveConfigPatch(module: string, patch: Partial<SyncConfigEntry>) {
  try {
    await syncApi.updateConfig(module, patch as any)
    await refreshAll()
  } catch (e) {
    errorBanner.value = `save config failed: ${apiErrorMessage(e)}`
  }
}

// ---- skills allowlist (which skills to sync) ----------------------------

async function loadAvailableSkills() {
  try {
    availableSkills.value = await syncApi.availableSkills()
  } catch {
    availableSkills.value = []
  }
}

function skillsAllowlist(): string[] | null {
  return configs.value.find(c => c.module === 'skills')?.entry?.resource_allowlist ?? null
}

function isSkillSelected(name: string): boolean {
  const al = skillsAllowlist()
  return al !== null && al.includes(name)
}

async function toggleSkillSelected(name: string, checked: boolean) {
  const next = new Set(skillsAllowlist() ?? [])
  if (checked) next.add(name)
  else next.delete(name)
  await saveConfigPatch('skills', { resource_allowlist: Array.from(next) } as any)
}

async function clearSkillFilter() {
  // null = no filter (every skill syncs).
  await saveConfigPatch('skills', { resource_allowlist: null } as any)
}

async function selectMyOwnSkills() {
  // Quick-pick: the source agent's own (non-marketplace) skills.
  const own = sourceSkills.value
    .filter(s => s.source_hint === 'user')
    .map(s => s.name)
  await saveConfigPatch('skills', { resource_allowlist: own } as any)
}

// ---- bundle size (files beside SKILL.md) --------------------------------

function bundleSizeOn(s: AvailableSkill, agent: string | null): number {
  if (!agent) return 0
  return s.file_count?.[agent] ?? 0
}

function bundleSize(s: AvailableSkill): number {
  return bundleSizeOn(s, syncSource.value)
}

function isBundleShort(s: AvailableSkill): boolean {
  // Target has the skill but fewer files than the source — i.e. a copy that
  // looks present and isn't usable. Precisely the failure this surfaces.
  if (!syncTarget.value || !s.agents.includes(syncTarget.value)) return false
  return bundleSizeOn(s, syncTarget.value) < bundleSize(s)
}

// ---- repair: re-read bundles off the source and re-push ------------------

const reingestBusy = ref(false)
const reingestMsg = ref('')

async function runReingest() {
  if (!syncSource.value) return
  reingestBusy.value = true
  reingestMsg.value = ''
  try {
    const r = await syncApi.reingestSkills(syncSource.value)
    const repaired = r.items.filter(i => i.action === 'reingested')
    const files = repaired.reduce((n, i) => n + (i.file_count ?? 0), 0)
    const problems = r.items.filter(i => i.action === 'error' || i.action === 'skipped')
    reingestMsg.value =
      `Repaired ${repaired.length} skill(s), ${files} bundle file(s)` +
      (problems.length ? ` — ${problems.length} needed attention: ` +
        problems.map(p => `${p.name} (${p.detail ?? p.action})`).join('; ') : '')
    await loadAvailableSkills()
    await refreshAll()
  } catch (e: any) {
    reingestMsg.value = `Repair failed: ${apiErrorMessage(e)}`
  } finally {
    reingestBusy.value = false
  }
}

// ---- deterministic (LLM-free) migrate -----------------------------------

const migrateBusy = ref(false)
const migrateResult = ref('')

function swapDirection() {
  const s = syncSource.value
  syncSource.value = syncTarget.value
  syncTarget.value = s
  migrateResult.value = ''
}

// Skills present on the SOURCE agent — the list follows the chosen direction.
const sourceSkills = computed(() =>
  availableSkills.value.filter(s => !syncSource.value || s.agents.includes(syncSource.value)),
)

async function runMigrate() {
  if (!syncSource.value || !syncTarget.value || syncSource.value === syncTarget.value) {
    migrateResult.value = 'pick distinct source and target agents'
    return
  }
  if (syncType.value === 'mcp') {
    migrateResult.value = 'mcp migrate is not supported yet (define servers in CSM directly)'
    return
  }
  migrateBusy.value = true
  migrateResult.value = ''
  try {
    // skills: migrate only the selected names (allowlist); memory: whole file.
    const names = syncType.value === 'skills' ? (skillsAllowlist() ?? undefined) : undefined
    const res = await syncApi.migrate(syncType.value, {
      source: syncSource.value, target: syncTarget.value, names,
    })
    const items = res.items || []
    const applied = items.filter(r => r.action !== 'skipped' && r.action !== 'unsupported').length
    migrateResult.value = `migrated ${applied} ${syncType.value} item(s): ${syncSource.value} → ${syncTarget.value}`
    await refreshAll()
  } catch (e) {
    migrateResult.value = `error: ${apiErrorMessage(e)}`
  } finally {
    migrateBusy.value = false
  }
}

// ---- global auto-sync: one switch + one interval for ALL modules ---------

const GLOBAL_DEFAULT_HOURS = 6
const globalIntervalHours = ref(GLOBAL_DEFAULT_HOURS)

// "On" = any module has a positive interval (auto-tick scheduled).
const autoSyncOn = computed(() =>
  configs.value.some(c =>
    (c.entry?.tick_interval_hours || 0) > 0 || (c.entry?.tick_interval_minutes || 0) > 0,
  ),
)

// Reflect whatever interval is set back into the input.
watch(configs, () => {
  const h = configs.value.map(c => c.entry?.tick_interval_hours || 0).find(v => v > 0)
  if (h) globalIntervalHours.value = h
})

async function applyToAllModules(hours: number) {
  await Promise.all(configs.value.map(c =>
    syncApi.updateConfig(c.module, {
      tick_interval_hours: hours, tick_interval_minutes: 0, enabled: true,
    }),
  ))
  await loadCore()
}

async function toggleAutoSync() {
  if (autoSyncOn.value) {
    await applyToAllModules(0)  // turn off (manual only)
  } else {
    await applyToAllModules(globalIntervalHours.value || GLOBAL_DEFAULT_HOURS)
    kickTick()  // sync once right away, then on the interval
  }
}

async function setGlobalInterval(h: number) {
  if (Number.isNaN(h) || h < 1) { h = 1 }
  globalIntervalHours.value = h
  if (autoSyncOn.value) await applyToAllModules(h)
}

// Fire one agent tick in the background (on turn-on). Progress shows in Log.
async function kickTick() {
  try { await syncApi.agentTick() } catch { /* non-blocking; surfaced in Log */ }
}

async function toggleEnrollment(module: string, agent: string, checked: boolean) {
  const cur = configs.value.find(c => c.module === module)?.entry
  const enrolled = new Set(cur?.enrolled_agents || [])
  if (checked) enrolled.add(agent); else enrolled.delete(agent)
  await saveConfigPatch(module, { enrolled_agents: Array.from(enrolled) })
}

async function unenroll(module: string, agent: string) {
  if (!confirm(`Unenroll ${agent} from ${module}? All hash keys for ${agent} on ${module} rows will be stripped.`)) return
  try {
    await syncApi.unenrollAgent(module, agent)
    await refreshAll()
  } catch (e) {
    errorBanner.value = `unenroll failed: ${apiErrorMessage(e)}`
  }
}

async function runNow() {
  runInFlight.value = true
  runPhase.value = 'starting…'
  lastRunResult.value = ''
  try {
    // The tick now runs in the background (the decide step is a real agent
    // session, tens of seconds). agentTick returns immediately with a run_id
    // to poll for live phase — no more blocking the 30s HTTP timeout.
    const res = await syncApi.agentTick()
    if (res.run_id == null) {
      // Backend couldn't stamp the row in time; give it a moment then refresh.
      runPhase.value = 'running…'
      await new Promise(r => setTimeout(r, 2000))
      await refreshAll()
      lastRunResult.value = 'tick started (run id unavailable) — see Recent runs'
      return
    }
    const runId = res.run_id
    let finalRow: any = null
    // Poll live_phase until the orchestrator clears it (run finished). Cap at
    // ~200s so a hung session can't spin forever; the server force-stops its
    // own session at sync_decide_timeout anyway.
    for (let i = 0; i < 140; i++) {
      let row: any
      try {
        row = await syncApi.getAgentRun(runId)
      } catch {
        await new Promise(r => setTimeout(r, 1500))
        continue
      }
      if (row.live_phase) {
        runPhase.value = PHASE_LABELS[row.live_phase] || row.live_phase
      } else {
        // live_phase null → run is no longer current, i.e. finished.
        finalRow = row
        break
      }
      await new Promise(r => setTimeout(r, 1500))
    }
    if (finalRow) {
      const err = finalRow.error
      if (err) {
        lastRunResult.value = `run #${runId} finished with error: ${err}`
      } else {
        const applied = finalRow.applied_count ?? 0
        const decided = finalRow.decisions_count ?? 0
        lastRunResult.value = `run #${runId} done — ${decided} decisions, ${applied} applied`
      }
    } else {
      lastRunResult.value = `run #${runId} still running — check Recent runs`
    }
    await refreshAll()
    if (unifiedPending.value.length) tab.value = 'conflicts'
  } catch (e) {
    lastRunResult.value = `error: ${apiErrorMessage(e)}`
  } finally {
    runInFlight.value = false
    runPhase.value = ''
  }
}

// policy editor
const policyDraft = ref('')
const policyDirty = computed(() => policyDraft.value.trim() !== (policy.value?.prompt || '').trim())

function loadPolicyDraft() { policyDraft.value = policy.value?.prompt || '' }

async function savePolicy() {
  if (!policyDirty.value) return
  try {
    const r = await syncApi.updatePolicy(policyDraft.value)
    policy.value = r
    loadPolicyDraft()
  } catch (e) {
    errorBanner.value = `save policy failed: ${apiErrorMessage(e)}`
  }
}

async function resetPolicy() {
  if (!confirm('Reset system prompt to shipped default? This overwrites your edits.')) return
  try {
    const r = await syncApi.resetPolicy()
    policy.value = r
    loadPolicyDraft()
  } catch (e) {
    errorBanner.value = `reset failed: ${apiErrorMessage(e)}`
  }
}

// ---- tab 4: Activity (merged) -------------------------------------------

interface UnifiedActivityRow {
  key: string
  source: 'v1-poll' | 'v2-tick' | 'fanout'
  ts: string
  label: string
  status: string
  duration_ms: number | null
  error: string | null
  raw: ActivityRow | AgentRunRow | FanoutLedgerRow
}

type ActivityFilter = 'all' | 'errors' | 'pending-retries'
const activityFilter = ref<ActivityFilter>('all')

const unifiedActivity = computed<UnifiedActivityRow[]>(() => {
  const out: UnifiedActivityRow[] = []
  for (const a of activity.value) {
    out.push({
      key: `act:${a.id}`, source: 'v1-poll',
      ts: a.ts, label: `${a.module} · ${a.agent} · ${a.action}${a.resource_id ? ` #${a.resource_id}` : ''}`,
      status: a.status, duration_ms: a.duration_ms, error: null, raw: a,
    })
  }
  for (const r of runs.value) {
    out.push({
      key: `run:${r.id}`, source: 'v2-tick',
      ts: r.ts, label: `run #${r.id} ${r.trigger}${r.parent_run_id ? ` ←${r.parent_run_id}` : ''} · applied ${r.applied_count ?? '—'} rejected ${r.rejected_count ?? '—'}`,
      status: r.error ? 'error' : (r.phase || 'ok'),
      duration_ms: r.duration_ms, error: r.error, raw: r,
    })
  }
  for (const l of ledger.value) {
    out.push({
      key: `led:${l.id}`, source: 'fanout',
      ts: l.attempted_at || l.ts,
      label: `${l.resource_type}:${l.resource_id} → ${(l.target_agents || []).join(', ') || '—'} · attempts ${l.attempt_count}`,
      status: l.status, duration_ms: null, error: null, raw: l,
    })
  }
  out.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''))
  const filtered = out.filter(r => {
    if (activityFilter.value === 'all') return true
    if (activityFilter.value === 'errors') {
      return r.status === 'error' || r.status === 'timeout' || r.status === 'failed_terminal' || !!r.error
    }
    // pending-retries: fanout ledger non-done rows only
    return r.source === 'fanout' && r.status !== 'done'
  })
  return filtered
})

const ledgerBusy = ref<Record<number, boolean>>({})

async function retryLedgerRow(id: number) {
  ledgerBusy.value[id] = true
  try {
    await syncApi.retryLedger(id)
    await refreshAll()
  } catch (e) {
    errorBanner.value = `retry failed: ${apiErrorMessage(e)}`
  } finally {
    ledgerBusy.value[id] = false
  }
}

async function dismissLedgerRow(id: number) {
  ledgerBusy.value[id] = true
  try {
    await syncApi.dismissLedger(id)
    await refreshAll()
  } catch (e) {
    errorBanner.value = `dismiss failed: ${apiErrorMessage(e)}`
  } finally {
    ledgerBusy.value[id] = false
  }
}

// ---- lifecycle -----------------------------------------------------------

onMounted(() => {
  loadCore()  // fast: only config + agents + pending
})
</script>

<template>
  <div class="sync-page" :class="{ embedded }">
    <div class="toolbar">
      <div class="tabs">
        <button :class="{ active: tab === 'sync' }" @click="tab = 'sync'">Sync</button>
        <button :class="{ active: tab === 'conflicts' }" @click="tab = 'conflicts'">
          Conflicts<span v-if="totalPending" class="pill danger">{{ totalPending }}</span>
        </button>
        <button :class="{ active: tab === 'log' }" @click="tab = 'log'">Log</button>
      </div>
    </div>

    <div v-if="errorBanner" class="banner err">{{ errorBanner }}</div>

    <!-- ===== Sync ===== -->
    <div v-if="tab === 'sync'" class="pane">
      <div v-if="totalPending" class="banner warn conflict-banner">
        ⚠ {{ totalPending }} conflict(s) need your decision
        <button class="sm" @click="tab = 'conflicts'">Resolve →</button>
      </div>

      <!-- global auto-sync switch -->
      <div class="autosync-bar" :class="{ on: autoSyncOn }">
        <button class="toggle" :class="{ on: autoSyncOn }" @click="toggleAutoSync">
          <span class="dot"></span>{{ autoSyncOn ? 'Auto-sync ON' : 'Auto-sync OFF' }}
        </button>
        <span class="autosync-detail">
          every
          <input type="number" min="1" step="1" class="tick-input"
            :value="globalIntervalHours"
            @change="setGlobalInterval(Number(($event.target as HTMLInputElement).value))" />
          hours
        </span>
        <span class="hint tiny">
          {{ autoSyncOn ? 'all enrolled modules sync automatically' : 'off — use the manual migrate below to seed, or turn on to keep agents in sync' }}
        </span>
      </div>

      <hr class="soft" />
      <p class="hint tiny">Manual seed / one-off copy (no schedule needed):</p>

      <div class="dir-bar">
        <label>From
          <select v-model="syncSource">
            <option value="">source…</option>
            <option v-for="a in knownAgents" :key="a" :value="a">{{ a }}</option>
          </select>
        </label>
        <button class="sm swap" @click="swapDirection" title="swap direction">⇄</button>
        <label>To
          <select v-model="syncTarget">
            <option value="">target…</option>
            <option v-for="a in knownAgents" :key="a" :value="a">{{ a }}</option>
          </select>
        </label>
      </div>

      <div class="type-toggle">
        <button :class="{ active: syncType === 'memory' }" @click="syncType = 'memory'">Memory (md)</button>
        <button :class="{ active: syncType === 'skills' }" @click="syncType = 'skills'">Skills</button>
        <button :class="{ active: syncType === 'mcp' }" @click="syncType = 'mcp'">MCP</button>
      </div>

      <!-- Memory -->
      <section v-if="syncType === 'memory'">
        <p class="hint">
          Shares <strong>{{ syncSource || 'source' }}</strong>'s global memory file
          (e.g. <code>~/.claude/CLAUDE.md</code>) into
          <strong>{{ syncTarget || 'target' }}</strong> as a CSM-managed marker
          block. Your hand-written content on the target stays put — only the
          block is inserted / updated.
        </p>
        <button class="primary" @click="runMigrate" :disabled="migrateBusy">
          {{ migrateBusy ? '…' : `Migrate memory: ${syncSource || '?'} → ${syncTarget || '?'}` }}
        </button>
      </section>

      <!-- Skills -->
      <section v-else-if="syncType === 'skills'">
        <p class="hint">
          Skills on <strong>{{ syncSource || 'source' }}</strong> — pick which to
          copy into <strong>{{ syncTarget || 'target' }}</strong>. The selection is
          also the scope for scheduled agent sync.
        </p>
        <div class="skill-filter-bar">
          <span v-if="skillsAllowlist() === null" class="pill warn">no filter · all sync</span>
          <span v-else class="pill ok">{{ skillsAllowlist()!.length }} selected</span>
          <button class="sm" @click="selectMyOwnSkills">Select my own ({{ sourceSkills.filter(s => s.source_hint === 'user').length }})</button>
          <button class="sm" @click="clearSkillFilter" :disabled="skillsAllowlist() === null">Clear</button>
          <button class="sm" @click="loadAvailableSkills">↻</button>
        </div>
        <p class="hint tiny">
          <em>marketplace</em> = installed (has <code>version:</code>); the rest are yours. Heuristic.
        </p>
        <ul v-if="sourceSkills.length" class="skill-picker">
          <li v-for="s in sourceSkills" :key="s.name">
            <label class="skill-row">
              <input type="checkbox" :checked="isSkillSelected(s.name)"
                @change="toggleSkillSelected(s.name, ($event.target as HTMLInputElement).checked)" />
              <span class="mono skill-name">{{ s.name }}</span>
              <span v-if="bundleSize(s) > 0" class="pill files">+{{ bundleSize(s) }} file{{ bundleSize(s) > 1 ? 's' : '' }}</span>
              <span v-if="s.source_hint === 'marketplace'" class="pill mkt">marketplace</span>
              <span v-if="syncTarget && s.agents.includes(syncTarget)" class="pill ok">on {{ syncTarget }} ✓</span>
              <!-- The exact shape of the bug this feature fixes: target has
                   the skill, but fewer files than the source. -->
              <span v-if="isBundleShort(s)" class="pill warn"
                :title="`${syncTarget} has ${bundleSizeOn(s, syncTarget)} of ${bundleSize(s)} files`">
                incomplete on {{ syncTarget }}
              </span>
            </label>
            <div v-if="s.description" class="skill-desc">{{ s.description }}</div>
          </li>
        </ul>
        <p v-else class="hint">No skills on {{ syncSource || 'the source agent' }}.</p>
        <div class="skill-actions">
          <button class="primary" @click="runMigrate" :disabled="migrateBusy || !sourceSkills.length">
            {{ migrateBusy ? '…' : `Migrate selected → ${syncTarget || '?'}` }}
          </button>
          <button class="sm" @click="runReingest" :disabled="reingestBusy || !syncSource"
            title="Re-read every known skill's files from the source and re-push them. Repairs skills that were synced before bundle support existed.">
            {{ reingestBusy ? '…' : `Repair bundles from ${syncSource || '?'}` }}
          </button>
        </div>
        <p v-if="reingestMsg" class="hint tiny">{{ reingestMsg }}</p>
      </section>

      <!-- MCP -->
      <section v-else>
        <p class="hint">
          MCP migration isn't supported yet (the CLI can't expose full server
          definitions). Define MCP servers in CSM directly and they fan out via
          scheduled sync.
        </p>
      </section>

      <div v-if="migrateResult" class="banner">{{ migrateResult }}</div>

      <details class="policy-details" @toggle="ensurePolicyLoaded">
        <summary class="serif">Agent system prompt (advanced)</summary>
        <p class="hint">
          The sync agent's system prompt (from <code>sync_policy(id=1)</code>).
          Reset restores the shipped default.
          Hash: <code class="mono">{{ policy?.prompt_hash?.slice(0, 12) || '…' }}</code>
        </p>
        <textarea v-model="policyDraft" rows="14" spellcheck="false"></textarea>
        <div class="policy-actions">
          <button class="primary" :disabled="!policyDirty" @click="savePolicy">Save prompt</button>
          <button @click="resetPolicy">Reset to default</button>
          <span v-if="policyDirty" class="hint">unsaved changes</span>
        </div>
      </details>
    </div>

    <!-- ===== Conflicts (was Pending) ===== -->
    <div v-if="tab === 'conflicts'" class="pane">
      <p class="hint">
        Two sides diverged and the sync agent couldn't safely pick one — choose
        which becomes canonical.
      </p>
      <div v-if="!unifiedPending.length" class="empty">No conflicts — everything's in agreement. 🎉</div>
      <div v-for="row in unifiedPending" :key="row.key" class="pending-card">
        <div class="pending-head">
          <span class="mono">{{ row.resource_type }}{{ row.resource_id != null ? ' · id=' + row.resource_id : ' · (new)' }}</span>
          <span class="hint tiny">{{ row.ts }}</span>
        </div>
        <div class="pending-body">
          <div class="summary">{{ row.summary }}</div>
          <!-- v2 candidate diffs: show only what's unique to each side -->
          <template v-if="row.source === 'agent'">
            <p class="hint tiny">Pick which side becomes canonical. Each box shows the lines <strong>unique to that side</strong>; full content is collapsed.</p>
            <div class="conflict-diff">
              <div v-for="d in conflictDiff(row.raw as PendingDecisionRow)" :key="d.agent" class="diff-side">
                <div class="cand-head">
                  <strong class="mono">{{ d.agent === 'csm' ? 'CSM (current)' : d.agent }}</strong>
                  <span class="hint tiny">{{ d.only.length }} unique · {{ d.total }} lines</span>
                  <button
                    v-if="d.agent !== 'csm'"
                    class="primary sm"
                    :disabled="pendingBusy[row.key]"
                    @click="resolveV2Pending(row, `take_agent:${d.agent}`)"
                  >Take {{ d.agent }}</button>
                </div>
                <pre v-if="d.only.length" class="diff-lines">{{ d.only.slice(0, 30).join('\n') }}{{ d.only.length > 30 ? `\n… +${d.only.length - 30} more` : '' }}</pre>
                <p v-else class="hint tiny">nothing unique to this side</p>
                <details class="cand-full">
                  <summary class="hint tiny">full content ({{ d.total }} lines)</summary>
                  <pre class="cand-body">{{ d.body.slice(0, 6000) }}</pre>
                </details>
              </div>
            </div>
            <div v-if="(row.raw as PendingDecisionRow).apply_error" class="err-inline">
              {{ (row.raw as PendingDecisionRow).apply_error }}
            </div>
          </template>
        </div>
        <div class="pending-actions">
          <button :disabled="pendingBusy[row.key]" @click="resolveV2Pending(row, 'keep_diverged')">Keep diverged</button>
          <button :disabled="pendingBusy[row.key]" @click="resolveV2Pending(row, 'dismiss')">Dismiss</button>
        </div>
      </div>
    </div>


    <!-- ===== Log (was Activity) ===== -->
    <div v-if="tab === 'log'" class="pane">
      <div class="row-controls">
        <label>
          Show:
          <select v-model="activityFilter">
            <option value="all">all</option>
            <option value="errors">errors only</option>
            <option value="pending-retries">pending retries</option>
          </select>
        </label>
        <span class="hint">
          <span class="src-badge src-v1-poll">v1-poll</span>
          <span class="src-badge src-v2-tick">v2-tick</span>
          <span class="src-badge src-fanout">fanout</span>
          — merged chronologically (newest first).
        </span>
      </div>
      <table v-if="unifiedActivity.length" class="listing">
        <thead>
          <tr><th>Time</th><th>Source</th><th>Event</th><th>Status</th><th>Dur (ms)</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="row in unifiedActivity" :key="row.key">
            <td class="mono tiny">{{ row.ts }}</td>
            <td><span :class="'src-badge src-' + row.source">{{ row.source }}</span></td>
            <td>{{ row.label }}</td>
            <td>
              <span :class="'status-badge status-' + row.status">{{ row.status }}</span>
              <span v-if="row.error" class="err-inline"> · {{ row.error }}</span>
            </td>
            <td>{{ row.duration_ms ?? '—' }}</td>
            <td>
              <template v-if="row.source === 'fanout'">
                <button
                  v-if="(row.raw as FanoutLedgerRow).status === 'failed_terminal'"
                  class="sm"
                  :disabled="ledgerBusy[(row.raw as FanoutLedgerRow).id]"
                  @click="retryLedgerRow((row.raw as FanoutLedgerRow).id)"
                >Retry</button>
                <button
                  v-if="(row.raw as FanoutLedgerRow).status !== 'done'"
                  class="sm"
                  :disabled="ledgerBusy[(row.raw as FanoutLedgerRow).id]"
                  @click="dismissLedgerRow((row.raw as FanoutLedgerRow).id)"
                >Dismiss</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">No activity matches the filter.</div>
    </div>
  </div>
</template>

<style scoped>
.sync-page { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.sync-page.embedded { height: auto; overflow: visible; }

.toolbar {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 0 12px;
  border-bottom: 1px solid var(--border);
}
.tabs { display: flex; gap: 4px; }
.tabs button {
  border: 1px solid transparent;
  background: transparent;
  padding: 6px 12px;
  border-radius: 6px;
  color: var(--ink-mute);
  cursor: pointer;
}
.tabs button.active {
  background: var(--card);
  border-color: var(--border);
  color: var(--ink);
}
.pill {
  margin-left: 6px;
  background: var(--ink-mute);
  color: var(--card);
  border-radius: 10px;
  padding: 1px 6px;
  font-size: 11px;
}
.pill.danger { background: var(--pastel-red-fg); }
.pill.warn { background: var(--pastel-amber-fg, #b7791f); }
.pill.ok { background: var(--pastel-green-fg, #2f855a); }
.pill.mkt { background: var(--ink-mute, #888); font-size: 10px; padding: 0 5px; }
.pill.files { background: var(--pastel-blue-fg, #2b6cb0); font-size: 10px; padding: 0 5px; }

.skill-filter-bar { display: flex; align-items: center; gap: 8px; margin: 6px 0 10px; flex-wrap: wrap; }
.skill-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.migrate-bar { display: flex; align-items: center; gap: 8px; margin: 6px 0; flex-wrap: wrap; }
.interval-cell { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.interval-cell .tick-input { width: 52px; }
.autosync-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 12px 14px; border: 1px solid var(--line, #ddd); border-radius: 10px; margin-bottom: 4px; }
.autosync-bar.on { border-color: var(--pastel-green-fg, #2f855a); background: var(--pastel-green-bg, #f0fdf4); }
.autosync-bar .toggle { display: inline-flex; align-items: center; gap: 8px; font-weight: 700;
  padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line, #ccc); background: var(--surface-2, #eee); cursor: pointer; }
.autosync-bar .toggle.on { background: var(--pastel-green-fg, #2f855a); color: #fff; border-color: transparent; }
.autosync-bar .toggle .dot { width: 9px; height: 9px; border-radius: 50%; background: currentColor; opacity: .55; }
.autosync-bar .toggle.on .dot { opacity: 1; box-shadow: 0 0 0 3px rgba(255,255,255,.35); }
.autosync-detail { display: inline-flex; align-items: center; gap: 5px; }
.autosync-detail .tick-input { width: 56px; }
hr.soft { border: none; border-top: 1px solid var(--line-soft, #eee); margin: 14px 0 8px; }
.dir-bar { display: flex; align-items: center; gap: 10px; margin: 4px 0 12px; flex-wrap: wrap; font-weight: 600; }
.dir-bar select { font-weight: 400; }
.dir-bar .swap { font-size: 15px; }
.type-toggle { display: inline-flex; gap: 0; margin-bottom: 12px; border: 1px solid var(--line, #ddd); border-radius: 8px; overflow: hidden; }
.type-toggle button { border: none; background: transparent; padding: 6px 14px; cursor: pointer; border-right: 1px solid var(--line-soft, #eee); }
.type-toggle button:last-child { border-right: none; }
.type-toggle button.active { background: var(--primary, #1989fa); color: #fff; }
.conflict-banner { display: flex; align-items: center; gap: 10px; }
.banner.warn { background: var(--pastel-amber-bg, #fff8e1); color: var(--pastel-amber-fg, #b7791f); }
.policy-details { margin-top: 14px; }
.policy-details summary { cursor: pointer; font-size: 15px; margin-bottom: 8px; }
.conflict-diff { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin: 8px 0; }
.diff-side { border: 1px solid var(--line, #ddd); border-radius: 8px; padding: 8px 10px; }
.diff-side .cand-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.diff-lines { background: var(--pastel-amber-bg, #fff8e1); border-radius: 6px; padding: 6px 8px;
  font-family: 'Geist Mono', monospace; font-size: 11px; white-space: pre-wrap; word-break: break-word;
  max-height: 220px; overflow-y: auto; margin: 0; }
.cand-full { margin-top: 6px; }
.cand-full summary { cursor: pointer; }
.cand-full .cand-body { background: var(--surface-2, #f6f6f6); border-radius: 6px; padding: 6px 8px;
  font-size: 11px; white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; margin: 4px 0 0; }
.skill-picker { list-style: none; padding: 0; margin: 0; max-height: 320px; overflow-y: auto;
  border: 1px solid var(--line, #ddd); border-radius: 8px; }
.skill-picker li { padding: 6px 10px; border-bottom: 1px solid var(--line-soft, #eee); }
.skill-picker li:last-child { border-bottom: none; }
.skill-row { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.skill-name { font-weight: 600; }
.skill-agents { color: var(--ink-mute); font-size: 11px; }
.skill-desc { color: var(--ink-mute); font-size: 11px; margin: 2px 0 0 24px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.toolbar .right { margin-left: auto; display: flex; gap: 8px; }
.primary {
  background: var(--ink); color: var(--card);
  border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer;
}
button.sm { padding: 3px 8px; font-size: 12px; margin-right: 4px; }
button.danger { color: var(--pastel-red-fg); }

.banner {
  padding: 8px 12px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  margin: 8px 0;
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
}
.banner.err { border-color: var(--pastel-red-fg); color: var(--pastel-red-fg); }

.pane {
  flex: 1; overflow-y: auto;
  padding: 16px 0;
  display: flex; flex-direction: column; gap: 16px;
}
.sync-page.embedded .pane { overflow: visible; }
.row-controls {
  display: flex; align-items: center; gap: 16px;
  font-size: 13px;
}
.row-controls select {
  padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px;
  background: var(--card); color: var(--ink);
}
.hint { color: var(--ink-mute); font-size: 13px; }
.hint.tiny { font-size: 11px; }
.mono { font-family: 'Geist Mono', monospace; font-size: 12px; }
.mono.tiny { font-size: 11px; }
.empty {
  padding: 40px;
  text-align: center;
  color: var(--ink-mute);
  border: 1px dashed var(--border);
  border-radius: 6px;
}
.empty.small { padding: 20px; font-size: 13px; }
.serif { font-family: 'Newsreader', serif; font-weight: 500; margin: 0 0 8px; }

table {
  width: 100%; border-collapse: collapse;
  background: var(--card);
  border-radius: 6px;
  overflow: hidden;
}
th, td {
  padding: 6px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
th { background: var(--canvas); font-weight: 500; }
tr:last-child td { border-bottom: none; }

.cell {
  display: inline-block;
  min-width: 20px;
  padding: 2px 6px;
  border-radius: 3px;
  text-align: center;
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
}
.cell-ok { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.cell-diverged { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.cell-unknown { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.cell-unsupported { background: var(--canvas); color: var(--ink-mute); }
.cell-empty { color: var(--ink-mute); }

.pending-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.pending-head {
  display: flex; align-items: center; gap: 12px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}
.summary { font-size: 13px; margin-bottom: 8px; }
.candidate { margin: 8px 0; }
.cand-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 4px 0;
}
.cand-body {
  background: var(--canvas);
  padding: 8px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 11px;
  max-height: 200px;
  margin: 0;
}
.pending-actions {
  display: flex; gap: 8px;
  padding-top: 8px; border-top: 1px solid var(--border);
}

.err-inline {
  color: var(--pastel-red-fg);
  font-size: 12px;
  font-family: 'Geist Mono', monospace;
}
.origin { font-size: 11px; color: var(--ink-mute); }

/* config tab */
.tick-input { width: 64px; padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; background: var(--canvas); color: var(--ink); }
.agent-cb { display: inline-flex; align-items: center; gap: 4px; margin-right: 12px; font-size: 12px; }

textarea {
  width: 100%;
  padding: 8px;
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--canvas);
  color: var(--ink);
  resize: vertical;
}
.policy-actions {
  display: flex; gap: 8px; align-items: center;
  padding-top: 8px;
}

/* source badges — thin, uniform, color-coded */
.src-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
  border: 1px solid var(--border);
  background: var(--canvas);
  color: var(--ink-mute);
  margin-right: 6px;
}
.src-rule, .src-v1-poll { background: var(--pastel-yellow-bg, #fef3c7); color: var(--pastel-yellow-fg, #78350f); }
.src-agent, .src-v2-tick { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.src-fanout { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }

.status-badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
}
.status-ok, .status-done { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.status-error, .status-timeout, .status-failed_terminal { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.status-pending, .status-phase2_done { background: var(--pastel-yellow-bg, #fef3c7); color: var(--pastel-yellow-fg, #78350f); }
.status-unsupported, .status-skipped { background: var(--canvas); color: var(--ink-mute); }
</style>
