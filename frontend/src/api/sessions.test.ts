import { describe, expect, it } from 'vitest'
import type { SessionRow } from './sessions'
import { normalizeSessionListResponse } from './sessions'

const item = { id: 'session-1' } as SessionRow

describe('normalizeSessionListResponse', () => {
  it('preserves modern offset pagination metadata', () => {
    expect(normalizeSessionListResponse({
      count: 12,
      page_count: 1,
      offset: 5,
      has_more: true,
      items: [item],
    })).toEqual({
      count: 12,
      page_count: 1,
      offset: 5,
      has_more: true,
      items: [item],
      legacy_pagination: false,
    })
  })

  it('marks count/items-only responses as legacy without inventing an offset page', () => {
    expect(normalizeSessionListResponse({
      count: 1,
      items: [item],
    }, 75)).toEqual({
      count: 1,
      page_count: 1,
      offset: 75,
      has_more: false,
      items: [item],
      legacy_pagination: true,
    })
  })

  it('never reports a total smaller than the returned snapshot', () => {
    const result = normalizeSessionListResponse({
      count: 0,
      items: [item],
    })
    expect(result.count).toBe(1)
  })
})
