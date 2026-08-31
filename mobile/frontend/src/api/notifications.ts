import { http } from "./client";

/** Raw wire shape from GET /api/notifications and the WS broadcast. */
export interface RawNotification {
  id: string;
  type: string;
  session_id: string | null;
  title: string;
  body: string;
  created_at: string;
  read_at: string | null;
  dismissed_at: string | null;
  metadata?: Record<string, unknown>;
}

/** Normalized shape the mobile store/views consume. */
export interface NotificationItem {
  id: string;
  type: string;
  session_id: string | null;
  title: string;
  body: string;
  ts: string; // = created_at
  read: boolean; // = read_at != null
  dismissed: boolean; // = dismissed_at != null
  metadata?: Record<string, unknown>;
}

export function normalizeNotification(raw: RawNotification): NotificationItem {
  return {
    id: raw.id,
    type: raw.type,
    session_id: raw.session_id,
    title: raw.title,
    body: raw.body,
    ts: raw.created_at,
    read: raw.read_at != null,
    dismissed: raw.dismissed_at != null,
    metadata: raw.metadata,
  };
}

export interface UnreadSummary {
  total: number; // = total_unread
  by_session?: Record<string, number>;
}

export const notificationsApi = {
  list: async (limit = 50, offset = 0) => {
    const data = (
      await http.get("/api/notifications", { params: { limit, offset } })
    ).data as { count: number; items: RawNotification[] };
    return {
      count: data.count,
      items: (data.items ?? []).map(normalizeNotification),
    };
  },

  unreadSummary: async (): Promise<UnreadSummary> => {
    const data = (await http.get("/api/notifications/unread-summary")).data as {
      total_unread: number;
      by_session?: Record<string, number>;
    };
    return { total: data.total_unread ?? 0, by_session: data.by_session };
  },

  markRead: async (nid: string) =>
    (await http.post(`/api/notifications/${nid}/read`)).data,

  dismiss: async (nid: string) =>
    (await http.post(`/api/notifications/${nid}/dismiss`)).data,

  markSessionRead: async (sessionId: string) =>
    (await http.post(`/api/notifications/mark-session-read/${sessionId}`)).data,

  clearAll: async () => (await http.post("/api/notifications/clear-all")).data,
};

export function buildNotificationsWsUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  const token = (() => {
    try {
      return localStorage.getItem("csm_access_token");
    } catch {
      return null;
    }
  })();
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${scheme}://${host}/api/notifications/ws${qs}`;
}
