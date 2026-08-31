import { http } from './client'

export type Project = {
  id: string
  name: string
  description: string | null
  workflow_count: number
  archived_at: string | null
  created_at: string | null
  updated_at: string | null
}

export const projectsApi = {
  list: async (includeArchived = false): Promise<{ count: number; items: Project[] }> =>
    (await http.get('/api/projects', { params: { include_archived: includeArchived } })).data,
  create: async (body: {
    name: string
    description?: string | null
    workflow_names?: string[]
  }): Promise<Project & { bound_workflows: number }> =>
    (await http.post('/api/projects', body)).data,
  update: async (id: string, body: { name?: string; description?: string | null }): Promise<Project> =>
    (await http.patch(`/api/projects/${id}`, body)).data,
  archive: async (id: string): Promise<{ archived: true; id: string; archived_at: string }> =>
    (await http.post(`/api/projects/${id}/archive`)).data,
  absorb: async (
    id: string,
    workflow_names: string[],
  ): Promise<{ project_id: string; project_name: string; bound: number }> =>
    (await http.post(`/api/projects/${id}/absorb`, { workflow_names })).data,
}
