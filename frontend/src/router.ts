import { createRouter, createWebHistory, RouteRecordRaw } from 'vue-router'
import { setBaseTitle } from './composables/useTabBadge'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/sessions' },
  { path: '/sessions', component: () => import('./views/Sessions.vue'), meta: { title: 'Sessions' } },
  { path: '/sessions/:sid', component: () => import('./views/Sessions.vue'), meta: { title: 'Sessions' } },
  { path: '/agents', component: () => import('./views/AgentDeck.vue'), meta: { title: 'Agents' } },
  { path: '/agents/conversations/:cid', component: () => import('./views/AgentChat.vue'), meta: { title: 'Agent Chat' } },
  { path: '/automation', component: () => import('./views/AutomationRuns.vue'), meta: { title: 'Automation' } },
  { path: '/tokens', component: () => import('./views/Tokens.vue'), meta: { title: 'Tokens' } },
  { path: '/budgets', component: () => import('./views/Budgets.vue'), meta: { title: 'Budgets' } },
  { path: '/sync', redirect: { path: '/settings', query: { section: 'sync' } } },
  { path: '/settings', component: () => import('./views/Settings.vue'), meta: { title: 'Settings' } },
  { path: '/:pathMatch(.*)*', component: () => import('./views/NotFound.vue'), meta: { title: 'Not Found' } },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

const BASE_TITLE = 'PowerGrandFather · Claude Session Manager'

router.afterEach((to) => {
  const t = (to.meta?.title as string | undefined) || ''
  // `document.title` is owned by useTabBadge — feed it the unbadged base and
  // let the composable re-compose "(N) <base>" when there are unread notifs.
  setBaseTitle(t ? `${t} · PowerGrandFather` : BASE_TITLE)
})

// Stale-deploy safety net. Every route above uses `() => import(...)` which
// Vite splits into a hash-named chunk (e.g. Automation-abc123.js). When the
// frontend is redeployed, hashes change and the OLD file gets removed from
// dist/. A user with the old index.js still in memory (or from browser cache)
// will try to fetch the old chunk on the next lazy-loaded route click — the
// server returns 404, `import()` rejects, vue-router silently swallows it and
// the click appears to do nothing. This is exactly the "clicking A / B / P
// does nothing" symptom after a rebuild.
//
// Detect that failure mode and force a full reload so the user picks up the
// current index.js (which references the current chunk hashes). We stash a
// sentinel in sessionStorage to break out of an infinite reload loop if the
// module is genuinely broken (server misconfig, network down) rather than
// stale.
router.onError((err, to) => {
  const msg = String((err as any)?.message || '')
  const isChunkLoadFailure =
    msg.includes('Failed to fetch dynamically imported module') ||
    msg.includes('Importing a module script failed') ||
    msg.includes('error loading dynamically imported module') ||
    msg.includes('Loading chunk')
  if (!isChunkLoadFailure) return
  const KEY = 'csm.stale-chunk-reload'
  if (sessionStorage.getItem(KEY)) {
    // Second failure in a row — don't spin. Let the user see something's off.
    console.error('Chunk load still failing after reload:', err)
    return
  }
  sessionStorage.setItem(KEY, '1')
  // Reload straight to the intended path so the user doesn't lose their nav
  // target during the fix. `.fullPath` includes query / hash.
  window.location.href = to.fullPath
})
// Clear the sentinel on any successful navigation — next stale-deploy scenario
// gets a fresh chance to auto-heal.
router.afterEach(() => {
  sessionStorage.removeItem('csm.stale-chunk-reload')
})
