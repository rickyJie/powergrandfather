// Custom Service Worker handlers, appended to the workbox-generated SW via
// vite-plugin-pwa `workbox.importScripts`. Handles clicks on the OS-level
// notifications fired by src/lib/desktopNotify.ts: focus the app window that
// raised them (routing it to the target session via postMessage) or, if none
// is open, open a fresh one at the target URL.
//
// The tricky part is *which* window to focus. `clients.matchAll()` returns the
// installed-PWA window and every stale same-origin browser tab together, in an
// order we don't control, and a Client carries nothing that tells them apart —
// so naively focusing clients[0] lands the user in a browser tab instead of the
// app. Two signals fix that, both stamped on the notification by the page:
//   - `data.clientId`  — the exact window that fired it (preferred).
//   - `data.standalone`— whether that window was the installed app, so we know
//                        never to settle for a browser tab.
// As a backstop, every page reports its display mode on load (see
// registerDisplayModeWithSW) and we persist that by client id in the Cache API.
// In-memory alone is not enough: the SW is routinely killed between the
// showNotification() call and the click, so the map is cold exactly when it
// matters.

const MODE_CACHE = 'csm-client-display-mode-v1'
const MODE_PREFIX = '/__csm/client-mode/'

// clientId -> boolean (standalone). Warm cache in front of MODE_CACHE.
const modeMemo = new Map()

function modeKey(clientId) {
  return MODE_PREFIX + encodeURIComponent(clientId)
}

async function rememberMode(clientId, standalone) {
  modeMemo.set(clientId, standalone)
  try {
    const cache = await caches.open(MODE_CACHE)
    await cache.put(new Request(modeKey(clientId)), new Response(standalone ? '1' : '0'))
  } catch (_) { /* storage unavailable — memo still covers this SW's lifetime */ }
}

async function recallMode(clientId) {
  if (modeMemo.has(clientId)) return modeMemo.get(clientId)
  try {
    const cache = await caches.open(MODE_CACHE)
    const hit = await cache.match(modeKey(clientId))
    if (!hit) return null
    const standalone = (await hit.text()) === '1'
    modeMemo.set(clientId, standalone)
    return standalone
  } catch (_) {
    return null
  }
}

/** Drop entries for windows that no longer exist, so the cache can't grow. */
async function pruneModes(liveIds) {
  try {
    const cache = await caches.open(MODE_CACHE)
    for (const req of await cache.keys()) {
      const id = decodeURIComponent(new URL(req.url).pathname.slice(MODE_PREFIX.length))
      if (!liveIds.has(id)) {
        await cache.delete(req)
        modeMemo.delete(id)
      }
    }
  } catch (_) { /* ignore */ }
}

self.addEventListener('message', (event) => {
  const data = event.data
  if (!data || data.type !== 'csm-display-mode') return
  const clientId = event.source && event.source.id
  if (!clientId) return
  // Reply with the id so the page can stamp it onto its notifications.
  const port = event.ports && event.ports[0]
  if (port) {
    try { port.postMessage({ clientId }) } catch (_) { /* ignore */ }
  }
  event.waitUntil(rememberMode(clientId, Boolean(data.standalone)))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data || {}
  const url = typeof data.url === 'string' ? data.url : '/sessions'
  // Only true for notifications raised from the installed app; those must never
  // resolve to a browser tab.
  const wantStandalone = data.standalone === true
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    })
    const modes = await Promise.all(clients.map((c) => recallMode(c.id)))
    // matchAll is ordered most-recently-focused first; filtering preserves it.
    const standalone = clients.filter((_, i) => modes[i] === true)

    const firing = typeof data.clientId === 'string'
      ? clients.find((c) => c.id === data.clientId)
      : undefined
    // Fall back to any app window, then — only for browser-raised
    // notifications — to whatever window is around.
    const target = firing || standalone[0] || (wantStandalone ? undefined : clients[0])

    if (target && 'focus' in target) {
      try { await target.focus() } catch (_) { /* ignore */ }
      // The SPA is already loaded — route it in place rather than reloading.
      try { target.postMessage({ type: 'csm-notif-navigate', url }) } catch (_) { /* ignore */ }
    } else if (self.clients.openWindow) {
      // No app window left to focus (discarded / reloaded). The URL is inside
      // the manifest scope, so an installed PWA captures it and launches the
      // app window; uninstalled, it opens a normal tab.
      await self.clients.openWindow(url)
    }

    await pruneModes(new Set(clients.map((c) => c.id)))
  })())
})
