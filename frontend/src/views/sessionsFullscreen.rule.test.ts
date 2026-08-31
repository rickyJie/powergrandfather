/**
 * Rule: a control whose only output is the session sidebar must not be wired
 * straight to sidebar state, because fullscreen hides that sidebar.
 *
 * `.sess-layout.terminal-max .sess-list { display: none }` removes the list but
 * NOT the two toolbars above it. So `@click="filter = 'history'"` left the
 * History tab clickable and completely inert in fullscreen: the tab lit up, the
 * list it was filtering was invisible, and nothing told the user why. Same for
 * the Active / Auto tabs, the search box and the group-by toggles.
 *
 * The fix is not to hide them — it is to honour the intent, since reaching for
 * one of these IS a request to look at the list. Every such control therefore
 * goes through a handler that leaves fullscreen first.
 *
 * Sessions.vue is ~2600 lines with a router, a pinia store, a WebSocket and
 * xterm behind it, so this reads the source rather than mounting the view. It
 * is a structural check, and it earns its keep on the case that actually
 * regrows: someone adds a fourth tab and copies the two-token `filter = '…'`
 * binding from the three above it.
 */
import { describe, expect, it } from 'vitest'

const SRC = Object.values(
  import.meta.glob('./Sessions.vue', { query: '?raw', import: 'default', eager: true }),
)[0] as string

/** Body of the first element carrying every one of `classes`. */
function block(classes: string[], label: string): string {
  const open = new RegExp(
    `<div[^>]*${classes.map((c) => `(?=[^>]*"[^"]*\\b${c}\\b)`).join('')}[^>]*>`,
  )
  const at = SRC.search(open)
  expect(at, `${label}: no element matching ${classes.join(' + ')} — did it move or get renamed?`)
    .toBeGreaterThan(-1)
  const from = SRC.slice(at)
  const end = from.indexOf('</div>')
  return from.slice(0, end)
}

/** Body of a top-level `function name(...) { … }`, brace-matched. */
function fnBody(name: string): string {
  const at = SRC.indexOf(`function ${name}(`)
  expect(at, `${name}() is gone — the fullscreen-aware handlers were removed or renamed`)
    .toBeGreaterThan(-1)
  let depth = 0
  for (let i = SRC.indexOf('{', at); i < SRC.length; i++) {
    if (SRC[i] === '{') depth++
    else if (SRC[i] === '}' && --depth === 0) return SRC.slice(at, i + 1)
  }
  throw new Error(`unbalanced braces in ${name}()`)
}

const clicksIn = (body: string) => [...body.matchAll(/@click="([^"]+)"/g)].map((m) => m[1])

describe('sidebar-only controls stay usable in fullscreen', () => {
  it('every primary-filter tab routes through selectFilterTab', () => {
    const clicks = clicksIn(block(['filter-group', 'primary-filter'], 'primary filter'))
    // Active / Auto / History today. The count assertion is what fails loudly
    // when a tab is added outside the group rather than inside it.
    expect(clicks.length, 'expected the three filter tabs').toBeGreaterThanOrEqual(3)
    for (const handler of clicks) {
      expect(
        handler,
        `filter tab bound to \`${handler}\` — assigning \`filter\` directly is inert in `
        + 'fullscreen; go through selectFilterTab() so the sidebar is revealed',
      ).toMatch(/^selectFilterTab\(/)
    }
  })

  it('every group-by toggle routes through selectGroupBy', () => {
    const groupBy = SRC.slice(SRC.indexOf('title="Group by"'))
    const clicks = clicksIn(groupBy.slice(0, groupBy.indexOf('</div>')))
    expect(clicks.length, 'expected the by-project / by-cwd toggles').toBeGreaterThanOrEqual(2)
    for (const handler of clicks) {
      expect(
        handler,
        `group-by bound to \`${handler}\` — calling setGroupBy() directly only re-sorts a `
        + 'sidebar that fullscreen has hidden; go through selectGroupBy()',
      ).toMatch(/^selectGroupBy\(/)
    }
  })

  it('the search box reveals the sidebar on focus', () => {
    // Search results render in the same hidden list, so typing into it while
    // fullscreen produced no visible result either.
    const input = SRC.slice(SRC.indexOf('class="search-input"'))
    expect(input.slice(0, input.indexOf('/>'))).toContain('@focus="revealSidebar"')
  })

  it('those handlers actually leave fullscreen', () => {
    // Without this the three assertions above only prove indirection exists.
    expect(fnBody('revealSidebar')).toContain('setFullscreen(false)')
    for (const name of ['selectFilterTab', 'selectGroupBy']) {
      expect(fnBody(name), `${name}() must call revealSidebar()`).toContain('revealSidebar()')
    }
  })

  it('losing the active session drops out of fullscreen', () => {
    // The other half of the same trap: fullscreen with no session shows a bare
    // "Select a session." — no list (hidden) and no ⛶ button (it lives inside
    // `v-if="activeSession"`), so only an undiscoverable Esc gets you out.
    // Reached from "← Back to list" and from the archive / purge / kill paths.
    const at = SRC.indexOf('watch(activeSid')
    expect(at, 'the activeSid watcher moved — re-point this check').toBeGreaterThan(-1)
    const watcher = SRC.slice(at, SRC.indexOf('{ immediate: false })', at))
    expect(
      watcher,
      'the activeSid watcher no longer exits fullscreen when sid becomes null',
    ).toContain('setFullscreen(false)')
  })
})
