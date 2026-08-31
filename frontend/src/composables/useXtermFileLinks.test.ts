import { describe, expect, it } from 'vitest'

// Imported, not re-declared: this one is exported by the module that owns it
// (`lib/ossLink.ts`), shared with the markdown renderer, so a copy here could
// drift from the pattern actually shipped — which is how the file matchers
// below are still written, and why they carry the sync warning.
import { S3_URI_RE, setOssConfigured } from '../lib/ossLink'
import { attachXtermFileLinks } from './useXtermFileLinks'

// Re-declare the regexes here for test isolation (mirrors source-of-truth
// in useXtermFileLinks.ts — keep in sync if that file changes).
const FILE_ABS_RE = /(?<![A-Za-z0-9_.\-/~])(?:\/(?!\/)|~\/)[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,10}\b/g
const FILE_BARE_REL_RE = /(?<![A-Za-z0-9_.\-/~:])[A-Za-z0-9_\-]+(?:\/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]{1,10}\b/g
const FILE_DOT_REL_RE = /(?<![A-Za-z0-9_.\-/~:])\.[A-Za-z0-9_\-]+(?:\/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]{1,10}\b/g
const HTTP_URL_RE = /https?:\/\/[A-Za-z0-9._~:/?#@!$&*+;=%\-]+[A-Za-z0-9/#=]/g

function matches(re: RegExp, text: string): string[] {
  const local = new RegExp(re.source, re.flags)
  const out: string[] = []
  let m: RegExpExecArray | null
  while ((m = local.exec(text)) !== null) out.push(m[0])
  return out
}

describe('FILE_ABS_RE', () => {
  it('matches a plain absolute path with extension', () => {
    expect(matches(FILE_ABS_RE, 'see /etc/hosts.txt for details')).toEqual(['/etc/hosts.txt'])
  })

  it('matches ~/ prefixed path', () => {
    expect(matches(FILE_ABS_RE, 'cat ~/.zshrc.local end')).toEqual(['~/.zshrc.local'])
  })

  it('does NOT match inside a relative prose path', () => {
    // The `/csm/api/files.py` fragment should not be snapped up.
    expect(matches(FILE_ABS_RE, 'backend/csm/api/files.py')).toEqual([])
  })

  it('does NOT match inside an http(s):// URL', () => {
    // Regression for the wandb URL bug — `://wandb.ai/foo.png` used to
    // be grabbed as `//wandb.ai/foo.png` (an "absolute path") and sent
    // to preview, which 404'd or hit the wrong redirect path. Still
    // rejected after the `:`-lookbehind → `(?!\/)`-lookahead swap because
    // scheme bodies always begin with a double slash.
    expect(matches(FILE_ABS_RE, 'open https://wandb.ai/foo/img.png here')).toEqual([])
    expect(matches(FILE_ABS_RE, 'http://example.com/x.py')).toEqual([])
  })

  it('matches an absolute path glued to a prose label by a colon', () => {
    // Regression: `新 APK:/data/.../pgf.apk` — the `:` immediately before
    // `/data` (no space) used to fail the lookbehind and make the whole
    // path unclickable. A single-slash label prefix must still resolve.
    expect(
      matches(FILE_ABS_RE, '新 APK:/home/dev/app/pgf-connector-v0.3.3-debug.apk'),
    ).toEqual(['/home/dev/app/pgf-connector-v0.3.3-debug.apk'])
  })
})

describe('FILE_BARE_REL_RE', () => {
  it('matches a bare relative path', () => {
    expect(matches(FILE_BARE_REL_RE, 'edit backend/csm/api/files.py:32')).toEqual([
      'backend/csm/api/files.py',
    ])
  })

  it('does NOT match example.com/foo.html', () => {
    // First-segment forbids `.` so `example` -> can't reach `.com/foo.html`
    // via lookbehind rejection either.
    expect(matches(FILE_BARE_REL_RE, 'see example.com/foo.html')).toEqual([])
  })

  it('does NOT match inside https:// URL body', () => {
    // With `:` in the lookbehind, `host/path.py` in `https://host/path.py`
    // is rejected because the `h` in `host` is preceded by `/` (already
    // in the class) — but the stricter guard is against `.png` tails.
    expect(matches(FILE_BARE_REL_RE, 'go to https://host/path.py now')).toEqual([])
  })
})

describe('FILE_DOT_REL_RE', () => {
  it('matches .claude/skills/foo/SKILL.md', () => {
    expect(matches(FILE_DOT_REL_RE, 'open .claude/skills/pre-merge-check/SKILL.md now')).toEqual([
      '.claude/skills/pre-merge-check/SKILL.md',
    ])
  })

  it('matches .github/workflows/ci.yml', () => {
    expect(matches(FILE_DOT_REL_RE, 'edit .github/workflows/ci.yml')).toEqual([
      '.github/workflows/ci.yml',
    ])
  })

  it('does NOT match .env alone (no subpath)', () => {
    // Dot-rooted matcher requires ≥1 `/` between root and ext, so top
    // level dotfiles like `.env` don't match — that's intentional; those
    // aren't previewable anyway.
    expect(matches(FILE_DOT_REL_RE, 'check .env for secrets')).toEqual([])
  })

  it('does NOT match ./foo.py (that is FILE_REL_RE territory)', () => {
    // `.` then `/` — no wordchar between — so FILE_DOT_REL_RE won't
    // start (first segment needs at least one wordchar after `.`).
    expect(matches(FILE_DOT_REL_RE, './foo.py')).toEqual([])
  })
})

describe('HTTP_URL_RE', () => {
  it('matches a full OSS report https URL', () => {
    // Regression: a rendered OSS report link was dead text because no
    // matcher covered plain http(s):// URLs (only s3:// + file paths).
    const url =
      'https://oss.example.com/example-bucket/sample_pipeline/reports/report_validity_2026-08-23.html'
    expect(matches(HTTP_URL_RE, `see ${url} for results`)).toEqual([url])
  })

  it('drops sentence punctuation glued to the link', () => {
    expect(matches(HTTP_URL_RE, '打开 https://x.com/a.html.')).toEqual(['https://x.com/a.html'])
    expect(matches(HTTP_URL_RE, '见 (https://x.com/path/a) 里')).toEqual(['https://x.com/path/a'])
  })

  it('keeps port / query / fragment', () => {
    expect(matches(HTTP_URL_RE, 'http://host:8000/x?b=1&c=2#frag next')).toEqual([
      'http://host:8000/x?b=1&c=2#frag',
    ])
  })

  it('does NOT match s3:// (that is S3_RE territory)', () => {
    expect(matches(HTTP_URL_RE, 's3://bucket/key.html')).toEqual([])
  })
})

describe('S3_URI_RE (shared with the chat renderer)', () => {
  it('stops before sentence punctuation glued to the URI', () => {
    // The terminal used to carry its own `/s3:\/\/[A-Za-z0-9_.\-/]+/`, which
    // ended in a plain `+` and swallowed a trailing period. The backend
    // allowlist accepts `.` in a key, so it 302'd happily to a URL that 404s on
    // OSS — a failure visible only at the last hop.
    expect(matches(S3_URI_RE, 'see s3://b/k.html.')).toEqual(['s3://b/k.html'])
    expect(matches(S3_URI_RE, '报告 s3://b/k.html。')).toEqual(['s3://b/k.html'])
    expect(matches(S3_URI_RE, '(s3://b/k.html)')).toEqual(['s3://b/k.html'])
  })

  it('matches the real report URI whole', () => {
    const uri = 's3://example-bucket/sample_pipeline/reports/report_scene_2026-08-27.html'
    expect(matches(S3_URI_RE, `产物在 ${uri} 里`)).toEqual([uri])
  })

  it('ignores a scheme with no key', () => {
    expect(matches(S3_URI_RE, '裸 s3:// 没内容')).toEqual([])
  })

  it('stops at a query string the backend would reject anyway', () => {
    expect(matches(S3_URI_RE, 's3://b/k.html?sig=x')).toEqual(['s3://b/k.html'])
  })
})

describe('s3:// matcher registration is gated on the server config', () => {
  // The regex blocks above test `S3_URI_RE` in isolation, which stays valid
  // whether or not the matcher is ever registered. Without this block the
  // gate added in `attachXtermFileLinks` would have zero coverage — and a
  // terminal that underlines `s3://` on an install with no OSS host sends the
  // user to a 503. Counting providers is enough: it isolates the one `if`.
  function fakeTerm() {
    let count = 0
    const term = {
      registerLinkProvider: () => {
        count += 1
        return { dispose() {} }
      },
      buffer: { active: { getLine: () => null } },
    }
    return { term, providers: () => count }
  }

  it('registers one fewer provider when no OSS host is configured', () => {
    setOssConfigured(true)
    const open = fakeTerm()
    attachXtermFileLinks(open.term as never, 'sid-1')

    setOssConfigured(false)
    const shut = fakeTerm()
    attachXtermFileLinks(shut.term as never, 'sid-1')

    expect(open.providers() - shut.providers()).toBe(1)
  })

  it('leaves the file and http matchers registered either way', () => {
    // The gate must remove exactly the s3 provider, not disable the composable.
    setOssConfigured(false)
    const shut = fakeTerm()
    attachXtermFileLinks(shut.term as never, 'sid-1')
    expect(shut.providers()).toBeGreaterThan(0)
  })
})
