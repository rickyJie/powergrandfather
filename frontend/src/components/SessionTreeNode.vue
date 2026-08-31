<script setup lang="ts">
import { ref, computed } from 'vue'
import type { SessionRow } from '../api/sessions'
import type { SessionProject } from '../api/sessionProjects'
import AgentBadge from './AgentBadge.vue'

interface TreeNode {
  name: string
  fullPath: string
  isLeaf: boolean
  session?: SessionRow
  children: TreeNode[]
}

const props = defineProps<{
  node: TreeNode
  activeSid: string | undefined
  depth: number
  isOpen: (path: string) => boolean
  toggleFolder: (path: string) => void
  leavesCount: (node: TreeNode) => number
  stateTag: (status: string) => string
  isWaitingAuth: (s: SessionRow) => boolean
  formatTime: (ts: string | null) => string
  // Optional so any legacy caller still compiles; if omitted, badges won't show.
  unreadForSession?: (sid: string) => number
  // Project-assignment props — passed down so leaf rows can move sessions
  // between projects without the user having to open the session first.
  // Both optional: components that don't wire them get the tree as before.
  sessionProjects?: SessionProject[]
  assignProject?: (sid: string, projectId: string | null) => void
  isAssigning?: (sid: string) => boolean
  // History-mode additions (Option A design):
  //   leafMode = 'history' → leaves render a compact single-line
  //     row: ● title · time · exit + Resume/Delete buttons.
  //   folderPreviewN → cap the number of direct-leaf children a folder
  //     renders (default = unlimited). "+ show N more" reveals the rest.
  //     Only applies when the folder's children are all leaves.
  //   isResuming → guard for Resume button spam; hoisted from Sessions.vue
  //     so double-clicks land in the same per-sid lock as the History flat
  //     view used to hit.
  leafMode?: 'live' | 'history'
  folderPreviewN?: number
  isResuming?: (sid: string) => boolean
  //   isTerminating → guard for the live-row stop (×) button; set while a
  //     stop/kill is in flight (async 202 accepted but the SIGINT→SIGTERM→
  //     SIGKILL ladder is still running, up to 15s). Disables the × and shows
  //     a spinner so the user doesn't re-click thinking nothing happened.
  isTerminating?: (sid: string) => boolean
  // History leaf meta-line composition: when true, the meta line leads
  // with the session's cwd (Recent bucket case — rows are context-free
  // outside their normal folder). Propagated down through nested
  // SessionTreeNode instances so nested leaves inherit the mode.
  showCwdInMeta?: boolean
}>()

function unreadCount(sid: string): number {
  return props.unreadForSession ? props.unreadForSession(sid) : 0
}
// True when the row should show an unread indicator — either there's a
// real unread count OR the user manually marked this session as unread
// (sticky flag; only cleared via the right-click "Mark as read").
function hasUnreadIndicator(s: SessionRow): boolean {
  return unreadCount(s.id) > 0 || !!s.manual_unread
}
// Sum unread across every session under a folder — folder-level rollup lets
// the user see there's "something new down there" without expanding.
function unreadUnderFolder(n: TreeNode): number {
  if (n.isLeaf) return n.session ? unreadCount(n.session.id) : 0
  let total = 0
  for (const c of n.children) total += unreadUnderFolder(c)
  return total
}
// The collapsed-folder badge referenced unreadUnderFolder(node) twice in the
// template (v-if + :title), each re-walking the whole subtree every render.
// Memoize to one subtree walk per render; recomputes only when the node shape
// or the unread map changes.
const folderUnread = computed(() => unreadUnderFolder(props.node))

const emit = defineEmits<{
  (e: 'select', sid: string): void
  (e: 'purge', sid: string, ev?: Event): void
  (e: 'archive', sid: string, ev?: Event): void
  (e: 'stop', sid: string, ev?: Event): void
  (e: 'resume', sid: string, ev?: Event): void
  (e: 'contextmenu', sid: string, ev: MouseEvent): void
}>()

function onSelect(sid: string) { emit('select', sid) }
function onPurge(sid: string, ev?: Event) { emit('purge', sid, ev) }
function onArchive(sid: string, ev?: Event) { emit('archive', sid, ev) }
function onStop(sid: string, ev?: Event) { emit('stop', sid, ev) }
function onResume(sid: string, ev?: Event) { emit('resume', sid, ev) }
function onContextMenu(sid: string, ev: MouseEvent) {
  ev.preventDefault()
  emit('contextmenu', sid, ev)
}

// Handler for the inline project select. Stops click/change from bubbling
// to the row's @click="onSelect" — otherwise touching the dropdown would
// also switch the active session.
function onProjectChange(sid: string, ev: Event) {
  ev.stopPropagation()
  const target = ev.target as HTMLSelectElement
  const val = target.value || null
  if (props.assignProject) props.assignProject(sid, val)
}

// Per-folder "show all" toggle — collapsed by default; state lives on this
// component instance so different folders track independently. Vue caches
// child components by :key=fullPath, so this ref survives across parent
// re-renders as long as the folder stays mounted.
const showAllLeaves = ref(false)
function toggleShowAll(ev: Event) {
  ev.stopPropagation()
  showAllLeaves.value = !showAllLeaves.value
}

// Split children into "immediately renderable" + "hidden by N-cap". Only
// applies the cap when every child is a leaf (project/Recent bucket case).
// Nested folder trees render in full — the cap is about session count
// noise, not folder count.
const allChildrenAreLeaves = computed(() =>
  props.node.children.length > 0 && props.node.children.every((c) => c.isLeaf),
)
const capActive = computed(() =>
  props.leafMode === 'history' &&
  allChildrenAreLeaves.value &&
  typeof props.folderPreviewN === 'number' &&
  props.node.children.length > (props.folderPreviewN as number),
)
const visibleChildren = computed(() => {
  if (!capActive.value || showAllLeaves.value) return props.node.children
  return props.node.children.slice(0, props.folderPreviewN as number)
})
const hiddenCount = computed(() =>
  capActive.value ? props.node.children.length - (props.folderPreviewN as number) : 0,
)

// History leaf Resume gate — matches the backend's resumability check.
function canResume(s: SessionRow): boolean {
  if (s.superseded_by) return false
  const agent = s.agent || s.backend || 'claude'
  if (agent === 'claude') return !!s.external_session_id && s.jsonl_present !== false
  if (agent === 'codex') return !!(s.external_session_id || s.rollout_path)
  return false
}
// Human-readable reason a session can't be resumed, shown as the tooltip
// on the disabled Resume button (users kept asking "why is it greyed
// out" when we hid the button entirely — now they get a straight answer).
function resumeBlockedReason(s: SessionRow): string {
  if (s.superseded_by) return 'Session was already resumed into a newer chain — resume that row instead.'
  const agent = s.agent || s.backend || 'claude'
  if (agent === 'claude' && !s.external_session_id) return 'No Claude conversation id was bound.'
  if (agent === 'claude' && s.jsonl_present === false) return 'Claude has pruned this session history.'
  if (agent === 'codex' && !s.external_session_id && !s.rollout_path) return 'No Codex rollout was bound.'
  return `${agent} does not advertise session resume support.`
}

function statusText(status: string): string {
  if (status === 'starting') return 'starting process'
  if (status === 'running') return 'agent working'
  if (status === 'idle' || status === 'waiting_input') return 'waiting for input'
  if (status === 'waiting_auth') return 'permission needed'
  if (status === 'orphaned') return 'PTY unavailable'
  if (status === 'exited') return 'ended'
  if (status === 'crashed') return 'failed'
  return status
}

// Wall-clock session duration — shown as the third segment of the meta
// line ("ran 14m"). Returns null when either endpoint is missing or the
// dates parse to garbage, so the template can omit the segment entirely
// instead of rendering "ran ?".
function duration(s: SessionRow): string | null {
  if (!s.started_at || !s.ended_at) return null
  const start = new Date(s.started_at).getTime()
  const end = new Date(s.ended_at).getTime()
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null
  const secs = Math.floor((end - start) / 1000)
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m`
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return m ? `${h}h${m}m` : `${h}h`
}

// Map raw session status → design-token status key used by the pill CSS.
// Group killed/orphaned under "warn" and treat exited as neutral.
function pillStatus(status: string): string {
  if (status === 'crashed') return 'crashed'
  if (status === 'orphaned') return 'orphaned'
  return 'exited'
}
</script>

<template>
  <!-- folder (non-leaf, non-root) -->
  <template v-if="!node.isLeaf">
    <div
      class="tree-folder"
      :style="{ paddingLeft: (depth * 12 + 8) + 'px' }"
      role="button"
      tabindex="0"
      @click="toggleFolder(node.fullPath)"
      @keydown.enter.prevent="toggleFolder(node.fullPath)"
      @keydown.space.prevent="toggleFolder(node.fullPath)"
    >
      <span class="caret">{{ isOpen(node.fullPath) ? '▾' : '▸' }}</span>
      <span class="folder-name mono">{{ node.name }}</span>
      <span class="folder-meta">({{ leavesCount(node) }})</span>
      <!-- Folder-level unread pip: any unread under this subtree that the
           user hasn't drilled into yet. Hidden when the folder is open (the
           child badges are visible). -->
      <span
        v-if="!isOpen(node.fullPath) && folderUnread > 0"
        class="dot"
        :title="`${folderUnread} unread`"
      ></span>
    </div>
    <template v-if="isOpen(node.fullPath)">
      <SessionTreeNode
        v-for="c in visibleChildren"
        :key="c.fullPath"
        :node="c"
        :active-sid="activeSid"
        :depth="depth + 1"
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
        :folder-preview-n="folderPreviewN"
        :is-resuming="isResuming"
        :is-terminating="isTerminating"
        :show-cwd-in-meta="showCwdInMeta"
        @select="onSelect"
        @purge="onPurge"
        @archive="onArchive"
        @stop="onStop"
        @resume="onResume"
        @contextmenu="(sid, ev) => emit('contextmenu', sid, ev)"
      />
      <button
        v-if="capActive"
        class="show-more-btn"
        :style="{ paddingLeft: ((depth + 1) * 12 + 8) + 'px' }"
        @click="toggleShowAll"
      >{{ showAllLeaves ? `− collapse (${hiddenCount} hidden)` : `+ show ${hiddenCount} more` }}</button>
    </template>
  </template>

  <!-- leaf (session) — history mode: two-line receipt row.
       Row1: [status pill] [title]                    [▶ Resume] [×]
       Row2:               [meta: (cwd) · age · exit · ran]
       Recent-bucket variant prepends cwd to the meta line so context-free
       rows are still identifiable. -->
  <template v-else-if="node.session && leafMode === 'history'">
    <div
      class="hist-row"
      :class="{ active: node.session.id === activeSid }"
      :style="{ paddingLeft: (depth * 12 + 12) + 'px' }"
      role="button"
      tabindex="0"
      @click="onSelect(node.session.id)"
      @keydown.enter.prevent="onSelect(node.session.id)"
      @keydown.space.prevent="onSelect(node.session.id)"
      @contextmenu="onContextMenu(node.session.id, $event)"
    >
      <span
        class="tag"
        :class="pillStatus(node.session.status)"
        :title="node.session.status === 'orphaned'
          ? 'orphaned — backend was restarted while this session was live. The process is still running but CSM lost the PTY handle. Resume in a fresh PTY to keep going, or delete to reclaim.'
          : node.session.status"
      >{{ statusText(node.session.status) }}</span>
      <AgentBadge
        v-if="node.session.agent"
        class="hist-agent-badge"
        :agent="node.session.agent"
        :compact="true"
      />
      <span class="hist-title" :title="node.session.title || node.session.id">
        <span v-if="node.session.pinned" class="pin-mark" title="Pinned">📌</span>{{ node.session.title || node.session.id.slice(0, 8) }}
      </span>
      <button
        class="hist-resume-btn"
        :disabled="!canResume(node.session) || (isResuming ? isResuming(node.session.id) : false)"
        :title="canResume(node.session)
          ? (isResuming && isResuming(node.session.id) ? 'Resuming…' : `Resume ${node.session.agent || 'agent'} conversation in a fresh PTY`)
          : resumeBlockedReason(node.session)"
        @click.stop="canResume(node.session) && onResume(node.session.id, $event)"
      >{{ isResuming && isResuming(node.session.id) ? '…' : '▶ Resume' }}</button>
      <button
        class="hist-del-btn"
        title="Archive from the default History view"
        aria-label="Archive session"
        @click.stop="onArchive(node.session.id, $event)"
      >×</button>
      <div class="hist-meta mono" :title="node.session.cwd">
        <span v-if="showCwdInMeta" class="hist-meta-cwd">{{ node.session.cwd }}</span>
        <span v-if="showCwdInMeta" class="sep">·</span>
        <span>{{ formatTime(node.session.ended_at || node.session.last_activity_ts) }}</span>
        <template v-if="node.session.exit_code != null">
          <span class="sep">·</span>
          <span :class="{ 'hist-exit-nonzero': node.session.exit_code !== 0 }">exit {{ node.session.exit_code }}</span>
        </template>
        <template v-if="duration(node.session)">
          <span class="sep">·</span>
          <span>ran {{ duration(node.session) }}</span>
        </template>
      </div>
    </div>
  </template>

  <!-- leaf (session) — live mode (Active/Auto tabs): detail-rich row -->
  <template v-else-if="node.session">
    <div
      class="sess-row"
      :class="{
        active: node.session.id === activeSid,
        'pulse-auth': isWaitingAuth(node.session),
        highlighted: !!node.session.highlighted,
      }"
      :style="{ paddingLeft: (depth * 12 + 8) + 'px' }"
      role="button"
      tabindex="0"
      @click="onSelect(node.session.id)"
      @keydown.enter.prevent="onSelect(node.session.id)"
      @keydown.space.prevent="onSelect(node.session.id)"
      @contextmenu="onContextMenu(node.session.id, $event)"
    >
      <div class="top">
        <span class="title">
          <span v-if="node.session.pinned" class="pin-mark" title="Pinned">📌</span><span v-if="node.session.highlighted" class="highlight-mark" title="Highlighted">⭐</span>{{ node.session.title || node.session.id.slice(0, 8) }}
        </span>
        <span class="row-actions">
          <!-- Permission-waiting dot: always visible while WAITING_AUTH so
               the user has an unmistakable red marker even on the active
               row (unlike the unread indicators, which self-hide on
               active — permission blocks progress and needs to be seen
               regardless). Rendered before the unread cluster so it
               anchors the visual scan. -->
          <span
            v-if="isWaitingAuth(node.session)"
            class="perm-dot"
            title="Waiting for your permission — Claude is paused"
          ></span>
          <!-- Chat-app style unread indicator: pip for 1, iOS badge for 2+.
               Hidden on the active row (see .sess-row.active .badge/.dot CSS)
               because being IN the session is the natural "I've seen it" state.
               manual_unread (right-click "Mark unread") forces a pip regardless. -->
          <span
            v-if="unreadCount(node.session.id) > 1"
            class="badge"
            :title="`${unreadCount(node.session.id)} unread messages`"
          >{{ unreadCount(node.session.id) > 99 ? '99+' : unreadCount(node.session.id) }}</span>
          <span
            v-else-if="hasUnreadIndicator(node.session)"
            class="dot"
            :title="node.session.manual_unread ? 'Marked unread' : '1 unread message'"
          ></span>
          <button
            class="del-btn"
            :disabled="isTerminating ? isTerminating(node.session.id) : false"
            :title="isTerminating && isTerminating(node.session.id) ? 'Stopping…' : 'Stop (moves to History)'"
            @click="!(isTerminating && isTerminating(node.session.id)) && onStop(node.session.id, $event)"
          >{{ isTerminating && isTerminating(node.session.id) ? '…' : '×' }}</button>
        </span>
      </div>
      <span class="tag" :class="stateTag(node.session.status)" :title="`Process/turn state: ${node.session.status}`">{{ statusText(node.session.status) }}</span>
      <AgentBadge
        v-if="node.session.agent"
        class="row-agent-badge"
        :agent="node.session.agent"
        :compact="true"
      />
      <div v-if="node.session.current_tool" class="meta now">now: {{ node.session.current_tool }}</div>
      <div v-else-if="node.session.last_assistant_msg" class="meta last-msg">
        "{{ node.session.last_assistant_msg.slice(0, 60) }}{{ node.session.last_assistant_msg.length > 60 ? '…' : '' }}"
      </div>
      <div class="time-row">
        <span class="time">{{ formatTime(node.session.last_activity_ts || node.session.started_at) }}<span v-if="node.session.pid" class="mono"> · pid {{ node.session.pid }}</span></span>
        <!-- Inline project picker — always visible for unassigned sessions
             (the common "please move me" state), hover-visible for already-
             assigned rows so they stay clean at rest. click.stop keeps the
             row's own @click from swallowing the interaction. -->
        <select
          v-if="sessionProjects && assignProject"
          class="row-proj-picker"
          :class="{ unassigned: !node.session.session_project_id }"
          :value="node.session.session_project_id || ''"
          :disabled="isAssigning ? isAssigning(node.session.id) : false"
          :title="node.session.session_project_id ? 'Move to another project' : 'Assign to a project'"
          @click.stop
          @change="onProjectChange(node.session.id, $event)"
        >
          <option value="">📁 (unassigned)</option>
          <option
            v-for="p in sessionProjects"
            :key="p.id"
            :value="p.id"
          >📁 {{ p.name }}</option>
        </select>
      </div>
    </div>
  </template>
</template>

<script lang="ts">
export default { name: 'SessionTreeNode' }
</script>

<style scoped>
/* Section header — same density as sess-row so the sidebar doesn't
   visually re-flow when switching between Active / History. */
.tree-folder {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 14px;
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-mute);
  background: var(--canvas);
  cursor: pointer;
  user-select: none;
  border-bottom: 1px solid var(--border);
}
.tree-folder:hover { color: var(--ink); }
.tree-folder:focus-visible,
.sess-row:focus-visible,
.hist-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.tree-folder .caret { width: 10px; color: var(--ink-faint); font-size: 10px; }
.tree-folder .folder-name { color: inherit; font-size: 11px; font-family: inherit; }
.tree-folder .folder-meta { color: var(--ink-faint); font-size: 10px; font-family: 'Geist Mono', monospace; }
.tree-folder .dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--pastel-red-fg);
  margin-left: auto;
  flex-shrink: 0;
}
.hist-agent-badge { grid-area: agent; align-self: center; }

.sess-row {
  position: relative;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 120ms var(--ease-soft);
}
.sess-row:last-child { border-bottom: 0; }
.sess-row:hover { background: var(--canvas); }
.sess-row.active {
  background: var(--accent-soft-bg);
  color: var(--accent-soft-fg);
}
.sess-row.active::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0;
  width: 2px; background: var(--accent);
}
/* Highlighted (⭐ from right-click menu) — soft gold accent bar on the
   left, tinted background. Distinct from .active so a highlighted-and-
   active row still reads as active first, highlighted second. */
.sess-row.highlighted {
  background: var(--pastel-yellow-bg, #FCF6E4);
}
.sess-row.highlighted::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0;
  width: 2px; background: var(--pastel-yellow-fg, #957024);
}
.sess-row.highlighted.active::before {
  background: var(--accent);
}
.highlight-mark { margin-right: 4px; font-size: 12px; }
.sess-row .top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.sess-row .title { font-family: 'Newsreader', serif; font-size: 15px; font-weight: 500; color: var(--ink); }
.sess-row .row-actions { display: flex; gap: 6px; align-items: center; }
/* Chat-app style unread pip — tone-tinted red (matches pastel palette). */
.sess-row .dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--pastel-red-fg);
  flex-shrink: 0;
}
/* Chat-app style unread badge — red pill with count (2..99+).
   Uses pastel-red-fg (matches pill palette) instead of hardcoded iOS red. */
.sess-row .badge {
  min-width: 17px; height: 17px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--pastel-red-fg);
  color: var(--card);
  font-family: system-ui, -apple-system, "Segoe UI", "Helvetica Neue", sans-serif;
  font-size: 11px;
  font-weight: 500;
  line-height: 17px;
  letter-spacing: -0.15px;
  text-align: center;
  flex-shrink: 0;
}
/* When row is active (highlighted background), keep badge crisp. */
.sess-row.active .badge, .sess-row.active .dot { display: none; }
.sess-row .del-btn {
  padding: 0 6px; line-height: 18px;
  background: transparent; border: none;
  color: var(--ink-faint);
  opacity: 0; visibility: hidden;
  transition: opacity 150ms;
  font-size: 14px;
  cursor: pointer;
}
.sess-row:hover .del-btn { opacity: 1; visibility: visible; }
.sess-row .del-btn:hover { color: var(--pastel-red-fg); background: var(--pastel-red-bg); border-radius: 4px; }
.sess-row .meta {
  font-size: 12px; color: var(--ink-mute);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-top: 4px;
  font-family: 'Geist Mono', monospace;
}
.sess-row .meta.now { color: var(--pastel-blue-fg); }
.sess-row .meta.last-msg { color: var(--ink-2); font-family: 'Newsreader', serif; font-style: italic; font-size: 13px; }
.sess-row .time { font-size: 11px; color: var(--ink-faint); }
.sess-row .time-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 6px; margin-top: 2px;
}
.sess-row .row-proj-picker {
  font-size: 10px;
  padding: 1px 4px;
  max-width: 130px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 3px;
  color: var(--ink-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 120ms, border-color 120ms, color 120ms, background 120ms;
}
.sess-row:hover .row-proj-picker { opacity: 1; border-color: var(--border); }
.sess-row .row-proj-picker:hover { border-color: var(--ink-mute); color: var(--ink); background: var(--card); }
/* Unassigned rows always show the picker at low emphasis — that's the
   whole point of surfacing it inline: users can spot rows without a
   project and assign one without opening the session first. */
.sess-row .row-proj-picker.unassigned {
  opacity: 0.7;
  border-color: var(--pastel-yellow-fg, #d1a441);
  color: var(--pastel-yellow-fg, #957024);
  background: var(--pastel-yellow-bg, #FCF6E4);
}
.sess-row:hover .row-proj-picker.unassigned { opacity: 1; }
.sess-row .row-proj-picker:disabled { opacity: 0.4; cursor: wait; }
.sess-row.pulse-auth {
  border-left: 3px solid var(--pastel-red-fg);
  animation: pulse-bg 1.6s ease-in-out infinite;
}
@keyframes pulse-bg {
  0%, 100% { background: var(--card); }
  50% { background: var(--pastel-red-bg); }
}
.perm-dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--pastel-red-fg);
  box-shadow: 0 0 6px var(--pastel-red-fg);
  vertical-align: middle;
  margin-right: 6px;
  animation: perm-pulse 1s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes perm-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.55; transform: scale(0.85); }
}

/* --- History-mode leaf: two-line receipt row (Scheme A) ---
   Row1: [pill] [title]  ... [▶ Resume] [×]
   Row2:        [meta: cwd? · age · exit · ran] */
.hist-row {
  display: grid;
  grid-template-columns: auto auto 1fr auto auto;
  grid-template-areas:
    "pill agent title resume del"
    "meta meta  meta  meta   meta";
  column-gap: 10px;
  row-gap: 2px;
  position: relative;
  /* Match sess-row padding so switching Active↔History doesn't cause the
     sidebar to visually re-flow at a different density. */
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 120ms var(--ease-soft);
}
.hist-row:last-child { border-bottom: 0; }
.hist-row:hover { background: var(--canvas); }
.hist-row.active {
  background: var(--accent-soft-bg);
  color: var(--accent-soft-fg);
}
.hist-row.active::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0;
  width: 2px; background: var(--accent);
}

/* .hist-pill dialects removed 2026-07-25 — consolidated to global .tag
   primitive in style.css. See UI redesign spec §P0.1. */
.hist-row > .tag { grid-area: pill; align-self: center; }

.hist-title {
  grid-area: title; align-self: center;
  font-family: 'Newsreader', serif;
  font-size: 14px; font-weight: 500;
  color: var(--ink);
  line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  min-width: 0;
}

.hist-resume-btn {
  grid-area: resume; align-self: center;
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
  padding: 3px 10px;
  background: var(--card); color: var(--ink);
  border: 1px solid var(--border); border-radius: 4px;
  cursor: pointer;
  transition: border-color 120ms, background 120ms;
}
.hist-resume-btn:hover:not(:disabled) { border-color: var(--ink); background: var(--canvas); }
.hist-resume-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.hist-del-btn {
  grid-area: del; align-self: center;
  padding: 0 6px; line-height: 20px;
  background: transparent; border: none;
  color: var(--ink-faint);
  font-size: 15px; cursor: pointer;
  opacity: 0.4;
  transition: opacity 120ms, color 120ms, background 120ms;
  border-radius: 4px;
}
.hist-row:hover .hist-del-btn { opacity: 1; }
.hist-del-btn:hover { color: var(--pastel-red-fg); background: var(--pastel-red-bg); }

.hist-meta {
  grid-area: meta;
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
  color: var(--ink-faint);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  min-width: 0;
  display: flex; align-items: baseline; gap: 0;
}
.hist-meta .hist-meta-cwd {
  color: var(--ink-mute);
  overflow: hidden; text-overflow: ellipsis;
  direction: rtl; text-align: left;
  min-width: 0; flex-shrink: 1;
}
.hist-meta .sep { margin: 0 6px; opacity: 0.6; flex-shrink: 0; }
.hist-meta .hist-exit-nonzero {
  color: var(--pastel-red-fg, #b85450);
  font-weight: 500;
}

/* Pinned indicator — appears inline before the title on both live/history
   leaves. Small enough to not visually dominate a normal row. */
.pin-mark {
  font-size: 10px;
  margin-right: 4px;
  opacity: 0.7;
  vertical-align: 1px;
}

/* "+ show N more" toggle used by the folder N-cap. */
.show-more-btn {
  display: block; width: 100%;
  padding: 4px 16px;
  background: transparent; color: var(--ink-mute);
  border: none;
  border-bottom: 1px dashed var(--border);
  font-size: 11px; text-align: left; cursor: pointer;
  transition: color 120ms, background 120ms;
}
.show-more-btn:hover { color: var(--ink); background: var(--canvas); }
</style>
