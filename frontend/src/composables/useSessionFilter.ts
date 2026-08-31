// useSessionFilter — one composable for the Sessions view's derived state.
//
// What it owns:
//   * searchQuery       — the sidebar's title-only search box
//   * filter (tab)      — active | auto | history
//   * groupBy           — 'project' | 'cwd' (persisted to localStorage)
//   * expandedFolders   — Set<string> of open folder fullPaths, persisted
//   * derived computeds — visibleRows, searchResults, sessionTree,
//                         historyTree, recentBucket, liveRows, historyRows
//
// What it does NOT own:
//   * `rows` (the source of truth from /api/sessions) — passed in by caller
//   * `sessionProjects` — also passed in; loaded by Sessions.vue separately
//   * unread counts, PTY state, terminal, WebSocket — those stay in the view
//
// Slot 12 will thread these values into Sessions.vue; this file is created
// standalone and does not touch the .vue file.

import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
import type { SessionRow } from '../api/sessions'
import type { SessionProject } from '../api/sessionProjects'
import {
  buildTree,
  buildHistoryTree,
  buildRecentBucket,
  isVisibleHistoryRow as isVisibleHistoryRowPure,
  type TreeNode,
  type GroupBy,
} from '../lib/session_tree'

export type FilterTab = 'active' | 'auto' | 'history'

// Matches Sessions.vue's status buckets. Kept here (rather than imported)
// so the composable is self-contained — the view can drop its own copies
// once it starts consuming this file.
const LIVE_STATUSES = new Set([
  'starting', 'running', 'idle', 'waiting_input', 'waiting_auth', 'orphaned',
])
const CLOSED_STATUSES = new Set(['exited', 'crashed'])

export function isLive(s: SessionRow): boolean {
  return LIVE_STATUSES.has(s.status)
}
export function isClosed(s: SessionRow): boolean {
  return CLOSED_STATUSES.has(s.status)
}

export interface UseSessionFilterOpts {
  /** Top-N cap for the "⏱ Recent" virtual bucket in History. */
  recentBucketSize?: number
  /** localStorage key for expanded folder set. Default: csm.tree.expanded. */
  expandedStorageKey?: string
  /** localStorage key for groupBy. Default: csm.sess.groupBy. */
  groupByStorageKey?: string
  /** Initial tab. Default: 'active'. */
  initialFilter?: FilterTab
}

export interface UseSessionFilterReturn {
  // — inputs (writable) —
  searchQuery: Ref<string>
  filter: Ref<FilterTab>
  groupBy: Ref<GroupBy>
  expandedFolders: Ref<Set<string>>
  showArchived: Ref<boolean>

  // — derived —
  searchActive: ComputedRef<boolean>
  visibleRows: ComputedRef<SessionRow[]>       // rows for the current tab
  liveRows: ComputedRef<SessionRow[]>          // interactive + auto, live only
  historyRows: ComputedRef<SessionRow[]>       // interactive, closed only
  searchResults: ComputedRef<SessionRow[]>     // flat, title-only, cross-tab
  sessionTree: ComputedRef<TreeNode>           // built from visibleRows (Active/Auto)
  historyTree: ComputedRef<TreeNode>           // history + Recent bucket
  recentBucket: ComputedRef<TreeNode | null>

  // — helpers —
  setGroupBy(mode: GroupBy): void
  toggleFolder(path: string): void
  isOpen(path: string): boolean
  isVisibleHistoryRow(s: SessionRow): boolean
}

const DEFAULT_RECENT_N = 7
const DEFAULT_EXPANDED_KEY = 'csm.tree.expanded'
const DEFAULT_GROUPBY_KEY = 'csm.sess.groupBy'
const SHOW_ARCHIVED_KEY = 'csm.sess.showArchived'

function loadExpanded(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    return new Set(Array.isArray(arr) ? arr : [])
  } catch {
    return new Set()
  }
}

function loadGroupBy(key: string): GroupBy {
  try {
    const v = localStorage.getItem(key)
    if (v === 'project' || v === 'cwd') return v
  } catch { /* localStorage unavailable */ }
  return 'cwd'
}

/**
 * Compose search + tree-building + recent bucket + folder collapse state.
 *
 * @param rows  A ref/computed pointing at the raw session list from
 *              /api/sessions. Composable does not fetch — caller owns
 *              refresh cadence and reactivity source.
 * @param sessionProjects  Same contract — caller owns loading these.
 * @param opts  See UseSessionFilterOpts.
 */
export function useSessionFilter(
  rows: Ref<SessionRow[]> | ComputedRef<SessionRow[]>,
  sessionProjects: Ref<SessionProject[]> | ComputedRef<SessionProject[]>,
  opts: UseSessionFilterOpts = {},
): UseSessionFilterReturn {
  const recentN = opts.recentBucketSize ?? DEFAULT_RECENT_N
  const expandedKey = opts.expandedStorageKey ?? DEFAULT_EXPANDED_KEY
  const groupByKey = opts.groupByStorageKey ?? DEFAULT_GROUPBY_KEY

  const searchQuery = ref<string>('')
  const filter = ref<FilterTab>(opts.initialFilter ?? 'active')
  const groupBy = ref<GroupBy>(loadGroupBy(groupByKey))
  const expandedFolders = ref<Set<string>>(loadExpanded(expandedKey))
  const showArchived = ref<boolean>(
    (() => {
      try { return localStorage.getItem(SHOW_ARCHIVED_KEY) === '1' }
      catch { return false }
    })(),
  )
  watch(showArchived, (value) => {
    try { localStorage.setItem(SHOW_ARCHIVED_KEY, value ? '1' : '0') }
    catch { /* best effort */ }
  })

  function setGroupBy(mode: GroupBy) {
    groupBy.value = mode
    try { localStorage.setItem(groupByKey, mode) } catch { /* ignore */ }
  }
  function toggleFolder(path: string) {
    const next = new Set(expandedFolders.value)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    expandedFolders.value = next
    try {
      localStorage.setItem(expandedKey, JSON.stringify([...next]))
    } catch { /* ignore quota / unavailable */ }
  }
  function isOpen(path: string): boolean {
    return expandedFolders.value.has(path)
  }

  const searchActive = computed(() => searchQuery.value.trim().length > 0)

  // Tab-scoped filter. Search bypasses this — searchResults reads `rows` directly.
  const visibleRows = computed<SessionRow[]>(() => {
    return rows.value.filter((s) => {
      if (filter.value === 'active') {
        return s.type === 'interactive' && isLive(s)
      }
      if (filter.value === 'auto') {
        // Auto tab shows *live* workflow-owned sessions only — ended auto
        // runs belong to the Automation module.
        return s.type === 'auto' && isLive(s)
      }
      // history
      return s.type === 'interactive' && isClosed(s)
        && (showArchived.value || !s.archived_at)
    })
  })

  // Convenience splits — same data as visibleRows but keyed by intent rather
  // than the current tab, useful for consumers that want both at once.
  const liveRows = computed<SessionRow[]>(() =>
    rows.value.filter((s) =>
      (s.type === 'interactive' || s.type === 'auto') && isLive(s)
    ),
  )
  const historyRows = computed<SessionRow[]>(() =>
    rows.value.filter((s) =>
      s.type === 'interactive' && isClosed(s)
      && (showArchived.value || !s.archived_at)
    ),
  )

  function projectName(s: SessionRow): string {
    return sessionProjects.value.find((p) => p.id === s.session_project_id)?.name || ''
  }

  function matchesSearch(s: SessionRow, raw: string): boolean {
    const fields = new Map<string, string[]>()
    const plain = raw.replace(
      /(\w+):(?:"([^"]+)"|(\S+))/g,
      (_whole, key: string, quoted: string, unquoted: string) => {
        const values = fields.get(key.toLowerCase()) || []
        values.push((quoted || unquoted || '').toLowerCase())
        fields.set(key.toLowerCase(), values)
        return ' '
      },
    ).trim().toLowerCase()

    const agent = (s.agent || s.backend || '').toLowerCase()
    const haystack = [
      s.id, s.title, s.cwd, agent, s.status, s.type,
      s.last_assistant_msg, projectName(s),
    ].filter(Boolean).join('\n').toLowerCase()
    if (plain && !plain.split(/\s+/).every((token) => haystack.includes(token))) return false

    for (const [key, values] of fields) {
      const candidate =
        key === 'agent' || key === 'backend' ? agent
        : key === 'status' ? s.status.toLowerCase()
        : key === 'cwd' || key === 'path' ? s.cwd.toLowerCase()
        : key === 'project' ? projectName(s).toLowerCase()
        : key === 'type' ? s.type.toLowerCase()
        : key === 'id' ? s.id.toLowerCase()
        : key === 'archived' ? String(!!s.archived_at)
        : ''
      if (!candidate || !values.every((value) => candidate.includes(value))) return false
    }
    return true
  }

  // Cross-tab, multi-field search with optional field terms such as
  // ``agent:codex status:idle cwd:PowerGrandFather``.
  const searchResults = computed<SessionRow[]>(() => {
    if (!searchActive.value) return []
    const q = searchQuery.value.trim().toLowerCase()
    return rows.value
      .filter((s) =>
        (showArchived.value || !s.archived_at || q.includes('archived:'))
        && matchesSearch(s, q)
      )
      .sort((a, b) => {
        const ta = new Date(a.last_activity_ts || a.ended_at || a.started_at || 0).getTime()
        const tb = new Date(b.last_activity_ts || b.ended_at || b.started_at || 0).getTime()
        return tb - ta
      })
  })

  const treeOpts = computed(() => ({
    groupBy: groupBy.value,
    sessionProjects: sessionProjects.value,
  }))

  const sessionTree = computed<TreeNode>(() =>
    buildTree(visibleRows.value, treeOpts.value),
  )

  const recentBucket = computed<TreeNode | null>(() => {
    if (filter.value !== 'history') return null
    if (searchActive.value) return null  // search view supersedes tree
    return buildRecentBucket(
      visibleRows.value.filter(isVisibleHistoryRowPure),
      recentN,
    )
  })

  const historyTree = computed<TreeNode>(() => {
    const t = buildHistoryTree(
      visibleRows.value.filter(isVisibleHistoryRowPure),
      treeOpts.value,
    )
    const bucket = recentBucket.value
    return bucket ? { ...t, children: [bucket, ...t.children] } : t
  })

  return {
    searchQuery,
    filter,
    groupBy,
    expandedFolders,
    showArchived,
    searchActive,
    visibleRows,
    liveRows,
    historyRows,
    searchResults,
    sessionTree,
    historyTree,
    recentBucket,
    setGroupBy,
    toggleFolder,
    isOpen,
    isVisibleHistoryRow: isVisibleHistoryRowPure,
  }
}

export type { TreeNode, GroupBy } from '../lib/session_tree'
