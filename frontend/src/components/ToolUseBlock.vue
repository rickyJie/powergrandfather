<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  tool: string
  input: any
  result: { ok: boolean; preview: string } | null
  ts: string
}>()

const expanded = ref(true)
const showFull = ref(false)

// Auto-collapse once a result arrives (per design: open while running,
// collapsed once done).
watch(
  () => props.result,
  (val) => {
    if (val !== null) expanded.value = false
  },
)

const statusColor = computed(() => {
  if (!props.result) return 'pending'
  return props.result.ok ? 'ok' : 'fail'
})

const inputPretty = computed(() => {
  try {
    return JSON.stringify(props.input ?? {}, null, 2)
  } catch {
    return String(props.input)
  }
})

const RESULT_TRUNCATE = 2000
const resultDisplay = computed(() => {
  if (!props.result) return ''
  const full = props.result.preview
  if (showFull.value || full.length <= RESULT_TRUNCATE) return full
  return full.slice(0, RESULT_TRUNCATE)
})
const resultTruncated = computed(() =>
  !!props.result && props.result.preview.length > RESULT_TRUNCATE,
)

// One-line summary of the tool input, shown in the collapsed header so users
// can see what each Bash/Read/Edit/etc is about without expanding.
const summary = computed(() => {
  const t = (props.tool || '').toLowerCase()
  const i = props.input || {}
  const pick = (v: unknown, max = 100): string => {
    if (v === undefined || v === null) return ''
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return s.length > max ? s.slice(0, max) + '…' : s
  }
  switch (t) {
    case 'bash': return pick(i.command, 120)
    case 'read': return pick(i.file_path || i.path)
    case 'write': return pick(i.file_path || i.path)
    case 'edit':
    case 'multiedit': return pick(i.file_path || i.path)
    case 'glob': return pick(i.pattern)
    case 'grep': return `${pick(i.pattern, 50)}${i.path ? ' · ' + pick(i.path, 60) : ''}`
    case 'webfetch': return pick(i.url)
    case 'websearch': return pick(i.query, 80)
    case 'task': return pick(i.description || i.subagent_type)
    case 'skill': return pick(i.skill)
  }
  // Generic fallback: first scalar value.
  for (const k of Object.keys(i)) {
    const v = i[k]
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      return `${k}=${pick(v)}`
    }
  }
  return ''
})
</script>

<template>
  <div class="tool-block" :class="statusColor">
    <button
      class="head"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span class="caret" :class="{ open: expanded }">▸</span>
      <span class="tool-name mono">{{ tool }}</span>
      <span v-if="summary" class="summary mono">{{ summary }}</span>
      <span v-if="!result" class="badge pending">
        <span class="spin">⟳</span> running
      </span>
      <span v-else-if="result.ok" class="badge ok">done</span>
      <span v-else class="badge fail">error</span>
    </button>
    <div v-if="expanded" class="body">
      <div class="section">
        <div class="label">input</div>
        <pre class="mono">{{ inputPretty }}</pre>
      </div>
      <div v-if="result" class="section">
        <div class="label">{{ result.ok ? 'result' : 'error' }}</div>
        <pre class="mono">{{ resultDisplay }}</pre>
        <button
          v-if="resultTruncated"
          class="show-full"
          @click="showFull = !showFull"
        >{{ showFull ? 'Collapse' : `Show all (+${result!.preview.length - RESULT_TRUNCATE} chars)` }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-block {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  font-size: 12px;
  margin: 6px 0;
}
.tool-block.pending { border-color: var(--pastel-blue-fg); }
.tool-block.fail { border-color: var(--pastel-red-fg); }
.head {
  display: flex; align-items: center; gap: 8px;
  width: 100%;
  padding: 6px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  color: var(--ink);
}
.head:hover { background: var(--canvas); }
.caret { display: inline-block; transition: transform 150ms; color: var(--ink-mute); }
.caret.open { transform: rotate(90deg); }
.tool-name { font-weight: 500; flex-shrink: 0; }
.summary {
  flex: 1; min-width: 0;
  font-size: 11px;
  color: var(--ink-mute);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding-left: 6px;
}
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  flex-shrink: 0;
}
.spin {
  display: inline-block;
  animation: spin 1.4s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.show-full {
  margin-top: 6px;
  font-size: 10px;
  padding: 2px 8px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
  color: var(--ink-mute);
}
.show-full:hover { background: var(--card); color: var(--ink); }
.badge.pending { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.badge.ok { background: var(--pastel-green-bg, #e2f0e3); color: var(--pastel-green-fg, #346538); }
.badge.fail { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.body { padding: 6px 10px 10px; }
.section { margin-top: 4px; }
.section .label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--ink-mute); margin-bottom: 2px;
}
.section pre {
  background: var(--canvas);
  border-radius: 4px;
  padding: 6px 8px;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
</style>
