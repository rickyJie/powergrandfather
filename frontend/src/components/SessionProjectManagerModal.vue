<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
// Project management modal for interactive-session grouping (local:a79c795d).
// Full CRUD lives here so Sessions.vue only needs one click-to-open handler
// and one prop for the initial state; parent listens for @updated to refresh
// its cached list.
import { onMounted, ref } from 'vue'
import { sessionProjectsApi, type SessionProject } from '../api/sessionProjects'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'updated'): void
}>()

const rows = ref<SessionProject[]>([])
const loading = ref(false)
const includeArchived = ref(false)
const errorMsg = ref('')

const newName = ref('')
const newDesc = ref('')
const creating = ref(false)

const editingId = ref<string | null>(null)
const editName = ref('')
const editDesc = ref('')

async function refresh() {
  loading.value = true
  errorMsg.value = ''
  try {
    const { items } = await sessionProjectsApi.list(includeArchived.value)
    rows.value = items
  } catch (e) {
    errorMsg.value = `Load failed: ${apiErrorMessage(e)}`
  } finally {
    loading.value = false
  }
}

async function create() {
  const name = newName.value.trim()
  if (!name || creating.value) return
  creating.value = true
  errorMsg.value = ''
  try {
    await sessionProjectsApi.create({ name, description: newDesc.value.trim() || null })
    newName.value = ''
    newDesc.value = ''
    await refresh()
    emit('updated')
  } catch (e) {
    errorMsg.value = `Create failed: ${apiErrorMessage(e)}`
  } finally {
    creating.value = false
  }
}

function startEdit(p: SessionProject) {
  editingId.value = p.id
  editName.value = p.name
  editDesc.value = p.description || ''
}

function cancelEdit() {
  editingId.value = null
  editName.value = ''
  editDesc.value = ''
}

async function saveEdit() {
  const id = editingId.value
  if (!id) return
  const name = editName.value.trim()
  if (!name) { cancelEdit(); return }
  try {
    await sessionProjectsApi.update(id, { name, description: editDesc.value.trim() || null })
    cancelEdit()
    await refresh()
    emit('updated')
  } catch (e) {
    errorMsg.value = `Save failed: ${apiErrorMessage(e)}`
  }
}

async function archive(p: SessionProject) {
  if (!confirm(`Archive project "${p.name}"? Sessions in it will fall back to their auto cwd group.`)) return
  try {
    await sessionProjectsApi.archive(p.id)
    await refresh()
    emit('updated')
  } catch (e) {
    errorMsg.value = `Archive failed: ${apiErrorMessage(e)}`
  }
}

async function unarchive(p: SessionProject) {
  try {
    await sessionProjectsApi.unarchive(p.id)
    await refresh()
    emit('updated')
  } catch (e) {
    errorMsg.value = `Unarchive failed: ${apiErrorMessage(e)}`
  }
}

onMounted(refresh)
</script>

<template>
  <div v-if="open" class="modal-backdrop" @click.self="emit('close')" role="presentation">
    <div class="panel modal spm-modal" role="dialog" aria-modal="true" aria-label="Session projects">
      <header class="spm-head">
        <h3>Session projects</h3>
        <label class="spm-toggle">
          <input type="checkbox" v-model="includeArchived" @change="refresh" />
          Show archived
        </label>
        <button @click="emit('close')" class="spm-close">✕</button>
      </header>

      <div v-if="errorMsg" class="spm-error">{{ errorMsg }}</div>

      <section class="spm-create">
        <input v-model="newName" placeholder="new project name" @keydown.enter="create" />
        <input v-model="newDesc" placeholder="description (optional)" @keydown.enter="create" />
        <button class="primary" :disabled="creating || !newName.trim()" @click="create">
          {{ creating ? 'Creating…' : 'Create' }}
        </button>
      </section>

      <div v-if="loading" class="spm-loading">Loading…</div>
      <table v-else class="spm-list">
        <thead>
          <tr>
            <th>Name</th>
            <th>Description</th>
            <th>Sessions</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!rows.length">
            <td colspan="5" class="spm-empty">No projects yet — create one above.</td>
          </tr>
          <tr v-for="p in rows" :key="p.id" :class="{ archived: !!p.archived_at }">
            <td v-if="editingId === p.id">
              <input v-model="editName" @keydown.enter="saveEdit" @keydown.esc="cancelEdit" />
            </td>
            <td v-else>{{ p.name }}</td>

            <td v-if="editingId === p.id">
              <input v-model="editDesc" @keydown.enter="saveEdit" @keydown.esc="cancelEdit" />
            </td>
            <td v-else class="spm-desc">{{ p.description || '—' }}</td>

            <td class="spm-count">{{ p.session_count }}</td>
            <td>
              <span v-if="p.archived_at" class="spm-badge archived">archived</span>
              <span v-else class="spm-badge active">active</span>
            </td>
            <td class="spm-actions">
              <template v-if="editingId === p.id">
                <button @click="saveEdit" class="primary">Save</button>
                <button @click="cancelEdit">Cancel</button>
              </template>
              <template v-else>
                <button @click="startEdit(p)">Rename</button>
                <button v-if="!p.archived_at" @click="archive(p)">Archive</button>
                <button v-else @click="unarchive(p)">Unarchive</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.spm-modal { min-width: 620px; max-width: 90vw; max-height: 80vh; overflow: auto; padding: 16px; }
.spm-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.spm-head h3 { margin: 0; flex: 1; font-family: 'Newsreader', serif; font-weight: 500; }
.spm-toggle { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--ink-mute); }
.spm-close { background: transparent; border: none; font-size: 18px; cursor: pointer; color: var(--ink-mute); }
.spm-error { padding: 8px 12px; background: var(--pastel-red-bg, #fee); color: var(--pastel-red-fg, #900); border-radius: 4px; margin-bottom: 8px; font-size: 12px; }
.spm-create { display: grid; grid-template-columns: 1fr 2fr auto; gap: 8px; margin-bottom: 12px; }
.spm-loading { padding: 20px; text-align: center; color: var(--ink-mute); }
.spm-list { width: 100%; border-collapse: collapse; font-size: 13px; }
.spm-list th, .spm-list td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border); }
.spm-list th { font-weight: 500; color: var(--ink-mute); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.spm-list tr.archived td { opacity: 0.55; }
.spm-desc { color: var(--ink-mute); }
.spm-count { text-align: center; font-family: 'Geist Mono', monospace; }
.spm-empty { text-align: center; padding: 20px; color: var(--ink-mute); }
.spm-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; }
.spm-badge.active { background: var(--pastel-green-bg, #e6f4ea); color: var(--pastel-green-fg, #1e6b39); }
.spm-badge.archived { background: var(--canvas); color: var(--ink-mute); }
.spm-actions { display: flex; gap: 4px; justify-content: flex-end; }
.spm-actions button { font-size: 11px; padding: 2px 8px; }
</style>
