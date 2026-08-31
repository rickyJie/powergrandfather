/**
 * Paths the desktop service worker must NOT answer from its app-shell cache.
 *
 * The SW is registered at scope `/`, so its `navigateFallback` intercepts
 * EVERY top-level document load on the origin — not just desktop SPA routes.
 * Anything the origin serves that is not the desktop app has to be listed
 * here or the SW hands the browser the desktop `index.html` instead, without
 * ever touching the network.
 *
 * That failure is silent and confusing: the request never reaches the server
 * (so nothing shows up in the access log), the desktop SPA boots on a URL it
 * has no route for, and the user gets "Page not found · The route you tried
 * doesn't exist." Reproduced for `/m/`: after one visit to the desktop
 * console, a cold load of `/m/s/<sid>` on the same origin came back
 * `from_service_worker=True` carrying `/assets/index-<desktop>.js`, and the
 * phone showed the desktop 404 instead of the mobile chat.
 *
 * Extracted from vite.config.ts so it can be asserted against the set of
 * prefixes the backend actually co-serves — the list is only correct
 * relative to that, and the whole point is that getting it wrong produces
 * no error anywhere.
 */
export const NAVIGATION_FALLBACK_DENYLIST: RegExp[] = [
  // Backend routers.
  /^\/api\//,
  /^\/proxy\//,
  /^\/metrics(?:\/|$)/,
  // FastAPI's interactive docs are real navigations on this origin.
  /^\/docs(?:\/|$)/,
  /^\/redoc(?:\/|$)/,
  // The mobile SPA, co-served by the same process (mobile/backend_patch).
  // Its own router owns everything below /m/.
  /^\/m(?:\/|$)/,
]
