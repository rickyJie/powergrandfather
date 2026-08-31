/**
 * Which jump-rail node is "current", as a pure decision.
 *
 * A node marks the start of one turn — my message plus whatever the assistant
 * said back — and owns the transcript from there until the next node. You are
 * "on" node i while any part of that turn fills the screen.
 *
 * The two ends are decided BEFORE any geometry, and that is the whole point.
 * The rule this replaces asked "is this message's top above the 35% line",
 * which a message can only satisfy once ~65% of a screenful of content sits
 * BELOW it. A message you had just sent has nothing below it yet, so it could
 * never light its own dot and the rail sat one behind — "my last message isn't
 * the last node". Every short turn had the same lag; the tail is just where it
 * was obvious.
 *
 * Kept out of the component so the rule can be tested as arithmetic instead of
 * through a jsdom layout that reports every rectangle as zero.
 */

/** Default slack, in px, below the fold. Without it the active dot flickers
 *  between two neighbours while a boundary sits exactly on the edge. */
export const NODE_HIT_SLACK_PX = 24;

// ── the visible window ─────────────────────────────────────────────────────
//
// The rail shows a fixed number of evenly spaced dots, not the whole session.
// Indexing every message was correct and unusable: a working session runs to
// dozens of turns, and dozens of dots on a phone's right edge is a dotted line,
// not a control. Spacing them by share of the conversation (which this
// replaces) made it worse — the dots bunched exactly where the conversation was
// densest, which is where you most need to hit one.
//
// So the rail is a PAGER over the message list rather than a map of it. Slots
// are always in the same places, which is what makes them hittable without
// looking. You reach the rest by moving the window, not by cramming it in.

/** Dots on the rail at once. Odd, so "centre on this one" has a middle slot. */
export const RAIL_WINDOW = 7;
/** How far either side of the centre the window reaches (±3). */
export const RAIL_HALF = Math.floor(RAIL_WINDOW / 2);
/** Slots of context kept between the active dot and the window's edge. Moving
 *  only when the active node reaches the second-from-last slot stops the whole
 *  rail from shuffling under a small scroll. */
const EDGE_KEEP = 1;

function clampStart(start: number, count: number): number {
  return Math.min(Math.max(start, 0), Math.max(0, count - RAIL_WINDOW));
}

/** Slots a chevron tap moves the window by — half a window, rounded down. */
export const RAIL_PAGE = RAIL_HALF;

/** Move the window by `pages × RAIL_PAGE` slots, clamped. Chevron taps only. */
export function pageWindow(current: number, dir: -1 | 1, count: number): number {
  return clampStart(current + dir * RAIL_PAGE, count);
}

/**
 * Window that keeps `active` visible with a slot of context on each side,
 * moving as little as possible from `current`.
 *
 * The minimum-movement rule is the hysteresis: scrolling within the window
 * leaves the rail alone, and crossing its edge slides it by one slot rather
 * than re-centring with a jump.
 */
export function followWindow(
  current: number,
  active: number,
  count: number
): number {
  if (count <= RAIL_WINDOW) return 0;
  if (active < 0) return clampStart(current, count);
  const lo = active - (RAIL_WINDOW - 1 - EDGE_KEEP);
  const hi = active - EDGE_KEEP;
  return clampStart(Math.min(Math.max(current, lo), hi), count);
}

/** Fraction 0..1 for slot `k` of `visible` dots — always evenly spaced. */
export function slotFraction(k: number, visible: number): number {
  if (visible <= 1) return 0.5;
  return k / (visible - 1);
}

export interface RailPosition {
  /**
   * Each node's message top edge in viewport coordinates *relative to the
   * scroller's top edge*, in transcript order. Negative means scrolled off
   * above the fold.
   */
  tops: number[];
  /** Scrolled to (or within a hair of) the very top of the transcript. */
  atTop: boolean;
  /** Scrolled to (or within a hair of) the very bottom. */
  atBottom: boolean;
  slackPx?: number;
}

/**
 * Index into `tops` of the node to highlight, or -1 when there are no nodes.
 *
 * Bottom wins over top when a transcript is short enough to be both: the
 * newest turn is what you are looking at.
 */
export function activeNodeIndex({
  tops,
  atTop,
  atBottom,
  slackPx = NODE_HIT_SLACK_PX,
}: RailPosition): number {
  if (tops.length === 0) return -1;
  if (atBottom) return tops.length - 1;
  if (atTop) return 0;

  // Last node whose message has reached the fold. Nodes are in transcript
  // order, so the first one still below the fold ends the search.
  let best = 0;
  for (let i = 0; i < tops.length; i++) {
    if (tops[i] <= slackPx) best = i;
    else break;
  }
  return best;
}
