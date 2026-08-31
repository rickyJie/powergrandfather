import { http } from './client'

export interface FsEntry { name: string; path: string; is_dir: boolean }
export interface BrowseResp { path: string; parent: string | null; entries: FsEntry[] }

export const fsApi = {
  browse: async (path: string) => (await http.get('/api/fs/browse', { params: { path } })).data as BrowseResp,
  recentCwds: async (limit = 10) => (await http.get('/api/fs/recent-cwds', { params: { limit } })).data as { items: string[] },
}
