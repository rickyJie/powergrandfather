<script setup lang="ts">
/**
 * NewProjectModal — creates a Project bucket that groups workflows.
 *
 * Two entry points:
 *  1. Blank creation (`prefill` is null): user types a name, optional
 *     description, no workflows bound yet.
 *  2. "Convert auto-group" (`prefill` is set): user clicked the button
 *     on a heuristic bucket header — name is prefilled with the bucket
 *     label, `workflow_names` carries the current bucket contents so
 *     the backend binds them atomically on create.
 */
import { apiErrorMessage } from '../../lib/apiError'
import { ref, watch } from 'vue'
import { projectsApi } from '../../api/projects'
import { useToast } from '../../composables/useToast'

const props = defineProps<{
  open: boolean
  prefill: { name: string; workflow_names: string[] } | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'saved'): void
}>()

const toast = useToast()
const name = ref('')
const description = ref('')
const submitting = ref(false)

watch(
  () => [props.open, props.prefill] as const,
  ([isOpen, pf]) => {
    if (!isOpen) return
    name.value = pf?.name || ''
    description.value = ''
    submitting.value = false
  },
  { immediate: true },
)

async function submit() {
  const trimmed = name.value.trim()
  if (!trimmed) {
    toast.error('Project name is required')
    return
  }
  submitting.value = true
  try {
    await projectsApi.create({
      name: trimmed,
      description: description.value.trim() || null,
      workflow_names: props.prefill?.workflow_names,
    })
    const bound = props.prefill?.workflow_names?.length || 0
    toast.success(
      bound > 0
        ? `Project ${trimmed} created (${bound} workflow${bound === 1 ? '' : 's'} moved in)`
        : `Project ${trimmed} created`,
    )
    emit('saved')
  } catch (e) {
    toast.error(`Create failed: ${apiErrorMessage(e)}`)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal panel" role="dialog" aria-modal="true" :aria-label="prefill ? 'Convert to project' : 'New project'">
      <h3>{{ prefill ? 'Convert to project' : 'New project' }}</h3>
      <p v-if="prefill" class="hint">
        This auto-detected group holds <b>{{ prefill.workflow_names.length }}</b>
        workflow(s); they'll all be moved into the new project. You can rename it.
      </p>
      <label class="field">
        <span>Name</span>
        <input
          v-model="name"
          class="nt-input"
          placeholder="e.g. MyPipeline"
          @keyup.enter="submit"
        />
      </label>
      <label class="field">
        <span>Description <em class="opt">optional</em></span>
        <textarea
          v-model="description"
          class="nt-input"
          rows="2"
          placeholder="What lives in this project?"
        />
      </label>
      <div class="actions">
        <button @click="emit('close')" :disabled="submitting">Cancel</button>
        <button class="primary" @click="submit" :disabled="submitting || !name.trim()">
          {{ submitting ? 'Saving…' : (prefill ? 'Create & Move' : 'Create') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}
.modal { min-width: 30rem; max-width: 34rem; padding: 24px; display: flex; flex-direction: column; gap: 14px; }
.modal h3 { margin: 0; font-size: 18px; }
.hint { margin: 0; font-size: 13px; color: var(--ink-mute); line-height: 1.5; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field span { font-size: 12px; color: var(--ink-mute); }
.field .opt { color: var(--ink-faint); font-style: normal; font-size: 11px; margin-left: 4px; }
.nt-input {
  width: 100%;
  padding: 8px 10px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 14px;
  color: var(--ink);
}
.nt-input:focus { outline: none; border-color: var(--ink); }
textarea.nt-input { font-family: inherit; resize: vertical; min-height: 60px; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
.actions button { padding: 8px 14px; min-width: 92px; }
</style>
