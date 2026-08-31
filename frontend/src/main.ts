import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { initPerfConsole } from './lib/perfLog'
import { setOssConfigured } from './lib/ossLink'
import { apiFetch } from './api/client'
import './style.css'

// Expose window.__csmPerf for latency inspection (see lib/perfLog.ts). The
// axios interceptors are installed in api/client.ts on import.
initPerfConsole('web')

const bootstrapUrl = new URL(window.location.href)
const accessToken = bootstrapUrl.searchParams.get('token')
if (accessToken) {
  // The backend has already exchanged this query value for an HttpOnly
  // SameSite cookie on the index response. Frontend only scrubs the URL.
  bootstrapUrl.searchParams.delete('token')
  window.history.replaceState(
    {},
    '',
    bootstrapUrl.pathname + bootstrapUrl.search + bootstrapUrl.hash,
  )
}

// Self-heal for devices still running a service worker built before `/m/`
// joined `navigateFallbackDenylist` (see ../pwa-navigation.ts).
//
// THIS bundle running on a `/m/` URL can only mean one thing: a stale SW
// answered a mobile navigation out of the desktop app shell. The desktop
// router has no such route, so the phone just shows "Page not found". Those
// devices can't recover on their own either — `registerType: 'prompt'` waits
// for a click on a refresh prompt that lives in the desktop UI, which is not
// what the phone is looking at.
//
// Drop the stale worker and re-request the URL, which now reaches the server
// and gets the real mobile app. The desktop PWA re-registers a current
// worker on its next visit to `/`.
//
// Runs BEFORE the router is mounted, deliberately: doing it in a navigation
// guard means aborting the navigation, and vue-router then reverts the URL —
// which cancels the very reload we're trying to perform.
const MOBILE_SHELL_HEAL_KEY = 'csm.mobile-shell-heal'
if (
  /^\/m(?:\/|$)/.test(window.location.pathname) &&
  !sessionStorage.getItem(MOBILE_SHELL_HEAL_KEY)
) {
  // One attempt only. If the reload lands back here the cause is something
  // other than a stale worker, and looping would hide it.
  sessionStorage.setItem(MOBILE_SHELL_HEAL_KEY, '1')
  const target = window.location.href
  const done = () => window.location.replace(target)
  navigator.serviceWorker
    ?.getRegistrations?.()
    .then((regs) => Promise.all(regs.map((r) => r.unregister())))
    .then(done, done) ?? done()
} else {
  const app = createApp(App)
  app.use(createPinia())
  app.use(router)
  app.mount('#app')

  // Server feature gates. Fetched after mount so a slow or failed request
  // never delays first paint — `s3://` linkification simply stays off until
  // this resolves, which is the safe direction.
  apiFetch('/api/version')
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (d) setOssConfigured(Boolean(d.oss_configured))
    })
    .catch(() => {
      /* offline / backend down — leave the gate closed */
    })
}
