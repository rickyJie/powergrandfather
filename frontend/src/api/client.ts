import { apiErrorMessage } from '../lib/apiError'
import axios, { type AxiosRequestConfig, type AxiosResponse, type AxiosError } from 'axios'
import { installPerfLog } from '../lib/perfLog'

export const http = axios.create({
  baseURL: '',
  timeout: 30000,
  // FastAPI expects repeated keys for list[str] Query params (?model=a&model=b).
  // Axios 1.x's default emits `model[]=a&model[]=b` which FastAPI ignores.
  paramsSerializer: {
    serialize: (params) => {
      const usp = new URLSearchParams()
      for (const [k, v] of Object.entries(params)) {
        if (v == null || v === '') continue
        if (Array.isArray(v)) v.forEach(x => usp.append(k, String(x)))
        else usp.append(k, String(v))
      }
      return usp.toString()
    },
  },
})

// C5 — always tag same-origin XHRs with the CSM client header. The backend
// requires this on /api/* (except a small exempt list — hooks, metrics,
// SSE) to force a CORS preflight for non-simple requests, which blocks
// form-encoded CSRF from a stray browser tab. Setting it here means every
// axios call in the SPA automatically qualifies.
http.interceptors.request.use((config) => {
  config.headers = config.headers ?? {}
  ;(config.headers as Record<string, string>)['X-CSM-Client'] = '1'
  return config
})

/**
 * `fetch` for /api/*, with the C5 header attached.
 *
 * The interceptor above only covers axios. A raw `window.fetch('/api/...')`
 * skips it and the backend answers 400 — the caller sees an opaque
 * `HTTP 400` and no hint that a header is missing. That is not hypothetical:
 * the workflows drawer, its Reload YAML button and both Schedule dialogs were
 * all broken this way, each one a separate copy of the same oversight.
 *
 * Prefer the typed clients in `api/*.ts`. Use this only where a raw fetch is
 * genuinely wanted (streaming, `res.ok` handling), and never call `fetch` on
 * an /api/ path directly.
 */
export function apiFetch(input: string, init: RequestInit = {}) {
  return fetch(input, {
    ...init,
    headers: { ...(init.headers ?? {}), 'X-CSM-Client': '1' },
  })
}

// Latency instrumentation: times every request, tags it with X-Request-Id (the
// backend logs the same id), records failures always + everything in verbose
// mode. Inspect via window.__csmPerf in the console. See lib/perfLog.ts.
installPerfLog(http, 'web')

// Fast-fail poll timeout. The global 30s timeout is right for one-shot user
// actions (resume ~15s, DELETE ladder ~15s), but WRONG for high-frequency poll
// GETs over an SSH tunnel: perf.log shows a single forwarded connection can
// wedge mid-response for the full 30s while SIBLING connections keep working
// (queue/ttfb/download all <150ms, yet axios total=30003ms). Waiting 30s on a
// dead connection is pure loss — a fresh request lands on a new connection and
// returns in <100ms. So poll GETs fail at 8s and immediately retry once.
export const POLL_TIMEOUT_MS = 8000

/**
 * Worst-case wall time ONE `pollGet` can legitimately consume: the initial
 * attempt plus its single retry.
 *
 * Anything that races a timer against work containing a `pollGet` must allow
 * at least this much, or it cuts off a recovery that was about to succeed.
 * Exported so callers derive their deadline instead of hard-coding a second
 * number — which is exactly how the Sessions list ended up flashing
 * "list refresh retry timed out" on a 10s gate wrapped around a 16s budget,
 * turning a tunnel blip that pollGet handles into a user-visible banner.
 */
export const POLL_GET_MAX_MS = POLL_TIMEOUT_MS * 2

/**
 * GET for high-frequency polls (session list, worktime/live). Fails fast at
 * POLL_TIMEOUT_MS and retries ONCE on a transient error (no response / abort).
 * The abandoned wedged connection is discarded by the browser, so the retry is
 * issued on a fresh connection and recovers a tunnel single-connection wedge in
 * ~1s instead of surfacing the "Could not sync" banner after 30-40s. Only for
 * idempotent GETs — never wrap writes with this.
 */
export async function pollGet<T = unknown>(
  url: string,
  config: AxiosRequestConfig = {},
): Promise<AxiosResponse<T>> {
  const cfg: AxiosRequestConfig = { ...config, timeout: config.timeout ?? POLL_TIMEOUT_MS }
  try {
    return await http.get<T>(url, cfg)
  } catch (e) {
    const err = e as AxiosError
    // Only retry transient transport failures (timeout / no response). A real
    // 4xx/5xx is a genuine answer — surface it instead of masking with a retry.
    const transient = !err.response || err.code === 'ECONNABORTED'
    if (!transient) throw e
    return await http.get<T>(url, cfg)
  }
}

export function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${path}`
}

/**
 * Normalise any thrown error from an axios call into a display string.
 *
 * Kept as a re-export rather than a second implementation: this used to be a
 * near-copy of `apiErrorMessage`, including its own FastAPI-422 array
 * handling, and the two drifted. `apiErrorMessage` now owns the precedence
 * rules (and is unit-tested); this name stays because ~14 call sites use it.
 */
export { apiErrorMessage as formatApiError }
