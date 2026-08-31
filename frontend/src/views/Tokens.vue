<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { tokensApi } from '../api/tokens'
import AgentAlertsPanel from '../components/token/AgentAlertsPanel.vue'
import AgentBadge from '../components/AgentBadge.vue'
import { useBackendsStore } from '../stores/backends'
import { usePreferencesStore } from '../stores/preferences'

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

type Win = '1h' | '5h' | '12h' | '24h' | '7d' | '30d'
const WIN: Record<Win, { hours: number; granularity: string; label: string }> = {
  '1h':  { hours: 1,   granularity: 'minute', label: '1h'  },
  '5h':  { hours: 5,   granularity: '5min',   label: '5h'  },
  '12h': { hours: 12,  granularity: '5min',   label: '12h' },
  '24h': { hours: 24,  granularity: '5min',   label: '24h' },
  '7d':  { hours: 168, granularity: 'hour',   label: '7d'  },
  '30d': { hours: 720, granularity: 'day',    label: '30d' },
}

const win = ref<Win>('24h')
const projectSort = ref<'total_tokens' | 'msg_count' | 'cost'>('total_tokens')

// Model filter: multi-select, persisted in localStorage. Empty = no filter.
// The reason this exists: GLM / non-Anthropic proxies don't emit prompt-cache
// tokens, so mixing them into the aggregate tanks the hero cache_hit% number
// even when real Claude sessions are caching fine.
const MODEL_FILTER_KEY = 'csm.tokens.modelFilter'
const modelFilter = ref<string[]>(loadModelFilter())
const availableModels = ref<string[]>([])
const showModelPicker = ref(false)
const modelFilterEl = ref<HTMLElement | null>(null)

function loadModelFilter(): string[] {
  try {
    const raw = localStorage.getItem(MODEL_FILTER_KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.filter(x => typeof x === 'string') : []
  } catch { return [] }
}
function saveModelFilter(v: string[]) {
  try { localStorage.setItem(MODEL_FILTER_KEY, JSON.stringify(v)) } catch {}
}
function isAnthropic(name: string): boolean {
  const n = (name || '').toLowerCase()
  return n.startsWith('claude-') || n.includes('opus') || n.includes('sonnet') || n.includes('haiku')
}
function toggleModel(m: string) {
  const i = modelFilter.value.indexOf(m)
  if (i >= 0) modelFilter.value.splice(i, 1)
  else modelFilter.value.push(m)
}
function excludeNonAnthropic() {
  // Include only Anthropic-family models from the currently-observed set.
  modelFilter.value = availableModels.value.filter(isAnthropic)
}
function clearModelFilter() { modelFilter.value = [] }

// -----------------------------------------------------------------------
// M12: Agent scope filter. Defaults to UserPreference.default_agent so
// the Tokens page shows the user's primary CLI-adapter's usage. Special
// value 'all' clears the scope (show every adapter's spend mixed).
// Persisted per-session in localStorage.
// -----------------------------------------------------------------------
const AGENT_SCOPE_KEY = 'csm.tokens.agentScope'
const backendsStore = useBackendsStore()
const prefsStore = usePreferencesStore()
// 'all' | '<agent-name>' | '' (blank = "not chosen yet, use pref default")
const agentScope = ref<string>(loadAgentScope())
function loadAgentScope(): string {
  try { return localStorage.getItem(AGENT_SCOPE_KEY) || '' } catch { return '' }
}
function saveAgentScope(v: string) {
  try { localStorage.setItem(AGENT_SCOPE_KEY, v) } catch {}
}
// Effective agent for API queries: user's explicit choice > pref default.
const effectiveAgentScope = computed<string | null>(() => {
  const s = agentScope.value
  if (s === 'all') return null                       // clear filter
  if (s) return s
  return prefsStore.prefs?.default_agent ?? null
})

const activeFilters = computed(() => {
  const out: Record<string, unknown> = {}
  if (modelFilter.value.length) out.model = modelFilter.value
  if (effectiveAgentScope.value) out.agent = [effectiveAgentScope.value]
  return Object.keys(out).length ? out : undefined
})

function setAgentScope(v: string) {
  agentScope.value = v
  saveAgentScope(v)
}

const current = ref<any>(null)
const projectTop = ref<any[]>([])
const modelTop = ref<any[]>([])
const sourceTop = ref<any[]>([])
const sessionTop = ref<any[]>([])
const toolTop = ref<any[]>([])
const toolAgg = ref<any | null>(null)
const toolSort = ref<'total_tokens' | 'cost' | 'count'>('total_tokens')
const spikes = ref<any[]>([])
const chartItems = ref<any[]>([])
const loading = ref(false)
const loadErr = ref('')

// /usage live probe card state.
const usageLive = ref<any>(null)  // {latest: {...}, interval_min: 30}
const periodDelta = ref<any>(null)   // current WIN vs prior same-length WIN
// Label used in the delta badge tooltip and small text — follows `win`.
const deltaWindowLabel = computed(() => WIN[win.value].label)
// Flag when the prior window overlaps the recorded data range poorly —
// tells the user why a 7d/30d delta may look extreme.
const deltaCoverageLow = computed(() => {
  const c = periodDelta.value?.prev_coverage
  return typeof c === 'number' && c < 0.8
})
const deltaTooltip = computed(() => {
  const pd = periodDelta.value
  if (!pd) return ''
  const w = deltaWindowLabel.value
  const cur = humanTokens(pd.current.total_tokens)
  const prev = humanTokens(pd.previous.total_tokens)
  let s = `this ${w} vs the previous ${w}: ${prev} → ${cur}`
  if (deltaCoverageLow.value) {
    const pct = Math.round((pd.prev_coverage || 0) * 100)
    s += `\nonly ${pct}% of the previous ${w} falls inside the available data`
  }
  return s
})
const dataRange = ref<any>(null)     // earliest raw_token_event.ts
const usageRefreshing = ref(false)
const usageErr = ref('')

// Probe status derived from the snapshot itself. Users complained the card
// silently showed "—%" when the backend probe was failing or hadn't run in
// a long time; distinguish "no data yet" from "probe crashed" from "just
// stale" so the failure mode is legible instead of looking like a UI bug.
// Human-readable panel label that embeds the current Claude plan tier
// when known. `usageLive.latest.tier` typically looks like `default_max_5x`
// (Max 5x plan) or `default_pro`. Formatter strips the `default_` prefix,
// replaces underscores with spaces, and title-cases token boundaries so
// "default_max_5x" → "Max 5x".
function _formatTier(t: string | null | undefined): string | null {
  if (!t) return null
  const cleaned = String(t).replace(/^default_/, '').replace(/_/g, ' ').trim()
  if (!cleaned) return null
  // Title-case each whitespace-separated token; digits stay as-is.
  return cleaned.replace(/\b([a-z])/g, (m) => m.toUpperCase())
}
// Which agent's usage snapshot should we fetch + render?
//   - scope=claude / null (all agents) → claude snapshot (session% + week%)
//   - scope=codex                       → codex snapshot (week% only)
//   - future adapter with no probe      → no panel (placeholder shown)
const probeAgent = computed<'claude' | 'codex' | null>(() => {
  const s = effectiveAgentScope.value
  if (s === 'codex') return 'codex'
  if (s === null || s === 'claude') return 'claude'
  return null
})
const showUsagePanel = computed(() => probeAgent.value !== null)
// Toggle the session-bar row on/off. Codex has no 5h window; the /status
// pane only reports weekly. Read from the snapshot (not the scope) so a
// stale prior-scope snapshot doesn't render the wrong layout.
const isCodexUsage = computed(() =>
  usageLive.value?.latest?.agent === 'codex',
)

// Header: "{Agent} {Tier} usage" — e.g. "Claude Max 5x usage" or
// "Codex Plus usage". Falls back to "{Agent} plan usage" when tier hasn't
// landed yet. Agent name comes from the snapshot itself so it stays
// coherent with the numbers being shown.
const usageLabel = computed(() => {
  const agentName = isCodexUsage.value ? 'Codex' : 'Claude'
  const tier = _formatTier(usageLive.value?.latest?.tier)
  return tier ? `${agentName} ${tier} usage` : `${agentName} plan usage`
})

const usageStatus = computed(() => {
  const l = usageLive.value?.latest
  if (!l) return { kind: 'none' as const }
  if (l.error) return { kind: 'error' as const, msg: String(l.error) }
  // "No values" = probe finished but parse whiffed. Codex has no session
  // window, so week_pct alone is the load-bearing signal for both agents.
  const missing = l.agent === 'codex'
    ? l.week_pct == null
    : (l.session_pct == null && l.week_pct == null)
  const ts = l.ts ? new Date(l.ts).getTime() : 0
  const interval = (usageLive.value?.interval_min ?? 30) * 60 * 1000
  // 3× interval is a stale threshold: one missed tick can be transient, two
  // missed ticks means the scheduler is likely dead. 30-min default → 90 min.
  const staleAfter = interval * 3
  const ageMs = ts ? Date.now() - ts : 0
  if (missing) return { kind: 'no_values' as const, ageMs }
  if (ts && ageMs > staleAfter) return { kind: 'stale' as const, ageMs }
  return { kind: 'ok' as const, ageMs }
})

let chartEl: HTMLDivElement | null = null
let chart: echarts.ECharts | null = null
let resizeObs: ResizeObserver | null = null

function setChartRef(el: unknown) {
  // Function ref: Vue invokes this synchronously when the element is mounted
  // (and again with null on unmount). No useTemplateRef / nextTick guessing.
  if (!el) {
    if (resizeObs && chartEl) resizeObs.unobserve(chartEl)
    chartEl = null
    return
  }
  chartEl = el as HTMLDivElement
  if (!chart) chart = echarts.init(chartEl)
  if (!resizeObs) resizeObs = new ResizeObserver(() => chart?.resize())
  resizeObs.observe(chartEl)
  renderChart()
}

// Session detail modal.
const detailSid = ref<string | null>(null)
const detailData = ref<{
  summary: any | null
  models: any[]
  tasks: any[]
  sources: any[]
}>({ summary: null, models: [], tasks: [], sources: [] })
const detailLoading = ref(false)
const detailErr = ref('')
let detailChartEl: HTMLDivElement | null = null
let detailChart: echarts.ECharts | null = null
let pendingDetailItems: any[] | null = null

function setDetailChartRef(el: unknown) {
  if (!el) { detailChartEl = null; return }
  detailChartEl = el as HTMLDivElement
  if (!detailChart) detailChart = echarts.init(detailChartEl)
  if (pendingDetailItems) {
    drawDetailChart(pendingDetailItems)
    pendingDetailItems = null
  }
}

// Alert-rule authoring moved to AgentAlertsPanel component.

const totalTokens = computed(() => {
  const c = current.value
  if (!c) return 0
  return c.input_tokens + c.cache_creation_tokens + c.cache_read_tokens + c.output_tokens
})

const cacheHitRatio = computed(() => {
  const c = current.value
  if (!c) return 0
  const denom = c.input_tokens + c.cache_creation_tokens + c.cache_read_tokens
  return denom > 0 ? c.cache_read_tokens / denom : 0
})

const modelBars = computed(() => {
  // Top 4 models by token share, normalised against the window's total tokens.
  const total = totalTokens.value
  if (!total) return []
  return modelTop.value.slice(0, 4).map((m) => ({
    name: shortModel(m.entity),
    pct: total > 0 ? (m.total_tokens / total) * 100 : 0,
    cost: m.estimated_cost_usd,
    tokens: m.total_tokens,
  }))
})

const isEmpty = computed(() => !!current.value && current.value.msg_count === 0)

function shortModel(s: string | null): string {
  if (!s) return '—'
  // claude-3-5-opus-20241022 → opus-20241022 → opus
  const m = s.match(/(opus|sonnet|haiku)/i)
  return m ? m[1].toLowerCase() : (s.length > 16 ? s.slice(0, 14) + '…' : s)
}

function basename(p: string | null): string {
  if (!p) return '—'
  const parts = p.split('/').filter(Boolean)
  return parts[parts.length - 1] || p
}

function fmtUsd(v: number | undefined | null): string {
  const x = v || 0
  if (x >= 1000) return `$${(x / 1000).toFixed(1)}k`
  if (x >= 10) return `$${x.toFixed(0)}`
  return `$${x.toFixed(2)}`
}

function fmtLocalTs(iso: string | null | undefined): string {
  // Backend serializes naive UTC; iso_utc() appends 'Z'. Render in local zone.
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

function cacheHitPct(tl: any): string {
  // denominator excludes output_tokens: we're measuring "how much of the
  // *context* was served from cache". output is what the model generated, not
  // context.
  const denom = (tl.cache_read_tokens || 0) + (tl.cache_creation_tokens || 0) + (tl.input_tokens || 0)
  if (denom <= 0) return '—'
  return ((tl.cache_read_tokens || 0) / denom * 100).toFixed(0) + '%'
}

function humanTokens(v: number): string {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B'
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M'
  if (v >= 1e3) return (v / 1e3).toFixed(0) + 'k'
  return String(v ?? 0)
}

function parseUtcBucket(b: string): Date {
  // Backend buckets are naive UTC strings like "2026-06-24 08:00:00".
  // Parse explicitly as UTC so toLocale*() renders in the browser's local zone.
  return new Date(b.replace(' ', 'T') + 'Z')
}

function pad2(n: number): string { return n < 10 ? '0' + n : String(n) }

function fmtBucket(b: string): string {
  const d = parseUtcBucket(b)
  if (win.value === '1h' || win.value === '5h' || win.value === '12h' || win.value === '24h') {
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  }
  if (win.value === '7d') {
    return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}`
  }
  // 30d
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

function fmtBucketDetail(b: string): string {
  // Detail modal: longer labels when granularity is fine, date+hour otherwise.
  const d = parseUtcBucket(b)
  if (b.length > 13) {
    return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  }
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

async function refresh() {
  loading.value = true
  loadErr.value = ''
  try {
    const { hours, granularity } = WIN[win.value]
    // history() is in the unconditional Promise.all so it ALWAYS hits the API,
    // regardless of whether the chart container is mounted yet (the chart
    // panel lives inside v-else-if="current" — chartRef can be stale).
    const f = activeFilters.value
    // model-top's "always unfiltered" semantic is limited to the MODEL
    // filter itself (so users can see all models in the window and
    // pick some to include). It MUST still respect agent scope —
    // otherwise picking scope=codex shows a "top models" table dominated
    // by claude entries the user just filtered out.
    const fWithoutModelFilter: Record<string, unknown> | undefined = f
      ? Object.fromEntries(Object.entries(f).filter(([k]) => k !== 'model'))
      : undefined
    const [c, p, m, s, sess, h, tools, sp, ul, pd, dr] = await Promise.all([
      tokensApi.current(hours, f),
      tokensApi.top('project', hours, 8, projectSortKey(), f),
      tokensApi.top('model', hours, 6, 'total_tokens', fWithoutModelFilter),
      tokensApi.top('source', hours, 10, 'total_tokens', f),
      tokensApi.top('session', hours, 8, 'total_tokens', f),
      tokensApi.history(hours, granularity, f),
      tokensApi.toolsTop(hours, 20, toolSort.value, f).catch(() => ({ items: [] })),
      tokensApi.spikeTurns(hours, 15, f).catch(() => ({ items: [] })),
      // M14: fetch the snapshot matching the current agent scope so
      // codex-scoped view shows codex numbers instead of claude's.
      tokensApi.usageLive(probeAgent.value ?? 'claude').catch(() => null),
      tokensApi.periodDelta(WIN[win.value].hours, f).catch(() => null),
      tokensApi.dataRange(f).catch(() => null),
    ])
    usageLive.value = ul
    periodDelta.value = pd
    dataRange.value = dr
    current.value = c
    projectTop.value = p.items
    modelTop.value = m.items
    sourceTop.value = s.items
    sessionTop.value = sess.items
    chartItems.value = h.items
    toolTop.value = tools.items
    toolAgg.value = tools.aggregate || null
    spikes.value = sp.items
    // Render once the v-else-if branch has actually rendered the container.
    await nextTick()
    renderChart()
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  } finally {
    loading.value = false
  }
}

function projectSortKey(): string {
  if (projectSort.value === 'msg_count') return 'msg_count'
  if (projectSort.value === 'cost') return 'cost'
  return 'total_tokens'
}

function renderChart() {
  // Pure render — no API call. Safe to retry whenever element / data become ready.
  if (!chartEl || !chartItems.value.length) return
  if (!chart) chart = echarts.init(chartEl)
  renderTotalChart(chartItems.value)
  chart.resize()
}

function renderTotalChart(items: any[]) {
  // Tokens-over-time: 4 stacked bars per bucket (input / cc / cr / output),
  // with a cost($) line on the secondary axis for context. This makes the
  // "token spend over time" view the unambiguous default.
  const buckets = items.map(x => fmtBucket(x.bucket))
  chart!.setOption({
    grid: { left: 56, right: 56, top: 28, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (v: any) => typeof v === 'number' ? humanTokens(v) : v,
    },
    legend: {
      data: ['input', 'cache_creation', 'cache_read', 'output', 'cost($)'],
      top: 0, right: 8, textStyle: { color: '#787774', fontSize: 11 },
    },
    xAxis: {
      type: 'category',
      data: buckets,
      axisLine: { lineStyle: { color: '#EAEAEA' } },
      axisLabel: { color: '#787774', fontSize: 10 },
    },
    yAxis: [
      {
        type: 'value', name: 'tokens',
        axisLine: { lineStyle: { color: '#EAEAEA' } },
        splitLine: { lineStyle: { color: '#F0F0F0' } },
        axisLabel: { color: '#787774', fontSize: 10, formatter: humanTokens },
      },
      {
        type: 'value', name: 'cost($)', position: 'right',
        axisLine: { lineStyle: { color: '#EAEAEA' } },
        splitLine: { show: false },
        axisLabel: {
          color: '#787774', fontSize: 10,
          formatter: (v: number) => v >= 10 ? '$' + v.toFixed(0) : '$' + v.toFixed(1),
        },
      },
    ],
    series: [
      {
        name: 'input', type: 'bar', stack: 'tok',
        data: items.map(x => x.input_tokens),
        itemStyle: { color: '#1F6C9F' },
      },
      {
        name: 'cache_creation', type: 'bar', stack: 'tok',
        data: items.map(x => x.cache_creation_tokens),
        itemStyle: { color: '#346538' },
      },
      {
        name: 'cache_read', type: 'bar', stack: 'tok',
        data: items.map(x => x.cache_read_tokens),
        itemStyle: { color: '#956400' },
      },
      {
        name: 'output', type: 'bar', stack: 'tok',
        data: items.map(x => x.output_tokens),
        itemStyle: { color: '#C25450', borderRadius: [2, 2, 0, 0] },
      },
      {
        name: 'cost($)', type: 'line', yAxisIndex: 1,
        data: items.map(x => Number((x.estimated_cost_usd || 0).toFixed(2))),
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#7E57A6', width: 1.5 },
        itemStyle: { color: '#7E57A6' },
      },
    ],
  }, true)
}

function downloadCsv() {
  const u = tokensApi.exportCsvUrl()
  globalThis.open(u, '_blank')
}

function projectClick(_p: any) {
  // Future: drill into project sessions; for now just no-op.
}

async function openSessionDetail(sid: string | null) {
  if (!sid) return
  detailSid.value = sid
  detailLoading.value = true
  detailErr.value = ''
  detailData.value = { summary: null, models: [], tasks: [], sources: [] }
  // Session detail uses a wider window than the page window so we capture the
  // full session even if it overflows the current window.
  const hours = Math.max(WIN[win.value].hours, 168)
  const filter = { session: [sid] }
  try {
    const [sumRes, h, mods, tasks, srcs] = await Promise.all([
      tokensApi.top('session', hours, 1, 'total_tokens', filter),
      tokensApi.history(hours, hoursToGran(hours), filter),
      tokensApi.top('model', hours, 6, 'total_tokens', filter),
      tokensApi.top('task', hours, 8, 'total_tokens', filter),
      tokensApi.top('source', hours, 6, 'total_tokens', filter),
    ])
    detailData.value = {
      summary: sumRes.items[0] || null,
      models: mods.items,
      tasks: tasks.items,
      sources: srcs.items,
    }
    // Flip loading off so the inner v-if="summary && !loading" branch renders
    // the chart container, then wait for Vue to flush before init.
    detailLoading.value = false
    await nextTick()
    drawDetailChart(h.items)
  } catch (e) {
    detailErr.value = apiErrorMessage(e)
    detailLoading.value = false
  }
}

function hoursToGran(hours: number): string {
  if (hours <= 1) return 'minute'
  if (hours <= 7) return '5min'
  if (hours <= 48) return '5min'
  if (hours <= 168) return 'hour'
  return 'day'
}

function drawDetailChart(items: any[]) {
  if (!detailChartEl) {
    // Element not mounted yet — stash items so setDetailChartRef draws them
    // as soon as Vue invokes the function ref.
    pendingDetailItems = items
    return
  }
  if (!detailChart) detailChart = echarts.init(detailChartEl)
  const buckets = items.map(x => fmtBucketDetail(x.bucket))
  detailChart.setOption({
    grid: { left: 56, right: 56, top: 28, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: any) => typeof v === 'number' ? humanTokens(v) : v,
    },
    legend: {
      data: ['input', 'cache_creation', 'cache_read', 'output', 'cost($)'],
      top: 0, right: 8, textStyle: { color: '#787774', fontSize: 11 },
    },
    xAxis: {
      type: 'category', data: buckets,
      axisLine: { lineStyle: { color: '#EAEAEA' } },
      axisLabel: { color: '#787774', fontSize: 10 },
    },
    yAxis: [
      {
        type: 'value', name: 'tokens',
        axisLine: { lineStyle: { color: '#EAEAEA' } },
        splitLine: { lineStyle: { color: '#F0F0F0' } },
        axisLabel: { color: '#787774', fontSize: 10, formatter: humanTokens },
      },
      {
        type: 'value', name: 'cost($)', position: 'right',
        axisLine: { lineStyle: { color: '#EAEAEA' } },
        splitLine: { show: false },
        axisLabel: {
          color: '#787774', fontSize: 10,
          formatter: (v: number) => v >= 10 ? '$' + v.toFixed(0) : '$' + v.toFixed(1),
        },
      },
    ],
    series: [
      { name: 'input',          type: 'bar', stack: 'tok', data: items.map(x => x.input_tokens), itemStyle: { color: '#1F6C9F' } },
      { name: 'cache_creation', type: 'bar', stack: 'tok', data: items.map(x => x.cache_creation_tokens), itemStyle: { color: '#346538' } },
      { name: 'cache_read',     type: 'bar', stack: 'tok', data: items.map(x => x.cache_read_tokens), itemStyle: { color: '#956400' } },
      { name: 'output',         type: 'bar', stack: 'tok', data: items.map(x => x.output_tokens), itemStyle: { color: '#C25450', borderRadius: [2, 2, 0, 0] } },
      {
        name: 'cost($)', type: 'line', yAxisIndex: 1,
        data: items.map(x => Number((x.estimated_cost_usd || 0).toFixed(2))),
        smooth: true, symbol: 'none',
        lineStyle: { color: '#7E57A6', width: 1.5 },
        itemStyle: { color: '#7E57A6' },
      },
    ],
  }, true)
  detailChart.resize()
}

function closeSessionDetail() {
  detailSid.value = null
  detailErr.value = ''
  detailChart?.dispose()
  detailChart = null
}

async function refreshUsageLive() {
  if (usageRefreshing.value) return
  usageRefreshing.value = true
  usageErr.value = ''
  try {
    // M14: refresh the probe for the currently-scoped agent.
    const r = await tokensApi.usageLiveRefresh(probeAgent.value ?? 'claude')
    // API returns {ok, latest}; usageLive holds {latest, interval_min}
    usageLive.value = {
      latest: r.latest,
      interval_min: usageLive.value?.interval_min ?? 30,
    }
  } catch (e) {
    usageErr.value = apiErrorMessage(e)
  } finally {
    usageRefreshing.value = false
  }
}

function pctClass(pct: number | null | undefined): string {
  if (pct == null) return 'unknown'
  if (pct >= 85) return 'danger'
  if (pct >= 60) return 'warn'
  return 'ok'
}

function fmtRelTime(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const t = new Date(iso).getTime()
  if (isNaN(t)) return iso
  const s = (Date.now() - t) / 1000
  if (s < 0) return 'just now'
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

watch([win, projectSort, toolSort], refresh)

// Persist model filter + refresh when it changes. Use JSON stringify for
// deep-compare across ref[] mutations (splice/push don't trip [array] identity).
watch(() => JSON.stringify(modelFilter.value), (v) => {
  saveModelFilter(JSON.parse(v))
  refresh()
})

async function loadAvailableModels() {
  try {
    // 7d window is wide enough to surface all models the user has touched
    // recently — the picker doesn't need to match the page's current window.
    const r = await tokensApi.facet('model', 168, 50)
    availableModels.value = (r.values || []).filter((x: unknown) => typeof x === 'string')
  } catch { /* non-fatal */ }
}

function onDocClick(e: MouseEvent) {
  if (!showModelPicker.value) return
  const root = modelFilterEl.value
  if (root && !root.contains(e.target as Node)) showModelPicker.value = false
}

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  // M12: warm up backends + prefs stores so the agent scope selector +
  // effectiveAgentScope have data before the first refresh call.
  await Promise.all([backendsStore.ensureLoaded(), prefsStore.ensureLoaded()])
  await loadAvailableModels()
  refresh()
})
// Re-fetch when the user picks a different agent scope.
watch(effectiveAgentScope, () => { refresh() })
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  resizeObs?.disconnect()
  chart?.dispose()
  chart = null
  detailChart?.dispose()
  detailChart = null
})
</script>

<template>
  <div class="tok-page">
    <div class="toolbar">
      <div class="filter-group" role="tablist" aria-label="Time window">
        <button v-for="w in (['1h','5h','12h','24h','7d','30d'] as Win[])" :key="w"
          :class="{ active: win === w }" @click="win = w" :title="WIN[w].label">{{ w }}</button>
      </div>
      <div class="right">
        <!--
          M12: Agent scope. Defaults to UserPreference.default_agent so
          the page shows the user's primary CLI's spend; explicit
          "All agents" opts out. Renders AgentBadge next to the label
          so the user always knows which adapter's data they're staring at.
        -->
        <div class="agent-scope">
          <label class="scope-label">Agent scope:</label>
          <AgentBadge
            v-if="effectiveAgentScope"
            :agent="effectiveAgentScope"
            :compact="true"
          />
          <select
            :value="agentScope || (prefsStore.prefs?.default_agent ?? '')"
            @change="setAgentScope(($event.target as HTMLSelectElement).value)"
            :title="agentScope === 'all'
              ? 'Showing all agents combined'
              : (agentScope
                  ? 'Explicit override — click to change'
                  : `Following your default (${prefsStore.prefs?.default_agent ?? 'claude'})`)"
          >
            <option v-for="b in backendsStore.items" :key="b.name" :value="b.name">
              {{ b.display_name }}
            </option>
            <option value="all">All agents</option>
          </select>
        </div>
        <div class="model-filter" ref="modelFilterEl">
          <button
            :class="{ active: modelFilter.length > 0 }"
            @click="showModelPicker = !showModelPicker"
            :title="modelFilter.length ? `Filtering: ${modelFilter.join(', ')}` : 'Filter by model'"
            :aria-expanded="showModelPicker"
          >
            Model
            <span v-if="modelFilter.length" class="filter-count">{{ modelFilter.length }}</span>
          </button>
          <div v-if="showModelPicker" class="panel model-picker">
            <div class="picker-head">
              <span class="label">Include models</span>
              <span v-if="modelFilter.length" class="picker-summary mono">
                {{ modelFilter.length }} / {{ availableModels.length }}
              </span>
            </div>
            <div class="filter-group picker-actions">
              <button @click="excludeNonAnthropic" :disabled="!availableModels.length">
                Anthropic only
              </button>
              <button @click="clearModelFilter" :disabled="!modelFilter.length">
                Clear
              </button>
            </div>
            <div v-if="!availableModels.length" class="picker-empty">
              no models observed in the last 7 days
            </div>
            <ul v-else class="picker-list">
              <li v-for="m in availableModels" :key="m">
                <label>
                  <input type="checkbox"
                    :checked="modelFilter.includes(m)"
                    @change="toggleModel(m)" />
                  <span class="mono model-name">{{ m }}</span>
                  <span v-if="!isAnthropic(m)" class="tag exited">non-anthropic</span>
                </label>
              </li>
            </ul>
            <div class="picker-hint">
              Empty = all models. Applies to hero, chart, tools & CSV.
            </div>
          </div>
        </div>
        <button @click="downloadCsv">⤓ CSV</button>
        <button @click="refresh" :disabled="loading">{{ loading ? '…' : '↻' }} Refresh</button>
      </div>
    </div>

    <div v-if="loadErr" class="err-banner">
      load failed: {{ loadErr }}
      <button @click="refresh">retry</button>
    </div>

    <div class="tok-body">
      <!-- Empty state CTA -->
      <section v-if="isEmpty" class="empty-state panel">
        <h3 class="serif">No token data in this window</h3>
        <p>Switch to a longer window (try 7d / 30d), or run a Claude session to generate usage.</p>
        <div class="empty-actions">
          <button v-for="w in (['5h','12h','24h','7d','30d'] as Win[])" :key="w"
            v-show="win !== w" @click="win = w">Try {{ w }}</button>
        </div>
      </section>

      <template v-else-if="current">
        <!-- Hero stat row -->
        <section class="hero">
          <div class="panel hero-main">
            <div class="hero-label">
              Total tokens · {{ win }}
              <span
                v-if="periodDelta?.delta_pct?.total_tokens !== null && periodDelta?.delta_pct?.total_tokens !== undefined"
                class="delta"
                :class="periodDelta.delta_pct.total_tokens >= 0 ? 'up' : 'down'"
                :title="deltaTooltip"
              >
                {{ periodDelta.delta_pct.total_tokens >= 0 ? '↑' : '↓' }}
                {{ Math.abs(periodDelta.delta_pct.total_tokens).toFixed(1) }}%
                <small>vs the previous {{ deltaWindowLabel }}<template v-if="deltaCoverageLow"> · partial data</template></small>
              </span>
            </div>
            <div class="hero-val">{{ humanTokens(totalTokens) }}</div>
            <div class="hero-sub">
              {{ fmtUsd(current.estimated_cost_usd) }} · {{ current.msg_count }} msgs
              <span v-if="dataRange?.earliest" class="data-since" :title="`earliest record: ${dataRange.earliest}\n${dataRange.event_count?.toLocaleString()} events in total`">
                · data since {{ new Date(dataRange.earliest).toLocaleDateString() }}
              </span>
            </div>
            <div class="model-bar" v-if="modelBars.length">
              <div class="bar-row">
                <div v-for="(b, i) in modelBars" :key="b.name"
                  class="bar-seg" :style="{ width: b.pct + '%', background: ['#1F6C9F','#C25450','#7E57A6','#346538'][i] }"
                  :title="`${b.name}: ${fmtUsd(b.cost)} (${b.pct.toFixed(0)}%)`"></div>
              </div>
              <div class="bar-legend">
                <span v-for="(b, i) in modelBars" :key="b.name" class="leg">
                  <i :style="{ background: ['#1F6C9F','#C25450','#7E57A6','#346538'][i] }"></i>
                  {{ b.name }} {{ b.pct.toFixed(0) }}%
                </span>
              </div>
            </div>
          </div>

          <!--
            Plan-usage panel is claude-specific — the backend probe shells
            `claude /usage` and parses the pane. Shown only when the
            currently-viewed Agent scope includes claude (i.e. either
            explicit `claude` OR the "All agents" combined view).
            For codex / gemini / any non-claude scope, render a small
            placeholder card so the user knows the panel is not missing
            due to a bug — it's simply not applicable.
          -->
          <div v-if="!showUsagePanel" class="panel usage-na-panel">
            <div class="ul-head">
              <div class="ul-head-main">
                <span class="hero-label">Plan usage</span>
                <AgentBadge
                  v-if="effectiveAgentScope"
                  :agent="effectiveAgentScope"
                  :compact="true"
                />
              </div>
            </div>
            <div class="ul-empty">
              Plan-quota tracking is Claude-specific. Switch the
              <em>Agent scope</em> above to <strong>Claude Code</strong>
              (or <strong>All agents</strong>) to see your Claude plan usage.
            </div>
          </div>
          <div v-else class="panel usage-live-panel">
            <div class="ul-head">
              <div class="ul-head-main">
                <!--
                  "{Agent} {Tier} usage" — e.g. "Claude Max 5x usage" or
                  "Codex Plus usage". Managed by usageLabel computed.
                -->
                <span class="hero-label">{{ usageLabel }}</span>
                <span v-if="usageLive?.latest?.subscription_type"
                      class="tag succeeded ul-tier">
                  {{ usageLive.latest.subscription_type }}
                </span>
              </div>
              <div class="ul-actions">
                <span class="ul-fetched">
                  {{ fmtRelTime(usageLive?.latest?.ts) }}
                </span>
                <button @click="refreshUsageLive" :disabled="usageRefreshing"
                        :title="usageRefreshing
                          ? 'probe in progress (~10-30s)'
                          : `run ${isCodexUsage ? 'codex /status' : 'claude /usage'} now`">
                  {{ usageRefreshing ? '…' : '↻' }}
                </button>
              </div>
            </div>

            <div v-if="usageErr" class="ul-err">{{ usageErr }}</div>

            <div v-if="usageStatus.kind === 'error'" class="ul-warn"
                 :title="usageStatus.msg">
              ⚠ probe failed — {{ usageStatus.msg.slice(0, 80) }}. Click ↻ to retry.
            </div>
            <div v-else-if="usageStatus.kind === 'no_values'" class="ul-warn">
              ⚠ backend snapshot has no values yet (parse missed the summary). Click ↻.
            </div>
            <div v-else-if="usageStatus.kind === 'stale'" class="ul-warn">
              ⚠ probe stale — last snapshot {{ fmtRelTime(usageLive?.latest?.ts) }}, expected every {{ usageLive?.interval_min ?? 30 }} min. Scheduler may be stuck; click ↻ to force.
            </div>

            <div v-if="!usageLive?.latest" class="ul-empty">
              No snapshot yet — click <em>↻</em>.
            </div>

            <!--
              Shared bar layout for both agents. Claude has both a 5h
              session window and a weekly window; codex only has weekly
              (parsed from /status), so the session bar is hidden.
            -->
            <div v-else class="ul-grid">
              <div v-if="!isCodexUsage" class="ul-bar-block">
                <div class="ul-bar-label">
                  <span>5-hour session</span>
                  <span class="ul-pct" :class="pctClass(usageLive.latest.session_pct)">
                    {{ usageLive.latest.session_pct ?? '—' }}%
                  </span>
                </div>
                <div class="ul-bar-track">
                  <div class="ul-bar-fill" :class="pctClass(usageLive.latest.session_pct)"
                       :style="{ width: (usageLive.latest.session_pct ?? 0) + '%' }"></div>
                </div>
                <div class="ul-bar-foot">
                  resets {{ usageLive.latest.session_reset || '—' }}
                </div>
              </div>

              <div class="ul-bar-block">
                <div class="ul-bar-label">
                  <span>Current week</span>
                  <span class="ul-pct" :class="pctClass(usageLive.latest.week_pct)">
                    {{ usageLive.latest.week_pct ?? '—' }}%
                  </span>
                </div>
                <div class="ul-bar-track">
                  <div class="ul-bar-fill" :class="pctClass(usageLive.latest.week_pct)"
                       :style="{ width: (usageLive.latest.week_pct ?? 0) + '%' }"></div>
                </div>
                <div class="ul-bar-foot">
                  resets {{ usageLive.latest.week_reset || '—' }}
                </div>
              </div>
            </div>
          </div>

          <div class="panel hero-cache" :class="{ great: cacheHitRatio >= 0.7, ok: cacheHitRatio >= 0.4 && cacheHitRatio < 0.7 }">
            <div class="hero-label">cache hit ratio</div>
            <div class="hero-val">{{ (cacheHitRatio * 100).toFixed(0) }}<span class="unit">%</span></div>
            <div class="hero-sub">
              cache_read {{ humanTokens(current.cache_read_tokens) }} /
              all_input {{ humanTokens(current.input_tokens + current.cache_creation_tokens + current.cache_read_tokens) }}
            </div>
          </div>

          <div class="panel hero-grid4">
            <div class="mini">
              <div class="mini-label">input</div>
              <div class="mini-val">{{ humanTokens(current.input_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">cache_creation</div>
              <div class="mini-val">{{ humanTokens(current.cache_creation_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">cache_read</div>
              <div class="mini-val">{{ humanTokens(current.cache_read_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">output</div>
              <div class="mini-val">{{ humanTokens(current.output_tokens) }}</div>
            </div>
          </div>
        </section>

        <!-- Main row: chart (2/3) + project rank (1/3) -->
        <section class="main-row">
          <div class="panel tok-chart-panel">
            <h3 class="serif chart-title">
              Token usage over time · {{ WIN[win].label }}
              <small class="hdr-hint">Bars = input/cache_creation/cache_read/output · Line = cost</small>
            </h3>
            <div :ref="setChartRef" class="tok-chart"></div>
          </div>

          <div class="panel proj-panel">
            <h3 class="serif">
              Repo / Project ranking
              <span class="hdr-controls">
                <select v-model="projectSort">
                  <option value="total_tokens">↓ tokens</option>
                  <option value="msg_count">↓ msgs</option>
                  <option value="cost">↓ cost</option>
                </select>
              </span>
            </h3>
            <table>
              <thead>
                <tr><th>path</th><th>tokens</th><th>cost</th><th>msgs</th></tr>
              </thead>
              <tbody>
                <tr v-for="p in projectTop" :key="String(p.entity)" @click="projectClick(p)">
                  <td class="mono" :title="p.entity || ''">{{ basename(p.entity) }}</td>
                  <td>{{ humanTokens(p.total_tokens) }}</td>
                  <td>{{ fmtUsd(p.estimated_cost_usd) }}</td>
                  <td>{{ p.msg_count }}</td>
                </tr>
                <tr v-if="!projectTop.length">
                  <td colspan="4" class="empty">No project attribution in this window.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- Tool usage — split each assistant message's usage evenly across its tool_use blocks -->
        <section class="ops-row">
          <div class="panel">
            <h3 class="serif">
              Tool token usage
              <small class="hdr-hint">Each assistant message's usage is split evenly across its tool_use blocks</small>
              <span class="hdr-controls">
                <select v-model="toolSort">
                  <option value="total_tokens">↓ tokens</option>
                  <option value="cost">↓ cost</option>
                  <option value="count">↓ invocations</option>
                </select>
              </span>
            </h3>
            <div v-if="toolAgg" class="tool-agg">
              <span>
                Σ tool_tokens = <strong>{{ humanTokens(toolAgg.tool_total_tokens) }}</strong>
                ({{ fmtUsd(toolAgg.tool_total_cost_usd) }}, {{ toolAgg.tool_invocation_count }} calls)
              </span>
              <span class="sep">·</span>
              <span>
                Window total = <strong>{{ humanTokens(toolAgg.window_total_tokens) }}</strong>
                ({{ fmtUsd(toolAgg.window_total_cost_usd) }})
              </span>
              <span class="sep">·</span>
              <span>
                Share = <strong>{{ toolAgg.share_pct }}%</strong>
                <small class="hdr-hint">Difference = assistant replies without tool_use (plain-text planning/answers) + pre-tracking history</small>
              </span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>tool</th>
                  <th>tokens</th>
                  <th title="total_tokens / calls">tokens/call</th>
                  <th title="cache_read / (cache_read + cache_creation + input)">cache_hit</th>
                  <th>cache_creation</th>
                  <th>cache_read</th>
                  <th>output</th>
                  <th>cost</th>
                  <th>calls</th>
                  <th>sessions</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="tl in toolTop" :key="String(tl.tool_name ?? 'none')">
                  <td class="mono">{{ tl.tool_name || '(unknown)' }}</td>
                  <td>{{ humanTokens(tl.total_tokens) }}</td>
                  <td>{{ humanTokens(tl.count > 0 ? Math.round(tl.total_tokens / tl.count) : 0) }}</td>
                  <td>{{ cacheHitPct(tl) }}</td>
                  <td>{{ humanTokens(tl.cache_creation_tokens) }}</td>
                  <td>{{ humanTokens(tl.cache_read_tokens) }}</td>
                  <td>{{ humanTokens(tl.output_tokens) }}</td>
                  <td>{{ fmtUsd(tl.estimated_cost_usd) }}</td>
                  <td>{{ tl.count }}</td>
                  <td>{{ tl.session_count }}</td>
                </tr>
                <tr v-if="!toolTop.length">
                  <td colspan="10" class="empty">
                    No tool_use captured yet in this window (only new events are tracked — let a session run, then refresh).
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- Spike turns — top assistant turns by tokens (where the money actually went) -->
        <section class="ops-row">
          <div class="panel">
            <h3 class="serif">
              Spike turns
              <small class="hdr-hint">Costliest assistant turns in this window · see which tools pull large context</small>
            </h3>
            <table class="spike-tbl">
              <thead>
                <tr>
                  <th>ts (local)</th>
                  <th>project</th>
                  <th>session</th>
                  <th>tools</th>
                  <th>input</th>
                  <th>cc</th>
                  <th>cr</th>
                  <th>output</th>
                  <th>total</th>
                  <th>cost</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="sp in spikes" :key="sp.id">
                  <td class="mono">{{ fmtLocalTs(sp.ts) }}</td>
                  <td class="mono path-cell" :title="sp.project_path || '—'">{{ basename(sp.project_path) }}</td>
                  <td class="mono"
                      :title="sp.claude_session_id || ''"
                      :class="{ clickable: sp.claude_session_id }"
                      @click="sp.claude_session_id && openSessionDetail(sp.claude_session_id)">
                    {{ (sp.claude_session_id || '—').slice(0, 8) }}
                  </td>
                  <td>
                    <span v-if="!sp.tools?.length" class="empty">—</span>
                    <span v-for="t in sp.tools" :key="t" class="tool-chip">{{ t }}</span>
                  </td>
                  <td>{{ humanTokens(sp.input_tokens) }}</td>
                  <td>{{ humanTokens(sp.cache_creation_tokens) }}</td>
                  <td>{{ humanTokens(sp.cache_read_tokens) }}</td>
                  <td>{{ humanTokens(sp.output_tokens) }}</td>
                  <td><strong>{{ humanTokens(sp.total_tokens) }}</strong></td>
                  <td>{{ fmtUsd(sp.estimated_cost_usd) }}</td>
                </tr>
                <tr v-if="!spikes.length">
                  <td colspan="10" class="empty">No assistant turns in this window.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- Secondary row: sessions (click to drill in) + alert rules -->
        <section class="sec-row">
          <div class="panel">
            <h3 class="serif">Top sessions <small class="hdr-hint">Click a row for details</small></h3>
            <table>
              <thead><tr><th>session</th><th>path</th><th>tokens</th><th>msgs</th><th>cost</th></tr></thead>
              <tbody>
                <tr v-for="s in sessionTop" :key="String(s.entity)"
                  class="clickable" @click="openSessionDetail(s.entity)">
                  <td class="mono" :title="s.entity || ''">{{ (s.entity || '—').slice(0, 12) }}</td>
                  <td class="mono path-cell" :title="s.project_path || '—'">{{ basename(s.project_path) }}</td>
                  <td>{{ humanTokens(s.total_tokens) }}</td>
                  <td>{{ s.msg_count }}</td>
                  <td>{{ fmtUsd(s.estimated_cost_usd) }}</td>
                </tr>
                <tr v-if="!sessionTop.length"><td colspan="5" class="empty">No sessions.</td></tr>
              </tbody>
            </table>
          </div>

          <AgentAlertsPanel />
        </section>
      </template>
    </div>

    <!-- Session detail modal -->
    <div v-if="detailSid" class="modal-backdrop" @click.self="closeSessionDetail" role="presentation">
      <div class="modal panel detail-modal" role="dialog" aria-modal="true" aria-labelledby="session-detail-title">
        <div class="detail-head">
          <div>
            <h3 class="serif" id="session-detail-title">Session <code class="mono">{{ detailSid.slice(0, 16) }}…</code></h3>
            <div v-if="detailData.summary?.project_path" class="detail-path mono" :title="detailData.summary.project_path">
              📁 {{ detailData.summary.project_path }}
            </div>
            <small class="ink-mute">window: last 7d · all attribution dims joined by session_id</small>
          </div>
          <button @click="closeSessionDetail">×</button>
        </div>

        <div v-if="detailErr" class="err-banner">load failed: {{ detailErr }}</div>
        <div v-if="detailLoading" class="empty" style="padding:20px">loading…</div>

        <template v-if="detailData.summary && !detailLoading">
          <div class="detail-stats">
            <div class="mini">
              <div class="mini-label">total tokens</div>
              <div class="mini-val">{{ humanTokens(detailData.summary.total_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">cost</div>
              <div class="mini-val">{{ fmtUsd(detailData.summary.estimated_cost_usd) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">msgs</div>
              <div class="mini-val">{{ detailData.summary.msg_count }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">input</div>
              <div class="mini-val">{{ humanTokens(detailData.summary.input_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">cache_creation</div>
              <div class="mini-val">{{ humanTokens(detailData.summary.cache_creation_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">cache_read</div>
              <div class="mini-val">{{ humanTokens(detailData.summary.cache_read_tokens) }}</div>
            </div>
            <div class="mini">
              <div class="mini-label">output</div>
              <div class="mini-val">{{ humanTokens(detailData.summary.output_tokens) }}</div>
            </div>
          </div>

          <h4 class="serif chart-title">
            Token usage over time (this session · last {{ Math.max(WIN[win].hours, 168) }}h)
          </h4>
          <div :ref="setDetailChartRef" class="detail-chart"></div>

          <div class="detail-breakdown">
            <div>
              <h4 class="serif">Tasks (this session)</h4>
              <table>
                <thead><tr><th>task</th><th>tokens</th><th>cost</th></tr></thead>
                <tbody>
                  <tr v-for="t in detailData.tasks" :key="String(t.entity)">
                    <td class="mono" :title="t.entity || ''">{{ t.entity || '—' }}</td>
                    <td>{{ humanTokens(t.total_tokens) }}</td>
                    <td>{{ fmtUsd(t.estimated_cost_usd) }}</td>
                  </tr>
                  <tr v-if="!detailData.tasks.length"><td colspan="3" class="empty">No named tasks.</td></tr>
                </tbody>
              </table>
            </div>
            <div>
              <h4 class="serif">Models</h4>
              <table>
                <thead><tr><th>model</th><th>tokens</th><th>cost</th></tr></thead>
                <tbody>
                  <tr v-for="m in detailData.models" :key="String(m.entity)">
                    <td>{{ shortModel(m.entity) }}</td>
                    <td>{{ humanTokens(m.total_tokens) }}</td>
                    <td>{{ fmtUsd(m.estimated_cost_usd) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div>
              <h4 class="serif">Sources</h4>
              <table>
                <thead><tr><th>source</th><th>tokens</th><th>msgs</th></tr></thead>
                <tbody>
                  <tr v-for="s in detailData.sources" :key="String(s.entity)">
                    <td>{{ s.entity || '—' }}</td>
                    <td>{{ humanTokens(s.total_tokens) }}</td>
                    <td>{{ s.msg_count }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tok-page { display: flex; flex-direction: column; height: 100%; }
.tok-body { padding: 0 20px 20px; overflow: auto; flex: 1; }

.toolbar .dim-pick { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-mute); }
.toolbar .dim-pick select { font-size: 12px; padding: 3px 6px; }
.toolbar button.active { background: var(--ink); color: var(--card); }
.toolbar button[disabled] { opacity: 0.5; cursor: not-allowed; }

.err-banner {
  margin: 8px 20px;
  padding: 8px 12px;
  background: var(--pastel-red-bg);
  color: var(--pastel-red-fg);
  border-radius: 6px;
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px;
}
.err-banner button { font-size: 11px; }

.empty-state {
  padding: 40px;
  text-align: center;
}
.empty-state h3 { font-size: 20px; margin-bottom: 8px; }
.empty-state p { color: var(--ink-mute); margin-bottom: 16px; }
.empty-actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }

/* Hero stat row */
.hero {
  display: grid;
  /* main | usage (stacked bars) | cache% | 4-mini */
  grid-template-columns: 2fr 2fr 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.hero .panel { padding: 18px 20px; }
.hero-label { font-size: 11px; color: var(--ink-mute); text-transform: uppercase; letter-spacing: 0.05em; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.hero-label .delta {
  font-size: 10.5px; padding: 1px 6px; border-radius: 3px; font-family: 'Geist Mono', monospace;
  letter-spacing: 0; text-transform: none;
}
.hero-label .delta.up { background: var(--pastel-red-bg, #FCEAE9); color: var(--pastel-red-fg, #C25450); }
.hero-label .delta.down { background: var(--pastel-green-bg, #E8F2EA); color: var(--pastel-green-fg, #4A8A5F); }
.hero-label .delta small { color: currentColor; opacity: 0.75; margin-left: 3px; text-transform: none; }
.data-since { color: var(--ink-faint, #B0AFAB); font-size: 10.5px; margin-left: 6px; }
.hero-val {
  font-family: 'Newsreader', serif;
  font-size: 40px;
  font-weight: 500;
  letter-spacing: -0.03em;
  line-height: 1.05;
  color: var(--ink);
  margin-top: 6px;
}
.hero-val .unit { font-size: 16px; color: var(--ink-mute); margin-left: 4px; }
.hero-sub { font-size: 11px; color: var(--ink-mute); margin-top: 6px; }
.hero-cache.great .hero-val { color: var(--pastel-green-fg, #346538); }
.hero-cache.ok .hero-val { color: var(--pastel-yellow-fg, #946400); }

.model-bar { margin-top: 14px; }

/* ---- /usage live snapshot card (lives inside .hero grid) ---- */
.usage-live-panel { display: flex; flex-direction: column; }
.usage-na-panel {
  /* Same footprint as usage-live-panel so the grid layout doesn't jump
     when the user toggles scope between agents. */
  display: flex;
  flex-direction: column;
  border: 1px dashed var(--border);
  background: var(--canvas);
}
.usage-na-panel .ul-empty {
  padding: 24px 20px;
  color: var(--ink-mute);
  font-size: 13px;
  line-height: 1.6;
}
.usage-na-panel .ul-empty em { font-style: italic; color: var(--ink); }
.usage-na-panel .ul-empty strong { color: var(--ink); }
.ul-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 8px; margin-bottom: 12px;
}
.ul-head-main { display: flex; align-items: center; gap: 6px; min-width: 0; }
.ul-tier {
  text-transform: none; font-family: 'Geist Mono', monospace;
  font-size: 9px; padding: 1px 5px;
}
.ul-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.ul-fetched {
  color: var(--ink-mute); font-size: 10px;
  font-family: 'Geist Mono', monospace;
}
.ul-actions button {
  padding: 2px 8px; font-size: 11px;
}
.ul-err {
  background: var(--pastel-red-bg); color: var(--pastel-red-fg);
  padding: 4px 8px; border-radius: 4px; font-size: 11px; margin-bottom: 8px;
}
.ul-warn {
  background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg);
  padding: 4px 8px; border-radius: 4px; font-size: 11px; margin-bottom: 8px;
  line-height: 1.4;
}
.ul-empty {
  padding: 10px; text-align: center;
  color: var(--ink-mute); font-size: 12px;
  background: var(--canvas); border-radius: 6px;
}
.ul-empty em { font-style: normal; color: var(--ink); }
/* Session and week stacked vertically inside the hero cell. */
.ul-grid {
  display: flex; flex-direction: column;
  gap: 12px;
}
.ul-bar-block { display: flex; flex-direction: column; gap: 4px; }
.ul-bar-label {
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 10px; color: var(--ink-mute);
}
.ul-bar-label > span:first-child {
  text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500;
}
.ul-pct {
  font-family: 'Newsreader', serif; font-size: 18px; font-weight: 500;
  letter-spacing: -0.02em;
  color: var(--ink);
}
.ul-pct.warn { color: var(--pastel-yellow-fg); }
.ul-pct.danger { color: var(--pastel-red-fg); }
.ul-pct.ok { color: var(--pastel-green-fg); }
.ul-bar-track {
  height: 6px; background: var(--canvas);
  border-radius: 3px; overflow: hidden;
  border: 1px solid var(--border);
}
.ul-bar-fill {
  height: 100%; border-radius: 3px;
  background: var(--pastel-green-fg);
  transition: width 400ms var(--ease-soft);
}
.ul-bar-fill.warn { background: var(--pastel-yellow-fg); }
.ul-bar-fill.danger { background: var(--pastel-red-fg); }
.ul-bar-fill.unknown { background: var(--ink-faint); }
.ul-bar-foot {
  font-size: 10px; color: var(--ink-mute);
  font-family: 'Geist Mono', monospace;
}

/* ---- Model filter (matches design system in style.css) ---- */
.agent-scope {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  font-size: 12px;
}
.agent-scope .scope-label { color: var(--ink-mute); }
.agent-scope select {
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--canvas);
  color: var(--ink);
  font: inherit;
}

.model-filter { position: relative; display: inline-block; }
.filter-count {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 16px; height: 16px; padding: 0 4px;
  margin-left: 4px;
  background: var(--card); color: var(--ink);
  border-radius: 8px;
  font-family: 'Geist Mono', monospace;
  font-size: 10px; font-weight: 600;
  line-height: 1;
}
.model-filter > button.active .filter-count {
  /* Invert when the parent button flips to ink background. */
  background: transparent;
  color: var(--card);
  border: 1px solid var(--card);
}
.model-picker {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 40;
  width: 300px;
  padding: 14px;
  cursor: default;
}
.picker-head {
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 10px;
}
.picker-summary { color: var(--ink-mute); font-size: 11px; }
.picker-actions {
  width: 100%;
  margin-bottom: 10px;
}
.picker-actions button { flex: 1; font-size: 12px; }
.picker-empty {
  padding: 12px 0;
  color: var(--ink-mute); font-size: 12px; font-style: italic;
  text-align: center;
}
.picker-list {
  list-style: none; margin: 0; padding: 0;
  max-height: 260px; overflow-y: auto;
  border-top: 1px solid var(--border);
  padding-top: 6px;
}
.picker-list li { padding: 2px 0; }
.picker-list label {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 150ms var(--ease-soft);
}
.picker-list label:hover { background: var(--canvas); }
.picker-list input[type="checkbox"] {
  margin: 0; padding: 0;
  width: 14px; height: 14px;
  cursor: pointer;
  accent-color: var(--ink);
}
.model-name { flex: 1; font-size: 12px; color: var(--ink); }
.picker-list .tag {
  font-size: 9px; padding: 1px 6px;
}
.picker-hint {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  color: var(--ink-mute); font-size: 11px; line-height: 1.4;
}
.bar-row {
  display: flex;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--canvas);
}
.bar-seg { height: 100%; transition: width 300ms var(--ease-soft); }
.bar-legend {
  display: flex; gap: 12px; flex-wrap: wrap;
  margin-top: 6px;
  font-size: 11px;
  color: var(--ink-mute);
}
.leg { display: inline-flex; align-items: center; gap: 4px; }
.leg i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }

.hero-grid4 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 16px;
  padding: 16px 20px;
}
.mini { display: flex; flex-direction: column; gap: 2px; }
.mini-label { font-size: 10px; color: var(--ink-mute); text-transform: uppercase; letter-spacing: 0.05em; }
.mini-val { font-family: 'Geist Mono', monospace; font-size: 16px; color: var(--ink); }

/* Main row */
.main-row {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.tok-chart-panel { padding: 12px 16px 16px; }
.tok-chart { height: 300px; width: 100%; min-height: 300px; }
.chart-title {
  font-size: 14px;
  margin: 0 0 6px;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.chart-title .hdr-hint { font-size: 10px; color: var(--ink-mute); font-style: italic; font-weight: 400; }
.proj-panel { padding: 16px; }
.proj-panel h3 { font-size: 14px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
.proj-panel table { width: 100%; border-collapse: collapse; font-size: 12px; }
.proj-panel td, .proj-panel th { padding: 5px 0; border-bottom: 1px solid var(--border); text-align: left; }
.proj-panel th { color: var(--ink-mute); font-weight: 500; font-size: 11px; }
.proj-panel tbody tr { cursor: default; }
.proj-panel tbody tr:hover { background: var(--canvas); }
.hdr-controls select { font-size: 11px; padding: 2px 6px; }
.hdr-btn { float: right; font-size: 11px; }

/* Operations row */
.ops-row {
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.ops-row .panel { padding: 16px; min-height: 180px; }
.ops-row h3 { font-size: 14px; margin-bottom: 10px; }
.tool-agg {
  display: flex; flex-wrap: wrap; gap: 8px;
  font-size: 12px; color: var(--ink-mute);
  margin: -4px 0 12px;
  padding: 8px 10px;
  background: var(--canvas);
  border-radius: 6px;
}
.tool-agg strong { color: var(--ink); font-weight: 500; }
.tool-agg .sep { color: var(--border); }
.tool-agg .hdr-hint { margin-left: 6px; }
.spike-tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.spike-tbl th, .spike-tbl td { padding: 5px 6px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
.spike-tbl th { color: var(--ink-mute); font-weight: 500; font-size: 11px; }
.spike-tbl .path-cell { color: var(--ink-mute); max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spike-tbl .clickable { cursor: pointer; text-decoration: underline dotted; }
.spike-tbl .clickable:hover { color: var(--ink); }
.tool-chip {
  display: inline-block;
  padding: 1px 6px;
  margin: 1px 2px 1px 0;
  font-size: 10px;
  background: var(--pastel-blue-bg);
  color: var(--pastel-blue-fg);
  border-radius: 6px;
}
.ops-row table { width: 100%; border-collapse: collapse; font-size: 12px; }
.ops-row td, .ops-row th { padding: 5px 0; border-bottom: 1px solid var(--border); text-align: left; }
.ops-row th { color: var(--ink-mute); font-weight: 500; font-size: 11px; }

/* Secondary row */
.sec-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.sec-row .panel { padding: 16px; min-height: 180px; }
.sec-row h3 { font-size: 14px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
.sec-row .hdr-hint { font-size: 10px; color: var(--ink-mute); font-weight: 400; font-style: italic; }
.sec-row table { width: 100%; border-collapse: collapse; font-size: 12px; }
.sec-row td, .sec-row th { padding: 5px 0; border-bottom: 1px solid var(--border); text-align: left; }
.sec-row th { color: var(--ink-mute); font-weight: 500; font-size: 11px; }
.sec-row tr.clickable { cursor: pointer; }
.sec-row tr.clickable:hover { background: var(--canvas); }
.sec-row .path-cell { color: var(--ink-mute); max-width: 14rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detail-path { font-size: 12px; color: var(--ink-mute); margin: 2px 0 4px; }

.rule-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.rule-row:last-child { border-bottom: 0; }
.rule-tags { margin-top: 2px; display: flex; gap: 4px; }
.rule-actions { display: flex; gap: 6px; }

.tag { padding: 1px 6px; border-radius: 6px; font-size: 10px; display: inline-block; }
.tag.info { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.tag.idle { background: var(--canvas); color: var(--ink-mute); }
.tag.sev.warn { background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg); }
.tag.sev.critical { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.tag.sev.info { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }

.empty { color: var(--ink-mute); font-style: italic; text-align: center; }

/* Modal */
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 99; }
.modal { padding: 24px; min-width: 32rem; max-width: 40rem; }
.rule-modal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.rule-modal-grid label { display: grid; gap: 4px; font-size: 12px; color: var(--ink-mute); }
.rule-modal-grid label.full { grid-column: 1 / -1; }
.rule-err { color: var(--pastel-red-fg); font-size: 12px; margin-top: 10px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }

/* Session detail modal */
.detail-modal { min-width: min(80rem, 92vw); max-width: 92vw; max-height: 88vh; overflow: auto; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.detail-head h3 { margin: 0 0 4px; }
.detail-head code { font-size: 12px; color: var(--ink-mute); }
.detail-head .ink-mute { color: var(--ink-mute); font-size: 11px; }
.detail-head button { font-size: 18px; padding: 0 8px; }
.detail-stats {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 12px;
  padding: 14px;
  background: var(--canvas);
  border-radius: 6px;
  margin-bottom: 14px;
}
.detail-chart { height: 220px; margin-bottom: 18px; }
.detail-breakdown {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 16px;
}
.detail-breakdown h4 { font-size: 12px; margin-bottom: 6px; color: var(--ink); font-weight: 500; }
.detail-breakdown table { width: 100%; border-collapse: collapse; font-size: 11px; }
.detail-breakdown td, .detail-breakdown th { padding: 4px 0; border-bottom: 1px solid var(--border); text-align: left; }
.detail-breakdown th { color: var(--ink-mute); font-weight: 500; }
@media (max-width: 880px) {
  .detail-stats { grid-template-columns: repeat(4, 1fr); }
  .detail-breakdown { grid-template-columns: 1fr; }
}
</style>
