import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import MockAdapter from "axios-mock-adapter";
import { useUsageStore } from "../../../../frontend/src/stores/usage";
import { formatTier, pctLevel } from "../../../../frontend/src/api/tokens";
import { http } from "../../../../frontend/src/api/client";

// Real wire shape from GET /api/tokens/usage-live, captured off the running
// backend. These tests pin the CONTRACT — notably that codex sends
// session_pct: null rather than 0, which the UI must not render as "no usage".
function snap(over: Record<string, unknown> = {}) {
  return {
    ts: new Date().toISOString(),
    agent: "claude",
    session_pct: 17,
    session_reset: "6pm (Asia/Shanghai)",
    week_pct: 2,
    week_reset: "Sep 6, 12am (Asia/Shanghai)",
    tier: "default_claude_max_5x",
    subscription_type: "max",
    source: "scheduled",
    duration_ms: 8238,
    error: null,
    ...over,
  };
}

describe("usage store", () => {
  let mock: MockAdapter;

  beforeEach(() => {
    // The selected agent is persisted; a leftover value would leak between
    // tests and silently start one of them on codex.
    localStorage.clear();
    setActivePinia(createPinia());
    mock = new MockAdapter(http);
  });

  afterEach(() => {
    mock.restore();
  });

  it("loads a snapshot and adopts the scheduler cadence", async () => {
    mock.onGet("/api/tokens/usage-live").reply(200, {
      latest: snap(),
      interval_min: 30,
    });
    const s = useUsageStore();
    await s.load("claude");
    expect(s.claude?.session_pct).toBe(17);
    expect(s.intervalMin).toBe(30);
    expect(s.loadError).toBeNull();
  });

  it("keeps agents in separate slots", async () => {
    mock
      .onGet("/api/tokens/usage-live", { params: { agent: "claude" } })
      .reply(200, { latest: snap(), interval_min: 30 });
    mock
      .onGet("/api/tokens/usage-live", { params: { agent: "codex" } })
      .reply(200, {
        latest: snap({ agent: "codex", session_pct: null, session_reset: null, week_pct: 44 }),
        interval_min: 30,
      });
    const s = useUsageStore();
    await s.load("claude");
    await s.load("codex");
    // Loading codex must not clobber claude — the drawer card reads claude
    // while the detail view may be sitting on codex.
    expect(s.snapshotFor("claude")?.session_pct).toBe(17);
    expect(s.snapshotFor("codex")?.session_pct).toBeNull();
    expect(s.snapshotFor("codex")?.week_pct).toBe(44);
  });

  it("a transport failure is recorded, not thrown", async () => {
    mock.onGet("/api/tokens/usage-live").networkError();
    const s = useUsageStore();
    await s.load("claude");
    expect(s.loadError).toBeTruthy();
    expect(s.loading).toBe(false);
  });

  describe("isStale", () => {
    it("fresh snapshot is not stale", async () => {
      mock.onGet("/api/tokens/usage-live").reply(200, {
        latest: snap({ ts: "2026-08-30T06:00:00+00:00" }),
        interval_min: 30,
      });
      const s = useUsageStore();
      await s.load("claude");
      // 10 min later, cadence 30 → well inside.
      expect(s.isStale("claude", Date.parse("2026-08-30T06:10:00Z"))).toBe(false);
    });

    it("goes stale only past TWO intervals", async () => {
      mock.onGet("/api/tokens/usage-live").reply(200, {
        latest: snap({ ts: "2026-08-30T06:00:00+00:00" }),
        interval_min: 30,
      });
      const s = useUsageStore();
      await s.load("claude");
      // 45 min = one missed tick = ordinary jitter, still not stale.
      expect(s.isStale("claude", Date.parse("2026-08-30T06:45:00Z"))).toBe(false);
      // 70 min > 2×30 → the scheduler is genuinely behind.
      expect(s.isStale("claude", Date.parse("2026-08-30T07:10:00Z"))).toBe(true);
    });

    it("a never-probed agent is empty, not stale", () => {
      const s = useUsageStore();
      expect(s.isStale("codex")).toBe(false);
    });
  });

  it("surfaces a failed probe distinctly from a transport failure", async () => {
    // The numbers are still there but they are silently old — this is how the
    // NONESSENTIAL_TRAFFIC breakage hid for so long.
    mock.onGet("/api/tokens/usage-live").reply(200, {
      latest: snap({ error: "probe timed out after 30s" }),
      interval_min: 30,
    });
    const s = useUsageStore();
    await s.load("claude");
    expect(s.probeError("claude")).toBe("probe timed out after 30s");
    expect(s.loadError).toBeNull();
    expect(s.claude?.session_pct).toBe(17);
  });

  it("probe() replaces the snapshot and clears the in-flight flag", async () => {
    mock
      .onPost("/api/tokens/usage-live/refresh")
      .reply(200, { ok: true, latest: snap({ session_pct: 42 }) });
    const s = useUsageStore();
    await s.probe("claude");
    expect(s.claude?.session_pct).toBe(42);
    expect(s.probing).toBe(false);
  });

  it("probe() is not re-entrant", async () => {
    let calls = 0;
    mock.onPost("/api/tokens/usage-live/refresh").reply(() => {
      calls += 1;
      return new Promise((resolve) =>
        setTimeout(() => resolve([200, { ok: true, latest: snap() }]), 20)
      );
    });
    const s = useUsageStore();
    // A double-tap on a 10-30s button must not launch two CLI processes.
    const a = s.probe("claude");
    const b = s.probe("claude");
    await Promise.all([a, b]);
    expect(calls).toBe(1);
  });

  // ── agent selection ─────────────────────────────────────────────────────
  it("defaults to claude and exposes the selected agent's snapshot", async () => {
    mock.onGet("/api/tokens/usage-live").reply(200, { latest: snap(), interval_min: 30 });
    const s = useUsageStore();
    expect(s.agent).toBe("claude");
    await s.load("claude");
    expect(s.current?.session_pct).toBe(17);
  });

  it("setAgent pulls the cached snapshot, and never probes", async () => {
    const posts: string[] = [];
    mock.onGet("/api/tokens/usage-live").reply((cfg) => [
      200,
      { latest: snap({ agent: String(cfg.params?.agent ?? "") }), interval_min: 30 },
    ]);
    mock.onPost(/.*/).reply((cfg) => {
      posts.push(cfg.url ?? "");
      return [200, {}];
    });

    const s = useUsageStore();
    s.setAgent("codex");
    await new Promise((r) => setTimeout(r, 0));

    expect(s.agent).toBe("codex");
    expect(s.snapshotFor("codex")).not.toBeNull();
    expect(posts).toEqual([]);
  });

  it("setAgent does not re-fetch an agent already in hand", async () => {
    let gets = 0;
    mock.onGet("/api/tokens/usage-live").reply(() => {
      gets += 1;
      return [200, { latest: snap(), interval_min: 30 }];
    });
    const s = useUsageStore();
    s.snapshots = { codex: snap({ agent: "codex" }) as never };
    s.setAgent("codex");
    await new Promise((r) => setTimeout(r, 0));
    expect(gets).toBe(0);
  });

  it("cycleAgent walks the ring and persists the choice", async () => {
    mock.onGet("/api/tokens/usage-live").reply(200, { latest: snap(), interval_min: 30 });
    const s = useUsageStore();
    s.cycleAgent();
    expect(s.agent).toBe("codex");
    expect(localStorage.getItem("csm.usage.agent")).toBe("codex");
    s.cycleAgent();
    expect(s.agent).toBe("claude");
  });

  it("restores the persisted agent on a fresh store", () => {
    localStorage.setItem("csm.usage.agent", "codex");
    setActivePinia(createPinia());
    expect(useUsageStore().agent).toBe("codex");
  });

  it("ignores a junk persisted value rather than showing a dead agent", () => {
    localStorage.setItem("csm.usage.agent", "gemini");
    setActivePinia(createPinia());
    expect(useUsageStore().agent).toBe("claude");
  });
});

describe("tokens api helpers", () => {
  it("formatTier strips the machine prefixes", () => {
    expect(formatTier("default_claude_max_5x")).toBe("Max 5x");
    expect(formatTier(null)).toBe("");
  });

  it("pctLevel matches the desktop thresholds", () => {
    expect(pctLevel(17)).toBe("ok");
    expect(pctLevel(59)).toBe("ok");
    expect(pctLevel(60)).toBe("warn");
    expect(pctLevel(84)).toBe("warn");
    expect(pctLevel(85)).toBe("danger");
    // null is "we don't know", which must not paint green.
    expect(pctLevel(null)).toBe("unknown");
  });
});
