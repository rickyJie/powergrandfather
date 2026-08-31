import { http } from './client'

// SessionProject — user-managed bucket for grouping interactive sessions.
// Separate from workflow.Project (which groups authoring templates). See
// backend/csm/models/session_project.py.
export type SessionProject = {
  id: string
  name: string
  description: string | null
  session_count: number
  archived_at: string | null
  created_at: string | null
  updated_at: string | null
}

export const sessionProjectsApi = {
  list: async (includeArchived = false): Promise<{ items: SessionProject[] }> =>
    (await http.get('/api/session-projects', { params: { include_archived: includeArchived } })).data,
  create: async (body: { name: string; description?: string | null }): Promise<SessionProject> =>
    (await http.post('/api/session-projects', body)).data,
  update: async (id: string, body: { name?: string; description?: string | null }): Promise<SessionProject> =>
    (await http.patch(`/api/session-projects/${id}`, body)).data,
  archive: async (id: string): Promise<SessionProject & { sessions_unassigned: number }> =>
    (await http.post(`/api/session-projects/${id}/archive`)).data,
  unarchive: async (id: string): Promise<SessionProject> =>
    (await http.post(`/api/session-projects/${id}/unarchive`)).data,
  remove: async (id: string): Promise<{ deleted: string; sessions_unassigned: number }> =>
    (await http.delete(`/api/session-projects/${id}`)).data,
}
