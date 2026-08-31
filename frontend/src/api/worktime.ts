import { http, pollGet } from './client'

export type WorktimeLive = {
  today_human_sec: number
  today_agent_sec: number
  all_human_sec: number
  all_agent_sec: number
  open_agent_sec: number
  open_human_sec: number
  open_agent_count: number
  day_bucket_utc: string
}

export type WorktimeHeartbeatResponse = {
  open_row_id: string | null
  reopened: boolean
  last_seen_ts: string
}

export const worktimeApi = {
  live: async (): Promise<WorktimeLive> =>
    // pollGet: 8s fast-fail + fresh-connection retry (worktime/live was the
    // most frequent tunnel-wedge timeout victim — 46 hits in perf.log).
    (await pollGet<WorktimeLive>('/api/worktime/live')).data,
  heartbeat: async (): Promise<WorktimeHeartbeatResponse> =>
    (await http.post('/api/worktime/heartbeat')).data,
}
