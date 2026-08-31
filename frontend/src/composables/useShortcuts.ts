/**
 * Tiny keyboard-shortcut composable. Registers `keydown` listeners scoped
 * to the component lifecycle. Ignores keystrokes when an editable element
 * has focus (input/textarea/contenteditable).
 *
 * Usage:
 *   useShortcuts({
 *     'n': () => openNewTask(),
 *     'r': () => refresh(),
 *     'esc': () => closeAll(),
 *   })
 *
 * Keys are case-insensitive. Modifiers: prefix with `mod+` for Ctrl/Cmd.
 */
import { onMounted, onUnmounted } from 'vue'

export type ShortcutMap = Record<string, (e: KeyboardEvent) => void | Promise<void>>

function targetIsEditable(t: EventTarget | null): boolean {
  if (!t) return false
  const el = t as HTMLElement
  if (el.isContentEditable) return true
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

function normalizeKey(e: KeyboardEvent): string {
  const parts: string[] = []
  if (e.ctrlKey || e.metaKey) parts.push('mod')
  if (e.shiftKey) parts.push('shift')
  if (e.altKey) parts.push('alt')
  const k = e.key.toLowerCase()
  // Special-case some keys for friendlier binding strings.
  const mapped = k === 'escape' ? 'esc' : k === ' ' ? 'space' : k
  parts.push(mapped)
  return parts.join('+')
}

export function useShortcuts(map: ShortcutMap) {
  function onKey(e: KeyboardEvent) {
    if (targetIsEditable(e.target)) return
    const key = normalizeKey(e)
    const handler = map[key]
    if (handler) {
      e.preventDefault()
      handler(e)
    }
  }
  onMounted(() => window.addEventListener('keydown', onKey))
  onUnmounted(() => window.removeEventListener('keydown', onKey))
}
