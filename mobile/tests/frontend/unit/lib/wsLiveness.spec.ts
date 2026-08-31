/**
 * Mirror of `frontend/src/lib/wsLiveness.test.ts`.
 *
 * The mobile app is a separate package, so it gets its own coverage rather
 * than relying on the desktop suite: when the desktop watchdog was fixed on
 * 2026-08-24 the mobile one was left on the old elapsed-time deadline and went
 * on closing healthy sockets, which is what the "phone keeps disconnecting"
 * reports actually were.
 */
import { describe, it, expect } from "vitest";
// `@/` is mobile's own src — this must exercise the MOBILE copy, not reach
// across into the desktop package's.
import { isConnectionDead } from "@/lib/wsLiveness";

const GRACE = 10_000;

describe("isConnectionDead (mobile)", () => {
  it("never judges before a probe has been sent", () => {
    expect(
      isConnectionDead({ lastInboundTs: 0, lastPingSentTs: 0, now: 500_000, graceMs: GRACE }),
    ).toBe(false);
  });

  it("treats an answered ping as alive on a normal tick", () => {
    // ping at 20_000, pong 40ms later, tick at 40_000
    expect(
      isConnectionDead({ lastInboundTs: 20_040, lastPingSentTs: 20_000, now: 40_000, graceMs: GRACE }),
    ).toBe(false);
  });

  it("REGRESSION: a healthy socket survives a badly delayed tick", () => {
    // Android Doze / backgrounded WebView throttles the interval to roughly one
    // tick a minute. The old rule saw ~60s of apparent silence and closed a
    // socket whose pong had come back 40ms after the ping.
    expect(
      isConnectionDead({ lastInboundTs: 20_040, lastPingSentTs: 20_000, now: 80_000, graceMs: GRACE }),
    ).toBe(false);
    // Screen off for ten minutes is still alive.
    expect(
      isConnectionDead({ lastInboundTs: 20_040, lastPingSentTs: 20_000, now: 620_000, graceMs: GRACE }),
    ).toBe(false);
  });

  it("holds off while an unanswered ping is still within grace", () => {
    expect(
      isConnectionDead({ lastInboundTs: 19_000, lastPingSentTs: 20_000, now: 25_000, graceMs: GRACE }),
    ).toBe(false);
  });

  it("declares dead once an unanswered ping outlives the grace period", () => {
    expect(
      isConnectionDead({ lastInboundTs: 19_000, lastPingSentTs: 20_000, now: 31_000, graceMs: GRACE }),
    ).toBe(true);
  });

  it("still detects a genuinely dead socket when the tick was also late", () => {
    // Throttling must not mask a real failure.
    expect(
      isConnectionDead({ lastInboundTs: 19_000, lastPingSentTs: 20_000, now: 80_000, graceMs: GRACE }),
    ).toBe(true);
  });
});
