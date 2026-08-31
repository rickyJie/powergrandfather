import { describe, expect, it } from 'vitest'
import { createSliceRetention } from './sliceRetention'

/**
 * These pin the trade the Sessions list makes: one wedged connection out of
 * three must NOT cost the two answers that arrived (perf.log 2026-08-25
 * 22:16:31 — two `/api/sessions` returned while the third burned its full 16s
 * budget, and all three results were discarded), while a real outage must
 * still reach the error banner.
 */

type Page = { items: string[] }
const KEYS = ['active', 'auto', 'history'] as const
type Key = (typeof KEYS)[number]

const ok = (...items: string[]): PromiseSettledResult<Page> => ({
  status: 'fulfilled',
  value: { items },
})
const fail = (reason = new Error('timeout of 8000ms exceeded')): PromiseSettledResult<Page> => ({
  status: 'rejected',
  reason,
})

const build = (tolerance = 3) => createSliceRetention<Key, Page>(KEYS, tolerance)

describe('sliceRetention', () => {
  it('keeps the answers that arrived when one slice wedges', () => {
    const r = build()
    r.apply([ok('a1'), ok('u1'), ok('h1')])

    const { values, stale } = r.apply([ok('a2'), fail(), ok('h2')])

    expect(values.active.items).toEqual(['a2'])
    expect(values.history.items).toEqual(['h2'])
    // The wedged one falls back to its last good answer rather than vanishing.
    expect(values.auto.items).toEqual(['u1'])
    expect(stale).toEqual(new Set(['auto']))
  })

  it('reports a clean round as not stale at all', () => {
    const r = build()
    const { stale } = r.apply([ok('a'), ok('u'), ok('h')])
    expect(stale.size).toBe(0)
  })

  it('throws when every slice fails — that is the channel, not a connection', () => {
    const r = build()
    r.apply([ok('a'), ok('u'), ok('h')]) // prime the cache so it CAN'T fall back
    expect(() => r.apply([fail(), fail(), fail()])).toThrow(/8000ms/)
  })

  it('throws on the first round when there is nothing to fall back on', () => {
    const r = build()
    // A partial first load would look like sessions disappeared, not like a
    // section being a few seconds stale.
    expect(() => r.apply([ok('a'), fail(), ok('h')])).toThrow(/8000ms/)
  })

  it('tolerates a blip but not a slice that keeps failing', () => {
    const r = build(3)
    r.apply([ok('a'), ok('u'), ok('h')])

    expect(r.apply([ok('a'), fail(), ok('h')]).stale).toEqual(new Set(['auto']))
    expect(r.apply([ok('a'), fail(), ok('h')]).stale).toEqual(new Set(['auto']))
    // Third miss running: still calling this a blip would be a lie.
    expect(() => r.apply([ok('a'), fail(), ok('h')])).toThrow(/8000ms/)
  })

  it('forgives a slice that recovers, so the count is CONSECUTIVE misses', () => {
    const r = build(3)
    r.apply([ok('a'), ok('u'), ok('h')])
    r.apply([ok('a'), fail(), ok('h')])
    r.apply([ok('a'), fail(), ok('h')])

    r.apply([ok('a'), ok('u2'), ok('h')]) // recovered — counter resets

    expect(r.apply([ok('a'), fail(), ok('h')]).stale).toEqual(new Set(['auto']))
    expect(r.apply([ok('a'), fail(), ok('h')]).stale).toEqual(new Set(['auto']))
  })

  it('refuses a result list that does not line up with its keys', () => {
    // Mis-keying the cache silently would show every slice another slice's
    // rows — worse than stopping.
    expect(() => build().apply([ok('a'), ok('u')])).toThrow(/expected 3 results/)
  })
})
