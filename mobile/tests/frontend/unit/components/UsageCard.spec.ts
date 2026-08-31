import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import Vant from "vant";
import MockAdapter from "axios-mock-adapter";

import UsageCard from "../../../../frontend/src/components/UsageCard.vue";
import { useUsageStore } from "../../../../frontend/src/stores/usage";
import { http } from "../../../../frontend/src/api/client";

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

function mountCard() {
  return mount(UsageCard, { global: { plugins: [Vant] } });
}

describe("UsageCard", () => {
  let mock: MockAdapter;

  beforeEach(() => {
    // The selected agent is persisted, so a leftover value would leak between
    // tests and silently start one of them on codex.
    localStorage.clear();
    setActivePinia(createPinia());
    mock = new MockAdapter(http);
    mock.onGet("/api/tokens/usage-live").reply(200, { latest: null, interval_min: 30 });
  });

  afterEach(() => {
    mock.restore();
  });

  it("renders both percentages and the formatted tier", async () => {
    const s = useUsageStore();
    s.snapshots = { claude: snap() as never };
    const w = mountCard();
    await w.vm.$nextTick();
    const text = w.text();
    expect(text).toContain("Max 5x");
    expect(text).toContain("17%");
    expect(text).toContain("2%");
  });

  it("colours the bar by the desktop thresholds", async () => {
    const s = useUsageStore();
    s.snapshots = { claude: snap({ session_pct: 91, week_pct: 65 }) as never };
    const w = mountCard();
    await w.vm.$nextTick();
    const fills = w.findAll(".uc-fill");
    expect(fills[0].classes()).toContain("danger");
    expect(fills[1].classes()).toContain("warn");
  });

  it("says 'no probe yet' instead of drawing 0% bars", async () => {
    // 0%-wide bars would read as "you've used nothing", which is a claim we
    // have no basis for before the first probe lands.
    const w = mountCard();
    await w.vm.$nextTick();
    expect(w.find(".uc-empty").exists()).toBe(true);
    expect(w.findAll(".uc-fill")).toHaveLength(0);
  });

  it("a null pct renders '—' and paints nothing", async () => {
    // codex has no 5h window; an empty track must not be green.
    const s = useUsageStore();
    s.snapshots = { claude: snap({ session_pct: null, session_reset: null }) as never };
    const w = mountCard();
    await w.vm.$nextTick();
    expect(w.text()).toContain("—%");
    expect(w.findAll(".uc-fill")[0].classes()).toContain("unknown");
  });

  it("flags a failed probe over the reset line", async () => {
    const s = useUsageStore();
    s.snapshots = { claude: snap({ error: "probe timed out" }) as never };
    const w = mountCard();
    await w.vm.$nextTick();
    expect(w.find(".uc-note.danger").exists()).toBe(true);
    expect(w.text()).toContain("Probe failed");
  });

  it("flags a stalled scheduler", async () => {
    const s = useUsageStore();
    s.intervalMin = 30;
    // 3 hours old, cadence 30min → well past two intervals.
    s.snapshots = {
      claude: snap({ ts: new Date(Date.now() - 3 * 3600_000).toISOString() }) as never,
    };
    const w = mountCard();
    await w.vm.$nextTick();
    expect(w.find(".uc-note.warn").exists()).toBe(true);
  });

  // ── the switch: cheap, so it gets the big target ────────────────────────
  it("switches agent on tap instead of navigating", async () => {
    const s = useUsageStore();
    s.snapshots = { claude: snap() as never };
    const w = mountCard();
    await w.vm.$nextTick();
    expect(w.text()).toContain("Claude");

    await w.find(".uc-switch").trigger("click");
    await w.vm.$nextTick();

    expect(s.agent).toBe("codex");
    expect(w.text()).toContain("Codex");
  });

  it("cycles back to claude on a second tap", async () => {
    const s = useUsageStore();
    s.snapshots = { claude: snap() as never };
    const w = mountCard();
    await w.find(".uc-switch").trigger("click");
    await w.find(".uc-switch").trigger("click");
    expect(s.agent).toBe("claude");
  });

  it("remembers the agent across a remount", async () => {
    // The drawer unmounts the card every time it closes; the choice has to
    // outlive it or the switch feels like it didn't take.
    const s = useUsageStore();
    s.snapshots = { claude: snap() as never };
    const w = mountCard();
    await w.find(".uc-switch").trigger("click");
    w.unmount();

    setActivePinia(createPinia());
    expect(useUsageStore().agent).toBe("codex");
  });

  it("reads the new agent's CACHED snapshot on switch — never a probe", async () => {
    const gets: string[] = [];
    const posts: string[] = [];
    mock.onGet("/api/tokens/usage-live").reply((cfg) => {
      gets.push(String(cfg.params?.agent ?? ""));
      return [200, { latest: null, interval_min: 30 }];
    });
    mock.onPost(/.*/).reply((cfg) => {
      posts.push(cfg.url ?? "");
      return [200, { ok: true, latest: null }];
    });

    const s = useUsageStore();
    s.snapshots = { claude: snap() as never };
    const w = mountCard();
    await w.find(".uc-switch").trigger("click");
    await new Promise((r) => setTimeout(r, 0));

    expect(gets).toContain("codex");
    expect(posts).toEqual([]);
  });

  // ── the probe: expensive, so it gets its own small target ───────────────
  it("probes only from the refresh control, for the shown agent", async () => {
    const posts: Array<string | undefined> = [];
    mock.onPost("/api/tokens/usage-live/refresh").reply((cfg) => {
      posts.push(String(cfg.params?.agent ?? ""));
      return [200, { ok: true, latest: snap({ agent: "codex" }) }];
    });

    const s = useUsageStore();
    s.snapshots = { claude: snap() as never };
    const w = mountCard();
    await w.find(".uc-switch").trigger("click"); // now on codex
    await w.find(".uc-refresh").trigger("click");
    await new Promise((r) => setTimeout(r, 0));

    expect(posts).toEqual(["codex"]);
  });

  it("says codex has no 5h window rather than drawing an empty bar", async () => {
    // An empty bar reads as "loads of headroom". Codex's 5h window doesn't
    // exist, which is a different statement.
    const s = useUsageStore();
    s.snapshots = {
      claude: snap() as never,
      codex: snap({ agent: "codex", session_pct: null, session_reset: null }) as never,
    };
    const w = mountCard();
    await w.find(".uc-switch").trigger("click");
    await w.vm.$nextTick();

    expect(w.find(".uc-na").exists()).toBe(true);
    expect(w.text()).toContain("weekly quota only");
  });

  it("does not probe on mount", async () => {
    // Mounting the drawer must never spawn a 10-30s CLI probe.
    const posts: string[] = [];
    mock.onPost(/.*/).reply((cfg) => {
      posts.push(cfg.url ?? "");
      return [200, {}];
    });
    mountCard();
    await new Promise((r) => setTimeout(r, 20));
    expect(posts).toHaveLength(0);
  });
});
