import type { IDisposable, Terminal } from '@xterm/xterm'
import { filesApi } from '../api/files'
import { isOssConfigured, S3_URI_RE } from '../lib/ossLink'

/**
 * Register clickable-link providers on an xterm.js Terminal for
 * (a) absolute file paths + ~/ prefixed paths, and (b) s3:// URIs.
 * Both open in a fresh browser tab so the running session stays put.
 *
 * Returns an IDisposable-like object; caller must invoke .dispose()
 * before tearing the terminal down to unregister the providers.
 *
 * Regex notes:
 *   Absolute path: `/` followed by 1+ path chars ending in a `.ext` of
 *   1..10 chars. Requires an extension so we don't over-match plain
 *   `/usr/bin` style non-file tokens.
 *
 *   ~/ path: same shape but rooted at `~/`. Server-side expands `~`
 *   via os.path.expanduser using uvicorn's HOME.
 *
 *   s3://: shared with the chat renderer — see `lib/ossLink.ts`, which
 *   owns the pattern so a URI resolves the same way from either surface.
 *
 * All three matchers deliberately stop at whitespace / typical shell
 * delimiters so surrounding punctuation in claude's prose doesn't get
 * swept into the URL.
 */
// Left-boundary lookbehind so `/` inside a printed relative path like
// `backend/csm/api/files.py` doesn't get grabbed as the start of an
// absolute path.
//
// URL schemes (`https://x/foo.png`, `s3://…`) are excluded via the
// `(?!\/)` lookahead on the leading slash — a scheme's body always starts
// with a DOUBLE slash (`://`), so rejecting a `/` immediately followed by
// another `/` drops `//x/foo.png` without needing to blacklist `:` in the
// lookbehind. Blacklisting `:` was too broad: it also killed legit
// absolute paths glued to a prose label with no space, e.g.
// `new APK:/data/.../pgf.apk` — the `:` before `/data` made the whole path
// unclickable. Single-slash label prefixes now resolve; `://` schemes don't.
const FILE_ABS_RE = /(?<![A-Za-z0-9_.\-/~])(?:\/(?!\/)|~\/)[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,10}\b/g
// Relative-path matcher: requires an explicit `./` prefix.
const FILE_REL_RE = /\.\/[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,10}\b/g
// Bare relative paths like `backend/csm/api/files.py:32` printed in
// prose. Requires ≥1 `/` and a real .ext. First segment forbids `.` so
// `example.com/foo.bar` won't start at `example`. `:` excluded from the
// lookbehind so URL bodies (`https://host/x.py`) don't have `host/x.py`
// grabbed as a bare relative path either.
const FILE_BARE_REL_RE = /(?<![A-Za-z0-9_.\-/~:])[A-Za-z0-9_\-]+(?:\/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]{1,10}\b/g
// Dotfile-rooted relative paths like `.claude/skills/foo/SKILL.md`. The
// bare-rel matcher can't handle these because its first segment is
// `[A-Za-z0-9_\-]+` (no `.`) and the lookbehind also excludes `.`, so a
// leading dot is rejected on both ends. This matcher anchors on `.` +
// wordchars + `/…ext` and stays sid-gated for cwd anchoring.
const FILE_DOT_REL_RE = /(?<![A-Za-z0-9_.\-/~:])\.[A-Za-z0-9_\-]+(?:\/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]{1,10}\b/g
// Plain http(s) URLs printed in the terminal (e.g. a rendered OSS report link
// `https://oss.example.com/bucket/key.html`, or any web link a tool emits).
// These have no matcher otherwise, so they render as dead text. Opened
// directly in a new tab — they're already absolute, no backend redirect. The
// trailing char class forces the match to END on a URL-ish char so sentence
// punctuation glued to the link (`…report.html.` / `(…/a)`) isn't swallowed.
const HTTP_URL_RE = /https?:\/\/[A-Za-z0-9._~:/?#@!$&*+;=%\-]+[A-Za-z0-9/#=]/g

interface AnyLinkProvider {
  provideLinks(
    line: number,
    callback: (links: unknown[] | undefined) => void,
  ): void
}

// xterm.js typedefs don't expose the link provider surface cleanly
// enough for TS strict mode; the runtime shape is stable, so cast
// through `any` at the registration boundary.
export function attachXtermFileLinks(term: Terminal, sid?: string | null): IDisposable {
  const disposables: IDisposable[] = []
  disposables.push(registerMatcher(term, FILE_ABS_RE, (match) => {
    window.open(filesApi.previewUrl(match, sid), '_blank', 'noopener,noreferrer')
  }))
  if (sid) {
    disposables.push(registerMatcher(term, FILE_REL_RE, (match) => {
      window.open(filesApi.previewUrl(match, sid), '_blank', 'noopener,noreferrer')
    }))
    disposables.push(registerMatcher(term, FILE_BARE_REL_RE, (match) => {
      window.open(filesApi.previewUrl(match, sid), '_blank', 'noopener,noreferrer')
    }))
    disposables.push(registerMatcher(term, FILE_DOT_REL_RE, (match) => {
      window.open(filesApi.previewUrl(match, sid), '_blank', 'noopener,noreferrer')
    }))
  }
  // Only when the server actually has an OSS host — otherwise the redirect
  // endpoint 503s and the underline in the terminal is a lie. Terminals are
  // created after boot, so the gate is settled by the time this runs; if it
  // somehow is not, the URI stays plain text, which is the old behaviour.
  if (isOssConfigured()) {
    disposables.push(registerMatcher(term, S3_URI_RE, (match) => {
      window.open(filesApi.ossRedirectUrl(match), '_blank', 'noopener,noreferrer')
    }))
  }
  // Plain http(s) links — open the absolute URL directly.
  disposables.push(registerMatcher(term, HTTP_URL_RE, (match) => {
    window.open(match, '_blank', 'noopener,noreferrer')
  }))
  return {
    dispose() {
      for (const d of disposables) {
        try { d.dispose() } catch (_) { /* ignore */ }
      }
    },
  }
}

function registerMatcher(
  term: Terminal,
  re: RegExp,
  onClick: (match: string) => void,
): IDisposable {
  const provider: AnyLinkProvider = {
    provideLinks(y, callback) {
      const line = term.buffer.active.getLine(y - 1)
      if (!line) { callback(undefined); return }
      const text = line.translateToString(true)
      // Fresh regex per call — global flag carries `lastIndex` state.
      const localRe = new RegExp(re.source, re.flags)
      const links: unknown[] = []
      let m: RegExpExecArray | null
      while ((m = localRe.exec(text)) !== null) {
        const startCol = m.index + 1  // xterm ranges are 1-indexed
        const endCol = m.index + m[0].length
        links.push({
          range: {
            start: { x: startCol, y },
            end: { x: endCol, y },
          },
          text: m[0],
          activate: (_ev: MouseEvent, uri: string) => onClick(uri),
          hover: undefined,
          leave: undefined,
        })
      }
      callback(links.length ? links : undefined)
    },
  }
  return (term as unknown as { registerLinkProvider(p: AnyLinkProvider): IDisposable })
    .registerLinkProvider(provider)
}
