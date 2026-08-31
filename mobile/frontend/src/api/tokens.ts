import { http } from "./client";

/**
 * Plan-quota surface. Read-only on mobile: the phone answers "can I keep
 * going?", the desktop console owns filtering / trends / export / alert rules.
 *
 * The percentages here are NOT a CSM estimate. ADR-0001 (no-quota-percentage)
 * forbids inventing a denominator, and it still holds — these come from
 * Claude Code's own `/usage` panel (codex: `/status`), scraped by the backend's
 * usage probe. Do not "clean this up" by deleting it on ADR-0001 grounds.
 */

/** One probe result. Shape is shared by both agents; codex leaves session_* null. */
export interface UsageSnapshot {
  ts: string;
  agent: string;
  /** 5h rolling window %, 0-100. Always null for codex — it has no 5h window. */
  session_pct: number | null;
  /** Human string straight from the CLI panel, e.g. "6pm (Asia/Shanghai)". */
  session_reset: string | null;
  week_pct: number | null;
  week_reset: string | null;
  /** e.g. "default_claude_max_5x" — machine-ish, needs formatting for display. */
  tier: string | null;
  /** e.g. "max" — already display-ready. */
  subscription_type: string | null;
  source: string | null;
  duration_ms: number | null;
  /** Non-null when the probe itself failed. The numbers above are then stale. */
  error: string | null;
}

export interface UsageLive {
  latest: UsageSnapshot | null;
  /** Scheduler cadence in minutes; drives the staleness threshold. */
  interval_min: number;
}

// `/api/tokens/quota` (absolute counts + burn rate for the 5h window) was read
// here until 2026-08-31. It is a CROSS-AGENT total, and the only surface left
// on mobile is a card labelled with one agent — those numbers under a "Claude"
// heading read as Claude's, which they are not. Removed rather than left as an
// unused client, so nobody assumes the phone still shows burn rate. Desktop
// Tokens.vue is where it lives.

export type UsageAgent = "claude" | "codex";

export const tokensApi = {
  /** Cached snapshot — a DB read, milliseconds. Safe to call on every open. */
  usageLive: async (agent: UsageAgent = "claude"): Promise<UsageLive> =>
    (await http.get("/api/tokens/usage-live", { params: { agent } })).data,

  /**
   * Force a probe. EXPENSIVE: spawns a real CLI process and blocks 10-30s, so
   * only ever call this from an explicit user gesture — never on mount, never
   * on a timer. The scheduler already probes every `interval_min`.
   */
  usageLiveRefresh: async (
    agent: UsageAgent = "claude",
    timeoutMs = 45_000
  ): Promise<{ ok: boolean; latest: UsageSnapshot | null }> =>
    (
      await http.post(
        "/api/tokens/usage-live/refresh",
        null,
        { params: { agent }, timeout: timeoutMs }
      )
    ).data,
};

/**
 * "default_claude_max_5x" → "Max 5x". Mirrors the desktop's `_formatTier` so
 * the two consoles name the same plan the same way.
 */
export function formatTier(tier: string | null | undefined): string {
  if (!tier) return "";
  return tier
    .replace(/^default_/, "")
    .replace(/^claude_/, "")
    .split("_")
    .map((w) => (/^\d/.test(w) ? w : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/** Desktop's thresholds, verbatim — one plan, one colour language. */
export function pctLevel(pct: number | null | undefined): "ok" | "warn" | "danger" | "unknown" {
  if (pct == null) return "unknown";
  if (pct >= 85) return "danger";
  if (pct >= 60) return "warn";
  return "ok";
}
