/**
 * `useBackend(name)` — reactive lookup of a Backend's metadata by name.
 *
 * The single access point for anything that needs adapter properties
 * (color, icon, default_argv, flags_schema, status). Components MUST NOT
 * reach into `useBackendsStore().getByName(...)` directly — go through
 * this composable so the reactive dependency is uniform and future
 * enhancements (e.g. auto-refresh on visibility change) live in one place.
 *
 * Returns a `ComputedRef<Backend | null>`. Null when:
 *   - `name` is null / undefined / empty
 *   - The store hasn't loaded yet (`loaded === false`)
 *   - The name isn't registered
 *
 * Consumers that need to render "loading…" or "unknown adapter" states
 * should check `store.loaded` separately from the return value.
 */
import { computed, unref, type MaybeRefOrGetter, type ComputedRef } from 'vue'
import { useBackendsStore } from '../stores/backends'
import type { Backend } from '../api/backends'

export function useBackend(
  name: MaybeRefOrGetter<string | null | undefined>,
): ComputedRef<Backend | null> {
  const store = useBackendsStore()
  return computed(() => {
    const n = typeof name === 'function' ? (name as () => any)() : unref(name)
    if (!n || typeof n !== 'string') return null
    return store.getByName(n) ?? null
  })
}
