// ChatGPT-web-style message rendering: full markdown via markdown-it, code
// blocks syntax-highlighted with Shiki (VS Code grammars, dual light/dark
// theme), output XSS-sanitised with DOMPurify.
//
// Safety model (defence in depth):
//   1. markdown-it runs with `html: false`, so any raw HTML in the message text
//      is escaped — a message can never inject markup.
//   2. markdown-it's default `validateLink` already blocks javascript:/vbscript:
//      /data: URLs.
//   3. DOMPurify is a final belt-and-suspenders pass over the produced HTML.
// The Shiki-generated markup is our OWN trusted output; only the message text
// is untrusted, and (1)+(2) neutralise it before Shiki ever sees a code fence.

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import { createHighlighterCore } from "shiki/core";

// Default color is the light theme (inline styles); the dark theme is emitted
// as `--shiki-dark*` CSS custom properties. `html.dark` CSS (see styles) flips
// to them, so highlighting follows the app's light/dark mode with no re-render.
const THEMES = { light: "github-light", dark: "github-dark" } as const;

// Per-language grammar loaders. Vite code-splits each into its own chunk; we
// import ONLY the ones a message actually uses (see ensureLangs), so opening a
// chat no longer eagerly downloads all ~23 grammars + the big ones (cpp ~780K).
// `default` typed loose (any) so the shiki module shape flows into
// createHighlighterCore / loadLanguage without a per-call cast.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const LANG_LOADERS: Record<string, () => Promise<{ default: any }>> = {
  javascript: () => import("shiki/langs/javascript.mjs"),
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  python: () => import("shiki/langs/python.mjs"),
  bash: () => import("shiki/langs/bash.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  vue: () => import("shiki/langs/vue.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  java: () => import("shiki/langs/java.mjs"),
  c: () => import("shiki/langs/c.mjs"),
  cpp: () => import("shiki/langs/cpp.mjs"),
  diff: () => import("shiki/langs/diff.mjs"),
  docker: () => import("shiki/langs/docker.mjs"),
};

// Fence aliases → canonical loader key.
const ALIASES: Record<string, string> = {
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  js: "javascript",
  ts: "typescript",
  py: "python",
  yml: "yaml",
  md: "markdown",
  rs: "rust",
  golang: "go",
  "c++": "cpp",
  cc: "cpp",
  h: "cpp",
  hpp: "cpp",
  dockerfile: "docker",
};

// Small high-frequency set loaded up front so the common path (bash/json/python
// /ts/js/diff) renders without a per-message grammar fetch. Everything else is
// pulled on demand the first time a message uses it.
const EAGER_LANGS = ["bash", "json", "python", "typescript", "javascript", "diff", "markdown"];

function canonical(lang: string): string {
  const l = lang.toLowerCase();
  return ALIASES[l] ?? l;
}

type HL = Awaited<ReturnType<typeof createHighlighterCore>>;
let hlPromise: Promise<HL> | null = null;
const inflight = new Map<string, Promise<void>>();

function ensureHighlighter(): Promise<HL> {
  if (!hlPromise) {
    hlPromise = (async () => {
      const { createOnigurumaEngine } = await import("shiki/engine/oniguruma");
      const [light, dark, ...eager] = await Promise.all([
        import("shiki/themes/github-light.mjs"),
        import("shiki/themes/github-dark.mjs"),
        ...EAGER_LANGS.map((l) => LANG_LOADERS[l]()),
      ]);
      return createHighlighterCore({
        themes: [light.default, dark.default],
        langs: eager.map((m) => m.default),
        engine: createOnigurumaEngine(import("shiki/wasm")),
      });
    })();
  }
  return hlPromise;
}

// Load (once) any not-yet-loaded grammars the given fence languages need.
async function ensureLangs(hl: HL, langs: Iterable<string>): Promise<void> {
  const loaded = new Set(hl.getLoadedLanguages());
  const jobs: Promise<void>[] = [];
  for (const raw of langs) {
    const lang = canonical(raw);
    if (loaded.has(lang) || !LANG_LOADERS[lang]) continue;
    let job = inflight.get(lang);
    if (!job) {
      job = LANG_LOADERS[lang]()
        .then((m) => hl.loadLanguage(m.default as never))
        .then(() => void inflight.delete(lang))
        .catch(() => void inflight.delete(lang));
      inflight.set(lang, job);
    }
    jobs.push(job);
  }
  if (jobs.length) await Promise.all(jobs);
}

// Scan fenced code blocks for their declared language, so we can preload just
// those grammars before rendering.
const FENCE_RE = /^[ \t]*(?:`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]+)/gm;
function scanFenceLangs(text: string): Set<string> {
  const out = new Set<string>();
  let m: RegExpExecArray | null;
  FENCE_RE.lastIndex = 0;
  while ((m = FENCE_RE.exec(text)) !== null) out.add(m[1]);
  return out;
}

type MD = InstanceType<typeof MarkdownIt>;
let md: MD | null = null;

function buildMd(hl: HL): MD {
  return new MarkdownIt({
    html: false,
    linkify: true,
    breaks: true,
    highlight(code, lang): string {
      const language =
        lang && hl.getLoadedLanguages().includes(canonical(lang))
          ? canonical(lang)
          : "text";
      try {
        // Returning a full <pre> makes markdown-it skip its own wrapper.
        // Stamp the language onto the <pre> so the component can render a
        // header (label + copy button); `language` is from a fixed whitelist.
        const out = hl.codeToHtml(code, { lang: language, themes: THEMES });
        return out.replace("<pre ", `<pre data-lang="${language}" `);
      } catch {
        return ""; // fall back to markdown-it's escaped <pre><code>
      }
    },
  });
}

let hookInstalled = false;
function installLinkHook() {
  if (hookInstalled) return;
  hookInstalled = true;
  // Open every link in a new tab, severing the opener.
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
}

/**
 * Render assistant message markdown to sanitised HTML. Async because Shiki's
 * highlighter (WASM + grammars) loads lazily, and this message's specific
 * languages are pulled on demand before rendering.
 */
export async function renderMarkdown(text: string): Promise<string> {
  installLinkHook();
  const hl = await ensureHighlighter();
  await ensureLangs(hl, scanFenceLangs(text ?? ""));
  if (!md) md = buildMd(hl);
  const raw = md.render(text ?? "");
  // Keep Shiki's inline styles + CSS custom props (--shiki-dark*) intact.
  return DOMPurify.sanitize(raw, { ADD_ATTR: ["style", "class"] });
}

/** Warm the highlighter ahead of first render (e.g. on chat mount). */
export function preloadHighlighter(): void {
  void ensureHighlighter();
}
