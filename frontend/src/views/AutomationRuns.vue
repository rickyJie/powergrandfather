<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { automationApi } from '../api/automation'
import type { Mission, Run, Schedule, Workflow } from '../api/automation'
import { projectsApi, type Project } from '../api/projects'
import { useToast } from '../composables/useToast'
import { useShortcuts } from '../composables/useShortcuts'
import { useEventStream, type CSMEvent } from '../composables/useEventStream'
const toast = useToast()
const route = useRoute()
const router = useRouter()
import NewWorkflowModal from '../components/automation/NewWorkflowModal.vue'
import WeekCalendar from '../components/automation/WeekCalendar.vue'
import CellQuickLaunchModal from '../components/automation/CellQuickLaunchModal.vue'
import WorkflowList from '../components/automation/WorkflowList.vue'
import NewProjectModal from '../components/automation/NewProjectModal.vue'
import WorkflowDetailModal from '../components/automation/WorkflowDetailModal.vue'
import WorkflowGuideModal from '../components/automation/WorkflowGuideModal.vue'
import MissionDetailModal from '../components/automation/MissionDetailModal.vue'
import WorkflowsDrawer from '../components/automation/WorkflowsDrawer.vue'

// `shallowRef` for the four collections that only ever get replaced
// wholesale in `refresh()` — every read is `.find()` / `.filter()` and
// every write is `runs.value = r.items`. Deep proxying the 100-200 row
// arrays every refresh cycle was a measurable chunk of the perceived
// list-diff cost; skipping it is safe because we never mutate a row
// object in place.
const runs = shallowRef<Run[]>([])
const schedules = shallowRef<Schedule[]>([])

// P2: primary automation entities.
const workflows = shallowRef<Workflow[]>([])
const missions = shallowRef<Mission[]>([])
const projects = shallowRef<Project[]>([])
const selectedWorkflowId = ref<string | null>(null)

// NewProjectModal — either creates a blank bucket (workflowNames left
// empty) or materializes an existing heuristic auto-group (frontend
// hands over the list of workflow names in that bucket, backend binds
// them atomically). See WorkflowList.vue's `convert-auto-group` emit.
const showNewProject = ref(false)
const newProjectPrefill = ref<{ name: string; workflow_names: string[] } | null>(null)
function openNewProject(prefill?: { name: string; workflow_names: string[] }) {
  newProjectPrefill.value = prefill ?? null
  showNewProject.value = true
}
async function onProjectSaved() {
  showNewProject.value = false
  newProjectPrefill.value = null
  await refresh()
}
async function onRenameProject(project_id: string, name: string) {
  try {
    await projectsApi.update(project_id, { name })
    await refresh()
  } catch (e) {
    toast.error(`Rename failed: ${apiErrorMessage(e)}`)
  }
}
async function onArchiveProject(project_id: string) {
  if (!confirm('Archive this project? Its workflows will fall back to the auto-detected bucket.')) return
  try {
    await projectsApi.archive(project_id)
    await refresh()
  } catch (e) {
    toast.error(`Archive failed: ${apiErrorMessage(e)}`)
  }
}
async function onMoveWorkflow(workflow_name: string, project_id: string | null) {
  try {
    await automationApi.moveWorkflow(workflow_name, project_id)
    await refresh()
  } catch (e) {
    toast.error(`Move failed: ${apiErrorMessage(e)}`)
  }
}
async function onMergeAutoGroup(payload: { project_id: string; workflow_names: string[] }) {
  try {
    const res = await projectsApi.absorb(payload.project_id, payload.workflow_names)
    toast.success(`Merged ${res.bound} workflow(s) into ${res.project_name}`)
    await refresh()
  } catch (e) {
    toast.error(`Merge failed: ${apiErrorMessage(e)}`)
  }
}

// New-workflow modal — one-shot server-side authoring flow (backend spawns
// `claude -p`, generates YAML, runs review). Replaces "new task" as the
// primary automation entry once M4 is fully deprecated (P2).
const showNewWorkflow = ref(false)
function openNewWorkflow() { showNewWorkflow.value = true }
async function onWorkflowGenerated(workflowId?: string) {
  await refresh()
  // Auto-open the newly generated workflow's detail modal so the user
  // sees the review verdict and can Launch a mission immediately.
  const wf = workflows.value.find((w) => w.id === workflowId)
    ?? workflows.value.find((w) => w.name === detailModalName.value)
  if (wf) {
    detailModalName.value = wf.name
  }
}

// Fired by the detail modal after Launch / Save / Delete / Add-schedule.
// Common re-fetch of everything so counts + review verdicts stay in sync.
async function onDetailAction() {
  await refresh()
}

// Launch / Schedule entrypoints for a workflow row live inside
// WorkflowDetailModal — it correctly opens LaunchParamsModal and
// ScheduleAlarmPicker on its own (see components/automation/
// WorkflowDetailModal.vue). No parent-level launch/schedule handlers.

// Workflow authoring help modal — teaches users how to have Claude
// generate a workflow YAML for them (they don't need to learn the YAML).
const showWorkflowGuide = ref(false)

// Workflows drawer — list of registered workflow definitions with
// server-side review verdicts (R9-R19).
const showWorkflows = ref(false)

// Workflow detail modal — click a WorkflowList row opens this.
const detailModalName = ref<string | null>(null)
const detailInitialTab = ref<'overview' | 'yaml' | 'runs' | 'schedule'>('overview')
function openWorkflowDetail(wf: Workflow) {
  detailInitialTab.value = 'overview'
  detailModalName.value = wf.name
}

// Mission detail modal — click a calendar chip opens this (one specific mission).
const missionModalMission = ref<Mission | null>(null)
const missionModalWorkflowName = ref<string | null>(null)
function openMissionDetail(mission: Mission) {
  const wf = workflows.value.find((w) => w.id === mission.workflow_def_id)
  missionModalWorkflowName.value = wf?.name || null
  missionModalMission.value = mission
}
function closeMissionDetail() {
  missionModalMission.value = null
  missionModalWorkflowName.value = null
}
// Event-driven refresh replaces the previous pending + mission poll
// timers. Any session-scope event, assistant-done, or api-error (which
// carries supervisor-review and port-conflict markers) triggers a
// debounced refresh; bursts (a stage transition emits several events in
// a row) coalesce into one fetch. Reconnect always full-refreshes to
// resync anything the server didn't buffer during downtime.
let refreshDebounce: number | null = null
function scheduleRefresh() {
  if (typeof document !== 'undefined' && document.hidden) return
  if (refreshDebounce != null) return
  refreshDebounce = window.setTimeout(() => {
    refreshDebounce = null
    refresh().catch(() => {})
  }, 300)
}
function isAutomationRelevantEvent(e: CSMEvent): boolean {
  if (e.type.startsWith('session.')) return true
  if (e.type === 'message.assistant_done') return true
  // Supervisor review completions are the canonical signal that a
  // mission just flipped into needs-review — refresh so the pill
  // updates without waiting for the next unrelated session event.
  if (e.type === 'supervisor.review_requested') return true
  return false
}
// Force one refresh when the tab becomes visible again — SSE events
// arriving while hidden are dropped by `scheduleRefresh`, so without
// this the list would sit stale on tab-switch-back until something
// happens on the backend.
function onVisibilityChange() {
  if (typeof document !== 'undefined' && !document.hidden) {
    refresh().catch(() => {})
  }
}
let eventSub: { stop: () => void } | null = null

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibilityChange)
  if (refreshDebounce != null) { clearTimeout(refreshDebounce); refreshDebounce = null }
  eventSub?.stop()
  eventSub = null
})

async function refresh() {
  // P2: M4 tasks are gone. Fetch workflows + missions + stage runs + schedules + projects.
  const [w, r, m, s, p] = await Promise.all([
    automationApi.listWorkflows(),
    automationApi.listRuns({ limit: 100 }),
    automationApi.listMissions({ limit: 200 }),
    automationApi.listSchedules(),
    projectsApi.list(),
  ])
  workflows.value = w.items
  runs.value = r.items
  missions.value = m.items
  schedules.value = s.items
  projects.value = p.items
  if (!selectedWorkflowId.value && workflows.value.length) {
    selectedWorkflowId.value = workflows.value[0].id
  }
}

// Helpers that used to live here (reviewPillLabel/Tooltip, canLaunch,
// taskNextRun, taskRunStats) all moved into TaskList.vue.

async function reloadYaml() {
  // P2: only workflows to reload — M4 tasks are gone.
  try {
    const res = await automationApi.reloadWorkflows()
    await refresh()
    toast.success(`Reloaded: ${res.count} workflow(s)`)
    // Auto-open Workflows drawer so users see the freshly-loaded rows.
    if ((res.count ?? 0) > 0) {
      showWorkflows.value = true
    }
  } catch (e) {
    toast.error(`Reload failed: ${apiErrorMessage(e)}`)
  }
}

// P2: click on a calendar cell → CellQuickLaunchModal to pick workflow +
// either launch now or schedule at that cell's timestamp.
const quickLaunchDay = ref<Date | null>(null)
const quickLaunchHour = ref<number | null>(null)
const quickLaunchOpen = ref(false)
function openQuickLaunch(day: Date, hour: number) {
  quickLaunchDay.value = day
  quickLaunchHour.value = hour
  quickLaunchOpen.value = true
}
function openScheduleEdit(s: Schedule) {
  // Route calendar's `edit schedule` click into the owning workflow's
  // Schedule tab — WorkflowDetailModal has the ScheduleAlarmPicker
  // wiring plus delete/enable/disable, so we consolidate all schedule
  // editing there instead of duplicating the picker at the parent.
  const wf = workflows.value.find((w) => w.id === s.workflow_def_id)
  if (!wf) {
    toast.error(`Workflow not found for schedule ${s.id?.slice(0, 8)}`)
    return
  }
  detailInitialTab.value = 'schedule'
  detailModalName.value = wf.name
}

// Calendar internals (7-day window + 3h binning + chip render) live in
// WeekCalendar.vue. The parent only owns events the calendar emits.

const cancellingRunId = ref<string | null>(null)
async function onRunChipRightClick(r: Run) {
  // Right-click on a Run chip: cancel if running, else fall through to
  // opening the detail modal.
  if (r.status !== 'running') {
    onRunChipClick(r)
    return
  }
  if (!confirm(`Cancel run ${r.id.slice(0, 8)}? The underlying claude session will be stopped.`)) return
  cancellingRunId.value = r.id
  try {
    await automationApi.cancelRun(r.id)
    await refresh()
  } catch (e) {
    toast.error(`cancel failed: ${apiErrorMessage(e)}`)
  } finally {
    cancellingRunId.value = null
  }
}
function onRunChipClick(r: Run) {
  // Calendar chip = one stage_execution → its owning mission gets its
  // own dedicated modal (distinct from the workflow-aggregate view).
  const mission = missions.value.find((m) => m.id === r.mission_id)
  if (!mission) {
    toast.error(`No mission found for run ${r.id?.slice(0, 8)}`)
    return
  }
  openMissionDetail(mission)
}

// Drag-and-drop reschedule on the calendar — calendar emits the ID + target slot.
async function onScheduleDrop(scheduleId: string, day: Date, hour: number) {
  const s = schedules.value.find(x => x.id === scheduleId)
  if (!s) return
  const newCron = `0 ${hour} * * ${day.getDay()}`
  if (newCron === s.cron) return
  try {
    // PATCH keeps workflow_def_id / parameters / enabled intact. The
    // previous delete-then-create passed the retired M4 `task_def_id`
    // to createSchedule and silently sent an invalid body.
    await automationApi.updateSchedule(s.id, { cron: newCron })
    await refresh()
  } catch (e) {
    toast.error(`Reschedule failed: ${apiErrorMessage(e)}`)
  }
}

function onChipClick(s: Schedule) {
  // Schedule chip click: jump to the owning workflow's Schedule tab.
  const wf = workflows.value.find((w) => w.id === s.workflow_def_id)
  if (!wf) {
    toast.error(`Workflow not found for schedule ${s.id?.slice(0, 8)}`)
    return
  }
  detailInitialTab.value = 'schedule'
  detailModalName.value = wf.name
}

async function toggleSchedule(s: Schedule) {
  if (s.enabled) await automationApi.disableSchedule(s.id)
  else await automationApi.enableSchedule(s.id)
  await refresh()
}
async function delSchedule(id: string) {
  if (!confirm('Delete this schedule?')) return
  await automationApi.deleteSchedule(id)
  await refresh()
}

onMounted(async () => {
  await refresh()
  eventSub = useEventStream({
    onEvent(e) { if (isAutomationRelevantEvent(e)) scheduleRefresh() },
    onReconnect() { refresh().catch(() => {}) },
  })
  document.addEventListener('visibilitychange', onVisibilityChange)
})

// Deep-link from NotificationPanel: when a `auto_needs_review` notif is
// clicked with a mission_id in metadata, it routes to
// `/automation?mission_id=<id>`. Watch for the query param, open the
// mission detail modal, then clear the param so re-opening the panel
// doesn't re-trigger the modal on refresh.
watch(
  () => route.query.mission_id,
  async (mid) => {
    if (typeof mid !== 'string' || !mid) return
    if (!missions.value.some((m) => m.id === mid)) {
      await refresh().catch(() => {})
    }
    const target = missions.value.find((m) => m.id === mid)
    if (target) {
      openMissionDetail(target)
    } else {
      toast.error(`Mission ${mid.slice(0, 8)} not found (may have been purged)`)
    }
    router.replace({ path: '/automation', query: {} })
  },
  { immediate: true },
)

// KPI derivation for the header strip — F10. Numbers come off the
// arrays we already loaded, so no extra API call.
const nowMs = Date.now()
const kpi = computed(() => {
  const dayAgo = nowMs - 86_400_000
  const weekAgo = nowMs - 7 * 86_400_000
  // `started_at` is null until the orchestrator picks the mission up; a
  // never-started mission sorts to epoch 0 and so falls outside every window.
  const startedMs = (m: Mission) => new Date(m.started_at || 0).getTime()
  const missions24h = missions.value.filter((m) => startedMs(m) >= dayAgo)
  const running = missions.value.filter((m) => m.status === 'running').length
  const failed7d = missions.value.filter(
    (m) => startedMs(m) >= weekAgo && m.status === 'failed',
  ).length
  // Type predicate, not a plain truthiness filter — it's what lets the
  // `new Date(...)` below see a `string` instead of `string | null`.
  const scheduled = (s: Schedule): s is Schedule & { next_run_at: string } =>
    s.enabled && s.next_run_at !== null
  const upcoming = schedules.value
    .filter(scheduled)
    .sort((a, b) => a.next_run_at.localeCompare(b.next_run_at))[0] || null
  let nextLabel: string | null = null
  if (upcoming) {
    const wf = workflows.value.find((w) => w.id === upcoming.workflow_def_id)
    const dtMs = new Date(upcoming.next_run_at).getTime() - nowMs
    const inStr = dtMs < 0
      ? 'overdue'
      : dtMs < 3_600_000 ? `in ${Math.max(1, Math.floor(dtMs/60_000))}m`
      : dtMs < 86_400_000 ? `in ${Math.floor(dtMs/3_600_000)}h`
      : `in ${Math.floor(dtMs/86_400_000)}d`
    nextLabel = `${wf?.name || 'workflow'} · ${inStr}`
  }
  return {
    runs24h: missions24h.length,
    ok24h: missions24h.filter((m) => m.status === 'succeeded').length,
    fail24h: missions24h.filter((m) => m.status === 'failed').length,
    running,
    failed7d,
    nextLabel,
  }
})

// Keyboard shortcuts — fire only when focus isn't in an input. `Esc` closes
// the topmost open modal; `n` opens new task; `r` reloads.
useShortcuts({
  'n': () => openNewWorkflow(),
  'r': () => refresh(),
  'esc': () => {
    // Close the topmost modal in priority order.
    if (detailModalName.value) { detailModalName.value = null; return }
    if (showNewWorkflow.value) { showNewWorkflow.value = false; return }
    if (showWorkflowGuide.value) { showWorkflowGuide.value = false; return }
    if (showWorkflows.value) { showWorkflows.value = false; return }
  },
})
</script>

<template>
  <div class="auto-page">
    <header class="auto-header">
      <div class="auto-header-top">
        <div class="auto-title-block">
          <h1 class="auto-title">Automation</h1>
          <p class="auto-subtitle">
            <span class="kpi-num">{{ kpi.runs24h }}</span> runs · 24h
            <span v-if="kpi.ok24h" class="kpi-ok">✓ {{ kpi.ok24h }}</span>
            <span v-if="kpi.fail24h" class="kpi-fail">✗ {{ kpi.fail24h }}</span>
            <span v-if="kpi.running" class="kpi-running">● {{ kpi.running }} running</span>
            <span v-if="kpi.failed7d" class="kpi-sep">·</span>
            <span v-if="kpi.failed7d" class="kpi-fail">{{ kpi.failed7d }} failed 7d</span>
            <span v-if="kpi.nextLabel" class="kpi-sep">·</span>
            <span v-if="kpi.nextLabel" class="kpi-next">next: {{ kpi.nextLabel }}</span>
          </p>
        </div>
        <div class="auto-actions">
          <div class="auto-actions-primary">
            <button class="primary" @click="openNewWorkflow">+ New workflow</button>
          </div>
          <div class="auto-actions-secondary">
            <button @click="reloadYaml" title="Re-scan tasks/*.workflow.yaml">↻ Reload</button>
            <button
              @click="showWorkflowGuide = true"
              title="How to author a new workflow (Claude can write it for you)"
              aria-label="Workflow authoring help"
            >? Help</button>
          </div>
        </div>
      </div>
    </header>

    <div class="auto-layout">
      <WorkflowList
        :workflows="workflows"
        :missions="missions"
        :schedules="schedules"
        :projects="projects"
        :selected-workflow-id="selectedWorkflowId"
        @select="(id) => (selectedWorkflowId = id)"
        @open-detail="openWorkflowDetail"
        @new-project="() => openNewProject()"
        @convert-auto-group="(payload) => openNewProject(payload)"
        @merge-auto-group="onMergeAutoGroup"
        @rename-project="onRenameProject"
        @archive-project="onArchiveProject"
        @move-workflow="onMoveWorkflow"
      />

      <WeekCalendar
        :schedules="schedules"
        :runs="runs"
        :workflows="workflows"
        :missions="missions"
        @cell-click="(day, hour) => openQuickLaunch(day, hour)"
        @schedule-click="onChipClick"
        @run-click="onRunChipClick"
        @run-right-click="onRunChipRightClick"
        @schedule-drop="onScheduleDrop"
        @schedule-toggle="toggleSchedule"
        @schedule-delete="(s) => delSchedule(s.id)"
        @schedule-edit="openScheduleEdit"
      />
    </div>

    <NewWorkflowModal
      :open="showNewWorkflow"
      @close="showNewWorkflow = false"
      @generated="onWorkflowGenerated"
    />

    <NewProjectModal
      :open="showNewProject"
      :prefill="newProjectPrefill"
      @close="showNewProject = false; newProjectPrefill = null"
      @saved="onProjectSaved"
    />

    <WorkflowGuideModal
      :open="showWorkflowGuide"
      @close="showWorkflowGuide = false"
    />

    <WorkflowsDrawer
      :open="showWorkflows"
      @close="showWorkflows = false"
    />

    <WorkflowDetailModal
      :open="!!detailModalName"
      :workflow-name="detailModalName"
      :runs="runs"
      :missions="missions"
      :schedules="schedules"
      :initial-tab="detailInitialTab"
      @close="detailModalName = null"
      @open-mission="openMissionDetail"
      @launched="onDetailAction"
      @deleted="onDetailAction(); detailModalName = null"
      @saved="onDetailAction"
      @schedule-added="onDetailAction"
    />

    <MissionDetailModal
      :open="!!missionModalMission"
      :mission="missionModalMission"
      :workflow-name="missionModalWorkflowName"
      :runs="runs"
      @close="closeMissionDetail"
      @cancelled="() => { refresh(); closeMissionDetail() }"
      @retried="() => { refresh() }"
    />

    <CellQuickLaunchModal
      :open="quickLaunchOpen"
      :workflows="workflows"
      :day="quickLaunchDay"
      :hour="quickLaunchHour"
      @close="quickLaunchOpen = false"
      @launched="refresh"
      @scheduled="refresh"
    />

  </div>
</template>

<style scoped>
.auto-page { display: flex; flex-direction: column; height: 100%; }

/* Page header — added by ui_review_auto_1 F4/F5/F10 */
.auto-header {
  padding: 16px 24px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--card);
}
.auto-header-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}
.auto-title-block { min-width: 0; flex: 1; }
.auto-title {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 24px;
  font-weight: 500;
  letter-spacing: 0.2px;
  color: var(--ink);
  line-height: 1.15;
}
.auto-subtitle {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--ink-mute);
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.kpi-num { color: var(--ink); font-weight: 600; }
.kpi-ok { color: #16a34a; font-weight: 500; }
.kpi-fail { color: #dc2626; font-weight: 500; }
.kpi-running { color: #1e40af; font-weight: 500; }
.kpi-next { color: var(--accent-soft-fg); font-weight: 500; background: var(--accent-soft-bg); padding: 2px 8px; border-radius: 999px; }
.kpi-sep { color: var(--border-strong); }

.auto-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.auto-actions-primary, .auto-actions-secondary {
  display: flex; gap: 6px; align-items: center;
}
.auto-actions-secondary {
  border-left: 1px solid var(--border);
  padding-left: 12px;
}

.auto-layout {
  flex: 1;
  display: grid;
  grid-template-columns: 340px 1fr;
  gap: 16px;
  padding: 16px 20px 20px;
  min-height: 0;
}


/* legacy form-grid kept for the param-launch modal */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.form-grid label { display: grid; gap: 4px; font-size: 12px; color: var(--ink-mute); }
.form-grid label.full { grid-column: 1 / -1; }
.modal .hint { color: var(--ink-mute); font-size: 12px; margin-top: 8px; }

/* ── Note: NewTaskModal / ScheduleModal / ReviewReportModal /
   TaskDetailModal / WeekCalendar styles all live in their own SFCs.
   The only styles below are for parent-owned bits: toolbar, task list,
   and the legacy param-launch modal. ── */

.x-deleted-stale-modal-css {
  /* placeholder kept so the block above doesn't accidentally chain to
     the wrong selector — actual stale CSS deleted in P1-2.1e refactor. */
}
.nt-header h3 { margin: 0 0 4px; font-size: 20px; }
.nt-sub {
  margin: 0; font-size: 12.5px; color: var(--ink-mute); line-height: 1.5;
}
.nt-sub code, .nt-section code, .nt-footer-note code {
  font-family: 'Geist Mono', monospace; font-size: 11.5px;
  background: var(--canvas); padding: 1px 5px; border-radius: 3px;
  color: var(--ink);
}
.nt-section { display: flex; flex-direction: column; gap: 6px; }
.nt-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-mute); font-weight: 500;
}
.nt-input {
  width: 100%;
  padding: 8px 10px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 14px; color: var(--ink);
  transition: border-color 120ms var(--ease-soft);
}
.nt-input:focus { outline: none; border-color: var(--ink); }
.nt-input-mono { font-family: 'Geist Mono', monospace; font-size: 13px; }
.nt-cwd-row { display: flex; gap: 8px; align-items: stretch; }
.nt-cwd-row .nt-input { flex: 1; }
.nt-browse {
  padding: 0 14px; font-size: 13px;
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  color: var(--ink); white-space: nowrap; cursor: pointer;
  transition: border-color 120ms var(--ease-soft);
}
.nt-browse:hover { border-color: var(--ink); }

/* probe panel */
.nt-probe {
  padding: 12px 14px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.nt-probe-line {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; line-height: 1.8;
  color: var(--ink-mute);
}
.nt-probe-line .nt-icon {
  width: 16px; height: 16px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; font-size: 11px; font-weight: 600;
  background: var(--border); color: var(--ink-faint);
  flex-shrink: 0;
}
.nt-probe-line.ok .nt-icon { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.nt-probe-line.ok { color: var(--ink); }
.nt-probe-line.bad .nt-icon { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.nt-probe-line.bad { color: var(--pastel-red-fg); }
.nt-probe-line.muted { color: var(--ink-faint); }
.nt-probe-line.nt-pending { color: var(--ink-mute); font-style: italic; }
.nt-spin {
  display: inline-block; animation: ntspin 0.9s linear infinite;
}
@keyframes ntspin { to { transform: rotate(360deg); } }
.nt-hint { color: var(--ink-faint); font-size: 12px; }
.nt-required {
  font-size: 10.5px; color: var(--pastel-red-fg);
  background: var(--pastel-red-bg);
  padding: 1px 6px; border-radius: 999px;
  margin-left: auto;
}
.nt-optional {
  font-size: 10.5px; color: var(--ink-faint);
  background: var(--card); border: 1px solid var(--border);
  padding: 1px 6px; border-radius: 999px;
  margin-left: auto;
}
.nt-warn {
  margin: 8px 0 0; padding: 8px 10px;
  background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg);
  font-size: 12px; border-radius: 6px; line-height: 1.5;
}
.nt-warn code { background: rgba(255,255,255,0.5); color: inherit; }

.nt-footer-note {
  margin: 0;
  font-size: 12px; color: var(--ink-faint); line-height: 1.5;
  padding-top: 4px;
  border-top: 1px dashed var(--border);
}

.nt-actions {
  display: flex; gap: 10px; justify-content: flex-end; margin-top: 4px;
}
.nt-actions button {
  min-width: 96px;
  padding: 8px 14px;
}
.nt-actions button.primary:disabled {
  opacity: 0.45; cursor: not-allowed;
}


/* modal */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex; align-items: center; justify-content: center;
  z-index: 99;
}
.modal {
  padding: 24px;
  min-width: 28rem; max-width: 36rem;
}
.param-row { margin: 12px 0; display: flex; flex-direction: column; gap: 4px; }
.param-row input { font-family: 'Geist Mono', monospace; }
</style>
