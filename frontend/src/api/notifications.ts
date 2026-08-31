import { http, pollGet, wsUrl } from './client'

export interface NotificationRow {
  id: string
  type: string
  session_id: string | null
  title: string
  body: string | null
  created_at: string | null
  read_at: string | null
  dismissed_at: string | null
  metadata: Record<string, unknown>
}

export const notificationsApi = {
  // pollGet: polled every few seconds — fast-fail 8s + fresh-connection retry
  // so a wedged tunnel connection doesn't hang the bell for 30s (see client.ts).
  list: async (params?: { limit?: number; only_unread?: boolean; include_dismissed?: boolean }) =>
    (await pollGet('/api/notifications', { params })).data as {
      count: number
      items: NotificationRow[]
    },
  unreadSummary: async () =>
    (await pollGet('/api/notifications/unread-summary')).data as {
      total_unread: number
      by_session: Record<string, number>
    },
  markRead: async (id: string) =>
    (await http.post(`/api/notifications/${id}/read`)).data,
  dismiss: async (id: string) =>
    (await http.post(`/api/notifications/${id}/dismiss`)).data,
  markSessionRead: async (sessionId: string) =>
    (await http.post(`/api/notifications/mark-session-read/${sessionId}`)).data,
  clearAll: async () =>
    (await http.post('/api/notifications/clear-all')).data as {
      notifications_cleared: number
      sessions_cleared: number
    },
  wsUrl: () => wsUrl('/api/notifications/ws'),
}
