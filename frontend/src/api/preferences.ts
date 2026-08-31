import { http } from './client'

export interface Preferences {
  default_agent: string
  supervisor_agent: string | null
  has_completed_first_run: boolean
  default_session_prompt: string | null
  default_session_prompt_enabled: boolean
  default_session_prompt_note: string | null
  default_session_prompt_note_enabled: boolean
  is_first_run: boolean
  created_at: string
  updated_at: string
}

export async function getPreferences(): Promise<Preferences> {
  const r = await http.get<Preferences>('/api/preferences')
  return r.data
}

export interface PreferencePatch {
  default_agent?: string
  supervisor_agent?: string | null
  has_completed_first_run?: boolean
  default_session_prompt?: string | null
  default_session_prompt_enabled?: boolean
  default_session_prompt_note?: string | null
  default_session_prompt_note_enabled?: boolean
}

export async function updatePreferences(patch: PreferencePatch): Promise<Preferences> {
  const r = await http.put<Preferences>('/api/preferences', patch)
  return r.data
}

export async function completeFirstRun(): Promise<Preferences> {
  const r = await http.post<Preferences>('/api/preferences/complete-first-run')
  return r.data
}
