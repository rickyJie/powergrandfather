<script setup lang="ts">
/**
 * WorkflowsDrawer — list of registered workflow definitions with their
 * server-side review verdicts (R9-R19 structural + contract review).
 *
 * Opens from the "🧾 Workflows" toolbar button; parallel to RunsDrawer
 * but for the workflow (M8) side, not the automation Task (M4) side.
 *
 * Design intent: the user sees which workflows are registered, whether
 * each passes review, and expands to see the per-rule verdict list so
 * they can grab the reason text and paste it back to Claude to fix.
 */
import { computed, ref, watch } from 'vue'
import { useToast } from '../../composables/useToast'
import { apiFetch } from '../../api/client'
const toast = useToast()

const props = defineProps<{
  open: boolean
}>()
const emit = defineEmits<{
  (e: 'close'): void
}>()

interface RuleVerdict {
  rule_id: string
  status: 'pass' | 'warn' | 'fail'
  reason: string
}

interface WorkflowRow {
  id: string
  name: string
  description: string | null
  file_path: string
  review_status: string
  review_report: { status: string; rules: RuleVerdict[] } | null
  reviewed_at: string | null
}

const workflows = ref<WorkflowRow[]>([])
const loading = ref(false)
const expanded = ref<Record<string, boolean>>({})
const reloading = ref(false)

async function fetchWorkflows() {
  loading.value = true
  try {
    const res = await apiFetch('/api/workflows')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    workflows.value = data.items || []
  } catch (e: any) {
    toast.error(`Load workflows failed: ${e}`)
  } finally {
    loading.value = false
  }
}

async function reloadYaml() {
  reloading.value = true
  try {
    const res = await apiFetch('/api/workflows/reload', { method: 'POST' })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    toast.info(`Reloaded ${data.count} workflow(s)`)
    await fetchWorkflows()
  } catch (e: any) {
    toast.error(`Reload failed: ${e}`)
  } finally {
    reloading.value = false
  }
}

watch(() => props.open, (isOpen) => {
  if (isOpen) fetchWorkflows()
})

function verdictCount(w: WorkflowRow, level: 'fail' | 'warn' | 'pass'): number {
  const rules = w.review_report?.rules || []
  return rules.filter(r => r.status === level).length
}

function fmtLocal(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function toggleExpand(id: string) {
  expanded.value = { ...expanded.value, [id]: !expanded.value[id] }
}

const orderedRules = (w: WorkflowRow) => {
  const rules = w.review_report?.rules || []
  // fails first, then warns, then pass
  const rank: Record<string, number> = { fail: 0, warn: 1, pass: 2 }
  return [...rules].sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3))
}

const summary = computed(() => {
  const total = workflows.value.length
  const passed = workflows.value.filter(w => w.review_status === 'passed').length
  const withWarns = workflows.value.filter(w => verdictCount(w, 'warn') > 0).length
  const rejected = total - passed
  return { total, passed, withWarns, rejected }
})
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal wd-modal panel" role="dialog" aria-modal="true" aria-label="Workflows drawer">
      <div class="wd-header">
        <div>
          <div class="wd-eyebrow">Registered workflows</div>
          <h3 class="serif">Workflows</h3>
          <div class="wd-summary">
            <span>{{ summary.total }} total</span>
            <span class="pill pill-ok">{{ summary.passed }} passed</span>
            <span v-if="summary.withWarns > 0" class="pill pill-warn">{{ summary.withWarns }} with warns</span>
            <span v-if="summary.rejected > 0" class="pill pill-fail">{{ summary.rejected }} rejected</span>
          </div>
        </div>
        <div class="wd-actions">
          <button @click="reloadYaml" :disabled="reloading">
            {{ reloading ? '…' : '↻' }} Reload yaml
          </button>
          <button class="wd-close" @click="emit('close')">×</button>
        </div>
      </div>

      <div class="wd-body">
        <div v-if="loading" class="wd-empty">Loading…</div>
        <div v-else-if="workflows.length === 0" class="wd-empty">
          No workflows registered. Drop a
          <code>&lt;name&gt;.workflow.yaml</code> in <code>tasks/</code> and click
          <b>Reload yaml</b>.
        </div>

        <div v-else class="wd-list">
          <div
            v-for="w in workflows"
            :key="w.id"
            class="wd-row"
            :class="{
              'wd-row-fail': w.review_status === 'rejected',
              'wd-row-warn': verdictCount(w, 'warn') > 0 && w.review_status !== 'rejected',
            }"
          >
            <div class="wd-row-main" @click="toggleExpand(w.id)">
              <div class="wd-row-name">
                <span class="wd-caret">{{ expanded[w.id] ? '▾' : '▸' }}</span>
                <code>{{ w.name }}</code>
              </div>
              <div class="wd-row-desc">
                {{ w.description || '(no description)' }}
              </div>
              <div class="wd-row-status">
                <span
                  class="pill"
                  :class="w.review_status === 'passed' ? 'pill-ok' : 'pill-fail'"
                >{{ w.review_status }}</span>
                <span
                  v-if="verdictCount(w, 'fail') > 0"
                  class="pill pill-fail"
                >{{ verdictCount(w, 'fail') }} fail</span>
                <span
                  v-if="verdictCount(w, 'warn') > 0"
                  class="pill pill-warn"
                >{{ verdictCount(w, 'warn') }} warn</span>
              </div>
              <div class="wd-row-meta">
                <span>{{ fmtLocal(w.reviewed_at) }}</span>
              </div>
            </div>

            <div v-if="expanded[w.id]" class="wd-row-detail">
              <div class="wd-detail-file">
                <span class="wd-muted">File:</span> <code>{{ w.file_path }}</code>
              </div>
              <table class="wd-rules">
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Status</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="r in orderedRules(w)"
                    :key="r.rule_id"
                    :class="`wd-rule wd-rule-${r.status}`"
                  >
                    <td class="wd-rule-id">{{ r.rule_id }}</td>
                    <td>
                      <span class="pill" :class="`pill-${r.status === 'pass' ? 'ok' : r.status === 'warn' ? 'warn' : 'fail'}`">
                        {{ r.status }}
                      </span>
                    </td>
                    <td class="wd-rule-reason">
                      {{ r.reason || '—' }}
                    </td>
                  </tr>
                </tbody>
              </table>
              <div v-if="verdictCount(w, 'fail') > 0" class="wd-tip">
                💡 Some rules failed: paste the reason text to Claude, have it fix the workflow YAML by rule id, then hit Reload yaml.
              </div>
              <div v-else-if="verdictCount(w, 'warn') > 0" class="wd-tip">
                💡 Some rules warned: these don't block anything — accept them, or follow the suggestion.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wd-modal {
  max-width: 1100px;
  width: 96%;
  max-height: 88vh;
  overflow-y: auto;
}
.wd-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 18px 24px 12px;
  border-bottom: 1px solid var(--border-soft, #e2e8f0);
}
.wd-eyebrow {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: var(--text-muted, #94a3b8);
  margin-bottom: 2px;
}
.wd-header h3 { margin: 0 0 6px; }
.wd-summary { display: flex; gap: 6px; align-items: center; font-size: 13px; color: var(--text-muted, #64748b); }
.wd-actions { display: flex; gap: 6px; align-items: flex-start; }
.wd-actions button { padding: 6px 12px; }
.wd-close {
  background: transparent; border: none; font-size: 20px; cursor: pointer;
  padding: 0 8px; color: var(--text-muted, #94a3b8);
}

.wd-body { padding: 12px 20px 20px; }
.wd-empty { padding: 40px; text-align: center; color: var(--text-muted, #94a3b8); }

.wd-list { display: flex; flex-direction: column; gap: 8px; }
.wd-row {
  border: 1px solid var(--border-soft, #e2e8f0);
  border-radius: 6px;
  overflow: hidden;
}
.wd-row-fail { border-color: var(--danger, #ef4444); }
.wd-row-warn { border-color: var(--warn, #f59e0b); }

.wd-row-main {
  display: grid;
  grid-template-columns: 1fr 2fr 1fr auto;
  gap: 12px;
  align-items: center;
  padding: 10px 14px;
  cursor: pointer;
  font-size: 13px;
}
.wd-row-main:hover { background: var(--surface-alt, #f8fafc); }
.wd-caret { color: var(--text-muted, #94a3b8); margin-right: 6px; }
.wd-row-name code { font-size: 13px; font-weight: 600; }
.wd-row-desc { color: var(--text-muted, #64748b); font-size: 12px; }
.wd-row-status { display: flex; gap: 4px; }
.wd-row-meta { font-size: 12px; color: var(--text-muted, #94a3b8); text-align: right; }

.wd-row-detail {
  padding: 12px 14px;
  background: var(--surface-alt, #f8fafc);
  border-top: 1px solid var(--border-soft, #e2e8f0);
  font-size: 12px;
}
.wd-detail-file { margin-bottom: 8px; }
.wd-detail-file code { font-size: 11px; }
.wd-muted { color: var(--text-muted, #94a3b8); }

.wd-rules { width: 100%; border-collapse: collapse; }
.wd-rules th {
  text-align: left; padding: 4px 6px; font-size: 11px;
  color: var(--text-muted, #94a3b8); text-transform: uppercase; letter-spacing: 0.5px;
}
.wd-rules td { padding: 6px; vertical-align: top; border-top: 1px solid var(--border-soft, #e2e8f0); }
.wd-rule-id { font-family: monospace; font-weight: 600; width: 50px; }
.wd-rule-reason { color: var(--text, #334155); }
.wd-rule-pass { opacity: 0.55; }

.wd-tip {
  margin-top: 10px;
  padding: 8px 12px;
  background: var(--tip-bg, #fef3c7);
  border-radius: 4px;
  font-size: 12px;
}

.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.4;
}
.pill-ok { background: #dcfce7; color: #166534; }
.pill-warn { background: #fef3c7; color: #92400e; }
.pill-fail { background: #fee2e2; color: #991b1b; }
</style>
