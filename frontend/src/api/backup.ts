import { http } from './client'

export interface BackupEntry {
  name: string
  size_bytes: number
  created_at: string
}

export interface BackupListResp {
  backups: BackupEntry[]
  count: number
  total_bytes: number
  max_backups: number
  backup_dir: string
}

export interface BackupCreateResp extends BackupEntry {
  workflow_count: number
  alembic_head: string | null
}

export const backupApi = {
  list: async (): Promise<BackupListResp> =>
    (await http.get('/api/backup/list')).data,
  create: async (note: string = ''): Promise<BackupCreateResp> =>
    (await http.post('/api/backup/create', null, { params: { note } })).data,
  remove: async (name: string): Promise<{ name: string; deleted: boolean }> =>
    (await http.delete(`/api/backup/${encodeURIComponent(name)}`)).data,
  downloadUrl: (name: string): string =>
    `/api/backup/download/${encodeURIComponent(name)}`,
}
