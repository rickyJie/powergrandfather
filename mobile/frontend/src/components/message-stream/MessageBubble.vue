<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from "vue";
import { showToast } from "vant";
import { renderMarkdown } from "@/lib/markdown";

interface Props {
  role: "user" | "assistant" | "system";
  text: string;
  ts?: string;
  /**
   * The stream marks the newest assistant bubble as latest. The latest bubble
   * is never auto-folded (you're reading it) and shows a trailing blink cursor
   * as a lightweight "this is the live output" cue (Phase E: message-level
   * reveal — real token streaming isn't reachable from the JSONL tail).
   */
  isLatest?: boolean;
  /**
   * Only for role "system", and only when what it reports actually failed.
   * The default is deliberately quiet: these notes are overwhelmingly routine
   * ("Agent X finished"), and colouring every one of them as an alert is what
   * would bury the rare one that needs attention.
   */
  level?: "warning";
}
const props = withDefaults(defineProps<Props>(), { isLatest: false });

const isUser = computed(() => props.role === "user");
const isSystem = computed(() => props.role === "system");

// Assistant / system text is rendered as full markdown (Shiki-highlighted code).
// User text stays verbatim in a compact card — no rendering, no surprises.
const html = ref("");
const contentEl = ref<HTMLElement | null>(null);

// ── Long-message folding ────────────────────────────────────────────────────
// Chat turns get long; clamp anything over the threshold and let the user open
// it. `collapsible` is set by measuring the rendered height post-render.
const COLLAPSE_PX = 340;
const userCardEl = ref<HTMLElement | null>(null);
const collapsible = ref(false);
const collapsed = ref(true);
// True once the user manually toggled — so we don't re-fold under them when a
// newer message arrives and this one stops being "latest".
const userToggled = ref(false);

// Char threshold below which a message can't possibly exceed the clamp height —
// skip the DOM measurement entirely so a long history load doesn't do N forced
// reflows (reading scrollHeight is a layout-flush). Only genuinely long messages
// pay for a measure.
const MEASURE_MIN_CHARS = 700;

async function measureCollapse() {
  if ((props.text?.length ?? 0) < MEASURE_MIN_CHARS) {
    collapsible.value = false;
    return;
  }
  await nextTick();
  const el = isUser.value ? userCardEl.value : contentEl.value;
  if (!el) return;
  const tall = el.scrollHeight > COLLAPSE_PX + 40;
  collapsible.value = tall;
  if (!userToggled.value) collapsed.value = tall && !props.isLatest;
}

// The blink cursor rides the newest assistant output only (not user/system,
// not folded, not history).
const showCursor = computed(
  () => props.isLatest && props.role === "assistant" && !collapsed.value
);

function toggleFold() {
  userToggled.value = true;
  collapsed.value = !collapsed.value;
}

async function render() {
  if (!isUser.value) {
    html.value = await renderMarkdown(props.text ?? "");
    await nextTick();
    enhanceCodeBlocks();
  }
  await measureCollapse();
}

// Give every Shiki code block a ChatGPT-style header: language label + copy.
function enhanceCodeBlocks() {
  const root = contentEl.value;
  if (!root) return;
  root.querySelectorAll<HTMLPreElement>("pre.shiki").forEach((pre) => {
    if (pre.dataset.enhanced) return;
    pre.dataset.enhanced = "1";
    const lang = pre.getAttribute("data-lang") || "code";

    const bar = document.createElement("div");
    bar.className = "code-bar";
    const label = document.createElement("span");
    label.className = "code-lang";
    label.textContent = lang;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy";
    btn.textContent = "Copy";
    btn.addEventListener("click", () => {
      const code = pre.querySelector("code")?.textContent ?? "";
      navigator.clipboard.writeText(code).then(
        () => {
          btn.textContent = "Copied";
          window.setTimeout(() => (btn.textContent = "Copy"), 1200);
        },
        () => showToast({ message: "Copy failed", type: "fail", duration: 1200 })
      );
    });
    bar.appendChild(label);
    bar.appendChild(btn);

    // Wrap <pre> in a figure so the header sits flush above it.
    const wrap = document.createElement("div");
    wrap.className = "code-wrap";
    pre.parentElement?.insertBefore(wrap, pre);
    wrap.appendChild(bar);
    wrap.appendChild(pre);
  });
}

const relTime = computed(() => {
  if (!props.ts) return "";
  const then = new Date(props.ts).getTime();
  if (isNaN(then)) return props.ts;
  const diff = Date.now() - then;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return new Date(then).toLocaleDateString();
});

async function copyMessage() {
  try {
    await navigator.clipboard.writeText(props.text);
    showToast({ message: "Copied", type: "success", duration: 1000 });
  } catch {
    showToast({ message: "Copy failed", type: "fail", duration: 1200 });
  }
}

// Long-press (500ms) to copy the whole message.
let pressTimer: number | null = null;
function pressStart() {
  clearPress();
  pressTimer = window.setTimeout(copyMessage, 500);
}
function clearPress() {
  if (pressTimer !== null) {
    clearTimeout(pressTimer);
    pressTimer = null;
  }
}

watch(() => props.text, render);
watch(() => props.isLatest, measureCollapse);
onMounted(render);
</script>

<template>
  <!-- User: compact right-aligned card. Assistant/system: full-width document. -->
  <div
    :class="[
      'msg-row',
      isUser ? 'is-user' : isSystem ? 'is-system' : 'is-assistant',
      { 'is-warning': isSystem && level === 'warning' },
    ]"
    @contextmenu.prevent="copyMessage"
  >
    <div class="msg-role" v-if="!isUser">
      {{ isSystem ? "system" : "assistant" }}
    </div>
    <div
      v-if="isUser"
      ref="userCardEl"
      class="user-card"
      :class="{ clamped: collapsible && collapsed }"
      @touchstart.passive="pressStart"
      @touchend="clearPress"
      @touchmove.passive="clearPress"
      @touchcancel="clearPress"
    >
      <pre class="user-text">{{ text }}</pre>
    </div>
    <div
      v-else
      ref="contentEl"
      class="md-body"
      :class="{ clamped: collapsible && collapsed, cursor: showCursor, reveal: isLatest }"
      v-html="html"
      @touchstart.passive="pressStart"
      @touchend="clearPress"
      @touchmove.passive="clearPress"
      @touchcancel="clearPress"
    />
    <button
      v-if="collapsible"
      type="button"
      class="fold-toggle"
      @click="toggleFold"
    >
      {{ collapsed ? "Show all" : "Collapse" }}
    </button>
    <div v-if="ts" class="msg-meta">{{ relTime }}</div>
  </div>
</template>

<style scoped>
.msg-row {
  display: flex;
  flex-direction: column;
  padding: 10px 16px 14px;
}
.msg-row.is-user {
  align-items: flex-end;
}
.msg-role {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: lowercase;
  color: var(--text-soft);
  margin-bottom: 4px;
}
/* Routine system notes stay quiet — a subagent finishing is not an alert, and
   painting all of them amber left the one that actually failed looking exactly
   like the 96% that didn't. `--warning` is now reserved for that case. */
.is-system .msg-role {
  color: var(--text-faint);
}
.is-system.is-warning .msg-role {
  color: var(--warning);
  font-weight: 600;
}

/* User: sage bubble, right-aligned. Layers by colour — no border/shadow. */
.user-card {
  max-width: 85%;
  background: var(--secondary-container);
  color: var(--on-secondary-container);
  border-radius: 18px 18px 6px 18px;
  padding: 10px 14px;
}
.user-text {
  margin: 0;
  font-family: var(--font-sans);
  font-size: var(--fs-body);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Assistant: full-width rendered document. */
.md-body {
  font-family: var(--font-sans);
  font-size: var(--fs-body);
  line-height: 1.55;
  color: var(--text);
  word-break: break-word;
}
.is-system .md-body {
  color: var(--text-soft);
  font-size: 14px;
}
.msg-meta {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-faint);
}
.is-user .msg-meta {
  text-align: right;
}

/* --- Rendered-markdown element styling (deep, since v-html) --- */
.md-body :deep(p) {
  margin: 0 0 10px;
}
.md-body :deep(p:last-child) {
  margin-bottom: 0;
}
.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3) {
  font-family: var(--font-sans);
  line-height: 1.3;
  margin: 16px 0 8px;
  font-weight: 700;
}
.md-body :deep(h1) {
  font-size: 18px;
}
.md-body :deep(h2) {
  font-size: 16px;
}
.md-body :deep(h3) {
  font-size: 15px;
  font-weight: 600;
}
.md-body :deep(ul),
.md-body :deep(ol) {
  margin: 0 0 10px;
  padding-left: 22px;
}
.md-body :deep(li) {
  margin: 3px 0;
}
.md-body :deep(a) {
  color: var(--primary);
  text-decoration: underline;
  text-underline-offset: 2px;
  word-break: break-all;
}
.md-body :deep(blockquote) {
  margin: 0 0 10px;
  padding: 2px 12px;
  border-left: 3px solid var(--primary-soft);
  color: var(--text-soft);
  font-style: italic;
}
.md-body :deep(:not(pre) > code) {
  font-family: var(--font-mono);
  font-size: 13px;
  background: var(--surface-1);
  color: var(--text);
  border: 1px solid var(--outline-soft);
  padding: 1px 5px;
  border-radius: 5px;
}
.md-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0 0 10px;
  font-size: 13px;
  display: block;
  overflow-x: auto;
}
.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid var(--outline-soft);
  padding: 5px 9px;
  text-align: left;
}
.md-body :deep(th) {
  background: var(--surface-1);
  font-weight: 600;
}

/* --- Code block: header (lang + copy) over a Shiki <pre> --- */
.md-body :deep(.code-wrap) {
  margin: 10px 0;
  border: 1px solid var(--outline-soft);
  border-radius: 10px;
  overflow: hidden;
  background: var(--surface-1);
}
.md-body :deep(.code-bar) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 10px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--outline-soft);
}
.md-body :deep(.code-lang) {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-soft);
  text-transform: lowercase;
}
.md-body :deep(.code-copy) {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-soft);
  background: transparent;
  border: 1px solid var(--outline);
  border-radius: 6px;
  padding: 2px 8px;
  cursor: pointer;
}
.md-body :deep(.code-copy:active) {
  background: var(--surface-2);
}
.md-body :deep(pre.shiki) {
  margin: 0;
  padding: 12px;
  overflow-x: auto;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12.5px;
  line-height: 1.5;
}
.md-body :deep(pre.shiki code) {
  font-family: inherit;
}

/* ── Long-message folding: clamp tall content behind a fade + toggle ──────── */
.md-body.clamped,
.user-card.clamped {
  max-height: 340px;
  overflow: hidden;
  position: relative;
}
.md-body.clamped::after,
.user-card.clamped::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 64px;
  pointer-events: none;
}
/* Fade matches each surface: assistant bleeds into the stream, user into sage. */
.md-body.clamped::after {
  background: linear-gradient(to bottom, transparent, var(--bg));
}
.user-card.clamped::after {
  background: linear-gradient(to bottom, transparent, var(--secondary-container));
}
.fold-toggle {
  align-self: flex-start;
  margin-top: 6px;
  padding: 3px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--primary);
  background: var(--surface-1);
  border: 1px solid var(--outline-soft);
  border-radius: var(--radius-pill);
  cursor: pointer;
}
.is-user .fold-toggle {
  align-self: flex-end;
}
.fold-toggle:active {
  background: var(--surface-2);
}

/* ── Message-level reveal: fade in the latest turn + a trailing blink cursor
   (real token streaming isn't reachable — see Phase E feasibility note). ──── */
.md-body.reveal {
  animation: msg-fade 0.3s ease;
}
@keyframes msg-fade {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.md-body.cursor::after {
  content: "";
  display: inline-block;
  width: 2px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--primary);
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
</style>
