<script setup lang="ts">
/**
 * WeekCalendar — 7-day × 24-hour grid showing both planned Schedules and
 * actual Runs. Pure view component: emits semantic events to the parent
 * (which owns the data + API calls).
 *
 * Chip vocabulary:
 *  - ● (filled dot)  one-shot schedule (planned)
 *  - ↻ (arrows)      recurring schedule (planned)
 *  - ▶ (triangle)    actual Run (past or in-flight) — colored by status
 */
import { computed, ref } from 'vue'
import type { Mission, Run, Schedule, Workflow } from '../../api/automation'

const props = defineProps<{
  schedules: Schedule[]
  runs: Run[]
  workflows?: Workflow[]  // for resolving mission-owned run → workflow name in tooltips
  missions?: Mission[]    // to hop run.mission_id → mission.workflow_def_id
}>()

const emit = defineEmits<{
  (e: 'cell-click', day: Date, hour: number): void
  (e: 'schedule-click', s: Schedule): void
  (e: 'run-click', r: Run): void
  (e: 'run-right-click', r: Run): void
  (e: 'schedule-drop', scheduleId: string, day: Date, hour: number): void
  (e: 'schedule-toggle', s: Schedule): void
  (e: 'schedule-delete', s: Schedule): void
  (e: 'schedule-edit', s: Schedule): void
}>()

// ---- week navigation (offset 0 = current week, -1 = last, +1 = next) ----
// A "week" here is calendar Mon → Sun (ISO week convention), NOT today + 7.
// The old today-forward window hid yesterday's runs behind the ◀ button and
// made the panel look empty whenever a task had just finished — the user
// expects a Monday-anchored view.
const weekOffset = ref(0)
function currentWeekMondayMidnight(): Date {
  const d = new Date(); d.setHours(0, 0, 0, 0)
  // JS Date.getDay() returns 0=Sun..6=Sat; shift so Monday=0..Sunday=6.
  const daysSinceMonday = (d.getDay() + 6) % 7
  d.setDate(d.getDate() - daysSinceMonday)
  return d
}
const days = computed(() => {
  const start = currentWeekMondayMidnight()
  start.setDate(start.getDate() + weekOffset.value * 7)
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(start); d.setDate(start.getDate() + i); return d
  })
})
const weekLabel = computed(() => {
  const a = days.value[0]
  const b = days.value[6]
  if (weekOffset.value === 0) return 'This week'
  if (weekOffset.value === -1) return 'Last week'
  if (weekOffset.value === 1) return 'Next week'
  const fmt = (d: Date) => `${d.getMonth()+1}/${d.getDate()}`
  return `${fmt(a)} – ${fmt(b)}`
})
function prevWeek() { weekOffset.value-- }
function nextWeek() { weekOffset.value++ }
function thisWeek() { weekOffset.value = 0 }

// ---- granularity: bin hours into 1h / 3h / 6h chunks ----
type BinSize = 1 | 3 | 6
const binSize = ref<BinSize>(3)
// Default to full 24h grid — early feedback (2026-07-12) said users were
// losing early-morning / late-night activity to the 06–22 crop and didn't
// notice the toggle. The condensed range stays available via the toggle
// for users who prefer it, and the choice persists in localStorage.
const show24h = ref<boolean>(localStorage.getItem('csm.cal.show24h') !== '0')
function toggle24h() {
  show24h.value = !show24h.value
  localStorage.setItem('csm.cal.show24h', show24h.value ? '1' : '0')
}
const HOURS = computed(() => {
  const step = binSize.value
  const arr: number[] = []
  const startH = show24h.value ? 0 : 6
  const endH = show24h.value ? 24 : 22
  for (let h = startH; h < endH; h += step) arr.push(h)
  return arr
})

// ---- formatters ----
function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
/** Fallback label when a run/schedule can't be resolved to a workflow name. */
const UNNAMED = '(mission)'

// Runs live under a mission whose workflow_def_id resolves to a workflow name
// in the parent's `workflows` array. This lookup makes chip labels and
// tooltips name the workflow the user launched instead of the fallback.
function workflowNameForRun(r: Run): string | null {
  if (!props.missions || !props.workflows) return null
  if (!r?.mission_id) return null
  const m = props.missions.find((x) => x.id === r.mission_id)
  if (!m) return null
  const wf = props.workflows.find((x) => x.id === m.workflow_def_id)
  return wf?.name || null
}
function workflowNameForSchedule(s: Schedule): string | null {
  if (!props.workflows) return null
  if (!s?.workflow_def_id) return null
  return props.workflows.find((x) => x.id === s.workflow_def_id)?.name || null
}
// Distinct display label for a Run chip: HH:MM start time (or seconds if
// several runs share the same minute) + task name (or mission stage label
// for M8 runs). Guarantees no two chips in the same cell look identical.
function runChipLabel(r: Run): string {
  const hhmm = r.started_at
    ? new Date(r.started_at).toLocaleTimeString(undefined, {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    : '--:--'
  // Prefer workflow name (mission-owned) → stage_name → mission id → run id.
  const name = workflowNameForRun(r)
    || r.stage_name
    || (r.mission_id ? `m:${r.mission_id.slice(0, 6)}` : null)
    || `r:${(r.id || '').slice(0, 6)}`
  const truncated = name.length > 8 ? name.slice(0, 8) + '…' : name
  return `${hhmm} ${truncated}`
}
/** Stable per-workflow chip tint, so two workflows in one cell read apart. */
function workflowColorClass(workflowDefId: string | null | undefined): string {
  const key = workflowDefId || 'mission'
  let h = 0
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) % 4
  return ['chip-a', 'chip-b', 'chip-c', 'chip-d'][h]
}
function runStatusToTagClass(status: string): string {
  if (status === 'succeeded') return 'succeeded'
  if (status === 'failed') return 'failed'
  if (status === 'running') return 'running'
  return 'idle'
}
/**
 * Most recent run belonging to `workflowDefId`, via its missions.
 *
 * Runs carry `mission_id`, not a workflow id, so the hop is mandatory. (The
 * predecessor keyed on `run.task_def_id`, a column P2 dropped — every run's
 * value was `undefined`, so `filter(r => r.task_def_id === undefined)` matched
 * ALL runs and every schedule's tooltip reported the globally-latest run.)
 */
function lastRunStatusForWorkflow(workflowDefId: string | null): string | null {
  if (!workflowDefId) return null
  const missionIds = new Set(
    (props.missions ?? [])
      .filter((m) => m.workflow_def_id === workflowDefId)
      .map((m) => m.id),
  )
  if (!missionIds.size) return null
  const r = props.runs
    .filter((x) => x.mission_id && missionIds.has(x.mission_id))
    .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))[0]
  return r ? `${r.status} (${fmtLocal(r.started_at)})` : null
}
function chipTooltip(s: Schedule): string {
  const name = workflowNameForSchedule(s) || UNNAMED
  const last = lastRunStatusForWorkflow(s.workflow_def_id)
  const lastLine = last ? `\nlast: ${last}` : ''
  if (s.kind === 'once') return `${name}\none-off · ${fmtLocal(s.run_at)}${lastLine}`
  return `${name}\nrecurring · cron: ${s.cron}\nnext: ${fmtLocal(s.next_run_at)}${lastLine}`
}
function runChipTooltip(r: Run): string {
  const label = workflowNameForRun(r) || UNNAMED
  const stageLine = r.stage_name ? `\nstage: ${r.stage_name}` : ''
  return `${label}${stageLine}\nactual run · ${r.status}\n${fmtLocal(r.started_at)}`
    + `${r.ended_at ? ' → ' + fmtLocal(r.ended_at).slice(11) : ''}`
    + `${r.status === 'running' ? '\nright-click to cancel' : ''}`
}
// ---- bucketing schedules / runs to cells ----
function fitsCell(cron: string | null, day: Date, hour: number): boolean {
  if (!cron) return false
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return false
  const [_m, h, _dom, _mon, dow] = parts
  function matchPart(p: string, v: number): boolean {
    if (p === '*') return true
    return p.split(',').some(tok => {
      if (tok.includes('-')) {
        const [a, b] = tok.split('-').map(Number)
        return v >= a && v <= b
      }
      if (tok.includes('/')) return v % Number(tok.split('/')[1]) === 0
      return Number(tok) === v
    })
  }
  // Bin-aware: a cron hits this cell if any hour in [hour, hour+binSize) matches.
  const range = Array.from({ length: binSize.value }, (_, i) => hour + i)
  const hourMatch = range.some(hv => matchPart(h, hv))
  if (!hourMatch) return false
  if (!matchPart(dow, day.getDay())) return false
  return true
}
function schedulesAt(day: Date, hour: number) {
  return props.schedules.filter(s => {
    if (!s.enabled) return false
    if (s.run_at) {
      const t = new Date(s.run_at)
      if (Number.isNaN(t.getTime())) return false
      const sameDay = t.toDateString() === day.toDateString()
      const inSlot = t.getHours() >= hour && t.getHours() < hour + binSize.value
      return sameDay && inSlot
    }
    return fitsCell(s.cron, day, hour)
  })
}
function runsAt(day: Date, hour: number) {
  return props.runs.filter(r => {
    if (!r.started_at) return false
    const t = new Date(r.started_at)
    if (Number.isNaN(t.getTime())) return false
    return t.toDateString() === day.toDateString()
        && t.getHours() >= hour && t.getHours() < hour + binSize.value
  })
}
function cellIsEmpty(day: Date, hour: number): boolean {
  return schedulesAt(day, hour).length === 0 && runsAt(day, hour).length === 0
}

// ---- overflow popover (Google-Calendar style "+N more") ----
// When a cell contains > MAX_INLINE_CHIPS items, we show the first
// MAX_INLINE_CHIPS-1 chips + a "+N more" button. Clicking it opens a
// popover positioned near the clicked cell showing every schedule/run
// in that hour as a clickable list. Guarantees the user can always
// reach each individual chip, regardless of cell density.
const MAX_INLINE_CHIPS = 3

interface PopoverState {
  day: Date
  hour: number
  x: number
  y: number
}
const popover = ref<PopoverState | null>(null)

function openOverflowPopover(day: Date, hour: number, ev: MouseEvent) {
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  const POPOVER_W = 340
  const POPOVER_H = 340
  const x = Math.min(rect.left + rect.width / 2 - POPOVER_W / 2, window.innerWidth - POPOVER_W - 10)
  const y = Math.min(rect.bottom + 4, window.innerHeight - POPOVER_H - 10)
  popover.value = {
    day,
    hour,
    x: Math.max(10, x),
    y: Math.max(10, y),
  }
}
function closePopover() { popover.value = null }
function popoverSchedules() {
  if (!popover.value) return []
  return schedulesAt(popover.value.day, popover.value.hour)
}
function popoverRuns() {
  if (!popover.value) return []
  return runsAt(popover.value.day, popover.value.hour)
}
function popoverHeader(): string {
  if (!popover.value) return ''
  const d = popover.value.day
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth()+1}/${d.getDate()} ${pad(popover.value.hour)}:00–${pad(popover.value.hour + binSize.value)}:00`
}

// ---- drag-and-drop ----
const dragId = ref<string | null>(null)
function onDragStart(s: any) { dragId.value = s.id }
function onDrop(day: Date, hour: number) {
  const id = dragId.value
  dragId.value = null
  if (!id) return
  emit('schedule-drop', id, day, hour)
}
</script>

<template>
  <div class="panel auto-calendar">
    <div class="cal-header">
      <h3 class="serif">This week</h3>
      <div class="cal-nav">
        <button class="cal-nav-btn" @click="prevWeek" title="Previous week">‹</button>
        <button class="cal-nav-btn cal-nav-today" :class="{ active: weekOffset === 0 }" @click="thisWeek">{{ weekLabel }}</button>
        <button class="cal-nav-btn" @click="nextWeek" title="Next week">›</button>
        <div class="cal-bin-group">
          <button
            v-for="opt in ([1,3,6] as const)"
            :key="opt"
            class="cal-bin-btn"
            :class="{ active: binSize === opt }"
            @click="binSize = opt"
            :title="`${opt}-hour bins`"
          >{{ opt }}h</button>
        </div>
        <button
          class="cal-bin-btn cal-range-btn"
          :class="{ active: show24h }"
          @click="toggle24h"
          :title="show24h ? 'Show 06:00–22:00 only' : 'Show full 24 hours'"
        >{{ show24h ? '24h' : '06–22' }}</button>
      </div>
      <div class="cal-legend" title="Chip legend">
        <span class="legend-item"><span class="legend-mark">●</span> once</span>
        <span class="legend-item"><span class="legend-mark">↻</span> recurring</span>
        <span class="legend-item"><span class="legend-mark">▶</span> actual run</span>
        <span class="legend-color legend-green" title="succeeded"></span>
        <span class="legend-color legend-red" title="failed"></span>
        <span class="legend-color legend-blue" title="running"></span>
      </div>
    </div>

    <div class="sch-grid">
      <div class="hdr"></div>
      <div v-for="d in days" :key="d.toISOString()" class="hdr">
        {{ d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }) }}
      </div>
      <template v-for="h in HOURS" :key="h">
        <div class="hdr hour">{{ String(h).padStart(2, '0') }}:00</div>
        <div
          v-for="d in days"
          :key="d.toISOString() + h"
          class="cell"
          :class="{ 'cell-empty': cellIsEmpty(d, h) }"
          @dragover.prevent
          @drop="onDrop(d, h)"
          @click="cellIsEmpty(d, h) && emit('cell-click', d, h)"
        >
          <template v-if="schedulesAt(d, h).length + runsAt(d, h).length <= MAX_INLINE_CHIPS">
            <span
              v-for="s in schedulesAt(d, h)"
              :key="'s-' + s.id"
              class="chip chip-schedule"
              :class="workflowColorClass(s.workflow_def_id)"
              draggable="true"
              @dragstart="onDragStart(s)"
              :title="chipTooltip(s)"
              @click.stop="emit('schedule-click', s)"
            >
              {{ (workflowNameForSchedule(s) || UNNAMED).slice(0, 12) }}{{ s.kind === 'once' ? ' ●' : ' ↻' }}
              <button
                class="chip-cancel"
                title="Cancel this schedule"
                @click.stop="emit('schedule-delete', s)"
              >✕</button>
            </span>
            <span
              v-for="r in runsAt(d, h)"
              :key="'r-' + r.id"
              class="chip chip-run"
              :class="`chip-run-${runStatusToTagClass(r.status)}`"
              :title="runChipTooltip(r)"
              @click.stop="emit('run-click', r)"
              @contextmenu.prevent.stop="emit('run-right-click', r)"
            >
              ▶ {{ runChipLabel(r) }}
            </span>
          </template>
          <template v-else>
            <!-- Overflow: show first MAX_INLINE_CHIPS-1 chips + `+N more` button -->
            <span
              v-for="s in schedulesAt(d, h).slice(0, MAX_INLINE_CHIPS - 1)"
              :key="'s-' + s.id"
              class="chip chip-schedule"
              :class="workflowColorClass(s.workflow_def_id)"
              draggable="true"
              @dragstart="onDragStart(s)"
              :title="chipTooltip(s)"
              @click.stop="emit('schedule-click', s)"
            >
              {{ (workflowNameForSchedule(s) || UNNAMED).slice(0, 12) }}{{ s.kind === 'once' ? ' ●' : ' ↻' }}
            </span>
            <span
              v-for="r in runsAt(d, h).slice(0, Math.max(0, MAX_INLINE_CHIPS - 1 - schedulesAt(d, h).length))"
              :key="'r-' + r.id"
              class="chip chip-run"
              :class="`chip-run-${runStatusToTagClass(r.status)}`"
              :title="runChipTooltip(r)"
              @click.stop="emit('run-click', r)"
              @contextmenu.prevent.stop="emit('run-right-click', r)"
            >
              ▶ {{ runChipLabel(r) }}
            </span>
            <button
              class="chip chip-more"
              :title="`Show all ${schedulesAt(d, h).length + runsAt(d, h).length} items in this hour`"
              @click.stop="openOverflowPopover(d, h, $event)"
            >
              +{{ schedulesAt(d, h).length + runsAt(d, h).length - (MAX_INLINE_CHIPS - 1) }} more
            </button>
          </template>
          <span v-if="cellIsEmpty(d, h)" class="cell-plus">+</span>
        </div>
      </template>
    </div>

    <!-- Overflow popover for cells with >MAX_INLINE_CHIPS items -->
    <div
      v-if="popover"
      class="cell-popover-backdrop"
      @click="closePopover"
    >
      <div
        class="cell-popover"
        :style="{ left: popover.x + 'px', top: popover.y + 'px' }"
        @click.stop
      >
        <div class="cp-header">
          <span class="cp-title">{{ popoverHeader() }}</span>
          <button class="cp-close" @click="closePopover">✕</button>
        </div>
        <div class="cp-body">
          <div v-if="popoverSchedules().length" class="cp-section">
            <div class="cp-section-label">Scheduled</div>
            <div
              v-for="s in popoverSchedules()"
              :key="'ps-' + s.id"
              class="cp-row"
              @click="emit('schedule-click', s); closePopover()"
            >
              <span
                class="pill"
                :class="workflowColorClass(s.workflow_def_id)"
              >{{ s.kind === 'once' ? '●' : '↻' }}</span>
              <span class="cp-row-name">{{ workflowNameForSchedule(s) || UNNAMED }}</span>
              <span class="cp-row-meta">
                {{ s.kind === 'once' ? fmtLocal(s.run_at) : s.cron }}
              </span>
            </div>
          </div>
          <div v-if="popoverRuns().length" class="cp-section">
            <div class="cp-section-label">Runs / Missions</div>
            <div
              v-for="r in popoverRuns()"
              :key="'pr-' + r.id"
              class="cp-row"
              @click="emit('run-click', r); closePopover()"
              @contextmenu.prevent="emit('run-right-click', r); closePopover()"
            >
              <span
                class="pill"
                :class="`pill-${runStatusToTagClass(r.status)}`"
              >{{ r.status }}</span>
              <span class="cp-row-name">{{ runChipLabel(r) }}</span>
              <span class="cp-row-meta">
                {{ fmtLocal(r.started_at) }}
              </span>
            </div>
          </div>
        </div>
        <div class="cp-footer">
          <span class="cp-muted">Click a row to open its detail. Right-click a run for context actions.</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auto-calendar {
  display: flex; flex-direction: column;
  overflow: hidden; min-width: 0;
  padding: 16px 20px;
}
.cal-header {
  display: flex; align-items: center; gap: 14px; margin-bottom: 12px;
}
.cal-header h3 { font-size: 18px; flex: 1; margin: 0; }

.cal-nav { display: flex; align-items: center; gap: 6px; }
.cal-nav-btn {
  padding: 4px 10px; font-size: 12px;
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  color: var(--ink); cursor: pointer; transition: all 120ms var(--ease-soft);
  min-width: 28px;
}
.cal-nav-btn:hover { border-color: var(--ink); }
.cal-nav-today { min-width: 90px; }
.cal-nav-today.active { background: var(--canvas); }
.cal-bin-group {
  display: inline-flex;
  margin-left: 6px;
  border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
}
.cal-bin-btn {
  padding: 4px 8px; font-size: 11px;
  background: var(--card); color: var(--ink-mute);
  border: none; border-right: 1px solid var(--border);
  cursor: pointer; font-family: 'Geist Mono', monospace;
}
.cal-bin-btn:last-child { border-right: none; }
.cal-bin-btn:hover { background: var(--canvas); }
.cal-bin-btn.active { background: var(--ink); color: var(--card); }

.cal-legend {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 10px;
  background: var(--canvas); border: 1px solid var(--border); border-radius: 6px;
  font-size: 11px; color: var(--ink-mute);
}
.legend-item { display: inline-flex; align-items: center; gap: 4px; }
.legend-mark { font-size: 12px; color: var(--ink); }
.legend-color {
  width: 10px; height: 10px; border-radius: 50%;
  display: inline-block;
}
.legend-green { background: var(--pastel-green-bg); border: 1px solid var(--pastel-green-fg); }
.legend-red   { background: var(--pastel-red-bg);   border: 1px solid var(--pastel-red-fg); }
.legend-blue  { background: var(--pastel-blue-bg);  border: 1px solid var(--pastel-blue-fg); }

.sch-grid {
  display: grid;
  grid-template-columns: 56px repeat(7, 1fr);
  gap: 0; margin-bottom: 12px;
  flex: 1; min-height: 0; overflow: auto;
}
.sch-grid > div { padding: 6px 8px; font-size: 12px; min-height: 36px; }
.sch-grid .hdr {
  color: var(--ink-mute); text-align: center; font-weight: 500;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
}
.sch-grid .hour { font-family: 'Geist Mono', monospace; text-transform: none; }
.sch-grid .cell {
  border-bottom: 1px solid var(--border);
  border-left: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 2px; align-items: stretch;
  position: relative;
  overflow: visible;
  min-width: 0;
}
.sch-grid .cell-empty { cursor: pointer; transition: background-color 120ms var(--ease-soft); }
.sch-grid .cell-empty:hover { background: var(--canvas); }
.sch-grid .cell-plus {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: var(--ink-faint); font-size: 14px; font-weight: 300;
  opacity: 0; pointer-events: none; transition: opacity 120ms;
}
.sch-grid .cell-empty:hover .cell-plus { opacity: 1; }
.sch-grid .chip {
  display: flex; align-items: center; gap: 3px;
  padding: 2px 8px; margin: 0;
  border-radius: 9999px;
  font-size: 11px; cursor: grab; user-select: none; white-space: nowrap;
  position: relative;
  min-height: 20px;
  overflow: hidden; text-overflow: ellipsis;
  z-index: 1;
  pointer-events: auto;
}
.sch-grid .chip:hover { z-index: 2; }

.sch-grid .chip-more {
  background: var(--ink-mute); color: var(--card);
  border: none; padding: 3px 10px;
  cursor: pointer; font-size: 10.5px; font-weight: 500;
  border-radius: 9999px;
  display: flex; align-items: center; justify-content: center;
}
.sch-grid .chip-more:hover { background: var(--ink); }

.cell-popover-backdrop {
  position: fixed; inset: 0;
  z-index: 100;
  background: transparent;
}
.cell-popover {
  position: fixed;
  width: 340px; max-height: 340px;
  background: var(--card); color: var(--ink);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.15);
  display: flex; flex-direction: column;
  overflow: hidden;
  z-index: 101;
}
.cp-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--canvas);
}
.cp-title {
  font-size: 12px; font-weight: 600; font-family: 'Geist Mono', monospace;
}
.cp-close {
  background: transparent; border: none; cursor: pointer;
  font-size: 14px; color: var(--ink-mute); padding: 2px 6px;
}
.cp-close:hover { color: var(--ink); }

.cp-body {
  flex: 1; overflow-y: auto;
  padding: 6px 4px;
}
.cp-section { padding: 4px 8px; }
.cp-section-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--ink-mute); margin-bottom: 4px; padding: 0 4px;
}
.cp-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  align-items: center;
  transition: background 100ms;
}
.cp-row:hover { background: var(--canvas); }
.cp-row-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cp-row-meta { font-size: 10.5px; color: var(--ink-mute); font-family: 'Geist Mono', monospace; white-space: nowrap; }

.cp-footer {
  padding: 6px 12px; border-top: 1px solid var(--border);
  font-size: 10px;
}
.cp-muted { color: var(--ink-faint); }

.pill {
  display: inline-flex; padding: 1px 6px;
  border-radius: 8px; font-size: 10px;
  align-items: center;
}
.pill-succeeded { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.pill-failed    { background: var(--pastel-red-bg);   color: var(--pastel-red-fg); }
.pill-running   { background: var(--pastel-blue-bg);  color: var(--pastel-blue-fg); }
.pill-idle      { background: var(--canvas); color: var(--ink-mute); }
.sch-grid .chip-cancel {
  background: transparent; border: none; cursor: pointer;
  font-size: 10px; padding: 0 2px; color: currentColor;
  opacity: 0; transition: opacity 120ms;
  line-height: 1;
}
.sch-grid .chip-schedule:hover .chip-cancel { opacity: 0.7; }
/* Later source order + equal specificity beats the parent-hover rule above. */
.sch-grid .chip-cancel:hover { opacity: 1; }
.sch-grid .chip.chip-a { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.sch-grid .chip.chip-b { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.sch-grid .chip.chip-c { background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg); }
.sch-grid .chip.chip-d { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.sch-grid .chip.dim { opacity: 0.35; }
.sch-grid .chip.active-task { box-shadow: 0 0 0 2px var(--ink); }
.sch-grid .chip:active { cursor: grabbing; }

/* .chip-run chips never receive .chip-a/b/c/d (those are schedule-only),
 * so equal specificity + later source order is enough — no !important. */
.sch-grid .chip-run {
  cursor: pointer; border: 1px dashed currentColor;
  background: transparent;
}
.sch-grid .chip-run-succeeded { color: var(--pastel-green-fg); background: var(--pastel-green-bg); border-style: solid; }
.sch-grid .chip-run-failed    { color: var(--pastel-red-fg);   background: var(--pastel-red-bg);   border-style: solid; }
.sch-grid .chip-run-running   { color: var(--pastel-blue-fg);  background: var(--pastel-blue-bg);  border-style: solid; }
.sch-grid .chip-run-idle      { color: var(--ink-mute); }

</style>
