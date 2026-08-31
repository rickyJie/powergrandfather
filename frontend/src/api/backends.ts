import { http } from './client'

export interface BackendStatus {
  installed: boolean
  authenticated: boolean
  version: string | null
  error: string | null
  capabilities: string[]
  usable: boolean
}

// ---- Schema-driven UI (M9.1) ----------------------------------------------
// Each adapter declares `flags_schema` — a list of these. The frontend's
// generic <AdapterFlagsPanel> renders each entry by its `kind` discriminator
// with zero adapter-name branching.

export interface SelectChoice { value: string; label: string }

export interface CheckboxFlag {
  kind: 'checkbox'
  name: string
  label: string
  argv_flag: string
  hint?: string | null
  default_on?: boolean
}
export interface SelectFlag {
  kind: 'select'
  name: string
  label: string
  argv_flag: string
  choices: SelectChoice[]
  hint?: string | null
}
export interface ResumeFlag {
  kind: 'resume'
  name: string
  label: string
  argv_flag: string
  hint?: string | null
}
export interface InfoBlock {
  kind: 'info'
  text: string
}
export type FlagDescriptor = CheckboxFlag | SelectFlag | ResumeFlag | InfoBlock

export interface Backend {
  name: string
  display_name: string
  icon: string             // single-char glyph rendered by AgentBadge
  color: string            // hex or CSS var — drives AgentBadge accent
  enabled: boolean
  status: BackendStatus
  default_argv: string     // pre-populated in New Session dialog when selected
  flags_schema: FlagDescriptor[]
}

export async function listBackends(): Promise<Backend[]> {
  const r = await http.get<Backend[]>('/api/backends')
  return r.data
}

export async function getBackend(name: string): Promise<Backend> {
  const r = await http.get<Backend>(`/api/backends/${encodeURIComponent(name)}`)
  return r.data
}
