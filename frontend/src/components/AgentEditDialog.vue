<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { nextTick, ref, watch } from 'vue'
import { agentsApi, type AgentDef, type CreateAgentBody, type PatchAgentBody } from '../api/agents'
import FilePicker from './FilePicker.vue'

const props = defineProps<{
  open: boolean
  agent: AgentDef | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'saved', a: AgentDef): void
}>()

const name = ref('')
const displayName = ref('')
const icon = ref('')
const description = ref('')
const cwd = ref('')
const promptSource = ref('')
const promptCached = ref('')
const refreshFromSource = ref(false)
const disableSkills = ref(false)
const err = ref('')
const busy = ref(false)
const showCwdPicker = ref(false)
const showSrcPicker = ref(false)
function onPickCwd(p: string) { cwd.value = p; showCwdPicker.value = false }
function onPickSrc(p: string) { promptSource.value = p; showSrcPicker.value = false }

const modalEl = ref<HTMLDivElement | null>(null)
const firstField = ref<HTMLInputElement | null>(null)

// Autofocus first field when opening; ESC closes.
watch(
  () => props.open,
  async (now) => {
    if (!now) return
    await nextTick()
    firstField.value?.focus()
  },
)

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return
  if (e.key === 'Escape') {
    e.preventDefault()
    if (!busy.value) emit('close')
    return
  }
  if (e.key !== 'Tab') return
  const root = modalEl.value
  if (!root) return
  const focusable = root.querySelectorAll<HTMLElement>('input:not([disabled]), textarea:not([disabled]), button:not([disabled]), [href]')
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

watch(
  () => [props.open, props.agent],
  () => {
    err.value = ''
    refreshFromSource.value = false
    if (props.agent) {
      name.value = props.agent.name
      displayName.value = props.agent.display_name
      icon.value = props.agent.icon || ''
      description.value = props.agent.description || ''
      cwd.value = props.agent.cwd
      promptSource.value = props.agent.prompt_source || ''
      promptCached.value = props.agent.prompt_cached
      disableSkills.value = !!props.agent.disable_skills
    } else {
      name.value = ''
      displayName.value = ''
      icon.value = ''
      description.value = ''
      cwd.value = ''
      promptSource.value = ''
      promptCached.value = ''
      disableSkills.value = false
    }
  },
  { immediate: true },
)

async function save() {
  err.value = ''
  busy.value = true
  try {
    if (props.agent) {
      const body: PatchAgentBody = {
        display_name: displayName.value,
        icon: icon.value,
        description: description.value,
        cwd: cwd.value,
        prompt_source: promptSource.value,
        disable_skills: disableSkills.value,
      }
      if (refreshFromSource.value) {
        body.refresh_from_source = true
      } else if (promptCached.value !== props.agent.prompt_cached) {
        body.prompt_cached = promptCached.value
      }
      const updated = await agentsApi.patch(props.agent.id, body)
      emit('saved', updated)
    } else {
      const body: CreateAgentBody = {
        name: name.value.trim(),
        display_name: displayName.value.trim() || name.value.trim(),
        cwd: cwd.value.trim(),
        icon: icon.value.trim() || null,
        description: description.value.trim() || null,
        disable_skills: disableSkills.value,
      }
      if (promptCached.value.trim()) {
        body.prompt_cached = promptCached.value
      } else if (promptSource.value.trim()) {
        body.prompt_source = promptSource.value.trim()
      } else {
        err.value = 'either prompt body or prompt_source is required'
        busy.value = false
        return
      }
      if (promptSource.value.trim()) body.prompt_source = promptSource.value.trim()
      const created = await agentsApi.create(body)
      emit('saved', created)
    }
  } catch (e) {
    err.value = apiErrorMessage(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div
    v-if="open"
    class="modal-backdrop"
    role="presentation"
    @click.self="emit('close')"
    @keydown="onKeydown"
  >
    <div
      ref="modalEl"
      class="modal panel"
      role="dialog"
      aria-modal="true"
      :aria-label="agent ? 'Edit agent' : 'New agent'"
    >
      <h3 class="serif">{{ agent ? 'Edit agent' : 'New agent' }}</h3>
      <div class="grid">
        <label class="full" v-if="!agent">
          name (machine id)
          <input ref="firstField" v-model="name" placeholder="e.g. code_reviewer" />
        </label>
        <label class="full">
          display name
          <input
            :ref="!agent ? undefined : (el: any) => (firstField = el)"
            v-model="displayName"
            placeholder="Human-readable name"
          />
        </label>
        <label>
          icon (emoji)
          <input v-model="icon" placeholder="🔍" maxlength="8" />
        </label>
        <label>
          cwd (absolute path)
          <div style="display:flex; gap:6px; align-items:center;">
            <input v-model="cwd" placeholder="&lt;REPOS&gt;/foo" style="flex:1" />
            <button type="button" @click="showCwdPicker = true">📁</button>
          </div>
        </label>
        <label class="full">
          description
          <input v-model="description" placeholder="One-line role description" />
        </label>
        <label class="full inline">
          <input type="checkbox" v-model="disableSkills" />
          <span>Disable all Skill calls</span>
          <small>Passes <code>--disallowedTools Skill</code> to claude, so this agent cannot invoke any installed skill. Use when you need strict role isolation.</small>
        </label>
        <label class="full">
          prompt source (file path or http(s) URL)
          <div style="display:flex; gap:6px; align-items:center;">
            <input v-model="promptSource" placeholder="/path/to/prompt.md  or  https://..." style="flex:1" />
            <button type="button" @click="showSrcPicker = true">📁</button>
          </div>
        </label>
        <label class="full" v-if="agent">
          <span class="row-between">
            <span>prompt body (cached)</span>
            <span>
              <input type="checkbox" v-model="refreshFromSource" id="refresh-src" />
              <label for="refresh-src" style="font-size:11px"> Reload from source on save</label>
            </span>
          </span>
          <textarea v-model="promptCached" rows="10" :disabled="refreshFromSource"></textarea>
        </label>
        <label class="full" v-else>
          prompt body (leave empty to pull from source)
          <textarea v-model="promptCached" rows="10" placeholder="Paste system prompt directly"></textarea>
        </label>
      </div>
      <p v-if="err" class="err">{{ err }}</p>
      <div class="actions">
        <button @click="emit('close')" :disabled="busy">Cancel</button>
        <button class="primary" @click="save" :disabled="busy">{{ busy ? '…' : (agent ? 'Save' : 'Create') }}</button>
      </div>
    </div>
    <FilePicker
      :open="showCwdPicker"
      mode="dir"
      :initial-path="cwd"
      title="Pick agent cwd"
      @close="showCwdPicker = false"
      @pick="onPickCwd"
    />
    <FilePicker
      :open="showSrcPicker"
      mode="file"
      :initial-path="promptSource"
      title="Pick prompt source file"
      :show-recent="false"
      @close="showSrcPicker = false"
      @pick="onPickSrc"
    />
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 99;
}
.modal { padding: 24px; min-width: 40rem; max-width: 52rem; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 14px; }
.grid label { display: grid; gap: 4px; font-size: 12px; color: var(--ink-mute); }
.grid label.full { grid-column: 1 / -1; }
.grid input, .grid textarea {
  font-family: inherit;
  font-size: 13px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--card);
  color: var(--ink);
}
.grid textarea { font-family: 'Geist Mono', monospace; font-size: 12px; }
.grid label.inline {
  flex-direction: row; gap: 8px; align-items: flex-start;
  padding: 8px 10px; border: 1px dashed var(--border); border-radius: 6px;
  background: var(--canvas);
  display: flex;
}
.grid label.inline input[type="checkbox"] { margin-top: 2px; }
.grid label.inline span { font-size: 13px; color: var(--ink); font-weight: 500; }
.grid label.inline small {
  flex-basis: 100%; margin-left: 0; margin-top: 4px;
  font-size: 11px; color: var(--ink-mute); font-weight: 400;
}
.grid label.inline code {
  font-family: 'Geist Mono', monospace;
  background: var(--card); padding: 1px 4px; border-radius: 3px; font-size: 10px;
}
.row-between { display: flex; justify-content: space-between; align-items: center; }
.err { color: var(--pastel-red-fg); font-size: 12px; margin-top: 10px; }
.actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
</style>
