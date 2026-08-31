/**
 * Pinia store wrapping the single-row UserPreference resource.
 *
 * Loaded once on app boot (App.vue does the `ensureLoaded()` call). The
 * FirstRunWizard reads `is_first_run` to decide whether to render.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  completeFirstRun,
  getPreferences,
  updatePreferences,
  type PreferencePatch,
  type Preferences,
} from '../api/preferences'
import { formatApiError } from '../api/client'

export const usePreferencesStore = defineStore('preferences', () => {
  const prefs = ref<Preferences | null>(null)
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref<string | null>(null)

  async function refresh() {
    loading.value = true
    error.value = null
    try {
      prefs.value = await getPreferences()
      loaded.value = true
    } catch (e) {
      error.value = formatApiError(e)
    } finally {
      loading.value = false
    }
  }

  async function ensureLoaded() {
    if (!loaded.value && !loading.value) await refresh()
  }

  async function update(patch: PreferencePatch) {
    prefs.value = await updatePreferences(patch)
  }

  async function completeWizard() {
    prefs.value = await completeFirstRun()
  }

  return {
    prefs, loading, loaded, error,
    refresh, ensureLoaded, update, completeWizard,
  }
})
