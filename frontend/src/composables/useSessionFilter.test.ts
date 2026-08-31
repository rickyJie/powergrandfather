import { ref } from 'vue'
import { beforeEach, describe, expect, it } from 'vitest'
import type { SessionRow } from '../api/sessions'
import { useSessionFilter } from './useSessionFilter'

function row(overrides: Partial<SessionRow>): SessionRow {
  return {
    id: 'session-1',
    title: 'Review checkout',
    type: 'interactive',
    cwd: '/work/shop',
    status: 'idle',
    pid: 123,
    started_at: '2026-07-31T00:00:00Z',
    ended_at: null,
    exit_code: null,
    external_session_id: 'external-1',
    claude_session_id: 'external-1',
    superseded_by: null,
    associated_run_id: null,
    tags: [],
    last_activity_ts: '2026-07-31T01:00:00Z',
    unread_count: 0,
    current_tool: null,
    last_assistant_msg: 'Updated the payment flow',
    session_project_id: null,
    agent: 'claude',
    ...overrides,
  }
}

describe('useSessionFilter', () => {
  beforeEach(() => localStorage.clear())

  it('searches title, cwd and assistant text across tabs', () => {
    const rows = ref([
      row({ id: 'a' }),
      row({ id: 'b', title: 'Other', cwd: '/srv/api', last_assistant_msg: 'database migration' }),
    ])
    const filter = useSessionFilter(rows, ref([]))

    filter.searchQuery.value = 'srv api'
    expect(filter.searchResults.value.map((item) => item.id)).toEqual(['b'])
    filter.searchQuery.value = 'payment'
    expect(filter.searchResults.value.map((item) => item.id)).toEqual(['a'])
  })

  it('supports structured agent, status and project terms', () => {
    const rows = ref([
      row({ id: 'claude', session_project_id: 'p1' }),
      row({ id: 'codex', agent: 'codex', status: 'running', session_project_id: 'p2' }),
    ])
    const projects = ref([
      {
        id: 'p1', name: 'Storefront', description: null, session_count: 1,
        archived_at: null, created_at: null, updated_at: null,
      },
      {
        id: 'p2', name: 'Platform', description: null, session_count: 1,
        archived_at: null, created_at: null, updated_at: null,
      },
    ])
    const filter = useSessionFilter(rows, projects)

    filter.searchQuery.value = 'agent:codex status:running project:platform'
    expect(filter.searchResults.value.map((item) => item.id)).toEqual(['codex'])
  })

  it('hides archived history until explicitly requested', () => {
    const rows = ref([
      row({ id: 'visible', status: 'exited', ended_at: '2026-07-31T02:00:00Z' }),
      row({
        id: 'archived',
        status: 'exited',
        ended_at: '2026-07-31T03:00:00Z',
        archived_at: '2026-07-31T04:00:00Z',
      }),
    ])
    const filter = useSessionFilter(rows, ref([]), { initialFilter: 'history' })

    expect(filter.visibleRows.value.map((item) => item.id)).toEqual(['visible'])
    filter.showArchived.value = true
    expect(filter.visibleRows.value.map((item) => item.id)).toEqual(['visible', 'archived'])
  })
})
