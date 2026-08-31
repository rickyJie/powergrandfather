import { http } from './client'

export interface ProxyEnvVar {
  value: string
  source: 'sniff' | 'file'
}

export interface ProxyEnvView {
  vars: Record<string, ProxyEnvVar>
  sniff_enabled: boolean
  sniff_shell: string | null
  env_file_path: string | null
  env_file_exists: boolean
  warnings: string[]
}

export async function getProxyEnv(): Promise<ProxyEnvView> {
  const r = await http.get<ProxyEnvView>('/api/settings/proxy-env')
  return r.data
}

export async function refreshProxyEnv(): Promise<ProxyEnvView> {
  const r = await http.post<ProxyEnvView>('/api/settings/proxy-env/refresh')
  return r.data
}

// Write ~/.csm/proxy.env with the supplied entries (whitelist-filtered
// server-side). Empty-string values are kept (writing `HTTP_PROXY=`
// explicitly clears the sniffed value at merge time). To remove a key
// entirely, omit it from `entries` (or DELETE for a full wipe).
export async function putProxyEnvFile(entries: Record<string, string>): Promise<ProxyEnvView> {
  const r = await http.put<ProxyEnvView>('/api/settings/proxy-env/file', { entries })
  return r.data
}

export async function deleteProxyEnvFile(): Promise<ProxyEnvView> {
  const r = await http.delete<ProxyEnvView>('/api/settings/proxy-env/file')
  return r.data
}
