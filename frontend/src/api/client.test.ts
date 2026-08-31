import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AxiosError } from 'axios'
import { http, pollGet, POLL_GET_MAX_MS, POLL_TIMEOUT_MS } from './client'

/**
 * These pin the contract `POLL_GET_MAX_MS` advertises. Callers race timers
 * against work built out of `pollGet` (Sessions.vue's list-refresh gate), and
 * a deadline shorter than pollGet's real budget rejects a recovery that was
 * already in flight — which is what made the "Could not sync sessions:
 * list refresh retry timed out" banner fire on blips pollGet handles.
 *
 * So the number must stay honest: if pollGet ever stops retrying, or starts
 * retrying twice, `POLL_GET_MAX_MS` is a lie and every derived deadline is
 * silently wrong.
 */

function transportError(code = 'ECONNABORTED'): AxiosError {
  return Object.assign(new Error('timeout'), { code, isAxiosError: true }) as AxiosError
}

function httpError(status: number): AxiosError {
  return Object.assign(new Error(`status ${status}`), {
    isAxiosError: true,
    response: { status, data: {} },
  }) as AxiosError
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('POLL_GET_MAX_MS', () => {
  it('covers a full pollGet chain: the attempt plus its one retry', () => {
    expect(POLL_GET_MAX_MS).toBe(POLL_TIMEOUT_MS * 2)
  })

  it('leaves room for the retry pollGet actually makes', async () => {
    // Two transport failures — proves the chain is 2 attempts, not 1, so the
    // "* 2" above is derived from behaviour rather than assumed.
    const get = vi.spyOn(http, 'get').mockRejectedValue(transportError())

    await expect(pollGet('/api/sessions')).rejects.toThrow()

    expect(get).toHaveBeenCalledTimes(2)
  })
})

describe('pollGet', () => {
  it('applies the fast-fail timeout instead of the 30s global one', async () => {
    const get = vi.spyOn(http, 'get').mockResolvedValue({ data: {} } as never)

    await pollGet('/api/sessions')

    expect(get.mock.calls[0][1]).toMatchObject({ timeout: POLL_TIMEOUT_MS })
  })

  it('lets an explicit per-call timeout win', async () => {
    const get = vi.spyOn(http, 'get').mockResolvedValue({ data: {} } as never)

    await pollGet('/api/sessions/x/output', { timeout: 1234 })

    expect(get.mock.calls[0][1]).toMatchObject({ timeout: 1234 })
  })

  it('recovers on the retry, which is the whole point of the budget', async () => {
    const get = vi.spyOn(http, 'get')
      .mockRejectedValueOnce(transportError())
      .mockResolvedValueOnce({ data: { count: 1 } } as never)

    const res = await pollGet<{ count: number }>('/api/sessions')

    expect(res.data.count).toBe(1)
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('does NOT retry a real HTTP answer — a 4xx is a result, not a blip', async () => {
    const get = vi.spyOn(http, 'get').mockRejectedValue(httpError(422))

    await expect(pollGet('/api/sessions')).rejects.toMatchObject({
      response: { status: 422 },
    })

    expect(get).toHaveBeenCalledTimes(1)
  })

  it('retries a response-less network failure, not just an abort', async () => {
    const get = vi.spyOn(http, 'get')
      .mockRejectedValueOnce(transportError('ERR_NETWORK'))
      .mockResolvedValueOnce({ data: {} } as never)

    await pollGet('/api/sessions')

    expect(get).toHaveBeenCalledTimes(2)
  })
})
