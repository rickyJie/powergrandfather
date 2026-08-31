<script setup lang="ts">
/**
 * RunsDrawer — cross-task Run history, opened from the toolbar.
 *
 * The TaskList / WeekCalendar both show *per-task* history (last 5 runs);
 * this is the global view, with status + task filters.
 */
import { computed, ref, watch } from 'vue'
import { automationApi } from '../../api/automation'

const props = defineProps<{
  open: boolean
  tasks: any[]
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-run', r: any): void
}>()

const runs = ref<any[]>([])
const loading = ref(false)
const filterStatus = ref<'all' | 'running' | 'succeeded' | 'failed' | 'needs_review'>('all')
const filterTaskId = ref<string>('')  // '' = all tasks

watch(() => props.open, async (isOpen) => {
  if (!isOpen) return
  loading.value = true
  try {
    const data = await automationApi.listRuns({ limit: 200 })
    runs.value = data.items
  } finally { loading.value = false }
})

const visible = computed(() => {
  return runs.value.filter(r => {
    if (filterStatus.value !== 'all' && r.status !== filterStatus.value) return false
    if (filterTaskId.value && r.task_def_id !== filterTaskId.value) return false
    return true
  })
})

function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
function taskName(taskId: string | null): string {
  if (!taskId) return '(deleted task)'
  return props.tasks.find(t => t.id === taskId)?.name || taskId.slice(0, 8)
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal rd-modal panel" role="dialog" aria-modal="true" aria-label="All runs">
      <div class="rd-header">
        <h3 class="serif">All runs</h3>
        <button class="rd-close" @click="emit('close')">×</button>
      </div>

      <div class="rd-filters">
        <select v-model="filterStatus" class="rd-select">
          <option value="all">all statuses</option>
          <option value="running">running</option>
          <option value="succeeded">succeeded</option>
          <option value="failed">failed</option>
          <option value="needs_review">needs review</option>
        </select>
        <select v-model="filterTaskId" class="rd-select">
          <option value="">all tasks</option>
          <option v-for="t in tasks" :key="t.id" :value="t.id">{{ t.name }}</option>
        </select>
        <span class="rd-count">{{ visible.length }} / {{ runs.length }}</span>
      </div>

      <div v-if="loading" class="rd-empty">Loading…</div>
      <div v-else-if="!visible.length" class="rd-empty">No runs match.</div>
      <div v-else class="rd-list">
        <div
          v-for="r in visible"
          :key="r.id"
          class="rd-row"
          @click="emit('open-run', r)"
        >
          <span class="tag" :class="r.status">{{ r.status }}</span>
          <span class="rd-task">{{ taskName(r.task_def_id) }}</span>
          <span class="mono rd-time">{{ fmtLocal(r.started_at) }}</span>
          <span class="mono rd-time" v-if="r.ended_at">→ {{ fmtLocal(r.ended_at).slice(11) }}</span>
          <span v-if="r.exit_code !== null && r.exit_code !== undefined" class="mono rd-exit">exit {{ r.exit_code }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex; align-items: center; justify-content: center; z-index: 99;
}
.rd-modal {
  min-width: 44rem; max-width: 56rem;
  padding: 20px 24px 16px;
  display: flex; flex-direction: column; gap: 12px;
  max-height: 80vh;
}
.rd-header {
  display: flex; justify-content: space-between; align-items: center;
}
.rd-header h3 { margin: 0; font-size: 20px; }
.rd-close {
  background: transparent; border: none; cursor: pointer;
  font-size: 22px; color: var(--ink-mute); line-height: 1;
}
.rd-close:hover { color: var(--ink); }
.rd-filters {
  display: flex; gap: 10px; align-items: center;
  padding: 8px 0; border-bottom: 1px solid var(--border);
}
.rd-select {
  padding: 4px 8px; font-size: 13px;
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  color: var(--ink);
}
.rd-count {
  margin-left: auto; font-size: 11.5px;
  color: var(--ink-faint); font-family: 'Geist Mono', monospace;
}
.rd-empty {
  padding: 36px; text-align: center; color: var(--ink-faint); font-style: italic;
}
.rd-list { overflow-y: auto; flex: 1; min-height: 200px; }
.rd-row {
  display: grid;
  grid-template-columns: 90px 1fr 150px 80px 80px;
  align-items: center; gap: 12px;
  padding: 8px 6px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  cursor: pointer;
  transition: background 120ms;
}
.rd-row:hover { background: var(--canvas); }
.rd-task { color: var(--ink); font-family: 'Newsreader', serif; }
.rd-time, .rd-exit { color: var(--ink-mute); font-family: 'Geist Mono', monospace; font-size: 11.5px; }
</style>
