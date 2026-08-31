/**
 * useEventStream — thin composable over browser EventSource, wired to
 * `GET /api/events/stream` (SSE fan-out of the backend EventStream).
 *
 * One shared EventSource across the SPA: multiple views can subscribe
 * with their own `onEvent` / `onReconnect` callbacks and the module
 * owns lifecycle (open on first subscriber, close on last unsubscribe).
 *
 * Reconnect strategy: browser EventSource auto-reconnects on network
 * errors, but only with the default (usually 3s) interval — we override
 * by explicitly closing on error and scheduling with exponential
 * backoff capped at 30s. This lets us also fire `onReconnect` at each
 * successful re-open so views can `refresh()` back to a consistent
 * state (the server does not replay events dropped during downtime).
 *
 * Contract:
 *  - `onEvent(e)`: called for every SSE event received. Payload matches
 *    the JSON shape from `/api/events/recent` items.
 *  - `onReconnect()`: called after every successful re-open that is NOT
 *    the initial open. Views typically use this to trigger a full
 *    refresh so they resync any state changes that fired during the
 *    outage.
 *  - Returns `{ stop() }` — call from `onBeforeUnmount` to unsubscribe.
 */

export interface CSMEvent {
  id: string
  type: string
  ts: string
  session_id: string | null
  project_path: string | null
  payload: Record<string, unknown>
}

export interface EventStreamHandlers {
  onEvent?: (e: CSMEvent) => void
  onReconnect?: () => void
}

interface Subscriber extends EventStreamHandlers {
  id: number
}

const _subs: Map<number, Subscriber> = new Map()
let _nextSubId = 1
let _es: EventSource | null = null
let _reconnectTimer: number | null = null
let _reconnectAttempt = 0
let _hasBeenConnected = false
let _manualClose = false

const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 30_000

function _scheduleReconnect() {
  if (_reconnectTimer != null) return
  if (_manualClose) return
  // Exponential backoff with a small jitter to avoid thundering herd
  // if the backend restarts and every tab reconnects on the same tick.
  const backoff = Math.min(RECONNECT_BASE_MS * 2 ** _reconnectAttempt, RECONNECT_MAX_MS)
  const jitter = Math.floor(Math.random() * 250)
  _reconnectAttempt += 1
  _reconnectTimer = window.setTimeout(() => {
    _reconnectTimer = null
    _connect()
  }, backoff + jitter)
}

function _connect() {
  if (_es) return
  _manualClose = false
  const es = new EventSource('/api/events/stream')
  _es = es
  es.onopen = () => {
    _reconnectAttempt = 0
    if (_hasBeenConnected) {
      // Reconnect — fan out to subscribers so they can bootstrap.
      for (const sub of _subs.values()) {
        try { sub.onReconnect?.() } catch (err) { console.error('SSE onReconnect handler failed', err) }
      }
    }
    _hasBeenConnected = true
  }
  es.onmessage = (evt) => {
    let parsed: CSMEvent
    try {
      parsed = JSON.parse(evt.data)
    } catch (err) {
      console.error('SSE payload parse failed', err, evt.data)
      return
    }
    for (const sub of _subs.values()) {
      try { sub.onEvent?.(parsed) } catch (err) { console.error('SSE onEvent handler failed', err) }
    }
  }
  es.onerror = () => {
    // Browser EventSource may auto-retry; we take control instead so
    // reconnect timing and `onReconnect` fan-out live in one place.
    if (_es) {
      _es.close()
      _es = null
    }
    _scheduleReconnect()
  }
}

function _teardownIfIdle() {
  if (_subs.size > 0) return
  _manualClose = true
  if (_reconnectTimer != null) {
    clearTimeout(_reconnectTimer)
    _reconnectTimer = null
  }
  if (_es) {
    _es.close()
    _es = null
  }
  _hasBeenConnected = false
  _reconnectAttempt = 0
}

export function useEventStream(handlers: EventStreamHandlers): { stop: () => void } {
  const id = _nextSubId++
  _subs.set(id, { id, ...handlers })
  if (_es == null && _reconnectTimer == null) _connect()
  return {
    stop() {
      _subs.delete(id)
      _teardownIfIdle()
    },
  }
}
