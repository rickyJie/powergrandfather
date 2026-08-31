import { http } from './client'

export type BudgetScopeType = 'global' | 'project' | 'task' | 'source' | 'model' | 'session'
export type BudgetPeriod = 'window_5h' | 'hourly' | 'daily' | 'weekly' | 'monthly'
export type BudgetAction = 'warn' | 'block'

export interface Budget {
  id: string
  name: string
  enabled: boolean
  scope_type: BudgetScopeType
  scope_value: string | null
  period: BudgetPeriod
  token_limit: number | null
  cost_limit: number | null
  warn_pct: number
  action: BudgetAction
  notify_channel: string[]
  cooldown_minutes: number
  last_fired_at: string | null
  last_state: string | null
  created_at: string | null
  updated_at: string | null
}

export interface BudgetStatus {
  budget_id: string
  name: string
  scope_type: BudgetScopeType
  scope_value: string | null
  period: BudgetPeriod
  period_start: string
  period_end: string
  current_tokens: number
  current_cost_usd: number
  msg_count: number
  token_limit: number | null
  cost_limit: number | null
  pct_tokens: number | null
  pct_cost: number | null
  effective_pct: number
  state: 'ok' | 'warn' | 'breached'
  warn_pct: number
  action: BudgetAction
  notify_channel: string[]
}

export const budgetsApi = {
  list: async (): Promise<{ items: Budget[] }> =>
    (await http.get('/api/budgets')).data,
  create: async (body: Partial<Budget>): Promise<Budget> =>
    (await http.post('/api/budgets', body)).data,
  patch: async (id: string, body: Partial<Budget>): Promise<Budget> =>
    (await http.patch(`/api/budgets/${id}`, body)).data,
  remove: async (id: string) =>
    (await http.delete(`/api/budgets/${id}`)).data,
  allStatus: async (): Promise<{ items: BudgetStatus[] }> =>
    (await http.get('/api/budgets/status')).data,
  status: async (id: string): Promise<BudgetStatus> =>
    (await http.get(`/api/budgets/${id}/status`)).data,
}
