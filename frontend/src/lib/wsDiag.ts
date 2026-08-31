/**
 * wsDiag — temporary instrumentation for the WS reconnect-churn investigation
 * (2026-08-24). REMOVE once the question below is settled.
 *
 * The console re-establishes its long-lived sockets far more often than it
 * should while the tab is backgrounded. The server side can't answer "who
 * closed this socket, and why": uvicorn's `connection open` / `connection
 * closed` lines carry neither a timestamp nor a connection id, and the repo
 * sets no custom log_config.
 *
 * So record it on the client instead. Two things make this cheap:
 *
 *  - It ships on the query string of a harmless GET. uvicorn logs the full
 *    request line, so each sample lands in the normal access log with NO
 *    backend change — which matters, because the PTY children are owned by
 *    the backend process and restarting it to add an endpoint would kill the
 *    user's live sessions.
 *  - `by` is decided by the caller, not inferred from a close code. The
 *    watchdog sets it explicitly before it calls close(), so there is no
 *    ambiguity between "we killed a healthy socket" (by=watchdog) and "the
 *    transport dropped it" (by=other, code 1006).
 *
 * The decisive field is `gaps`: the ACTUAL interval between watchdog ticks.
 * The watchdog declares death when no frame arrived within its timeout, but
 * on an idle connection the only inbound frame is the pong to a ping that the
 * same callback sends AFTER the check — so a tick that runs late kills a
 * healthy socket without ever probing it. `gaps` shows whether that happened.
 */
import { apiFetch } from '../api/client'

/** Rolling record of when an interval callback *actually* ran. */
export class TickRecorder {
  private last = 0
  private gaps: number[] = []

  /** Call when the interval is (re)armed. */
  start() {
    this.last = performance.now()
    this.gaps = []
  }

  /** Call at the top of every tick — records how long since the previous one. */
  tick() {
    const now = performance.now()
    if (this.last) {
      this.gaps.push(Math.round(now - this.last))
      if (this.gaps.length > 4) this.gaps.shift()
    }
    this.last = now
  }

  recent(): number[] {
    return this.gaps.slice()
  }
}

export interface WsCloseSample {
  /** Which long-lived connection this was. */
  ch: 'term' | 'notif'
  /** 'watchdog' = our own liveness check closed it; 'other' = anything else. */
  by: 'watchdog' | 'other'
  code: number
  clean: boolean
  /** ms since the last inbound frame (-1 if none ever arrived). */
  sinceInbound: number
  /** ms since we last successfully sent a ping (-1 if we never did). */
  sincePing: number
  /** Actual ms between the last few watchdog ticks. */
  gaps: number[]
  vis: string
}

export function reportWsClose(s: WsCloseSample): void {
  try {
    const payload = [
      `ch=${s.ch}`,
      `by=${s.by}`,
      `code=${s.code}`,
      `clean=${s.clean ? 1 : 0}`,
      `inb=${s.sinceInbound}`,
      `ping=${s.sincePing}`,
      `gaps=${s.gaps.join('|')}`,
      `vis=${s.vis}`,
    ].join(',')
    // keepalive: the sample must still ship when the close is part of the tab
    // being torn down or frozen.
    void apiFetch(`/api/health?wsdiag=${encodeURIComponent(payload)}`, {
      keepalive: true,
    }).catch(() => { /* diagnostic only — never surface */ })
  } catch (_) { /* diagnostic only — never surface */ }
}

export function visState(): string {
  return typeof document !== 'undefined' ? document.visibilityState : '?'
}
