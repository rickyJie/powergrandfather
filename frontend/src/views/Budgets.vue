<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { onMounted, ref } from 'vue'
import { budgetsApi, type Budget, type BudgetStatus } from '../api/budgets'

const budgets = ref<Budget[]>([])
const statuses = ref<Record<string, BudgetStatus>>({})
const showModal = ref(false)

const form = ref({
  name: '',
  scope_type: 'global' as 'global' | 'project' | 'task' | 'source' | 'model' | 'session',
  scope_value: '',
  period: 'daily' as 'window_5h' | 'hourly' | 'daily' | 'weekly' | 'monthly',
  token_limit: null as number | null,
  cost_limit: null as number | null,
  warn_pct: 80,
  action: 'warn' as 'warn' | 'block',
  notify_channel: '' as string,
  cooldown_minutes: 60,
})

async function refresh() {
  const r = await budgetsApi.list(); budgets.value = r.items
  const s = await budgetsApi.allStatus()
  statuses.value = Object.fromEntries(s.items.map(x => [x.budget_id, x]))
}

function statusOf(id: string): BudgetStatus | null {
  return statuses.value[id] ?? null
}

function fmtTok(n: number | null): string {
  if (n === null || n === undefined) return '—'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k'
  return String(n)
}

function fmtCost(n: number | null): string {
  if (n === null || n === undefined) return '—'
  return '$' + n.toFixed(2)
}

async function submit() {
  if (!form.value.name) { alert('name required'); return }
  if (form.value.token_limit === null && form.value.cost_limit === null) {
    alert('at least one of token_limit / cost_limit required'); return
  }
  if (form.value.scope_type !== 'global' && !form.value.scope_value) {
    alert('scope_value required for scope_type=' + form.value.scope_type); return
  }
  try {
    const body: any = {
      name: form.value.name,
      scope_type: form.value.scope_type,
      scope_value: form.value.scope_type === 'global' ? null : form.value.scope_value,
      period: form.value.period,
      token_limit: form.value.token_limit,
      cost_limit: form.value.cost_limit,
      warn_pct: form.value.warn_pct,
      action: form.value.action,
      cooldown_minutes: form.value.cooldown_minutes,
    }
    const nc = form.value.notify_channel.trim()
    if (nc) body.notify_channel = nc.split(',').map(s => s.trim()).filter(Boolean)
    await budgetsApi.create(body)
    showModal.value = false
    Object.assign(form.value, { name: '', scope_value: '', token_limit: null, cost_limit: null, notify_channel: '' })
    await refresh()
  } catch (e) {
    alert('create failed: ' + (apiErrorMessage(e)))
  }
}

async function toggle(b: Budget) {
  await budgetsApi.patch(b.id, { enabled: !b.enabled })
  await refresh()
}

async function remove(b: Budget) {
  if (!confirm(`Delete budget "${b.name}"?`)) return
  await budgetsApi.remove(b.id)
  await refresh()
}

onMounted(refresh)
</script>

<template>
  <div class="b-page">
    <div class="toolbar">
      <button class="primary" @click="showModal = true">+ Budget</button>
      <div class="right"><button @click="refresh">↻ Refresh</button></div>
    </div>

    <div class="b-body">
      <div v-if="!budgets.length" class="empty-state panel">
        <h3 class="serif">No budgets yet</h3>
        <p>A <strong>budget</strong> is a continuous statement of intended spend on a scope.</p>
        <p>Example: <em>"PowerGrandFather project, daily, max 100M tokens, warn at 80%"</em>.</p>
        <button class="primary" @click="showModal = true">Create your first budget</button>
      </div>

      <div v-for="b in budgets" :key="b.id" class="panel budget-card" :class="statusOf(b.id)?.state">
        <div class="row">
          <div class="head">
            <strong>{{ b.name }}</strong>
            <span class="tag" :class="b.enabled ? 'info' : 'idle'">{{ b.enabled ? 'on' : 'off' }}</span>
            <span class="tag" v-if="statusOf(b.id)" :class="statusOf(b.id)!.state">{{ statusOf(b.id)!.state }}</span>
          </div>
          <div class="actions">
            <button @click="toggle(b)">{{ b.enabled ? 'disable' : 'enable' }}</button>
            <button @click="remove(b)">×</button>
          </div>
        </div>
        <div class="meta">
          <span><b>scope:</b> {{ b.scope_type }}<span v-if="b.scope_value">: {{ b.scope_value }}</span></span>
          <span><b>period:</b> {{ b.period }}</span>
          <span v-if="b.token_limit"><b>tokens:</b> ≤ {{ fmtTok(b.token_limit) }}</span>
          <span v-if="b.cost_limit"><b>cost:</b> ≤ {{ fmtCost(b.cost_limit) }}</span>
          <span><b>warn@:</b> {{ b.warn_pct }}%</span>
          <span v-if="b.notify_channel.length"><b>notify:</b> {{ b.notify_channel.join(', ') }}</span>
        </div>
        <div v-if="statusOf(b.id)" class="status">
          <div class="bar"><i :style="{ width: Math.min(100, statusOf(b.id)!.effective_pct) + '%' }"></i></div>
          <div class="status-line">
            <span>{{ statusOf(b.id)!.effective_pct.toFixed(1) }}%</span>
            <span>{{ fmtTok(statusOf(b.id)!.current_tokens) }} tok · {{ fmtCost(statusOf(b.id)!.current_cost_usd) }}</span>
            <span>{{ statusOf(b.id)!.msg_count }} msgs</span>
          </div>
        </div>
      </div>
    </div>

    <div v-if="showModal" class="modal-backdrop" @click.self="showModal = false" role="presentation">
      <div class="modal panel" role="dialog" aria-modal="true" aria-labelledby="new-budget-title">
        <h3 class="serif" id="new-budget-title">New budget</h3>
        <div class="b-form">
          <label>name <input v-model="form.name" placeholder="e.g. daily_tokens" /></label>
          <label>scope type
            <select v-model="form.scope_type">
              <option value="global">global · all usage</option>
              <option value="project">project · by path</option>
              <option value="task">task · by name</option>
              <option value="source">source · interactive / auto / agent</option>
              <option value="model">model · opus / sonnet / haiku</option>
              <option value="session">session · by claude_session_id</option>
            </select>
          </label>
          <label v-if="form.scope_type !== 'global'">scope value
            <input v-model="form.scope_value" :placeholder="form.scope_type === 'project' ? '/path/to/PowerGrandFather' : form.scope_type === 'task' ? 'sample_pipeline_iter' : form.scope_type === 'source' ? 'interactive' : form.scope_type === 'model' ? 'opus' : '...'" />
          </label>
          <label>period
            <select v-model="form.period">
              <option value="window_5h">5h rolling</option>
              <option value="hourly">hourly</option>
              <option value="daily">daily (UTC)</option>
              <option value="weekly">weekly (Mon-Sun)</option>
              <option value="monthly">monthly</option>
            </select>
          </label>
          <label>token limit
            <input type="number" v-model.number="form.token_limit" placeholder="e.g. 100000000" />
          </label>
          <label>cost limit USD
            <input type="number" step="0.01" v-model.number="form.cost_limit" placeholder="e.g. 25" />
          </label>
          <label>warn at %
            <input type="number" v-model.number="form.warn_pct" />
          </label>
          <label>action
            <select v-model="form.action">
              <option value="warn">warn · notify only</option>
              <option value="block">block · notify (block is advisory in v1)</option>
            </select>
          </label>
          <label class="full">notify channels (comma-separated, e.g. <code>lark</code>)
            <input v-model="form.notify_channel" placeholder="lark" />
          </label>
          <label>cooldown min
            <input type="number" v-model.number="form.cooldown_minutes" />
          </label>
        </div>
        <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px;">
          <button @click="showModal = false">Cancel</button>
          <button class="primary" @click="submit">Create</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.b-page { display: flex; flex-direction: column; height: 100%; }
.b-body { padding: 0 20px 20px; overflow: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }

.empty-state { padding: 28px; text-align: center; }
.empty-state h3 { margin-bottom: 10px; }
.empty-state p { color: var(--ink-mute); margin-bottom: 6px; }
.empty-state button { margin-top: 12px; }

.budget-card { padding: 16px; border-left: 4px solid transparent; }
.budget-card.warn { border-left-color: var(--pastel-yellow-fg); }
.budget-card.breached { border-left-color: var(--pastel-red-fg); }
.budget-card .row { display: flex; justify-content: space-between; align-items: center; }
.budget-card .head { display: flex; gap: 8px; align-items: center; }
.budget-card .head strong { font-size: 14px; }
.budget-card .actions { display: flex; gap: 4px; }
.budget-card .meta { display: flex; flex-wrap: wrap; gap: 14px; color: var(--ink-mute); font-size: 12px; margin-top: 8px; }
.budget-card .meta b { color: var(--ink); font-weight: 500; }
.budget-card .status { margin-top: 10px; }
.budget-card .status .bar { height: 6px; background: rgba(0,0,0,0.08); border-radius: 3px; overflow: hidden; }
.budget-card .status .bar > i { display: block; height: 100%; background: var(--ink); transition: width 400ms var(--ease-soft); }
.budget-card.warn .status .bar > i { background: var(--pastel-yellow-fg); }
.budget-card.breached .status .bar > i { background: var(--pastel-red-fg); }
.budget-card .status-line { display: flex; justify-content: space-between; margin-top: 4px; font-size: 12px; color: var(--ink-mute); }

.tag { padding: 2px 8px; border-radius: 8px; font-size: 11px; }
.tag.info { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.tag.idle { background: var(--canvas); color: var(--ink-mute); }
.tag.ok { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.tag.warn { background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg); }
.tag.breached { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }

.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 99; }
.modal { padding: 24px; min-width: 36rem; max-width: 44rem; }
.b-form { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.b-form label { display: grid; gap: 4px; font-size: 12px; color: var(--ink-mute); }
.b-form label.full { grid-column: 1 / -1; }
.b-form code { font-family: 'Geist Mono', monospace; font-size: 11px; }
</style>
