<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { renderMarkdown } from '../lib/markdown'

const props = defineProps<{
  role: 'user' | 'assistant' | 'system'
  text: string
  ts: string
}>()

/**
 * A streaming reply arrives as blocks, and `AgentChat.appendText` MERGES each
 * one into this bubble's `text` (`last.text += …`). As a plain computed, that
 * meant re-parsing the markdown and re-running highlight.js over the entire
 * message-so-far on every block — the work grows with the square of the
 * reply's length, and it lands on the main thread mid-stream.
 *
 * Throttle it: render at most every MIN_RENDER_GAP_MS while the text is still
 * growing. A trailing run is always scheduled, and it reads `props.text` when
 * it fires, so the final state is never the stale one — which is the failure
 * mode a naive `setTimeout` guard would introduce here.
 */
const MIN_RENDER_GAP_MS = 120
const html = ref('')
let pendingRender: number | null = null
// -Infinity, not 0: the FIRST render must never be throttled. With 0 a bubble
// mounted within MIN_RENDER_GAP_MS of page load (i.e. every bubble in a history
// replay) would sit empty until the trailing timer fired.
let lastRenderAt = -Infinity

function renderNow() {
  html.value = renderMarkdown(props.text)
  lastRenderAt = performance.now()
}

watch(
  () => props.text,
  () => {
    // A trailing render is already queued — it will pick up this text too.
    if (pendingRender !== null) return
    const since = performance.now() - lastRenderAt
    if (since >= MIN_RENDER_GAP_MS) {
      renderNow()
      return
    }
    pendingRender = window.setTimeout(() => {
      pendingRender = null
      renderNow()
    }, MIN_RENDER_GAP_MS - since)
  },
  { immediate: true },
)

onUnmounted(() => {
  if (pendingRender !== null) clearTimeout(pendingRender)
})

const copied = ref(false)
async function copyText() {
  try {
    await navigator.clipboard.writeText(props.text)
    copied.value = true
    setTimeout(() => { copied.value = false }, 1500)
  } catch {
    // Fallback: select range; ignore failure silently.
  }
}

// Intercept clicks on any code block: a Copy button on each <pre> would
// require post-render DOM walking. Instead we attach a single delegated
// handler that copies the nearest <pre> on shift-click.
function onContentClick(e: MouseEvent) {
  if (!e.shiftKey) return
  const target = e.target as HTMLElement
  const pre = target.closest('pre')
  if (!pre) return
  const txt = pre.textContent || ''
  navigator.clipboard.writeText(txt).catch(() => {})
}
</script>

<template>
  <div class="chat-msg" :class="role">
    <div class="bubble">
      <div
        v-if="role !== 'system'"
        class="markdown"
        v-html="html"
        @click="onContentClick"
      />
      <div v-else class="system-text">{{ text }}</div>
      <div v-if="role !== 'system'" class="actions">
        <button class="act-btn" @click="copyText" :title="copied ? 'Copied' : 'Copy all'">
          {{ copied ? '✓' : '⧉' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-msg { display: flex; margin: 10px 0; }
.chat-msg.user { justify-content: flex-end; }
.chat-msg.assistant { justify-content: flex-start; }
.chat-msg.system { justify-content: center; }

.bubble {
  position: relative;
  max-width: 82%;
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 13px;
  line-height: 1.6;
  word-wrap: break-word;
}
.chat-msg.user .bubble {
  background: var(--pastel-blue-bg, #e3edf7);
  color: var(--ink);
  border: 1px solid var(--pastel-blue-fg, #4a6584);
  border-bottom-right-radius: 4px;
}
.chat-msg.assistant .bubble {
  background: var(--card);
  color: var(--ink);
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
}
.chat-msg.system .bubble {
  background: var(--canvas);
  color: var(--ink-mute);
  font-size: 11px;
  font-style: italic;
  border-left: 3px solid var(--ink-mute);
  border-radius: 4px;
  padding: 6px 10px;
  max-width: 60%;
}
.system-text { white-space: pre-wrap; }

/* Action bar: hidden by default, fades in on hover. */
.actions {
  position: absolute;
  top: -10px;
  right: 8px;
  display: flex; gap: 4px;
  opacity: 0;
  transition: opacity 150ms;
}
.bubble:hover .actions { opacity: 1; }
.chat-msg.user .actions { right: auto; left: 8px; }
.act-btn {
  font-size: 11px;
  padding: 2px 6px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
  color: var(--ink);
}
.act-btn:hover { background: var(--canvas); }

/* Markdown internals (unscoped via :deep would be cleaner; using global is fine
   because we host the styles in this component's scoped slot via :deep). */
.markdown :deep(p) { margin: 0.4em 0; }
.markdown :deep(p:first-child) { margin-top: 0; }
.markdown :deep(p:last-child) { margin-bottom: 0; }
.markdown :deep(ul),
.markdown :deep(ol) { margin: 0.4em 0; padding-left: 1.4em; }
.markdown :deep(li) { margin: 0.15em 0; }
.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4) {
  margin: 0.7em 0 0.4em;
  font-family: 'Newsreader', serif;
  font-weight: 500;
  line-height: 1.3;
}
.markdown :deep(h1) { font-size: 1.3em; }
.markdown :deep(h2) { font-size: 1.18em; }
.markdown :deep(h3) { font-size: 1.08em; }
.markdown :deep(h4) { font-size: 1em; }
.markdown :deep(a) { color: var(--pastel-blue-fg); text-decoration: underline; }
.markdown :deep(code) {
  font-family: 'Geist Mono', monospace;
  background: var(--canvas);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.92em;
}
.markdown :deep(pre) {
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  margin: 6px 0;
  overflow: auto;
  font-size: 12px;
  cursor: text;
  position: relative;
}
.markdown :deep(pre code) {
  background: transparent;
  padding: 0;
  font-size: inherit;
  display: block;
  white-space: pre;
}
.markdown :deep(pre::after) {
  content: 'Shift-click to copy';
  position: absolute;
  top: 4px; right: 6px;
  font-size: 9px;
  color: var(--ink-mute);
  opacity: 0;
  transition: opacity 150ms;
  pointer-events: none;
}
.markdown :deep(pre:hover::after) { opacity: 0.7; }
.markdown :deep(blockquote) {
  border-left: 3px solid var(--border);
  padding-left: 10px;
  margin: 0.4em 0;
  color: var(--ink-mute);
}
.markdown :deep(table) {
  border-collapse: collapse;
  margin: 0.5em 0;
}
.markdown :deep(th),
.markdown :deep(td) {
  border: 1px solid var(--border);
  padding: 4px 8px;
  font-size: 12px;
}
.markdown :deep(th) { background: var(--canvas); }

/* Within the dark user bubble, code chips should match. */
.chat-msg.user .markdown :deep(code) {
  background: rgba(255, 255, 255, 0.4);
}
.chat-msg.user .markdown :deep(pre) {
  background: rgba(255, 255, 255, 0.5);
}
</style>
