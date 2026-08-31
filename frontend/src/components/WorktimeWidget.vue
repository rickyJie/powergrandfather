<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { worktimeApi, type WorktimeLive } from '../api/worktime'

/**
 * Header top-right widget.
 *
 * Polls `/api/worktime/live` every 5s for authoritative totals; between
 * polls, a 1s local ticker advances the displayed value when the server
 * reports an open interval. Backend already applies wall-clock accumulation
 * (choice 3=a) and per-kind safety caps — this component just formats.
 *
 * Layout: two pill capsules side-by-side + a live-tick suffix. The `Today`
 * capsule (pastel-blue tint) shows today's UTC-bucketed totals; the `All`
 * capsule (neutral canvas) shows all-time accumulation. Each capsule
 * contains a 👤 (human) and ✦ (agent) icon-pair with a bold number.
 * The trailing `● MM:SS` (or `HH:MM:SS`) only renders while at least
 * one agent interval is open — its green pulse is the "alive" signal.
 * Narrow screens (<640px) wrap the capsules onto two lines.
 */

const POLL_INTERVAL_MS = 5_000
const TICK_INTERVAL_MS = 1_000

const live = ref<WorktimeLive | null>(null)
const localNowMs = ref<number>(Date.now())
const lastPollMs = ref<number>(Date.now())
const lastError = ref<string | null>(null)

let pollTimer: number | undefined
let tickTimer: number | undefined

function secondsSincePoll(): number {
  return Math.max(0, Math.floor((localNowMs.value - lastPollMs.value) / 1000))
}

// today_* / all_* already include open contributions AS OF the last poll,
// so we just add the seconds elapsed since — but only when an interval is
// open. Same drift applies to both today and all-time totals.
const todayHumanSec = computed<number>(() => {
  const l = live.value
  if (!l) return 0
  const drift = l.open_human_sec > 0 ? secondsSincePoll() : 0
  return l.today_human_sec + drift
})

const todayAgentSec = computed<number>(() => {
  const l = live.value
  if (!l) return 0
  const drift = l.open_agent_count > 0 ? secondsSincePoll() : 0
  return l.today_agent_sec + drift
})

const allHumanSec = computed<number>(() => {
  const l = live.value
  if (!l) return 0
  const drift = l.open_human_sec > 0 ? secondsSincePoll() : 0
  return l.all_human_sec + drift
})

const allAgentSec = computed<number>(() => {
  const l = live.value
  if (!l) return 0
  const drift = l.open_agent_count > 0 ? secondsSincePoll() : 0
  return l.all_agent_sec + drift
})

const openAgentSec = computed<number>(() => {
  const l = live.value
  if (!l || l.open_agent_count === 0) return 0
  return l.open_agent_sec + secondsSincePoll()
})

function fmtCumulative(sec: number): string {
  if (sec < 60) return `${sec}s`
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h <= 0) return `${m}m`
  return `${h}h ${m}m`
}

function fmtTicker(sec: number): string {
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  const pad = (n: number) => n.toString().padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

async function poll(): Promise<void> {
  // Don't poll while the tab is backgrounded. The widget is invisible anyway,
  // and over an SSH tunnel this /live GET (every ~4s) is the single biggest
  // consumer of the browser's 6-connection-per-origin pool — a hidden tab that
  // keeps polling starves foreground session-connect requests (perf.log showed
  // 88 /live timeouts, many with hidden:true). Resume + poll immediately when
  // the tab is shown again so the numbers refresh without waiting a full tick.
  if (document.hidden) return
  try {
    live.value = await worktimeApi.live()
    lastPollMs.value = Date.now()
    lastError.value = null
  } catch (e) {
    lastError.value = e instanceof Error ? e.message : String(e)
  }
}

function onVisibilityChange(): void {
  if (!document.hidden) void poll()
}

onMounted(() => {
  void poll()
  pollTimer = window.setInterval(poll, POLL_INTERVAL_MS)
  tickTimer = window.setInterval(() => {
    localNowMs.value = Date.now()
  }, TICK_INTERVAL_MS)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
  if (tickTimer !== undefined) window.clearInterval(tickTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <div
    class="worktime"
    :class="{ 'is-live': (live?.open_agent_count ?? 0) > 0 }"
    :title="lastError ?? `Today = the UTC day (${live?.day_bucket_utc ?? '—'})  ·  All = every work_interval ever recorded`"
  >
    <span class="capsule today">
      <span class="tag">Today</span>
      <span class="metric" :title="`human work today: ${fmtCumulative(todayHumanSec)}`">
        <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><circle cx="6" cy="3.6" r="1.9"/><path d="M2 11c0-2.4 1.9-3.9 4-3.9s4 1.5 4 3.9z"/></svg>
        <b>{{ fmtCumulative(todayHumanSec) }}</b>
      </span>
      <span class="metric" :title="`agent work today: ${fmtCumulative(todayAgentSec)}`">
        <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><path d="M6 0.5 L6.9 5.1 L11.5 6 L6.9 6.9 L6 11.5 L5.1 6.9 L0.5 6 L5.1 5.1 Z"/></svg>
        <b>{{ fmtCumulative(todayAgentSec) }}</b>
      </span>
    </span>
    <span class="capsule all">
      <span class="tag">All</span>
      <span class="metric" :title="`human work, all time: ${fmtCumulative(allHumanSec)}`">
        <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><circle cx="6" cy="3.6" r="1.9"/><path d="M2 11c0-2.4 1.9-3.9 4-3.9s4 1.5 4 3.9z"/></svg>
        <b>{{ fmtCumulative(allHumanSec) }}</b>
      </span>
      <span class="metric" :title="`agent work, all time: ${fmtCumulative(allAgentSec)}`">
        <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><path d="M6 0.5 L6.9 5.1 L11.5 6 L6.9 6.9 L6 11.5 L5.1 6.9 L0.5 6 L5.1 5.1 Z"/></svg>
        <b>{{ fmtCumulative(allAgentSec) }}</b>
      </span>
    </span>
    <span v-if="openAgentSec > 0" class="live-tick" :title="`${live?.open_agent_count} open agent interval(s)`">
      <span class="dot">●</span>{{ fmtTicker(openAgentSec) }}
    </span>
  </div>
</template>

<style scoped>
.worktime {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
  color: var(--ink-mute);
}
.worktime .capsule {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 9px;
  border-radius: 999px;
  background: transparent;
  border: 1px solid var(--border);
}
.worktime .capsule.today .tag,
.worktime .capsule.all .tag {
  color: var(--ink-faint);
}
.worktime .tag {
  font-weight: 600;
  font-size: 10px;
  letter-spacing: 0.02em;
  opacity: 0.7;
}
.worktime .metric {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--ink-2);
}
.worktime .metric svg {
  fill: currentColor;
  color: var(--ink-faint);
  opacity: 0.65;
  flex-shrink: 0;
}
.worktime .metric b {
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.worktime .live-tick {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 2px;
  color: var(--ink-mute);
  font-variant-numeric: tabular-nums;
}
.worktime .dot {
  color: var(--pastel-green-fg);
  opacity: 0.55;
  animation: worktime-pulse 3s ease-in-out infinite;
}
@keyframes worktime-pulse {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 0.25; }
}
@media (max-width: 640px) {
  .worktime { flex-wrap: wrap; row-gap: 3px; }
}
</style>
