/**
 * Rule: the modules listed below stay byte-identical between the desktop and
 * mobile packages.
 *
 * The two SPAs are deliberately separate npm packages with no shared build (and
 * today, different markdown-it majors), so a handful of small modules are
 * duplicated rather than extracted. That is a real cost. On 2026-08-24 the
 * desktop watchdog moved off its elapsed-time deadline onto ping/pong pairing
 * and mobile was left behind — still closing healthy sockets on every throttled
 * tick, which is what the "phone keeps disconnecting" reports were. The commit
 * that made the fix even said it extracted the rule "so the two call sites
 * can't drift apart again"; it stopped the drift inside the desktop app and did
 * nothing for mobile.
 *
 * A comment asking people to edit both is not a mechanism. This is.
 *
 * The list is explicit, not "every file present in both": `markdown.ts` and
 * `perfLog.ts` also share a name across the packages and are SUPPOSED to differ
 * (hljs vs Shiki, different transports). Adding a module here is a deliberate
 * statement that its two copies must match.
 *
 * `ossLink.ts` is deliberately NOT here: the OSS host is unreachable from the
 * phone (it reaches CSM through an SSH tunnel and nothing else), so the redirect
 * is desktop-only. A clickable link that cannot resolve is worse than plain
 * text — the copy was removed rather than kept in sync.
 */
import { describe, expect, it } from 'vitest'

// Adding a module to the contract means adding it to DUPLICATED — nothing
// else. The globs stay broad on purpose: Vite parses them at build time and
// accepts only literals (pattern AND options), so a narrowed pattern would have
// to be hand-edited in two places, and a single-element brace expansion
// silently resolves to nothing.
const desktop = import.meta.glob('./*.ts', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>
const mobile = import.meta.glob('../../../mobile/frontend/src/lib/*.ts', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const DUPLICATED = ['wsLiveness.ts']

const basename = (p: string) => p.slice(p.lastIndexOf('/') + 1)
const byName = (m: Record<string, string>) =>
  new Map(Object.entries(m).map(([p, src]) => [basename(p), src]))

describe('duplicated modules have not diverged', () => {
  it('resolved every module on both sides', () => {
    // Guards the guard: a glob that matched nothing, or a module that has since
    // been deleted from one side, would make every comparison below trivially
    // pass. This is what caught `ossLink.ts` when the mobile copy was removed.
    const missing = DUPLICATED.flatMap((name) => [
      ...(byName(desktop).has(name) ? [] : [`frontend/src/lib/${name}`]),
      ...(byName(mobile).has(name) ? [] : [`mobile/frontend/src/lib/${name}`]),
    ])
    expect(missing, 'listed in DUPLICATED but not present').toEqual([])
  })

  it.each(DUPLICATED)('%s is byte-identical across both packages', (name) => {
    const d = byName(desktop).get(name)
    const m = byName(mobile).get(name)
    expect(
      m,
      `mobile/frontend/src/lib/${name} has drifted from frontend/src/lib/${name} — edit both, or neither`,
    ).toBe(d)
  })
})
