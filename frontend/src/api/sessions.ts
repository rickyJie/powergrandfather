import { http, pollGet, wsUrl } from './client'

export interface SessionRow {
  id: string
  title: string | null
  type: string
  cwd: string
  status: string
  pid: number | null
  started_at: string | null
  ended_at: string | null
  exit_code: number | null
  external_session_id: string | null
  claude_session_id: string | null
  superseded_by: string | null
  // Backend `_jsonl_present(cwd, claude_session_id)` — true when the
  // `~/.claude/projects/<encoded_cwd>/<sid>.jsonl` file exists on disk.
  // Frontend History view hides Resume when this is false because claude
  // has pruned the transcript and `--resume <sid>` would immediately die.
  // Optional in the type so pre-restart snapshots don't fail to parse.
  jsonl_present?: boolean
  associated_run_id: string | null
  tags: string[]
  last_activity_ts: string | null
  unread_count: number
  current_tool: string | null
  last_assistant_msg: string | null
  // local:a79c795d — SessionProject FK. NULL means "auto cwd bucket".
  session_project_id: string | null
  // local:45b259b4 — right-click menu additions.
  pinned?: boolean
  manual_unread?: boolean
  // Multi-agent v2 — which CLI-adapter this session runs on.
  // Optional so pre-v2 API responses (or legacy rows) still parse.
  agent?: string
  // Deprecated alias for `agent` (1-release compat window).
  backend?: string
  // POST_SPAWN_BIND artifact path (codex rollout file) — null for claude.
  rollout_path?: string | null
  // Deprecated alias for `rollout_path`.
  codex_rollout_path?: string | null
  highlighted?: boolean
  archived_at?: string | null
}

export interface CreateSessionPayload {
  cwd: string
  type?: string
  title?: string
  initial_prompt?: string
  run_id?: string
  argv?: string[]
  session_project_id?: string | null
  // Multi-agent v2 explicit override. Omit to use user default agent.
  agent?: string
}

export interface SessionListResult {
  count: number
  page_count: number
  offset: number
  has_more: boolean
  items: SessionRow[]
  /**
   * Older backends returned only `{ count, items }` and ignored `offset`.
   * Keeping this explicit lets the view use a safe one-shot compatibility
   * fetch instead of silently treating the first page as the full history.
   */
  legacy_pagination: boolean
}

export function normalizeSessionListResponse(
  data: unknown,
  requestedOffset = 0,
): SessionListResult {
  const raw = data && typeof data === 'object'
    ? data as Record<string, unknown>
    : {}
  const items = Array.isArray(raw.items) ? raw.items as SessionRow[] : []
  const modern = typeof raw.has_more === 'boolean'
    && typeof raw.page_count === 'number'
    && typeof raw.offset === 'number'
  const rawCount = typeof raw.count === 'number' && Number.isFinite(raw.count)
    ? Math.max(0, raw.count)
    : items.length

  return {
    count: Math.max(rawCount, items.length),
    page_count: modern ? Number(raw.page_count) : items.length,
    offset: modern ? Number(raw.offset) : requestedOffset,
    // A legacy endpoint cannot be paged safely because it does not advertise
    // or necessarily honour offsets. The Sessions view retries once with the
    // largest supported limit and then treats that response as a snapshot.
    has_more: modern ? Boolean(raw.has_more) : false,
    items,
    legacy_pagination: !modern,
  }
}

export const sessionsApi = {
  list: async (params?: { status?: string; type?: string; limit?: number; offset?: number }) => {
    // pollGet: fail fast at 8s + retry on a fresh connection so a wedged SSH
    // tunnel connection doesn't hang the list refresh for 30s (see client.ts).
    const { data } = await pollGet('/api/sessions', { params })
    return normalizeSessionListResponse(data, params?.offset ?? 0)
  },
  get: async (id: string) => {
    // pollGet: connect-critical — opening a session fetches this + /changes +
    // /output. Plain http.get let a wedged tunnel connection hang the connect
    // 30s (perf.log). Fast-fail 8s + fresh-connection retry instead.
    const { data } = await pollGet(`/api/sessions/${id}`)
    return data as SessionRow
  },
  create: async (body: CreateSessionPayload) => {
    const { data } = await http.post('/api/sessions', body)
    return data as SessionRow
  },
  rename: async (id: string, title: string) => {
    const { data } = await http.patch(`/api/sessions/${id}`, { title })
    return data as SessionRow
  },
  setProject: async (id: string, session_project_id: string | null) => {
    // Empty string is the backend's "unset" marker. Sending literal null
    // works too but empty string is JSON-friendlier via curl / etc.
    const { data } = await http.patch(`/api/sessions/${id}`, {
      session_project_id: session_project_id === null ? '' : session_project_id,
    })
    return data as SessionRow
  },
  setPinned: async (id: string, pinned: boolean) => {
    const { data } = await http.patch(`/api/sessions/${id}`, { pinned })
    return data as SessionRow
  },
  setManualUnread: async (id: string, manual_unread: boolean) => {
    const { data } = await http.patch(`/api/sessions/${id}`, { manual_unread })
    return data as SessionRow
  },
  setHighlighted: async (id: string, highlighted: boolean) => {
    const { data } = await http.patch(`/api/sessions/${id}`, { highlighted })
    return data as SessionRow
  },
  setArchived: async (id: string, archived: boolean) => {
    const { data } = await http.patch(`/api/sessions/${id}`, { archived })
    return data as SessionRow
  },
  archiveEnded: async () => {
    const { data } = await http.post('/api/sessions/archive-ended')
    return data as { archived: number }
  },
  stop: async (id: string, graceful = true, async_ = false) => {
    // async_=true returns 202 immediately; the signal ladder (up to 15s) runs
    // in a backend BackgroundTask. Use it for UI actions where you don't want
    // to block on the terminate handshake.
    const params: Record<string, boolean> = { graceful }
    if (async_) params.async_ = true
    const { data } = await http.delete(`/api/sessions/${id}`, { params })
    return data
  },
  kill: async (id: string) => {
    const { data } = await http.post(`/api/sessions/${id}/kill`)
    return data
  },
  purge: async (id: string) => {
    const { data } = await http.post(`/api/sessions/${id}/purge`)
    return data
  },
  purgeHistory: async () => {
    const { data } = await http.post('/api/sessions/purge-history')
    return data as { purged: number; ids: string[] }
  },
  resume: async (id: string) => {
    const { data } = await http.post(`/api/sessions/${id}/resume`)
    return data as SessionRow
  },
  reapStale: async () => {
    const { data } = await http.post('/api/sessions/reap-stale')
    return data as { reaped: number; items: Array<{ id: string; title: string | null; pid: number | null; prior_status: string }> }
  },
  // Session Changes panel: per-session file edits parsed from the Claude
  // transcript or Codex rollout. `changes` = aggregate per-file summary;
  // `diff` = full edit history for one path (chronological, oldest first).
  changes: async (id: string) => {
    // pollGet: connect-critical (loaded on session open) — fast-fail + retry.
    const { data } = await pollGet(`/api/sessions/${id}/changes`)
    return data as {
      sid: string
      total_edits: number
      files: Array<{
        path: string
        edit_count: number
        tools: string[]
        first_ts: string
        last_ts: string
        additions: number
        deletions: number
        change_kind: 'added' | 'modified' | 'deleted' | 'renamed'
      }>
    }
  },
  changeDiff: async (id: string, path: string) => {
    const { data } = await http.get(`/api/sessions/${id}/changes/diff`, {
      params: { path },
    })
    return data as {
      sid: string
      path: string
      edits: Array<{
        index: number
        ts: string
        tool: string
        old: string | null
        new: string
        tool_use_id: string
        sub_index: number
        source_path: string | null
      }>
    }
  },
  output: async (id: string) => {
    // pollGet: connect-critical (terminal backlog on session open) — fast-fail
    // + fresh-connection retry so a wedged tunnel connection can't hang connect.
    const response = await pollGet<ArrayBuffer>(`/api/sessions/${id}/output`, {
      responseType: 'arraybuffer',
    })
    return {
      data: new Uint8Array(response.data),
      source: String(response.headers['x-csm-output-source'] || 'missing'),
    }
  },
  wsUrl: (id: string) => wsUrl(`/api/sessions/${id}/ws`),
}
