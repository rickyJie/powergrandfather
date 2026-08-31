<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useNotificationsStore } from './stores/notifications'
import { useBackendsStore } from './stores/backends'
import { usePreferencesStore } from './stores/preferences'
import ToastStack from './components/ToastStack.vue'
import NotificationPanel from './components/NotificationPanel.vue'
import FirstRunWizard from './components/FirstRunWizard.vue'
import WorktimeWidget from './components/WorktimeWidget.vue'
import PwaStatus from './components/PwaStatus.vue'
import { useTheme } from './composables/useTheme'
import { useTabBadge } from './composables/useTabBadge'
import { useWorktimeHeartbeat } from './composables/useWorktimeHeartbeat'
import { registerDisplayModeWithSW } from './lib/desktopNotify'

const showNotifications = ref(false)
const showMobileMore = ref(false)
const { theme, cycle: cycleTheme } = useTheme()
const route = useRoute()
const router = useRouter()

// vue-router 4's built-in `router-link-active` only lights up on the target
// route record itself + any declared child routes. Our routes are FLAT
// (`/sessions` and `/sessions/:sid` are siblings, not parent-child), so
// visiting `/sessions/<sid>` does NOT activate the sidebar's Sessions icon.
// Same for `/agents/conversations/:cid` etc. Compute the highlight from a
// prefix match instead so any URL under a module lights up its icon.
function isModuleActive(prefix: string): boolean {
  return route.path === prefix || route.path.startsWith(prefix + '/')
}

const store = useNotificationsStore()
// The bell is the SOLE surface for notification state. Sidebar module icons
// (Sessions / Agents / Automation / …) intentionally do NOT read from the
// notifications store — they represent module navigation, not message
// unread. This keeps the Sessions module fully decoupled from the
// notification channel (a token warning / port conflict must never light up
// the "S" icon).
const unread = computed(() => store.totalUnread)
// True whenever any session has an unread "Permission required" notif —
// drives the bell dot's orange-pulse variant and useTabBadge's `[!]` prefix
// so a fullscreen user (whose sole cross-session channel is bell + tab
// title) can distinguish "someone is blocked" from "someone got a message".
const permPending = computed(() => store.hasPendingPermission)
const mobileMoreActive = computed(() =>
  ['/budgets', '/settings'].some(prefix => isModuleActive(prefix)),
)

watch(() => route.fullPath, () => { showMobileMore.value = false })

// Mirror the unread count onto the browser tab (title prefix + favicon dot) so
// the signal survives the user tabbing away from the app.
useTabBadge(unread, permPending)

// Worktime heartbeat: pings backend every 30s while tab is visible + active.
// Backend HeartbeatManager owns the `kind=human` interval state machine.
useWorktimeHeartbeat()

// Multi-agent v2: warm up backends + preferences stores at boot so the
// first-run wizard can render and AgentBadge / AgentSelector components
// have data immediately.
const backendsStore = useBackendsStore()
const prefsStore = usePreferencesStore()

onMounted(() => {
  store.refresh()
  store.connect()
  backendsStore.ensureLoaded()
  prefsStore.ensureLoaded()
  // Tell the SW whether we're the installed PWA window or a browser tab, so a
  // notification click focuses the right one (see lib/desktopNotify.ts).
  registerDisplayModeWithSW()
  // Clicking a desktop OS notification focuses this window and the SW posts us
  // the target URL — route the already-loaded SPA in place (no reload).
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (e: MessageEvent) => {
      const d = e.data
      if (d && d.type === 'csm-notif-navigate' && typeof d.url === 'string') {
        router.push(d.url).catch(() => { /* already there / invalid */ })
      }
    })
  }
})
</script>

<template>
  <div class="app">
    <div class="topbar">
      <span class="logo">PowerGrandFather</span>
      <div class="right">
        <button @click="cycleTheme" :title="`Theme: ${theme}`">{{ theme === 'dark' ? '🌙' : theme === 'light' ? '☀' : '◐' }}</button>
        <button
          class="bell"
          :class="{ has: unread > 0, 'has-permission': permPending }"
          :title="permPending ? `${unread} unread · permission required` : `${unread} unread`"
          @click="showNotifications = !showNotifications"
        >
          🔔<span v-if="unread" class="bell-dot" :class="{ perm: permPending }"></span>
        </button>
      </div>
      <WorktimeWidget class="topbar-worktime" />
    </div>

    <div class="body">
      <nav class="sidebar" aria-label="Modules">
        <router-link to="/sessions" title="Sessions" :class="{ 'module-active': isModuleActive('/sessions') }">
          <span class="icon">S</span><span class="label">Sessions</span>
        </router-link>
        <router-link to="/agents" title="Agents" :class="{ 'module-active': isModuleActive('/agents') }">
          <span class="icon">G</span><span class="label">Agents</span>
        </router-link>
        <router-link to="/automation" title="Automation" :class="{ 'module-active': isModuleActive('/automation') }">
          <span class="icon">A</span><span class="label">Automation</span>
        </router-link>
        <router-link to="/tokens" title="Tokens" :class="{ 'module-active': isModuleActive('/tokens') }">
          <span class="icon">T</span><span class="label">Tokens</span>
        </router-link>
        <router-link to="/budgets" title="Budgets" :class="{ 'module-active': isModuleActive('/budgets') }">
          <span class="icon">B</span><span class="label">Budgets</span>
        </router-link>
        <router-link to="/settings" title="Settings" :class="{ 'module-active': isModuleActive('/settings') }">
          <span class="icon">⚙</span><span class="label">Settings</span>
        </router-link>
      </nav>

      <main class="canvas">
        <router-view />
      </main>
    </div>
    <nav class="mobile-tabbar" aria-label="Primary mobile navigation">
      <router-link to="/sessions" :class="{ active: isModuleActive('/sessions') }">
        <span class="mobile-tab-icon">S</span><span>Sessions</span>
      </router-link>
      <router-link to="/automation" :class="{ active: isModuleActive('/automation') }">
        <span class="mobile-tab-icon">A</span><span>Automation</span>
      </router-link>
      <router-link to="/agents" :class="{ active: isModuleActive('/agents') }">
        <span class="mobile-tab-icon">G</span><span>Agents</span>
      </router-link>
      <router-link to="/tokens" :class="{ active: isModuleActive('/tokens') }">
        <span class="mobile-tab-icon">T</span><span>Insights</span>
      </router-link>
      <button type="button" :class="{ active: mobileMoreActive || showMobileMore }"
        @click="showMobileMore = !showMobileMore" aria-haspopup="dialog" :aria-expanded="showMobileMore">
        <span class="mobile-tab-icon">•••</span><span>More</span>
      </button>
    </nav>
    <div v-if="showMobileMore" class="mobile-more-backdrop" @click="showMobileMore = false"></div>
    <aside v-if="showMobileMore" class="mobile-more-sheet" aria-label="More modules">
      <div class="mobile-more-handle"></div>
      <div class="mobile-more-title">More</div>
      <router-link to="/budgets"><b>B</b><span>Budgets<small>Token and cost guardrails</small></span></router-link>
      <router-link to="/settings"><b>⚙</b><span>Settings<small>Notifications, sync and preferences</small></span></router-link>
    </aside>
    <ToastStack />
    <NotificationPanel :open="showNotifications" @close="showNotifications = false" />
    <FirstRunWizard />
    <PwaStatus />
  </div>
</template>

<style scoped>
.app { display: grid; grid-template-rows: 48px minmax(0, 1fr); height: 100vh; height: 100dvh; }

.topbar {
  display: flex; align-items: center; gap: 16px;
  padding: 0 20px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
}
.topbar .logo-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
  margin-right: -8px;
}
.topbar .logo {
  font-family: 'Newsreader', serif;
  font-weight: 500;
  font-size: 18px;
  color: var(--ink);
}
.topbar .stats { color: var(--ink-mute); font-size: 13px; flex: 1; }
.topbar .right { display: flex; align-items: center; gap: 6px; }
.topbar .topbar-worktime { margin-left: auto; }
.topbar kbd {
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--canvas);
  padding: 1px 5px;
}
.topbar .bell { position: relative; }
.topbar .bell-dot {
  position: absolute; top: 2px; right: 4px;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--pastel-red-fg);
  box-shadow: 0 0 0 2px var(--card);
}
/* Permission-pending variant — some session is blocked waiting for user
   approval. Switches to a warm amber and pulses so it reads distinctly
   from a "just got a new message" red dot. Bright ring around the dot
   makes it survive against the emoji bell glyph in dark themes too. */
.topbar .bell-dot.perm {
  background: var(--pastel-yellow-fg);
  box-shadow: 0 0 0 2px var(--card), 0 0 8px var(--pastel-yellow-fg);
  animation: bell-perm-pulse 1s ease-in-out infinite;
}
@keyframes bell-perm-pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50%      { transform: scale(0.75); opacity: 0.55; }
}

.body { display: grid; grid-template-columns: 56px 1fr; overflow: hidden; height: 100%; }

.sidebar {
  background: var(--card);
  border-right: 1px solid var(--border);
  padding: 12px 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  transition: width 220ms var(--ease-soft);
  width: 56px;
  position: relative;
  z-index: 20;   /* float over canvas when expanded */
}
/* F8 — expand to reveal labels on hover; canvas stays in place so
   this doesn't reflow the whole layout. */
.sidebar:hover { width: 176px; box-shadow: var(--shadow-md); }
.sidebar a {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px;
  margin: 4px 8px;
  border-radius: 8px;
  color: var(--ink-mute);
  font-weight: 500;
  position: relative;
  transition: background 120ms var(--ease-soft), color 120ms var(--ease-soft);
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
}
.sidebar a .icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; flex: 0 0 24px;
}
.sidebar a .label {
  font-size: 13px; opacity: 0;
  transform: translateX(-6px);
  transition: opacity 160ms 60ms var(--ease-soft), transform 220ms var(--ease-soft);
  pointer-events: none;
}
.sidebar:hover a .label { opacity: 1; transform: none; }
.sidebar a:hover { background: var(--canvas); color: var(--ink); }
/* Highlight the icon for the current module. `.router-link-active` covers the
   exact-match case (e.g. sitting on /sessions itself); `.module-active` is
   the prefix-match branch we compute manually so child routes like
   /sessions/<sid> still light up the icon. */
.sidebar a.router-link-active,
.sidebar a.module-active { background: var(--ink); color: var(--card); }
.sidebar a .badge {
  position: absolute; top: -4px; right: -4px;
  transform: scale(0.85); transform-origin: top right;
}

.canvas {
  overflow: hidden;
  background: var(--canvas);
  display: flex; flex-direction: column;
}

.mobile-tabbar,
.mobile-more-sheet,
.mobile-more-backdrop { display: none; }

/* Mobile: app-style bottom navigation. The desktop sidebar disappears rather
   than taking 44px away from an already narrow terminal. */
@media (max-width: 640px) {
  .app {
    grid-template-rows: 48px minmax(0, 1fr) calc(58px + env(safe-area-inset-bottom));
  }
  .body { grid-template-columns: 1fr; min-height: 0; }
  .sidebar { display: none; }
  .topbar { padding: 0 12px; gap: 8px; }
  .topbar .logo { font-size: 15px; }
  .topbar .logo-avatar { width: 20px; height: 20px; margin-right: -4px; }
  .canvas { min-width: 0; }

  .mobile-tabbar {
    z-index: 70;
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    align-items: stretch;
    padding: 4px 4px env(safe-area-inset-bottom);
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--card) 94%, transparent);
    backdrop-filter: blur(16px);
  }
  .mobile-tabbar a,
  .mobile-tabbar button {
    min-width: 0;
    min-height: 50px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1px;
    padding: 3px 2px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--ink-mute);
    text-decoration: none;
    font-size: 9px;
    font-weight: 600;
  }
  .mobile-tabbar a.active,
  .mobile-tabbar button.active { color: var(--ink); background: var(--canvas); }
  .mobile-tab-icon {
    min-height: 25px;
    display: flex;
    align-items: center;
    font-family: 'Newsreader', serif;
    font-size: 17px;
    font-weight: 600;
    line-height: 1;
  }

  .mobile-more-backdrop {
    position: fixed;
    z-index: 78;
    inset: 0;
    display: block;
    background: rgba(0, 0, 0, .35);
  }
  .mobile-more-sheet {
    position: fixed;
    z-index: 79;
    left: 8px;
    right: 8px;
    bottom: calc(62px + env(safe-area-inset-bottom));
    display: grid;
    gap: 4px;
    padding: 8px 10px 12px;
    border: 1px solid var(--border);
    border-radius: 16px;
    background: var(--card);
    box-shadow: var(--shadow-md);
  }
  .mobile-more-handle {
    width: 38px;
    height: 4px;
    margin: 2px auto 4px;
    border-radius: 99px;
    background: var(--border);
  }
  .mobile-more-title {
    padding: 2px 8px 6px;
    font-family: 'Newsreader', serif;
    font-size: 18px;
    font-weight: 600;
  }
  .mobile-more-sheet a {
    min-height: 54px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 7px 9px;
    border-radius: 10px;
    color: var(--ink);
    text-decoration: none;
  }
  .mobile-more-sheet a:active { background: var(--canvas); }
  .mobile-more-sheet a b {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    border-radius: 9px;
    background: var(--canvas);
    font-family: 'Newsreader', serif;
  }
  .mobile-more-sheet a span { display: grid; font-size: 13px; font-weight: 600; }
  .mobile-more-sheet a small { color: var(--ink-mute); font-size: 10px; font-weight: 400; }
}
</style>
