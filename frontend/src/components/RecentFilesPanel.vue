<script setup lang="ts">
/**
 * RecentFilesPanel — dedicated popover for the "recent files claude
 * touched in this session" chip in Sessions.vue.
 *
 * Historically Sessions.vue reused the generic ContextMenu component
 * to render this list, which meant every path was flattened to a
 * `label` string and truncation was done server-side. This component
 * gives us a proper file-list surface: dedicated cells for tool icon /
 * path / timestamp, ellipsis via CSS (so the full path is available on
 * hover via `title`), and its own loading + empty states.
 *
 * Placement mirrors the ContextMenu pattern — floating at cursor
 * coordinates passed in via `position`. When `position` is omitted
 * the panel centers itself as a modal (fallback for keyboard triggers
 * or future toolbar placement). Backdrop dismiss + explicit close
 * button both emit `close`. Item click emits `open-file` with the raw
 * absolute path; the parent is responsible for `window.open`-ing the
 * preview URL (keeps this component free of API imports beyond the
 * shared `FileTouch` type).
 *
 * Wiring into Sessions.vue is deferred to slot 12 — this component
 * is dropped in isolation so it can be reviewed independently.
 */
import type { FileTouch } from '../api/files'

const props = defineProps<{
  visible: boolean
  files: FileTouch[]
  isLoading?: boolean
  position?: { x: number; y: number }
  title?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-file', path: string): void
}>()

/**
 * Icon key mirrors Sessions.vue `_fileToolIcon` so users get the same
 * visual language they're used to from the ContextMenu version.
 */
function toolIcon(tool: string): string {
  return tool === 'Write' ? '📝'
    : tool === 'Edit' ? '✎'
    : tool === 'MultiEdit' ? '✏️'
    : tool === 'Create' ? '➕'
    : '·'
}

/**
 * Timestamps come off the wire as naked UTC strings (per README).
 * Render as local HH:MM:SS — the panel is inherently a "recent" view
 * so date is redundant; showing just the time keeps the cell narrow.
 */
function formatTs(ts: string | null): string {
  if (!ts) return ''
  try {
    const d = new Date(ts.endsWith('Z') ? ts : `${ts}Z`)
    return d.toLocaleTimeString('en-US', { hour12: false })
  } catch {
    return ''
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="rfp-fade">
      <div
        v-if="props.visible"
        class="rfp-backdrop"
        @click.self="emit('close')"
      >
        <div
          class="rfp-panel"
          :class="{ 'rfp-panel--floating': !!props.position, 'rfp-panel--modal': !props.position }"
          :style="props.position ? { top: `${props.position.y}px`, left: `${props.position.x}px` } : {}"
          role="dialog"
          aria-label="Recent files"
        >
          <header class="rfp-header">
            <span class="rfp-title">{{ props.title ?? 'Recent Files' }}</span>
            <button
              class="rfp-close"
              type="button"
              @click="emit('close')"
              aria-label="Close recent files"
            >✕</button>
          </header>

          <div v-if="props.isLoading" class="rfp-status">Loading…</div>
          <div v-else-if="!props.files || props.files.length === 0" class="rfp-status">
            No files touched yet in this session
          </div>
          <ul v-else class="rfp-list">
            <li
              v-for="f in props.files"
              :key="f.id"
              class="rfp-item"
              :title="f.path"
              @click="emit('open-file', f.path)"
            >
              <span class="rfp-tool" :aria-label="f.tool">{{ toolIcon(f.tool) }}</span>
              <span class="rfp-path">{{ f.path }}</span>
              <span class="rfp-ts">{{ formatTs(f.ts) }}</span>
            </li>
          </ul>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* Transparent backdrop — floating popover shouldn't dim the app,
 * but we still need a full-viewport catcher so click-outside closes. */
.rfp-backdrop {
  position: fixed;
  inset: 0;
  background: transparent;
  z-index: 1000;
}

.rfp-panel {
  background: var(--panel-bg, #1c1e26);
  border: 1px solid var(--panel-border, #2a2c36);
  border-radius: 8px;
  min-width: 320px;
  max-width: 520px;
  max-height: 60vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.4);
  color: var(--text-primary, #d0d3dc);
}

.rfp-panel--floating {
  position: absolute;
}

.rfp-panel--modal {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}

.rfp-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid var(--panel-border, #2a2c36);
  font-size: 12px;
  color: var(--text-secondary, #9098aa);
  flex: 0 0 auto;
}

.rfp-title {
  font-weight: 500;
}

.rfp-close {
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 2px 4px;
  border-radius: 4px;
}

.rfp-close:hover {
  background: var(--panel-hover, #262832);
  color: var(--text-primary, #d0d3dc);
}

.rfp-status {
  padding: 16px 12px;
  text-align: center;
  color: var(--text-secondary, #9098aa);
  font-size: 12px;
}

.rfp-list {
  list-style: none;
  margin: 0;
  padding: 4px 0;
  overflow-y: auto;
  flex: 1 1 auto;
}

.rfp-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 12px;
  cursor: pointer;
  font-family: var(--mono-font, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
  color: var(--text-primary, #d0d3dc);
}

.rfp-item:hover {
  background: var(--panel-hover, #262832);
}

.rfp-tool {
  flex: 0 0 auto;
  width: 16px;
  text-align: center;
}

.rfp-path {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  /* Anchor the ellipsis to the LEFT so filename stays visible on
   * long absolute paths — matches Sessions.vue's server-side "…/foo"
   * trick but in pure CSS so hover-title shows the full path. */
  direction: rtl;
  text-align: left;
}

.rfp-ts {
  flex: 0 0 auto;
  color: var(--text-tertiary, #7a8090);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.rfp-fade-enter-active,
.rfp-fade-leave-active {
  transition: opacity 120ms ease;
}

.rfp-fade-enter-from,
.rfp-fade-leave-to {
  opacity: 0;
}
</style>
