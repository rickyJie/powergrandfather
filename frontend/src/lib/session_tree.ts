// Pure helpers for building the Sessions view's folder trees.
//
// Extracted from Sessions.vue so:
//   1. useSessionFilter composable can compose them,
//   2. unit tests can exercise the tree shape without mounting a component.
//
// Design notes:
//   * All functions are pure — they take rows + config in, return a fresh
//     TreeNode out. No refs, no localStorage, no computed. State (expanded
//     folders, search query, active tab) lives one layer up in the composable.
//   * `buildTree` powers Active/Auto (deep cwd path tree OR flat project
//     grouping). `buildHistoryTree` is intentionally a *different* function
//     for History (flat 1-level by cwd/project) — Sessions.vue's original
//     comment explains why: deep cwd nesting was too many clicks to reach
//     closed sessions.
//   * Pinned rows float to the top of their folder in both builders.
//   * Recent bucket is a virtual folder derived from the top-N most recently
//     ended history rows across every cwd.

import type { SessionRow } from '../api/sessions'
import type { SessionProject } from '../api/sessionProjects'

export interface TreeNode {
  name: string
  fullPath: string
  isLeaf: boolean
  session?: SessionRow
  children: TreeNode[]
}

export type GroupBy = 'project' | 'cwd'

export interface BuildTreeOpts {
  groupBy: GroupBy
  sessionProjects: SessionProject[]
}

const pinnedFirst = (a: SessionRow, b: SessionRow): number => {
  if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1
  return 0
}

// C2: sort by a 60s-quantised activity bucket, not the raw timestamp. An SSE
// event frame optimistically stamps last_activity_ts=e.ts, then the server
// value lands ~180ms later via scheduleRowRefresh; those two differ by
// seconds. Comparing raw timestamps reorders the row twice (jump on the
// event, jump back on the reconcile) — enough to make the user misclick.
// Bucketing to the minute puts both values in the same bucket so the row
// holds position; the tiebreak keys (started_at, id) never change on events,
// so intra-bucket order is stable frame-to-frame.
const activityBucket = (s: SessionRow): number => {
  const t = new Date(s.last_activity_ts || s.started_at || 0).getTime()
  return Math.floor(t / 60000)
}
const byLastActivityDesc = (a: SessionRow, b: SessionRow): number => {
  const p = pinnedFirst(a, b)
  if (p) return p
  const ba = activityBucket(a)
  const bb = activityBucket(b)
  if (ba !== bb) return bb - ba
  const sa = new Date(a.started_at || 0).getTime()
  const sb = new Date(b.started_at || 0).getTime()
  if (sa !== sb) return sb - sa
  return a.id < b.id ? 1 : a.id > b.id ? -1 : 0
}

const byEndedDesc = (a: SessionRow, b: SessionRow): number => {
  const p = pinnedFirst(a, b)
  if (p) return p
  const ta = new Date(a.ended_at || a.last_activity_ts || a.started_at || 0).getTime()
  const tb = new Date(b.ended_at || b.last_activity_ts || b.started_at || 0).getTime()
  return tb - ta
}

const toLeaf = (s: SessionRow): TreeNode => ({
  name: s.title || s.id.slice(0, 8),
  fullPath: s.id,
  isLeaf: true,
  session: s,
  children: [],
})

/**
 * Live sessions view (Active + Auto tabs).
 *
 * groupBy='project'  → one folder per SessionProject + '(unassigned)' bucket,
 *                      flat leaves under each folder, empty projects included.
 * groupBy='cwd'      → nested folder tree following the filesystem path;
 *                      single-child folder chains collapse into slash-joined
 *                      names (e.g. `home/owner/repo`).
 */
export function buildTree(rows: SessionRow[], opts: BuildTreeOpts): TreeNode {
  const root: TreeNode = { name: '/', fullPath: '/', isLeaf: false, children: [] }

  if (opts.groupBy === 'project') {
    const projMap = new Map(opts.sessionProjects.map((p) => [p.id, p]))
    const buckets = new Map<string, SessionRow[]>()
    for (const s of rows) {
      const key = s.session_project_id || '__unassigned__'
      const arr = buckets.get(key) || []
      arr.push(s)
      buckets.set(key, arr)
    }
    for (const p of opts.sessionProjects) {
      if (!buckets.has(p.id)) buckets.set(p.id, [])
    }
    const entries = [...buckets.entries()].sort((a, b) => {
      if (a[0] === '__unassigned__') return 1
      if (b[0] === '__unassigned__') return -1
      const na = projMap.get(a[0])?.name || a[0]
      const nb = projMap.get(b[0])?.name || b[0]
      return na.localeCompare(nb)
    })
    for (const [key, sessions] of entries) {
      const label = key === '__unassigned__'
        ? '(unassigned)'
        : (projMap.get(key)?.name || key)
      root.children.push({
        name: label,
        fullPath: 'project:' + key,
        isLeaf: false,
        children: [...sessions].sort(byLastActivityDesc).map(toLeaf),
      })
    }
    return root
  }

  // cwd mode → nested path tree.
  const sorted = [...rows].sort(pinnedFirst)
  for (const s of sorted) {
    const parts = (s.cwd || '/').split('/').filter(Boolean)
    let node = root
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i]
      const path = '/' + parts.slice(0, i + 1).join('/')
      let child = node.children.find((c) => !c.isLeaf && c.name === part)
      if (!child) {
        child = { name: part, fullPath: path, isLeaf: false, children: [] }
        node.children.push(child)
      }
      node = child
    }
    node.children.push(toLeaf(s))
  }
  // Collapse single-child folder chains so deep paths render compactly.
  function collapse(node: TreeNode): TreeNode {
    node.children = node.children.map(collapse)
    if (!node.isLeaf && node.children.length === 1 && !node.children[0].isLeaf) {
      const only = node.children[0]
      return { ...only, name: node.name + '/' + only.name }
    }
    return node
  }
  root.children = root.children.map(collapse).sort((a, b) => {
    if (a.isLeaf !== b.isLeaf) return a.isLeaf ? 1 : -1  // folders first
    return a.name.localeCompare(b.name)
  })
  return root
}

/**
 * History view — flat 1-level grouping.
 *
 * User complaint on the deep cwd tree: too many clicks to reach closed
 * sessions. History flattens to "one folder per distinct cwd (or project)",
 * with leaves directly underneath, folders ordered by most-recent ended_at.
 */
export function buildHistoryTree(rows: SessionRow[], opts: BuildTreeOpts): TreeNode {
  const root: TreeNode = { name: '/', fullPath: '/', isLeaf: false, children: [] }

  if (opts.groupBy === 'project') {
    const projMap = new Map(opts.sessionProjects.map((p) => [p.id, p]))
    const buckets = new Map<string, SessionRow[]>()
    for (const s of rows) {
      const key = s.session_project_id || '__unassigned__'
      const arr = buckets.get(key) || []
      arr.push(s)
      buckets.set(key, arr)
    }
    // Only surface projects with closed sessions — empty ones are noise in History.
    const entries = [...buckets.entries()].sort((a, b) => {
      if (a[0] === '__unassigned__') return 1
      if (b[0] === '__unassigned__') return -1
      const na = projMap.get(a[0])?.name || a[0]
      const nb = projMap.get(b[0])?.name || b[0]
      return na.localeCompare(nb)
    })
    for (const [key, sessions] of entries) {
      const label = key === '__unassigned__'
        ? '(unassigned)'
        : (projMap.get(key)?.name || key)
      root.children.push({
        name: label,
        fullPath: 'histproj:' + key,
        isLeaf: false,
        children: [...sessions].sort(byEndedDesc).map(toLeaf),
      })
    }
    return root
  }

  const byCwd = new Map<string, SessionRow[]>()
  for (const s of rows) {
    const key = s.cwd || '/'
    const arr = byCwd.get(key) || []
    arr.push(s)
    byCwd.set(key, arr)
  }
  const folders: TreeNode[] = []
  for (const [cwd, sessions] of byCwd) {
    folders.push({
      name: cwd,
      fullPath: 'histcwd:' + cwd,
      isLeaf: false,
      children: [...sessions].sort(byEndedDesc).map(toLeaf),
    })
  }
  folders.sort((a, b) => {
    const ta = new Date(a.children[0]?.session?.ended_at || 0).getTime()
    const tb = new Date(b.children[0]?.session?.ended_at || 0).getTime()
    return tb - ta
  })
  root.children = folders
  return root
}

/**
 * Top-N most recently ended rows across every cwd, packaged as a virtual
 * folder. Returns null when there's nothing to show — caller can then avoid
 * prepending an empty node to the history tree.
 */
export function buildRecentBucket(rows: SessionRow[], topN: number): TreeNode | null {
  const closed = [...rows].sort(byEndedDesc).slice(0, topN)
  if (!closed.length) return null
  return {
    name: `⏱ Recent (${closed.length})`,
    // Distinct fullPath so localStorage expand state doesn't collide with
    // a real folder that happens to be named "Recent".
    fullPath: '__recent__',
    isLeaf: false,
    children: closed.map(toLeaf),
  }
}

/**
 * History-visibility filter — hides rows that already have a successor
 * in the resume chain. Non-resumable-but-not-superseded rows still surface
 * (with the Resume button disabled by SessionTreeNode's canResume).
 */
export function isVisibleHistoryRow(s: SessionRow): boolean {
  return !s.superseded_by
}

export function leavesOf(node: TreeNode): SessionRow[] {
  if (node.isLeaf && node.session) return [node.session]
  return node.children.flatMap(leavesOf)
}
