<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import type {
  SessionNode,
  TranscriptEvent,
  ToolUseStartEvent,
  ToolUseResultEvent,
} from "@/api/ws-events";
import MessageBubble from "./MessageBubble.vue";
import ToolUseBubble from "./ToolUseBubble.vue";
import { preloadHighlighter } from "@/lib/markdown";
import {
  activeNodeIndex,
  followWindow,
  pageWindow,
  slotFraction,
  RAIL_WINDOW,
} from "@/lib/rail";
import { haptic } from "@/stores/ui";

interface Props {
  events: TranscriptEvent[];
  loading?: boolean;
  /** When true, always auto-scroll to bottom on new events (overrides pin). */
  forcePin?: boolean;
  /** Older history exists on the server before what's loaded (show the button). */
  canLoadOlder?: boolean;
  /** An older-page request is in flight. */
  loadingOlder?: boolean;
  /**
   * Every human-typed message in the WHOLE transcript, for the jump rail —
   * including messages that haven't been paged in. Built server-side because
   * `events` is only the loaded tail, and a rail that indexes the tail claims
   * to be a session timeline while covering the last few turns.
   */
  nodes?: SessionNode[];
  /** Index of `events[0]` within the server's full history. */
  offset?: number;
  /** Server-side event count — the denominator for rail positions. */
  total?: number;
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  forcePin: false,
  canLoadOlder: false,
  loadingOlder: false,
  nodes: () => [],
  offset: 0,
  total: 0,
});

const emit = defineEmits<{
  (e: "loadOlder"): void;
  /** Page back far enough that the event at `index` is loaded. */
  (e: "loadUntil", index: number): void;
}>();

const scrollEl = ref<HTMLElement | null>(null);
const contentEl = ref<HTMLElement | null>(null);
const userScrolledUp = ref(false);
const newCount = ref(0);
const showJumpButton = computed(() => userScrolledUp.value && !props.forcePin);

// "Thinking…" while the newest event is a user turn / an in-flight tool call
// (i.e. we're waiting for the assistant to respond).
const isThinking = computed(() => {
  const last = props.events[props.events.length - 1];
  return !!last && (last.type === "user_message" || last.type === "tool_use_start");
});

// Pair up tool_use_start with its tool_use_result by tool_id, then render
// each in the linear order of the tool_use_start.
// Flatten fields into the row so the template avoids TS narrowing tricks
// (vue-tsc rejects `as` casts inside template expressions).
type BubbleRow = {
  key: string;
  kind: "bubble";
  role: "user" | "assistant" | "system";
  text: string;
  ts?: string;
  /** role "user" but not typed by the user — see UserMessageEvent.injected. */
  injected?: boolean;
  /** role "system" reporting something that failed — see SystemNoteEvent. */
  level?: "warning";
};
type ToolRow = {
  key: string;
  kind: "tool";
  tool: string;
  toolId: string;
  input: unknown;
  ts?: string;
  result: { ok: boolean; preview: string } | null;
};
type Row = BubbleRow | ToolRow;

// Stable per-event key, keyed by object IDENTITY (not array index). Index keys
// (`bub-${i}`) broke when older history is prepended: every row's index shifts,
// so Vue reused the wrong component instances (fold/expand state bled between
// messages) and the prepend scroll-anchor was fooled. An event object keeps the
// same reference across re-renders, and a prepend inserts NEW objects while
// leaving existing ones untouched — so this key is invariant under prepend.
let keySeq = 0;
const keyMap = new WeakMap<object, string>();
function stableKey(e: object): string {
  let k = keyMap.get(e);
  if (!k) {
    k = `e${++keySeq}`;
    keyMap.set(e, k);
  }
  return k;
}

const rows = computed<Row[]>(() => {
  const resultsByToolId = new Map<string, ToolUseResultEvent>();
  for (const e of props.events) {
    if (e.type === "tool_use_result") {
      resultsByToolId.set(e.tool_id, e);
    }
  }

  const out: Row[] = [];
  for (let i = 0; i < props.events.length; i++) {
    const e = props.events[i];
    if (e.type === "tool_use_result") continue;
    if (e.type === "tool_use_start") {
      const r = resultsByToolId.get(e.tool_id) ?? null;
      out.push({
        key: `tool-${e.tool_id}`,
        kind: "tool",
        tool: e.tool,
        toolId: e.tool_id,
        input: e.input,
        ts: e.ts,
        result: r ? { ok: r.ok, preview: r.preview } : null,
      });
    } else if (e.type === "user_message") {
      // `injected` = filed under role "user" by the CLI, not typed by the user
      // (a skill preamble, the post-compaction recap, a `claude -p` prompt CSM
      // or a cron job issued). Rendering those in the sage right-aligned card
      // put words in the user's mouth — a token-alert agent's prompt read as
      // something they had just sent. They stay in the transcript, in the
      // muted system style that says "the machine did this".
      out.push({
        key: stableKey(e),
        kind: "bubble",
        role: e.injected ? "system" : "user",
        text: e.text,
        ts: e.ts,
        injected: e.injected,
      });
    } else if (e.type === "assistant_text") {
      out.push({
        key: stableKey(e),
        kind: "bubble",
        role: "assistant",
        text: e.text,
        ts: e.ts,
      });
    } else if (e.type === "text") {
      out.push({ key: stableKey(e), kind: "bubble", role: "assistant", text: e.text });
    } else if (e.type === "system_note") {
      out.push({
        key: stableKey(e),
        kind: "bubble",
        role: "system",
        text: e.text,
        ts: e.ts,
        level: e.level,
      });
    }
  }
  return out;
});

// The newest assistant bubble is the "live" one: never auto-folded, fades in,
// and carries the trailing blink cursor.
const latestAssistantKey = computed<string | null>(() => {
  for (let i = rows.value.length - 1; i >= 0; i--) {
    const r = rows.value[i];
    if (r.kind === "bubble" && r.role === "assistant") return r.key;
  }
  return null;
});

// Right-edge navigator rail: one node per USER message I actually TYPED, so you
// can jump straight to any question you asked. Two kinds of role-"user" text
// are NOT mine and get no node:
//   - slash commands (/compact, /clear, /model …) — control chrome
//   - `injected` records — a skill's preamble, the post-compaction recap, the
//     auto-continue nudge. Claude files these under role "user" (the server
//     drops these from `nodes` before we see them).
// Assistant replies never had nodes.
//
// The index spans the WHOLE session, not the loaded tail. A node whose message
// hasn't been paged in yet still gets a dot — tapping it pages back and then
// jumps, which is the point of a jump rail.
function isSlashCommand(text: string): boolean {
  // Matches "/name" or "/name args" but NOT a path like "/repo/…"
  // (a path has a second "/" where a command has whitespace or end-of-string).
  return /^\/[a-zA-Z][\w-]*(\s|$)/.test(text.trimStart());
}

interface RailNode {
  /** Index within the server's full event array. */
  i: number;
  text: string;
  /** Position in `props.events`, or -1 when this message isn't loaded yet. */
  pos: number;
}

const userNodes = computed<RailNode[]>(() =>
  (props.nodes ?? [])
    .filter((n) => !!n.text.trim() && !isSlashCommand(n.text))
    .map((n) => {
      const p = n.i - props.offset;
      const e = p >= 0 && p < props.events.length ? props.events[p] : undefined;
      // Verify rather than trust the arithmetic: an optimistic echo the JSONL
      // hasn't reconciled yet sits in `events` without being in the server's
      // count, which shifts everything after it by one. Scanning a small
      // neighbourhood re-locks onto the right message instead of scrolling to
      // whatever happens to be adjacent.
      if (e?.type === "user_message") return { i: n.i, text: n.text, pos: p };
      return { i: n.i, text: n.text, pos: relocate(n, p) };
    })
);

/** Nearest user_message to `p` whose text matches the node. -1 if not loaded. */
function relocate(n: SessionNode, p: number): number {
  const ev = props.events;
  const snippet = n.text.trim().slice(0, 40);
  for (let d = 1; d <= 4; d++) {
    for (const q of [p - d, p + d]) {
      const e = q >= 0 && q < ev.length ? ev[q] : undefined;
      if (e?.type === "user_message" && e.text.trim().startsWith(snippet)) return q;
    }
  }
  return -1;
}

/** The subset with a message on screen — the only ones we can measure. */
const loadedNodes = computed(() => userNodes.value.filter((n) => n.pos >= 0));

/** DOM key for a rail node, via the row key of the event it points at. */
function nodeKey(n: RailNode): string | null {
  if (n.pos < 0) return null;
  const e = props.events[n.pos];
  return e ? stableKey(e) : null;
}

// One-line preview of a node's message, for the floating hint on tap.
function nodePreview(text: string): string {
  const t = text.replace(/\s+/g, " ").trim();
  return t.length > 42 ? t.slice(0, 42) + "…" : t;
}
// Floating hint shown briefly when a node is tapped, so anonymous dots reveal
// which message they point at (title= tooltips never fire on touch).
const railHint = ref<{ text: string; top: string } | null>(null);
let hintTimer: number | null = null;
function flashHint(text: string, top: string) {
  railHint.value = { text: nodePreview(text), top };
  if (hintTimer !== null) clearTimeout(hintTimer);
  hintTimer = window.setTimeout(() => (railHint.value = null), 1800);
}

// The rail is a WINDOW over the message list — RAIL_WINDOW evenly spaced dots,
// always in the same places — not a map of the whole session. See lib/rail.ts.
//
// Starts past the end on purpose: `clampWindowStart` pins that to the newest
// messages, which is where the view opens. Starting at 0 would show the oldest
// window for the frame before the first measurement lands, so the rail visibly
// snapped from one end to the other on every mount.
const windowStart = ref(Number.MAX_SAFE_INTEGER);

/** The slice actually on the rail, tagged with each node's global index. */
const visibleNodes = computed(() => {
  const all = userNodes.value;
  if (all.length <= RAIL_WINDOW) return all.map((n, g) => ({ node: n, g }));
  const s = clampWindowStart(windowStart.value);
  return all.slice(s, s + RAIL_WINDOW).map((n, k) => ({ node: n, g: s + k }));
});

function clampWindowStart(s: number): number {
  const max = Math.max(0, userNodes.value.length - RAIL_WINDOW);
  return Math.min(Math.max(s, 0), max);
}

/** More messages than slots — the only case with a window to move or chart. */
const windowed = computed(() => userNodes.value.length > RAIL_WINDOW);
const hasNewerOffscreen = computed(
  () =>
    windowed.value &&
    clampWindowStart(windowStart.value) + RAIL_WINDOW < userNodes.value.length
);
const hasOlderOffscreen = computed(
  () => windowed.value && clampWindowStart(windowStart.value) > 0
);

// Session bar geometry, as percentages of the strip. The thumb has a floor so
// that on a 200-turn session it stays visible while still reading as "you can
// see almost none of this".
const MIN_THUMB_PX = 24;
const thumbStyle = computed(() => {
  const n = userNodes.value.length || 1;
  return {
    top: `${(clampWindowStart(windowStart.value) / n) * 100}%`,
    height: `max(${MIN_THUMB_PX}px, ${(RAIL_WINDOW / n) * 100}%)`,
  };
});
const notchStyle = computed(() => {
  const n = userNodes.value.length || 1;
  return { top: `${(Math.max(activeNodeG.value, 0) / n) * 100}%` };
});

/** Slot `k` as a unitless 0..1 fraction of the slot track.
 *
 *  Unitless, not a percentage: the track is `100% - 48px` (the chevron zones
 *  own the ends), and `<percentage> * <length>` is invalid CSS — only
 *  `<length> * <number>` is. jsdom quietly folds the bad form into something
 *  plausible, so this would have shipped looking fine in tests. */
function nodeTop(k: number): string {
  return String(slotFraction(k, visibleNodes.value.length));
}

// A jump is not just a scroll: the transcript changes height UNDER the
// animation. Long assistant messages mount folded, and Shiki highlights code
// asynchronously — so a landing computed at tap time can be hundreds of pixels
// stale by the time the smooth scroll finishes. Scroll, then re-measure and
// correct without animating a second time.
const JUMP_SETTLE_MS = 420; // past the browser's smooth-scroll animation
const JUMP_TOLERANCE_PX = 8;
// Where a jump puts the message: just under the top edge, not centred.
//
// Centring was wrong twice over. Practically, you jump to a question in order
// to read the answer, and centring spends the top half of the screen on the
// PREVIOUS turn. Mechanically, it also broke the highlight: "which node am I
// on" asks whether a message's top has passed the fold (top edge + slack), and
// a centred message sits ~45% down the viewport — far below it — so the rule
// attributed the position to the previous node and the dot you just pressed
// went dark the moment the scroll settled.
//
// Landing inside the slack keeps the answer to that question the message you
// actually asked for.
const JUMP_TOP_PADDING_PX = 12;
let jumpTimer: number | null = null;

/** How far `el` is from its landing position, in px (0 = landed). */
function alignOffset(el: HTMLElement): number | null {
  const scroller = scrollEl.value;
  if (!scroller) return null;
  const sRect = scroller.getBoundingClientRect();
  const eRect = el.getBoundingClientRect();
  return eRect.top - sRect.top - JUMP_TOP_PADDING_PX;
}

function alignElement(el: HTMLElement, smooth: boolean) {
  const scroller = scrollEl.value;
  const off = alignOffset(el);
  if (!scroller || off === null) return;
  const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  const target = Math.max(0, Math.min(scroller.scrollTop + off, max));
  // jsdom (test env) doesn't implement scrollTo — fall back to scrollTop.
  if (typeof scroller.scrollTo === "function") {
    scroller.scrollTo({ top: target, behavior: smooth ? "smooth" : "auto" });
  } else {
    scroller.scrollTop = target;
  }
}

function cancelPendingJump() {
  if (jumpTimer !== null) {
    clearTimeout(jumpTimer);
    jumpTimer = null;
  }
  pendingJumpG = null;
}

/** Node index we're paging back to reach, or null. Survives across the
 *  history_page round-trips a deep jump needs. */
const awaitingNode = ref<number | null>(null);

function onNodeTap(node: RailNode, g: number, top: string) {
  haptic(8);
  flashHint(node.text, top);
  userScrolledUp.value = true; // we deliberately left the bottom
  cancelPendingJump();
  // The window does NOT move. Re-centring on the tapped dot meant pressing the
  // top dot slid it to the middle — the thing under your finger walked away,
  // which is the one thing a touch control may never do. Reaching messages
  // outside the window is the chevrons' job now, and only theirs.
  followSuppressed = true;

  if (node.pos < 0) {
    // Not paged in yet. Ask for the history that contains it; the watcher
    // below finishes the jump once it lands. Tapping an old message used to
    // do nothing at all here, which read as a dead dot.
    awaitingNode.value = node.i;
    emit("loadUntil", node.i);
    return;
  }
  jumpTo(node);
}

function jumpTo(node: RailNode) {
  const key = nodeKey(node);
  const el = key ? contentEl.value?.querySelector<HTMLElement>(`[data-mk="${key}"]`) : null;
  if (!el || !key) return;

  // Light the dot you pressed straight away. Waiting for the scroll to settle
  // makes a deliberate tap feel unacknowledged.
  //
  // `pendingJumpG` is deliberately NOT cleared when the scroll finishes: you
  // asked to be at this node, so this is where you are until you move
  // yourself. `cancelPendingJump()` on a real gesture (and on jump-to-bottom)
  // hands control back to the geometric rule. Clearing it on a timer instead
  // made the highlight depend on exactly where the scroll happened to land,
  // which is the kind of thing that is right in a test and wrong on a phone
  // whose content is still growing.
  pendingJumpG = userNodes.value.indexOf(node);
  activeNodeG.value = pendingJumpG;
  alignElement(el, true);

  jumpTimer = window.setTimeout(() => {
    jumpTimer = null;
    // The transcript changes height under the animation (folds, async
    // highlighting), so re-measure and correct without animating again.
    const again = contentEl.value?.querySelector<HTMLElement>(`[data-mk="${key}"]`);
    const off = again ? alignOffset(again) : null;
    if (again && off !== null && Math.abs(off) > JUMP_TOLERANCE_PX) {
      alignElement(again, false);
    }
  }, JUMP_SETTLE_MS);
}

// A deep jump needs one or more history pages first. Each arrival re-runs
// this: either the target is now loaded (jump, done) or there is still more
// to page back through (ask again).
watch(
  () => [props.offset, props.events.length] as const,
  async () => {
    const want = awaitingNode.value;
    if (want === null) return;
    const node = userNodes.value.find((n) => n.i === want);
    if (!node) return;
    if (node.pos < 0) {
      // Still above the loaded window. Only keep asking while the server says
      // there IS more — otherwise a node we can never reach would loop.
      if (props.canLoadOlder && !props.loadingOlder) emit("loadUntil", want);
      return;
    }
    awaitingNode.value = null;
    await nextTick();
    jumpTo(node);
  }
);

// Which node is "current" — see `lib/rail.ts` for the rule and why the ends
// are decided before any geometry. This half only measures.
/** Index into `userNodes` of the node currently under the reader, or -1. */
const activeNodeG = ref(-1);
let activeRaf: number | null = null;
// Set while the reader is at a node BECAUSE they tapped it. Held until they
// scroll themselves: measuring instead would strobe the highlight across every
// node the animation flies past, and then hand the answer to wherever the
// scroll happened to land.
let pendingJumpG: number | null = null;

function computeActiveNode(): number {
  const nodes = loadedNodes.value;
  if (!nodes.length) return -1;
  if (pendingJumpG !== null) return pendingJumpG;

  const scroller = scrollEl.value;
  const content = contentEl.value;
  const all = userNodes.value;
  const firstG = all.indexOf(nodes[0]);
  if (!scroller || !content) return firstG;

  const scrollerTop = scroller.getBoundingClientRect().top;
  const globals: number[] = [];
  const tops: number[] = [];
  for (const n of nodes) {
    const key = nodeKey(n);
    const el = key ? content.querySelector<HTMLElement>(`[data-mk="${key}"]`) : null;
    if (!el || !key) continue;
    globals.push(all.indexOf(n));
    tops.push(el.getBoundingClientRect().top - scrollerTop);
  }
  // `atTop` only when there is genuinely nothing above — with older pages still
  // unloaded, the topmost LOADED message is not the start of the session.
  const i = activeNodeIndex({
    tops,
    atTop: scroller.scrollTop <= 0 && !props.canLoadOlder,
    atBottom: nearBottom(),
  });
  return i >= 0 ? globals[i] : firstG;
}

function updateActiveNode() {
  if (activeRaf !== null) return;
  activeRaf = requestAnimationFrame(() => {
    activeRaf = null;
    activeNodeG.value = computeActiveNode();
  });
}

// The window follows the READER — never the finger. `followWindow` moves it the
// minimum needed to keep the active dot off the edge, so scrolling inside the
// window leaves the rail still and crossing its edge slides it one slot.
//
// Suppressed after any deliberate act (a tap, a chevron page) until the user
// scrolls the transcript themselves. Without the flag the rule fires when the
// jump animation lands and drags the window anyway — the same teleport, 300ms
// later and harder to explain.
let followSuppressed = false;
watch([activeNodeG, () => userNodes.value.length], ([g, count]) => {
  if (followSuppressed) return;
  windowStart.value = followWindow(windowStart.value, g, count);
});

/** Chevron: move the window, leave the transcript alone. */
function pageRail(dir: -1 | 1) {
  const next = pageWindow(clampWindowStart(windowStart.value), dir, userNodes.value.length);
  if (next === clampWindowStart(windowStart.value)) return;
  haptic(12);
  followSuppressed = true;
  windowStart.value = next;
}

/** The reader is at a node the window has been paged away from. The chevron
 *  pointing back at them says so — otherwise no dot is lit and the rail looks
 *  like it has lost track of where you are. */
const activeAbove = computed(
  () => activeNodeG.value >= 0 && activeNodeG.value < clampWindowStart(windowStart.value)
);
const activeBelow = computed(
  () =>
    activeNodeG.value >= 0 &&
    activeNodeG.value >= clampWindowStart(windowStart.value) + RAIL_WINDOW
);

function scrollToBottom(smooth = true) {
  const el = scrollEl.value;
  if (!el) return;
  // Going to the bottom supersedes any jump still settling, and is itself a
  // deliberate "put me back with the conversation" — so the window follows again.
  cancelPendingJump();
  followSuppressed = false;
  // jsdom (test env) doesn't implement scrollTo — fall back to scrollTop.
  if (typeof el.scrollTo === "function") {
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  } else {
    el.scrollTop = el.scrollHeight;
  }
  userScrolledUp.value = false;
  newCount.value = 0;
}

function nearBottom(): boolean {
  const el = scrollEl.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}

// `scroll` events fire for BOTH user gestures and reflow (async markdown/Shiki
// growth keeps scrollTop fixed while scrollHeight jumps, which looks like a
// scroll-up). Using them to UNPIN stranded the view mid-transcript on load. So:
// - scroll only ever RE-PINS (when the user lands back at the bottom).
// - only a real gesture (wheel / touch drag) UNPINS.
function onScroll() {
  if (nearBottom()) {
    userScrolledUp.value = false;
    newCount.value = 0;
  }
  updateActiveNode();
}
function onUserScroll() {
  // A real gesture outranks a jump — it both cancels the pending landing
  // correction (yanking the transcript out of the user's hands) and hands the
  // highlight back to the geometric rule.
  const wasPinned = pendingJumpG !== null;
  cancelPendingJump();
  followSuppressed = false;
  if (wasPinned) updateActiveNode();
  if (!nearBottom()) userScrolledUp.value = true;
}

// Auto-scroll on new events unless user has scrolled up. Also anchors the
// viewport across an older-history PREPEND (load-earlier), so the content the
// user is reading doesn't jump when older messages are spliced in above.
let prevFirstKey: string | null = null;
watch(
  () => props.events.length,
  async (n, o) => {
    const el = scrollEl.value;
    const beforeH = el?.scrollHeight ?? 0;
    const beforeTop = el?.scrollTop ?? 0;
    const firstKeyBefore = prevFirstKey;
    await nextTick();
    const firstKeyNow = rows.value[0]?.key ?? null;
    prevFirstKey = firstKeyNow;
    const prepended =
      firstKeyBefore !== null && firstKeyNow !== firstKeyBefore && n > (o ?? 0);
    if (prepended && el) {
      // Keep the same content under the user's eyes: shift scrollTop by the
      // height that was inserted above the viewport.
      el.scrollTop = beforeTop + (el.scrollHeight - beforeH);
      updateActiveNode();
      return;
    }
    if (props.forcePin || !userScrolledUp.value) {
      scrollToBottom();
    } else {
      newCount.value += Math.max(0, n - (o ?? 0));
    }
    updateActiveNode();
  }
);

// Keep pinned to the bottom while the content GROWS — markdown + Shiki render
// asynchronously, so the height when history first lands is smaller than the
// final height; a one-shot scrollToBottom would then land mid-transcript. The
// observer re-snaps on every growth until the user deliberately scrolls up.
let ro: ResizeObserver | null = null;
let roRaf: number | null = null;
onMounted(() => {
  // Warm Shiki (WASM + grammars) so the first assistant code block renders
  // without a highlight-then-repaint flash.
  preloadHighlighter();
  prevFirstKey = rows.value[0]?.key ?? null;
  nextTick(() => {
    scrollToBottom(false);
    updateActiveNode();
  });
  if (contentEl.value && "ResizeObserver" in window) {
    // Coalesce growth callbacks: a batch history load grows the content dozens
    // of times as each bubble's markdown/shiki resolves async — without this,
    // every growth fired its own scrollToBottom, a layout-thrash storm. One rAF
    // snap per frame is enough to stay pinned.
    ro = new ResizeObserver(() => {
      if (roRaf !== null) return;
      roRaf = requestAnimationFrame(() => {
        roRaf = null;
        if (props.forcePin || !userScrolledUp.value) scrollToBottom(false);
      });
    });
    ro.observe(contentEl.value);
  }
});
onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  if (roRaf !== null) {
    cancelAnimationFrame(roRaf);
    roRaf = null;
  }
  if (activeRaf !== null) {
    cancelAnimationFrame(activeRaf);
    activeRaf = null;
  }
  if (hintTimer !== null) {
    clearTimeout(hintTimer);
    hintTimer = null;
  }
  cancelPendingJump();
});
</script>

<template>
  <div class="stream-wrap">
    <div
      ref="scrollEl"
      class="stream"
      @scroll="onScroll"
      @wheel.passive="onUserScroll"
      @touchmove.passive="onUserScroll"
    >
      <div ref="contentEl" class="stream-content">
        <!-- With rows on screen this is a thin hint above them; with none it
             is the whole view, so centre it instead of pinning a 16px spinner
             to the top-left corner of an empty page. -->
        <div v-if="loading" class="loading-hint" :class="{ 'loading-full': !rows.length }">
          <van-loading :size="rows.length ? 16 : 22" />
          <span>{{ rows.length ? "Loading history..." : "Loading messages…" }}</span>
        </div>
        <button
          v-if="canLoadOlder && !loading"
          type="button"
          class="load-older"
          :disabled="loadingOlder"
          @click="emit('loadOlder')"
        >
          <van-loading v-if="loadingOlder" size="14" />
          <span v-else>↑ Load earlier messages</span>
        </button>
        <template v-for="row in rows" :key="row.key">
          <MessageBubble
            v-if="row.kind === 'bubble'"
            :data-mk="row.key"
            :role="row.role"
            :text="row.text"
            :ts="row.ts"
            :level="row.level"
            :is-latest="row.key === latestAssistantKey"
          />
          <ToolUseBubble
            v-else
            :tool="row.tool"
            :tool-id="row.toolId"
            :input="row.input"
            :ts="row.ts"
            :result="row.result"
          />
        </template>
        <div v-if="isThinking && !loading" class="thinking">
          <span class="dot" /><span class="dot" /><span class="dot" />
          <span class="tlabel">Thinking…</span>
        </div>
        <div v-if="!loading && rows.length === 0" class="empty-hint">
          No messages yet.
        </div>
      </div>
    </div>
    <!-- Right-edge navigator: a fixed-size WINDOW over the messages I typed,
         evenly spaced. Tapping re-centres the window on that message, so
         repeated taps on the end dot walk through a long session. -->
    <nav
      v-if="userNodes.length > 1"
      class="msg-rail"
      :aria-label="`Jump to my messages (${visibleNodes.length} of ${userNodes.length} shown)`"
    >
      <!-- Where you are in the WHOLE session, in 2px. Track = every message,
           thumb = the ones reachable as dots right now, notch = you. Ordinal,
           not pixel-mapped: content height changes async under us. -->
      <span v-if="windowed" class="rail-bar" aria-hidden="true">
        <span class="rail-bar-thumb" :style="thumbStyle" />
        <span class="rail-bar-notch" :style="notchStyle" />
      </span>

      <!-- The window-mover. Moving the window is its ONLY job — it never
           scrolls the transcript, just as a dot never moves the window. -->
      <template v-if="windowed">
        <button
          type="button"
          class="rail-page rail-page-up"
          :class="{ 'points-at-active': activeAbove }"
          :disabled="!hasOlderOffscreen"
          :aria-disabled="!hasOlderOffscreen"
          aria-label="Show older messages on the rail"
          @click="pageRail(-1)"
        >
          <span class="rail-chev rail-chev-up" />
        </button>
        <button
          type="button"
          class="rail-page rail-page-down"
          :class="{ 'points-at-active': activeBelow }"
          :disabled="!hasNewerOffscreen"
          :aria-disabled="!hasNewerOffscreen"
          aria-label="Show newer messages on the rail"
          @click="pageRail(1)"
        >
          <span class="rail-chev rail-chev-down" />
        </button>
      </template>
      <button
        v-for="({ node: n, g }, k) in visibleNodes"
        :key="n.i"
        type="button"
        class="rail-node"
        :class="{
          active: g === activeNodeG,
          pending: awaitingNode === n.i,
        }"
        :style="{ top: `calc(24px + (100% - 48px) * ${nodeTop(k)})` }"
        :aria-label="`Jump to my message ${g + 1} of ${userNodes.length}`"
        @click="onNodeTap(n, g, nodeTop(k))"
      >
        <span class="rail-dot" />
      </button>
      <transition name="hint-fade">
        <span v-if="railHint" class="rail-hint" :style="{ top: railHint.top }">
          {{ railHint.text }}
        </span>
      </transition>
    </nav>
    <button
      v-if="showJumpButton"
      class="jump-btn"
      type="button"
      @click="scrollToBottom()"
    >
      ↓ {{ newCount > 0 ? `${newCount} new` : "New messages" }}
    </button>
  </div>
</template>

<style scoped>
.stream-wrap {
  position: relative;
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.stream {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0 16px;
  scroll-behavior: smooth;
  background: var(--bg);
}
.loading-hint,
.empty-hint {
  padding: 16px;
  text-align: center;
  color: var(--text-faint);
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
/* Nothing behind it — give the spinner the page instead of the top edge. */
.loading-full {
  flex-direction: column;
  gap: 12px;
  min-height: 45vh;
}
.load-older {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin: 8px auto 4px;
  padding: 5px 16px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-soft);
  background: var(--surface-1);
  border: 1px solid var(--outline-soft);
  border-radius: var(--radius-pill);
  cursor: pointer;
}
.load-older:disabled {
  opacity: 0.6;
}
.thinking {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 8px 16px;
}
.thinking .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
  animation: think 1.2s infinite ease-in-out;
}
.thinking .dot:nth-child(2) {
  animation-delay: 0.2s;
}
.thinking .dot:nth-child(3) {
  animation-delay: 0.4s;
}
.thinking .tlabel {
  margin-left: 6px;
  font-size: 13px;
  color: var(--text-soft);
}
@keyframes think {
  0%,
  60%,
  100% {
    opacity: 0.3;
  }
  30% {
    opacity: 1;
  }
}
.jump-btn {
  position: absolute;
  right: 12px;
  bottom: 12px;
  padding: 7px 14px;
  border-radius: var(--radius-pill);
  background: var(--primary);
  color: #fff;
  border: none;
  font-size: 12px;
  font-weight: 600;
  box-shadow: var(--shadow-fab);
  cursor: pointer;
}
/* Right-edge navigator rail: a thin vertical line with one dot per message I
   sent. Dots are distributed evenly top→bottom (a discrete timeline). */
/* Internal columns of the 20px strip, x from its left edge:
     0–4   gutter
     4–6   session bar   (whole session)
     6–11  gap
     11–13 dot hairline  (dot centre x = 12)
     13–20 gutter                                          */
.msg-rail {
  position: absolute;
  right: 4px;
  top: 14px;
  bottom: 84px; /* clear the composer + ↓-new button */
  width: 20px;
  z-index: 5;
  pointer-events: none; /* only the dots and chevrons are interactive */
}
.msg-rail::before {
  content: "";
  position: absolute;
  top: 24px; /* the chevron zones own the ends */
  bottom: 24px;
  left: 11px;
  width: 2px;
  background: var(--outline-soft);
  border-radius: 1px;
}
/* ── session bar: the whole conversation, in 2px ────────────────────────── */
.rail-bar {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 4px;
  width: 2px;
  border-radius: 1px;
  background: var(--outline-soft);
  opacity: 0.35;
}
.rail-bar-thumb {
  position: absolute;
  left: 0;
  width: 2px;
  border-radius: 1px;
  background: var(--primary);
  opacity: 0.4;
  transition: top 0.2s cubic-bezier(0.2, 0, 0, 1), height 0.2s ease;
}
/* Overhangs the track 1px each side so the reader reads as a mark ON the
   session, not another segment of it. */
.rail-bar-notch {
  position: absolute;
  left: -1px;
  width: 4px;
  height: 3px;
  border-radius: 1.5px;
  background: var(--primary);
  opacity: 0.9;
  transition: top 0.2s cubic-bezier(0.2, 0, 0, 1);
}
/* The button is a generous 22px touch target (dots would be impossible to hit
   at their 8px visual size); the visible dot lives in an inner span. */
.rail-node {
  position: absolute;
  left: 12px;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  background: transparent;
  transform: translate(-50%, -50%);
  display: grid;
  place-items: center;
  cursor: pointer;
  pointer-events: auto;
}
.rail-dot {
  width: 8px;
  height: 8px;
  border: 1.5px solid var(--bg);
  border-radius: 50%;
  background: var(--outline);
  transition: background 0.15s, transform 0.15s;
}
.rail-node:active .rail-dot {
  background: var(--text-soft);
}
.rail-node.active .rail-dot {
  background: var(--primary);
  transform: scale(1.6);
}
/* ── the window-mover ───────────────────────────────────────────────────────
   A 32px target in a 24px zone at each end of the strip. Tapping moves the
   RAIL and nothing else; the transcript stays exactly where it is. This is the
   half of the old tap behaviour that was worth keeping — separated out, so no
   single gesture moves two things in two different frames of reference. */
.rail-page {
  position: absolute;
  left: 12px;
  width: 32px;
  height: 32px;
  padding: 0;
  border: none;
  background: transparent;
  transform: translate(-50%, -50%);
  display: grid;
  place-items: center;
  cursor: pointer;
  pointer-events: auto;
  color: var(--text-faint);
  opacity: 0.55;
  transition: opacity 0.16s ease, transform 0.16s cubic-bezier(0.2, 0, 0, 1);
}
.rail-page-up {
  top: 12px;
}
.rail-page-down {
  bottom: 12px;
  transform: translate(-50%, 50%);
}
.rail-page:active:not(:disabled) {
  opacity: 1;
}
.rail-page:disabled {
  opacity: 0.25;
  cursor: default;
}
/* The reader is at a message the window has been paged away from. No dot in
   the column is active in that state, so without this the rail looks like it
   has lost track of where you are. */
.rail-page.points-at-active::after {
  content: "";
  position: absolute;
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--primary);
  opacity: 0.9;
}
.rail-page-up.points-at-active::after {
  bottom: 2px;
}
.rail-page-down.points-at-active::after {
  top: 2px;
}
.rail-chev {
  width: 6px;
  height: 6px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
}
.rail-chev-up {
  transform: rotate(-135deg) translate(-1px, -1px);
}
.rail-chev-down {
  transform: rotate(45deg) translate(-1px, -1px);
}
/* Paging older history in to reach this one. Without a sign, a deep jump reads
   as a dead dot for however long the round-trips take. */
.rail-node.pending .rail-dot {
  background: var(--primary);
  animation: rail-pulse 0.9s ease-in-out infinite;
}
@keyframes rail-pulse {
  0%, 100% { transform: scale(1); opacity: 0.55; }
  50% { transform: scale(1.6); opacity: 1; }
}
/* Floating one-line preview shown when a node is tapped (touch has no title=). */
.rail-hint {
  position: absolute;
  right: 26px;
  transform: translateY(-50%);
  max-width: 62vw;
  padding: 5px 10px;
  font-size: 12px;
  line-height: 1.3;
  color: var(--text);
  background: var(--surface-1);
  border: 1px solid var(--outline-soft);
  border-radius: var(--radius-sm);
  box-shadow: 0 2px 10px rgba(42, 33, 24, 0.14);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  pointer-events: none;
}
.hint-fade-enter-active,
.hint-fade-leave-active {
  transition: opacity 0.18s, transform 0.18s;
}
.hint-fade-enter-from,
.hint-fade-leave-to {
  opacity: 0;
  transform: translateY(-50%) translateX(6px);
}
</style>
