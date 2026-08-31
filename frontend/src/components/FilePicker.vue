<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { ref, watch } from 'vue'
import { fsApi, type FsEntry } from '../api/fs'

const props = withDefaults(
  defineProps<{
    open: boolean
    mode?: 'dir' | 'file'         // 'dir' = pick a directory; 'file' = pick a file
    initialPath?: string           // path to open at first; falls back to most recent cwd or /data
    title?: string                 // modal header
    showRecent?: boolean           // show recent-cwd chips above list
  }>(),
  { mode: 'dir', initialPath: '', title: '', showRecent: true },
)

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'pick', path: string): void
}>()

const path = ref<string>('')
const parent = ref<string | null>(null)
const entries = ref<FsEntry[]>([])
const recent = ref<string[]>([])
const err = ref('')

async function navigate(to: string) {
  err.value = ''
  try {
    const r = await fsApi.browse(to)
    path.value = r.path
    parent.value = r.parent
    entries.value = r.entries
  } catch (e) {
    err.value = apiErrorMessage(e)
  }
}

async function loadRecent() {
  if (!props.showRecent) return
  try {
    const r = await fsApi.recentCwds(10)
    recent.value = r.items
  } catch {
    recent.value = []
  }
}

watch(
  () => props.open,
  async (now) => {
    if (!now) return
    await loadRecent()
    const start = props.initialPath || recent.value[0] || '/data'
    await navigate(start)
  },
  { immediate: true },
)

function onRowClick(e: FsEntry) {
  if (e.is_dir) {
    navigate(e.path)
  } else if (props.mode === 'file') {
    emit('pick', e.path)
  }
  // dir-mode + file row: no-op (just for context)
}

function useCurrent() {
  if (props.mode !== 'dir') return
  emit('pick', path.value)
}
</script>

<template>
  <div v-if="open" class="fp-backdrop" @click.self="emit('close')">
    <div class="fp-modal panel">
      <div class="fp-head">
        <strong v-if="title">{{ title }}</strong>
        <strong v-else>{{ mode === 'file' ? 'Pick a file' : 'Pick a directory' }}</strong>
        <button @click="emit('close')" class="fp-x" aria-label="close">×</button>
      </div>

      <div class="fp-nav">
        <button :disabled="!parent" @click="parent && navigate(parent)">⬆ ..</button>
        <span class="mono fp-path">{{ path || '…' }}</span>
        <button
          v-if="mode === 'dir'"
          class="primary"
          :disabled="!path"
          @click="useCurrent"
        >
          Use this dir
        </button>
      </div>

      <div v-if="showRecent && recent.length" class="fp-recent">
        <small>recent:</small>
        <span
          v-for="c in recent.slice(0, 6)"
          :key="c"
          class="fp-chip mono"
          @click="navigate(c)"
        >{{ c }}</span>
      </div>

      <p v-if="err" class="fp-err mono">{{ err }}</p>

      <div class="fp-list">
        <div
          v-for="e in entries"
          :key="e.path"
          class="fp-row"
          :class="{ dir: e.is_dir, file: !e.is_dir, pickable: e.is_dir || mode === 'file' }"
          @click="onRowClick(e)"
        >
          <span class="fp-icn">{{ e.is_dir ? '📁' : '📄' }}</span>
          <span class="fp-name mono">{{ e.name }}</span>
        </div>
        <div v-if="!entries.length && !err" class="fp-empty">(empty)</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fp-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 200;
}
.fp-modal {
  padding: 16px 18px;
  min-width: 44rem; max-width: 64rem;
  max-height: 82vh;
  display: flex; flex-direction: column; gap: 10px;
}
.fp-head { display: flex; align-items: center; gap: 8px; }
.fp-head strong { flex: 1; }
.fp-x { background: none; border: none; font-size: 18px; cursor: pointer; color: var(--ink-mute); }
.fp-nav { display: flex; align-items: center; gap: 8px; }
.fp-path {
  flex: 1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 4px 8px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 12px;
}
.fp-recent { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.fp-recent small { color: var(--ink-mute); font-size: 11px; }
.fp-chip {
  display: inline-block;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  cursor: pointer;
  font-size: 11px;
}
.fp-chip:hover { background: var(--pastel-blue-bg); }
.fp-err { color: var(--pastel-red-fg); font-size: 12px; margin: 0; }
.fp-list {
  overflow: auto;
  max-height: 60vh;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.fp-row {
  padding: 5px 10px;
  display: flex; gap: 10px; align-items: center;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}
.fp-row:last-child { border-bottom: none; }
.fp-row.pickable { cursor: pointer; }
.fp-row.pickable:hover { background: var(--canvas); }
.fp-row.file:not(.pickable) { opacity: 0.55; }
.fp-icn { width: 1.2em; }
.fp-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fp-empty { padding: 18px; text-align: center; color: var(--ink-mute); font-size: 12px; }
</style>
