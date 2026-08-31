/**
 * Reading errors thrown by an axios call.
 *
 * Two entry points, and picking the wrong one is a silent bug:
 *
 *   `apiErrorMessage(e)` → a string for a human. Use for toasts, banners,
 *                          inline form errors. Never empty.
 *   `apiErrorDetail(e)`  → the RAW `response.data.detail` value, untouched.
 *                          Use only when the endpoint puts structured data
 *                          there that the caller destructures.
 *
 * The second one exists because a few endpoints deliberately ship a whole
 * object as `detail` — `POST /api/workflows/generate` raises
 * `HTTPException(500, detail=<full Result dict>)` so the modal can still show
 * `review_report` and `stdout_tail` on failure. Passing that through
 * `apiErrorMessage` collapses it to a string and throws the structure away.
 *
 * Before these existed, the same expression was open-coded at ~85 call sites
 * in four mutually-inconsistent spellings, and each one needed
 * `catch (e: any)` to typecheck — a third of the frontend's `any` came from
 * exactly that.
 */

/** Shape we probe for; `unknown` is narrowed against this, never cast to it. */
type AxiosLikeError = {
  response?: { data?: { detail?: unknown } }
  message?: unknown
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

/**
 * The raw `response.data.detail`, or `undefined` when the throw wasn't an
 * axios error with a body.
 *
 * Callers that want to display something should use `apiErrorMessage`; this
 * is for the handful that inspect the shape (`Array.isArray`, `typeof ===
 * 'object'`) before deciding what to do.
 */
export function apiErrorDetail(err: unknown): unknown {
  if (!isObject(err)) return undefined
  return (err as AxiosLikeError).response?.data?.detail
}

/**
 * A non-empty display string for any thrown value.
 *
 * Precedence is the backend's error contract first, then the JS one:
 *   1. `detail` when it's a usable string.
 *   2. `detail.error` — some endpoints (sync agent-tick) nest it one level.
 *   3. `detail` as a FastAPI 422 array — join the per-field `msg`s, which is
 *      what the user can actually act on.
 *   4. any other non-null `detail` — JSON, so it isn't "[object Object]".
 *   5. `Error.message` — network failures, aborts, anything thrown locally.
 *   6. `String(value)` — someone threw a string/number/null.
 *
 * Deliberately does NOT touch `response.status`: callers prefix their own
 * context ("Launch failed: …") and a bare 500 adds nothing to that.
 */
export function apiErrorMessage(err: unknown): string {
  const detail = apiErrorDetail(err)

  // (1) An empty string is not a message — fall through rather than render a
  // blank toast the user can't act on.
  if (typeof detail === 'string' && detail) return detail

  if (detail !== undefined && detail !== null && typeof detail !== 'string') {
    // (3) FastAPI request-validation errors: [{loc, msg, type}, ...]. The
    // per-field `msg` is the actionable part; the loc/type noise is not.
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => (isObject(d) && typeof d.msg === 'string' ? d.msg : String(d)))
        .filter(Boolean)
      if (msgs.length) return msgs.join('; ')
    } else if (isObject(detail) && typeof detail.error === 'string' && detail.error) {
      return detail.error // (2)
    }
    // (4) Anything else structured. JSON.stringify throws on a cycle and
    // returns undefined for a bare `undefined`, hence both guards.
    try {
      const json = JSON.stringify(detail)
      if (json) return json
    } catch {
      /* fall through to String() below */
    }
    return String(detail)
  }

  // (5)
  if (isObject(err)) {
    const message = (err as AxiosLikeError).message
    if (typeof message === 'string' && message) return message
  }
  return String(err) // (6)
}
