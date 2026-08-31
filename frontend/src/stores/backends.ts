/**
 * Pinia store for the list of registered CLI adapter backends.
 *
 * The list mostly changes when a user installs / uninstalls / logs into
 * a CLI, which is rare — so we cache aggressively but expose `.refresh()`
 * for pages that show the setup guide.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { listBackends, type Backend } from '../api/backends'
import { formatApiError } from '../api/client'

export const useBackendsStore = defineStore('backends', () => {
  const items = ref<Backend[]>([])
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref<string | null>(null)

  // Self-heal after a transient load failure. A single failed /api/backends
  // (e.g. a tunnel blip while Settings mounts) otherwise leaves the store
  // permanently empty — the AgentSelector shows "no agents available" and the
  // user can't pick claude/codex until a full remount. Retry with backoff.
  let retryTimer: number | null = null
  let retryAttempt = 0
  const MAX_RETRY = 5

  function scheduleRetry() {
    if (retryTimer != null || loaded.value || retryAttempt >= MAX_RETRY) return
    const delay = Math.min(1000 * 2 ** retryAttempt, 15_000)
    retryAttempt += 1
    retryTimer = window.setTimeout(() => {
      retryTimer = null
      if (!loaded.value) refresh()
    }, delay)
  }

  async function refresh() {
    loading.value = true
    error.value = null
    try {
      items.value = await listBackends()
      loaded.value = true
      retryAttempt = 0 // success resets the backoff ladder
    } catch (e) {
      error.value = formatApiError(e)
      scheduleRetry()
    } finally {
      loading.value = false
    }
  }

  async function ensureLoaded() {
    if (!loaded.value && !loading.value) await refresh()
  }

  function getByName(name: string): Backend | undefined {
    return items.value.find(b => b.name === name)
  }

  // M9.2: computed so re-derivations cache. Previous `usable()` method
  // returned a fresh Array every call, defeating memoisation in consumers.
  const usable = computed(() => items.value.filter(b => b.status.usable))
  const hasAnyUsable = computed(() => usable.value.length > 0)

  return {
    items, loading, loaded, error,
    refresh, ensureLoaded, getByName,
    usable, hasAnyUsable,
  }
})
