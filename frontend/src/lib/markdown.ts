// Markdown renderer with syntax highlighting + sanitization.
// Used by ChatMessage to render assistant/user text. Returns a sanitized HTML
// string safe to drop into v-html.
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/common'
import DOMPurify from 'dompurify'
import 'highlight.js/styles/github.css'
import { installOssLinks } from './ossLink'

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(str: string, lang: string): string {
    const langClass = `language-${lang || 'plaintext'}`
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code class="${langClass}">${hljs.highlight(str, { language: lang, ignoreIllegals: true }).value}</code></pre>`
      } catch { /* fall through */ }
    }
    // Unlabeled code: render as escaped plaintext. `hljs.highlightAuto` scans
    // EVERY registered grammar for each block — a heavy synchronous long-task
    // that, across a big transcript's worth of unlabeled fences, was a top
    // cause of the chat's first-paint jank. Not worth it for auto-detect.
    return `<pre class="hljs"><code class="${langClass}">${md.utils.escapeHtml(str)}</code></pre>`
  },
})

// Linkifies `s3://` URIs to the OSS redirect endpoint, and opens external
// links in a new tab (relative / same-origin stay in place). Owns the
// `link_open` rule; see ossLink.ts for why the rewrite lives there rather than
// in linkify's `normalize` hook.
installOssLinks(md)

export function renderMarkdown(text: string): string {
  const raw = md.render(text)
  return DOMPurify.sanitize(raw, {
    ADD_ATTR: ['target', 'rel'],
  })
}
