<script setup lang="ts">
/**
 * ScheduleAlarmPicker — alarm-clock style schedule creator.
 *
 * User picks a mode (Once / Daily / Weekly / Monthly), fills in the
 * relevant time fields, and gets back a `{cron?: string, run_at?: string}`
 * payload that matches the schedule API's shape.
 *
 * Time semantics (verified against backend/csm/modules/automation/scheduler.py):
 * - `cron` is interpreted by APScheduler in the backend's SYSTEM LOCAL TZ.
 *   User's local time here is assumed to match that TZ (usually CST for
 *   this deployment). We emit hour/minute directly.
 * - `run_at` is stored as UTC. We convert the user's local pick to
 *   ISO 8601 UTC before submitting.
 */
import { computed, ref, watch } from 'vue'

type Mode = 'once' | 'daily' | 'weekly' | 'monthly'

const props = defineProps<{
  open: boolean
  title?: string
  /** Optional pre-fill: seed date/time from calendar cell click (local time). */
  initialDate?: Date | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'submit', payload: { cron?: string; run_at?: string; label: string }): void
}>()

const mode = ref<Mode>('once')

// Shared time fields — 0-23 hour, 0-59 minute (steps of 5 in UI).
const hour = ref(9)
const minute = ref(0)

// Once
const onceDate = ref('') // YYYY-MM-DD

// Weekly — 0=Sun ... 6=Sat, matches cron day_of_week convention.
const weekdays = ref<Set<number>>(new Set())

// Monthly — 1..28 only (avoid month-end ambiguity).
const dayOfMonth = ref(1)

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function toIsoDate(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

watch(
  () => [props.open, props.initialDate] as const,
  ([isOpen, seed]) => {
    if (!isOpen) return
    // Sensible default: pick "Once" if user came from calendar cell click,
    // else "Daily" (most common recurring schedule).
    const now = new Date()
    if (seed instanceof Date) {
      mode.value = 'once'
      onceDate.value = toIsoDate(seed)
      hour.value = seed.getHours()
      minute.value = Math.round(seed.getMinutes() / 5) * 5
    } else {
      mode.value = 'daily'
      onceDate.value = toIsoDate(now)
      hour.value = 9
      minute.value = 0
    }
    weekdays.value = new Set()
    dayOfMonth.value = 1
  },
  { immediate: true },
)

const WEEKDAY_LABEL = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
// cron.dow: 0=Sun matches JS getDay(). For weekly we sort ascending, but
// UI reads more naturally starting Mon → Sun for the user.
const WEEK_ORDER = [1, 2, 3, 4, 5, 6, 0]

function toggleWeekday(d: number) {
  const next = new Set(weekdays.value)
  next.has(d) ? next.delete(d) : next.add(d)
  weekdays.value = next
}

// ---------- Preview: human-readable + next-run computation ----------

const humanReadable = computed(() => {
  const hh = pad2(hour.value)
  const mm = pad2(minute.value)
  if (mode.value === 'once') {
    if (!onceDate.value) return '(pick a date)'
    return `once, ${onceDate.value} at ${hh}:${mm}`
  }
  if (mode.value === 'daily') {
    return `daily at ${hh}:${mm}`
  }
  if (mode.value === 'weekly') {
    if (weekdays.value.size === 0) return '(pick one or more weekdays)'
    const labels = WEEK_ORDER
      .filter(d => weekdays.value.has(d))
      .map(d => WEEKDAY_LABEL[d])
      .join(', ')
    return `${labels} at ${hh}:${mm}`
  }
  if (mode.value === 'monthly') {
    return `monthly on day ${dayOfMonth.value} at ${hh}:${mm}`
  }
  return ''
})

function nextRunLocal(): Date | null {
  const now = new Date()
  if (mode.value === 'once') {
    if (!onceDate.value) return null
    const [y, m, d] = onceDate.value.split('-').map(n => parseInt(n, 10))
    if (Number.isNaN(y) || Number.isNaN(m) || Number.isNaN(d)) return null
    return new Date(y, m - 1, d, hour.value, minute.value, 0, 0)
  }
  if (mode.value === 'daily') {
    const cand = new Date(now)
    cand.setHours(hour.value, minute.value, 0, 0)
    if (cand <= now) cand.setDate(cand.getDate() + 1)
    return cand
  }
  if (mode.value === 'weekly') {
    if (weekdays.value.size === 0) return null
    for (let i = 0; i < 14; i++) {
      const cand = new Date(now)
      cand.setDate(cand.getDate() + i)
      cand.setHours(hour.value, minute.value, 0, 0)
      if (weekdays.value.has(cand.getDay()) && cand > now) return cand
    }
    return null
  }
  if (mode.value === 'monthly') {
    for (let i = 0; i < 12; i++) {
      const cand = new Date(now.getFullYear(), now.getMonth() + i, dayOfMonth.value,
        hour.value, minute.value, 0, 0)
      if (cand > now) return cand
    }
    return null
  }
  return null
}

const nextRunPreview = computed(() => {
  const n = nextRunLocal()
  if (!n) return '—'
  const wd = WEEKDAY_LABEL[n.getDay()]
  return `${n.getFullYear()}-${pad2(n.getMonth() + 1)}-${pad2(n.getDate())} (${wd}) ${pad2(n.getHours())}:${pad2(n.getMinutes())}`
})

const canSubmit = computed(() => {
  if (mode.value === 'once') {
    const n = nextRunLocal()
    return n !== null && n > new Date()
  }
  if (mode.value === 'weekly') return weekdays.value.size > 0
  return true
})

// ---------- Emit payload ----------

function buildPayload(): { cron?: string; run_at?: string; label: string } {
  const hh = pad2(hour.value)
  const mm = pad2(minute.value)
  if (mode.value === 'once') {
    const local = nextRunLocal()!
    // Convert local to UTC ISO. toISOString gives UTC by default.
    return {
      run_at: local.toISOString(),
      label: humanReadable.value,
    }
  }
  if (mode.value === 'daily') {
    return { cron: `${minute.value} ${hour.value} * * *`, label: humanReadable.value }
  }
  if (mode.value === 'weekly') {
    const dow = Array.from(weekdays.value).sort((a, b) => a - b).join(',')
    return { cron: `${minute.value} ${hour.value} * * ${dow}`, label: humanReadable.value }
  }
  // monthly
  return { cron: `${minute.value} ${hour.value} ${dayOfMonth.value} * *`, label: humanReadable.value }
}

function onSubmit() {
  if (!canSubmit.value) return
  emit('submit', buildPayload())
  emit('close')
}

// Convenience arrays for select boxes
const hours = Array.from({ length: 24 }, (_, i) => i)
const minutes = Array.from({ length: 12 }, (_, i) => i * 5)
const dom = Array.from({ length: 28 }, (_, i) => i + 1)
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal panel sap-modal" role="dialog" aria-modal="true" aria-label="Schedule alarm">
      <div class="sap-header">
        <div>
          <div class="sap-eyebrow">Schedule</div>
          <h3 class="serif sap-title">{{ title || 'Set a schedule' }}</h3>
        </div>
        <button class="sap-close" @click="emit('close')" aria-label="Close">×</button>
      </div>

      <div class="sap-body">
        <!-- Mode tabs -->
        <div class="sap-tabs">
          <button :class="{ active: mode === 'once' }" @click="mode = 'once'">Once</button>
          <button :class="{ active: mode === 'daily' }" @click="mode = 'daily'">Daily</button>
          <button :class="{ active: mode === 'weekly' }" @click="mode = 'weekly'">Weekly</button>
          <button :class="{ active: mode === 'monthly' }" @click="mode = 'monthly'">Monthly</button>
        </div>

        <!-- Once -->
        <div v-if="mode === 'once'" class="sap-field">
          <span class="sap-label">Date</span>
          <input type="date" v-model="onceDate" />
        </div>

        <!-- Weekly picker -->
        <div v-else-if="mode === 'weekly'" class="sap-field">
          <span class="sap-label">Days of the week (pick any)</span>
          <div class="sap-weekdays">
            <button
              v-for="d in WEEK_ORDER"
              :key="d"
              :class="{ active: weekdays.has(d) }"
              @click="toggleWeekday(d)"
            >{{ WEEKDAY_LABEL[d] }}</button>
          </div>
        </div>

        <!-- Monthly picker -->
        <div v-else-if="mode === 'monthly'" class="sap-field">
          <span class="sap-label">Day of month (1-28)</span>
          <select v-model.number="dayOfMonth" class="sap-day">
            <option v-for="d in dom" :key="d" :value="d">{{ d }}</option>
          </select>
          <div class="sap-hint">Only 1-28, so there's no ambiguity in months that don't have a 29th, 30th or 31st.</div>
        </div>

        <!-- Time — common to all modes -->
        <div class="sap-field">
          <span class="sap-label">Time</span>
          <div class="sap-time-row">
            <select v-model.number="hour" class="sap-time-select">
              <option v-for="h in hours" :key="h" :value="h">{{ pad2(h) }}</option>
            </select>
            <span class="sap-time-sep">:</span>
            <select v-model.number="minute" class="sap-time-select">
              <option v-for="m in minutes" :key="m" :value="m">{{ pad2(m) }}</option>
            </select>
            <span class="sap-hint sap-hint-inline">interpreted in this machine's timezone</span>
          </div>
        </div>

        <!-- Preview -->
        <div class="sap-preview">
          <div class="sap-preview-row">
            <span class="sap-preview-label">Rule</span>
            <span class="sap-preview-val">{{ humanReadable }}</span>
          </div>
          <div class="sap-preview-row">
            <span class="sap-preview-label">Next run</span>
            <span class="sap-preview-val sap-preview-mono">{{ nextRunPreview }}</span>
          </div>
        </div>

        <div class="sap-actions">
          <button class="primary" :disabled="!canSubmit" @click="onSubmit">
            Confirm
          </button>
          <button @click="emit('close')">Cancel</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sap-modal { max-width: 520px; width: 92%; padding: 0; overflow: hidden; }
.sap-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--border);
}
.sap-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute); margin-bottom: 3px;
}
.sap-title { margin: 0; font-size: 17px; color: var(--ink); }
.sap-close {
  background: transparent; border: none; font-size: 22px; padding: 0 6px;
  color: var(--ink-mute); cursor: pointer; box-shadow: none;
}
.sap-close:hover { color: var(--ink); transform: none; }

.sap-body { padding: 14px 18px 18px; font-size: 13px; }

.sap-tabs {
  display: inline-flex; gap: 4px; margin-bottom: 14px;
  padding: 3px; border-radius: 6px;
  background: var(--canvas);
  border: 1px solid var(--border);
}
.sap-tabs button {
  padding: 4px 12px; font-size: 12.5px;
  background: transparent; border: none; box-shadow: none;
  color: var(--ink-mute);
}
.sap-tabs button:hover { transform: none; box-shadow: none; }
.sap-tabs button.active {
  background: var(--ink); color: var(--card); font-weight: 500;
}

.sap-field { margin-bottom: 12px; }
.sap-label {
  display: block; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--ink-mute);
  margin-bottom: 5px; font-weight: 600;
}
.sap-hint {
  display: block; font-size: 11.5px; color: var(--ink-mute); margin-top: 4px;
}
.sap-hint-inline { display: inline; margin-left: 10px; margin-top: 0; }

.sap-weekdays { display: flex; gap: 5px; flex-wrap: wrap; }
.sap-weekdays button { padding: 4px 11px; font-size: 12.5px; }
.sap-weekdays button.active {
  background: var(--ink); color: var(--card); border-color: var(--ink);
}

.sap-day { width: 80px; }

.sap-time-row { display: flex; align-items: center; gap: 4px; }
.sap-time-select { min-width: 62px; }
.sap-time-sep { font-size: 15px; font-weight: 600; color: var(--ink-mute); }

.sap-preview {
  margin-top: 12px; padding: 10px 12px;
  background: var(--pastel-blue-bg);
  border-left: 3px solid var(--pastel-blue-fg);
  border-radius: 4px;
}
.sap-preview-row {
  display: flex; gap: 10px; align-items: baseline; padding: 2px 0;
}
.sap-preview-label {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--pastel-blue-fg); min-width: 90px; opacity: 0.8;
}
.sap-preview-val { font-size: 13px; font-weight: 500; color: var(--pastel-blue-fg); }
.sap-preview-mono { font-family: 'Geist Mono', 'SF Mono', monospace; font-size: 12.5px; }

.sap-actions { display: flex; gap: 8px; margin-top: 14px; }
</style>
