import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ContextMenuItem } from '../components/ContextMenu.vue'

// ---------------------------------------------------------------------------
// Session UI store — extracted from Sessions.vue (H1 P1, 2026-07-25).
//
// Purpose: consolidate ephemeral view-level UI state so Sessions.vue and any
// future split child components (SessionSidebar / TerminalPanel / etc.) can
// share the same source of truth without prop-drilling or emit-plumbing.
//
// Scope: PURE UI STATE ONLY. Data that lives on the server (session rows,
// notifications, agent conversations) has its own store / composable and must
// NOT be duplicated here.
//
// Naming / shape choices match the pre-existing Sessions.vue locals so slot 12
// (the actual rewrite / migration) can do a near-mechanical find-replace.
//   - `activeSid` mirrors the `activeSid` computed(route.params.sid) in
//     Sessions.vue. This store's version is a writable ref so components that
//     don't own the router can still request navigation targets; the actual
//     router.push stays in Sessions.vue.
//   - `contextMenu.visible` (not `open`) matches ContextMenu.vue's prop name.
//   - `sidebarWidth` uses localStorage key `csm.sess.sidebarW`, same as the
//     existing implementation, so users don't lose their saved width on the
//     first load after migration.
// ---------------------------------------------------------------------------

const SIDEBAR_WIDTH_KEY = 'csm.sess.sidebarW'
const SIDEBAR_WIDTH_DEFAULT = 260
const SIDEBAR_WIDTH_MIN = 180
const SIDEBAR_WIDTH_MAX = 600

const KEEP_ENDED_OPEN_KEY = 'csm.sess.keepEndedOpen'

function loadKeepEndedOpen(): boolean {
  try {
    return localStorage.getItem(KEEP_ENDED_OPEN_KEY) === '1'
  } catch {
    return false
  }
}

function saveKeepEndedOpen(v: boolean): void {
  try {
    localStorage.setItem(KEEP_ENDED_OPEN_KEY, v ? '1' : '0')
  } catch { /* best-effort */ }
}

function loadSidebarWidth(): number {
  try {
    const raw = localStorage.getItem(SIDEBAR_WIDTH_KEY)
    if (!raw) return SIDEBAR_WIDTH_DEFAULT
    const parsed = parseInt(raw, 10)
    if (Number.isNaN(parsed)) return SIDEBAR_WIDTH_DEFAULT
    return Math.max(SIDEBAR_WIDTH_MIN, Math.min(SIDEBAR_WIDTH_MAX, parsed))
  } catch {
    return SIDEBAR_WIDTH_DEFAULT
  }
}

function saveSidebarWidth(w: number): void {
  try {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(w))
  } catch {
    /* private-mode / disk full — treat as best-effort */
  }
}

export interface ContextMenuState {
  visible: boolean
  x: number
  y: number
  items: ContextMenuItem[]
  // Session id the menu was opened against, when applicable. Not consumed by
  // ContextMenu.vue itself; kept here so callbacks fired from the menu can
  // reference the target without closing over the click event.
  targetSid: string | null
}

export const useSessionUIStore = defineStore('sessionUI', () => {
  // --- terminal / active session ------------------------------------------
  const activeSid = ref<string | null>(null)
  const isTerminalMaxed = ref(false)

  function setActive(sid: string | null): void {
    activeSid.value = sid
  }

  function toggleFullscreen(): void {
    isTerminalMaxed.value = !isTerminalMaxed.value
  }

  function setFullscreen(v: boolean): void {
    isTerminalMaxed.value = v
  }

  // --- sidebar width (persisted) ------------------------------------------
  const sidebarWidth = ref<number>(loadSidebarWidth())

  function setSidebarWidth(w: number): void {
    const clamped = Math.max(SIDEBAR_WIDTH_MIN, Math.min(SIDEBAR_WIDTH_MAX, w))
    sidebarWidth.value = clamped
    saveSidebarWidth(clamped)
  }

  // --- context menu (right-click on session row / overflow) ---------------
  const contextMenu = ref<ContextMenuState>({
    visible: false,
    x: 0,
    y: 0,
    items: [],
    targetSid: null,
  })

  function openContextMenu(
    x: number,
    y: number,
    items: ContextMenuItem[],
    targetSid: string | null = null,
  ): void {
    contextMenu.value = { visible: true, x, y, items, targetSid }
  }

  function closeContextMenu(): void {
    // Preserve the rest of the state so an outside-click that races with a
    // re-open doesn't drop the items list. Only flip visibility.
    contextMenu.value = { ...contextMenu.value, visible: false }
  }

  // --- file browser panel toggle ------------------------------------------
  // Not yet consumed by Sessions.vue but scoped here in advance so the
  // rewrite slot can wire it without another round-trip through this store.
  const showFileBrowser = ref(false)

  function toggleFileBrowser(): void {
    showFileBrowser.value = !showFileBrowser.value
  }

  function setFileBrowser(v: boolean): void {
    showFileBrowser.value = v
  }

  // --- auto-close ended sessions (persisted) ------------------------------
  // When a session transitions to exited/crashed, Sessions.vue triggers
  // a short auto-close timer that navigates back to the list. Users who
  // want to inspect the exit code / final buffer flip this to true.
  const keepEndedOpen = ref<boolean>(loadKeepEndedOpen())

  function setKeepEndedOpen(v: boolean): void {
    keepEndedOpen.value = v
    saveKeepEndedOpen(v)
  }

  return {
    // state
    activeSid,
    isTerminalMaxed,
    sidebarWidth,
    contextMenu,
    showFileBrowser,
    keepEndedOpen,
    // actions
    setActive,
    toggleFullscreen,
    setFullscreen,
    setSidebarWidth,
    openContextMenu,
    closeContextMenu,
    toggleFileBrowser,
    setFileBrowser,
    setKeepEndedOpen,
  }
})
