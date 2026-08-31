<script setup lang="ts">
// SessionTree — thin wrapper over SessionTreeNode that owns:
//   1. the v-for over root children (all three call sites in Sessions.vue
//      iterate tree.children, not the synthetic root itself), and
//   2. the empty-state fallback so callers don't have to duplicate it.
//
// Everything else is delegated: props are forwarded straight to
// SessionTreeNode, and every emit is re-emitted with the same shape.
// Slot 12 will migrate Sessions.vue's three <SessionTreeNode v-for> blocks
// to a single <SessionTree :children="..." ... /> call each.
//
// Design deliberately keeps `children: TreeNode[]` rather than a `tree:
// TreeNode` root so it stays a drop-in for the existing v-for pattern
// (search results also build synthetic single-leaf nodes on the fly).

import type { TreeNode } from '../lib/session_tree'
import type { SessionRow } from '../api/sessions'
import type { SessionProject } from '../api/sessionProjects'
import SessionTreeNode from './SessionTreeNode.vue'

const props = defineProps<{
  children: TreeNode[]
  activeSid: string | undefined
  isOpen: (path: string) => boolean
  toggleFolder: (path: string) => void
  leavesCount: (node: TreeNode) => number
  stateTag: (status: string) => string
  isWaitingAuth: (s: SessionRow) => boolean
  formatTime: (ts: string | null) => string
  unreadForSession?: (sid: string) => number
  sessionProjects?: SessionProject[]
  assignProject?: (sid: string, projectId: string | null) => void
  isAssigning?: (sid: string) => boolean
  leafMode?: 'live' | 'history'
  // Per-child folderPreviewN + showCwdInMeta: history call site varies
  // these per-child (Recent bucket uses different caps than normal
  // folders). Accept a function so the wrapper stays generic.
  folderPreviewN?: number | ((c: TreeNode) => number | undefined)
  showCwdInMeta?: boolean | ((c: TreeNode) => boolean)
  isResuming?: (sid: string) => boolean
  // Optional empty-state override. Callers already render their own
  // .empty div outside the tree today; when nothing is passed we stay
  // silent so we don't double up.
  emptyText?: string
}>()

const emit = defineEmits<{
  (e: 'select', sid: string): void
  (e: 'purge', sid: string, ev?: Event): void
  (e: 'stop', sid: string, ev?: Event): void
  (e: 'resume', sid: string, ev?: Event): void
  (e: 'contextmenu', sid: string, ev: MouseEvent): void
}>()

function resolveFolderPreviewN(c: TreeNode): number | undefined {
  if (typeof props.folderPreviewN === 'function') return props.folderPreviewN(c)
  return props.folderPreviewN
}
function resolveShowCwdInMeta(c: TreeNode): boolean | undefined {
  if (typeof props.showCwdInMeta === 'function') return props.showCwdInMeta(c)
  return props.showCwdInMeta
}
</script>

<template>
  <div class="session-tree">
    <template v-if="children.length">
      <SessionTreeNode
        v-for="c in children"
        :key="c.fullPath"
        :node="c"
        :active-sid="activeSid"
        :depth="0"
        :is-open="isOpen"
        :toggle-folder="toggleFolder"
        :leaves-count="leavesCount"
        :state-tag="stateTag"
        :is-waiting-auth="isWaitingAuth"
        :format-time="formatTime"
        :unread-for-session="unreadForSession"
        :session-projects="sessionProjects"
        :assign-project="assignProject"
        :is-assigning="isAssigning"
        :leaf-mode="leafMode"
        :folder-preview-n="resolveFolderPreviewN(c)"
        :show-cwd-in-meta="resolveShowCwdInMeta(c)"
        :is-resuming="isResuming"
        @select="(sid) => emit('select', sid)"
        @purge="(sid, ev) => emit('purge', sid, ev)"
        @stop="(sid, ev) => emit('stop', sid, ev)"
        @resume="(sid, ev) => emit('resume', sid, ev)"
        @contextmenu="(sid, ev) => emit('contextmenu', sid, ev)"
      />
    </template>
    <div v-else-if="emptyText" class="session-tree-empty">
      {{ emptyText }}
    </div>
  </div>
</template>

<script lang="ts">
export default { name: 'SessionTree' }
</script>

<style scoped>
/* Wrapper is presentation-free by design — SessionTreeNode owns row/folder
   styling, and Sessions.vue owns the sidebar chrome. We only need enough
   here to render the empty-state hint (which today lives in Sessions.vue
   as .empty; kept optional so migration can decide whether to keep the
   external one). */
.session-tree-empty {
  padding: 24px 16px;
  color: var(--ink-faint);
  font-size: 12px;
  text-align: center;
}
</style>
