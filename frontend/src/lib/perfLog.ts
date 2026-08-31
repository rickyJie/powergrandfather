// Client-side request-latency logging, the browser half of the perf mechanism
// (server half: backend/csm/core/perf_log.py). Correlated by an X-Request-Id
// that both halves log, so `grep req=<id> perf.log` splits a slow request into
// backend vs SSH-tunnel vs browser-queue.
//
// Surface split: desktop (/) and mobile (/m/) are the SAME origin, so they
// share localStorage. Each build passes its `surface` ('web' | 'mobile') — it
// keys the ring buffer per-surface (no clobber), tags every record, and sends
// an `X-CSM-Surface` header so the backend can roll up web and mobile
// separately.
//
// Two modes:
//   - Always on (zero config): records FAILURES (timeout / network error) with
//     context — so the next "Could not sync sessions" always leaves a trace.
//   - Verbose (window.__csmPerf.enable()): records EVERY request + its Resource
//     Timing breakdown, and ships slow/failed ones to POST /api/clientperf.
//
// Resource Timing gives the attribution that matters:
//   queue_ms    = requestStart - startTime    → browser HTTP/1.1 pool wait
//   ttfb_ms     = responseStart - requestStart → server + tunnel round-trip
//   download_ms = responseEnd - responseStart  → bandwidth
// and (ttfb_ms - server_ms from perf.log) isolates the SSH-tunnel hop.
import type { AxiosInstance } from 'axios'

export type Surface = 'web' | 'mobile'

const LS_FLAG = 'csm.perfLog'          // shared verbose flag
const LS_BUF_BASE = 'csm.perfLog.buf'  // suffixed by surface
const MAX = 1000
const SLOW_MS = 800

export interface PerfEntry {
  t: number            // epoch ms
  surface: Surface
  req: string          // X-Request-Id (matches server perf.log)
  method: string
  url: string
  status: number | null
  err?: string         // 'timeout' | 'network' | message
  total_ms: number     // client start..settle
  hidden: boolean
  online: boolean
  inflight: number     // concurrent in-flight XHRs at settle
  queue_ms?: number
  ttfb_ms?: number
  download_ms?: number
}

let SURFACE: Surface = 'web'
let buf: PerfEntry[] = []
let inflight = 0

function bufKey(): string { return `${LS_BUF_BASE}.${SURFACE}` }
function loadBuf(): PerfEntry[] {
  try { return JSON.parse(localStorage.getItem(bufKey()) || '[]') } catch { return [] }
}
function setSurface(s: Surface): void {
  SURFACE = s
  buf = loadBuf()
}

let saveTimer: number | null = null
function saveSoon() {
  if (saveTimer != null) return
  saveTimer = window.setTimeout(() => {
    saveTimer = null
    try { localStorage.setItem(bufKey(), JSON.stringify(buf.slice(-MAX))) } catch { /* quota */ }
  }, 500)
}
function isVerbose(): boolean {
  try { return localStorage.getItem(LS_FLAG) === '1' } catch { return false }
}

function newReqId(): string {
  return 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

// ── server sink (batched, retried) ──────────────────────────────────────────
// A failed POST used to drop its batch on the floor. That is precisely
// backwards: the records worth reading are the ones from a tunnel collapse,
// and a tunnel collapse is exactly when this POST also fails. perf.log
// 2026-08-25 has a 139s hole starting on the same second a Sessions list
// refresh entered its retry — so the server-side copy of the one episode that
// produced a user-visible "Could not sync sessions" banner never existed, and
// it had to be reconstructed from the backend's own request log instead.
//
// The local ring buffer (`buf`, window.__csmPerf.dump()) always kept these;
// only the server mirror was lost. So this is about making the host-side
// evidence complete, not about preventing data loss in the browser.
const SHIP_MAX_QUEUE = 200      // ring — a long outage must not grow unbounded
const SHIP_MAX_BATCH = 100      // keepalive fetch bodies are capped at 64KB
const SHIP_BASE_MS = 2000
const SHIP_MAX_BACKOFF_MS = 60_000

/** Batched sink that re-queues on failure. Exported for tests; `ship` wires
 *  the real one to POST /api/clientperf. */
export function createShipper(post: (batch: PerfEntry[]) => Promise<unknown>) {
  let queue: PerfEntry[] = []
  let timer: number | null = null
  let backoff = SHIP_BASE_MS

  function schedule(delay: number) {
    if (timer != null) return
    timer = window.setTimeout(flush, delay)
  }

  function requeue(batch: PerfEntry[]) {
    // Front of the queue: older than anything pushed since, so delivery stays
    // FIFO and the outage reads in order once it drains. Overflow sheds from
    // the front too — an outage longer than the ring loses its oldest records,
    // never its most recent ones.
    queue = [...batch, ...queue].slice(-SHIP_MAX_QUEUE)
    backoff = Math.min(backoff * 2, SHIP_MAX_BACKOFF_MS)
    schedule(backoff)
  }

  function flush() {
    timer = null
    const batch = queue.splice(0, SHIP_MAX_BATCH)
    if (!batch.length) return
    let sent: Promise<unknown>
    try {
      sent = Promise.resolve(post(batch))
    } catch {
      requeue(batch) // no fetch in this environment, or it threw synchronously
      return
    }
    sent.then(
      () => {
        backoff = SHIP_BASE_MS
        if (queue.length) schedule(SHIP_BASE_MS)
      },
      () => requeue(batch),
    )
  }

  return {
    push(e: PerfEntry) {
      queue.push(e)
      if (queue.length > SHIP_MAX_QUEUE) queue.splice(0, queue.length - SHIP_MAX_QUEUE)
      schedule(backoff)
    },
  }
}

// The one place allowed to call `fetch` on an /api path directly: everywhere
// else uses `apiFetch` from api/client.ts, but client.ts imports THIS module
// to install itself, so reaching back for the helper would be a cycle. The
// header below is what apiFetch would have added — keep them in step.
const shipper = createShipper((batch) =>
  fetch('/api/clientperf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSM-Client': '1' },
    body: JSON.stringify(batch),
    keepalive: true,
  }).then((r) => {
    // A 5xx means the sink did not take it — retry rather than silently drop.
    if (!r.ok) throw new Error(`clientperf ${r.status}`)
  }),
)

function ship(e: PerfEntry) {
  shipper.push(e)
}

function record(e: PerfEntry) {
  buf.push(e)
  if (buf.length > MAX) buf.splice(0, buf.length - MAX)
  saveSoon()
  if (e.url.includes('/api/clientperf')) return // never ship the sink POST (recursion)
  if (e.err || (isVerbose() && e.total_ms >= SLOW_MS)) ship(e)
}

// Resource Timing phase breakdown for a just-settled request.
function resourceTimingFor(url: string, startPerf: number): Partial<PerfEntry> {
  try {
    const pathish = url.startsWith('http') ? new URL(url, location.href).pathname : url.split('?')[0]
    const entries = performance.getEntriesByType('resource') as PerformanceResourceTiming[]
    for (let i = entries.length - 1; i >= 0; i--) {
      const e = entries[i]
      if (!e.name.includes(pathish)) continue
      if (e.startTime < startPerf - 50) break // older than this request — stop
      if (!e.responseEnd) continue
      return {
        queue_ms: Math.max(0, Math.round(e.requestStart - e.startTime)),
        ttfb_ms: Math.max(0, Math.round(e.responseStart - e.requestStart)),
        download_ms: Math.max(0, Math.round(e.responseEnd - e.responseStart)),
      }
    }
  } catch { /* Timing-Allow / not found */ }
  return {}
}

/** Attach interceptors that time every axios call. Pass this build's surface. */
export function installPerfLog(http: AxiosInstance, surface: Surface): void {
  setSurface(surface)
  http.interceptors.request.use((config) => {
    const req = newReqId()
    ;(config as unknown as { __perf?: unknown }).__perf = { req, start: performance.now() }
    config.headers = config.headers ?? {}
    ;(config.headers as Record<string, string>)['X-Request-Id'] = req
    ;(config.headers as Record<string, string>)['X-CSM-Surface'] = surface
    inflight++
    return config
  })

  const settle = (config: unknown, status: number | null, err?: string) => {
    inflight = Math.max(0, inflight - 1)
    const p = (config as { __perf?: { req: string, start: number } } | undefined)?.__perf
    if (!p) return
    const total = performance.now() - p.start
    if (!isVerbose() && !err && total < SLOW_MS) return // cheap path
    const c = config as { baseURL?: string, url?: string, method?: string }
    const url = (c.baseURL || '') + (c.url || '')
    record({
      t: Date.now(),
      surface: SURFACE,
      req: p.req,
      method: (c.method || 'get').toUpperCase(),
      url,
      status,
      err,
      total_ms: Math.round(total),
      hidden: typeof document !== 'undefined' && document.hidden,
      online: typeof navigator !== 'undefined' ? navigator.onLine : true,
      inflight,
      ...resourceTimingFor(url, p.start),
    })
  }

  http.interceptors.response.use(
    (resp) => { settle(resp.config, resp.status); return resp },
    (error) => {
      const err = error?.code === 'ECONNABORTED'
        ? 'timeout'
        : error?.message === 'Network Error' ? 'network' : (error?.message || 'error')
      settle(error?.config, error?.response?.status ?? null, err)
      return Promise.reject(error)
    },
  )
}

function stats(): Record<string, { n: number, p50: number, p95: number, max: number, fails: number }> {
  const byKey: Record<string, number[]> = {}
  const fails: Record<string, number> = {}
  for (const e of buf) {
    const key = `${e.surface} ${e.method} ${e.url.replace(/\/[0-9a-f-]{12,}/gi, '/:id').split('?')[0]}`
    ;(byKey[key] ||= []).push(e.total_ms)
    if (e.err) fails[key] = (fails[key] || 0) + 1
  }
  const out: Record<string, { n: number, p50: number, p95: number, max: number, fails: number }> = {}
  for (const k of Object.keys(byKey)) {
    const v = byKey[k].slice().sort((a, b) => a - b)
    const pct = (q: number) => v[Math.min(v.length - 1, Math.round(q * (v.length - 1)))]
    out[k] = { n: v.length, p50: pct(0.5), p95: pct(0.95), max: v[v.length - 1], fails: fails[k] || 0 }
  }
  return out
}

/** window.__csmPerf.{enable,disable,dump,download,clear,stats}. Pass surface. */
export function initPerfConsole(surface: Surface): void {
  setSurface(surface)
  const api = {
    surface,
    enable() { localStorage.setItem(LS_FLAG, '1'); console.info(`[perf:${surface}] verbose ON — every request recorded + slow ones shipped to /api/clientperf`) },
    disable() { localStorage.removeItem(LS_FLAG); console.info(`[perf:${surface}] verbose OFF — only failures recorded`) },
    isOn: () => isVerbose(),
    dump: () => buf.slice(),
    stats,
    clear() { buf = []; try { localStorage.removeItem(bufKey()) } catch { /* */ }; console.info(`[perf:${surface}] cleared`) },
    download() {
      const blob = new Blob([JSON.stringify(buf, null, 2)], { type: 'application/json' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `csm-perf-${surface}-${new Date().toISOString().replace(/[:.]/g, '-')}.json`
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 1000)
    },
  }
  ;(window as unknown as { __csmPerf?: unknown }).__csmPerf = api
  window.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      try { localStorage.setItem(bufKey(), JSON.stringify(buf.slice(-MAX))) } catch { /* */ }
    }
  })
}
