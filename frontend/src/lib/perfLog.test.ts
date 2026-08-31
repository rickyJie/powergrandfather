import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createShipper, type PerfEntry } from './perfLog'

/**
 * The sink exists so a failure leaves a trace on the HOST, where it can be
 * correlated against the backend's own request log. Dropping a batch when the
 * POST fails defeats that exactly when it matters: the records worth reading
 * come from a tunnel collapse, and during a tunnel collapse this POST fails
 * too. perf.log 2026-08-25 has a 139s hole starting on the same second a list
 * refresh entered its retry — the one episode that produced a user-visible
 * banner is the one episode with no client-side record of it.
 */

function entry(req: string): PerfEntry {
  return {
    t: 1,
    surface: 'web',
    req,
    method: 'GET',
    url: '/api/sessions',
    status: null,
    err: 'timeout',
    total_ms: 8000,
    hidden: false,
    online: true,
    inflight: 3,
  }
}

const reqIds = (batch: PerfEntry[]) => batch.map((e) => e.req)

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('createShipper', () => {
  it('batches what it collects before the first flush', async () => {
    const post = vi.fn().mockResolvedValue(undefined)
    const s = createShipper(post)

    s.push(entry('r1'))
    s.push(entry('r2'))
    expect(post).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(2000)

    expect(post).toHaveBeenCalledTimes(1)
    expect(reqIds(post.mock.calls[0][0])).toEqual(['r1', 'r2'])
  })

  it('re-sends a batch the server never took, instead of dropping it', async () => {
    const post = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(undefined)
    const s = createShipper(post)

    s.push(entry('r1'))
    await vi.advanceTimersByTimeAsync(2000)
    expect(post).toHaveBeenCalledTimes(1)

    // Backed off to 4000ms, not dropped.
    await vi.advanceTimersByTimeAsync(4000)

    expect(post).toHaveBeenCalledTimes(2)
    expect(reqIds(post.mock.calls[1][0])).toEqual(['r1'])
  })

  it('backs off instead of hammering a dead tunnel', async () => {
    const post = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    const s = createShipper(post)

    s.push(entry('r1'))
    await vi.advanceTimersByTimeAsync(2000)   // attempt 1
    await vi.advanceTimersByTimeAsync(3999)
    expect(post).toHaveBeenCalledTimes(1)     // 4000ms backoff not elapsed yet
    await vi.advanceTimersByTimeAsync(1)
    expect(post).toHaveBeenCalledTimes(2)     // attempt 2
    await vi.advanceTimersByTimeAsync(7999)
    expect(post).toHaveBeenCalledTimes(2)     // now 8000ms
    await vi.advanceTimersByTimeAsync(1)
    expect(post).toHaveBeenCalledTimes(3)
  })

  it('keeps the records that arrived DURING the outage, ordered oldest first', async () => {
    const post = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(undefined)
    const s = createShipper(post)

    s.push(entry('during-outage-1'))
    await vi.advanceTimersByTimeAsync(2000)   // fails, batch re-queued
    s.push(entry('during-outage-2'))          // arrives while still down
    await vi.advanceTimersByTimeAsync(4000)

    expect(reqIds(post.mock.calls[1][0])).toEqual(['during-outage-1', 'during-outage-2'])
  })

  it('treats a 5xx as "not taken" — the caller rejects and the batch survives', async () => {
    // Wired the same way `ship` does: a non-ok response rejects.
    const seen: string[][] = []
    let fail = true
    const s = createShipper(async (batch) => {
      seen.push(reqIds(batch))
      if (fail) { fail = false; throw new Error('clientperf 503') }
    })

    s.push(entry('r1'))
    await vi.advanceTimersByTimeAsync(2000)
    await vi.advanceTimersByTimeAsync(4000)

    expect(seen).toEqual([['r1'], ['r1']])
  })

  it('resets the backoff once a send lands, so the next blip is not slow', async () => {
    const post = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(undefined)
    const s = createShipper(post)

    s.push(entry('r1'))
    await vi.advanceTimersByTimeAsync(2000)   // fail → backoff 4000
    await vi.advanceTimersByTimeAsync(4000)   // success → backoff back to 2000

    s.push(entry('r2'))
    await vi.advanceTimersByTimeAsync(2000)

    expect(post).toHaveBeenCalledTimes(3)
    expect(reqIds(post.mock.calls[2][0])).toEqual(['r2'])
  })

  it('stays a ring — a long outage cannot grow the queue without bound', async () => {
    let down = true
    const sizes: number[] = []
    const seen: string[] = []
    const s = createShipper(async (batch) => {
      sizes.push(batch.length)
      if (down) throw new TypeError('Failed to fetch')
      seen.push(...reqIds(batch))
    })
    for (let i = 0; i < 500; i++) s.push(entry(`r${i}`))

    await vi.advanceTimersByTimeAsync(2000) // attempt 1, still down
    down = false
    await vi.advanceTimersByTimeAsync(4000) // recovered — first batch lands
    await vi.advanceTimersByTimeAsync(2000) // and it keeps draining on its own

    // Every POST stays inside the keepalive body budget.
    expect(Math.max(...sizes)).toBeLessThanOrEqual(100)
    // 200-deep ring: the oldest 300 never made it in. What IS held drains in
    // arrival order once the tunnel is back, without another push to kick it.
    expect(seen).toHaveLength(200)
    expect(seen[0]).toBe('r300')
    expect(seen[199]).toBe('r499')
  })
})
