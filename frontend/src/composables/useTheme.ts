/**
 * Theme: light | dark | auto. Persists choice in localStorage; "auto"
 * means follow `prefers-color-scheme`. CSS does the heavy lifting — we
 * just set/clear `<html data-theme>`.
 */
import { onMounted, ref, watch } from 'vue'

export type Theme = 'auto' | 'light' | 'dark'

const STORAGE_KEY = 'csm.theme'
const theme = ref<Theme>('auto')

function apply(t: Theme) {
  const html = document.documentElement
  if (t === 'auto') html.removeAttribute('data-theme')
  else html.setAttribute('data-theme', t)
}

export function useTheme() {
  onMounted(() => {
    const stored = localStorage.getItem(STORAGE_KEY) as Theme | null
    if (stored === 'light' || stored === 'dark' || stored === 'auto') {
      theme.value = stored
    }
    apply(theme.value)
  })
  watch(theme, (t) => {
    localStorage.setItem(STORAGE_KEY, t)
    apply(t)
  })
  function cycle() {
    theme.value = theme.value === 'auto' ? 'light' : theme.value === 'light' ? 'dark' : 'auto'
  }
  return { theme, cycle }
}
