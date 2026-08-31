import { describe, expect, it } from 'vitest'
import { NAVIGATION_FALLBACK_DENYLIST } from './pwa-navigation'

/**
 * The desktop service worker is scoped to `/`, so its app-shell fallback
 * answers EVERY document load on the origin from cache. Anything the origin
 * serves that isn't the desktop SPA must be denylisted, or the browser gets
 * the desktop `index.html` for it — without the request ever reaching the
 * server, so nothing is logged and the only symptom is the desktop router
 * rendering "Page not found" on a URL it was never meant to handle.
 *
 * These assertions are the guard, because nothing else fails when the list is
 * wrong: the build succeeds, the tests pass, the server is healthy, and the
 * breakage only appears on a device that has visited `/` at least once.
 */

const denied = (path: string) =>
  NAVIGATION_FALLBACK_DENYLIST.some((re) => re.test(path))

describe('navigateFallbackDenylist', () => {
  // Every prefix this origin serves that is NOT the desktop SPA.
  it.each([
    ['/api/sessions', 'backend API'],
    ['/proxy/8080/', 'dev proxy'],
    ['/metrics', 'prometheus endpoint'],
    ['/docs', "FastAPI's interactive docs — a real navigation"],
    ['/redoc', 'FastAPI redoc'],
    ['/m/', 'mobile SPA root'],
    ['/m/s/ff25306e-e36f-417b-9c28-46c7b844cf87', 'mobile session deep link'],
    ['/m/notifications', 'mobile route'],
  ])('%s is left to the server (%s)', (path) => {
    expect(denied(path)).toBe(true)
  })

  // Desktop SPA routes MUST still be served from the app shell — denying
  // these would break offline start and cost a network round-trip per
  // navigation, which is the entire reason navigateFallback exists.
  it.each([
    ['/'],
    ['/sessions'],
    ['/sessions/ff25306e-e36f-417b-9c28-46c7b844cf87'],
    ['/agents'],
    ['/agents/conversations/abc'],
    ['/automation'],
    ['/tokens'],
    ['/settings'],
  ])('%s still uses the app shell', (path) => {
    expect(denied(path)).toBe(false)
  })

  it('does not deny a desktop route that merely starts with a denied word', () => {
    // `/metrics` is denied but a hypothetical `/metricsboard` SPA route is
    // not — the anchors matter, and a sloppy /^\/m/ would swallow every
    // desktop route beginning with "m".
    expect(denied('/mission-control')).toBe(false)
    expect(denied('/models')).toBe(false)
  })
})
