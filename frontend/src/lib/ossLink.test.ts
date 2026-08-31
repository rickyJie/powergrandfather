/**
 * `s3://` URIs in chat messages.
 *
 * The backend has 302'd `/api/files/oss-redirect?uri=…` for a long time and the
 * terminal wired it up, but chat never did: markdown-it's linkify only knows
 * http/https/ftp/mailto, so an agent printing `s3://bucket/key` produced dead
 * text and the endpoint was never called. These pin the rendering contract on
 * both sides — the href must resolve, and the reader must still see the URI
 * they recognise, not the redirect plumbing.
 */
import MarkdownIt from 'markdown-it'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { renderMarkdown } from './markdown'
import {
  installOssLinks,
  isOssConfigured,
  OSS_REDIRECT_PATH,
  opensInNewTab,
  ossRedirectUrl,
  setOssConfigured,
} from './ossLink'

// The gate ships CLOSED (see ossLink.ts). Every assertion below about links
// actually rendering therefore has to open it first — and the `gate closed`
// block at the bottom is what pins the shipped default.
beforeEach(() => setOssConfigured(true))
afterEach(() => setOssConfigured(false))

const REAL = 's3://example-bucket/sample_pipeline/reports/report_scene_2026-08-27.html'

/**
 * Isolated instance — exercises `installOssLinks` alone, so a failure points at
 * the rule rather than at anything else in the pipeline.
 *
 * NOT sufficient on its own: it skips DOMPurify, which is what actually ships
 * the HTML to the DOM. The `renderMarkdown` block at the bottom covers that.
 */
function render(text: string): string {
  const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
  installOssLinks(md as never)
  return md.render(text).trim()
}

describe('ossRedirectUrl', () => {
  it('encodes the whole URI into the query so slashes survive', () => {
    expect(ossRedirectUrl('s3://b/k.html')).toBe(
      '/api/files/oss-redirect?uri=s3%3A%2F%2Fb%2Fk.html',
    )
  })
})

describe('s3:// in rendered markdown', () => {
  it('links a bare URI to the redirect endpoint', () => {
    const html = render(`报告在 ${REAL} 里`)
    expect(html).toContain(`href="${ossRedirectUrl(REAL)}"`)
  })

  it('still DISPLAYS the s3:// URI, not the redirect plumbing', () => {
    // Rewriting in linkify's `normalize` hook makes markdown-it derive the
    // anchor text from the rewritten URL, so the reader sees
    // `/api/files/oss-redirect?uri=s3%3A%2F%2F…`. Hence the render-time rule.
    const html = render(`报告在 ${REAL} 里`)
    expect(html).toContain(`>${REAL}</a>`)
    expect(html).not.toContain('>/api/files/oss-redirect')
  })

  it('leaves sentence punctuation outside the link', () => {
    // Two failure modes: the character is swallowed into the key (400 from the
    // backend allowlist), or it vanishes from the output entirely — which is
    // what the `normalize` approach did.
    for (const [text, tail] of [
      ['末尾 s3://b/k.html。', '。'],
      ['see s3://b/k.html.', '.'],
      ['(s3://b/k.html)', ')'],
    ] as const) {
      const html = render(text)
      expect(html, text).toContain(`href="${ossRedirectUrl('s3://b/k.html')}"`)
      expect(html, text).toContain(`</a>${tail}`)
    }
  })

  it('opens in a new tab without leaking the opener', () => {
    const html = render(REAL)
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('rewrites an explicit [label](s3://…) link too', () => {
    // These never reach linkify — only the render-time rule catches them.
    const html = render('[报告](s3://b/k.html)')
    expect(html).toContain(`href="${ossRedirectUrl('s3://b/k.html')}"`)
    expect(html).toContain('>报告</a>')
  })

  it('leaves a bare scheme with no key as plain text', () => {
    expect(render('裸 s3:// 后面没有东西')).not.toContain('<a')
  })

  it('does not linkify inside code spans or fences', () => {
    expect(render('`s3://b/k.html`')).not.toContain('<a')
    expect(render('```\ns3://b/k.html\n```')).not.toContain('<a')
  })

  it('leaves http(s) links working as before', () => {
    const html = render('https://oss.example.com/x/y.html')
    expect(html).toContain('href="https://oss.example.com/x/y.html"')
    expect(html).toContain('target="_blank"')
  })

  it('only linkifies keys the backend allowlist accepts', () => {
    // `_OSS_KEY_RE` in api/files.py is `^[A-Za-z0-9._\-/]+$`; a `?` in the key
    // is rejected there with a 400. Stopping the match before it means a link
    // that renders is a link that resolves.
    const html = render('s3://b/k.html?sig=abc')
    expect(html).toContain(`href="${ossRedirectUrl('s3://b/k.html')}"`)
    expect(html).not.toContain('sig%3Dabc')
  })
})

describe('opensInNewTab', () => {
  it('covers web links and our own redirect, nothing else', () => {
    expect(opensInNewTab('https://x/y')).toBe(true)
    expect(opensInNewTab('http://x/y')).toBe(true)
    expect(opensInNewTab(ossRedirectUrl('s3://b/k'))).toBe(true)
    expect(opensInNewTab('/sessions/abc')).toBe(false)
    expect(opensInNewTab('#anchor')).toBe(false)
  })
})

describe('through the real renderMarkdown (DOMPurify included)', () => {
  // The block above builds its own MarkdownIt, so it proves the RULE works and
  // nothing else. These go through the exported function that ChatMessage.vue
  // actually calls — the singleton `md` (is the rule even installed on it?) and
  // DOMPurify (does a relative href survive sanitisation?). A regression in
  // either would look exactly like "the feature doesn't work", while every
  // isolated test stayed green.
  it('keeps the rewritten href after sanitisation', () => {
    const html = renderMarkdown(`报告在 ${REAL} 里`)
    expect(html).toContain(`href="${ossRedirectUrl(REAL)}"`)
  })

  it('keeps the s3:// URI as the visible text after sanitisation', () => {
    expect(renderMarkdown(REAL)).toContain(`>${REAL}</a>`)
  })

  it('keeps target/rel, which DOMPurify strips unless allow-listed', () => {
    // `ADD_ATTR: ['target', 'rel']` in markdown.ts is what makes this pass;
    // dropping it silently reverts every link to same-tab navigation.
    const html = renderMarkdown(REAL)
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('still renders ordinary markdown and http links unchanged', () => {
    // Guards against the rule replacement having broken the pre-existing
    // link_open behaviour it took over.
    const html = renderMarkdown('# T\n\nsee https://oss.example.com/x/y.html and `code`')
    expect(html).toContain('<h1>T</h1>')
    expect(html).toContain('<code>code</code>')
    expect(html).toContain('href="https://oss.example.com/x/y.html"')
    expect(html).toContain('target="_blank"')
  })

  it('leaves a same-origin relative link in the current tab', () => {
    // Pre-existing behaviour: only external links get target=_blank. The new
    // rule widened the predicate to include our own redirect path, and must
    // not have widened it to everything.
    const html = renderMarkdown('[会话](/sessions/abc)')
    expect(html).toContain('href="/sessions/abc"')
    expect(html).not.toContain('target="_blank"')
  })

  it('does not linkify a javascript: URI', () => {
    // markdown-it's validateLink rejects the href outright, so no anchor is
    // produced and the source stays inert literal text — asserting the string
    // is absent would be wrong, since the escaped text still contains it.
    // Unchanged by this work, but the new rule runs on every link_open token,
    // so pin that it did not accidentally resurrect one.
    const html = renderMarkdown('[x](javascript:alert(1))')
    expect(html).not.toContain('<a')
    expect(html).not.toContain('href=')
  })
})

describe('gate closed (no OSS host configured on the server)', () => {
  // The reason this block exists: `/api/files/oss-redirect` answers 503 when
  // `settings.oss_base_url` is empty, which is the case on every install that
  // has no OSS host. Rendering a clickable link there promises a jump the
  // server will refuse — worse than the plain text it used to be.
  beforeEach(() => setOssConfigured(false))

  it('ships closed by default', () => {
    // Guards the default itself, not just the setter: a future refactor that
    // flips the initialiser to `true` would make every install below leak
    // dead links, and nothing else in the suite would notice.
    expect(isOssConfigured()).toBe(false)
  })

  it('leaves a bare s3:// URI as plain text', () => {
    const html = render(`see ${REAL}`)
    expect(html).not.toContain('<a')
    expect(html).toContain(REAL)
  })

  it('does not rewrite an explicit [label](s3://…) into the redirect', () => {
    const html = render(`[report](${REAL})`)
    expect(html).not.toContain(OSS_REDIRECT_PATH)
  })

  it('still leaves http(s) links alone', () => {
    // The gate must not take unrelated linkification down with it.
    const html = render('see https://example.com/x')
    expect(html).toContain('href="https://example.com/x"')
  })
})
