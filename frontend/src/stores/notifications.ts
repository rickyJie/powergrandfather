import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { notificationsApi, type NotificationRow } from '../api/notifications'
import { maybeDesktopNotify } from '../lib/desktopNotify'
import { TickRecorder, reportWsClose, visState } from '../lib/wsDiag'
import { isConnectionDead } from '../lib/wsLiveness'

// Cross-tab notification coordination (D1). Read/dismiss/clear actions are
// plain HTTP on the backend and are NOT broadcast over the notification WS —
// so without this, tab A marking something read leaves tab B showing a
// "ghost unread" (stale bell count + panel rows) until B happens to receive
// an unrelated push or reconnects. A same-origin BroadcastChannel lets every
// tab mirror another tab's mutation locally (no extra API call) and then
// reconcile counts against the server via scheduleSummarySync. Guarded for
// environments without BroadcastChannel (SSR / very old browsers).
type NotifBroadcast =
  | { kind: 'read', id: string }
  | { kind: 'dismiss', id: string }
  | { kind: 'session_read', sessionId: string }
  | { kind: 'clear_all' }

export const useNotificationsStore = defineStore('notifications', () => {
  const items = ref<NotificationRow[]>([])
  const totalUnread = ref<number>(0)
  let ws: WebSocket | null = null
  // Exponential-backoff reconnect for the notification WS (mirrors the SSE
  // strategy in useEventStream). The old fixed 2s reconnect meant that when
  // the backend was down every tab hammered a reconnect every 2s with no
  // ceiling — N tabs * every 2s forever. Now: 1s → 2s → … capped at 30s with
  // jitter to avoid a thundering herd when the backend restarts.
  let wsReconnectTimer: number | null = null
  let wsReconnectAttempt = 0
  const WS_RECONNECT_BASE_MS = 1000
  const WS_RECONNECT_MAX_MS = 30_000
  // App-level heartbeat for the notification WS (mirrors the terminal WS). The
  // server pushes only sporadically, so a silently-dropped idle connection
  // could sit OPEN-but-dead for a long time; ping while idle and force a
  // reconnect if no frame (push or pong) arrives within the liveness window.
  // Monotonic (performance.now) throughout — Date.now() is wall-clock and
  // jumps on an NTP correction or a suspend/resume, which on its own is
  // enough to make a liveness check fire against a healthy socket.
  let wsPingTimer: number | null = null
  let wsLastInboundTs = 0
  let wsLastPingSentTs = 0
  const WS_PING_INTERVAL_MS = 25_000
  // How long a ping may go unanswered before we call the socket dead. Only
  // consulted once a ping we actually sent has gone unanswered, so a late
  // tick cannot turn it into a false positive. See startWsPing.
  const WS_PONG_GRACE_MS = 10_000
  // wsDiag (temporary, 2026-08-24): observation only — nothing below reads
  // this to decide anything. See lib/wsDiag.ts.
  let wsClosedByWatchdog = false
  const wsTickRec = new TickRecorder()
  function stopWsPing() {
    if (wsPingTimer != null) { window.clearInterval(wsPingTimer); wsPingTimer = null }
  }
  function startWsPing(sock: WebSocket) {
    stopWsPing()
    wsLastInboundTs = performance.now()
    wsLastPingSentTs = 0
    wsTickRec.start()
    wsPingTimer = window.setInterval(() => {
      wsTickRec.tick()
      if (ws !== sock || sock.readyState !== WebSocket.OPEN) { stopWsPing(); return }
      // Liveness by PAIRING, not by deadline — see the long note in
      // useTerminalManager.startPing. Short version: "no frame in 55s"
      // measures elapsed wall time with a clock the browser stalls for hidden
      // tabs, so the first throttled tick killed a healthy socket without
      // ever probing. Comparing "did the ping I sent get answered" delays
      // both halves together and cannot false-positive.
      if (isConnectionDead({
        lastInboundTs: wsLastInboundTs,
        lastPingSentTs: wsLastPingSentTs,
        now: performance.now(),
        graceMs: WS_PONG_GRACE_MS,
      })) {
        wsClosedByWatchdog = true
        stopWsPing()
        try { sock.close() } catch (_) { /* onclose reconnects */ }
        return
      }
      try {
        sock.send(JSON.stringify({ type: 'ping' }))
        wsLastPingSentTs = performance.now()
      } catch (_) { /* ignore */ }
    }, WS_PING_INTERVAL_MS)
  }

  const bc: BroadcastChannel | null =
    typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('csm-notif') : null
  function broadcast(msg: NotifBroadcast) {
    try { bc?.postMessage(msg) } catch (_) { /* ignore */ }
  }

  // Session id that Sessions.vue is currently displaying. Sessions.vue
  // syncs this on mount / activeSid change / unmount. When a NEW_MESSAGE
  // notif for this session arrives via WS AND the tab is visible, we
  // treat it as "already seen" and mark-read on arrival — otherwise the
  // bell red-dot lingers even though the user is staring right at the
  // conversation. `markSessionRead` on watch(activeSid) only clears
  // notifs that existed at the time of navigation; this closes the
  // "notif born after user is already in the session" gap.
  const activeSessionId = ref<string | null>(null)
  function setActiveSessionId(sid: string | null) {
    activeSessionId.value = sid
  }

  // Exact server-side counts; unlike deriving from the 100-row bell panel,
  // this remains correct for a noisy session with older unread messages.
  const unreadBySession = ref<Record<string, number>>({})
  function unreadForSession(sid: string): number {
    return unreadBySession.value[sid] ?? 0
  }

  // True while ANY session has an unread "Permission required" notif in the
  // bell panel — used by the topbar bell dot and useTabBadge to give the
  // "someone is blocked" state a distinct visual (orange pulse / `[!]`
  // tab prefix) so the fullscreen user, whose only cross-session channel
  // IS the bell / tab title, can spot a blocked peer without expanding
  // the panel. Derived from the 100-row store — good enough because a
  // pending permission always sits at the top of the recent window; if
  // the user has 100+ unread notifs they have bigger problems.
  const hasPendingPermission = computed(() => items.value.some(
    (n) => n.type === 'auto_needs_review'
      && n.title === 'Permission required'
      && !n.read_at
      && !n.dismissed_at,
  ))

  async function refresh() {
    const { items: rows } = await notificationsApi.list({ limit: 100, include_dismissed: false })
    items.value = rows
    const summary = await notificationsApi.unreadSummary()
    totalUnread.value = summary.total_unread
    unreadBySession.value = summary.by_session || {}
  }

  // Debounced re-fetch of unreadSummary so a burst of merge-pushes (backend
  // re-pushes the same NEW_MESSAGE notif on every assistant-done inside the
  // 5s dedup window) coalesces into one round-trip and totalUnread stays in
  // sync with the server truth instead of drifting from local increments.
  let summaryTimer: number | null = null
  function scheduleSummarySync() {
    if (summaryTimer != null) return
    summaryTimer = window.setTimeout(async () => {
      summaryTimer = null
      try {
        const s = await notificationsApi.unreadSummary()
        totalUnread.value = s.total_unread
        unreadBySession.value = s.by_session || {}
      } catch (_) { /* ignore */ }
    }, 300)
  }

  // ── Optimistic "forward" mutations, shared by the user-initiated actions
  // (which then hit the API + roll back on failure) and the cross-tab
  // BroadcastChannel handler (which applies the same local change with no
  // API call and lets scheduleSummarySync reconcile counts). Extracting
  // them keeps the two callers from drifting.

  function _forwardMarkRead(id: string): { sessionId: string | null } | null {
    const idx = items.value.findIndex(x => x.id === id)
    if (idx >= 0 && !items.value[idx].read_at) {
      const row = items.value[idx]
      items.value[idx] = { ...items.value[idx], read_at: new Date().toISOString() }
      totalUnread.value = Math.max(0, totalUnread.value - 1)
      const sessionId = (row.type === 'new_message' && row.session_id) ? row.session_id : null
      if (sessionId) {
        const next = { ...unreadBySession.value }
        next[sessionId] = Math.max(0, (next[sessionId] ?? 0) - 1)
        if (!next[sessionId]) delete next[sessionId]
        unreadBySession.value = next
      }
      return { sessionId }
    }
    return null
  }

  function _forwardDismiss(id: string): { wasUnread: boolean, dropped: NotificationRow } | null {
    const dropped = items.value.find(x => x.id === id)
    if (!dropped) return null
    const wasUnread = !dropped.read_at
    items.value = items.value.filter(x => x.id !== id)
    if (wasUnread) totalUnread.value = Math.max(0, totalUnread.value - 1)
    if (wasUnread && dropped.type === 'new_message' && dropped.session_id) {
      const next = { ...unreadBySession.value }
      next[dropped.session_id] = Math.max(0, (next[dropped.session_id] ?? 0) - 1)
      if (!next[dropped.session_id]) delete next[dropped.session_id]
      unreadBySession.value = next
    }
    return { wasUnread, dropped }
  }

  function _forwardSessionRead(sessionId: string): { priorSessionUnread: number, clearedIds: string[] } {
    const priorSessionUnread = unreadBySession.value[sessionId] ?? 0
    if (priorSessionUnread) {
      const next = { ...unreadBySession.value }
      delete next[sessionId]
      unreadBySession.value = next
    }
    const now = new Date().toISOString()
    const clearedIds: string[] = []
    items.value = items.value.map(x => {
      if (x.session_id === sessionId && !x.read_at) {
        clearedIds.push(x.id)
        return { ...x, read_at: now }
      }
      return x
    })
    if (priorSessionUnread) {
      totalUnread.value = Math.max(0, totalUnread.value - priorSessionUnread)
    }
    return { priorSessionUnread, clearedIds }
  }

  function connect() {
    if (ws && ws.readyState <= 1) return
    ws = new WebSocket(notificationsApi.wsUrl())
    const sock = ws
    ws.onopen = () => {
      wsReconnectAttempt = 0
      startWsPing(sock)
      // Resync from server on every (re)connect — the socket may have been
      // silent for seconds/minutes and any pushes during that window are
      // gone. Without this the badge sits stale after a network hiccup or
      // suspend/resume until the user manually reloads.
      refresh().catch(() => { /* non-fatal — WS pushes will still arrive */ })
    }
    ws.onmessage = (ev) => {
      wsLastInboundTs = performance.now()
      try {
        const raw = JSON.parse(ev.data) as { type?: string }
        // Heartbeat pong — liveness already refreshed; not a notification row.
        if (raw && raw.type === 'pong') return
        const n = raw as NotificationRow
        // Backend merges NEW_MESSAGE notifs inside a dedup window and re-pushes
        // the same id. Dedupe in-place so the panel doesn't accumulate stale
        // duplicates and totalUnread doesn't inflate past the server truth.
        const idx = items.value.findIndex(x => x.id === n.id)
        const prevReadAt = idx >= 0 ? items.value[idx].read_at : null
        if (idx >= 0) {
          const prev = items.value[idx]
          items.value[idx] = n
          // Re-push recovery (P2-5): a merged re-push can flip a row that was
          // already read back to unread (read_at cleared server-side). When
          // that happens we must re-add the per-session count — otherwise the
          // sidebar per-session red dot silently drops until the 300ms summary
          // sync backfills it. Only the count matters here; totalUnread is
          // reconciled by scheduleSummarySync below.
          if (
            n.type === 'new_message'
            && n.session_id
            && !n.read_at
            && !n.dismissed_at
            && prev.read_at
          ) {
            unreadBySession.value = {
              ...unreadBySession.value,
              [n.session_id]: (unreadBySession.value[n.session_id] ?? 0) + 1,
            }
          }
        } else {
          items.value.unshift(n)
          if (
            n.type === 'new_message'
            && n.session_id
            && !n.read_at
            && !n.dismissed_at
          ) {
            unreadBySession.value = {
              ...unreadBySession.value,
              [n.session_id]: (unreadBySession.value[n.session_id] ?? 0) + 1,
            }
          }
        }
        // Trust the server for the count instead of local +=1 (which
        // double-counted on merges).
        scheduleSummarySync()
        // Auto-clear NEW_MESSAGE notifs the user is already looking at —
        // Sessions.vue set activeSessionId when the user navigated into
        // this session; if the tab is visible AND focused we know they're
        // staring at the terminal and the red dot would just be noise. Only
        // apply to new_message (session-crashed / port-conflict etc. are
        // bell-only surfaces the user still wants to acknowledge explicitly).
        //
        // Same treatment for "Permission required" AUTO_NEEDS_REVIEW notifs
        // targeting the active session: claude's own in-terminal prompt is
        // already staring the user in the face, so the bell bump is pure
        // duplicate noise (especially painful in fullscreen, where the bell
        // is the ONLY out-of-terminal signal and the user has zero reason
        // to be told about a request they're literally answering). Panel
        // history still shows the row so retrospection isn't lost.
        //
        // D2: gate on document.hasFocus() as well as !document.hidden. A tab
        // can be *visible but not focused* (second monitor, split-screen,
        // user working in another app) — `document.hidden` is false in all of
        // those, so the old gate silently marked messages read the user never
        // actually looked at, and the red dot never appeared. Requiring focus
        // means "visible AND the user's attention is here" before auto-read.
        if (
          typeof document !== 'undefined'
          && !document.hidden
          && document.hasFocus()
          && n.session_id
          && n.session_id === activeSessionId.value
          && !n.read_at
          && (
            n.type === 'new_message'
            || (n.type === 'auto_needs_review' && n.title === 'Permission required')
          )
        ) {
          markRead(n.id).catch(() => { /* best effort */ })
        }
        // Desktop OS notification for a genuinely-new unread (brand-new row, or
        // a re-push that flipped read→unread). No-op unless enabled + granted +
        // secure context + high-signal + app unfocused — so it never fires when
        // the user is looking (the auto-read above owns that case).
        if (!n.read_at && !n.dismissed_at && (idx < 0 || prevReadAt)) {
          void maybeDesktopNotify(n)
        }
      } catch (_) { /* ignore */ }
    }
    ws.onclose = (ev) => {
      // wsDiag (temporary): see lib/wsDiag.ts.
      reportWsClose({
        ch: 'notif',
        by: wsClosedByWatchdog ? 'watchdog' : 'other',
        code: ev.code,
        clean: ev.wasClean,
        sinceInbound: wsLastInboundTs ? Math.round(performance.now() - wsLastInboundTs) : -1,
        sincePing: wsLastPingSentTs ? Math.round(performance.now() - wsLastPingSentTs) : -1,
        gaps: wsTickRec.recent(),
        vis: visState(),
      })
      wsClosedByWatchdog = false
      stopWsPing()
      if (wsReconnectTimer != null) return
      const backoff = Math.min(WS_RECONNECT_BASE_MS * 2 ** wsReconnectAttempt, WS_RECONNECT_MAX_MS)
      const jitter = Math.floor(Math.random() * 250)
      wsReconnectAttempt += 1
      wsReconnectTimer = window.setTimeout(() => {
        wsReconnectTimer = null
        connect()
      }, backoff + jitter)
    }
  }

  // Cross-tab mirror: another tab performed a read/dismiss/clear. Apply the
  // same local change here (no API call, no re-broadcast — the originating
  // tab already hit the server) and let scheduleSummarySync reconcile counts
  // against the authoritative server truth.
  if (bc) {
    bc.onmessage = (ev: MessageEvent<NotifBroadcast>) => {
      const msg = ev.data
      try {
        switch (msg?.kind) {
          case 'read': _forwardMarkRead(msg.id); break
          case 'dismiss': _forwardDismiss(msg.id); break
          case 'session_read': _forwardSessionRead(msg.sessionId); break
          case 'clear_all':
            items.value = []
            totalUnread.value = 0
            unreadBySession.value = {}
            break
        }
      } catch (_) { /* ignore */ }
      scheduleSummarySync()
    }
  }

  // Mutating actions optimistically update local state BEFORE the API
  // round-trip returns, so the UI never sits idle waiting for the server.
  //
  // Rollback semantics (P1 #10 / P2 #13):
  //   - We CANNOT just snapshot `totalUnread` and restore on failure — a WS
  //     push during the in-flight window would have legitimately bumped the
  //     count, and a snapshot-based rollback would erase that bump.
  //   - We CANNOT just snapshot `items` and restore on failure — same problem:
  //     WS pushes during the window unshift new rows into the *live* array;
  //     snapshot restore would drop them.
  //   - Instead: track the DELTA we applied optimistically (how many we
  //     decremented, which ids we marked read / removed) and on failure
  //     invert the delta on the CURRENT state, not the snapshot. That way
  //     concurrent WS changes are preserved.

  async function markRead(id: string) {
    const undo = _forwardMarkRead(id)
    broadcast({ kind: 'read', id })
    try {
      await notificationsApi.markRead(id)
      scheduleSummarySync()
    } catch (e) {
      // Invert on current state: mark unread again + add back the 1 we took,
      // including the per-session count (previously only read_at + totalUnread
      // were restored, leaving unreadBySession skewed low until the debounce).
      const cur = items.value.findIndex(x => x.id === id)
      if (cur >= 0) items.value[cur] = { ...items.value[cur], read_at: null }
      if (undo) {
        totalUnread.value += 1
        if (undo.sessionId) {
          unreadBySession.value = {
            ...unreadBySession.value,
            [undo.sessionId]: (unreadBySession.value[undo.sessionId] ?? 0) + 1,
          }
        }
      }
      scheduleSummarySync()
      throw e
    }
  }

  async function dismiss(id: string) {
    const undo = _forwardDismiss(id)
    if (!undo) {
      try { await notificationsApi.dismiss(id) } catch (e) { throw e }
      return
    }
    broadcast({ kind: 'dismiss', id })
    try {
      await notificationsApi.dismiss(id)
      scheduleSummarySync()
    } catch (e) {
      // Put the row back at its original head-ish position (unshift is
      // close enough; the exact ordering will re-sync on the next refresh).
      if (!items.value.some(x => x.id === id)) items.value.unshift(undo.dropped)
      if (undo.wasUnread) {
        totalUnread.value += 1
        if (undo.dropped.type === 'new_message' && undo.dropped.session_id) {
          unreadBySession.value = {
            ...unreadBySession.value,
            [undo.dropped.session_id]: (unreadBySession.value[undo.dropped.session_id] ?? 0) + 1,
          }
        }
      }
      scheduleSummarySync()
      throw e
    }
  }

  async function markSessionRead(sessionId: string) {
    const { priorSessionUnread, clearedIds } = _forwardSessionRead(sessionId)
    broadcast({ kind: 'session_read', sessionId })
    try {
      await notificationsApi.markSessionRead(sessionId)
      scheduleSummarySync()
    } catch (e) {
      // Invert the delta on the current state — leaves any WS-arrived rows
      // in place instead of clobbering them with a stale snapshot.
      const idSet = new Set(clearedIds)
      items.value = items.value.map(x => idSet.has(x.id) ? { ...x, read_at: null } : x)
      if (priorSessionUnread) totalUnread.value += priorSessionUnread
      if (priorSessionUnread) {
        unreadBySession.value = {
          ...unreadBySession.value,
          [sessionId]: priorSessionUnread,
        }
      }
      throw e
    }
  }

  async function clearAll() {
    // For a batch clear, delta bookkeeping isn't tractable (we'd have to
    // remember every id + prior read_at). Instead: keep the optimistic empty
    // for instant UX, and on failure refetch server truth — which is safer
    // than restoring a snapshot that would drop WS-arrived rows.
    items.value = []
    totalUnread.value = 0
    unreadBySession.value = {}
    broadcast({ kind: 'clear_all' })
    // Always resync from server after the round-trip — even on success —
    // to pick up any WS pushes that arrived during the in-flight window.
    // Without this, a new_message that landed while clearAll was mid-flight
    // would show as an unread row in the panel with totalUnread=0 briefly.
    scheduleSummarySync()
    try {
      const r = await notificationsApi.clearAll()
      // Also refresh items — the server truth may include rows that WS
      // delivered during the round-trip that we optimistically cleared.
      refresh().catch(() => { /* non-fatal */ })
      return r
    } catch (e) {
      // Rebuild from server so we recover any rows that arrived via WS
      // during the failed round-trip. Best-effort — if refresh also fails
      // the user's next interaction will re-fetch anyway.
      try { await refresh() } catch (_) { /* leave empty; will recover on next tick */ }
      throw e
    }
  }

  return {
    items,
    totalUnread,
    unreadBySession,
    unreadForSession,
    hasPendingPermission,
    refresh,
    connect,
    markRead,
    dismiss,
    markSessionRead,
    clearAll,
    setActiveSessionId,
  }
})
