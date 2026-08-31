import { apiErrorMessage } from '../lib/apiError'
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { agentsApi, type AgentDef, type AgentConversation, type AgentOverviewItem } from '../api/agents'

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref<AgentDef[]>([])
  // agentId → live conversation (or null if idle)
  const active = ref<Record<string, AgentConversation | null>>({})
  // agentId → recent ended conversations (most recent first)
  const history = ref<Record<string, AgentConversation[]>>({})
  const loading = ref(false)
  const error = ref<string>('')

  async function refresh() {
    loading.value = true
    error.value = ''
    try {
      // Single-shot overview replaces the old list + N×activeConversation.
      const { items } = await agentsApi.overview(5)
      agents.value = items.map((i) => i.agent)
      const a: Record<string, AgentConversation | null> = {}
      const h: Record<string, AgentConversation[]> = {}
      for (const item of items) {
        a[item.agent.id] = item.active
        h[item.agent.id] = item.history
      }
      active.value = a
      history.value = h
    } catch (e) {
      error.value = apiErrorMessage(e)
    } finally {
      loading.value = false
    }
  }

  async function refreshActive(agentId: string) {
    try {
      const r = await agentsApi.activeConversation(agentId)
      active.value = { ...active.value, [agentId]: r.active }
    } catch {
      active.value = { ...active.value, [agentId]: null }
    }
  }

  async function refreshHistory(agentId: string) {
    try {
      const r = await agentsApi.listConversations(agentId, true, 20)
      history.value = {
        ...history.value,
        [agentId]: r.items.filter((c) => c.ended_at !== null),
      }
    } catch { /* ignore */ }
  }

  return { agents, active, history, loading, error, refresh, refreshActive, refreshHistory }
})

// Per-cid conversation cache: items + ws status persist across route hops so
// switching between agent chats doesn't tear down the WS or lose scroll context.
// Items themselves are a generic shape — defined in AgentChat to avoid coupling
// the store to a UI-layer type. This store only holds a string-keyed Map; the
// view inserts/reads its own shape via the typed helpers below.
import { reactive } from 'vue'

interface ConvCacheEntry {
  items: any[]                                  // ChatMessage / ToolUseBlock items, owned by view
  pendingUserKeys: Set<string>
  isAtBottom: boolean
  newSinceScroll: number
  nextId: number
}

const _cache: Map<string, ConvCacheEntry> = reactive(new Map())

export function getConvCache(cid: string): ConvCacheEntry {
  let e = _cache.get(cid)
  if (!e) {
    e = {
      items: reactive([]),
      pendingUserKeys: reactive(new Set()),
      isAtBottom: true,
      newSinceScroll: 0,
      nextId: 1,
    }
    _cache.set(cid, e)
  }
  return e
}

export function clearConvCache(cid: string) {
  _cache.delete(cid)
}
