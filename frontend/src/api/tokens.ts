import { http } from './client'

export type TokenFilters = {
  model?: string[]
  project?: string[]
  source?: string[]
  task?: string[]
  command_type?: string[]
  session?: string[]
  agent?: string[]      // M12 — CLI-adapter scope
  start?: string
  end?: string
}

function filtersToParams(f?: TokenFilters): Record<string, any> {
  if (!f) return {}
  const p: Record<string, any> = {}
  for (const k of ['model', 'project', 'source', 'task', 'command_type', 'session', 'agent'] as const) {
    const v = f[k]
    if (v && v.length) p[k] = v
  }
  if (f.start) p.start = f.start
  if (f.end) p.end = f.end
  return p
}

export const tokensApi = {
  current: async (hours = 5, filters?: TokenFilters) =>
    (await http.get('/api/tokens/current', {
      params: { hours, ...filtersToParams(filters) },
    })).data,
  quota: async () => (await http.get('/api/tokens/quota')).data,
  history: async (hours = 24, granularity = 'hour', filters?: TokenFilters) =>
    (await http.get('/api/tokens/history', {
      params: { hours, granularity, ...filtersToParams(filters) },
    })).data,
  top: async (
    scope = 'session',
    hours = 5,
    limit = 10,
    sort_by = 'cache_creation_tokens',
    filters?: TokenFilters,
  ) =>
    (await http.get('/api/tokens/top', {
      params: { scope, hours, limit, sort_by, ...filtersToParams(filters) },
    })).data,
  facet: async (facet: string, hours = 168, limit = 50) =>
    (await http.get(`/api/tokens/facets/${facet}`, { params: { hours, limit } })).data,
  toolsTop: async (
    hours = 24,
    limit = 20,
    sort_by: 'total_tokens' | 'cost' | 'count' = 'total_tokens',
    filters?: TokenFilters,
  ) =>
    (await http.get('/api/tokens/tools/top', {
      params: { hours, limit, sort_by, ...filtersToParams(filters) },
    })).data,
  spikeTurns: async (hours = 24, limit = 20, filters?: TokenFilters) =>
    (await http.get('/api/tokens/spike-turns', {
      params: { hours, limit, ...filtersToParams(filters) },
    })).data,
  exportCsvUrl: (filters?: TokenFilters) => {
    const qs = new URLSearchParams()
    const p = filtersToParams(filters)
    for (const [k, v] of Object.entries(p)) {
      if (Array.isArray(v)) v.forEach(x => qs.append(k, String(x)))
      else qs.append(k, String(v))
    }
    return `/api/tokens/export.csv?${qs.toString()}`
  },
  hitObservations: async (limit = 50) =>
    (await http.get('/api/tokens/hit-observations', { params: { limit } })).data,
  periodDelta: async (hours = 168, filters?: TokenFilters) =>
    (await http.get('/api/tokens/period-delta', {
      params: { hours, ...filtersToParams(filters) },
    })).data,
  dataRange: async (filters?: TokenFilters) =>
    (await http.get('/api/tokens/data-range', {
      params: filtersToParams(filters),
    })).data,
  // M14: `agent` selects which adapter's snapshot to fetch/refresh.
  // Default 'claude' matches pre-M14 behaviour.
  usageLive: async (agent = 'claude') =>
    (await http.get('/api/tokens/usage-live', { params: { agent } })).data,
  usageLiveRefresh: async (agent = 'claude') =>
    (await http.post(
      '/api/tokens/usage-live/refresh',
      {},
      { params: { agent }, timeout: 60000 },
    )).data,
  // Alert rules moved to `agentAlertsApi` in ./agent_alerts.ts.
}
