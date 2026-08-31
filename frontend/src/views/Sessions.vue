<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
// Sessions.vue (rewritten in H1 P3, 2026-07-25).
//
// Prior file was 2788 LOC. This rewrite delegates to slot-10/11 primitives:
//   * useTerminalManager  — xterm + WS + fit + file links
//   * useSessionFilter    — search + tree + filter + expandedFolders
//   * useFileSystem       — recent-files LRU + preview
//   * useSessionUIStore   — activeSid / fullscreen / sidebarWidth / ctx menu
//   * SessionTree         — sidebar tree wrapper (root v-for + empty state)
//   * TerminalToolbar     — fullscreen chip strip + actions
//   * ContextMenu         — right-click menu (also reused for recent-files)
//
// Orchestration kept in-view (deliberately): session CRUD API calls, SSE wiring,
// splitter drag, edit-in-place rename, project modal.
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { sessionsApi, type SessionRow } from '../api/sessions'
import { sessionProjectsApi, type SessionProject } from '../api/sessionProjects'
import { fsApi } from '../api/fs'
import { filesApi } from '../api/files'
import SessionTreeNode from '../components/SessionTreeNode.vue'
import FilePicker from '../components/FilePicker.vue'
import SessionProjectManagerModal from '../components/SessionProjectManagerModal.vue'
import ContextMenu, { type ContextMenuItem } from '../components/ContextMenu.vue'
import TerminalToolbar, { type ChipItem } from '../components/TerminalToolbar.vue'
import { useToast } from '../composables/useToast'
import { useEventStream, type CSMEvent } from '../composables/useEventStream'
import { useSessionUIStore } from '../stores/sessionUI'
import { useSessionFilter, type FilterTab } from '../composables/useSessionFilter'
import { useTerminalManager } from '../composables/useTerminalManager'
import { useFileSystem } from '../composables/useFileSystem'
import { useNotificationsStore } from '../stores/notifications'
import AgentBadge from '../components/AgentBadge.vue'
import AgentSelector from '../components/AgentSelector.vue'
import AdapterFlagsPanel from '../components/AdapterFlagsPanel.vue'
import SessionOutputViewer from '../components/SessionOutputViewer.vue'
import { useCreateSessionForm } from '../composables/useCreateSessionForm'
import type { TerminalConnectionState, TerminalDisconnectReason } from '../composables/useTerminalManager'
import { formatApiError, POLL_GET_MAX_MS } from '../api/client'
import { createSliceRetention } from '../lib/sliceRetention'

const toast = useToast()
const route = useRoute()
const router = useRouter()
const notifStore = useNotificationsStore()
const ui = useSessionUIStore()
const { isTerminalMaxed, sidebarWidth, contextMenu } = storeToRefs(ui)

// ---------------------------------------------------------------------------
// Data source (rows) + tab / filter composable
// ---------------------------------------------------------------------------
const rows = shallowRef<SessionRow[]>([])
const sessionProjects = ref<SessionProject[]>([])
const listAtLimit = ref(false)
const HISTORY_PAGE_SIZE = 75
const LIVE_STATUSES = 'starting,running,idle,waiting_input,waiting_auth,orphaned'
const CLOSED_STATUSES = 'exited,crashed'
const historyLoadedLimit = ref(HISTORY_PAGE_SIZE)
const historyAllLoaded = ref(false)
const historyTotal = ref(0)
const activeTotal = ref(0)
const autoTotal = ref(0)
const loadingMoreHistory = ref(false)
const refreshing = ref(false)
const listError = ref('')
const searchHistoryLoading = ref(false)
const legacyHistoryCapped = ref(false)
const lastRefreshedAt = ref<Date | null>(null)
const RECENT_BUCKET_N = 7
const SIDEBAR_MIN = 180
const SIDEBAR_MAX = 600
const FS_STRIP_CAP = 10
const mobileMedia = window.matchMedia('(max-width: 640px)')
const isMobileViewport = ref(mobileMedia.matches)

const filterComposable = useSessionFilter(rows, sessionProjects, { recentBucketSize: RECENT_BUCKET_N })
const {
  searchQuery: search,
  filter,
  groupBy,
  expandedFolders,
  showArchived,
  searchActive,
  visibleRows,
  searchResults,
  sessionTree,
  historyTree,
  liveRows,
  isVisibleHistoryRow,
} = filterComposable
const { setGroupBy, toggleFolder, isOpen } = filterComposable

// ---------------------------------------------------------------------------
// Sidebar-scoped controls vs. fullscreen
// ---------------------------------------------------------------------------
// Fullscreen hides the sidebar (`.sess-layout.terminal-max .sess-list`) but NOT
// the two toolbars above it, so every control whose only output is that list
// stayed clickable while doing nothing visible: clicking History set `filter`
// and the user saw no change until they happened to leave fullscreen. Dead
// controls are worse than absent ones — the click looks broken, not ignored.
//
// Rather than hiding them, honour the intent: using one of these IS a request
// to look at the list, so drop out of fullscreen and show it. The terminal is
// refit by the `isTerminalMaxed` watcher, same as the ⛶ button and Esc.
//
// Not every toolbar control belongs here. `Reap stale` / `Archive ended` /
// `Projects` / `+ New session` act on the server or open a modal and work fine
// fullscreen. `Show archived` and `Clear history` only render when
// `filter === 'history'`, which after this can no longer be true in fullscreen.
function revealSidebar(): void {
  if (isTerminalMaxed.value) ui.setFullscreen(false)
}
function selectFilterTab(tab: FilterTab): void {
  revealSidebar()
  filter.value = tab
}
function selectGroupBy(mode: 'project' | 'cwd'): void {
  revealSidebar()
  setGroupBy(mode)
}

const fs = useFileSystem()
const { recentFilesCount } = fs

// ---------------------------------------------------------------------------
// Purge tombstones — optimistic delete guard against refresh races
// ---------------------------------------------------------------------------
const purgeTombstones = ref<Set<string>>(new Set())
type SessionBucket = 'active' | 'auto' | 'history' | null

function bucketOf(s: SessionRow | undefined): SessionBucket {
  if (!s) return null
  if (['starting', 'running', 'idle', 'waiting_input', 'waiting_auth', 'orphaned'].includes(s.status)) {
    return s.type === 'auto' ? 'auto' : s.type === 'interactive' ? 'active' : null
  }
  if (s.type === 'interactive' && (s.status === 'exited' || s.status === 'crashed')) return 'history'
  return null
}

function adjustBucketCount(bucket: SessionBucket, delta: number) {
  if (bucket === 'active') activeTotal.value = Math.max(0, activeTotal.value + delta)
  else if (bucket === 'auto') autoTotal.value = Math.max(0, autoTotal.value + delta)
  else if (bucket === 'history') historyTotal.value = Math.max(0, historyTotal.value + delta)
}

function sessionRowsEqual(a: SessionRow, b: SessionRow): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof SessionRow>
  for (const key of keys) {
    const av = a[key]
    const bv = b[key]
    if (Array.isArray(av) && Array.isArray(bv)) {
      if (av.length !== bv.length || av.some((value, index) => value !== bv[index])) return false
    } else if (av !== bv) {
      return false
    }
  }
  return true
}

/**
 * Preserve object identities for unchanged rows and skip assigning the
 * shallowRef when a refresh returns the exact same snapshot. That prevents
 * no-op SSE reconciliation from rebuilding the filter trees and remounting
 * a long sidebar.
 */
function replaceRows(nextRows: SessionRow[]) {
  const priorById = new Map(rows.value.map((row) => [row.id, row]))
  const seen = new Set<string>()
  const stable = nextRows
    .filter((row) => {
      if (seen.has(row.id) || purgeTombstones.value.has(row.id)) return false
      seen.add(row.id)
      return true
    })
    .map((row) => {
      const prior = priorById.get(row.id)
      return prior && sessionRowsEqual(prior, row) ? prior : row
    })
  // Release any terminating guard whose row came back closed (or vanished)
  // in a full refresh — the SSE session.ended may have been missed.
  if (terminatingIds.value.size) {
    const byId = new Map(stable.map((row) => [row.id, row]))
    for (const sid of [...terminatingIds.value]) {
      const row = byId.get(sid)
      if (!row || isClosed(row)) endTerminating(sid)
    }
  }
  if (stable.length === rows.value.length
    && stable.every((row, index) => row === rows.value[index])) return
  rows.value = stable
}

function upsertSessionRow(next: SessionRow) {
  if (purgeTombstones.value.has(next.id)) return
  // Release the terminating guard once the row we're upserting has actually
  // closed (covers both the optimistic SSE patch and the single-row refresh).
  clearTerminatingIfClosed(next)
  const index = rows.value.findIndex((row) => row.id === next.id)
  const prior = index >= 0 ? rows.value[index] : undefined
  if (prior && sessionRowsEqual(prior, next)) return
  const priorBucket = bucketOf(prior)
  const nextBucket = bucketOf(next)
  if (priorBucket !== nextBucket) {
    adjustBucketCount(priorBucket, -1)
    adjustBucketCount(nextBucket, 1)
  }
  if (index < 0) {
    rows.value = [next, ...rows.value]
    return
  }
  const updated = rows.value.slice()
  updated[index] = next
  rows.value = updated
}

async function fetchAllSessionPages(
  params: { status: string; type: string },
): Promise<Awaited<ReturnType<typeof sessionsApi.list>>> {
  const items: SessionRow[] = []
  let offset = 0
  let total = 0
  let legacyPagination = false
  do {
    const page = await sessionsApi.list({ ...params, limit: 500, offset })
    total = page.count
    legacyPagination ||= page.legacy_pagination
    items.push(...page.items)
    offset += page.items.length
    if (page.legacy_pagination || !page.has_more || page.items.length === 0) break
  } while (true)
  return {
    count: total,
    page_count: items.length,
    offset: 0,
    has_more: false,
    items,
    legacy_pagination: legacyPagination,
  }
}

async function fetchHistorySnapshot() {
  if (historyAllLoaded.value) {
    return fetchAllSessionPages({ status: CLOSED_STATUSES, type: 'interactive' })
  }
  let page = await sessionsApi.list({
    status: CLOSED_STATUSES,
    type: 'interactive',
    limit: historyLoadedLimit.value,
  })
  // Compatibility with a backend that predates offset pagination. A second
  // request at the server's maximum limit recovers the complete history for
  // normal-sized installations instead of silently hiding everything after
  // row 75.
  if (page.legacy_pagination && page.items.length >= historyLoadedLimit.value
    && historyLoadedLimit.value < 500) {
    page = await sessionsApi.list({
      status: CLOSED_STATUSES,
      type: 'interactive',
      limit: 500,
    })
  }
  return page
}

// The list is three independent queries over one HTTP/1.1 connection pool, and
// a wedged tunnel connection takes down whichever of them happened to land on
// it — not all three. Awaiting them with Promise.all made the survivors die
// with the casualty; `sliceRetention` keeps each one's last good answer so a
// blip costs staleness in one section instead of the whole list. It still
// throws when every slice fails, or when one keeps failing — see its header.
type SessionSlice = Awaited<ReturnType<typeof fetchAllSessionPages>>
const SLICE_KEYS = ['active', 'auto', 'history'] as const
const STALE_TOLERANCE = 3
const sliceRetention = createSliceRetention<(typeof SLICE_KEYS)[number], SessionSlice>(
  SLICE_KEYS,
  STALE_TOLERANCE,
)
/** Slices the most recent refreshOnce() rendered from cache. */
const staleSlices = ref(new Set<(typeof SLICE_KEYS)[number]>())

async function refreshOnce() {
  const settled = await Promise.allSettled([
    fetchAllSessionPages({ status: LIVE_STATUSES, type: 'interactive' }),
    fetchAllSessionPages({ status: LIVE_STATUSES, type: 'auto' }),
    fetchHistorySnapshot(),
  ])
  const { values, stale } = sliceRetention.apply(settled)
  staleSlices.value = stale
  const { active, auto, history } = values
  // `replaceRows` dedupes first-wins by id and `active` leads the list, so a
  // CACHED active slice outranks a FRESH history row for the same session: one
  // that just exited keeps rendering as RUNNING until the cache expires (up to
  // STALE_TOLERANCE refreshes). Before retention all three slices necessarily
  // came from the same instant, so the order was safe — now it isn't. Anything
  // that answered fresh this round wins over a remembered copy of itself.
  const freshIds = new Set(
    SLICE_KEYS.filter((k) => !stale.has(k)).flatMap((k) => values[k].items.map((r) => r.id)),
  )
  const slice = (k: (typeof SLICE_KEYS)[number]) =>
    stale.has(k) ? values[k].items.filter((r) => !freshIds.has(r.id)) : values[k].items
  const combined = [...slice('active'), ...slice('auto'), ...slice('history')]
  if (activeSid.value && !combined.some((row) => row.id === activeSid.value)) {
    try {
      combined.push(await sessionsApi.get(activeSid.value))
    } catch { /* route may point to a purged row */ }
  }
  replaceRows(combined)
  activeTotal.value = active.count
  autoTotal.value = auto.count
  historyTotal.value = history.count
  legacyHistoryCapped.value = history.legacy_pagination && history.items.length >= 500
  listAtLimit.value = !history.legacy_pagination && history.has_more
  if (!history.has_more && !legacyHistoryCapped.value) historyAllLoaded.value = true
  // A partial refresh must not claim it synced everything — the toolbar reads
  // this back as "last synced <time>". Leave it at the last COMPLETE refresh.
  if (!stale.size) lastRefreshedAt.value = new Date()
  // Only a COMPLETE refresh may declare the list healthy. Clearing this on a
  // partial answer is what let a degraded list look identical to a good one:
  // the retry ladder in refresh() never fires (we resolved), the timestamp
  // only moves on a full pass, and the banner would have been wiped too.
  if (!stale.size) listError.value = ''
}

let refreshInFlight: Promise<void> | null = null
let refreshQueued = false

/**
 * Single-flight, trailing-edge reconciliation. Callers that arrive while a
 * request is running share the same promise and request exactly one follow-up
 * snapshot. This avoids a burst of agent events producing overlapping full
 * list queries while still guaranteeing the last event is observed.
 */
function refresh(): Promise<void> {
  if (refreshInFlight) {
    refreshQueued = true
    return refreshInFlight
  }
  refreshing.value = true
  const run = (async () => {
    do {
      refreshQueued = false
      await refreshOnce()
    } while (refreshQueued)
  })()
  refreshInFlight = run
    .catch(async (error) => {
      // A single failed list fetch is usually a transient tunnel blip (SSH
      // port-forward hiccup / brief congestion), not a real outage. Silently
      // retry once after a short delay before flashing the "Could not sync
      // sessions" banner — a recovered blip leaves no trace (refreshOnce()
      // clears listError itself on success).
      //
      // The deadline is DERIVED from pollGet's own budget, never hard-coded.
      // refreshOnce() is built out of pollGets, each of which may spend
      // POLL_TIMEOUT_MS wedged and then POLL_TIMEOUT_MS more on its retry.
      // A gate shorter than that rejects a recovery already in flight and
      // reports it as a timeout — the previous hard-coded 10s did exactly
      // that against a 16s budget, leaving only ~1.9s of headroom once a
      // real wedge burned its 8s. Browser HTTP/1.1 caps ~6 connections per
      // origin and a wedged one is held the whole time, so concurrent polls
      // queue and eat that headroom easily.
      await new Promise((resolve) => setTimeout(resolve, 2000))
      const retry = refreshOnce()
      retry.catch(() => {}) // swallow if the race below rejects first
      try {
        await Promise.race([
          retry,
          new Promise((_, reject) =>
            setTimeout(
              () => reject(new Error('list refresh retry timed out')),
              POLL_GET_MAX_MS,
            ),
          ),
        ])
        return // recovered — listError already cleared by refreshOnce()
      } catch (retryError) {
        listError.value = formatApiError(retryError)
        throw retryError
      }
    })
    .finally(() => {
      refreshing.value = false
      refreshInFlight = null
    })
  return refreshInFlight
}

async function manualRefresh() {
  try {
    await refresh()
  } catch (error) {
    toast.error(`Refresh failed: ${formatApiError(error)}`)
  }
}
async function loadMoreHistory() {
  if (!listAtLimit.value || loadingMoreHistory.value) return
  loadingMoreHistory.value = true
  const priorLimit = historyLoadedLimit.value
  const priorAllLoaded = historyAllLoaded.value
  if (historyLoadedLimit.value + HISTORY_PAGE_SIZE > 500) {
    historyAllLoaded.value = true
  } else {
    historyLoadedLimit.value += HISTORY_PAGE_SIZE
  }
  try {
    await refresh()
    // refresh() now resolves on a partial answer, so "it didn't throw" no
    // longer means the page we asked for arrived. Without this the button is
    // a silent no-op whenever the history slice is the one that got wedged.
    if (staleSlices.value.has('history')) {
      historyLoadedLimit.value = priorLimit
      historyAllLoaded.value = priorAllLoaded
      toast.error('Load history failed: the request never came back — try again')
    }
  }
  catch (e) {
    historyLoadedLimit.value = priorLimit
    historyAllLoaded.value = priorAllLoaded
    toast.error(`Load history failed: ${formatApiError(e)}`)
  } finally {
    loadingMoreHistory.value = false
  }
}
async function loadSessionProjects() {
  try {
    const { items } = await sessionProjectsApi.list(false)
    sessionProjects.value = items
  } catch (_) { /* pre-migration backend */ }
}

// ---------------------------------------------------------------------------
// Route ↔ active session
// ---------------------------------------------------------------------------
const activeSid = computed(() => route.params.sid as string | undefined)
const activeSession = computed(() => rows.value.find(s => s.id === activeSid.value))
const historyLoadedCount = computed(() =>
  rows.value.filter((s) => s.type === 'interactive' && isClosed(s)).length
)
const activeSessionClosed = computed(() => {
  const s = activeSession.value
  return !!s && (s.status === 'exited' || s.status === 'crashed')
})
// A session enters ORPHANED only on backend startup — the PTY child is
// still alive but the in-memory `SessionManager._live[sid]` handle was
// lost across the restart, so CSM can't send input anymore even though
// hooks keep firing. Not "closed" (terminal isn't dead) but not
// interactively usable either.
const activeSessionOrphaned = computed(() => {
  const s = activeSession.value
  return !!s && s.status === 'orphaned'
})
const activeSessionResumable = computed(() => {
  const s = activeSession.value
  // Orphaned rows are deliberately NOT resumable from the UI. Backend
  // sessions.py mirrors the same allow-list. Root cause: the pre-fix
  // Resume path SIGKILLed the orphan pid before spawning a fresh PTY,
  // which routinely nuked the user's live claude when the orphan was
  // still driven from another terminal (2026-07-25 incident).
  if (!s || !activeSessionClosed.value || s.superseded_by) return false
  const agent = s.agent || s.backend || 'claude'
  if (agent === 'claude') return !!s.external_session_id && s.jsonl_present !== false
  if (agent === 'codex') return !!(s.external_session_id || s.rollout_path)
  return false
})

function selectSession(sid: string) {
  router.push(`/sessions/${sid}`)
}

// Fullscreen-mode title hover: session sidebar is display:none, so the
// header title is the user's only handle on "which session am I in".
// Hovering it reveals a details popover (full id, agent, cwd, timings,
// current tool, etc.) so a user with 5 similarly-titled sessions
// doesn't have to exit fullscreen just to double-check they're on the
// right one. Kept off in windowed mode where the sidebar already shows
// most of this.
const showTitleDetails = ref(false)
function onTitleEnter() {
  if (!ui.isTerminalMaxed) return
  showTitleDetails.value = true
}
function onTitleLeave() {
  showTitleDetails.value = false
}

function backToMobileSessionList() {
  ui.setFullscreen(false)
  router.push('/sessions')
}

function fmtAbsTs(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function sessionDuration(): string {
  const s = activeSession.value
  if (!s || !s.started_at) return '—'
  const start = new Date(s.started_at).getTime()
  const end = s.ended_at ? new Date(s.ended_at).getTime() : Date.now()
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '—'
  const secs = Math.floor((end - start) / 1000)
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return m ? `${h}h ${m}m` : `${h}h`
}

function onMobileMediaChange(event: MediaQueryListEvent) {
  isMobileViewport.value = event.matches
  nextTick(() => term.scheduleFit())
}

// ---------------------------------------------------------------------------
// Session Changes panel — files the backing agent edited (from transcript/rollout)
// ---------------------------------------------------------------------------
interface ChangeFile {
  path: string
  edit_count: number
  tools: string[]
  first_ts: string
  last_ts: string
  additions: number
  deletions: number
  change_kind: 'added' | 'modified' | 'deleted' | 'renamed'
}
const changesFiles = ref<ChangeFile[]>([])
const changesTotalEdits = ref(0)
const changesExpanded = ref(false)  // popover open/closed
const changesLoading = ref(false)
const changesError = ref('')
const changesMenuRef = ref<HTMLElement | null>(null)

async function loadSessionChanges(sid: string) {
  changesLoading.value = true
  changesError.value = ''
  try {
    const data = await sessionsApi.changes(sid)
    changesFiles.value = data.files
    changesTotalEdits.value = data.total_edits
  } catch (e) {
    changesFiles.value = []
    changesTotalEdits.value = 0
    changesError.value = formatApiError(e)
  } finally {
    changesLoading.value = false
  }
}

function toggleSessionChanges() {
  changesExpanded.value = !changesExpanded.value
  if (changesExpanded.value && activeSid.value) {
    // Always refresh on open. Live Codex rollouts append structured patch
    // results continuously, and a page kept open across a backend restart
    // must not remain stuck on its previously cached empty result.
    loadSessionChanges(activeSid.value).catch(() => {})
  }
}

// Filename / parent path split for the popover row layout.
function fileBasename(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts[parts.length - 1] || path
}
function fileParent(path: string): string {
  const idx = path.lastIndexOf('/')
  if (idx <= 0) return ''
  return path.slice(0, idx) + '/'
}

// Diff view opens as a full page in a new tab — same UX as file preview.
// Server-side rendered with pygments DiffLexer + tango/one-dark themes so
// the styling matches the preview shell exactly. The naive inline mini-diff
// that used to live in the panel was too cramped to actually read, per user
// feedback ("I want to see before/after, like a git plugin does").
// Single-file diff view — user opens one file at a time from the
// Changes popover. `whole-file` render mode lives on the backend
// (unified_diff with n = file length). Keeps the "one file per tab"
// mental model per user feedback.
function openDiffView(path: string, sid: string) {
  const url = `/api/sessions/${encodeURIComponent(sid)}/changes/diff-view?path=${encodeURIComponent(path)}`
  window.open(url, '_blank', 'noopener,noreferrer')
}
function openAllDiffView(sid: string) {
  const url = `/api/sessions/${encodeURIComponent(sid)}/changes/diff-view`
  window.open(url, '_blank', 'noopener,noreferrer')
}

// ---------------------------------------------------------------------------
// Terminal wiring — composable owns xterm + WS + fit + file links
// ---------------------------------------------------------------------------
const termRef = ref<HTMLDivElement | null>(null)
const terminalConnectionState = ref<TerminalConnectionState>('disconnected')
const terminalReconnectAttempt = ref(0)
// Set when a 'disconnected' state is terminal (session ended / unauthorized)
// so the banner explains why and hides the futile "Reconnect now" (F3).
const terminalDisconnectReason = ref<TerminalDisconnectReason | null>(null)
let pasteWarnedThisMount = false
type PasteMode = 'ok' | 'denied' | 'insecure' | 'unknown'
const pasteMode = ref<PasteMode>('unknown')

async function probePasteCapability() {
  if (!window.isSecureContext) { pasteMode.value = 'insecure'; return }
  if (!navigator.clipboard?.readText) { pasteMode.value = 'denied'; return }
  try {
    const perms = 'permissions' in navigator ? navigator.permissions : undefined
    if (perms?.query) {
      const res = await perms.query({ name: 'clipboard-read' as PermissionName })
      pasteMode.value = res.state === 'denied' ? 'denied' : 'ok'
    } else pasteMode.value = 'ok'
  } catch { pasteMode.value = 'ok' }
}

const term = useTerminalManager({
  onClose: () => scheduleRefresh(),
  onConnectionState: (state, attempt, reason) => {
    terminalConnectionState.value = state
    terminalReconnectAttempt.value = attempt
    terminalDisconnectReason.value = state === 'disconnected' ? (reason ?? null) : null
  },
})

async function attachTerm(sid: string) {
  if (!termRef.value) return
  pasteWarnedThisMount = false
  await term.attach(termRef.value, sessionsApi.wsUrl(sid), sid)
  // Right-click clipboard on mount element
  const mountEl = term.getMountElement()
  if (mountEl) {
    const handler = onTermContextMenu
    mountEl.addEventListener('contextmenu', handler)
    ;(mountEl as any).__csm_ctx = handler
  }
  requestAnimationFrame(() => term.focus())
}
function detachTerm() {
  const mountEl = term.getMountElement()
  if (mountEl) {
    const h = (mountEl as any).__csm_ctx
    if (h) mountEl.removeEventListener('contextmenu', h)
    delete (mountEl as any).__csm_ctx
  }
  term.detach()
  terminalConnectionState.value = 'disconnected'
  terminalReconnectAttempt.value = 0
  terminalDisconnectReason.value = null
}
async function onTermContextMenu(ev: MouseEvent) {
  ev.preventDefault()
  const t = term.getTerminal()
  if (!t) return
  if (t.hasSelection()) {
    const sel = t.getSelection()
    if (sel) {
      try { await navigator.clipboard?.writeText?.(sel) }
      catch (e) {
        // Keep the domain-specific fallback: an empty DOMException message
        // is far less useful here than naming the actual cause.
        const why = e instanceof Error && e.message ? e.message : 'clipboard blocked'
        toast.error(`Copy failed: ${why}`)
      }
    }
    t.clearSelection()
    return
  }
  await pasteFromClipboard()
}

async function pasteFromClipboard() {
  const t = term.getTerminal()
  if (!t) return
  let text = ''
  if (window.isSecureContext && navigator.clipboard?.readText) {
    try { text = await navigator.clipboard.readText() } catch {}
  }
  if (!text) {
    try {
      const ta = document.createElement('textarea')
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0'
      document.body.appendChild(ta)
      ta.focus()
      const ok = document.execCommand('paste')
      if (ok) text = ta.value
      document.body.removeChild(ta)
    } catch {}
  }
  if (text) { t.paste(text); t.focus(); return }
  if (pasteWarnedThisMount) return
  if (!window.isSecureContext) {
    toast.warn('Paste blocked: browser needs http://localhost or HTTPS. Ctrl-Shift-V still works.')
    pasteWarnedThisMount = true
  } else if (pasteMode.value === 'denied') {
    toast.warn('Clipboard read denied. Use Ctrl-Shift-V, or grant via the padlock.')
    pasteWarnedThisMount = true
  }
}

// ---------------------------------------------------------------------------
// Splitter drag (sidebar resize) → ui store
// ---------------------------------------------------------------------------
const isDraggingSplitter = ref(false)
function startSplitterDrag(e: PointerEvent) {
  e.preventDefault()
  isDraggingSplitter.value = true
  const layoutEl = (e.currentTarget as HTMLElement).closest('.sess-layout') as HTMLElement | null
  const layoutLeft = layoutEl ? layoutEl.getBoundingClientRect().left : 0
  let cleanedUp = false

  const onMove = (ev: PointerEvent) => {
    if (cleanedUp) return
    const raw = ev.clientX - layoutLeft
    ui.setSidebarWidth(raw)
  }
  const onUp = () => cleanup()
  const cleanup = () => {
    if (cleanedUp) return
    cleanedUp = true
    isDraggingSplitter.value = false
    document.removeEventListener('pointermove', onMove, { capture: true })
    document.removeEventListener('pointerup', onUp, { capture: true })
    document.removeEventListener('pointercancel', onUp, { capture: true })
    clearTimeout(safetyTimer)
    // forceSync (not scheduleFit) on drag-end: the terminal's final cell size
    // may equal a size the PTY has seen before, so the resize-if-changed fast
    // path would skip the SIGWINCH and a TUI (codex/vim) redraws at the wrong
    // width. forceSync bypasses that guard — matches the fullscreen toggle.
    term.forceSync()
  }

  // Safety net: force cleanup after 5s if drag events never fire (off-window / alt-tab / tab switch)
  const safetyTimer = window.setTimeout(cleanup, 5000)

  document.addEventListener('pointermove', onMove, { capture: true })
  document.addEventListener('pointerup', onUp, { capture: true })
  document.addEventListener('pointercancel', onUp, { capture: true })
}

// ---------------------------------------------------------------------------
// Fullscreen chip strip
// ---------------------------------------------------------------------------
const fsAllSessions = computed(() =>
  [...liveRows.value].sort((a, b) => {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1
    const ta = new Date(a.last_activity_ts || a.started_at || 0).getTime()
    const tb = new Date(b.last_activity_ts || b.started_at || 0).getTime()
    return tb - ta
  })
)
const fsVisibleSessions = computed(() => fsAllSessions.value.slice(0, FS_STRIP_CAP))
const fsOverflowSessions = computed(() => fsAllSessions.value.slice(FS_STRIP_CAP))
const chipItems = computed<ChipItem[]>(() =>
  fsVisibleSessions.value.map(s => ({
    sid: s.id,
    title: s.title || s.id.slice(0, 8),
    status: s.status,
    unreadCount: s.unread_count || 0,
    isActive: s.id === activeSid.value,
    pinned: !!s.pinned,
    manualUnread: !!s.manual_unread,
    tooltip: `${s.cwd}${s.pid ? ' · pid ' + s.pid : ''} · ${formatTime(s.last_activity_ts)}`,
  }))
)
function onFsOverflowClick(ev: MouseEvent) {
  const items: ContextMenuItem[] = fsOverflowSessions.value.map(s => ({
    label: s.title || s.id.slice(0, 8),
    icon: s.pinned ? '📌' : '·',
    action: () => selectSession(s.id),
  }))
  if (!items.length) return
  ui.openContextMenu(ev.clientX, ev.clientY, items)
}

// ---------------------------------------------------------------------------
// Formatters / helpers reused across template + tree
// ---------------------------------------------------------------------------
function formatTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const s = Math.floor((Date.now() - d.getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return d.toISOString().slice(0, 10)
}
function stateTag(status: string): string {
  if (status === 'waiting_auth') return 'waiting'
  if (status === 'waiting_input' || status === 'idle') return 'idle'
  if (status === 'running' || status === 'starting') return 'running'
  if (status === 'crashed') return 'crashed'
  // Orphaned is architecturally distinct — process is still alive but
  // backend was restarted so the PTY handle is lost. Yellow pill + its
  // own info card explain the state instead of aliasing to "crashed".
  if (status === 'orphaned') return 'orphaned'
  if (status === 'exited') return 'exited'
  return 'info'
}
function isWaitingAuth(s: SessionRow): boolean { return s.status === 'waiting_auth' }
function isClosed(s: SessionRow): boolean { return s.status === 'exited' || s.status === 'crashed' }
function leavesOf(node: any): SessionRow[] {
  if (node.isLeaf && node.session) return [node.session]
  return (node.children || []).flatMap(leavesOf)
}

// ---------------------------------------------------------------------------
// Session CRUD
// ---------------------------------------------------------------------------
// M9.4: create-form state lives in useCreateSessionForm — no adapter-name
// branching survives in this view. `<AdapterFlagsPanel>` renders per-agent
// flags from Backend.flags_schema, and default_argv comes from the same
// schema. Adding a 3rd adapter is a zero-frontend-file change.
const showCreate = ref(false)
const form = useCreateSessionForm()
const recentCwds = ref<string[]>([])
const showBrowser = ref(false)
const showProjectManager = ref(false)

// Resumable-session picker feed for the `resume` FlagDescriptor kind.
// Filtered to interactive claude sessions with a live JSONL — the picker
// then reads `session.claude_session_id` via the default `resumeIdOf`.
const resumableRows = computed(() =>
  rows.value.filter(s => s.claude_session_id && s.type === 'interactive').slice(0, 20)
)

// Inline project-create
const showInlineProjectForm = ref(false)
const inlineProjectName = ref('')
const creatingInlineProject = ref(false)
const inlineProjectError = ref('')
const inlineProjectNameRef = ref<HTMLInputElement | null>(null)
async function openInlineProjectForm() {
  showInlineProjectForm.value = true
  inlineProjectError.value = ''
  await nextTick()
  try { inlineProjectNameRef.value?.focus() } catch (_) {}
}
function cancelInlineProject() {
  showInlineProjectForm.value = false
  inlineProjectName.value = ''
  inlineProjectError.value = ''
}
async function submitInlineProject() {
  const name = inlineProjectName.value.trim()
  if (!name || creatingInlineProject.value) return
  creatingInlineProject.value = true
  inlineProjectError.value = ''
  try {
    const created = await sessionProjectsApi.create({ name })
    await loadSessionProjects()
    form.sessionProjectId.value = created.id
    showInlineProjectForm.value = false
    inlineProjectName.value = ''
  } catch (e) {
    inlineProjectError.value = `Create failed: ${formatApiError(e)}`
  } finally { creatingInlineProject.value = false }
}

const settingProject = ref<Set<string>>(new Set())
async function assignSessionToProject(sid: string, projectId: string | null) {
  if (settingProject.value.has(sid)) return
  settingProject.value = new Set([...settingProject.value, sid])
  try {
    upsertSessionRow(await sessionsApi.setProject(sid, projectId))
  } catch (e) {
    toast.error(`Assign project failed: ${formatApiError(e)}`)
  } finally {
    const done = new Set(settingProject.value); done.delete(sid); settingProject.value = done
  }
}

async function createSession() {
  const cwdSnapshot = form.cwd.value
  showCreate.value = false
  // Pending toast is sticky (ttl=0) so it stays visible until the create
  // call resolves. Session spawn can take 10+ s (codex especially) — the
  // old 4 s auto-dismiss was leaving the user with no visible status,
  // and if the call then failed, the toast could vanish before the error
  // toast replaced it.
  const pendingId = toast.info(`Starting session in ${cwdSnapshot}…`, 0)
  try {
    const result = await form.submit(async (payload) => sessionsApi.create(payload))
    if (result) {
      upsertSessionRow(result)
      selectSession(result.id)
      form.reset()
      toast.success(`Session started in ${cwdSnapshot}`)
    }
  } finally {
    toast.dismiss(pendingId)
  }
}
// Own error handling for the submit path — keep it here so the view
// can also decide "reopen modal on failure" (which the composable
// doesn't know about).
async function createSessionSafe() {
  try {
    await createSession()
  } catch (e) {
    toast.error(`Create failed: ${formatApiError(e)}`)
    showCreate.value = true  // let user retry
  }
}

async function openCreate() {
  showCreate.value = !showCreate.value
  if (showCreate.value) {
    try {
      const r = await fsApi.recentCwds(10)
      recentCwds.value = r.items
      if (r.items.length && form.cwd.value === '/tmp') form.cwd.value = r.items[0]
    } catch (_) {}
  }
}
function openBrowser() { showBrowser.value = true }
function onPickCwd(p: string) { form.cwd.value = p; showBrowser.value = false }

// Edit-in-place rename
const editingTitle = ref(false)
const editTitleVal = ref('')
function startEditTitle() {
  if (!activeSession.value) return
  editTitleVal.value = activeSession.value.title || ''
  editingTitle.value = true
}
async function saveTitle() {
  if (!activeSession.value) return
  try {
    upsertSessionRow(await sessionsApi.rename(activeSession.value.id, editTitleVal.value))
  } catch (e) {
    toast.error(`Rename failed: ${apiErrorMessage(e)}`)
  }
  editingTitle.value = false
}
function cancelEditTitle() { editingTitle.value = false }

// Click-send helpers — route through xterm.paste() which triggers term.onData
// (WS-forwarded by useTerminalManager). Bracketed-paste wrapping is a minor
// tradeoff vs raw ws.send bytes; shells / claude handle both fine for these
// short sequences.
function sendBytes(data: string | Uint8Array) {
  if (!term.isConnected()) return
  const str = typeof data === 'string' ? data : new TextDecoder().decode(data)
  try { term.getTerminal()?.paste(str) } catch (_) { /* ignore */ }
}
function focusTerm() { term.focus() }
function sendEsc()   { sendBytes('\x1b'); focusTerm() }
function sendCtrlC() { sendBytes('\x03'); focusTerm() }
function sendCtrlD() { sendBytes('\x04'); focusTerm() }
function sendMode()  { sendBytes('\x1b[Z'); focusTerm() }
function sendUp()    { sendBytes('\x1b[A'); focusTerm() }
function sendDown()  { sendBytes('\x1b[B'); focusTerm() }
function sendLeft()  { sendBytes('\x1b[D'); focusTerm() }
function sendRight() { sendBytes('\x1b[C'); focusTerm() }
function sendEnter() { sendBytes('\r'); focusTerm() }
function sendTab()   { sendBytes('\t'); focusTerm() }

const navKeysExpanded = ref<boolean>(
  ((): boolean => { const raw = localStorage.getItem('csm.sess.navkeys.expanded'); return raw === null ? true : raw === '1' })()
)
function toggleNavKeys() {
  navKeysExpanded.value = !navKeysExpanded.value
  try { localStorage.setItem('csm.sess.navkeys.expanded', navKeysExpanded.value ? '1' : '0') } catch {}
}
const clipHintText = computed(() => {
  if (pasteMode.value === 'insecure' || pasteMode.value === 'denied')
    return '⚠ paste blocked · Ctrl-Shift-V still works'
  return '📋 select + right-click to copy · right-click to paste'
})
const pasteHintTitle = computed(() => {
  if (pasteMode.value === 'insecure') return 'Right-click paste needs a secure context (localhost or HTTPS). Copy still works.'
  if (pasteMode.value === 'denied') return 'Clipboard read denied. Grant permission via the browser padlock, or use Ctrl-Shift-V.'
  return 'Select text and right-click to copy; right-click on an empty area to paste.'
})
const pasteHintWarn = computed(() => pasteMode.value === 'insecure' || pasteMode.value === 'denied')

// Session-level actions
async function togglePin(s: SessionRow) {
  try { upsertSessionRow(await sessionsApi.setPinned(s.id, !s.pinned)) }
  catch (e) { toast.error(`Pin failed: ${apiErrorMessage(e)}`) }
}
async function toggleManualUnread(s: SessionRow) {
  const next = !s.manual_unread
  try {
    upsertSessionRow(await sessionsApi.setManualUnread(s.id, next))
    if (!next) notifStore.markSessionRead(s.id).catch(() => {})
  } catch (e) { toast.error(`Mark failed: ${apiErrorMessage(e)}`) }
}
async function toggleHighlighted(s: SessionRow) {
  try {
    upsertSessionRow(await sessionsApi.setHighlighted(s.id, !s.highlighted))
  } catch (e) { toast.error(`Highlight failed: ${apiErrorMessage(e)}`) }
}
// Sticky manual-unread should clear when the user actually opens the session
// (they've now seen it — matches how NEW_MESSAGE notifications auto-clear on
// visit). Kept as a separate call from markSessionRead because the
// manual_unread flag lives on the Session row, not on notif rows.
async function clearManualUnreadOnVisit(sid: string) {
  const row = rows.value.find(r => r.id === sid)
  if (!row || !row.manual_unread) return
  try {
    upsertSessionRow(await sessionsApi.setManualUnread(sid, false))
  } catch { /* best-effort — user can right-click Mark as read manually */ }
}
async function renameFromMenu(sid: string) {
  if (activeSid.value !== sid) { router.push(`/sessions/${sid}`); await nextTick() }
  if (isTerminalMaxed.value) { ui.setFullscreen(false); await nextTick() }
  startEditTitle()
}
async function fullscreenFromMenu(sid: string) {
  if (activeSid.value !== sid) { router.push(`/sessions/${sid}`); await nextTick() }
  ui.setFullscreen(true)
}
async function killFromMenu(sid: string) {
  if (terminatingIds.value.has(sid)) return
  if (!confirm('Kill this session (SIGKILL)?')) return
  beginTerminating(sid)
  toast.info('Killing session…')
  try { await sessionsApi.kill(sid) }
  catch (e) {
    endTerminating(sid)
    toast.error(`Kill failed: ${apiErrorMessage(e)}`)
  }
  finally { await refresh().catch(() => {}) }
}
async function killSession() {
  const sid = activeSid.value
  if (!sid) return
  if (terminatingIds.value.has(sid)) return
  if (!confirm('Hard KILL?')) return
  beginTerminating(sid)
  toast.info('Killing session…')
  try { await sessionsApi.kill(sid) }
  catch (e) {
    endTerminating(sid)
    toast.error(`Kill failed: ${apiErrorMessage(e)}`)
  }
  finally { await refresh().catch(() => {}) }
}

const resumingIds = ref<Set<string>>(new Set())
function isResuming(sid: string) { return resumingIds.value.has(sid) }

// Terminating guard (B1/B3): a stop/kill returns its 202 immediately but the
// backend then runs the SIGINT→SIGTERM→SIGKILL ladder (up to 15s for stop,
// ~5s for kill), during which the row is still `running`. Track in-flight
// terminations so the row's × / Kill buttons disable + show a spinner and a
// re-click can't fire a second request. Cleared when the row actually closes
// (session.ended/crashed) or, defensively, when a refreshed row comes back
// already closed — see clearTerminatingIfClosed.
const terminatingIds = ref<Set<string>>(new Set())
function isTerminating(sid: string) { return terminatingIds.value.has(sid) }
function beginTerminating(sid: string) {
  terminatingIds.value = new Set([...terminatingIds.value, sid])
}
function endTerminating(sid: string) {
  if (!terminatingIds.value.has(sid)) return
  const next = new Set(terminatingIds.value); next.delete(sid); terminatingIds.value = next
}
function clearTerminatingIfClosed(row: SessionRow) {
  if (terminatingIds.value.has(row.id) && isClosed(row)) endTerminating(row.id)
}

// B2: a running row whose RUNNING→IDLE hook callback was dropped stays stuck
// at "agent working" forever with no client-side recovery. reap-stale asks the
// backend to reconcile running rows whose pid is actually dead → CRASHED.
// Surfaced as a manual button on the live tabs so the user can self-recover
// instead of being told nothing (the endpoint existed but had no UI entry).
const reaping = ref(false)
async function reapStaleSessions() {
  if (reaping.value) return
  reaping.value = true
  try {
    const { reaped } = await sessionsApi.reapStale()
    await refresh().catch(() => {})
    toast.success(reaped > 0
      ? `Reaped ${reaped} stale session${reaped === 1 ? '' : 's'}`
      : 'No stale sessions found')
  } catch (e) {
    toast.error(`Reap failed: ${formatApiError(e)}`)
  } finally {
    reaping.value = false
  }
}
async function resumeSession(sid: string, ev?: Event) {
  if (ev) ev.stopPropagation()
  if (resumingIds.value.has(sid)) return
  resumingIds.value = new Set([...resumingIds.value, sid])
  try {
    const fresh = await sessionsApi.resume(sid)
    await refresh()
    router.push(`/sessions/${fresh.id}`)
    filter.value = 'active'
    toast.success('Resumed session')
  } catch (e) { toast.error(`Resume failed: ${apiErrorMessage(e)}`) }
  finally {
    const next = new Set(resumingIds.value); next.delete(sid); resumingIds.value = next
  }
}

async function purgeSession(sid: string, ev?: Event) {
  if (ev) ev.stopPropagation()
  const s = rows.value.find(x => x.id === sid)
  if (!s || !isClosed(s)) {
    toast.warn('Stop the live session before permanently deleting it.')
    return
  }
  const label = s?.title || sid.slice(0, 8)
  if (!confirm(
    `Permanently delete "${label}"?\n\n`
    + 'This removes its session record, notifications, and saved output. '
    + 'This cannot be undone.',
  )) return
  purgeTombstones.value = new Set([...purgeTombstones.value, sid])
  rows.value = rows.value.filter(r => r.id !== sid)
  if (activeSid.value === sid) router.push('/sessions')
  try { await sessionsApi.purge(sid) }
  catch (e) { toast.error(`Purge failed: ${apiErrorMessage(e)}`) }
  finally {
    const next = new Set(purgeTombstones.value); next.delete(sid); purgeTombstones.value = next
    await refresh().catch(() => {})
  }
}

async function archiveSession(sid: string, ev?: Event) {
  if (ev) ev.stopPropagation()
  const s = rows.value.find(x => x.id === sid)
  if (!s || !isClosed(s)) return
  try {
    upsertSessionRow(await sessionsApi.setArchived(sid, true))
    if (activeSid.value === sid && !showArchived.value) router.push('/sessions')
    toast.success('Session archived')
  } catch (e) {
    toast.error(`Archive failed: ${formatApiError(e)}`)
  }
}

async function unarchiveSession(sid: string) {
  try {
    upsertSessionRow(await sessionsApi.setArchived(sid, false))
    toast.success('Session restored to History')
  } catch (e) {
    toast.error(`Restore failed: ${formatApiError(e)}`)
  }
}

// Sidebar × on a live row: stop the process (SIGINT→SIGTERM→SIGKILL) but
// keep the row so it lands in History. Contrast with `purgeSession` which
// deletes the row entirely.
async function stopSessionFromTree(sid: string, ev?: Event) {
  if (ev) ev.stopPropagation()
  if (terminatingIds.value.has(sid)) return
  const s = rows.value.find(x => x.id === sid)
  if (!s) return
  const label = s.title || sid.slice(0, 8)
  if (!confirm(`Stop session "${label}"?\n(process is killed, row moves to History)`)) return
  // B1: async_=true returns 202 immediately, then the backend runs the
  // SIGINT→SIGTERM→SIGKILL ladder for up to 15s during which the row stays
  // `running`. Give an immediate cue (toast + the row's × spinner via the
  // terminating guard) so the user doesn't think the click missed and click
  // again. The guard clears when the row actually closes.
  beginTerminating(sid)
  toast.info('Stopping session…')
  try {
    await sessionsApi.stop(sid, true, true)
  } catch (e) {
    endTerminating(sid)
    toast.error(`Stop failed: ${apiErrorMessage(e)}`)
  } finally {
    await refresh().catch(() => {})
  }
}

const purging = ref(false)
async function purgeAllExited() {
  if (purging.value) return
  const exited: SessionRow[] = []
  let offset = 0
  do {
    const page = await sessionsApi.list({
      status: CLOSED_STATUSES,
      type: 'interactive',
      limit: 500,
      offset,
    })
    exited.push(...page.items.filter((s) => !s.archived_at))
    offset += page.items.length
    if (!page.has_more || page.items.length === 0) break
  } while (true)
  if (!exited.length) { toast.info('No unarchived ended sessions.'); return }
  if (!confirm(`Archive ${exited.length} ended session(s)?\nThey remain available under “Show archived”.`)) return
  purging.value = true
  try {
    const result = await sessionsApi.archiveEnded()
    if (activeSessionClosed.value && !showArchived.value) router.push('/sessions')
    await refresh()
    toast.success(`Archived ${result.archived} session(s).`)
  } catch (e) {
    toast.error(`Archive failed: ${apiErrorMessage(e)}`)
  } finally { purging.value = false }
}

const clearingHistory = ref(false)
async function clearHistoryPermanent() {
  if (clearingHistory.value) return
  const n = historyTotal.value
  if (!n) { toast.info('History is already empty.'); return }
  if (!confirm(
    `Permanently delete ALL ${n} ended interactive session(s)?\n\n` +
    `This drops the DB rows, associated notifications, and cached output ` +
    `files. Archived rows are also cleared. This cannot be undone — use ` +
    `"Archive ended" if you might want them back.`
  )) return
  clearingHistory.value = true
  try {
    const result = await sessionsApi.purgeHistory()
    if (activeSessionClosed.value) router.push('/sessions')
    await refresh()
    toast.success(`Deleted ${result.purged} history session(s).`)
  } catch (e) {
    toast.error(`Clear history failed: ${apiErrorMessage(e)}`)
  } finally { clearingHistory.value = false }
}

// ---------------------------------------------------------------------------
// Context menu builder
// ---------------------------------------------------------------------------
function buildMenuItems(s: SessionRow): ContextMenuItem[] {
  const closed = isClosed(s)
  const items: ContextMenuItem[] = []
  items.push({ label: s.pinned ? 'Unpin' : 'Pin to top', icon: '📌', action: () => togglePin(s) })
  items.push({
    label: s.highlighted ? 'Remove highlight' : 'Highlight',
    icon: '⭐', action: () => toggleHighlighted(s),
  })
  if (!closed) items.push({
    label: s.manual_unread ? 'Mark as read' : 'Mark as unread',
    icon: '🔴', action: () => toggleManualUnread(s),
  })
  items.push({ label: '', divider: true })
  if (closed && (
    ((s.agent || 'claude') === 'claude' && !!s.external_session_id && s.jsonl_present !== false)
    || ((s.agent || s.backend) === 'codex' && !!(s.external_session_id || s.rollout_path))
  ) && !s.superseded_by)
    items.push({ label: 'Resume in fresh PTY', icon: '▶', action: () => resumeSession(s.id) })
  items.push({ label: 'Rename', icon: '✎', action: () => renameFromMenu(s.id) })
  if (sessionProjects.value.length > 0) {
    for (const p of sessionProjects.value) {
      if (p.id === s.session_project_id) continue
      items.push({ label: `Move to project: ${p.name}`, icon: '📁',
        action: () => assignSessionToProject(s.id, p.id) })
    }
    if (s.session_project_id) items.push({
      label: 'Unassign from project', icon: '📁',
      action: () => assignSessionToProject(s.id, null),
    })
  }
  if (!closed) {
    items.push({ label: 'Fullscreen terminal', icon: '⛶', action: () => fullscreenFromMenu(s.id) })
    items.push({ label: '', divider: true })
    items.push({ label: 'Kill', icon: '🚫', danger: true, action: () => killFromMenu(s.id) })
  } else {
    items.push({ label: '', divider: true })
    items.push({
      label: s.archived_at ? 'Restore to History' : 'Archive',
      icon: s.archived_at ? '↩' : '📦',
      action: () => s.archived_at ? unarchiveSession(s.id) : archiveSession(s.id),
    })
    items.push({
      label: 'Permanently delete…',
      icon: '🗑', danger: true, action: () => purgeSession(s.id),
    })
  }
  return items
}
function onRowContextMenu(sid: string, ev: MouseEvent) {
  const s = rows.value.find(r => r.id === sid)
  if (!s) return
  ui.openContextMenu(ev.clientX, ev.clientY, buildMenuItems(s), sid)
}

// Recent files popover — reuse ContextMenu for zero extra components
async function openRecentFiles(ev?: MouseEvent) {
  if (!activeSid.value) return
  await fs.loadRecentFiles(activeSid.value, { force: true })
  const items: ContextMenuItem[] = fs.recentFiles.value.length
    ? fs.recentFiles.value.map(f => {
        const short = f.path.length > 56 ? '…' + f.path.slice(-55) : f.path
        const icon = f.tool === 'Write' ? '📝' : f.tool === 'Edit' ? '✎'
          : f.tool === 'MultiEdit' ? '✏️' : f.tool === 'Create' ? '➕' : '·'
        return { label: short, icon, action: () => fs.openPreview(f.path, activeSid.value) }
      })
    : [{ label: 'No files touched yet in this session', disabled: true }]
  const x = ev?.clientX ?? window.innerWidth / 2
  const y = ev?.clientY ?? 100
  ui.openContextMenu(x, y, items)
}

// ---------------------------------------------------------------------------
// Lifecycle / watchers / SSE
// ---------------------------------------------------------------------------
const initialLoading = ref(true)
let refreshDebounce: number | null = null
let rowRefreshDebounce: number | null = null
let changesRefreshDebounce: number | null = null
let readDwellTimer: number | null = null
const pendingRowRefreshes = new Set<string>()
const READ_DWELL_MS = 1200

function cancelReadEngagement() {
  if (readDwellTimer != null) {
    window.clearTimeout(readDwellTimer)
    readDwellTimer = null
  }
  notifStore.setActiveSessionId(null)
}
// A tab can be *visible but not focused* (second monitor, split-screen,
// user in another app). `document.hidden` is false in all of those, so
// gating auto-read on visibility alone silently marked sessions read that
// the user never actually looked at (D2). Require focus too: "attention is
// here" before we treat the session as engaged / auto-read its messages.
function attentionHere(): boolean {
  if (typeof document === 'undefined') return true
  return !document.hidden && document.hasFocus()
}
function beginReadEngagement(sid: string) {
  cancelReadEngagement()
  if (!attentionHere()) return
  readDwellTimer = window.setTimeout(() => {
    readDwellTimer = null
    if (activeSid.value !== sid || !attentionHere()) return
    notifStore.setActiveSessionId(sid)
    if (notifStore.unreadForSession(sid) > 0) {
      notifStore.markSessionRead(sid).catch(() => {})
    }
    clearManualUnreadOnVisit(sid)
  }, READ_DWELL_MS)
}
function scheduleRefresh() {
  if (typeof document !== 'undefined' && document.hidden) return
  if (refreshDebounce != null) return
  refreshDebounce = window.setTimeout(() => {
    refreshDebounce = null
    refresh().catch(() => {})
  }, 250)
}

function eventSessionId(e: CSMEvent): string | null {
  const internal = e.payload?.csm_session_id
  if (typeof internal === 'string' && internal) return internal
  if (e.session_id) {
    const agent = String(e.payload?.agent || e.payload?.backend || '')
    const exact = rows.value.find((row) =>
      row.external_session_id === e.session_id
      && (!agent || (row.agent || row.backend) === agent)
    )
    if (exact) return exact.id
  }
  if (e.project_path) {
    const agent = String(e.payload?.agent || e.payload?.backend || '')
    const candidates = rows.value.filter((row) =>
      row.cwd === e.project_path
      && bucketOf(row) !== 'history'
      && (!agent || (row.agent || row.backend) === agent)
    )
    if (candidates.length === 1) return candidates[0].id
  }
  return null
}

function applyEventPatch(sid: string, e: CSMEvent) {
  const prior = rows.value.find((row) => row.id === sid)
  if (!prior) return
  // C1: session.tool_progress is the high-frequency stream (many frames per
  // running tool). Bumping last_activity_ts on every frame makes
  // sessionRowsEqual false every time → a new rows array → a full filter-tree
  // rebuild + recursive sidebar re-diff per frame. Skip the activity bump for
  // tool_progress: a repeat frame for the same tool (status already running,
  // current_tool unchanged) then hits the equality short-circuit and is a
  // genuine no-op. Meaningful transitions (started / user_sent / assistant_done
  // / idle / ended) still bump activity, and the server value lands via the
  // 180ms scheduleRowRefresh anyway.
  const bumpActivity = e.type !== 'session.tool_progress'
  const next: SessionRow = {
    ...prior,
    last_activity_ts: bumpActivity ? (e.ts || prior.last_activity_ts) : prior.last_activity_ts,
  }
  if (e.type === 'session.started' || e.type === 'message.user_sent'
    || e.type === 'session.tool_progress') {
    next.status = 'running'
  } else if (e.type === 'message.assistant_done' || e.type === 'session.idle'
    || e.type === 'session.interrupted') {
    next.status = 'idle'
    next.current_tool = null
  } else if (e.type === 'session.waiting_auth') {
    next.status = 'waiting_auth'
  } else if (e.type === 'session.waiting_input') {
    next.status = 'waiting_input'
  } else if (e.type === 'session.ended' || e.type === 'session.crashed') {
    next.status = e.type === 'session.crashed' ? 'crashed' : 'exited'
    next.ended_at = e.ts || next.ended_at
    next.current_tool = null
    if (typeof e.payload?.exit_code === 'number') next.exit_code = e.payload.exit_code
  }
  if (e.type === 'session.tool_progress' && typeof e.payload?.tool_name === 'string') {
    next.current_tool = e.payload.tool_name
  }
  if (e.type === 'message.assistant_done'
    && typeof e.payload?.assistant_text === 'string'
    && e.payload.assistant_text) {
    next.last_assistant_msg = e.payload.assistant_text.slice(0, 2000)
  }
  upsertSessionRow(next)
}

async function refreshPendingRows() {
  const ids = [...pendingRowRefreshes]
  pendingRowRefreshes.clear()
  if (!ids.length) return
  const results = await Promise.allSettled(ids.map((sid) => sessionsApi.get(sid)))
  let failed = false
  for (const result of results) {
    if (result.status === 'fulfilled') upsertSessionRow(result.value)
    else failed = true
  }
  // A targeted request can race a just-created/just-purged row. One
  // coalesced snapshot is the safe fallback; do not surface a toast for this
  // normal reconciliation race.
  if (failed) scheduleRefresh()
}

function scheduleRowRefresh(sid: string) {
  pendingRowRefreshes.add(sid)
  if (rowRefreshDebounce != null) return
  rowRefreshDebounce = window.setTimeout(() => {
    rowRefreshDebounce = null
    refreshPendingRows().catch(() => scheduleRefresh())
  }, 180)
}

function handleSessionEvent(e: CSMEvent) {
  if (!isSessionRelevantEvent(e)) return
  const sid = eventSessionId(e)
  if (!sid) {
    scheduleRefresh()
    return
  }
  // Patch the visible state immediately, then verify it with a cheap
  // single-row read. The user sees working/idle/waiting transitions on the
  // event frame instead of after three full-list queries and a tree rebuild.
  applyEventPatch(sid, e)
  scheduleRowRefresh(sid)
}

function scheduleChangesRefresh() {
  if (!activeSid.value || changesRefreshDebounce != null) return
  changesRefreshDebounce = window.setTimeout(() => {
    changesRefreshDebounce = null
    if (activeSid.value) loadSessionChanges(activeSid.value).catch(() => {})
  }, 500)
}
function isSessionRelevantEvent(e: CSMEvent) {
  return e.type.startsWith('session.')
    || e.type === 'message.user_sent'
    || e.type === 'message.assistant_done'
}
function isFileTouchEvent(e: CSMEvent) { return e.type === 'session.tool_progress' }
function onVisibilityChange() {
  if (typeof document !== 'undefined' && !document.hidden) {
    refresh().catch(() => {})
    if (activeSid.value) beginReadEngagement(activeSid.value)
  } else cancelReadEngagement()
}
// Window focus/blur mirror visibility for read-engagement (D2): losing focus
// while the tab stays visible must stop auto-read, regaining it must re-arm
// the dwell. Without these, a visible-but-unfocused tab would never re-engage
// on focus and — combined with the hasFocus() gate — could sit un-engaged.
function onWindowFocus() {
  if (activeSid.value) beginReadEngagement(activeSid.value)
}
function onWindowBlur() {
  cancelReadEngagement()
}
function onWindowResize() { term.scheduleFit() }
function onEscKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && isTerminalMaxed.value) {
    const active = document.activeElement
    const inTerm = active && (active as HTMLElement).closest('.xterm-mount')
    if (inTerm) return
    e.preventDefault()
    ui.setFullscreen(false)
    nextTick(() => term.scheduleFit())
  }
}
let eventSub: { stop: () => void } | null = null

// Watch: switch attached session on route change
watch(activeSid, async (sid) => {
  await nextTick()
  cancelReadEngagement()
  if (sid) {
    const selected = rows.value.find((row) => row.id === sid)
    if (selected && isClosed(selected)) detachTerm()
    else attachTerm(sid)
    beginReadEngagement(sid)
    fs.loadRecentFiles(sid).catch(() => {})
    // Reset changes state — different session, different files.
    changesFiles.value = []
    changesTotalEdits.value = 0
    changesError.value = ''
    changesExpanded.value = false
    loadSessionChanges(sid).catch(() => {})
  } else {
    detachTerm()
    // Fullscreen means "this session fills the canvas" — with no session there
    // is nothing to fill it, and the sidebar is still display:none, so the user
    // lands on a bare "Select a session." with no list AND no ⛶ button (it
    // lives inside the `v-if="activeSession"` toolbar). Esc is the only way
    // out, and nothing on screen says so. Reachable from "← Back to list" on a
    // closed/orphaned session, and from the archive / purge / kill paths that
    // route away on their own.
    ui.setFullscreen(false)
  }
}, { immediate: false })

watch(searchActive, async (active) => {
  // Search is cross-tab and must not silently miss history that has not been
  // paged into the sidebar yet. Fetch every history page once on first use.
  if (active && !historyAllLoaded.value) {
    historyAllLoaded.value = true
    searchHistoryLoading.value = true
    try {
      await refresh()
    } catch (e) {
      historyAllLoaded.value = false
      toast.error(`Search history load failed: ${formatApiError(e)}`)
    } finally {
      searchHistoryLoading.value = false
    }
  }
})

// Ended sessions remain selected so the user can inspect final output and
// changes. Only the live WebSocket/PTY view is torn down.
watch(activeSessionClosed, (closed, wasClosed) => {
  if (closed && !wasClosed && activeSid.value) {
    detachTerm()
  }
})

// Watch: fullscreen toggle → forceSync (not plain fit).
// Rationale: the grid reflow that fullscreen triggers can leave xterm's
// cell dimensions unchanged (e.g., the terminal panel already occupied
// the full remaining canvas width) while the PTY's cached SIGWINCH is
// stale — plain fit()'s "same size, skip" guard then never re-sends
// the resize, and codex/vim/etc. keep drawing at the old size (some
// content ends up past the visible bottom). forceSync bypasses that
// guard so the PTY always sees a fresh SIGWINCH after the toggle.
watch(isTerminalMaxed, () => {
  nextTick(() => requestAnimationFrame(() => term.forceSync()))
})

onMounted(async () => {
  probePasteCapability()
  try {
    await Promise.all([refresh(), loadSessionProjects()])
  } catch {
    // `refresh()` stores the actionable error in `listError`. Continue
    // mounting the retry controls and SSE subscription instead of leaving
    // the page stuck on a permanent skeleton.
  } finally {
    initialLoading.value = false
  }
  // Auto-expand top-level folders on first view
  for (const c of sessionTree.value.children) {
    if (!c.isLeaf && !expandedFolders.value.has(c.fullPath)) {
      const next = new Set(expandedFolders.value); next.add(c.fullPath)
      expandedFolders.value = next
    }
  }
  await nextTick()
  window.addEventListener('resize', onWindowResize)
  window.addEventListener('keydown', onEscKey)
  mobileMedia.addEventListener('change', onMobileMediaChange)
  if (activeSid.value) {
    const selected = activeSession.value
    if (selected && !isClosed(selected)) setTimeout(() => attachTerm(activeSid.value!), 0)
    beginReadEngagement(activeSid.value)
    fs.loadRecentFiles(activeSid.value).catch(() => {})
    loadSessionChanges(activeSid.value).catch(() => {})
  } else {
    const liveInteractive = rows.value.filter(s => s.type === 'interactive' &&
      ['starting', 'running', 'idle', 'waiting_input', 'waiting_auth', 'orphaned'].includes(s.status))
    liveInteractive.sort((a, b) => {
      const ta = new Date(a.last_activity_ts || a.started_at || 0).getTime()
      const tb = new Date(b.last_activity_ts || b.started_at || 0).getTime()
      return tb - ta
    })
    const first = liveInteractive[0]
    // Desktop keeps the convenient auto-select behaviour. On phones the
    // session list is a real master route and should not immediately jump to
    // an arbitrary terminal before the user chooses one.
    if (first && !isMobileViewport.value) selectSession(first.id)
  }
  eventSub = useEventStream({
    onEvent(e) {
      handleSessionEvent(e)
      if (isFileTouchEvent(e) && activeSid.value) {
        fs.loadRecentFiles(activeSid.value, { force: true }).catch(() => {})
        scheduleChangesRefresh()
      }
    },
    onReconnect() { refresh().catch(() => {}) },
  })
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('focus', onWindowFocus)
  window.addEventListener('blur', onWindowBlur)
  document.addEventListener('click', onDocumentClickForPopovers)
})

// Close the Changes popover on outside click. Trigger uses @click.stop
// so clicking the trigger itself doesn't fall through and close.
function onDocumentClickForPopovers(e: MouseEvent) {
  if (!changesExpanded.value) return
  const menu = changesMenuRef.value
  if (menu && !menu.contains(e.target as Node)) {
    changesExpanded.value = false
  }
}

onUnmounted(() => {
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('keydown', onEscKey)
  mobileMedia.removeEventListener('change', onMobileMediaChange)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('focus', onWindowFocus)
  window.removeEventListener('blur', onWindowBlur)
  document.removeEventListener('click', onDocumentClickForPopovers)
  if (refreshDebounce != null) { clearTimeout(refreshDebounce); refreshDebounce = null }
  if (rowRefreshDebounce != null) { clearTimeout(rowRefreshDebounce); rowRefreshDebounce = null }
  pendingRowRefreshes.clear()
  if (changesRefreshDebounce != null) { clearTimeout(changesRefreshDebounce); changesRefreshDebounce = null }
  cancelReadEngagement()
  eventSub?.stop(); eventSub = null
  detachTerm()
})
</script>

<template>
  <div class="sess-page" :class="{ 'mobile-session-detail': Boolean(activeSid) }">
    <!-- Two-tier sticky toolbar (P0.2):
         tier 1 = page title + primary filter (Active/Auto/History) + search
         tier 2 = subordinate tools (grouping, project mgr, purge, refresh) -->
    <div class="toolbar">
      <span class="sess-page-title">Sessions</span>
      <div class="filter-group primary-filter">
        <button :class="{ active: filter === 'active' }" @click="selectFilterTab('active')" title="Live user-driven sessions">Active <span class="tab-count">{{ activeTotal }}</span></button>
        <button :class="{ active: filter === 'auto' }" @click="selectFilterTab('auto')" title="Live workflow-owned sessions">Auto <span class="tab-count">{{ autoTotal }}</span></button>
        <button :class="{ active: filter === 'history' }" @click="selectFilterTab('history')" title="Closed sessions">History <span class="tab-count">{{ historyTotal }}</span></button>
      </div>
      <div class="search-wrap">
        <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
        </svg>
        <input v-model="search" class="search-input"
          aria-label="Search sessions"
          placeholder="Search title, cwd, agent…  agent:codex"
          @focus="revealSidebar" />
        <button v-if="search" type="button" class="search-clear"
          aria-label="Clear session search" title="Clear search"
          @click="search = ''">×</button>
      </div>
    </div>
    <div class="toolbar-sub">
      <button class="primary tb-sub-primary" @click="openCreate">+ New session</button>
      <span class="tb-sep"></span>
      <span class="tb-sub-label">Group:</span>
      <div class="filter-group" title="Group by">
        <button :class="{ active: groupBy === 'project' }" @click="selectGroupBy('project')">by project</button>
        <button :class="{ active: groupBy === 'cwd' }" @click="selectGroupBy('cwd')">by cwd</button>
      </div>
      <span class="tb-sep"></span>
      <button class="tb-icon-btn" @click="showProjectManager = true" title="Manage session projects" aria-label="Manage projects">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span>Projects</span>
      </button>
      <label v-if="filter === 'history'" class="archive-toggle" title="Include archived sessions in History and search">
        <input v-model="showArchived" type="checkbox" />
        Show archived
      </label>
      <button class="tb-icon-btn" :disabled="purging" @click="purgeAllExited" title="Archive all ended sessions" aria-label="Archive ended">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14"/></svg>
        <span>{{ purging ? 'Archiving…' : 'Archive ended' }}</span>
      </button>
      <button v-if="filter === 'history'" class="tb-icon-btn danger" :disabled="clearingHistory"
        @click="clearHistoryPermanent" title="Permanently delete every ended interactive session" aria-label="Clear history">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
        <span>{{ clearingHistory ? 'Clearing…' : 'Clear history' }}</span>
      </button>
      <button v-if="filter !== 'history'" class="tb-icon-btn" :disabled="reaping"
        @click="reapStaleSessions"
        title="Reconcile sessions stuck 'running' after a lost hook callback (dead pid → History)"
        aria-label="Reap stale sessions">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/></svg>
        <span>{{ reaping ? 'Reaping…' : 'Reap stale' }}</span>
      </button>
      <span v-if="refreshing" class="sync-indicator" role="status">Syncing…</span>
      <!-- Without this the only trace of a degraded list is a tooltip
           timestamp that stops advancing. -->
      <span v-else-if="staleSlices.size" class="sync-indicator sync-stale" role="status"
        :title="`Showing the last good data for: ${[...staleSlices].join(', ')}. Retrying on the next refresh.`">
        Partial sync · {{ [...staleSlices].join(', ') }}
      </span>
      <button class="tb-icon-btn" :class="{ spinning: refreshing }"
        :disabled="refreshing" @click="manualRefresh"
        :title="lastRefreshedAt ? `Refresh list · last synced ${lastRefreshedAt.toLocaleTimeString()}` : 'Refresh list'"
        aria-label="Refresh">
        <svg class="refresh-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15A9 9 0 1 1 5.64 5.64L23 10"/></svg>
      </button>
    </div>

    <SessionProjectManagerModal
      :open="showProjectManager"
      @close="showProjectManager = false"
      @updated="loadSessionProjects"
    />

    <ContextMenu
      :visible="contextMenu.visible"
      :x="contextMenu.x"
      :y="contextMenu.y"
      :items="contextMenu.items"
      @close="ui.closeContextMenu"
    />

    <!-- Create modal -->
    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate = false" role="presentation">
    <div class="cf-modal panel modal" role="dialog" aria-modal="true" aria-label="New session">
      <header class="cf-head">
        <h3>New session</h3>
        <button class="cf-close" type="button" @click="showCreate = false" aria-label="Close">✕</button>
      </header>
      <div class="cf-body">
        <section class="cf-field">
          <label class="cf-label" for="cf-cwd">Working directory <span class="cf-req">*</span></label>
          <div class="cf-row">
            <input id="cf-cwd" v-model="form.cwd.value" placeholder="/path/to/project" list="recent-cwds" class="cf-grow" />
            <datalist id="recent-cwds">
              <option v-for="c in recentCwds" :key="c" :value="c" />
            </datalist>
            <button type="button" class="cf-btn" @click="openBrowser">📁 Browse</button>
          </div>
          <div v-if="recentCwds.length" class="cf-hint">
            <span class="cf-hint-label">Recent:</span>
            <span v-for="c in recentCwds.slice(0, 4)" :key="c" class="recent-chip" @click="form.cwd.value = c">{{ c }}</span>
          </div>
        </section>
        <section class="cf-field">
          <label class="cf-label" for="cf-title">Title <span class="cf-optional">optional</span></label>
          <input id="cf-title" v-model="form.title.value" placeholder="Short label" />
        </section>
        <section class="cf-field">
          <label class="cf-label">Agent</label>
          <div class="cf-row">
            <AgentBadge :agent="form.effectiveAgent.value" />
            <AgentSelector
              class="cf-grow"
              :model-value="form.explicitAgent.value"
              :allow-null="true"
              :null-label="`Use default${form.effectiveAgent.value ? ' (' + form.effectiveAgent.value + ')' : ''}`"
              @update:modelValue="form.setExplicitAgent"
            />
          </div>
          <p class="cf-hint" v-if="!form.explicitAgent.value">
            No override — this session runs your default agent
            (<span v-if="form.effectiveAgent.value">{{ form.effectiveAgent.value }}</span>
            <span v-else>loading…</span>).
          </p>
        </section>
        <section class="cf-field">
          <label class="cf-label" for="cf-project">Project</label>
          <div class="cf-row">
            <select id="cf-project" v-model="form.sessionProjectId.value" class="cf-grow" :disabled="showInlineProjectForm">
              <option value="">(unassigned)</option>
              <option v-for="p in sessionProjects" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <button v-if="!showInlineProjectForm" type="button" class="cf-btn"
              title="Create a new project" @click="openInlineProjectForm">+ New project</button>
          </div>
          <div v-if="showInlineProjectForm" class="cf-inline-card">
            <input ref="inlineProjectNameRef" v-model="inlineProjectName"
              placeholder="New project name" class="cf-grow"
              :disabled="creatingInlineProject"
              @keydown.enter.prevent="submitInlineProject"
              @keydown.esc.prevent="cancelInlineProject" />
            <button type="button" class="cf-btn primary"
              :disabled="creatingInlineProject || !inlineProjectName.trim()"
              @click="submitInlineProject">{{ creatingInlineProject ? '…' : 'Create' }}</button>
            <button type="button" class="cf-btn" :disabled="creatingInlineProject"
              @click="cancelInlineProject">Cancel</button>
          </div>
          <div v-if="inlineProjectError" class="cf-error">{{ inlineProjectError }}</div>
        </section>
        <!--
          Schema-driven per-adapter flags. AdapterFlagsPanel reads
          backend.flags_schema and renders every descriptor generically
          (checkbox / select / resume / info). Zero adapter-name branching
          in this file, per M9 design. Adding a new adapter is a backend-
          only change.
        -->
        <section class="cf-field">
          <label class="cf-label">
            {{ form.backend.value?.display_name ?? form.effectiveAgent.value ?? 'Agent' }} flags
          </label>
          <AdapterFlagsPanel
            :agent="form.effectiveAgent.value"
            :argv="form.argv.value"
            :resumable-sessions="resumableRows"
            @update:argv="(v) => { form.argv.value = v; form.markArgvDirty() }"
          />
        </section>
        <section class="cf-field">
          <label class="cf-label" for="cf-argv">Command <span class="cf-optional">advanced</span></label>
          <input id="cf-argv" v-model="form.argv.value" @input="form.markArgvDirty"
            placeholder="claude / codex / bash -i / ..." class="cf-mono" />
        </section>
        <p class="cf-footnote">
          For scheduled or reviewed automation, use the
          <router-link to="/automation">Automation module</router-link> instead.
        </p>
      </div>
      <footer class="cf-actions">
        <button type="button" class="cf-btn" :disabled="form.submitting.value" @click="showCreate = false">Cancel</button>
        <button type="button" class="cf-btn primary"
          :disabled="!form.canSubmit.value"
          @click="createSessionSafe">{{ form.submitting.value ? 'Starting…' : 'Create session' }}</button>
      </footer>
    </div>
    </div>

    <FilePicker :open="showBrowser" mode="dir" :initial-path="form.cwd.value"
      title="Pick session cwd" @close="showBrowser = false" @pick="onPickCwd" />

    <div class="sess-layout"
      :class="{ 'terminal-max': isTerminalMaxed, 'splitter-dragging': isDraggingSplitter, 'mobile-detail': Boolean(activeSid) }"
      :style="isTerminalMaxed ? undefined : { '--sess-grid-cols': sidebarWidth + 'px 6px 1fr' }">

      <!-- Left: session list -->
      <div class="panel sess-list">
        <div v-if="listError" class="sess-list-error" role="alert">
          <span>Could not sync sessions: {{ listError }}</span>
          <button type="button" :disabled="refreshing" @click="manualRefresh">Retry</button>
        </div>
        <div v-if="initialLoading" class="sess-list-skeleton">
          <div v-for="i in 6" :key="i" class="skel-row">
            <div class="skel-line skel-title"></div>
            <div class="skel-line skel-meta"></div>
          </div>
        </div>
        <div v-else-if="searchActive && !searchHistoryLoading && !searchResults.length" class="empty">
          No sessions match "{{ search }}" across any tab.
        </div>
        <div v-else-if="!searchActive && !visibleRows.length" class="empty">
          {{ filter === 'history' ? 'No closed sessions yet.' : 'No sessions matching filter.' }}
        </div>

        <template v-if="searchActive">
          <div class="search-hint">
            <template v-if="searchHistoryLoading">
              Searching all history… {{ searchResults.length }} result{{ searchResults.length === 1 ? '' : 's' }} so far
            </template>
            <template v-else>
              {{ searchResults.length }} result{{ searchResults.length === 1 ? '' : 's' }} across all tabs
            </template>
          </div>
          <SessionTreeNode
            v-for="s in searchResults" :key="s.id"
            :node="{ name: s.title || s.id.slice(0, 8), fullPath: s.id, isLeaf: true, session: s, children: [] }"
            :active-sid="activeSid" :depth="0"
            :is-open="isOpen" :toggle-folder="toggleFolder"
            :leaves-count="() => 1"
            :state-tag="stateTag" :is-waiting-auth="isWaitingAuth"
            :format-time="formatTime"
            :unread-for-session="(sid) => notifStore.unreadForSession(sid)"
            :leaf-mode="isClosed(s) ? 'history' : 'live'"
            :is-resuming="isResuming"
            :is-terminating="isTerminating"
            @select="selectSession" @purge="purgeSession" @archive="archiveSession" @stop="stopSessionFromTree"
            @resume="resumeSession" @contextmenu="onRowContextMenu"
          />
        </template>

        <template v-else-if="filter !== 'history'">
          <SessionTreeNode
            v-for="c in sessionTree.children" :key="c.fullPath"
            :node="c" :active-sid="activeSid" :depth="0"
            :is-open="isOpen" :toggle-folder="toggleFolder"
            :leaves-count="(n) => leavesOf(n).length"
            :state-tag="stateTag" :is-waiting-auth="isWaitingAuth"
            :format-time="formatTime"
            :unread-for-session="(sid) => notifStore.unreadForSession(sid)"
            :session-projects="groupBy === 'project' ? sessionProjects : undefined"
            :assign-project="groupBy === 'project' ? assignSessionToProject : undefined"
            :is-assigning="(sid) => settingProject.has(sid)"
            :is-terminating="isTerminating"
            @select="selectSession" @purge="purgeSession" @archive="archiveSession" @stop="stopSessionFromTree"
            @contextmenu="onRowContextMenu"
          />
        </template>

        <template v-else>
          <SessionTreeNode
            v-for="c in historyTree.children" :key="c.fullPath"
            :node="c" :active-sid="activeSid" :depth="0"
            :is-open="isOpen" :toggle-folder="toggleFolder"
            :leaves-count="(n) => leavesOf(n).length"
            :state-tag="stateTag" :is-waiting-auth="isWaitingAuth"
            :format-time="formatTime"
            :unread-for-session="(sid) => notifStore.unreadForSession(sid)"
            :session-projects="groupBy === 'project' ? sessionProjects : undefined"
            :assign-project="groupBy === 'project' ? assignSessionToProject : undefined"
            :is-assigning="(sid) => settingProject.has(sid)"
            :leaf-mode="'history'"
            :folder-preview-n="c.fullPath === '__recent__' ? RECENT_BUCKET_N : 3"
            :is-resuming="isResuming"
            :show-cwd-in-meta="c.fullPath === '__recent__'"
            @select="selectSession" @purge="purgeSession" @archive="archiveSession" @stop="stopSessionFromTree"
            @resume="resumeSession" @contextmenu="onRowContextMenu"
          />
          <button v-if="listAtLimit" class="history-load-more"
            :disabled="loadingMoreHistory" @click="loadMoreHistory">
            {{ loadingMoreHistory ? 'Loading…' : `Load older history (${Math.max(0, historyTotal - historyLoadedCount)} remaining)` }}
          </button>
          <div v-if="legacyHistoryCapped" class="history-compat-note">
            Showing the newest 500 sessions. This running backend does not support history pagination yet.
          </div>
        </template>
      </div>

      <div v-if="!isTerminalMaxed" class="sess-splitter"
        role="separator" aria-orientation="vertical"
        :aria-valuenow="sidebarWidth" :aria-valuemin="SIDEBAR_MIN" :aria-valuemax="SIDEBAR_MAX"
        title="Drag to resize"
        @pointerdown="startSplitterDrag"></div>

      <!-- Right: active terminal -->
      <div class="panel sess-active">
        <template v-if="activeSession">
          <div class="sess-active-head" :class="{ 'fs-mode': isTerminalMaxed }">
            <TerminalToolbar
              :chips="chipItems"
              :active-sid="activeSid || null"
              :is-terminal-maxed="isTerminalMaxed"
              :recent-files-count="recentFilesCount"
              :show-recent-files-btn="true"
              :overflow-count="fsOverflowSessions.length"
              @select-chip="selectSession"
              @chip-context="({ sid, event }) => onRowContextMenu(sid, event)"
              @overflow-click="onFsOverflowClick"
              @toggle-fullscreen="ui.toggleFullscreen"
              @open-recent-files="openRecentFiles"
            >
              <template #identity>
                <!-- Icon + two-line identity block (P0.4 rebuild — mirrors
                     files.py:310-322 preview shell). Icon column is a
                     32×32 tinted slot; lines column holds title over
                     mono breadcrumb-style cwd + pid + last activity. -->
                <div class="head-identity">
                  <button type="button" class="mobile-session-back" aria-label="Back to sessions"
                    title="Back to sessions" @click="backToMobileSessionList">←</button>
                  <div class="head-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="4 17 10 11 4 5"/>
                      <line x1="12" y1="19" x2="20" y2="19"/>
                    </svg>
                  </div>
                  <div class="head-lines">
                    <input v-if="editingTitle"
                      v-model="editTitleVal" class="name name-edit"
                      @keydown.enter.prevent="saveTitle"
                      @keydown.esc.prevent="cancelEditTitle"
                      @blur="saveTitle" placeholder="session title" autofocus />
                    <span v-else class="name-wrap"
                      @mouseenter="onTitleEnter"
                      @mouseleave="onTitleLeave">
                      <span class="name editable" @click="startEditTitle" title="Click to rename">
                        {{ activeSession.title || activeSession.id.slice(0, 8) }}
                        <span class="rename-hint">✎</span>
                      </span>
                      <!-- Fullscreen-only details popover. Displayed on hover;
                           windowed mode already surfaces most of this via the
                           sidebar row so we skip it there to avoid noise. -->
                      <div v-if="showTitleDetails && ui.isTerminalMaxed"
                        class="title-details-popover"
                        role="tooltip">
                        <div class="tdp-row"><span class="tdp-k">id</span><span class="tdp-v mono">{{ activeSession.id }}</span></div>
                        <div class="tdp-row"><span class="tdp-k">agent</span><span class="tdp-v mono">{{ activeSession.agent || activeSession.backend || 'claude' }}</span></div>
                        <div v-if="activeSession.external_session_id" class="tdp-row">
                          <span class="tdp-k">agent id</span>
                          <span class="tdp-v mono">{{ activeSession.external_session_id }}</span>
                        </div>
                        <div class="tdp-row"><span class="tdp-k">status</span><span class="tdp-v mono">{{ activeSession.status }}</span></div>
                        <div class="tdp-row"><span class="tdp-k">cwd</span><span class="tdp-v mono">{{ activeSession.cwd }}</span></div>
                        <div class="tdp-row"><span class="tdp-k">started</span><span class="tdp-v mono">{{ fmtAbsTs(activeSession.started_at) }}</span></div>
                        <div class="tdp-row"><span class="tdp-k">duration</span><span class="tdp-v mono">{{ sessionDuration() }}</span></div>
                        <div v-if="activeSession.last_activity_ts" class="tdp-row">
                          <span class="tdp-k">last activity</span>
                          <span class="tdp-v mono">{{ fmtAbsTs(activeSession.last_activity_ts) }}</span>
                        </div>
                        <div v-if="activeSession.pid" class="tdp-row">
                          <span class="tdp-k">pid</span>
                          <span class="tdp-v mono">{{ activeSession.pid }}</span>
                        </div>
                        <div v-if="activeSession.exit_code != null" class="tdp-row">
                          <span class="tdp-k">exit</span>
                          <span class="tdp-v mono" :class="{ 'tdp-exit-nonzero': activeSession.exit_code !== 0 }">{{ activeSession.exit_code }}</span>
                        </div>
                        <div v-if="activeSession.current_tool" class="tdp-row">
                          <span class="tdp-k">now</span>
                          <span class="tdp-v mono">{{ activeSession.current_tool }}</span>
                        </div>
                        <div v-if="activeSession.last_assistant_msg" class="tdp-row tdp-row-block">
                          <span class="tdp-k">last msg</span>
                          <span class="tdp-v tdp-quote">{{ activeSession.last_assistant_msg.slice(0, 200) }}{{ activeSession.last_assistant_msg.length > 200 ? '…' : '' }}</span>
                        </div>
                      </div>
                    </span>
                    <span class="state mono">
                      {{ activeSession.cwd }}<span v-if="activeSession.pid"> · pid {{ activeSession.pid }}</span>
                    </span>
                  </div>
                  <AgentBadge :agent="activeSession.agent || activeSession.backend || 'claude'" :compact="true" />
                  <span :class="['head-state-chip', stateTag(activeSession.status)]"
                    :title="`Agent/process state: ${activeSession.status}`">
                    {{ activeSession.status === 'running' ? 'agent working'
                      : activeSession.status === 'idle' || activeSession.status === 'waiting_input' ? 'waiting for input'
                      : activeSession.status === 'waiting_auth' ? 'permission needed'
                      : activeSession.status }}
                  </span>
                  <span v-if="!activeSessionClosed && !activeSessionOrphaned"
                    class="head-connection-chip"
                    :class="terminalConnectionState"
                    :title="`Browser terminal connection: ${terminalConnectionState}`">
                    {{ terminalConnectionState }}
                  </span>
                </div>
              </template>
              <template #extra-actions>
                <label v-if="!activeSessionClosed && groupBy === 'project'" class="proj-picker"
                  title="Move this session to a project">
                  <span class="proj-picker-label">Project:</span>
                  <select :value="activeSession.session_project_id || ''"
                    :disabled="settingProject.has(activeSession.id)"
                    @change="assignSessionToProject(activeSession.id, ($event.target as HTMLSelectElement).value || null)">
                    <option value="">(unassigned)</option>
                    <option v-for="p in sessionProjects" :key="p.id" :value="p.id">{{ p.name }}</option>
                  </select>
                </label>
                <!-- Changes menu: trigger toggles a popover with the
                     list of files claude modified in this session.
                     Clicking a file opens THAT file's whole-file diff
                     in a new tab. Replaces the previous Recent Files
                     `📄` button in TerminalToolbar (modified files is
                     the more useful subset of touched files). -->
                <div class="changes-menu"
                  ref="changesMenuRef">
                  <button class="changes-trigger" :class="{ open: changesExpanded }"
                    @click.stop="toggleSessionChanges"
                    :title="changesError ? 'Changes unavailable' : `${changesFiles.length} files · ${changesTotalEdits} edits`">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <polyline points="16 18 22 12 16 6"/>
                      <polyline points="8 6 2 12 8 18"/>
                    </svg>
                    Changes
                    <span v-if="changesLoading" class="changes-count">…</span>
                    <span v-else-if="changesError" class="changes-count error">!</span>
                    <span v-else class="changes-count">{{ changesTotalEdits }}</span>
                  </button>
                  <div v-if="changesExpanded" class="changes-popover" @click.stop>
                    <div class="changes-popover-head">
                      <span class="title">Files modified this session</span>
                      <span class="subtitle">{{ changesFiles.length }} files · {{ changesTotalEdits }} edits</span>
                      <button class="close-btn" @click="changesExpanded = false" aria-label="Close">×</button>
                    </div>
                    <div class="changes-popover-body">
                      <div v-if="changesLoading" class="changes-empty">Loading agent-recorded changes…</div>
                      <div v-else-if="changesError" class="changes-empty error">
                        Could not load changes: {{ changesError }}
                        <button type="button" @click="loadSessionChanges(activeSession.id)">Retry</button>
                      </div>
                      <div v-else-if="changesFiles.length === 0" class="changes-empty">
                        No agent-recorded file edits in this run.
                      </div>
                      <button v-for="f in changesFiles" :key="f.path" class="change-file-row"
                        @click="openDiffView(f.path, activeSession.id); changesExpanded = false"
                        :title="`${f.path}\nClick to view whole-file diff in a new tab`">
                        <span class="filecol">
                          <span class="filename mono">{{ fileBasename(f.path) }}</span>
                          <span class="filedir mono">{{ fileParent(f.path) }}</span>
                        </span>
                        <span class="metacol">
                          <span class="change-kind">{{ f.change_kind }}</span>
                          <span class="line-delta"><b>+{{ f.additions }}</b> <i>-{{ f.deletions }}</i></span>
                          <span class="open-hint" aria-hidden="true">↗</span>
                        </span>
                      </button>
                    </div>
                    <div class="changes-popover-foot">
                      <span>Agent-recorded edits in this CSM run; shell/manual changes may not appear.</span>
                      <button v-if="changesFiles.length > 1" type="button"
                        @click="openAllDiffView(activeSession.id)">View combined diff ↗</button>
                    </div>
                  </div>
                </div>
                <button v-if="!activeSessionClosed"
                  :disabled="isTerminating(activeSession.id)"
                  @click="killSession">{{ isTerminating(activeSession.id) ? 'Killing…' : 'Kill' }}</button>
              </template>
            </TerminalToolbar>
          </div>

          <!-- Ended (exited / crashed) and Orphaned states share the
               .state-card primitive from style.css. Same shape as file
               preview state cards — one visual language across all "no
               live PTY" surfaces. -->
          <div v-if="activeSessionClosed" class="ended-session">
          <div class="state-card tone-neutral ended-summary">
            <div class="state-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
              </svg>
            </div>
            <h2>Session ended</h2>
            <div class="state-body">
              <span :class="['tag', stateTag(activeSession.status)]">{{ activeSession.status }}</span>
              <span v-if="activeSession.ended_at"> · ended {{ formatTime(activeSession.ended_at) }}</span>
              <span v-if="activeSession.exit_code != null"> · exit code <code>{{ activeSession.exit_code }}</code></span>
            </div>
            <div class="state-actions">
              <button v-if="activeSessionResumable" class="primary"
                :disabled="isResuming(activeSession.id)"
                @click="resumeSession(activeSession.id)">
                {{ isResuming(activeSession.id) ? 'Resuming…' : '▶ Resume in fresh PTY' }}
              </button>
              <button v-if="activeSession.archived_at" @click="unarchiveSession(activeSession.id)">Restore to History</button>
              <button v-else @click="archiveSession(activeSession.id)">Archive</button>
              <button class="danger-text" @click="purgeSession(activeSession.id)">Permanently delete…</button>
              <button @click="router.push('/sessions')">← Back to list</button>
            </div>
          </div>
          <SessionOutputViewer :sid="activeSession.id" />
          </div>
          <div v-else-if="activeSessionOrphaned" class="state-card tone-warn">
            <div class="state-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
            </div>
            <h2>CSM lost the handle to this process</h2>
            <div class="state-body">
              <span class="tag orphaned">orphaned</span>
              — pid <code>{{ activeSession.pid || 'unknown' }}</code>
              <br><br>
              The backend was restarted, so CSM no longer owns the PTY.
              <b>The agent process may still be alive</b> — but CSM can't
              tell whether it's abandoned or something you're actively
              driving from another terminal. To avoid nuking a live
              conversation, CSM will <b>not</b> touch this pid.
              <ul>
                <li>Still using this agent process elsewhere → do nothing here.</li>
                <li>Really abandoned → run
                  <code>kill {{ activeSession.pid || '&lt;pid&gt;' }}</code>
                  in your shell; once dead, this row becomes resumable.</li>
                <li>Don't need the history entry → purge from the list.</li>
              </ul>
            </div>
            <div class="state-actions">
              <button @click="router.push('/sessions')">← Back to list</button>
            </div>
          </div>
          <div v-else class="term-wrap">
            <div class="term-area">
              <div class="term-body">
                <div ref="termRef" class="xterm-mount"></div>
                <div v-if="terminalConnectionState !== 'connected'" class="term-connecting">
                  <div class="term-connecting-spinner"></div>
                  <div class="term-connecting-label">
                    <template v-if="terminalConnectionState === 'reconnecting'">
                      Connection lost. Reconnecting
                      <span v-if="terminalReconnectAttempt"> (attempt {{ terminalReconnectAttempt }})</span>…
                    </template>
                    <template v-else-if="terminalConnectionState === 'disconnected' && terminalDisconnectReason === 'ended'">
                      This session has ended — its live terminal is gone.
                      Resume it in a fresh PTY, or open History to read its output.
                    </template>
                    <template v-else-if="terminalConnectionState === 'disconnected' && terminalDisconnectReason === 'unauthorized'">
                      Not authorized to attach to this terminal.
                    </template>
                    <template v-else-if="terminalConnectionState === 'disconnected'">
                      Terminal disconnected.
                      <button type="button" @click="term.reconnect()">Reconnect now</button>
                    </template>
                    <template v-else>
                      Connecting to <b>{{ activeSession.title || activeSession.id.slice(0, 8) }}</b>…
                    </template>
                  </div>
                  <button v-if="terminalConnectionState === 'reconnecting'"
                    type="button" @click="term.reconnect()">Retry now</button>
                </div>
              </div>
              <div class="nav-keys" :class="{ collapsed: !navKeysExpanded }">
                <button class="nav-toggle"
                  :title="navKeysExpanded ? 'Hide click-send keys' : 'Show click-send keys'"
                  @mousedown.prevent @click="toggleNavKeys">
                  ⌨<span v-if="!navKeysExpanded" class="chev">▸</span>
                </button>
                <template v-if="navKeysExpanded">
                  <div class="key-group nav">
                    <button class="mobile-key" @mousedown.prevent @click="sendEsc" title="Escape">Esc</button>
                    <button class="mobile-key" @mousedown.prevent @click="sendCtrlC" title="Interrupt · Ctrl-C">^C</button>
                    <button class="mobile-key" @mousedown.prevent @click="sendCtrlD" title="EOF · Ctrl-D">^D</button>
                    <button @mousedown.prevent @click="sendUp" title="↑">↑</button>
                    <button @mousedown.prevent @click="sendDown" title="↓">↓</button>
                    <button @mousedown.prevent @click="sendLeft" title="←">←</button>
                    <button @mousedown.prevent @click="sendRight" title="→">→</button>
                    <button @mousedown.prevent @click="sendEnter" title="⏎">⏎</button>
                    <button @mousedown.prevent @click="sendTab" title="⇥">⇥</button>
                    <button @mousedown.prevent @click="sendMode" title="⇧⇥">⇧⇥</button>
                    <button class="mobile-key paste-key" @mousedown.prevent @click="pasteFromClipboard" title="Paste from clipboard">Paste</button>
                  </div>
                  <span class="key-hints">
                    <span class="hint hint-clip mono"
                      :class="{ warn: pasteHintWarn }" :title="pasteHintTitle">
                      {{ clipHintText }}
                    </span>
                  </span>
                </template>
              </div>
            </div>
          </div>
          <!-- Changes panel moved to the top-right popover in the toolbar
               (see `changes-menu` in the TerminalToolbar #extra-actions slot).
               Bottom-of-terminal placement felt intrusive, especially with
               a long file list. -->
        </template>
        <div v-else-if="initialLoading" class="sess-right-skeleton">
          <div class="skel-header">
            <div class="skel-line skel-title" style="width: 200px"></div>
          </div>
          <div class="skel-terminal-body"></div>
        </div>
        <div v-else class="empty" style="margin: auto;">Select a session.</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sess-page { display: flex; flex-direction: column; height: 100%; }
.tab-count {
  display: inline-flex; min-width: 17px; height: 17px;
  align-items: center; justify-content: center;
  margin-left: 3px; padding: 0 4px; border-radius: 9px;
  background: var(--canvas); color: var(--ink-mute);
  font: 10px 'Geist Mono', monospace;
}
.filter-group button.active .tab-count {
  background: color-mix(in srgb, var(--card) 18%, transparent);
  color: inherit;
}
.search-wrap .search-input { padding-right: 32px; }
.search-clear {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  width: 24px; height: 24px; padding: 0;
  border: 0; border-radius: 5px;
  color: var(--ink-mute); background: transparent;
  font-size: 17px; line-height: 1; cursor: pointer;
}
.search-clear:hover { color: var(--ink); background: var(--canvas); }
.sync-indicator {
  margin-left: auto;
  color: var(--ink-mute);
  font: 10px 'Geist Mono', monospace;
  white-space: nowrap;
}
.sync-indicator.sync-stale {
  color: var(--pastel-yellow-fg);
  background: var(--pastel-yellow-bg);
  padding: 2px 6px;
  border-radius: 4px;
}
.tb-icon-btn.spinning .refresh-icon {
  animation: refresh-spin 850ms linear infinite;
}
@keyframes refresh-spin { to { transform: rotate(360deg); } }
.archive-toggle {
  display: inline-flex; align-items: center; gap: 5px;
  margin-left: auto; color: var(--ink-mute); white-space: nowrap;
}
.archive-toggle input { margin: 0; }

/* Create modal */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex; align-items: center; justify-content: center; z-index: 99;
}
.cf-modal {
  width: 580px; max-width: 92vw; max-height: 90vh;
  padding: 0;
  display: flex; flex-direction: column;
}
.cf-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
}
.cf-head h3 { margin: 0; font-family: 'Newsreader', serif; font-weight: 500; font-size: 18px; color: var(--ink); }
.cf-close {
  background: transparent; border: none;
  font-size: 18px; line-height: 1;
  color: var(--ink-mute); cursor: pointer;
  padding: 4px 8px; border-radius: 4px;
}
.cf-close:hover { background: var(--canvas); color: var(--ink); }
.cf-body { padding: 16px 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; }
.cf-field { display: flex; flex-direction: column; gap: 6px; }
.cf-label { font-size: 12px; color: var(--ink-mute); font-weight: 500; letter-spacing: 0.2px; }
.cf-optional { color: var(--ink-faint); font-weight: 400; font-size: 11px; margin-left: 4px; }
.cf-req { color: var(--pastel-red-fg); font-weight: 400; margin-left: 2px; }
.cf-row { display: flex; gap: 8px; align-items: center; }
.cf-grow { flex: 1; min-width: 0; }
.cf-mono { font-family: 'Geist Mono', monospace; font-size: 12px; }
.cf-btn {
  padding: 6px 12px; font-size: 12px;
  background: var(--card); color: var(--ink);
  border: 1px solid var(--border); border-radius: 4px;
  cursor: pointer; white-space: nowrap;
  transition: border-color 120ms, background 120ms;
}
.cf-btn:hover:not(:disabled) { border-color: var(--ink); background: var(--canvas); }
.cf-btn.primary { background: var(--ink); color: var(--card); border-color: var(--ink); }
.cf-btn.primary:hover:not(:disabled) { opacity: 0.9; }
.cf-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.cf-hint { font-size: 11px; color: var(--ink-faint); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.cf-hint-label { color: var(--ink-mute); }
.cf-inline-card {
  display: flex; gap: 6px; align-items: center;
  padding: 8px; background: var(--canvas);
  border: 1px dashed var(--border); border-radius: 4px;
}
.cf-error { font-size: 11px; color: var(--pastel-red-fg); }
.cf-flags {
  display: flex; flex-wrap: wrap; gap: 8px 14px;
  padding: 10px 12px; background: var(--canvas);
  border: 1px solid var(--border); border-radius: 4px;
}
.cf-flag { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-mute); cursor: pointer; }
.cf-flag input[type="checkbox"] { margin: 0; }
.cf-flag select { font-size: 12px; padding: 2px 4px; }
.cf-flag-name { color: var(--ink); font-weight: 500; }
.cf-footnote {
  font-size: 11px; color: var(--ink-faint);
  margin: 4px 0 0; padding-top: 10px;
  border-top: 1px dashed var(--border);
}
.cf-footnote a { color: var(--ink); text-decoration: underline; }
.cf-actions {
  display: flex; gap: 8px; justify-content: flex-end;
  padding: 12px 20px; border-top: 1px solid var(--border);
  background: var(--card);
}

/* Layout grid — the resizable sidebar width is bound via the
 * `--sess-grid-cols` custom prop on the element (see template). Using a
 * CSS var (not `style="grid-template-columns: ..."`) lets .terminal-max
 * and the mobile media query override without `!important`, because they
 * reset the grid-template-columns property directly. */
.sess-layout {
  flex: 1; display: grid;
  grid-template-columns: var(--sess-grid-cols, 280px 6px 1fr);
  gap: 0; padding: 0 20px 20px; min-height: 0;
}
.sess-splitter {
  cursor: col-resize; background: transparent;
  border-left: 1px solid var(--border);
  border-right: 1px solid var(--border);
  transition: background 120ms;
}
.sess-splitter:hover,
.sess-layout.splitter-dragging .sess-splitter { background: var(--canvas); }
.sess-layout.splitter-dragging { user-select: none; cursor: col-resize; }
.sess-layout.splitter-dragging * { pointer-events: none; }
.sess-layout.splitter-dragging .sess-splitter { pointer-events: auto; }
.mobile-session-back { display: none; }
@media (max-width: 640px) {
  .sess-page > .toolbar {
    grid-template-columns: auto 1fr;
    gap: 8px; padding: 8px;
  }
  .sess-page > .toolbar .primary-filter { justify-self: end; }
  .sess-page > .toolbar .search-wrap {
    grid-column: 1 / -1; max-width: none; justify-self: stretch;
  }
  .sess-page > .toolbar-sub {
    position: static; padding: 6px 8px;
    overflow-x: auto; white-space: nowrap;
  }
  .sess-page > .toolbar-sub .tb-sub-label,
  .sess-page > .toolbar-sub .tb-sep { display: none; }
  .archive-toggle { margin-left: 0; }
  .sess-layout {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(0, 1fr);
    padding: 0 8px 8px;
    gap: 0;
  }
  .sess-splitter { display: none; }
  .sess-layout:not(.mobile-detail) .sess-active { display: none; }
  .sess-layout.mobile-detail { padding: 0; }
  .sess-layout.mobile-detail .sess-list,
  .sess-layout.mobile-detail .sess-splitter { display: none; }
  .sess-layout.mobile-detail .sess-active {
    display: flex;
    border-width: 0;
    border-radius: 0;
  }
  .sess-page.mobile-session-detail > .toolbar,
  .sess-page.mobile-session-detail > .toolbar-sub { display: none; }
  .mobile-session-back {
    flex: 0 0 38px;
    width: 38px;
    height: 38px;
    display: grid;
    place-items: center;
    padding: 0;
    border-radius: 10px;
    font-size: 19px;
  }
  .cf-modal {
    width: 100vw;
    max-width: 100vw;
    max-height: calc(100dvh - 12px);
    align-self: flex-end;
    border-radius: 16px 16px 0 0;
  }
  .cf-body { padding: 14px; }
  .cf-row { align-items: stretch; }
  .cf-row > .cf-btn { min-height: 42px; }
}
.sess-layout.terminal-max { grid-template-columns: 1fr; }
.sess-layout.terminal-max .sess-list,
.sess-layout.terminal-max .sess-splitter { display: none; }

/* Sidebar list */
.sess-list { overflow: auto; padding: 0; }
.sess-list-error {
  position: sticky; top: 0; z-index: 3;
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px;
  color: var(--pastel-red-fg); background: var(--pastel-red-bg);
  border-bottom: 1px solid color-mix(in srgb, var(--pastel-red-fg) 25%, transparent);
  font-size: 11px;
}
.sess-list-error span {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-list-error button {
  flex: 0 0 auto; padding: 3px 7px;
  color: inherit; background: var(--card);
  border: 1px solid currentColor; border-radius: 4px;
}
.sess-list-skeleton { padding: 8px 0; }
.skel-row {
  display: flex; flex-direction: column; gap: 6px;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
}
.skel-line {
  height: 12px;
  background: linear-gradient(90deg, var(--canvas) 0%, var(--border) 40%, var(--canvas) 80%);
  background-size: 200% 100%;
  border-radius: 3px;
  animation: skel-shimmer 1.4s ease-in-out infinite;
}
.skel-title { width: 65%; height: 14px; }
.skel-meta { width: 40%; height: 10px; }
@keyframes skel-shimmer {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
.sess-right-skeleton { flex: 1 1 auto; display: flex; flex-direction: column; }
.sess-right-skeleton .skel-header {
  padding: 14px 20px; border-bottom: 1px solid var(--border);
}
.sess-right-skeleton .skel-terminal-body { flex: 1; background: #1A1A1A; margin: 0; }
.history-load-more {
  display: block; width: calc(100% - 20px);
  margin: 10px; padding: 8px;
  color: var(--ink-mute); background: var(--canvas);
  border: 1px solid var(--border); border-radius: 6px;
}
.history-compat-note {
  margin: 8px 10px 12px; padding: 8px 10px;
  color: var(--pastel-yellow-fg); background: var(--pastel-yellow-bg);
  border-radius: 6px; font-size: 11px; line-height: 1.45;
}
.search-hint {
  padding: 6px 16px; font-size: 11px;
  color: var(--ink-mute); background: var(--canvas);
  border-bottom: 1px solid var(--border);
  font-family: 'Geist Mono', monospace;
}

/* Active pane */
.sess-active { display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
/* P0.4 rebuild — mirrors files.py preview shell (icon / title / breadcrumb). */
.sess-active-head {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 20px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
}
.sess-active-head.fs-mode {
  gap: 6px; padding: 8px 12px 8px 16px;
  overflow: hidden;
}
/* Identity block = icon + two-line stack (title over cwd meta). */
.head-identity {
  display: flex; align-items: center; gap: 12px;
  flex: 1; min-width: 0;
}
.head-icon {
  flex: 0 0 32px;
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 6px;
  background: var(--accent-soft-bg);
  color: var(--accent);
}
.head-icon svg { width: 18px; height: 18px; }
.head-lines {
  display: flex; flex-direction: column; gap: 2px;
  min-width: 0; flex: 1;
}
.head-state-chip,
.head-connection-chip {
  flex: 0 0 auto; padding: 3px 7px; border-radius: 999px;
  font: 10px 'Geist Mono', monospace;
  color: var(--ink-mute); background: var(--canvas);
  border: 1px solid var(--border);
  white-space: nowrap;
}
.head-state-chip.running { color: var(--pastel-blue-fg); background: var(--pastel-blue-bg); }
.head-state-chip.waiting { color: var(--pastel-yellow-fg); background: var(--pastel-yellow-bg); }
.head-state-chip.crashed { color: var(--pastel-red-fg); background: var(--pastel-red-bg); }
.head-connection-chip.connected { color: var(--pastel-green-fg); background: var(--pastel-green-bg); }
.head-connection-chip.reconnecting,
.head-connection-chip.connecting { color: var(--pastel-yellow-fg); background: var(--pastel-yellow-bg); }
.sess-active-head .name-wrap { position: relative; display: inline-block; }
.sess-active-head .name {
  font-family: 'Newsreader', serif; font-weight: 500; font-size: 17px;
  color: var(--ink); line-height: 1.2;
}
.sess-active-head .name.editable { cursor: pointer; padding: 2px 4px; border-radius: 4px; }
.sess-active-head .name.editable:hover { background: var(--canvas); }
.sess-active-head .name.editable .rename-hint { opacity: 0; margin-left: 4px; color: var(--ink-mute); font-size: 12px; }
.sess-active-head .name.editable:hover .rename-hint { opacity: 1; }
/* Fullscreen title-details popover. Non-modal; hover-only. Positioned
   below the title with a small offset. Content is a compact k/v table
   (Geist Mono values, muted keys) so it reads as a "quick facts" card
   rather than a modal. Elevated with shadow-md so it's clearly above
   the terminal underneath but not a full backdrop. */
.sess-active-head .title-details-popover {
  position: absolute; top: 100%; left: 0;
  z-index: 50;
  margin-top: 6px;
  min-width: 340px; max-width: 480px;
  padding: 10px 12px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-md);
  display: grid; gap: 4px;
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 12px;
  color: var(--ink);
  pointer-events: none; /* hover-only, no click target inside */
}
.sess-active-head .tdp-row {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: 8px;
  align-items: baseline;
}
.sess-active-head .tdp-row-block {
  grid-template-columns: 88px 1fr;
  align-items: start;
}
.sess-active-head .tdp-k {
  color: var(--ink-faint);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 10px;
  font-weight: 600;
}
.sess-active-head .tdp-v {
  color: var(--ink);
  overflow-wrap: anywhere;
  word-break: break-all;
}
.sess-active-head .tdp-v.mono {
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
}
.sess-active-head .tdp-exit-nonzero { color: var(--pastel-red-fg); font-weight: 500; }
.sess-active-head .tdp-quote {
  font-family: 'Newsreader', serif;
  font-style: italic;
  color: var(--ink-2, var(--ink-mute));
  font-size: 13px;
  line-height: 1.4;
}
.sess-active-head .name-edit {
  font-family: 'Newsreader', serif; font-weight: 500; font-size: 17px;
  border: 1px solid var(--border); border-radius: 4px;
  padding: 2px 6px; min-width: 200px;
  background: var(--card); color: var(--ink);
}
.sess-active-head .state {
  color: var(--ink-mute); font-size: 11px;
  font-family: 'Geist Mono', monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-active-head .proj-picker {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; color: var(--ink-mute);
}
.sess-active-head .proj-picker-label { color: var(--ink-faint); }
.sess-active-head .proj-picker select {
  font-size: 12px; padding: 2px 4px;
  background: var(--card); color: var(--ink);
  border: 1px solid var(--border); border-radius: 4px;
  cursor: pointer;
}
.sess-active-head .proj-picker select:disabled { opacity: 0.5; cursor: wait; }

.term-wrap {
  flex: 1; min-height: 0; display: flex;
  /* Clip xterm to the parent panel's rounded bottom so the corners
     don't render as a rectangular black cutout inside the rounded shell. */
  border-radius: 0 0 12px 12px;
  overflow: hidden;
}
.xterm-mount { height: 100%; }
.term-area { display: flex; flex-direction: column; flex: 1; min-width: 0; height: 100%; }
.term-body {
  flex: 1; min-height: 0;
  /* Match xterm's default background exactly; the previous #1A1A1A with
     12px padding created a visible dead border of raw slab-color around
     the terminal grid. */
  background: #0b0b0b;
  padding: 8px 4px 4px 8px;
  overflow: hidden; position: relative;
}
@media (prefers-color-scheme: dark) { .term-body { background: #000; } }
:root[data-theme="dark"] .term-body { background: #000; }
.term-connecting {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 12px; background: #1A1A1A; color: #DCDCDC;
  z-index: 5;
}
.term-connecting-spinner {
  width: 24px; height: 24px;
  border: 2px solid rgba(220, 220, 220, 0.2);
  border-top-color: #DCDCDC; border-radius: 50%;
  animation: term-connecting-spin 0.8s linear infinite;
}
.term-connecting-label { font-family: 'Geist Mono', monospace; font-size: 12px; opacity: 0.75; }
.term-connecting-label b { font-family: 'Newsreader', serif; font-weight: 500; opacity: 1; }
.term-connecting button {
  background: transparent; color: #dcdcdc;
  border-color: rgba(255, 255, 255, 0.35);
}
@keyframes term-connecting-spin { to { transform: rotate(360deg); } }

.nav-keys {
  display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
  padding: 6px 12px;
  background: var(--card); border-top: 1px solid var(--border);
}
.nav-keys.collapsed { padding: 3px 10px; }
.nav-keys button { font-family: 'Geist Mono', monospace; font-size: 11px; padding: 2px 8px; min-width: 30px; }
.nav-keys .mobile-key { display: none; }
.nav-keys .nav-toggle { min-width: 32px; }
.nav-keys .nav-toggle .chev { margin-left: 4px; font-size: 9px; color: var(--ink-faint); }
.nav-keys .key-group { display: flex; gap: 3px; align-items: center; }
.nav-keys .key-hints { margin-left: auto; display: flex; gap: 10px; align-items: center; }
.nav-keys .hint { font-size: 11px; color: var(--ink-faint); }
.nav-keys .hint-clip {
  padding: 2px 8px; border-radius: 4px;
  background: var(--card); color: var(--ink);
  border: 1px solid var(--border); font-weight: 600;
}
.nav-keys .hint-clip.warn {
  background: var(--pastel-red-bg);
  color: var(--pastel-red-fg);
  border-color: var(--pastel-red-fg);
}

@media (max-width: 640px) {
  .sess-active-head { padding: 7px 8px; }
  .head-identity { gap: 7px; }
  .head-icon { display: none; }
  .head-lines .state { max-width: 42vw; }
  .head-state-chip { display: none; }
  .head-identity :deep(.agent-badge) { display: none; }
  .sess-active-head .proj-picker,
  .sess-active-head .changes-menu { display: none; }
  .term-wrap { border-radius: 0; }
  .term-body { padding: 4px 2px 2px 4px; }
  .nav-keys {
    flex-wrap: nowrap;
    gap: 4px;
    padding: 5px 6px;
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scrollbar-width: none;
  }
  .nav-keys::-webkit-scrollbar { display: none; }
  .nav-keys.collapsed { padding: 4px 6px; }
  .nav-keys .key-group { flex: 0 0 auto; }
  .nav-keys button {
    min-width: 42px;
    min-height: 40px;
    padding: 5px 9px;
    font-size: 12px;
  }
  .nav-keys .mobile-key { display: inline-flex; align-items: center; justify-content: center; }
  .nav-keys .paste-key { min-width: 58px; }
  .nav-keys .key-hints { display: none; }
}

/* .sess-ended-card / .sess-orphan-card removed 2026-07-25 —
   consolidated to global `.state-card` primitive in style.css
   (see UI redesign spec §P0.6). */
/* .tag.orphaned lives in global style.css now — consolidated 2026-07-25. */

.ended-session {
  min-height: 0; flex: 1;
  display: flex; flex-direction: column; overflow: auto;
}
.ended-summary {
  flex: 0 0 auto; min-height: 0;
  padding-top: 22px; padding-bottom: 18px;
}
.danger-text { color: var(--pastel-red-fg); }

.recent-chip {
  display: inline-block;
  background: var(--canvas); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 6px; margin-right: 4px;
  cursor: pointer; font-family: 'Geist Mono', monospace; font-size: 11px;
}
.recent-chip:hover { background: var(--pastel-blue-bg); }

/* ---- Changes menu (top-right of the detail-pane toolbar) ----
   Trigger + popover: click opens a list of files claude modified in
   this session; click a file → opens THAT file's whole-file diff in a
   new tab. Replaced the Recent Files `📄` button (modified files is
   the more useful subset of touched files — this is the single entry). */
.changes-menu { position: relative; }
.changes-trigger {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; height: 28px;
  background: transparent; border: 1px solid var(--border); border-radius: 6px;
  color: var(--ink-mute);
  font-family: 'Geist', system-ui, sans-serif; font-size: 12px; font-weight: 500;
  cursor: pointer;
  transition: color 120ms var(--ease-soft), background 120ms var(--ease-soft),
              border-color 120ms var(--ease-soft);
}
.changes-trigger:hover {
  color: var(--ink); background: var(--canvas); border-color: var(--border-strong);
}
.changes-trigger.open {
  color: var(--accent-soft-fg);
  background: var(--accent-soft-bg);
  border-color: var(--accent);
}
.changes-trigger svg { width: 14px; height: 14px; }
.changes-trigger .changes-count {
  padding: 1px 6px; border-radius: 3px;
  background: var(--accent-soft-bg); color: var(--accent-soft-fg);
  font-family: 'Geist Mono', monospace; font-size: 11px; font-weight: 600;
}
.changes-trigger.open .changes-count { background: var(--card); }
.changes-trigger .changes-count.error { color: var(--pastel-red-fg); background: var(--pastel-red-bg); }

.changes-popover {
  position: absolute; top: calc(100% + 8px); right: 0;
  width: 380px; max-width: calc(100vw - 40px);
  max-height: 60vh;
  background: var(--card);
  border: 1px solid var(--border); border-radius: 8px;
  box-shadow: var(--shadow-lg);
  display: flex; flex-direction: column;
  z-index: 30;
  overflow: hidden;
}
.changes-popover-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--canvas);
}
.changes-popover-head .title {
  font-family: 'Newsreader', serif; font-weight: 500;
  font-size: 13px; color: var(--ink);
  flex: 1;
}
.changes-popover-head .subtitle {
  color: var(--ink-mute); font-size: 11px; font-family: 'Geist Mono', monospace;
}
.changes-popover-head .close-btn {
  width: 20px; height: 20px; padding: 0;
  border: 0; background: transparent; color: var(--ink-mute);
  font-size: 18px; line-height: 1; cursor: pointer;
  border-radius: 4px;
}
.changes-popover-head .close-btn:hover { background: var(--card); color: var(--ink); }
.changes-popover-body {
  padding: 6px; overflow-y: auto;
}
.changes-empty {
  padding: 18px 12px; text-align: center;
  color: var(--ink-faint); font-size: 12px;
}
.changes-empty.error { color: var(--pastel-red-fg); }
.changes-empty button { margin-left: 6px; }
.change-file-row {
  display: flex; align-items: center; gap: 10px;
  width: 100%; padding: 8px 10px; margin: 2px 0;
  background: transparent; border: 0; border-radius: 4px;
  cursor: pointer; text-align: left; color: var(--ink);
  transition: background 120ms var(--ease-soft);
}
.change-file-row:hover { background: var(--canvas); }
.change-file-row .filecol {
  flex: 1; min-width: 0;
  display: flex; align-items: baseline; gap: 8px;
  overflow: hidden;
}
.change-file-row .filename {
  font-size: 13px; font-weight: 600; color: var(--ink);
  flex-shrink: 0;
}
.change-file-row .filedir {
  font-size: 11px; color: var(--ink-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  min-width: 0; flex: 1;
  direction: rtl; text-align: left; unicode-bidi: plaintext;
}
.change-file-row .metacol {
  display: inline-flex; gap: 6px; align-items: center; flex-shrink: 0;
  color: var(--ink-mute); font-family: 'Geist Mono', monospace; font-size: 11px;
}
.change-kind {
  padding: 1px 4px; border-radius: 3px;
  background: var(--canvas); color: var(--ink-mute);
}
.line-delta { white-space: nowrap; }
.line-delta b { color: var(--pastel-green-fg); font-weight: 600; }
.line-delta i { color: var(--pastel-red-fg); font-style: normal; font-weight: 600; }
.change-file-row .open-hint { color: var(--ink-faint); font-size: 13px; }
.change-file-row:hover .open-hint { color: var(--accent); }
.changes-popover-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 8px 12px; border-top: 1px solid var(--border);
  color: var(--ink-faint); background: var(--canvas); font-size: 10px;
}
.changes-popover-foot button { flex: 0 0 auto; font-size: 10px; padding: 3px 6px; }
</style>
