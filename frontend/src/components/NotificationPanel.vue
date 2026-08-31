<script setup lang="ts">
/**
 * NotificationPanel — bell-icon dropdown showing recent notifications.
 *
 * Pulls from the existing pinia store; reuses the store's WS connection
 * so the panel updates live when new events arrive.
 */
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useNotificationsStore } from '../stores/notifications'
import { sessionsApi } from '../api/sessions'
import type { NotificationRow } from '../api/notifications'
import { useToast } from '../composables/useToast'
import {
  type DesktopNotifState,
  desktopNotifState,
  isDesktopNotifEnabled,
  requestDesktopNotifPermission,
  setDesktopNotifEnabled,
} from '../lib/desktopNotify'

const router = useRouter()

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const store = useNotificationsStore()

// ── Desktop (OS-level) notifications toggle. Fires an OS popup for high-signal
// notifications while the app is unfocused (see lib/desktopNotify.ts). Enable
// requires a secure context (localhost/HTTPS) + granting OS permission.
const desktopEnabled = ref(isDesktopNotifEnabled())
const desktopState = ref<DesktopNotifState>(desktopNotifState())
const desktopHint = computed(() => {
  switch (desktopState.value) {
    case 'unsupported': return 'Not supported in this browser'
    case 'insecure': return 'Needs localhost/HTTPS (use the SSH tunnel)'
    case 'denied': return 'Blocked — allow in the browser site settings'
    default: return desktopEnabled.value ? 'On · pops when app is unfocused' : 'Off'
  }
})
const desktopToggleDisabled = computed(
  () => desktopState.value === 'unsupported' || desktopState.value === 'insecure',
)
async function toggleDesktop() {
  if (desktopToggleDisabled.value) return
  if (!desktopEnabled.value) {
    // Turning on — ensure OS permission first.
    if (Notification.permission !== 'granted') {
      const res = await requestDesktopNotifPermission()
      desktopState.value = res
      if (res !== 'granted') { toast.warn('Desktop notifications not granted'); return }
    }
    setDesktopNotifEnabled(true)
    desktopEnabled.value = true
  } else {
    setDesktopNotifEnabled(false)
    desktopEnabled.value = false
  }
  desktopState.value = desktopNotifState()
}

// Match the backend list limit (100) — under 50 the panel could hide
// unread rows while the bell badge is non-zero, giving the user no way
// to reach them except "Clear all".
const PANEL_ITEM_CAP = 100
const items = computed(() => store.items.slice(0, PANEL_ITEM_CAP))
const hasMore = computed(() => store.items.length > PANEL_ITEM_CAP)
const clearing = ref(false)
const toast = useToast()

// Per-item expanded state for long/agent-authored bodies.
const expanded = reactive<Record<string, boolean>>({})

// Notification type → visual severity (drives color accent).
// Token warnings and crashes get a warm color; port conflicts hard-red;
// new_message stays neutral to not compete with the primary chat unread flow.
function severityClass(type: string, title?: string): string {
  switch (type) {
    case 'token_warning': return 'sev-warn'
    case 'port_conflict':
    case 'session_crashed':
    case 'auto_run_failed': return 'sev-err'
    case 'auto_needs_review': return 'sev-attn'
    case 'mission_done':
      // Failures get error tint; successes stay neutral so the bell
      // isn't screaming green every time a mission wraps.
      return typeof title === 'string' && title.includes('failed') ? 'sev-err' : 'sev-neutral'
    case 'new_message':
    default: return 'sev-neutral'
  }
}

// User-facing label for the type tag.
function typeLabel(type: string, title?: string): string {
  switch (type) {
    case 'token_warning':    return '⚠ token alert'
    case 'port_conflict':    return '⚠ port conflict'
    case 'session_crashed':  return '✕ session crashed'
    case 'auto_run_failed':  return '✕ automation failed'
    case 'auto_needs_review': return '● needs review'
    case 'new_message':      return '● new message'
    case 'mission_done':
      return typeof title === 'string' && title.includes('failed')
        ? '✕ mission failed'
        : '✓ mission done'
    default:                 return type
  }
}

// Detect whether a body is long enough / structured enough to warrant an
// expand toggle. Agent-authored escalation summaries usually contain markdown
// bullets or multi-line paragraphs; short "metric X threshold Y" lines don't.
function isLongBody(body: string | null | undefined): boolean {
  if (!body) return false
  if (body.length > 140) return true
  if (body.split('\n').filter(Boolean).length >= 2) return true
  return false
}

function toggle(id: string) {
  expanded[id] = !expanded[id]
}

// Extract session-id-looking tokens from the notification body and render
// them as router-links to the Sessions detail modal (via /sessions/<id>).
// Matches:
//   - full uuid: `ef8f105f-38a2-4b3c-abcd-1234567890ab`
//   - short 8-char prefix followed by ellipsis / brackets etc: `ef8f105f…`
// Falls back to plain text if the regex doesn't match anything.
const _SID_FULL_RE = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/g
const _SID_SHORT_RE = /session (?:id )?(?:=|:)?\s*([0-9a-f]{8})\b/gi

type BodyPart = { text: string } | { link: string; sid: string }

function bodyParts(body: string | null | undefined, metadata: Record<string, any> | undefined): BodyPart[] {
  if (!body) return []
  const parts: BodyPart[] = []
  let cursor = 0
  const collect = (regex: RegExp, extract: (m: RegExpExecArray) => string) => {
    regex.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = regex.exec(body)) !== null) {
      if (m.index > cursor) parts.push({ text: body.slice(cursor, m.index) })
      const sid = extract(m)
      parts.push({ link: m[0], sid })
      cursor = m.index + m[0].length
    }
  }
  collect(_SID_FULL_RE, (m) => m[1])
  if (parts.length === 0) {
    collect(_SID_SHORT_RE, (m) => m[1])
  }
  if (cursor < body.length) parts.push({ text: body.slice(cursor) })
  // If nothing matched, `parts` is [] — fall back to whole body.
  if (parts.length === 0) parts.push({ text: body })
  return parts
}

function jumpToSession(sid: string) {
  // Full or short sid; try full first. Sessions.vue accepts a session id via
  // route param; short prefix is passed as-is and Sessions can fuzzy-match.
  // Close the panel so it doesn't hover over the destination view.
  router.push(`/sessions/${sid}`)
  emit('close')
}

function fmtRel(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const diff = Date.now() - t
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff/60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff/3_600_000)}h ago`
  return `${Math.floor(diff/86_400_000)}d ago`
}

async function dismiss(id: string) {
  await store.dismiss(id)
}

// Click anywhere on the item card:
//   - always mark read (silent — no navigation)
// This split lets the user acknowledge a notification without being
// teleported into an unrelated session. Explicit navigation lives on
// the session-tag chip / mission chip (see openTargetFromItem).
// The bell/panel is the sole authority on unread state after the
// Sessions/notification decouple, so click-to-read is the primary way to
// clear the badge. Dismiss (×) still hides the row entirely.
async function onItemClick(n: NotificationRow) {
  if (!n.read_at) {
    store.markRead(n.id).catch((e) => console.error('markRead failed', e))
  }
}

// Explicit navigation: fires when the user clicks the "open session" chip
// at the bottom of an item (or the mission chip for auto_needs_review).
// Also marks read so we don't leave a stale badge on a session the user
// just opened. Independent of onItemClick to prevent card-click from
// hijacking the user's attention.
async function openTargetFromItem(n: NotificationRow) {
  if (!n.read_at) {
    store.markRead(n.id).catch((e) => console.error('markRead failed', e))
  }
  // Supervisor's needs-review notif carries a mission_id in metadata —
  // deep-link straight into that mission's detail modal on the
  // automation page instead of the raw session, since the review verdict
  // is a mission-level concept and the mission modal surfaces the full
  // stage timeline the user needs to act on.
  const missionId = (n.metadata as any)?.mission_id
  if (n.type === 'auto_needs_review' && typeof missionId === 'string' && missionId) {
    router.push({ path: '/automation', query: { mission_id: missionId } })
    emit('close')
    return
  }
  if (!n.session_id) return
  // Verify the target session still exists — otherwise clicking a notif for
  // a purged session lands the user on "Select a session." with no visible
  // error, which reads like the click did nothing. HEAD-ish check via the
  // existing GET endpoint; 404 → toast + stay put.
  try {
    await sessionsApi.get(n.session_id)
  } catch (e: any) {
    if (e?.response?.status === 404) {
      toast.error('Session no longer exists (may have been purged).')
      return
    }
  }
  router.push(`/sessions/${n.session_id}`)
  emit('close')
}

// ---- Jump-to-unread (feedback local:NEW 2026-08-01) ------------------
// Users open the panel and immediately scan for unread rows; when the
// list is long the unread ones scroll off-screen. Wire a header button +
// j/k keyboard shortcuts that walk through unread items and scroll them
// into view. Doesn't mark-read on jump (user still has to click) so a
// glance-through doesn't wipe the badge.
const unreadRefs = ref<Map<string, HTMLElement>>(new Map())
function registerItemRef(id: string, el: Element | any) {
  if (el) unreadRefs.value.set(id, el as HTMLElement)
  else unreadRefs.value.delete(id)
}
const unreadItems = computed(() => items.value.filter((n) => !n.read_at))
// Focus cursor for j/k walk — reset when panel closes or list churns.
const jumpCursor = ref(-1)
function jumpToNextUnread() {
  if (!unreadItems.value.length) return
  jumpCursor.value = (jumpCursor.value + 1) % unreadItems.value.length
  scrollCursorIntoView()
}
function jumpToPrevUnread() {
  if (!unreadItems.value.length) return
  jumpCursor.value = jumpCursor.value <= 0
    ? unreadItems.value.length - 1
    : jumpCursor.value - 1
  scrollCursorIntoView()
}
function scrollCursorIntoView() {
  const target = unreadItems.value[jumpCursor.value]
  if (!target) return
  nextTick(() => {
    const el = unreadRefs.value.get(target.id)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    // Brief visual pulse so the eye knows where the cursor landed.
    el.classList.add('np-jump-pulse')
    setTimeout(() => el.classList.remove('np-jump-pulse'), 900)
  })
}
function handleKeydown(e: KeyboardEvent) {
  if (!props.open) return
  const tag = (e.target as HTMLElement | null)?.tagName || ''
  if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as any)?.isContentEditable) return
  if (e.metaKey || e.ctrlKey || e.altKey) return
  if (e.key === 'j' || e.key === 'J') {
    e.preventDefault(); jumpToNextUnread()
  } else if (e.key === 'k' || e.key === 'K') {
    e.preventDefault(); jumpToPrevUnread()
  } else if (e.key === 'Escape') {
    emit('close')
  }
}
watch(() => props.open, (open) => {
  if (open) {
    jumpCursor.value = -1
    window.addEventListener('keydown', handleKeydown)
  } else {
    window.removeEventListener('keydown', handleKeydown)
  }
})
onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))

async function clearAll() {
  if (clearing.value) return
  if (!store.items.length && store.totalUnread === 0) return
  if (!confirm('Clear ALL notifications and reset every session\'s unread count?')) return
  clearing.value = true
  try {
    // Store already does an optimistic local clear; the button state below
    // just guards against double-click during the network round-trip.
    await store.clearAll()
  } catch (e) {
    console.error('clear-all failed', e)
  } finally {
    clearing.value = false
  }
}
</script>

<template>
  <div v-if="open" class="np-backdrop" @click.self="emit('close')">
    <div class="np-panel">
      <div class="np-header">
        <h3 class="serif">Notifications</h3>
        <span class="np-count">{{ store.totalUnread }} unread</span>
        <button
          v-if="unreadItems.length"
          class="np-jump"
          @click="jumpToNextUnread"
          :title="`Jump to next unread (${unreadItems.length}) · press j / k to walk`"
        >↓ Unread</button>
        <button
          class="np-clear"
          @click="clearAll"
          :disabled="clearing || (!items.length && store.totalUnread === 0)"
          title="Clear all notifications + reset every session's unread"
        >{{ clearing ? 'Clearing…' : '🧹 Clear all' }}</button>
        <button class="np-close" @click="emit('close')">×</button>
      </div>
      <div class="np-subhead">
        <label class="np-desktop-toggle" :class="{ disabled: desktopToggleDisabled }">
          <input
            type="checkbox"
            :checked="desktopEnabled"
            :disabled="desktopToggleDisabled"
            @change="toggleDesktop"
          />
          <span>🖥 Desktop notifications</span>
        </label>
        <span
          class="np-desktop-hint"
          :class="{ warn: desktopState === 'insecure' || desktopState === 'denied' }"
        >{{ desktopHint }}</span>
      </div>
      <div v-if="!items.length" class="np-empty">No notifications yet.</div>
      <div v-else class="np-list">
        <div
          v-for="n in items"
          :key="n.id"
          :ref="(el) => registerItemRef(n.id, el)"
          class="np-item"
          :class="[severityClass(n.type, n.title), { unread: !n.read_at, clickable: !n.read_at }]"
          :title="!n.read_at ? 'Click to mark read' : ''"
          @click="onItemClick(n)"
        >
          <div class="np-item-head">
            <span class="type-tag" :class="severityClass(n.type, n.title)">{{ typeLabel(n.type, n.title) }}</span>
            <span v-if="(n.metadata as any)?.simulated" class="sim-tag" title="Simulated trigger">simulated</span>
            <span class="np-item-when">{{ fmtRel(n.created_at) }}</span>
            <button class="np-item-x" @click.stop="dismiss(n.id)" title="Dismiss">×</button>
          </div>
          <div v-if="n.title && n.body" class="np-item-title">{{ n.title }}</div>
          <div
            class="np-item-body"
            :class="{ collapsed: isLongBody(n.body) && !expanded[n.id] }"
          ><template v-for="(part, i) in bodyParts(n.body || n.title, n.metadata as any)" :key="i"><a
              v-if="'link' in part"
              class="np-body-link"
              href="javascript:void(0)"
              @click.stop="jumpToSession(part.sid)"
              :title="`Jump to session ${part.sid}`"
            >{{ part.link }}</a><span v-else>{{ part.text }}</span></template></div>
          <button
            v-if="isLongBody(n.body)"
            class="np-expand"
            @click.stop="toggle(n.id)"
          >{{ expanded[n.id] ? 'Collapse' : 'Show full report ▾' }}</button>
          <button
            v-if="n.session_id || (n.type === 'auto_needs_review' && (n.metadata as any)?.mission_id)"
            type="button"
            class="np-item-tag np-item-tag--link"
            @click.stop="openTargetFromItem(n)"
            :title="n.session_id
              ? `Open session ${((n.metadata as any)?.session_title) || n.session_id.slice(0,8)}`
              : 'Open mission'"
          >
            <template v-if="n.session_id">→ session {{ ((n.metadata as any)?.session_title) || n.session_id.slice(0,8) }}</template>
            <template v-else>→ mission</template>
          </button>
        </div>
        <div v-if="hasMore" class="np-more-hint">
          Showing {{ PANEL_ITEM_CAP }} most recent — older items truncated. Clear all or dismiss individually to see more.
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.np-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.25);
  z-index: 99;
}
.np-panel {
  position: absolute; top: 56px; right: 16px;
  width: 380px; max-height: 70vh;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.12);
  display: flex; flex-direction: column;
}
.np-header {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.np-header h3 { margin: 0; font-size: 15px; flex: 1; }
.np-subhead {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--canvas);
}
.np-desktop-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--ink); cursor: pointer;
}
.np-desktop-toggle.disabled { color: var(--ink-mute); cursor: not-allowed; }
.np-desktop-toggle input { margin: 0; cursor: inherit; }
.np-desktop-hint { margin-left: auto; font-size: 10px; color: var(--ink-mute); }
.np-desktop-hint.warn { color: var(--pastel-yellow-fg, #b8860b); }
.np-count {
  font-size: 11px; color: var(--ink-mute);
  padding: 1px 7px; background: var(--canvas);
  border-radius: 999px;
}
.np-clear {
  font-size: 11px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--canvas);
  color: var(--ink-mute);
  cursor: pointer;
  transition: color 120ms, border-color 120ms;
}
.np-clear:hover:not(:disabled) { color: var(--ink); border-color: var(--ink); }
/* Jump-to-unread — mirrors .np-clear shape but colored to signal action.
   Hidden when there are no unread rows. */
.np-jump {
  font-size: 11px;
  padding: 3px 8px;
  border: 1px solid var(--accent, #2563eb);
  border-radius: 5px;
  background: transparent;
  color: var(--accent, #2563eb);
  cursor: pointer;
  transition: background 120ms, color 120ms;
}
.np-jump:hover { background: var(--accent, #2563eb); color: #fff; }
/* Momentary highlight when j/k / button jumps to a row. */
@keyframes np-jump-pulse {
  0%   { box-shadow: 0 0 0 2px var(--accent, #2563eb); }
  100% { box-shadow: 0 0 0 0 transparent; }
}
.np-item.np-jump-pulse { animation: np-jump-pulse 900ms ease-out; }
.np-clear:disabled { opacity: 0.4; cursor: not-allowed; }
.np-close {
  background: transparent; border: none; cursor: pointer;
  font-size: 18px; color: var(--ink-mute); line-height: 1;
}
.np-close:hover { color: var(--ink); }
.np-empty {
  padding: 40px 20px; text-align: center;
  color: var(--ink-faint); font-style: italic; font-size: 13px;
}
.np-list { overflow-y: auto; flex: 1; }
.np-item {
  padding: 10px 14px 10px 12px;
  border-bottom: 1px solid var(--border);
  border-left: 3px solid transparent;
  font-size: 13px;
}
.np-item.unread { background: var(--canvas); }
.np-item.clickable { cursor: pointer; transition: filter 120ms; }
.np-item.clickable:hover { filter: brightness(0.97); }
.np-item:last-child { border-bottom: 0; }
/* Severity accents — left bar + subtle tint on unread */
.np-item.sev-warn    { border-left-color: var(--pastel-yellow-fg, #957024); }
.np-item.sev-err     { border-left-color: var(--pastel-red-fg, #C25450); }
.np-item.sev-attn    { border-left-color: var(--pastel-blue-fg, #4A6D8C); }
.np-item.sev-neutral { border-left-color: transparent; }
.np-item.sev-warn.unread    { background: color-mix(in srgb, var(--pastel-yellow-bg, #FCF6E4), var(--canvas) 40%); }
.np-item.sev-err.unread     { background: color-mix(in srgb, var(--pastel-red-bg, #FCEAE9), var(--canvas) 40%); }
.np-item.sev-attn.unread    { background: color-mix(in srgb, var(--pastel-blue-bg, #E8F0F7), var(--canvas) 40%); }

.np-item-head {
  display: flex; align-items: center; gap: 8px; margin-bottom: 4px;
}
.type-tag {
  display: inline-block; padding: 1px 8px; border-radius: 3px;
  font-size: 10.5px; font-weight: 600;
  border: 1px solid transparent;
}
.type-tag.sev-warn    { background: var(--pastel-yellow-bg, #FCF6E4); color: var(--pastel-yellow-fg, #957024); }
.type-tag.sev-err     { background: var(--pastel-red-bg, #FCEAE9);    color: var(--pastel-red-fg, #C25450); }
.type-tag.sev-attn    { background: var(--pastel-blue-bg, #E8F0F7);   color: var(--pastel-blue-fg, #4A6D8C); }
.type-tag.sev-neutral { background: var(--canvas); color: var(--ink-mute); border-color: var(--border); }
.sim-tag {
  padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;
  background: var(--pastel-purple-bg, #F0EAF7); color: var(--pastel-purple-fg, #7E57A6);
}
.np-body-link {
  color: var(--pastel-blue-fg, #4A6D8C); text-decoration: underline;
  font-family: var(--mono, monospace); font-size: 12px; cursor: pointer;
}
.np-body-link:hover { background: var(--pastel-blue-bg, #E8F0F7); }

.np-item-when { color: var(--ink-faint); font-size: 11px; font-family: 'Geist Mono', monospace; }
.np-item-x {
  margin-left: auto; background: transparent; border: none; cursor: pointer;
  color: var(--ink-faint); font-size: 14px; line-height: 1;
}
.np-item-x:hover { color: var(--ink); }
.np-item-title {
  color: var(--ink); font-weight: 600; margin-bottom: 3px; line-height: 1.4;
}
.np-item-body {
  color: var(--ink); line-height: 1.55; word-break: break-word;
  white-space: pre-wrap;
}
/* Collapsed clamp for agent reports — show ~3 lines. */
.np-item-body.collapsed {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.np-expand {
  margin-top: 6px; background: transparent; border: none; cursor: pointer;
  color: var(--pastel-blue-fg, #4A6D8C); font-size: 11.5px; padding: 2px 0;
  font-weight: 500;
}
.np-expand:hover { text-decoration: underline; }
.np-item-tag {
  margin-top: 4px; font-size: 10.5px; color: var(--ink-faint);
  font-family: 'Geist Mono', monospace;
}
/* Chip variant — explicit "→ open" affordance so users know this is
   the click-target for navigation. The parent .np-item card click only
   marks read, so this button is the sole navigation surface. */
.np-item-tag--link {
  display: inline-block; margin-top: 6px; padding: 3px 10px;
  background: transparent; color: var(--pastel-blue-fg, #4A6D8C);
  border: 1px solid var(--border); border-radius: 4px;
  cursor: pointer;
  transition: background 120ms, border-color 120ms, color 120ms;
}
.np-item-tag--link:hover {
  background: var(--pastel-blue-bg, #E8F0F7);
  border-color: var(--pastel-blue-fg, #4A6D8C);
  color: var(--pastel-blue-fg, #4A6D8C);
}
.np-more-hint {
  padding: 8px 14px;
  font-size: 11px;
  color: var(--ink-mute);
  background: var(--canvas);
  border-top: 1px dashed var(--border);
  text-align: center;
  font-style: italic;
}
</style>
