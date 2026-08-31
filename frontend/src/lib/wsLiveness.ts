/**
 * Shared liveness verdict for the console's long-lived sockets.
 *
 * Both the terminal WS and the notification WS keep themselves honest with an
 * app-level `{type:'ping'}` → `{type:'pong'}` heartbeat, because a NAT / proxy
 * / SSH tunnel that silently drops an idle TCP connection leaves readyState
 * stuck OPEN and no reconnect would ever fire.
 *
 * The rule they use to call a socket dead is the delicate part, and it used to
 * be wrong in the same way in both places. The old rule was a deadline —
 * "no inbound frame in the last 45s/55s" — which measures elapsed wall time
 * with a clock the browser is free to stall. Browsers throttle timers in a
 * hidden tab to roughly one tick per minute, so the first throttled tick saw
 * ~60s of apparent silence and closed the socket. It never probed first,
 * because the verdict ran before the ping was sent. Field measurement on
 * 2026-08-24: 18/18 closes were self-inflicted this way, and the pong to the
 * preceding ping had come back in 15-105ms — every one of those sockets was
 * healthy.
 *
 * The rule below is a PAIRING check instead: dead means "a ping I actually
 * sent went unanswered". It compares two event stamps rather than measuring an
 * interval, so however late the tick runs, a healthy socket's pong is still
 * recorded as arriving after its ping and no verdict is reached. Throttling,
 * page freeze, GC pause and suspend/resume all delay both halves together.
 *
 * Callers must pass monotonic timestamps (`performance.now()`), not
 * `Date.now()` — a wall clock jumps on NTP correction and suspend/resume,
 * which is on its own enough to fire a false verdict.
 *
 * DUPLICATED, ON PURPOSE: a byte-identical copy lives at
 * `mobile/frontend/src/lib/wsLiveness.ts`. The two SPAs are deliberately
 * separate packages with no shared build, so this is copied rather than
 * shared. Extracting it *within* the desktop app was meant to stop the two
 * call sites drifting — and it did, but the mobile app kept the old
 * elapsed-time deadline and went on killing healthy sockets anyway.
 * `wsLiveness.identity.test.ts` fails if the copies diverge: edit both, or
 * neither.
 */
export interface LivenessState {
  /** Monotonic stamp of the last inbound frame (data or pong); 0 if none. */
  lastInboundTs: number
  /** Monotonic stamp of the last ping we successfully sent; 0 if none. */
  lastPingSentTs: number
  /** Current monotonic time. */
  now: number
  /** How long an unanswered ping may stand before we call it dead. */
  graceMs: number
}

export function isConnectionDead(s: LivenessState): boolean {
  // Never sent a probe yet — nothing to conclude from.
  if (!s.lastPingSentTs) return false
  // The last ping was answered (any inbound frame counts). Healthy, no matter
  // how long ago that was — this is what makes the check throttle-immune.
  if (s.lastInboundTs >= s.lastPingSentTs) return false
  // A ping is outstanding; give the pong its grace period before judging.
  return s.now - s.lastPingSentTs > s.graceMs
}
