/**
 * Rule: nothing calls `fetch` on an /api path directly.
 *
 * The backend requires `X-CSM-Client: 1` on /api/* (main.py
 * `RequireClientHeaderMiddleware`) and answers 400 without it. The axios
 * interceptor in `client.ts` attaches it; a raw `window.fetch` does not, and
 * the caller just sees an opaque `HTTP 400` with no clue what is wrong.
 *
 * This isn't defensive: the workflows drawer, its Reload YAML button and both
 * Schedule dialogs shipped broken this way — four independent copies of one
 * oversight, while the same bug had already been found and fixed by hand in
 * `components/token/AgentAlertsPanel.vue`. Fixing the four sites again would
 * only have reset the clock, so the rule is enforced here instead.
 *
 * Use `apiFetch` from `api/client.ts`, or one of the typed `api/*.ts` clients.
 *
 * Sources are pulled through `import.meta.glob` rather than `node:fs` because
 * this frontend has no `@types/node` and `tsconfig.json` pins `types` to the
 * vite client typings — `vue-tsc --noEmit` fails on the import otherwise.
 */
import { describe, expect, it } from 'vitest'

const sources = import.meta.glob('../**/*.{ts,vue}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

/**
 * `lib/perfLog.ts` is installed BY `api/client.ts`, so it cannot import back
 * from it without a cycle. It sets the header inline instead — see the comment
 * at its call site.
 */
const ALLOWED = ['/lib/perfLog.ts']

/** `fetch('/api…')` but not `apiFetch(…)` / `http.fetch(…)`. */
const RAW_API_FETCH = /(?<![\w.])fetch\(\s*['"`]\/api/

describe('every /api call carries the X-CSM-Client header', () => {
  it('has no raw fetch() against an /api path', () => {
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.ts'))
      .filter(([path]) => !ALLOWED.some((a) => path.endsWith(a)))
      .filter(([, src]) => RAW_API_FETCH.test(src))
      .map(([path]) => path)

    expect(offenders, `use apiFetch() from api/client.ts in: ${offenders.join(', ')}`)
      .toEqual([])
  })

  it('scans a meaningful number of files', () => {
    // Guards the guard: a glob that silently matched nothing would pass above.
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })
})
