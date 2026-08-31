<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import { agentsApi, type AgentDef } from '../api/agents'
import AgentCard from '../components/AgentCard.vue'
import AgentEditDialog from '../components/AgentEditDialog.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const router = useRouter()
const store = useAgentsStore()

const dialogOpen = ref(false)
const editingAgent = ref<AgentDef | null>(null)
const actionErr = ref('')

onMounted(() => store.refresh())

function openCreate() {
  editingAgent.value = null
  dialogOpen.value = true
}

function openEdit(a: AgentDef) {
  editingAgent.value = a
  dialogOpen.value = true
}

async function onSaved() {
  dialogOpen.value = false
  await store.refresh()
}

const pendingDelete = ref<AgentDef | null>(null)
function askDelete(a: AgentDef) {
  pendingDelete.value = a
}
async function confirmDelete() {
  const a = pendingDelete.value
  pendingDelete.value = null
  if (!a) return
  actionErr.value = ''
  try {
    await agentsApi.delete(a.id)
    await store.refresh()
  } catch (e) {
    actionErr.value = apiErrorMessage(e)
  }
}

async function openChat(a: AgentDef) {
  actionErr.value = ''
  try {
    const conv = await agentsApi.spawn(a.id)
    router.push(`/agents/conversations/${conv.id}`)
  } catch (e) {
    actionErr.value = apiErrorMessage(e)
  }
}

function openHistoryConv(cid: string) {
  router.push(`/agents/conversations/${cid}`)
}
</script>

<template>
  <div class="deck-page">
    <div class="toolbar">
      <h2 class="serif">Agents</h2>
      <div class="right">
        <button @click="store.refresh()" :disabled="store.loading">{{ store.loading ? '…' : '↻' }} Refresh</button>
        <button class="primary" @click="openCreate">+ New Agent</button>
      </div>
    </div>

    <div v-if="store.error" class="err-banner">load failed: {{ store.error }}</div>
    <div v-if="actionErr" class="err-banner">{{ actionErr }}</div>

    <div class="deck-body">
      <div v-if="!store.agents.length && !store.loading" class="empty-state panel">
        <h3 class="serif">No agents yet</h3>
        <p>An agent is a Claude entry point with a preset cwd + system prompt. Click <strong>+ New Agent</strong> to start.</p>
      </div>

      <div v-else class="grid">
        <AgentCard
          v-for="a in store.agents"
          :key="a.id"
          :agent="a"
          :active="store.active[a.id] || null"
          :history="store.history[a.id] || []"
          @open="openChat(a)"
          @open-history="openHistoryConv"
          @edit="openEdit(a)"
          @delete="askDelete(a)"
        />
      </div>
    </div>

    <AgentEditDialog
      :open="dialogOpen"
      :agent="editingAgent"
      @close="dialogOpen = false"
      @saved="onSaved"
    />

    <ConfirmDialog
      :open="!!pendingDelete"
      title="Delete agent?"
      :message="pendingDelete ? `This deletes the agent preset &quot;${pendingDelete.display_name}&quot;. Ended conversations are kept; active conversations must be ended first.` : ''"
      confirm-text="Delete"
      :danger="true"
      @confirm="confirmDelete"
      @cancel="pendingDelete = null"
    />
  </div>
</template>

<style scoped>
.deck-page { display: flex; flex-direction: column; height: 100%; }
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--card);
}
.toolbar h2 { margin: 0; font-size: 18px; }
.toolbar .right { display: flex; gap: 8px; }
.err-banner {
  margin: 10px 20px 0;
  padding: 8px 12px;
  background: var(--pastel-red-bg);
  color: var(--pastel-red-fg);
  border-radius: 6px;
  font-size: 12px;
}
.deck-body { padding: 20px; overflow: auto; flex: 1; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 14px;
}
.empty-state { padding: 40px; text-align: center; }
.empty-state h3 { font-size: 18px; margin-bottom: 6px; }
.empty-state p { color: var(--ink-mute); }
</style>
