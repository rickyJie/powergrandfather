<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AgentDef, AgentConversation } from '../api/agents'

const props = defineProps<{
  agent: AgentDef
  active: AgentConversation | null
  history?: AgentConversation[]
}>()

const emit = defineEmits<{
  (e: 'open'): void
  (e: 'open-history', cid: string): void
  (e: 'edit'): void
  (e: 'delete'): void
}>()

const status = computed(() => (props.active ? 'running' : 'idle'))
const statusLabel = computed(() => (props.active ? 'Running · click to open' : 'Idle · click to start a session'))
const showHistory = ref(false)
const historyItems = computed(() => props.history || [])

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  const ms = Date.now() - d.getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return d.toISOString().slice(5, 10)
}
</script>

<template>
  <div class="card">
    <div class="card-body" @click="emit('open')">
      <div class="card-head">
        <div class="icon">{{ agent.icon || '🤖' }}</div>
        <div class="status-dot" :class="status" :title="statusLabel"></div>
      </div>
      <div class="title">{{ agent.display_name }}</div>
      <div class="desc" v-if="agent.description">{{ agent.description }}</div>
      <div class="meta mono">{{ agent.cwd }}</div>
    </div>

    <div v-if="historyItems.length" class="history">
      <button class="hist-toggle" @click.stop="showHistory = !showHistory">
        <span class="caret" :class="{ open: showHistory }">▸</span>
        History ({{ historyItems.length }})
      </button>
      <div v-if="showHistory" class="hist-list" @click.stop>
        <button
          v-for="c in historyItems"
          :key="c.id"
          class="hist-row"
          @click="emit('open-history', c.id)"
          :title="`cid ${c.id.slice(0, 8)}`"
        >
          <span class="hist-title">{{ c.title || '(untitled)' }}</span>
          <span class="hist-time mono">{{ fmtTime(c.last_activity_ts || c.created_at) }}</span>
        </button>
      </div>
    </div>

    <div class="card-actions" @click.stop>
      <button class="ghost" @click="emit('edit')">Edit</button>
      <button class="ghost danger" @click="emit('delete')">Delete</button>
    </div>
  </div>
</template>

<style scoped>
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: all 180ms var(--ease-soft);
  position: relative;
}
.card:hover {
  border-color: var(--ink);
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(0,0,0,0.06);
}
.card-body {
  cursor: pointer;
  display: flex; flex-direction: column; gap: 4px;
  padding-bottom: 4px;
}
.card-head { display: flex; justify-content: space-between; align-items: center; }
.icon { font-size: 26px; line-height: 1; }
.status-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  background: var(--border);
}
.status-dot.running {
  background: var(--pastel-green-fg, #346538);
  box-shadow: 0 0 0 3px rgba(52,101,56,0.18);
}
.status-dot.idle { background: #cfcfcd; }
.title { font-family: 'Newsreader', serif; font-size: 18px; color: var(--ink); }
.desc { font-size: 12px; color: var(--ink-mute); }
.meta {
  font-size: 10px;
  color: var(--ink-mute);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-actions {
  display: flex; gap: 6px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.card-actions button {
  font-size: 11px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 3px 8px;
  color: var(--ink-mute);
}
.card-actions button:hover { color: var(--ink); border-color: var(--ink); }
.card-actions button.danger:hover { color: var(--pastel-red-fg); border-color: var(--pastel-red-fg); }

.history {
  border-top: 1px dashed var(--border);
  padding-top: 6px;
  font-size: 11px;
}
.hist-toggle {
  width: 100%;
  display: flex; align-items: center; gap: 6px;
  background: transparent; border: none; cursor: pointer;
  color: var(--ink-mute);
  font-size: 11px;
  padding: 2px 0;
  text-align: left;
}
.hist-toggle:hover { color: var(--ink); }
.caret { display: inline-block; transition: transform 150ms; }
.caret.open { transform: rotate(90deg); }
.hist-list { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }
.hist-row {
  display: flex; gap: 8px; align-items: baseline;
  padding: 3px 6px;
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
  text-align: left;
  width: 100%;
}
.hist-row:hover { background: var(--canvas); }
.hist-title {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 11px;
  color: var(--ink);
}
.hist-time { font-size: 9px; color: var(--ink-mute); flex-shrink: 0; }
</style>
