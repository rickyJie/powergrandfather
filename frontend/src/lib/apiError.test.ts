/**
 * `apiErrorMessage` replaced 94 open-coded copies of the same expression, so
 * its precedence rules are now load-bearing for every error toast in the app.
 * Each branch is pinned here, plus the two shapes the open-coded versions got
 * wrong (non-string `detail`, and a thrown non-object).
 */
import { describe, expect, it } from 'vitest'
import { apiErrorDetail, apiErrorMessage } from './apiError'

describe('apiErrorMessage', () => {
  it("prefers the backend's HTTPException detail", () => {
    const e = {
      response: { data: { detail: 'workflow not found' } },
      message: 'Request failed with status code 404',
    }
    expect(apiErrorMessage(e)).toBe('workflow not found')
  })

  it('falls back to Error.message when there is no response body', () => {
    expect(apiErrorMessage(new Error('Network Error'))).toBe('Network Error')
  })

  it('stringifies a thrown non-object', () => {
    expect(apiErrorMessage('boom')).toBe('boom')
    expect(apiErrorMessage(null)).toBe('null')
    expect(apiErrorMessage(undefined)).toBe('undefined')
  })

  it('joins the per-field msgs of a FastAPI 422, not the raw JSON', () => {
    // REGRESSION: the first pass at this helper JSON.stringify'd the array, so
    // a 422 toasted `[{"loc":["body","cron"],"msg":"invalid cron",...}]`
    // instead of `invalid cron`. loc/type are noise; msg is what the user can
    // act on.
    const e = {
      response: {
        data: {
          detail: [
            { loc: ['body', 'cron'], msg: 'invalid cron', type: 'value_error' },
            { loc: ['body', 'name'], msg: 'field required', type: 'missing' },
          ],
        },
      },
    }
    expect(apiErrorMessage(e)).toBe('invalid cron; field required')
  })

  it('does not crash on a self-referential detail', () => {
    const detail: Record<string, unknown> = { a: 1 }
    detail.self = detail // JSON.stringify throws on this
    const out = apiErrorMessage({ response: { data: { detail } } })
    expect(typeof out).toBe('string')
    expect(out.length).toBeGreaterThan(0)
  })
})

describe('apiErrorDetail', () => {
  it('returns the raw value so callers can inspect its shape', () => {
    // Load-bearing: `POST /api/workflows/generate` raises
    // HTTPException(500, detail=<full Result dict>) precisely so the modal can
    // still render review_report + stdout_tail after a failure. Routing that
    // through apiErrorMessage collapsed it to a string, the modal's
    // `typeof detail === 'object'` test became permanently false, and the
    // structured failure report was silently replaced by a JSON blob in the
    // one-line `error` field.
    const result = { review_status: 'generation_failed', review_report: null, stdout_tail: 'boom' }
    const e = { response: { data: { detail: result } } }
    expect(apiErrorDetail(e)).toBe(result)
    expect(typeof apiErrorDetail(e)).toBe('object')
  })

  it('is undefined when the throw carried no response body', () => {
    expect(apiErrorDetail(new Error('Network Error'))).toBeUndefined()
    expect(apiErrorDetail('boom')).toBeUndefined()
    expect(apiErrorDetail(null)).toBeUndefined()
  })

  it('unwraps the {error} envelope some endpoints nest inside detail', () => {
    // `/api/sync/agent-tick` returns `detail: {error: "..."}`; SyncSettings
    // open-coded a four-level fallback to reach it.
    const e = { response: { data: { detail: { error: 'agent tick already running' } } } }
    expect(apiErrorMessage(e)).toBe('agent tick already running')
  })

  it('skips an empty detail rather than showing a blank toast', () => {
    const e = { response: { data: { detail: '' } }, message: 'Request failed' }
    expect(apiErrorMessage(e)).toBe('Request failed')
  })

  it('survives a response with no data at all', () => {
    expect(apiErrorMessage({ response: {} , message: 'timeout of 0ms exceeded' }))
      .toBe('timeout of 0ms exceeded')
  })

  it('never returns the empty string, so a toast is never blank', () => {
    for (const thrown of [{}, { response: { data: {} } }, { message: '' }, 0, false]) {
      expect(apiErrorMessage(thrown).length).toBeGreaterThan(0)
    }
  })
})
