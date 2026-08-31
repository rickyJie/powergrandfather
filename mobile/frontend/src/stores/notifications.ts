import { defineStore } from "pinia";
import { computed, ref } from "vue";
import {
  notificationsApi,
  normalizeNotification,
  type NotificationItem,
  type RawNotification,
  buildNotificationsWsUrl,
} from "@/api/notifications";

export const useNotificationsStore = defineStore("notifications", () => {
  const items = ref<NotificationItem[]>([]);
  const loading = ref(false);
  let ws: WebSocket | null = null;
  let pollTimer: number | null = null;

  const unread = computed(() => items.value.filter((n) => !n.read && !n.dismissed));
  const readOnly = computed(() => items.value.filter((n) => n.read && !n.dismissed));
  // Derive the badge from loaded items so it self-converges across clients:
  // any local mark-read / dismiss / incoming WS push recomputes it.
  const unreadCount = computed(() => unread.value.length);

  // Per-session unread new_message count — the phone's ONLY cross-session
  // "something happened over there" signal now that the bell is gone, so the
  // drawer renders it on the row it belongs to. Derived from `items` for the
  // same reason `unreadCount` is: every push / mark-read / dismiss recomputes
  // it and it can't drift from the list it is summarising.
  //
  // Counts ROWS, not messages, which is exactly what the server's own
  // `unread-summary.by_session` counts: the bus merges a burst of replies from
  // one session into a single row retitled "N new messages".
  const unreadBySession = computed<Record<string, number>>(() => {
    const out: Record<string, number> = {};
    for (const n of items.value) {
      if (n.read || n.dismissed) continue;
      if (n.type !== "new_message" || !n.session_id) continue;
      out[n.session_id] = (out[n.session_id] ?? 0) + 1;
    }
    return out;
  });

  async function refresh() {
    loading.value = true;
    try {
      const list = await notificationsApi.list(100);
      items.value = list.items;
    } finally {
      loading.value = false;
    }
  }

  async function markRead(nid: string) {
    // Optimistic update
    const item = items.value.find((n) => n.id === nid);
    if (!item || item.read) return;
    item.read = true;
    try {
      await notificationsApi.markRead(nid);
    } catch {
      item.read = false; // rollback
    }
  }

  async function dismiss(nid: string) {
    const idx = items.value.findIndex((n) => n.id === nid);
    if (idx === -1) return;
    const [removed] = items.value.splice(idx, 1);
    try {
      await notificationsApi.dismiss(nid);
    } catch {
      items.value.splice(idx, 0, removed); // rollback
    }
  }

  /** Clear a session's new-message notifications, locally then server-side.
   *
   *  Opening a conversation — and leaving it — is the read receipt. That used
   *  to be the bell's job; with the bell gone nothing else on the phone ever
   *  marks these read, so they would pile up here AND in the desktop panel
   *  forever — the exact badge noise this is meant to replace.
   *
   *  Local-first so the badge goes quiet on tap instead of a tunnel round-trip
   *  later. A failed POST is not rolled back: the server 404s only when the
   *  session row is gone, in which case there is nothing left to clear. */
  async function markSessionRead(sessionId: string) {
    for (const n of items.value) {
      if (n.session_id === sessionId && n.type === "new_message" && !n.read) {
        n.read = true;
      }
    }
    try {
      await notificationsApi.markSessionRead(sessionId);
    } catch {
      /* 404 = session purged; anything else the next refresh reconciles */
    }
  }

  /** Insert-or-replace by id (WS pushes may be new items or state updates). */
  function upsert(n: NotificationItem) {
    const idx = items.value.findIndex((x) => x.id === n.id);
    if (idx >= 0) items.value[idx] = n;
    else items.value.unshift(n);
  }

  /** Apply one in-app-sink push. Split out of `ws.onmessage` so the badge
   *  behaviour is reachable without standing up a WebSocket.
   *
   *  Deliberately does NOT auto-clear the chat that happens to be on screen.
   *  The Android app polls `/api/notifications?only_unread=true` every 20s and
   *  raises the tray notification from whatever is still unread — so anything
   *  that marks a row read from a PUSH races that poller and can silently eat
   *  the tray alert. Read receipts are therefore only ever sent from a user
   *  gesture (opening / leaving a chat), never from an incoming message.
   *  See ChatView.sendReadReceipt. */
  function ingestPush(raw: RawNotification) {
    if (!raw || !raw.id) return;
    upsert(normalizeNotification(raw));
  }

  function connectWs() {
    if (ws) return;
    try {
      ws = new WebSocket(buildNotificationsWsUrl());
      ws.onmessage = (ev) => {
        try {
          // The in-app sink broadcasts the raw flat notification object,
          // not an enveloped { type, item } message.
          ingestPush(JSON.parse(ev.data) as RawNotification);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        ws = null;
        // Fall back to polling if WS keeps dying
        if (!pollTimer) startPolling(60_000);
      };
      ws.onerror = () => {
        // onclose will fire next
      };
    } catch {
      ws = null;
    }
  }

  function disconnectWs() {
    ws?.close();
    ws = null;
  }

  function startPolling(intervalMs = 60_000) {
    stopPolling();
    pollTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, intervalMs);
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  return {
    items,
    loading,
    unreadCount,
    unreadBySession,
    unread,
    readOnly,
    refresh,
    markRead,
    markSessionRead,
    dismiss,
    ingestPush,
    connectWs,
    disconnectWs,
    startPolling,
    stopPolling,
  };
});
