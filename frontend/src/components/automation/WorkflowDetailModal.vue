<script setup lang="ts">
/**
 * WorkflowDetailModal — the "hub" for one workflow. Tabs:
 * - Overview: description, review verdict + per-rule breakdown
 * - YAML: view / edit / save (calls PUT /api/workflows/{name})
 * - Runs: this workflow's stage_execution rows (filtered from list)
 * - Actions: Launch mission, Add schedule, Delete workflow
 *
 * Design intent: this replaces the "click row → select" pattern with
 * a full-detail modal so users don't need to hunt for edit / launch /
 * runs in separate places.
 */
import { apiErrorMessage } from '../../lib/apiError'
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { automationApi } from '../../api/automation'
import {
  reviewRuleCounts,
  reviewRules,
  semanticErrorOf,
  semanticVerdictsOf,
} from '../../api/automation'
import type { Mission, Run, Schedule, WorkflowDetail } from '../../api/automation'
import { apiFetch } from '../../api/client'
import { useToast } from '../../composables/useToast'

const router = useRouter()
import ScheduleAlarmPicker from './ScheduleAlarmPicker.vue'
import AgentEditModal from './AgentEditModal.vue'
import LaunchParamsModal from './LaunchParamsModal.vue'

const toast = useToast()

const props = defineProps<{
  open: boolean
  workflowName: string | null
  runs: Run[]           // filtered client-side by mission link
  missions: Mission[]   // all missions (we filter for this workflow)
  schedules: Schedule[] // filtered client-side by workflow_def_id
  initialTab?: 'overview' | 'yaml' | 'runs' | 'schedule'
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'launched'): void
  (e: 'deleted'): void
  (e: 'saved'): void
  (e: 'schedule-added'): void
  (e: 'open-mission', m: Mission): void
}>()

const tab = ref<'overview' | 'yaml' | 'runs' | 'schedule'>('overview')
const loading = ref(false)
const detail = ref<WorkflowDetail | null>(null)
const yamlDraft = ref('')
const editingYaml = ref(false)
const savingYaml = ref(false)
const launchingMission = ref(false)

async function fetchDetail() {
  if (!props.workflowName) return
  loading.value = true
  try {
    detail.value = await automationApi.getWorkflow(props.workflowName)
    yamlDraft.value = detail.value?.yaml_content || ''
    editingYaml.value = false
  } catch (e) {
    toast.error(`Load failed: ${apiErrorMessage(e)}`)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.open, props.workflowName] as const,
  ([isOpen, name]) => {
    if (isOpen && name) {
      tab.value = props.initialTab || 'overview'
      fetchDetail()
    }
  },
  { immediate: true },
)

const reviewCounts = computed(() => reviewRuleCounts(detail.value?.review_report))
const nonPassRules = computed(() =>
  reviewRules(detail.value?.review_report).filter((r) => r.status !== 'pass'),
)

// Pass-2 semantic verdicts (5 categories that R9-R19 can't catch).
const semanticVerdicts = computed(() => semanticVerdictsOf(detail.value?.review_report))
const semanticError = computed(() => semanticErrorOf(detail.value?.review_report))
const semanticIssueCount = computed(() =>
  semanticVerdicts.value.filter(v => v.status !== 'pass').length,
)
const SEMANTIC_LABELS: Record<string, string> = {
  stage_decomposition: 'Stage decomposition',
  output_naming: 'Output naming',
  prompt_completeness: 'Prompt completeness',
  primitive_choice: 'Primitive choice',
  branch_coverage: 'Edge-case coverage',
}
function semanticLabel(category: string): string {
  return SEMANTIC_LABELS[category] || category
}
function semanticIcon(status: string): string {
  if (status === 'fail') return '✗'
  if (status === 'warn') return '⚠'
  return '✓'
}

const canLaunch = computed(() => reviewCounts.value.fail === 0)

// Missions for this workflow
const workflowMissions = computed(() => {
  if (!detail.value) return []
  return props.missions.filter((m: any) => m.workflow_def_id === detail.value?.id)
    .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))
})

// Stage runs — link via mission_id
const workflowRuns = computed(() => {
  const missionIds = new Set(workflowMissions.value.map(m => m.id))
  return props.runs.filter(r => r.mission_id && missionIds.has(r.mission_id))
    .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))
})

function countStages(missionId: string) {
  return props.runs.filter((r: any) => r.mission_id === missionId).length
}

// Mission progress helpers — backend enriches list_missions with
// stages_completed (terminal-status rows) and stages_total (workflow
// definition stage count). Frontend keeps a safe fallback if the
// backend response is old (stages_total undefined → indeterminate bar).
function progressPct(m: any): number {
  const done = Number(m.stages_completed || 0)
  const total = Number(m.stages_total || 0)
  if (!total) return 0
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
}
function progressTitle(m: any): string {
  const done = Number(m.stages_completed || 0)
  const total = Number(m.stages_total || 0)
  if (total) return `${done} of ${total} stage${total === 1 ? '' : 's'} completed (${progressPct(m)}%)`
  return `${done} stage(s) completed — total unknown`
}

// Schedules for this workflow
const workflowSchedules = computed(() => {
  if (!detail.value) return []
  return props.schedules.filter((s: any) => s.workflow_def_id === detail.value?.id)
})

function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function saveYaml() {
  if (!detail.value) return
  savingYaml.value = true
  try {
    const updated = await automationApi.updateWorkflowYaml(detail.value.name, yamlDraft.value)
    detail.value = updated
    yamlDraft.value = updated.yaml_content
    editingYaml.value = false
    emit('saved')
    toast.success('YAML saved and re-reviewed')
  } catch (e) {
    toast.error(`Save failed: ${apiErrorMessage(e)}`)
  } finally {
    savingYaml.value = false
  }
}

// Two-step launch: if the workflow has any declared parameters we open
// LaunchParamsModal first (so required-no-default params can't 500 the
// backend). Otherwise fast-path straight to launchMission with {}.
const showLaunchParams = ref(false)
function launchMission() {
  if (!detail.value) return
  const params = detail.value.parameters || []
  if (params.length === 0) {
    doLaunch({})
  } else {
    showLaunchParams.value = true
  }
}
async function doLaunch(values: Record<string, any>) {
  if (!detail.value) return
  launchingMission.value = true
  showLaunchParams.value = false
  try {
    await automationApi.launchMission(detail.value.name, values)
    toast.success(`Mission launched for ${detail.value.name}`)
    emit('launched')
  } catch (e) {
    toast.error(`Launch failed: ${apiErrorMessage(e)}`)
  } finally {
    launchingMission.value = false
  }
}

const showAlarmPicker = ref(false)
const showAgentEdit = ref(false)
const startingDebug = ref(false)
async function startDebugSession() {
  if (!detail.value || startingDebug.value) return
  startingDebug.value = true
  try {
    const res = await automationApi.startDebugSession(detail.value.name)
    toast.success('Session started — opening it')
    emit('close')
    router.push(`/sessions/${res.session_id}`)
  } catch (e) {
    toast.error(`Failed to start session: ${apiErrorMessage(e)}`)
  } finally {
    startingDebug.value = false
  }
}
async function onAgentEdited() {
  // Re-fetch the workflow's detail so YAML/verdict cards refresh.
  await fetchDetail()
  emit('saved')
}
function addScheduleQuick() {
  if (!detail.value) return
  showAlarmPicker.value = true
}
async function onAlarmSubmit(payload: { cron?: string; run_at?: string; label: string }) {
  if (!detail.value) return
  try {
    const res = await apiFetch('/api/schedules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workflow_def_id: detail.value.id,
        cron: payload.cron,
        run_at: payload.run_at,
        parameters: {},
      }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`)
    toast.success(`Schedule added: ${payload.label}`)
    emit('schedule-added')
  } catch (e) {
    toast.error(`Add schedule failed: ${apiErrorMessage(e)}`)
  }
}

// Cron → human-readable for the schedule list display.
// Handles the shapes the alarm picker emits; falls back to the raw cron
// for anything hand-authored.
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
function describeCron(cron: string | null | undefined): string {
  if (!cron) return ''
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return cron
  const [mm, hh, dom, mon, dow] = parts
  const timeStr = `${hh.padStart(2, '0')}:${mm.padStart(2, '0')}`
  const isEvery = (s: string) => s === '*'
  const asInt = (s: string) => /^\d+$/.test(s) ? parseInt(s, 10) : null
  if (isEvery(dom) && isEvery(mon) && isEvery(dow)) return `daily at ${timeStr}`
  if (isEvery(dom) && isEvery(mon) && !isEvery(dow)) {
    const dows = dow.split(',').map(asInt).filter((n): n is number => n !== null && n >= 0 && n <= 6)
    if (!dows.length) return cron
    return `${dows.map(d => WEEKDAY[d]).join(', ')} at ${timeStr}`
  }
  if (!isEvery(dom) && isEvery(mon) && isEvery(dow)) {
    return `monthly on the ${dom} at ${timeStr}`
  }
  return cron
}

async function deleteWorkflow() {
  if (!detail.value) return
  const name = detail.value.name
  if (!confirm(`Delete workflow "${name}"?\n\nYAML file will be removed from disk. Past missions keep their history.`)) return
  try {
    await automationApi.deleteWorkflow(name)
    toast.success(`Deleted ${name}`)
    emit('deleted')
  } catch (e) {
    toast.error(`Delete failed: ${apiErrorMessage(e)}`)
  }
}

// Archive is soft-delete: row + YAML file stay on disk so past missions
// still resolve their workflow_def_id, but the workflow disappears from
// the default list. Intentionally no un-archive UI — user can re-run
// the reload flow or edit archived_at directly in the DB if they change
// their mind. (P1-B decision: soft delete only.)
async function archiveWorkflow() {
  if (!detail.value) return
  const name = detail.value.name
  if (!confirm(`Archive workflow "${name}"?\n\nHidden from the list, but the YAML file and past missions are kept. This cannot be undone.`)) return
  try {
    await automationApi.archiveWorkflow(name)
    toast.success(`Archived ${name}`)
    // Reuse the `deleted` event so parent closes the modal + refreshes
    // the list; from the parent's perspective an archive is equivalent
    // to a delete (both make the row disappear from the default list).
    emit('deleted')
  } catch (e) {
    toast.error(`Archive failed: ${apiErrorMessage(e)}`)
  }
}

function cancelEdit() {
  yamlDraft.value = detail.value?.yaml_content || ''
  editingYaml.value = false
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal wdm-modal panel" role="dialog" aria-modal="true" :aria-label="`Workflow ${workflowName || ''}`">
      <div class="wdm-header">
        <div class="wdm-title-block">
          <div class="wdm-eyebrow">Workflow</div>
          <h3 class="serif wdm-title">
            <code>{{ workflowName }}</code>
          </h3>
        </div>
        <button class="wdm-close" @click="emit('close')" aria-label="Close">×</button>
      </div>

      <div v-if="loading" class="wdm-loading">Loading…</div>

      <template v-else-if="detail">
        <!-- Tabs -->
        <div class="wdm-tabs">
          <button
            :class="{ active: tab === 'overview' }"
            @click="tab = 'overview'"
          >Overview</button>
          <button
            :class="{ active: tab === 'yaml' }"
            @click="tab = 'yaml'"
          >YAML</button>
          <button
            :class="{ active: tab === 'runs' }"
            @click="tab = 'runs'"
          >Missions · {{ workflowMissions.length }}</button>
          <button
            :class="{ active: tab === 'schedule' }"
            @click="tab = 'schedule'"
          >Schedule · {{ workflowSchedules.length }}</button>
        </div>

        <div class="wdm-body">
          <!-- Overview -->
          <section v-if="tab === 'overview'">
            <div v-if="detail.description" class="wdm-desc">
              {{ detail.description }}
            </div>
            <div v-else class="wdm-desc wdm-desc-empty">(no description)</div>

            <div class="wdm-verdict"
                 :class="reviewCounts.fail > 0 ? 'v-fail' : reviewCounts.warn > 0 ? 'v-warn' : 'v-pass'">
              <div class="wdm-verdict-summary">
                <span class="wdm-verdict-icon">
                  {{ reviewCounts.fail > 0 ? '✗' : reviewCounts.warn > 0 ? '!' : '✓' }}
                </span>
                <span>
                  R9-R19:
                  <b>{{ reviewCounts.pass }}</b> pass ·
                  <b>{{ reviewCounts.warn }}</b> warn ·
                  <b>{{ reviewCounts.fail }}</b> fail
                </span>
              </div>
              <ul v-if="nonPassRules.length" class="wdm-rules">
                <li v-for="r in nonPassRules" :key="r.rule_id"
                    :class="'rule-' + r.status">
                  <b>{{ r.rule_id }}</b>
                  <span class="wdm-rule-tag">{{ r.status }}</span>
                  <div class="wdm-rule-reason">{{ r.reason }}</div>
                </li>
              </ul>
            </div>

            <!-- Pass-2 semantic verdicts — 5 categories that R9-R19 can't see. -->
            <div
              v-if="semanticVerdicts.length || semanticError"
              class="wdm-verdict wdm-semantic"
            >
              <div class="wdm-verdict-summary">
                <span class="wdm-verdict-icon">
                  {{ semanticIssueCount > 0 ? '⚠' : semanticVerdicts.length ? '✓' : '·' }}
                </span>
                <span>
                  Semantic review (Pass 2):
                  <b v-if="semanticVerdicts.length">{{ semanticIssueCount }}</b>
                  <span v-if="semanticVerdicts.length"> to address across {{ semanticVerdicts.length }} categories</span>
                  <span v-else>(not run)</span>
                </span>
              </div>
              <div
                v-if="semanticError"
                class="wdm-rule-reason"
                style="padding: 6px 10px; margin-top: 6px;"
              >
                The Pass-2 semantic review did not complete: {{ semanticError }}
              </div>
              <ul v-else-if="semanticVerdicts.length" class="wdm-rules">
                <li
                  v-for="v in semanticVerdicts"
                  :key="v.category"
                  :class="'rule-' + v.status"
                >
                  <b>{{ semanticIcon(v.status) }} {{ semanticLabel(v.category) }}</b>
                  <span class="wdm-rule-tag">{{ v.status }}</span>
                  <div class="wdm-rule-reason">{{ v.reason }}</div>
                </li>
              </ul>
            </div>

            <div class="wdm-file-path">
              <span class="wdm-label">File</span>
              <code>{{ detail.file_path }}</code>
            </div>

            <div class="wdm-actions">
              <button
                class="primary"
                :disabled="!canLaunch || launchingMission"
                @click="launchMission"
                :title="canLaunch ? '' : 'Fix fail rules first (see YAML tab)'"
              >
                {{ launchingMission ? 'Launching…' : 'Launch mission' }}
              </button>
              <button @click="addScheduleQuick">Add schedule</button>
              <button @click="showAgentEdit = true">🤖 One-shot agent edit</button>
              <button :disabled="startingDebug" @click="startDebugSession">
                {{ startingDebug ? 'Starting…' : '💬 Debug in a claude session' }}
              </button>
              <button @click="archiveWorkflow" title="Hides it from the list, keeping the YAML and past missions. Cannot be undone.">📁 Archive</button>
              <button class="wdm-danger" @click="deleteWorkflow">Delete workflow</button>
            </div>
          </section>

          <!-- YAML editor -->
          <section v-if="tab === 'yaml'" class="wdm-yaml-tab">
            <div class="wdm-yaml-toolbar">
              <div>
                <span class="wdm-label">File</span>
                <code>{{ detail.file_path }}</code>
              </div>
              <div class="wdm-yaml-actions">
                <button v-if="!editingYaml" @click="showAgentEdit = true">🤖 One-shot agent edit</button>
                <button v-if="!editingYaml" :disabled="startingDebug" @click="startDebugSession">
                  {{ startingDebug ? 'Starting…' : '💬 Debug in a session' }}
                </button>
                <button v-if="!editingYaml" @click="editingYaml = true">✎ Edit by hand</button>
                <template v-else>
                  <button @click="cancelEdit" :disabled="savingYaml">Cancel</button>
                  <button
                    class="primary"
                    :disabled="savingYaml || yamlDraft === detail.yaml_content"
                    @click="saveYaml"
                  >
                    {{ savingYaml ? 'Saving…' : 'Save + re-review' }}
                  </button>
                </template>
              </div>
            </div>
            <textarea
              v-model="yamlDraft"
              :readonly="!editingYaml"
              class="wdm-yaml-editor"
              spellcheck="false"
              rows="24"
            ></textarea>
            <p v-if="editingYaml" class="wdm-yaml-hint">
              Save (1) validates the schema, (2) writes to disk, (3) re-runs
              the R9-R19 review. The top-level <code>name:</code> cannot change
              — to rename, create a new workflow.
            </p>
          </section>

          <!-- Missions -->
          <section v-if="tab === 'runs'">
            <div v-if="workflowMissions.length === 0" class="wdm-empty">
              No missions yet — click <b>Launch mission</b> on the Overview tab.
            </div>
            <table v-else class="wdm-table">
              <thead>
                <tr>
                  <th>Mission</th>
                  <th>Status</th>
                  <th>Current stage</th>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>Progress</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="m in workflowMissions"
                  :key="m.id"
                  class="wdm-row-clickable"
                  @click="emit('open-mission', m)"
                >
                  <td><code>{{ m.id.slice(0, 8) }}</code></td>
                  <td>
                    <span class="wdm-status" :class="'st-' + m.status">{{ m.status }}</span>
                  </td>
                  <td>
                    <code v-if="m.current_stage">{{ m.current_stage }}</code>
                    <span v-else class="wdm-muted">—</span>
                  </td>
                  <td>{{ fmtLocal(m.started_at) }}</td>
                  <td>{{ fmtLocal(m.ended_at) }}</td>
                  <td class="wdm-progress-cell">
                    <div class="wdm-progress" :title="progressTitle(m)">
                      <div class="wdm-progress-bar">
                        <div class="wdm-progress-fill" :style="{ width: progressPct(m) + '%' }"></div>
                      </div>
                      <span class="wdm-progress-label">
                        {{ m.stages_completed || 0 }}<span v-if="m.stages_total"> / {{ m.stages_total }}</span>
                      </span>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <!-- Schedules -->
          <section v-if="tab === 'schedule'">
            <div class="wdm-actions" style="margin-bottom: 12px;">
              <button @click="addScheduleQuick">+ Add schedule</button>
            </div>
            <div v-if="workflowSchedules.length === 0" class="wdm-empty">
              No schedules — <b>Add schedule</b> to set up recurring or one-shot runs.
            </div>
            <table v-else class="wdm-table">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Next run</th>
                  <th>Last run</th>
                  <th>Enabled</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="s in workflowSchedules" :key="s.id">
                  <td>
                    <span v-if="s.cron">
                      {{ describeCron(s.cron) }}
                      <code class="wdm-rule-raw">{{ s.cron }}</code>
                    </span>
                    <span v-else>
                      one-off @ {{ fmtLocal(s.run_at) }}
                    </span>
                  </td>
                  <td>{{ fmtLocal(s.next_run_at) }}</td>
                  <td>{{ fmtLocal(s.last_run_at) }}</td>
                  <td>{{ s.enabled ? '✓' : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </section>
        </div>
      </template>
    </div>

    <ScheduleAlarmPicker
      :open="showAlarmPicker"
      :title="detail ? `Schedule ${detail.name}` : 'Set a schedule'"
      @close="showAlarmPicker = false"
      @submit="onAlarmSubmit"
    />

    <AgentEditModal
      :open="showAgentEdit"
      :workflow-name="detail?.name || null"
      @close="showAgentEdit = false"
      @edited="onAgentEdited"
    />

    <LaunchParamsModal
      :open="showLaunchParams"
      :workflow-name="detail?.name || null"
      :parameters="detail?.parameters || []"
      @close="showLaunchParams = false"
      @submit="doLaunch"
    />
  </div>
</template>

<style scoped>
.wdm-modal {
  max-width: 900px;
  width: 94%;
  max-height: 90vh;
  overflow: hidden;
  display: flex; flex-direction: column;
}
.wdm-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 18px 22px 8px;
  border-bottom: 1px solid var(--border);
}
.wdm-eyebrow {
  font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute, #94a3b8); margin-bottom: 2px;
}
.wdm-title { margin: 0; font-size: 20px; }
.wdm-close {
  background: transparent; border: none; font-size: 22px; cursor: pointer;
  padding: 2px 8px; color: var(--ink-mute, #94a3b8); box-shadow: none;
}
.wdm-close:hover { color: var(--ink); transform: none; }

.wdm-loading { padding: 40px; text-align: center; color: var(--ink-mute); }

.wdm-tabs {
  display: flex; gap: 4px;
  padding: 8px 22px 0;
  border-bottom: 1px solid var(--border);
}
.wdm-tabs button {
  border: none; background: transparent; border-radius: 0;
  padding: 8px 12px; font-size: 13px; color: var(--ink-mute, #64748b);
  border-bottom: 2px solid transparent; box-shadow: none;
}
.wdm-tabs button:hover { color: var(--ink); background: transparent; transform: none; }
.wdm-tabs button.active {
  color: var(--ink); border-bottom-color: var(--ink);
}

.wdm-body {
  padding: 16px 22px 22px;
  overflow-y: auto;
  flex: 1;
}

.wdm-desc {
  margin: 0 0 14px;
  color: var(--ink);
  font-size: 14px;
  line-height: 1.55;
}
.wdm-desc-empty { color: var(--ink-mute, #94a3b8); font-style: italic; }

.wdm-verdict {
  padding: 12px 14px;
  border-radius: 6px;
  border-left: 3px solid transparent;
  margin-bottom: 14px;
  background: var(--canvas);
}
.v-pass { border-left-color: #16a34a; background: #f0fdf4; }
.v-warn { border-left-color: #f59e0b; background: #fffbeb; }
.v-fail { border-left-color: #ef4444; background: #fef2f2; }
.wdm-verdict-summary { display: flex; gap: 8px; align-items: center; font-size: 13.5px; }
.wdm-verdict-icon { font-weight: 700; font-size: 16px; }

.wdm-rules { list-style: none; margin: 8px 0 0; padding: 0; }
.wdm-rules li {
  padding: 6px 10px; margin: 4px 0;
  background: rgba(255,255,255,0.7);
  border-radius: 4px;
  border-left: 2px solid transparent;
}
.rule-fail { border-left-color: #ef4444; }
.rule-warn { border-left-color: #f59e0b; }
.rule-pass { border-left-color: #16a34a; }
.wdm-semantic {
  border-left-color: transparent;
  background: var(--canvas);
  border-top: 1px dashed var(--border);
  padding-top: 12px; margin-top: 6px;
}
.wdm-rule-tag {
  display: inline-block; margin-left: 6px; font-size: 10.5px;
  text-transform: uppercase; padding: 1px 6px; border-radius: 3px;
  background: rgba(0,0,0,0.05); color: var(--ink);
}
.wdm-rule-reason { font-size: 12.5px; margin-top: 3px; }

.wdm-file-path {
  display: flex; gap: 8px; align-items: center;
  margin: 10px 0; font-size: 12.5px; color: var(--ink-mute);
}
.wdm-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px;
  color: var(--ink-mute, #94a3b8);
}

.wdm-actions {
  display: flex; gap: 8px; flex-wrap: wrap;
  margin-top: 16px;
}
.wdm-danger { color: #b91c1c; }
.wdm-danger:hover { border-color: #b91c1c; }

.wdm-yaml-tab {
  display: flex; flex-direction: column; gap: 10px;
}
.wdm-yaml-toolbar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  flex-wrap: wrap;
}
.wdm-yaml-actions { display: flex; gap: 6px; }
.wdm-yaml-editor {
  width: 100%;
  min-height: 380px;
  font-family: 'SF Mono', Consolas, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.45;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  resize: vertical;
  box-sizing: border-box;
  background: var(--canvas, #fafafa);
  color: var(--ink);
}
.wdm-yaml-editor:read-only {
  background: var(--card);
  cursor: default;
}
.wdm-yaml-hint {
  font-size: 12px; color: var(--ink-mute); margin: 4px 0 0;
}

.wdm-empty {
  padding: 24px; text-align: center; color: var(--ink-mute);
  border: 1px dashed var(--border); border-radius: 6px;
}
.wdm-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
}
.wdm-table th, .wdm-table td {
  padding: 6px 10px; text-align: left;
  border-bottom: 1px solid var(--border);
}
.wdm-table th {
  font-weight: 600; color: var(--ink-mute); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.wdm-status {
  display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: 11.5px;
  background: rgba(0,0,0,0.05);
}
.st-succeeded { background: #dcfce7; color: #166534; }
.st-failed { background: #fee2e2; color: #991b1b; }
.st-running { background: #dbeafe; color: #1e40af; }
.st-cancelled { background: #f1f5f9; color: #475569; }
.st-paused { background: #fef3c7; color: #92400e; }
.st-pending { background: #f1f5f9; color: #475569; }

/* Mission progress bar column (local:380d5b52). Denominator is
   stages_total from the workflow definition; numerator is the count of
   stage_execution rows in a terminal state. */
.wdm-progress-cell { min-width: 140px; }
.wdm-progress { display: flex; align-items: center; gap: 8px; }
.wdm-progress-bar {
  position: relative;
  flex: 1; height: 6px;
  background: var(--canvas, #f1f5f9);
  border-radius: 3px; overflow: hidden;
}
.wdm-progress-fill {
  height: 100%; background: var(--accent, #4A6D8C);
  transition: width 200ms ease;
}
.wdm-progress-label {
  font-family: 'Geist Mono', monospace; font-size: 11px;
  color: var(--ink-mute); white-space: nowrap;
}
.st-needs_review { background: #fef3c7; color: #92400e; }

.wdm-row-clickable { cursor: pointer; }
.wdm-row-clickable:hover { background: var(--canvas); }
.wdm-muted { color: var(--ink-mute); }
.wdm-rule-raw {
  font-size: 11px; color: var(--ink-mute); margin-left: 6px;
  background: rgba(0,0,0,0.04);
}

code {
  background: rgba(0,0,0,0.05);
  padding: 1px 6px; border-radius: 3px; font-size: 12.5px;
  font-family: 'SF Mono', Consolas, monospace;
}
</style>
