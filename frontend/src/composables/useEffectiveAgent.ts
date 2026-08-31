/**
 * `useEffectiveAgent(explicit)` — the frontend mirror of the backend's
 * `resolve_agent` precedence chain.
 *
 * Precedence:  explicit > UserPreference.default_agent > null
 *
 * Returns a `ComputedRef<string | null>`. Null only when neither the
 * explicit override nor the preferences store yet knows anything (initial
 * paint before prefs load). Consumers should render a loading state in
 * that case.
 *
 * NOTE: this is the ONLY place resolution logic lives. If a component
 * wants "which agent am I about to spawn?" it must compose this — do
 * not roll your own `explicit || prefs.default || 'claude'` chain
 * (that hardcodes a fallback string and defeats the abstraction).
 */
import { computed, unref, type MaybeRefOrGetter, type ComputedRef } from 'vue'
import { usePreferencesStore } from '../stores/preferences'

export function useEffectiveAgent(
  explicit: MaybeRefOrGetter<string | null | undefined>,
): ComputedRef<string | null> {
  const prefs = usePreferencesStore()
  return computed(() => {
    const e = typeof explicit === 'function' ? (explicit as () => any)() : unref(explicit)
    if (typeof e === 'string' && e) return e
    return prefs.prefs?.default_agent ?? null
  })
}
