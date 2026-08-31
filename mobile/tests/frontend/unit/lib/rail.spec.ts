import { describe, it, expect } from "vitest";

import {
  activeNodeIndex,
  followWindow,
  pageWindow,
  slotFraction,
  NODE_HIT_SLACK_PX,
  RAIL_PAGE,
  RAIL_WINDOW,
} from "../../../../frontend/src/lib/rail";

/**
 * The rule behind the jump rail's highlight. Tested as arithmetic because a
 * jsdom layout reports every rectangle as zero, so driving this through the
 * component would assert nothing.
 */
describe("activeNodeIndex", () => {
  const mid = { atTop: false, atBottom: false };

  it("has no answer with no nodes", () => {
    expect(activeNodeIndex({ tops: [], ...mid })).toBe(-1);
  });

  // ── The bug this rule was written to end ────────────────────────────────
  it("lights the LAST node at the bottom, even with the message still low on screen", () => {
    // You just sent something: its top is near the bottom of the viewport and
    // nothing sits under it yet. The old rule wanted the top above 35% of the
    // viewport, so this message could never light its own dot and the rail sat
    // one behind — "my last message isn't the last node".
    const tops = [-900, -400, 560];
    expect(activeNodeIndex({ tops, atTop: false, atBottom: true })).toBe(2);
  });

  it("lights the last node at the bottom no matter how far below the fold it is", () => {
    expect(activeNodeIndex({ tops: [-100, 5000], atTop: false, atBottom: true })).toBe(1);
  });

  it("does not lag by one on a short turn", () => {
    // Turn 2's reply is short, so turn 2's message never gets 65% of a screen
    // under it. It is still the turn you are reading.
    const tops = [-800, -30];
    expect(activeNodeIndex({ tops, ...mid })).toBe(1);
  });

  it("lights the FIRST node at the top", () => {
    expect(activeNodeIndex({ tops: [40, 900], atTop: true, atBottom: false })).toBe(0);
  });

  it("prefers the bottom when a transcript is both at top and at bottom", () => {
    // Short transcript, no scrollbar: both ends are true. The newest turn is
    // what you are looking at.
    const tops = [10, 120];
    expect(activeNodeIndex({ tops, atTop: true, atBottom: true })).toBe(1);
  });

  // ── Mid-scroll: a node owns its turn ────────────────────────────────────
  it("stays on a turn while its reply fills the screen", () => {
    // Node 1 is far above the fold, node 2 is still below it.
    expect(activeNodeIndex({ tops: [-2000, 300], ...mid })).toBe(0);
  });

  it("moves on as soon as the next message crosses the fold", () => {
    expect(activeNodeIndex({ tops: [-2000, -1], ...mid })).toBe(1);
  });

  it("counts a node as current within the slack below the fold", () => {
    const justInside = NODE_HIT_SLACK_PX - 1;
    const justOutside = NODE_HIT_SLACK_PX + 1;
    expect(activeNodeIndex({ tops: [-500, justInside], ...mid })).toBe(1);
    expect(activeNodeIndex({ tops: [-500, justOutside], ...mid })).toBe(0);
  });

  it("honours a custom slack", () => {
    expect(activeNodeIndex({ tops: [-500, 100], ...mid, slackPx: 200 })).toBe(1);
  });

  it("falls back to the first node when everything is below the fold", () => {
    // Scrolled into the load-earlier button above the first message: there is
    // no turn yet, and the first node is the nearest true statement.
    expect(activeNodeIndex({ tops: [200, 900], ...mid })).toBe(0);
  });

  it("picks the last node that has crossed the fold, not the first", () => {
    expect(activeNodeIndex({ tops: [-900, -600, -20, 400, 900], ...mid })).toBe(2);
  });
});

/**
 * The visible window. Indexing every message was correct and unusable — a
 * working session runs to dozens of turns, and dozens of dots down a phone's
 * right edge is a dotted line, not a control. The rail is a pager over the
 * message list: a fixed number of evenly spaced slots you move.
 */
describe("rail window", () => {
  it("shows everything when it fits", () => {
    expect(followWindow(0, 4, 5)).toBe(0);
    expect(pageWindow(0, -1, 5)).toBe(0);
  });

  it("pages by half a window at a time", () => {
    expect(pageWindow(10, 1, 40)).toBe(10 + RAIL_PAGE);
    expect(pageWindow(10, -1, 40)).toBe(10 - RAIL_PAGE);
  });

  it("stops paging at either end instead of running off", () => {
    expect(pageWindow(1, -1, 40)).toBe(0);
    expect(pageWindow(40 - RAIL_WINDOW - 1, 1, 40)).toBe(40 - RAIL_WINDOW);
    expect(pageWindow(0, -1, 40)).toBe(0);
  });

  it("defaults to the newest messages", () => {
    // A fresh mount sits at the bottom, so the active node is the last one.
    expect(followWindow(0, 39, 40)).toBe(40 - RAIL_WINDOW);
  });

  it("leaves the rail alone while the reader stays inside the window", () => {
    // Hysteresis: scrolling between interior slots must not shuffle the whole
    // rail under the thumb.
    const start = 10;
    for (const active of [11, 12, 13, 14, 15]) {
      expect(followWindow(start, active, 40)).toBe(start);
    }
  });

  it("slides one slot when the reader crosses an edge, not a whole page", () => {
    const start = 10; // window covers 10..16
    expect(followWindow(start, 16, 40)).toBe(11);
    expect(followWindow(start, 9, 40)).toBe(8);
  });

  it("keeps a slot of context between the active dot and the edge", () => {
    const start = followWindow(10, 16, 40);
    expect(16 - start).toBeLessThanOrEqual(RAIL_WINDOW - 2);
    expect(16 - start).toBeGreaterThanOrEqual(1);
  });

  it("cannot scroll the window past either end of the list", () => {
    expect(followWindow(0, 0, 40)).toBe(0);
    expect(followWindow(99, 39, 40)).toBe(40 - RAIL_WINDOW);
  });

  it("holds still when there is no active node yet", () => {
    expect(followWindow(12, -1, 40)).toBe(12);
  });

  it("spaces slots evenly — the positions never move", () => {
    expect(slotFraction(0, RAIL_WINDOW)).toBe(0);
    expect(slotFraction((RAIL_WINDOW - 1) / 2, RAIL_WINDOW)).toBeCloseTo(0.5, 6);
    expect(slotFraction(RAIL_WINDOW - 1, RAIL_WINDOW)).toBe(1);
  });

  it("centres a lone dot rather than pinning it to the top", () => {
    expect(slotFraction(0, 1)).toBe(0.5);
  });
});
