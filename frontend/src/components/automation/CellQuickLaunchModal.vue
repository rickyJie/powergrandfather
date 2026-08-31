<script setup lang="ts">
/**
 * CellQuickLaunchModal — clicking a calendar cell opens this modal so
 * the user can pick a workflow to run for that time-slot.
 *
 * The calendar cell is 1-hour granularity, but the user can nudge the
 * minute within that hour before submitting (0/15/30/45 by default,
 * or any 5-min step).
 *
 * Two paths, decided by the final timestamp:
 * - **Past / <= 30 min from now** → only "Launch now" (mission).
 * - **Future** → main button = "Launch at that time" (schedule with
 *   `run_at`), plus a secondary "Launch now" for a one-shot test.
 */
import { apiErrorMessage } from '../../lib/apiError'
import { computed, ref, watch } from 'vue'
import { automationApi } from '../../api/automation'
import { apiFetch } from '../../api/client'
import { useToast } from '../../composables/useToast'
import LaunchParamsModal from './LaunchParamsModal.vue'

const toast = useToast()

const props = defineProps<{
  open: boolean
  workflows: any[]
  /** Local date + hour of the cell clicked. */
  day: Date | null
  hour: number | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'launched'): void
  (e: 'scheduled'): void
}>()

const selectedWorkflow = ref<string>('') // workflow name
const busy = ref(false)
const minute = ref(0) // user-picked minute within the cell's hour

watch(
  () => [props.open, props.day, props.hour] as const,
  ([isOpen, d, h]) => {
    if (!isOpen) return
    selectedWorkflow.value = ''
    busy.value = false
    // If cell is in the current hour, default to next 5-min round;
    // otherwise minute = 0.
    const now = new Date()
    if (d instanceof Date && h !== null
        && d.getFullYear() === now.getFullYear()
        && d.getMonth() === now.getMonth()
        && d.getDate() === now.getDate()
        && h === now.getHours()) {
      const bump = Math.ceil((now.getMinutes() + 1) / 5) * 5
      minute.value = Math.min(55, bump)
    } else {
      minute.value = 0
    }
  },
  { immediate: true },
)

const cellDate = computed<Date | null>(() => {
  if (!props.day || props.hour === null) return null
  const d = new Date(props.day)
  d.setHours(props.hour, minute.value, 0, 0)
  return d
})

const isPast = computed(() => {
  const c = cellDate.value
  if (!c) return false
  // <= 30 min into the future counts as "just do it now" (scheduler
  // ticks are coarse enough that scheduling that close is silly).
  return c.getTime() <= Date.now() + 30 * 60 * 1000
})

function pad2(n: number): string { return String(n).padStart(2, '0') }
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const cellLabel = computed(() => {
  const c = cellDate.value
  if (!c) return ''
  return `${c.getFullYear()}-${pad2(c.getMonth() + 1)}-${pad2(c.getDate())} (${WEEKDAY[c.getDay()]}) ${pad2(c.getHours())}:${pad2(c.getMinutes())}`
})

const relativeLabel = computed(() => {
  const c = cellDate.value
  if (!c) return ''
  const diffMs = c.getTime() - Date.now()
  const absMin = Math.abs(Math.round(diffMs / 60000))
  if (absMin < 1) return 'now'
  if (absMin < 60) return diffMs >= 0 ? `in ${absMin} min` : `${absMin} min ago`
  const absHr = Math.round(absMin / 60)
  if (absHr < 24) return diffMs >= 0 ? `in ${absHr}h` : `${absHr}h ago`
  const absDay = Math.round(absHr / 24)
  return diffMs >= 0 ? `in ${absDay}d` : `${absDay}d ago`
})

const workflowChoices = computed(() =>
  [...props.workflows].sort((a: any, b: any) => a.name.localeCompare(b.name))
)

const minuteChoices = Array.from({ length: 12 }, (_, i) => i * 5)

// Any workflow with declared parameters needs a params modal before
// launch/schedule so required-no-default params can't 500 the backend.
type LaunchAction = 'now' | 'schedule'
const paramModalOpen = ref(false)
const pendingAction = ref<LaunchAction | null>(null)

const selectedWorkflowObj = computed(() =>
  props.workflows.find((w: any) => w.name === selectedWorkflow.value) || null
)

function needsParamModal(): boolean {
  const params = (selectedWorkflowObj.value?.parameters || []) as any[]
  return params.length > 0
}

function launchNow() {
  if (!selectedWorkflow.value || busy.value) return
  if (needsParamModal()) {
    pendingAction.value = 'now'
    paramModalOpen.value = true
    return
  }
  doLaunchNow({})
}

function scheduleAtCell() {
  if (!selectedWorkflow.value || !cellDate.value || busy.value) return
  if (needsParamModal()) {
    pendingAction.value = 'schedule'
    paramModalOpen.value = true
    return
  }
  doScheduleAtCell({})
}

function onParamsSubmit(values: Record<string, any>) {
  paramModalOpen.value = false
  if (pendingAction.value === 'now') doLaunchNow(values)
  else if (pendingAction.value === 'schedule') doScheduleAtCell(values)
  pendingAction.value = null
}

async function doLaunchNow(values: Record<string, any>) {
  busy.value = true
  try {
    await automationApi.launchMission(selectedWorkflow.value, values)
    toast.success(`Mission launched for ${selectedWorkflow.value}`)
    emit('launched')
    emit('close')
  } catch (e) {
    toast.error(`Launch failed: ${apiErrorMessage(e)}`)
  } finally {
    busy.value = false
  }
}

async function doScheduleAtCell(values: Record<string, any>) {
  const wf = selectedWorkflowObj.value
  if (!wf || !cellDate.value) return
  busy.value = true
  try {
    const res = await apiFetch('/api/schedules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workflow_def_id: wf.id,
        run_at: cellDate.value.toISOString(),
        parameters: values,
      }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`)
    toast.success(`Scheduled ${selectedWorkflow.value} @ ${cellLabel.value}`)
    emit('scheduled')
    emit('close')
  } catch (e) {
    toast.error(`Schedule failed: ${apiErrorMessage(e)}`)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal panel cql-modal" role="dialog" aria-modal="true" aria-label="Quick launch">
      <div class="cql-header">
        <div>
          <div class="cql-eyebrow">Quick launch</div>
          <h3 class="serif cql-title">Run a workflow in this slot</h3>
        </div>
        <button class="cql-close" @click="emit('close')" aria-label="Close">×</button>
      </div>

      <div class="cql-body">
        <div class="cql-time-card">
          <div class="cql-time-main">{{ cellLabel || '—' }}</div>
          <div class="cql-time-rel">{{ relativeLabel }}</div>
        </div>

        <label class="cql-field">
          <span class="cql-label">Pick a workflow</span>
          <select v-model="selectedWorkflow">
            <option value="">— select —</option>
            <option v-for="wf in workflowChoices" :key="wf.id" :value="wf.name">
              {{ wf.name }}
            </option>
          </select>
        </label>

        <label class="cql-field">
          <span class="cql-label">Minute (0-55, in steps of 5)</span>
          <div class="cql-min-row">
            <select v-model.number="minute">
              <option v-for="m in minuteChoices" :key="m" :value="m">:{{ pad2(m) }}</option>
            </select>
            <span class="cql-min-hint">
              The calendar only goes down to the hour; this pins the minute.
            </span>
          </div>
        </label>

        <div class="cql-note">
          <template v-if="isPast">
            That slot is in the past or less than 30 minutes away — launching now is the only option.
          </template>
          <template v-else>
            For a future slot you can launch now, or schedule it for that time.
          </template>
        </div>

        <div class="cql-actions">
          <button
            v-if="!isPast"
            class="primary"
            :disabled="!selectedWorkflow || busy"
            @click="scheduleAtCell"
          >
            {{ busy ? '…' : `Launch at ${cellLabel}` }}
          </button>
          <button
            :class="{ primary: isPast }"
            :disabled="!selectedWorkflow || busy"
            @click="launchNow"
          >
            {{ busy ? '…' : 'Launch now' }}
          </button>
          <button @click="emit('close')">Cancel</button>
        </div>
      </div>
    </div>

    <LaunchParamsModal
      :open="paramModalOpen"
      :workflow-name="selectedWorkflow || null"
      :parameters="(selectedWorkflowObj?.parameters || []) as any"
      :submit-label="pendingAction === 'schedule' ? `Launch at ${cellLabel}` : 'Launch now'"
      :context-line="pendingAction === 'schedule' ? cellLabel : ''"
      @close="paramModalOpen = false; pendingAction = null"
      @submit="onParamsSubmit"
    />
  </div>
</template>

<style scoped>
.cql-modal {
  max-width: 480px; width: 92%;
  padding: 0;
  overflow: hidden;
}
.cql-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--border);
}
.cql-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute); margin-bottom: 3px;
}
.cql-title { margin: 0; font-size: 17px; color: var(--ink); }
.cql-close {
  background: transparent; border: none; font-size: 22px; padding: 0 6px;
  color: var(--ink-mute); cursor: pointer; box-shadow: none;
}
.cql-close:hover { color: var(--ink); transform: none; }

.cql-body { padding: 14px 18px 18px; font-size: 13px; }

.cql-time-card {
  padding: 10px 12px; margin-bottom: 14px;
  background: var(--pastel-blue-bg);
  border-left: 3px solid var(--pastel-blue-fg);
  border-radius: 4px;
}
.cql-time-main {
  font-size: 15px; font-weight: 600; color: var(--pastel-blue-fg);
  font-family: 'Geist Mono', 'SF Mono', monospace;
}
.cql-time-rel {
  font-size: 12px; color: var(--ink-mute); margin-top: 2px;
}

.cql-field { display: block; margin-bottom: 10px; }
.cql-label {
  display: block; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--ink-mute);
  margin-bottom: 4px; font-weight: 600;
}
.cql-field select {
  width: 100%;
}
.cql-min-row {
  display: flex; align-items: center; gap: 10px;
}
.cql-min-row select { width: 90px; }
.cql-min-hint { font-size: 11.5px; color: var(--ink-mute); }

.cql-note {
  padding: 8px 12px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 12px; color: var(--ink-mute);
  margin: 8px 0 12px;
}

.cql-actions { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
