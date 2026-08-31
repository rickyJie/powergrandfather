<script setup lang="ts">
/**
 * TerminalToolbar — the row above the xterm mount when a session is active.
 *
 * Extracted from Sessions.vue (2026-07-25). Two visual modes, both live in
 * `.sess-active-head`:
 *   - fullscreen (isTerminalMaxed): the identity line (name / cwd / pid)
 *     is swapped for a horizontal chip strip of every live session. Chip
 *     dot color = status, red glow = unread, yellow outline = waiting.
 *   - non-fullscreen: parent renders its own identity slot; this component
 *     only owns the right-side action buttons (recent files, fullscreen
 *     toggle) via the `right` slot fallback.
 *
 * Pure presentational — no store access, no API calls. Parent (Sessions.vue,
 * slot 12) wires events to selectSession / toggleTerminalMax / openRecentFiles.
 */

export interface ChipItem {
  sid: string
  title: string
  /** running | idle | waiting_input | waiting_auth | exited | crashed */
  status: string
  unreadCount: number
  isActive: boolean
  pinned?: boolean
  manualUnread?: boolean
  tooltip?: string
}

const props = withDefaults(defineProps<{
  chips: ChipItem[]
  activeSid: string | null
  isTerminalMaxed: boolean
  recentFilesCount: number
  showRecentFilesBtn?: boolean
  overflowCount?: number
  showEmpty?: boolean
}>(), {
  showRecentFilesBtn: true,
  overflowCount: 0,
  showEmpty: true,
})

const emit = defineEmits<{
  (e: 'select-chip', sid: string): void
  (e: 'chip-context', payload: { sid: string; event: MouseEvent }): void
  (e: 'overflow-click', event: MouseEvent): void
  (e: 'toggle-fullscreen'): void
  (e: 'open-recent-files'): void
}>()

// Map raw status → the dot modifier class the CSS knows about.
// Mirrors stateTag() in Sessions.vue (waiting_input / waiting_auth → 'waiting').
function dotClass(status: string): string {
  if (status === 'waiting_input' || status === 'waiting_auth') return 'waiting'
  if (status === 'running') return 'running'
  if (status === 'idle') return 'idle'
  return ''
}

function formatUnread(n: number): string {
  if (!n) return '·'
  return n > 99 ? '99+' : String(n)
}

function formatCount(n: number): string {
  return n > 99 ? '99+' : String(n)
}
</script>

<template>
  <div class="term-toolbar" :class="{ 'fs-mode': isTerminalMaxed }">
    <!-- Fullscreen: chip strip owns the identity row -->
    <div v-if="isTerminalMaxed" class="fs-chip-strip">
      <div
        v-for="c in props.chips"
        :key="c.sid"
        class="fs-chip"
        :class="{
          active: c.isActive,
          pinned: c.pinned,
          'has-unread': !c.isActive && (c.unreadCount > 0 || c.manualUnread),
          'has-waiting': c.status === 'waiting_auth' || c.status === 'waiting_input',
        }"
        :title="c.tooltip || c.title"
        @click="emit('select-chip', c.sid)"
        @contextmenu.prevent="emit('chip-context', { sid: c.sid, event: $event })"
      >
        <span class="fs-dot" :class="dotClass(c.status)"></span>
        <span v-if="c.pinned" class="fs-pin">📌</span>
        <span class="fs-label">{{ c.title }}</span>
        <span
          v-if="c.unreadCount || c.manualUnread"
          class="fs-unread"
          :title="c.manualUnread ? 'Marked unread' : `${c.unreadCount} unread messages`"
        >{{ formatUnread(c.unreadCount) }}</span>
      </div>
      <button
        v-if="props.overflowCount > 0"
        class="fs-chip fs-chip-overflow"
        :title="`${props.overflowCount} more sessions`"
        @click="emit('overflow-click', $event)"
      >⧉ +{{ props.overflowCount }} ▾</button>
      <div v-if="props.showEmpty && !props.chips.length && !props.overflowCount" class="fs-empty">
        No live sessions
      </div>
    </div>

    <!-- Non-fullscreen: parent owns the left identity slot; this component
         only contributes the right-side action buttons. Rendered via a slot
         so the parent can inject its editable title / cwd / project picker
         without this component knowing about them. -->
    <slot v-else name="identity" />

    <div class="toolbar-right">
      <slot name="extra-actions" />
      <button
        class="fs-toggle-btn"
        @click="emit('toggle-fullscreen')"
        :title="isTerminalMaxed ? 'Exit fullscreen (Esc)' : 'Fullscreen terminal'"
      >{{ isTerminalMaxed ? '⛶' : '⛶' }}</button>
      <button
        v-if="props.showRecentFilesBtn"
        class="files-btn"
        :title="`Recent files claude touched in this session (${props.recentFilesCount})`"
        @click="emit('open-recent-files')"
      >📄<span v-if="props.recentFilesCount" class="files-btn-count"> {{ formatCount(props.recentFilesCount) }}</span></button>
    </div>
  </div>
</template>

<style scoped>
/* Toolbar container. Mirrors `.sess-active-head` layout from Sessions.vue —
   parent wraps this component with the outer `.sess-active-head` shell in
   normal mode; in fullscreen mode this component fully owns the row. */
.term-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-width: 0;
}
.term-toolbar.fs-mode {
  gap: 6px;
  padding: 8px 12px 8px 16px;
  overflow: hidden;
}

.toolbar-right {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-shrink: 0;
  margin-left: auto;
}

/* Fullscreen session chip strip (be3959b8 v2 — Option A design).
   Copied from Sessions.vue so this component is self-contained. Scoped
   to prevent leaking to any legacy `.fs-chip` still in Sessions.vue. */
.fs-chip-strip {
  flex: 1 1 auto; min-width: 0;
  display: flex; align-items: center; gap: 6px;
  overflow-x: auto; overflow-y: hidden;
  scrollbar-width: none;
  /* Subtle right-edge fade tells the user "there's more if you scroll". */
  mask-image: linear-gradient(to right, black calc(100% - 20px), transparent);
  -webkit-mask-image: linear-gradient(to right, black calc(100% - 20px), transparent);
}
.fs-chip-strip::-webkit-scrollbar { display: none; }
.fs-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--card);
  color: var(--ink-mute);
  font-family: 'Newsreader', serif;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
  max-width: 20ch;
  overflow: hidden;
  transition: border-color 120ms, color 120ms, background 120ms;
  flex-shrink: 0;
}
.fs-chip:hover { border-color: var(--ink-mute); color: var(--ink); }
.fs-chip.active {
  border-color: var(--ink);
  color: var(--ink);
  background: var(--canvas);
  font-weight: 500;
}
/* Pinned emphasis (independent of active): tinted background so the
   sort-to-top position is reinforced visually. */
.fs-chip.pinned:not(.active) {
  background: var(--pastel-yellow-bg, #FCF6E4);
  border-color: var(--pastel-yellow-fg, #d1a441);
  color: var(--ink);
}
/* Unread pulse: subtle glowing red border ring. */
.fs-chip.has-unread {
  border-color: var(--pastel-red-fg, #b85450);
  color: var(--ink);
  animation: fs-chip-glow 1.8s ease-in-out infinite;
}
@keyframes fs-chip-glow {
  0%, 100% { box-shadow: 0 0 0 0 rgba(184, 84, 80, 0.4); }
  50% { box-shadow: 0 0 0 4px rgba(184, 84, 80, 0); }
}
/* Waiting-input / waiting-auth: soft yellow outline (no pulse — dot
   already pulses; two pulses would look chaotic). */
.fs-chip.has-waiting:not(.active):not(.has-unread) {
  border-color: var(--pastel-yellow-fg, #d1a441);
}
.fs-chip .fs-dot {
  width: 8px; height: 8px; border-radius: 50%;
  flex-shrink: 0;
  background: var(--ink-faint);
}
.fs-chip .fs-dot.running { background: var(--pastel-blue-fg, #4a7fbb); }
.fs-chip .fs-dot.idle { background: var(--pastel-green-fg, #6a9a4a); }
.fs-chip .fs-dot.waiting {
  background: var(--pastel-yellow-fg, #d1a441);
  animation: fs-dot-pulse 1.4s ease-in-out infinite;
}
@keyframes fs-dot-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.fs-chip .fs-label {
  overflow: hidden; text-overflow: ellipsis;
}
.fs-chip .fs-pin { font-size: 9px; opacity: 0.65; }
.fs-chip .fs-unread {
  font-family: 'Geist Mono', monospace;
  font-size: 10px;
  color: var(--pastel-red-fg, #b85450);
  margin-left: 2px;
}
.fs-chip-overflow {
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
  color: var(--ink-mute);
}
.fs-empty {
  padding: 4px 12px;
  color: var(--ink-faint);
  font-style: italic;
  font-size: 12px;
}

/* Recent files button — compact monospace count so the badge doesn't
   inflate the header row height. */
.files-btn .files-btn-count {
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
  opacity: 0.7;
}

/* Fullscreen toggle button — inherit the parent's button reset so it
   matches the surrounding Kill / files-btn siblings. */
.fs-toggle-btn { cursor: pointer; }

@media (max-width: 640px) {
  /* Mobile detail is already full-canvas; the desktop fullscreen mode removes
     the explicit back button and is therefore intentionally hidden here. */
  .fs-toggle-btn { display: none; }
  .toolbar-right { gap: 4px; }
  .files-btn { min-width: 38px; min-height: 38px; padding: 4px 7px; }
}
</style>
