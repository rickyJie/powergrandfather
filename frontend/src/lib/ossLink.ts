// Desktop only, deliberately. The phone reaches CSM through an SSH tunnel and
// nothing else, so the OSS host is unreachable from it — a link that
// renders as clickable and then goes nowhere is worse than plain text. Do not
// mirror this into `mobile/frontend/`.
//
// Why this exists: agents print `s3://bucket/key` URIs constantly, and the
// backend has had `GET /api/files/oss-redirect?uri=…` ready to 302 them to the
// OSS host for a long time. The terminal wired it up (`useXtermFileLinks.ts`),
// but chat messages never did — markdown-it's linkify only knows its built-in
// schemas (http/https/ftp/mailto), so `s3://…` rendered as dead text and the
// redirect endpoint was simply never called.
//
// `S3_URI_RE` is exported so the terminal matcher uses this exact pattern too:
// the same URI must produce the same href whether it was printed to a PTY or
// to a chat bubble.

/**
 * Tail of an `s3://` URI, matched AFTER the `s3:` schema prefix.
 *
 * Character set matches the backend's `_OSS_KEY_RE` allowlist exactly
 * (`api/files.py`), so anything linkified here is something the endpoint will
 * accept — a link that renders is a link that resolves, never a surprise 400.
 *
 * The final `[A-Za-z0-9_\-/]` forces the match to END on a non-dot, so a URI
 * glued to sentence punctuation (`…see s3://b/k.html.`) doesn't swallow the
 * period into the key. Same trick `HTTP_URL_RE` uses in the xterm matcher.
 * A bare `s3://` with no key matches nothing and stays plain text.
 */
const S3_TAIL_RE = /^\/\/[A-Za-z0-9._\-/]*[A-Za-z0-9_\-/]/

/**
 * A whole `s3://` URI, for scanning free text (the xterm link matcher).
 *
 * Same body as `S3_TAIL_RE` so the terminal and the chat renderer agree on
 * where a URI ends. They did not before: the terminal's own copy ended in a
 * plain `+`, so `…see s3://b/k.html.` sent the key `b/k.html.` to the backend
 * — which the allowlist accepts (`.` is legal in a key), producing a 302 to a
 * URL that 404s on OSS. Silent, and only at the very end of the chain.
 */
export const S3_URI_RE = /s3:\/\/[A-Za-z0-9._\-/]*[A-Za-z0-9_\-/]/g

/** Prefix of every href this module produces; also the `target=_blank` gate. */
export const OSS_REDIRECT_PATH = '/api/files/oss-redirect'

/**
 * Whether the server has an OSS host configured (`settings.oss_base_url`).
 *
 * Defaults to FALSE and is raised only by `setOssConfigured` once
 * `GET /api/version` has answered. That default is the whole point: with no
 * OSS host set the redirect endpoint answers 503, so linkifying would render
 * a promise the server cannot keep. Most installs outside the one this was
 * written for have no OSS host at all — for them `s3://` must stay plain
 * text, exactly as it did before the redirect was wired up.
 */
let ossConfigured = false

/** Set from the boot-time `/api/version` fetch; see `main.ts`. */
export function setOssConfigured(value: boolean): void {
  ossConfigured = value
}

/** Read the gate — exported for the terminal matcher, which registers late. */
export function isOssConfigured(): boolean {
  return ossConfigured
}

/** CSM endpoint that 302s an `s3://` URI to the configured OSS host. */
export function ossRedirectUrl(uri: string): string {
  return `${OSS_REDIRECT_PATH}?uri=${encodeURIComponent(uri)}`
}

/** Whether an href is one this module rewrote (or a plain web link). */
export function opensInNewTab(href: string): boolean {
  return /^https?:\/\//i.test(href) || href.startsWith(OSS_REDIRECT_PATH)
}

// markdown-it's own types differ between the two packages' majors, and this
// module must stay byte-identical across them. Structural typing of just the
// two surfaces used keeps that true without importing either package's types.
interface LinkifyCapable {
  linkify: { add(schema: string, rule: { validate(text: string, pos: number): number }): unknown }
  renderer: {
    rules: Record<string, ((...args: never[]) => string) | undefined>
    renderToken(...args: never[]): string
  }
}

/**
 * Teach a markdown-it instance to linkify `s3://` URIs, pointing them at the
 * redirect endpoint while still DISPLAYING the original URI.
 *
 * The href rewrite happens in the `link_open` renderer rule, not in linkify's
 * `normalize` hook. `normalize` looks like the obvious place and is wrong
 * twice: markdown-it derives the anchor's visible text from the (now rewritten)
 * URL, so the reader sees `/api/files/oss-redirect?uri=s3%3A%2F%2F…` instead of
 * the path they recognise — and trailing punctuation after the URI disappears
 * from the output entirely. Both verified against markdown-it 14 and 15.
 *
 * Rewriting at render time also covers explicit `[label](s3://…)` links, which
 * never go through linkify at all.
 */
export function installOssLinks(md: LinkifyCapable): void {
  md.linkify.add('s3:', {
    validate(text: string, pos: number): number {
      // Read the gate per-call, not at install time: `installOssLinks` runs at
      // module init, well before `/api/version` has answered.
      if (!ossConfigured) return 0
      const m = S3_TAIL_RE.exec(text.slice(pos))
      return m ? m[0].length : 0
    },
  })

  const previous = md.renderer.rules.link_open
  md.renderer.rules.link_open = ((
    tokens: { attrGet(n: string): string | null; attrSet(n: string, v: string): void }[],
    idx: number,
    options: never,
    env: never,
    self: never,
  ): string => {
    const href = tokens[idx].attrGet('href') || ''
    // Explicit `[label](s3://…)` never goes through linkify, so it needs the
    // same gate here. Ungated it would keep the raw `s3://` href, which no
    // browser can follow — dead either way, but this way it is not dressed up
    // as a CSM endpoint.
    if (ossConfigured && href.startsWith('s3://')) {
      tokens[idx].attrSet('href', ossRedirectUrl(href))
    }
    if (opensInNewTab(tokens[idx].attrGet('href') || '')) {
      tokens[idx].attrSet('target', '_blank')
      tokens[idx].attrSet('rel', 'noopener noreferrer')
    }
    const render = previous as ((...a: never[]) => string) | undefined
    return render
      ? render(tokens as never, idx as never, options, env, self)
      : (md.renderer.renderToken as (...a: never[]) => string)(
          tokens as never, idx as never, options,
        )
  }) as never
}
