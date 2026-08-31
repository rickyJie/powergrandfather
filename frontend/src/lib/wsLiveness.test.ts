import { describe, it, expect } from 'vitest'
import { isConnectionDead } from './wsLiveness'

const GRACE = 10_000

describe('isConnectionDead', () => {
  it('never judges before a probe has been sent', () => {
    expect(isConnectionDead({ lastInboundTs: 0, lastPingSentTs: 0, now: 500_000, graceMs: GRACE }))
      .toBe(false)
  })

  it('treats an answered ping as alive on a normal tick', () => {
    // ping at 20_000, pong 40ms later, tick at 40_000
    expect(isConnectionDead({ lastInboundTs: 20_040, lastPingSentTs: 20_000, now: 40_000, graceMs: GRACE }))
      .toBe(false)
  })

  it('REGRESSION: a healthy socket survives a badly delayed tick', () => {
    // The bug we shipped for months: hidden-tab throttling delayed the tick to
    // ~60s, the old deadline rule saw 60s of silence and closed a socket whose
    // pong had come back 40ms after the ping. Elapsed time must not matter
    // once the outstanding ping has been answered.
    expect(isConnectionDead({ lastInboundTs: 20_040, lastPingSentTs: 20_000, now: 80_000, graceMs: GRACE }))
      .toBe(false)
    // Even an extreme stall (suspend/resume, 10 minutes) is still alive.
    expect(isConnectionDead({ lastInboundTs: 20_040, lastPingSentTs: 20_000, now: 620_000, graceMs: GRACE }))
      .toBe(false)
  })

  it('holds off while an unanswered ping is still within grace', () => {
    expect(isConnectionDead({ lastInboundTs: 19_000, lastPingSentTs: 20_000, now: 25_000, graceMs: GRACE }))
      .toBe(false)
  })

  it('declares dead once an unanswered ping outlives the grace period', () => {
    expect(isConnectionDead({ lastInboundTs: 19_000, lastPingSentTs: 20_000, now: 31_000, graceMs: GRACE }))
      .toBe(true)
  })

  it('still detects a genuinely dead socket when the tick was also late', () => {
    // Throttling must not mask a real failure: the ping went out and nothing
    // ever came back, so a late tick should still conclude dead.
    expect(isConnectionDead({ lastInboundTs: 19_000, lastPingSentTs: 20_000, now: 80_000, graceMs: GRACE }))
      .toBe(true)
  })
})
