<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { sessionsApi } from '../api/sessions'

const props = defineProps<{ sid: string }>()

const mountRef = ref<HTMLDivElement | null>(null)
const loading = ref(true)
const error = ref('')
const source = ref('')
const empty = ref(false)

let term: Terminal | null = null
let fit: FitAddon | null = null
let observer: ResizeObserver | null = null
let loadToken = 0

function ensureTerminal() {
  if (term || !mountRef.value) return
  term = new Terminal({
    cols: 100,
    rows: 30,
    fontFamily: '"Geist Mono", "SF Mono", Menlo, monospace',
    fontSize: 13,
    lineHeight: 1.2,
    scrollback: 10000,
    disableStdin: true,
    cursorBlink: false,
    theme: {
      background: '#0b0b0b',
      foreground: '#DCDCDC',
      cursor: '#0b0b0b',
    },
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(mountRef.value)
  observer = new ResizeObserver(() => {
    try { fit?.fit() } catch (_) { /* transient layout */ }
  })
  observer.observe(mountRef.value)
}

async function load() {
  const token = ++loadToken
  loading.value = true
  error.value = ''
  empty.value = false
  await nextTick()
  ensureTerminal()
  try {
    const result = await sessionsApi.output(props.sid)
    if (token !== loadToken) return
    source.value = result.source
    empty.value = result.data.byteLength === 0
    term?.reset()
    if (result.data.byteLength) {
      term?.write(result.data, () => {
        try { fit?.fit() } catch (_) { /* ignore */ }
        term?.scrollToBottom()
      })
    }
  } catch (e) {
    if (token !== loadToken) return
    error.value = apiErrorMessage(e)
  } finally {
    if (token === loadToken) loading.value = false
  }
}

watch(() => props.sid, load)
onMounted(load)
onUnmounted(() => {
  loadToken += 1
  observer?.disconnect()
  observer = null
  term?.dispose()
  term = null
  fit = null
})
</script>

<template>
  <section class="output-viewer">
    <header>
      <div>
        <strong>Last terminal output</strong>
        <span v-if="source && source !== 'missing'" class="source">{{ source }}</span>
      </div>
      <button type="button" :disabled="loading" @click="load">
        {{ loading ? 'Loading…' : 'Reload' }}
      </button>
    </header>
    <div class="output-body">
      <div ref="mountRef" class="output-mount"></div>
      <div v-if="loading" class="output-state">Loading saved output…</div>
      <div v-else-if="error" class="output-state error">
        Could not load terminal output: {{ error }}
      </div>
      <div v-else-if="empty" class="output-state">
        No terminal snapshot was captured for this session.
      </div>
    </div>
  </section>
</template>

<style scoped>
.output-viewer {
  display: flex;
  flex-direction: column;
  min-height: 300px;
  flex: 1 1 auto;
  margin: 0 16px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: #0b0b0b;
}
.output-viewer > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  background: var(--card);
  color: var(--ink);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.source {
  margin-left: 8px;
  color: var(--ink-faint);
  font-family: 'Geist Mono', monospace;
  font-size: 10px;
}
.output-viewer button { font-size: 11px; padding: 3px 8px; }
.output-body { position: relative; flex: 1; min-height: 260px; }
.output-mount { position: absolute; inset: 0; padding: 8px; }
.output-state {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  padding: 24px;
  background: #0b0b0b;
  color: #aaa;
  font: 12px 'Geist Mono', monospace;
  text-align: center;
}
.output-state.error { color: #ff9b95; }
</style>
