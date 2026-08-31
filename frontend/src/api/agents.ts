import { http } from './client'

export interface AgentDef {
  id: string
  name: string
  display_name: string
  icon: string | null
  description: string | null
  cwd: string
  prompt_source: string | null
  prompt_cached: string
  disable_skills: boolean
  created_at: string
  updated_at: string
}

export interface AgentConversation {
  id: string
  agent_def_id: string
  session_id: string
  title: string | null
  created_at: string
  last_activity_ts: string | null
  ended_at: string | null
  session_status: string | null
  claude_session_id: string | null
  cwd: string | null
}

export interface CreateAgentBody {
  name: string
  display_name: string
  cwd: string
  prompt_cached?: string
  prompt_source?: string
  icon?: string | null
  description?: string | null
  disable_skills?: boolean
}

export interface PatchAgentBody {
  display_name?: string
  cwd?: string
  prompt_cached?: string
  prompt_source?: string
  icon?: string | null
  description?: string | null
  disable_skills?: boolean
  refresh_from_source?: boolean
}

export interface AgentOverviewItem {
  agent: AgentDef
  active: AgentConversation | null
  history: AgentConversation[]
}

export const agentsApi = {
  list: async () => (await http.get('/api/agents')).data as { count: number; items: AgentDef[] },
  overview: async (historyPerAgent = 5) =>
    (await http.get('/api/agents-overview', { params: { history_per_agent: historyPerAgent } }))
      .data as { count: number; items: AgentOverviewItem[] },
  get: async (id: string) => (await http.get(`/api/agents/${id}`)).data as AgentDef,
  create: async (body: CreateAgentBody) => (await http.post('/api/agents', body)).data as AgentDef,
  patch: async (id: string, body: PatchAgentBody) =>
    (await http.patch(`/api/agents/${id}`, body)).data as AgentDef,
  delete: async (id: string) => (await http.delete(`/api/agents/${id}`)).data,

  spawn: async (id: string) =>
    (await http.post(`/api/agents/${id}/spawn`)).data as AgentConversation & { reused: boolean },
  activeConversation: async (id: string) =>
    (await http.get(`/api/agents/${id}/active-conversation`)).data as { active: AgentConversation | null },
  listConversations: async (agentId: string, includeEnded = true, limit = 50) =>
    (await http.get(`/api/agents/${agentId}/conversations`, {
      params: { include_ended: includeEnded, limit },
    })).data as { count: number; items: AgentConversation[] },
  getConversation: async (cid: string) =>
    (await http.get(`/api/agents/conversations/${cid}`)).data as AgentConversation,
  endConversation: async (cid: string) =>
    (await http.delete(`/api/agents/conversations/${cid}`)).data,
  sendMessage: async (cid: string, text: string) =>
    (await http.post(`/api/agents/conversations/${cid}/messages`, { text })).data,
  interrupt: async (cid: string) =>
    (await http.post(`/api/agents/conversations/${cid}/interrupt`)).data,
}

export function buildAgentWsUrl(cid: string): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  // The agents router mounts under prefix="/api", so the WS path is /api/ws/...
  // Vite proxy covers /api/* with ws:true (see vite.config.ts), so dev works
  // without a separate host:port branch.
  return `${proto}://${location.host}/api/ws/agents/conversations/${cid}`
}
