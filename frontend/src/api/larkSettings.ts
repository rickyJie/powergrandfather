import { http } from './client'

export interface LarkSettingsView {
  enabled: boolean
  chat_id: string | null
  user_id: string | null
  dedup_window_sec: number
  dnd_hours: number[]
  tz: string | null
  enabled_types: Record<string, boolean>
  cli_installed: boolean
  updated_at: string | null
}

export interface LarkSettingsPatch {
  enabled?: boolean
  chat_id?: string | null
  user_id?: string | null
  dedup_window_sec?: number
  dnd_hours?: number[]
  tz?: string | null
  enabled_types?: Record<string, boolean>
}

export interface TestPushResult {
  sent: boolean
  error: string | null
  duration_ms: number
}

export async function getLarkSettings(): Promise<LarkSettingsView> {
  const r = await http.get<LarkSettingsView>('/api/settings/lark')
  return r.data
}

// Patch semantics: fields omitted from `patch` are left unchanged on
// the server. Empty-string chat_id/user_id clears the value.
export async function updateLarkSettings(
  patch: LarkSettingsPatch,
): Promise<LarkSettingsView> {
  const r = await http.put<LarkSettingsView>('/api/settings/lark', patch)
  return r.data
}

// Fires a synthetic push using current DB config. Server bounds this
// with an 8s wait_for so the UI spinner has a bound. Returns a
// result object rather than throwing on transport failure (the error
// message is surfaced in `error`).
export async function testLarkPush(): Promise<TestPushResult> {
  const r = await http.post<TestPushResult>('/api/settings/lark/test')
  return r.data
}
