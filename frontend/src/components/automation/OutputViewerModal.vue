<script setup lang="ts">
/**
 * OutputViewerModal — full-file viewer for a Run's Output.
 *
 * Calls /api/runs/{runId}/outputs/{outputId}/raw which serves up to 5MB
 * of the actual file (vs the 4KB preview in the table).
 */
import { apiErrorMessage } from '../../lib/apiError'
import { ref, watch } from 'vue'
import { automationApi } from '../../api/automation'
import { useToast } from '../../composables/useToast'

const props = defineProps<{
  open: boolean
  runId: string | null
  output: any | null  // {id, path, type, preview, discovered_at}
}>()
const emit = defineEmits<{ (e: 'close'): void }>()
const toast = useToast()

const raw = ref<string | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

watch(() => [props.open, props.runId, props.output?.id] as const, async ([isOpen, runId, outId]) => {
  if (!isOpen || !runId || !outId) { raw.value = null; error.value = null; return }
  loading.value = true
  error.value = null
  try {
    raw.value = await automationApi.getOutputRaw(runId, outId)
  } catch (e) {
    const msg = apiErrorMessage(e)
    error.value = msg
    toast.error(`Failed to load output: ${msg}`)
  } finally {
    loading.value = false
  }
}, { immediate: true })

function copy() {
  if (!raw.value) return
  navigator.clipboard.writeText(raw.value)
    .then(() => toast.success('Copied to clipboard'))
    .catch(() => toast.error('Copy failed (clipboard API blocked?)'))
}
</script>

<template>
  <div v-if="open && output" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal ov-modal panel" role="dialog" aria-modal="true" aria-label="Output viewer">
      <div class="ov-header">
        <div>
          <div class="ov-eyebrow">Output</div>
          <code class="ov-path">{{ output.path }}</code>
        </div>
        <span class="tag" :class="output.type === 'markdown' ? 'info' : output.type === 'json' ? 'succeeded' : 'idle'">{{ output.type }}</span>
      </div>

      <div v-if="loading" class="ov-empty">Loading…</div>
      <div v-else-if="error" class="ov-error">⚠ {{ error }}</div>
      <pre v-else-if="raw" class="ov-body">{{ raw }}</pre>
      <div v-else class="ov-empty">(empty)</div>

      <div class="ov-actions">
        <button @click="copy" :disabled="!raw">📋 Copy</button>
        <button class="primary" @click="emit('close')">Close</button>
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
.modal { padding: 24px; }
.ov-modal {
  min-width: 40rem; max-width: 60rem;
  padding: 22px 26px 16px;
  display: flex; flex-direction: column; gap: 12px;
  max-height: 86vh;
}
.ov-header {
  display: flex; justify-content: space-between; align-items: flex-start; gap: 14px;
}
.ov-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--ink-mute); font-weight: 500; margin-bottom: 3px;
}
.ov-path {
  font-family: 'Geist Mono', monospace; font-size: 14px;
  color: var(--ink); word-break: break-all;
}
.ov-body {
  margin: 0; padding: 14px;
  background: var(--canvas); border: 1px solid var(--border); border-radius: 6px;
  font-family: 'Geist Mono', monospace; font-size: 12.5px;
  line-height: 1.55; color: var(--ink);
  white-space: pre-wrap; word-break: break-word;
  overflow: auto; flex: 1; min-height: 240px;
}
.ov-empty {
  padding: 30px; text-align: center; color: var(--ink-faint); font-style: italic;
  background: var(--canvas); border-radius: 6px;
}
.ov-error {
  padding: 12px; background: var(--pastel-red-bg); color: var(--pastel-red-fg);
  border-radius: 6px; font-size: 13px;
}
.ov-actions {
  display: flex; gap: 10px; justify-content: flex-end; padding-top: 6px;
}
.ov-actions button { min-width: 90px; padding: 8px 14px; }
</style>
