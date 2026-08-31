import { http } from './client'

export type AgentAlertRule = {
  id: string
  name: string
  enabled: boolean
  nl_description: string
  threshold_spec: Record<string, any>
  check_script: string
  poll_interval_sec: number
  cooldown_sec: number
  channels: string[]
  escalate: boolean
  lark_chat_id?: string | null
  lark_user_id?: string | null
  last_fired_at?: string | null
  last_error?: string | null
  snoozed_until?: string | null
  created_at?: string | null
  updated_at?: string | null
  rule_metadata?: Record<string, any>
}

export type GenerateRequest = {
  name: string
  nl_description: string
  threshold_spec: Record<string, any>
  escalate: boolean
}

export type GenerateResponse = {
  ok: boolean
  script: string | null
  dry_run: {
    fired: boolean
    payload: Record<string, any> | null
    error: string | null
    duration_sec: number
  } | null
  duration_sec: number
  error: string | null
  window_snapshot: Record<string, any>
}

export type CreateRequest = {
  name: string
  nl_description: string
  threshold_spec: Record<string, any>
  check_script: string
  poll_interval_sec: number
  cooldown_sec: number
  channels: string[]
  escalate: boolean
  lark_chat_id?: string
  lark_user_id?: string
  enabled?: boolean
}

export type PresetParamSpec = {
  key: string
  label: string
  unit: string
  default: number
  min: number
  max: number
  step: number
  is_int: boolean
}

export type PresetDef = {
  id: string
  title: string
  description: string
  notify_example: string
  escalate_default: boolean
  poll_default_sec: number
  cooldown_default_sec: number
  params: PresetParamSpec[]
}

export type FromPresetRequest = {
  preset_id: string
  params: Record<string, number>
  name?: string
  poll_interval_sec?: number
  cooldown_sec?: number
  channels?: string[]
  escalate?: boolean
  lark_chat_id?: string
  lark_user_id?: string
  enabled?: boolean
}

export const agentAlertsApi = {
  list: async (): Promise<{ items: AgentAlertRule[] }> =>
    (await http.get('/api/tokens/agent-alerts')).data,
  presets: async (): Promise<{ items: PresetDef[] }> =>
    (await http.get('/api/tokens/agent-alerts/presets')).data,
  fromPreset: async (body: FromPresetRequest): Promise<AgentAlertRule> =>
    (await http.post('/api/tokens/agent-alerts/from-preset', body)).data,
  updateFromPreset: async (id: string, params: Record<string, number>): Promise<AgentAlertRule> =>
    (await http.post(`/api/tokens/agent-alerts/${id}/update-from-preset`, { params })).data,
  generate: async (body: GenerateRequest): Promise<GenerateResponse> =>
    (await http.post('/api/tokens/agent-alerts/generate', body, { timeout: 180000 })).data,
  create: async (body: CreateRequest): Promise<AgentAlertRule> =>
    (await http.post('/api/tokens/agent-alerts', body)).data,
  patch: async (id: string, body: Partial<CreateRequest> & { enabled?: boolean }): Promise<AgentAlertRule> =>
    (await http.patch(`/api/tokens/agent-alerts/${id}`, body)).data,
  delete: async (id: string): Promise<{ deleted: string }> =>
    (await http.delete(`/api/tokens/agent-alerts/${id}`)).data,
  simulate: async (id: string): Promise<{ ok: boolean; payload: any }> =>
    (await http.post(`/api/tokens/agent-alerts/${id}/simulate`, {})).data,
  snooze: async (id: string, minutes: number): Promise<AgentAlertRule> =>
    (await http.post(`/api/tokens/agent-alerts/${id}/snooze`, { minutes })).data,
  unsnooze: async (id: string): Promise<AgentAlertRule> =>
    (await http.post(`/api/tokens/agent-alerts/${id}/unsnooze`, {})).data,
}
