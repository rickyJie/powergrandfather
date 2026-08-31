<script setup lang="ts">
/**
 * MissionDetailModal — details of ONE mission (one execution of a
 * workflow). Distinct from WorkflowDetailModal which aggregates all
 * missions of a workflow. Opens from calendar chip click, or from a
 * row in WorkflowDetailModal's Missions tab.
 */
import { apiErrorMessage } from '../../lib/apiError'
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { automationApi } from '../../api/automation'
import { useToast } from '../../composables/useToast'

const router = useRouter()
const toast = useToast()

const props = defineProps<{
  open: boolean
  mission: any | null      // full mission dict from /api/missions
  workflowName: string | null
  runs: any[]              // all stage_executions; we filter by mission.id
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'cancelled'): void
  (e: 'retried'): void
}>()

const stages = computed(() =>
  props.mission
    ? props.runs
        .filter((r: any) => r.mission_id === props.mission.id)
        .sort((a: any, b: any) => (a.started_at || '').localeCompare(b.started_at || ''))
    : []
)

// Mission progress — backend-enriched stages_completed / stages_total on
// the mission dict. Falls back to counting terminal-status stage rows
// locally when the backend didn't ship the numbers (older response).
const progressDone = computed<number>(() => {
  const m = props.mission
  if (!m) return 0
  if (typeof m.stages_completed === 'number') return m.stages_completed
  return stages.value.filter(
    (r: any) => r.status === 'succeeded' || r.status === 'failed',
  ).length
})
const progressTotal = computed<number>(() => {
  const t = props.mission?.stages_total
  return typeof t === 'number' && t > 0 ? t : 0
})
const progressPct = computed<number>(() => {
  if (!progressTotal.value) return 0
  return Math.max(0, Math.min(100, Math.round((progressDone.value / progressTotal.value) * 100)))
})
const progressTitle = computed<string>(() =>
  progressTotal.value
    ? `${progressDone.value} of ${progressTotal.value} stages completed (${progressPct.value}%)`
    : `${progressDone.value} stage(s) completed — total unknown`,
)

const cancelling = ref(false)
async function cancelMission() {
  if (!props.mission) return
  if (!confirm(`Cancel mission ${props.mission.id.slice(0, 8)}?\n\nThe current stage's claude session will be stopped.`)) return
  cancelling.value = true
  try {
    await automationApi.cancelMission(props.mission.id)
    toast.success('Mission cancelled')
    emit('cancelled')
  } catch (e) {
    toast.error(`Cancel failed: ${apiErrorMessage(e)}`)
  } finally {
    cancelling.value = false
  }
}

// ---- Retry / Revalidate (P1) ----------------------------------------
// `POST /api/missions/{id}/retry?stage=...&mode=rerun|revalidate`
// Available when mission is in a terminal-ish state (failed/succeeded/
// cancelled). Paused missions have a dedicated resume path; we still
// expose retry for them so a stuck workflow can be pushed forward.
const retryOpen = ref(false)
const retryStage = ref<string>('')
const retryMode = ref<'rerun' | 'revalidate'>('rerun')
const retrying = ref(false)

const canRetry = computed(() => {
  const s = props.mission?.status
  return s === 'failed' || s === 'succeeded' || s === 'cancelled' || s === 'paused'
})

// Default retry-stage suggestion: prefer the mission's current_stage
// (that's where it stopped); fall back to the last stage that has a
// row in `stages`; final fallback is the first declared stage.
function defaultRetryStage(): string {
  const m = props.mission
  if (!m) return ''
  if (m.current_stage) return m.current_stage
  const rows = stages.value
  if (rows.length) return rows[rows.length - 1].stage_name || ''
  return ''
}

function openRetry() {
  retryStage.value = defaultRetryStage()
  retryMode.value = 'rerun'
  retryOpen.value = true
}

async function submitRetry() {
  if (!props.mission) return
  if (!retryStage.value) {
    toast.error('Pick a stage first')
    return
  }
  retrying.value = true
  try {
    await automationApi.retryMission(props.mission.id, retryStage.value, retryMode.value)
    toast.success(`Retry queued: ${retryStage.value} (${retryMode.value})`)
    retryOpen.value = false
    emit('retried')
  } catch (e) {
    toast.error(`Retry failed: ${apiErrorMessage(e)}`)
  } finally {
    retrying.value = false
  }
}

// ---- Stage row expand: outputs + validation evidence (P1) -----------
// Click a stage row → fetch /api/runs/{id} once, cache by run.id, then
// toggle inline detail panel showing generated outputs and any
// review_note the orchestrator persisted. Everything read-only.
const expandedId = ref<string | null>(null)
const runDetails = reactive<Record<string, any>>({})
const runLoading = reactive<Record<string, boolean>>({})
const runErrors = reactive<Record<string, string>>({})

async function toggleStage(runId: string) {
  if (expandedId.value === runId) {
    expandedId.value = null
    return
  }
  expandedId.value = runId
  if (runDetails[runId] || runLoading[runId]) return
  runLoading[runId] = true
  runErrors[runId] = ''
  try {
    runDetails[runId] = await automationApi.getRun(runId)
  } catch (e) {
    runErrors[runId] = apiErrorMessage(e)
  } finally {
    runLoading[runId] = false
  }
}

function outputRawUrl(runId: string, outputId: string): string {
  return `/api/runs/${runId}/outputs/${outputId}/raw`
}

function jumpToSession(sid: string | null | undefined) {
  if (!sid) {
    toast.error('This stage has no session (may have been purged after completion)')
    return
  }
  router.push(`/sessions/${sid}`)
  emit('close')
}

function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const paramEntries = computed(() =>
  Object.entries((props.mission?.parameters as Record<string, unknown>) || {})
)
</script>

<template>
  <div v-if="open && mission" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal mdm-modal panel" role="dialog" aria-modal="true" aria-label="Mission detail">
      <div class="mdm-header">
        <div class="mdm-title-block">
          <div class="mdm-eyebrow">Mission</div>
          <h3 class="serif mdm-title">
            <code>{{ mission.id.slice(0, 8) }}</code>
            <span class="mdm-status" :class="'st-' + mission.status">
              {{ mission.status }}
            </span>
          </h3>
          <div v-if="workflowName" class="mdm-sub">
            of workflow <code>{{ workflowName }}</code>
          </div>
          <div v-if="progressTotal" class="mdm-progress" :title="progressTitle">
            <div class="mdm-progress-bar">
              <div class="mdm-progress-fill" :style="{ width: progressPct + '%' }"></div>
            </div>
            <span class="mdm-progress-label">
              {{ progressDone }} / {{ progressTotal }} stages
              ({{ progressPct }}%)
            </span>
          </div>
        </div>
        <button class="mdm-close" @click="emit('close')" aria-label="Close">×</button>
      </div>

      <div class="mdm-body">
        <!-- Meta -->
        <section class="mdm-meta">
          <div class="mdm-meta-row">
            <span class="mdm-label">Current stage</span>
            <code v-if="mission.current_stage">{{ mission.current_stage }}</code>
            <span v-else class="mdm-muted">—</span>
          </div>
          <div class="mdm-meta-row">
            <span class="mdm-label">Started</span>
            <span>{{ fmtLocal(mission.started_at) }}</span>
          </div>
          <div class="mdm-meta-row">
            <span class="mdm-label">Ended</span>
            <span>{{ fmtLocal(mission.ended_at) }}</span>
          </div>
          <div class="mdm-meta-row">
            <span class="mdm-label">Workspace</span>
            <code>{{ mission.workspace_path }}</code>
          </div>
          <div v-if="paramEntries.length" class="mdm-meta-row">
            <span class="mdm-label">Params</span>
            <div class="mdm-params">
              <div v-for="[k, v] in paramEntries" :key="k" class="mdm-param">
                <code>{{ k }}</code> = <span>{{ v }}</span>
              </div>
            </div>
          </div>
        </section>

        <!-- Failure reason (if any) -->
        <div v-if="mission.failure_reason" class="mdm-failure">
          <b>Failure reason:</b> {{ mission.failure_reason }}
        </div>

        <!-- Actions -->
        <div v-if="mission.status === 'running' || mission.status === 'paused' || canRetry" class="mdm-actions">
          <button
            v-if="mission.status === 'running' || mission.status === 'paused'"
            class="mdm-danger"
            :disabled="cancelling"
            @click="cancelMission"
          >
            {{ cancelling ? 'Cancelling…' : 'Cancel mission' }}
          </button>
          <button
            v-if="canRetry"
            class="mdm-primary"
            :disabled="retrying"
            :title="'Rerun this mission from a chosen stage, or revalidate current workspace files'"
            @click="openRetry"
          >
            Retry…
          </button>
        </div>

        <!-- Stages -->
        <section class="mdm-section">
          <h4 class="mdm-h4">Stages ({{ stages.length }})</h4>
          <div v-if="stages.length === 0" class="mdm-empty">
            No stage runs recorded yet.
          </div>
          <table v-else class="mdm-table">
            <thead>
              <tr>
                <th style="width: 24px"></th>
                <th>Stage</th>
                <th>Status</th>
                <th>Session</th>
                <th>Started</th>
                <th>Ended</th>
                <th>Exit</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="r in stages" :key="r.id">
                <tr
                  class="mdm-stage-row"
                  :class="{
                    'mdm-current': r.stage_name === mission.current_stage && mission.status === 'running',
                    'mdm-expanded': expandedId === r.id,
                  }"
                  @click="toggleStage(r.id)"
                >
                  <td class="mdm-caret" :aria-label="expandedId === r.id ? 'Collapse' : 'Expand'">
                    {{ expandedId === r.id ? '▾' : '▸' }}
                  </td>
                  <td>{{ r.stage_name || '—' }}</td>
                  <td>
                    <span class="mdm-status" :class="'st-' + r.status">
                      {{ r.status }}
                    </span>
                  </td>
                  <td @click.stop>
                    <button
                      v-if="r.session_id"
                      class="mdm-link"
                      :title="r.session_id"
                      @click="jumpToSession(r.session_id)"
                    >
                      {{ r.session_id.slice(0, 8) }} ↗
                    </button>
                    <span v-else class="mdm-muted">—</span>
                  </td>
                  <td>{{ fmtLocal(r.started_at) }}</td>
                  <td>{{ fmtLocal(r.ended_at) }}</td>
                  <td>{{ r.exit_code ?? '—' }}</td>
                </tr>
                <tr v-if="expandedId === r.id" class="mdm-detail-row">
                  <td :colspan="7" class="mdm-detail-cell">
                    <div v-if="runLoading[r.id]" class="mdm-detail-loading">Loading stage detail…</div>
                    <div v-else-if="runErrors[r.id]" class="mdm-detail-error">
                      {{ runErrors[r.id] }}
                    </div>
                    <div v-else-if="runDetails[r.id]" class="mdm-detail-body">
                      <div class="mdm-detail-block">
                        <div class="mdm-detail-label">Outputs</div>
                        <div v-if="!runDetails[r.id].outputs?.length" class="mdm-muted mdm-detail-empty">
                          No outputs recorded for this stage.
                        </div>
                        <ul v-else class="mdm-outputs">
                          <li v-for="o in runDetails[r.id].outputs" :key="o.id" class="mdm-output">
                            <div class="mdm-output-head">
                              <code class="mdm-output-path" :title="o.path">{{ o.path }}</code>
                              <span v-if="o.type" class="mdm-output-type">{{ o.type }}</span>
                              <a
                                class="mdm-output-link"
                                :href="outputRawUrl(r.id, o.id)"
                                target="_blank"
                                rel="noopener noreferrer"
                              >raw ↗</a>
                            </div>
                            <pre v-if="o.preview" class="mdm-output-preview">{{ o.preview }}</pre>
                          </li>
                        </ul>
                      </div>
                      <div class="mdm-detail-block">
                        <div class="mdm-detail-label">Validation / review</div>
                        <pre v-if="runDetails[r.id].review_note" class="mdm-review-note">{{ runDetails[r.id].review_note }}</pre>
                        <div v-else class="mdm-muted mdm-detail-empty">
                          No validation note recorded.
                        </div>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </section>

        <!-- Retry dialog: overlays the modal body without leaving the page -->
        <div v-if="retryOpen" class="mdm-retry-backdrop" @click.self="retryOpen = false">
          <div class="mdm-retry-dialog" role="dialog" aria-label="Retry mission">
            <div class="mdm-retry-title">Retry mission from stage</div>
            <div class="mdm-retry-row">
              <label class="mdm-retry-label">Stage</label>
              <select v-model="retryStage" class="mdm-retry-input">
                <option value="" disabled>— pick a stage —</option>
                <option v-for="r in stages" :key="r.id" :value="r.stage_name">
                  {{ r.stage_name }} ({{ r.status }})
                </option>
              </select>
            </div>
            <div class="mdm-retry-row">
              <label class="mdm-retry-label">Mode</label>
              <div class="mdm-retry-modes">
                <label class="mdm-retry-mode">
                  <input type="radio" value="rerun" v-model="retryMode" />
                  <span>
                    <b>rerun</b>
                    <em>spawn a fresh AUTO session for this stage</em>
                  </span>
                </label>
                <label class="mdm-retry-mode">
                  <input type="radio" value="revalidate" v-model="retryMode" />
                  <span>
                    <b>revalidate</b>
                    <em>skip execution; re-check workspace against stage validation</em>
                  </span>
                </label>
              </div>
            </div>
            <div class="mdm-retry-actions">
              <button class="mdm-secondary" :disabled="retrying" @click="retryOpen = false">Cancel</button>
              <button class="mdm-primary" :disabled="retrying || !retryStage" @click="submitRetry">
                {{ retrying ? 'Submitting…' : 'Submit retry' }}
              </button>
            </div>
          </div>
        </div>

        <!-- Audit log -->
        <section v-if="mission.audit_log && mission.audit_log.length" class="mdm-section">
          <h4 class="mdm-h4">Audit log ({{ mission.audit_log.length }})</h4>
          <ol class="mdm-audit">
            <li v-for="(ev, idx) in mission.audit_log" :key="idx">
              <code>{{ ev.ts }}</code>
              <b>{{ ev.event }}</b>
              <template v-if="ev.from"> · {{ ev.from }} → {{ ev.to }}</template>
              <div v-if="ev.reason" class="mdm-audit-reason">{{ ev.reason }}</div>
            </li>
          </ol>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mdm-modal {
  max-width: 860px;
  width: 94%;
  max-height: 90vh;
  overflow: hidden;
  display: flex; flex-direction: column;
  position: relative;
}
.mdm-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  padding: 18px 22px 12px;
  border-bottom: 1px solid var(--border);
}
.mdm-eyebrow {
  font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute, #94a3b8); margin-bottom: 2px;
}
.mdm-title { margin: 0; font-size: 20px; display: flex; align-items: center; gap: 10px; }
.mdm-sub { font-size: 12px; color: var(--ink-mute); margin-top: 4px; }
.mdm-progress {
  display: flex; align-items: center; gap: 10px; margin-top: 10px;
  max-width: 360px;
}
.mdm-progress-bar {
  flex: 1; height: 6px;
  background: var(--canvas, #f1f5f9);
  border-radius: 3px; overflow: hidden;
}
.mdm-progress-fill {
  height: 100%; background: var(--accent, #4A6D8C);
  transition: width 200ms ease;
}
.mdm-progress-label {
  font-family: 'Geist Mono', monospace; font-size: 11px;
  color: var(--ink-mute); white-space: nowrap;
}
.mdm-close {
  background: transparent; border: none; font-size: 22px; cursor: pointer;
  padding: 2px 8px; color: var(--ink-mute, #94a3b8); box-shadow: none;
}
.mdm-close:hover { color: var(--ink); }

.mdm-body { padding: 16px 22px 22px; overflow-y: auto; flex: 1; }

.mdm-meta { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.mdm-meta-row { display: flex; gap: 12px; font-size: 13px; align-items: baseline; }
.mdm-label {
  min-width: 100px; color: var(--ink-mute);
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
}
.mdm-muted { color: var(--ink-mute); }
.mdm-params { display: flex; flex-direction: column; gap: 2px; }
.mdm-param { font-size: 12px; }

.mdm-failure {
  padding: 10px 12px; margin: 10px 0;
  background: #fef2f2; color: #991b1b; border-radius: 4px;
  font-size: 13px;
}

.mdm-actions { margin: 12px 0; display: flex; gap: 8px; align-items: center; }
.mdm-danger {
  color: #991b1b; border: 1px solid #fecaca; background: transparent;
  padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 13px;
}
.mdm-danger:hover:not(:disabled) { background: #fef2f2; }
.mdm-primary {
  color: #1e40af; border: 1px solid #bfdbfe; background: transparent;
  padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 13px;
}
.mdm-primary:hover:not(:disabled) { background: #eff6ff; }
.mdm-secondary {
  color: var(--ink-mute, #64748b); border: 1px solid var(--border, #e2e8f0);
  background: transparent;
  padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 13px;
}
.mdm-secondary:hover:not(:disabled) { background: #f8fafc; }

/* Stage row expand */
.mdm-stage-row { cursor: pointer; }
.mdm-stage-row:hover { background: rgba(0,0,0,0.02); }
.mdm-stage-row.mdm-expanded { background: #f8fafc; }
.mdm-caret {
  color: var(--ink-mute, #94a3b8);
  font-size: 11px;
  user-select: none;
}
.mdm-detail-row td.mdm-detail-cell {
  background: #fafbfc;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border, #e2e8f0);
}
.mdm-detail-loading, .mdm-detail-error, .mdm-detail-empty {
  font-size: 12px; color: var(--ink-mute);
}
.mdm-detail-error { color: #991b1b; }
.mdm-detail-body {
  display: grid; grid-template-columns: 1fr; gap: 14px;
}
@media (min-width: 900px) {
  .mdm-detail-body { grid-template-columns: 1fr 1fr; }
}
.mdm-detail-block { display: flex; flex-direction: column; gap: 6px; }
.mdm-detail-label {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--ink-mute); font-weight: 600;
}
.mdm-outputs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.mdm-output {
  border: 1px solid var(--border, #e2e8f0);
  border-radius: 4px; padding: 6px 8px; background: #fff;
}
.mdm-output-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mdm-output-path { flex: 1 1 200px; font-size: 12px; overflow-wrap: anywhere; }
.mdm-output-type {
  font-size: 10px; padding: 1px 6px; border-radius: 3px;
  background: #e0e7ff; color: #3730a3; text-transform: uppercase; letter-spacing: 0.4px;
}
.mdm-output-link {
  font-size: 11px; color: #2563eb; text-decoration: none;
}
.mdm-output-link:hover { text-decoration: underline; }
.mdm-output-preview {
  margin: 6px 0 0; padding: 6px 8px;
  background: #f1f5f9; border-radius: 3px; max-height: 160px; overflow: auto;
  font-family: 'SF Mono', Consolas, monospace; font-size: 11.5px; white-space: pre-wrap;
}
.mdm-review-note {
  margin: 0; padding: 8px 10px;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 4px;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12px; white-space: pre-wrap;
  max-height: 260px; overflow: auto;
}

/* Retry dialog */
.mdm-retry-backdrop {
  position: absolute; inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 5;
}
.mdm-retry-dialog {
  background: #fff; padding: 18px 20px; border-radius: 8px;
  min-width: 380px; max-width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.2);
  display: flex; flex-direction: column; gap: 14px;
}
.mdm-retry-title { font-weight: 600; font-size: 14px; }
.mdm-retry-row { display: flex; flex-direction: column; gap: 6px; }
.mdm-retry-label { font-size: 11px; color: var(--ink-mute); text-transform: uppercase; letter-spacing: 0.6px; }
.mdm-retry-input {
  padding: 6px 8px; border: 1px solid var(--border, #cbd5e1); border-radius: 4px;
  font-size: 13px; background: #fff;
}
.mdm-retry-modes { display: flex; flex-direction: column; gap: 8px; }
.mdm-retry-mode {
  display: flex; gap: 8px; align-items: flex-start; cursor: pointer;
  padding: 8px 10px; border: 1px solid var(--border, #e2e8f0); border-radius: 4px;
  font-size: 12.5px;
}
.mdm-retry-mode:hover { background: #f8fafc; }
.mdm-retry-mode input { margin-top: 3px; }
.mdm-retry-mode span { display: flex; flex-direction: column; gap: 2px; }
.mdm-retry-mode em { color: var(--ink-mute); font-style: normal; font-size: 11.5px; }
.mdm-retry-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }

.mdm-section { margin-top: 18px; }
.mdm-h4 {
  margin: 0 0 8px; font-size: 12px; font-weight: 600;
  color: var(--ink-mute); text-transform: uppercase; letter-spacing: 0.6px;
}
.mdm-empty { color: var(--ink-mute); font-size: 13px; padding: 10px 0; }

.mdm-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.mdm-table th, .mdm-table td {
  text-align: left; padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}
.mdm-table th {
  font-weight: 600; color: var(--ink-mute); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.mdm-table tr.mdm-current td { background: #eff6ff; }
.mdm-status {
  display: inline-block; padding: 1px 8px; border-radius: 3px;
  font-size: 11.5px; background: rgba(0,0,0,0.05);
}
.st-succeeded { background: #dcfce7; color: #166534; }
.st-failed { background: #fee2e2; color: #991b1b; }
.st-running { background: #dbeafe; color: #1e40af; }
.st-cancelled { background: #f1f5f9; color: #475569; }
.st-paused { background: #fef3c7; color: #92400e; }
.st-pending { background: #f1f5f9; color: #475569; }
.st-needs_review { background: #fef3c7; color: #92400e; }

.mdm-link {
  background: transparent; border: none; padding: 0;
  color: #2563eb; cursor: pointer;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12.5px;
}
.mdm-link:hover { text-decoration: underline; }

.mdm-audit { margin: 0; padding: 0 0 0 20px; font-size: 12px; }
.mdm-audit li { margin-bottom: 6px; }
.mdm-audit-reason { color: var(--ink-mute); margin-top: 2px; }

code {
  background: rgba(0,0,0,0.05);
  padding: 1px 6px; border-radius: 3px; font-size: 12.5px;
  font-family: 'SF Mono', Consolas, monospace;
}
</style>
