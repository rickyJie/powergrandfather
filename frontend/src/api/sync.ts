import { http } from './client'

// ---------- domain shapes (mirror backend §7.3) ---------------------------

export type SyncStatus = 'ok' | 'timeout' | 'unsupported' | 'skipped' | 'error'

export interface PerAgentResult {
  agent: string
  status: SyncStatus
  detail: string | null
}

export interface SyncEnvelope<T> {
  data: T
  sync: PerAgentResult[]
  warnings: string[]
}

export type SyncMode = 'lock' | 'agent'

export interface SyncConfigEntry {
  id: number
  module: string
  enrolled_agents: string[]
  poll_interval_sec: number
  enabled: boolean
  sync_mode: SyncMode
  tick_interval_hours: number
  tick_interval_minutes: number
  // null = no filter (sync everything); a list restricts sync to those
  // resource names. Used for the skills module — pick which skills to sync.
  resource_allowlist: string[] | null
  updated_at: string
}

export interface AvailableSkill {
  name: string
  description: string | null
  agents: string[]
  // Bundle size (files beside SKILL.md) per agent. Two agents disagreeing is
  // the tell that one of them has an incomplete copy.
  file_count: Record<string, number>
  // 'user' = hand-authored (no version: frontmatter), 'marketplace' = installed
  // from a marketplace/plugin (heuristic).
  source_hint: 'user' | 'marketplace'
}

export interface Instruction {
  id: number
  name: string
  title: string
  body: string
  share_scope: string[]
  priority: number
  created_at: string
  updated_at: string
}

export interface McpServer {
  id: number
  name: string
  transport: 'stdio' | 'http' | 'sse'
  command: string | null
  args_json: string[]
  url: string | null
  env_json: Record<string, string>
  enabled_for: string[]
  created_at: string
  updated_at: string
}

export interface SkillFile {
  rel_path: string
  content: string
  encoding: 'utf-8' | 'base64'
  mode: number
  size: number
  sha256: string
}

export interface Skill {
  id: number
  name: string
  description: string
  body_md: string
  share_scope: string[]
  // Files beside SKILL.md. Always present as a count; `files` itself only
  // comes back from getSkill(id), never from the list endpoint.
  file_count: number
  files?: SkillFile[]
  created_at: string
  updated_at: string
}

export interface DriftRow {
  id: number
  ts: string
  module: string
  resource_type: string
  resource_id: number
  agent: string
  reason: string
  expected_hash: string | null
  actual_hash: string | null
  resolved: boolean
  resolved_at: string | null
  detail_json: unknown
}

export interface ActivityRow {
  id: number
  ts: string
  module: string
  resource_type: string
  resource_id: number | null
  agent: string
  action: string
  status: SyncStatus
  duration_ms: number
  detail_json: unknown
}

export interface StatusRow {
  module: string
  enrolled_agents: string[]
  enabled: boolean
  unresolved_drift: number
}

// ---------- API surface ---------------------------------------------------

export const syncApi = {
  // config
  listConfig: () => http.get<{ config: Array<{ module: string; entry: SyncConfigEntry | null }> }>('/api/sync/config').then(r => r.data),
  updateConfig: (module: string, patch: Partial<Pick<SyncConfigEntry, 'enrolled_agents' | 'poll_interval_sec' | 'enabled' | 'sync_mode' | 'tick_interval_hours' | 'tick_interval_minutes' | 'resource_allowlist'>>) =>
    http.put<SyncConfigEntry>(`/api/sync/config/${module}`, patch).then(r => r.data),
  availableSkills: (agent?: string) =>
    http.get<AvailableSkill[]>('/api/sync/skills/available', { params: agent ? { agent } : {} }).then(r => r.data),
  status: () => http.get<{ modules: StatusRow[] }>('/api/sync/status').then(r => r.data),

  // instructions
  listInstructions: () => http.get<{ items: Instruction[] }>('/api/sync/memory/instructions').then(r => r.data.items),
  createInstruction: (body: Omit<Instruction, 'id' | 'created_at' | 'updated_at'>) =>
    http.post<SyncEnvelope<Instruction>>('/api/sync/memory/instructions', body).then(r => r.data),
  updateInstruction: (id: number, body: Omit<Instruction, 'id' | 'created_at' | 'updated_at'>) =>
    http.put<SyncEnvelope<Instruction>>(`/api/sync/memory/instructions/${id}`, body).then(r => r.data),
  deleteInstruction: (id: number) =>
    http.delete<SyncEnvelope<{ deleted: true; id: number }>>(`/api/sync/memory/instructions/${id}`).then(r => r.data),

  // mcp servers
  listMcpServers: () => http.get<{ items: McpServer[] }>('/api/sync/mcp/servers').then(r => r.data.items),
  createMcpServer: (body: Omit<McpServer, 'id' | 'created_at' | 'updated_at'>) =>
    http.post<SyncEnvelope<McpServer>>('/api/sync/mcp/servers', body).then(r => r.data),
  updateMcpServer: (id: number, body: Omit<McpServer, 'id' | 'created_at' | 'updated_at'>) =>
    http.put<SyncEnvelope<McpServer>>(`/api/sync/mcp/servers/${id}`, body).then(r => r.data),
  deleteMcpServer: (id: number) =>
    http.delete<SyncEnvelope<{ deleted: true; id: number }>>(`/api/sync/mcp/servers/${id}`).then(r => r.data),

  // skills
  listSkills: () => http.get<{ items: Skill[] }>('/api/sync/skills').then(r => r.data.items),
  // Single skill WITH its bundle contents; the list endpoint omits them.
  getSkill: (id: number) => http.get<Skill>(`/api/sync/skills/${id}`).then(r => r.data),
  createSkill: (body: Omit<Skill, 'id' | 'created_at' | 'updated_at' | 'file_count'>) =>
    http.post<SyncEnvelope<Skill>>('/api/sync/skills', body).then(r => r.data),
  // Omitting `files` leaves the bundle untouched; passing [] clears it.
  updateSkill: (id: number, body: Omit<Skill, 'id' | 'created_at' | 'updated_at' | 'file_count'>) =>
    http.put<SyncEnvelope<Skill>>(`/api/sync/skills/${id}`, body).then(r => r.data),
  deleteSkill: (id: number) =>
    http.delete<SyncEnvelope<{ deleted: true; id: number }>>(`/api/sync/skills/${id}`).then(r => r.data),
  // Re-read skill bundles off `agent`'s disk and re-push them. The repair
  // path for skills ingested before bundle sync existed.
  reingestSkills: (agent: string, names?: string[]) =>
    http.post<{ agent: string; items: Array<{
      name: string; action: string; file_count?: number
      detail?: string; skipped_files?: string[]
    }> }>('/api/sync/skills/reingest', null, { params: { agent, ...(names ? { names } : {}) } })
      .then(r => r.data),

  // preview
  importPreview: (module: 'memory' | 'mcp' | 'skills', agent: string) =>
    http.get(`/api/sync/${module}/import-preview`, { params: { agent } }).then(r => r.data),

  // Deterministic (LLM-free) migrate: copy source agent's resources into CSM
  // and fan out to target. memory + skills supported; mcp is unsupported.
  // Response is an envelope: { module, source, target, items: [...] }.
  migrate: (module: 'memory' | 'mcp' | 'skills', body: { source: string; target: string; names?: string[] }) =>
    http.post<{
      module: string; source: string; target: string
      items: Array<{ name: string | null; action: string; detail?: string }>
    }>(`/api/sync/${module}/migrate`, body).then(r => r.data),

  // drift & activity
  listDrift: (resolved = false, limit = 50) =>
    http.get<{ items: DriftRow[] }>('/api/sync/drift', { params: { resolved, limit } }).then(r => r.data.items),
  resolveDrift: (id: number) => http.post<DriftRow>(`/api/sync/drift/${id}/resolve`).then(r => r.data),
  listActivity: (opts: { module?: string; limit?: number; since?: string } = {}) =>
    http.get<{ items: ActivityRow[] }>('/api/sync/activity', { params: opts }).then(r => r.data.items),

  // ---- v2 agent-driven ---------------------------------------------------

  agentTick: () => http.post<AgentTickResult>('/api/sync/agent-tick', {}).then(r => r.data),
  listAgentRuns: (limit = 50) =>
    http.get<AgentRunRow[]>('/api/sync/agent-runs', { params: { limit } }).then(r => r.data),
  getAgentRun: (id: number) =>
    http.get<AgentRunRow & { live_phase: string | null }>(`/api/sync/agent-runs/${id}`).then(r => r.data),

  listPendingDecisions: (status: PendingStatus | 'all' = 'pending', limit = 100) =>
    http.get<PendingDecisionRow[]>('/api/sync/pending-decisions', { params: { status, limit } }).then(r => r.data),
  resolvePendingDecision: (id: number, resolution: string) =>
    http.post<PendingDecisionRow>(`/api/sync/pending-decisions/${id}/resolve`, { resolution }).then(r => r.data),

  listFanoutLedger: (status: LedgerStatus | 'non_done' | 'all' = 'non_done', limit = 100) =>
    http.get<FanoutLedgerRow[]>('/api/sync/fanout-ledger', { params: { status, limit } }).then(r => r.data),
  retryLedger: (id: number) =>
    http.post<FanoutLedgerRow>(`/api/sync/fanout-ledger/${id}/retry`).then(r => r.data),
  dismissLedger: (id: number) =>
    http.post<FanoutLedgerRow>(`/api/sync/fanout-ledger/${id}/dismiss`).then(r => r.data),

  getPolicy: () => http.get<PolicyRow>('/api/sync/policy').then(r => r.data),
  updatePolicy: (prompt: string) => http.put<PolicyRow>('/api/sync/policy', { prompt }).then(r => r.data),
  resetPolicy: () => http.post<PolicyRow>('/api/sync/policy/reset').then(r => r.data),

  unenrollAgent: (module: string, agent: string) =>
    http.delete<{ module: string; unenrolled_agent: string; resource_hashes_stripped: number }>(
      `/api/sync/config/${module}/agents/${agent}`,
    ).then(r => r.data),
}

// ---------- v2 shapes -----------------------------------------------------

export interface AgentTickResult {
  // null when the backend couldn't stamp the run row within its short wait
  // window; the tick still runs in the background.
  run_id: number | null
  status: string
  phase?: string | null
}

export interface AgentRunRow {
  id: number
  ts: string
  trigger: string
  phase: string | null
  prompt_hash: string
  input_state_hash: string
  decisions_count: number | null
  applied_count: number | null
  rejected_count: number | null
  stale_skipped_count: number | null
  deleted_after_collect_count: number | null
  error: string | null
  duration_ms: number | null
  token_usage_json: unknown
  parent_run_id: number | null
}

export type PendingStatus = 'pending' | 'resolve_failed' | 'resolved' | 'dismissed'

export interface PendingDecisionRow {
  id: number
  agent_run_id: number
  ts: string
  resource_type: string
  resource_id: number | null
  proposed_action: string
  candidates_json: Record<string, string>
  status: PendingStatus
  resolution: string | null
  resolved_at: string | null
  applied_at: string | null
  apply_error: string | null
  retry_count: number
}

export type LedgerStatus = 'pending' | 'phase2_done' | 'done' | 'failed_terminal'

export interface FanoutLedgerRow {
  id: number
  ts: string
  resource_type: string
  resource_id: number
  body_hash: string
  target_agents: string[]
  status: LedgerStatus
  attempt_count: number
  attempted_at: string | null
  fanout_result_json: unknown
}

export interface PolicyRow {
  id: number
  prompt: string
  prompt_hash: string
  updated_at: string
}
