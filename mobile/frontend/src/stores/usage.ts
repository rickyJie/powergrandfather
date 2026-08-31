import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { tokensApi, type UsageAgent, type UsageSnapshot } from "@/api/tokens";

/**
 * Plan-quota state, keyed by agent.
 *
 * Read path is cheap (a cached snapshot the backend already has) so the drawer
 * card can load it freely. The WRITE path — `probe()` — spawns a real CLI and
 * blocks 10-30s, so it is only ever wired to an explicit tap. Nothing in here
 * polls: the backend scheduler owns cadence, and a phone re-probing on every
 * drawer open would burn tokens and battery for numbers that move slowly.
 *
 * The selected agent lives here rather than in the card so it survives the
 * drawer closing — the card is unmounted every time.
 */
const AGENT_KEY = "csm.usage.agent";
const AGENTS: UsageAgent[] = ["claude", "codex"];

function loadStoredAgent(): UsageAgent {
  try {
    const v = localStorage.getItem(AGENT_KEY);
    if (v && (AGENTS as string[]).includes(v)) return v as UsageAgent;
  } catch {
    // Private mode / storage disabled — the default is fine.
  }
  return "claude";
}

export const useUsageStore = defineStore("usage", () => {
  const snapshots = ref<Record<string, UsageSnapshot | null>>({});
  const intervalMin = ref(30);
  const loading = ref(false);
  /** Set while a user-triggered probe is in flight — the long one. */
  const probing = ref(false);
  /** Transport-level failure (offline, tunnel blip). Distinct from probe error. */
  const loadError = ref<string | null>(null);
  /** Which agent the quota card is showing. Persisted across app launches. */
  const agent = ref<UsageAgent>(loadStoredAgent());

  const claude = computed(() => snapshots.value.claude ?? null);
  const current = computed(() => snapshots.value[agent.value] ?? null);

  /** Switch agents. Pulls that agent's CACHED snapshot if we don't have one —
   *  a DB read, never a probe, so a tap can never cost 30 seconds. */
  function setAgent(next: UsageAgent) {
    agent.value = next;
    try {
      localStorage.setItem(AGENT_KEY, next);
    } catch {
      // Not worth failing the switch over.
    }
    if (!snapshots.value[next]) load(next);
  }

  /** Advance to the next agent in the ring — what tapping the card does. */
  function cycleAgent() {
    const i = AGENTS.indexOf(agent.value);
    setAgent(AGENTS[(i + 1) % AGENTS.length]);
  }

  function snapshotFor(agent: UsageAgent): UsageSnapshot | null {
    return snapshots.value[agent] ?? null;
  }

  /**
   * True when the scheduler has visibly missed its slot. Two intervals of grace,
   * because one missed tick is normal jitter — this is meant to catch "the
   * scheduler is stuck", which is a real failure mode, not to nag.
   *
   * A never-probed agent is NOT stale; it's empty, which the UI says differently.
   */
  function isStale(agent: UsageAgent, now: number = Date.now()): boolean {
    const snap = snapshotFor(agent);
    if (!snap?.ts) return false;
    const t = new Date(snap.ts).getTime();
    if (Number.isNaN(t)) return false;
    return now - t > intervalMin.value * 2 * 60_000;
  }

  /**
   * A probe that ran but failed. Worth surfacing loudly: the numbers on screen
   * are then silently old. This is exactly how the
   * CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC breakage hid — the panel kept
   * showing the last good values and nothing said the fetch had stopped working.
   */
  function probeError(agent: UsageAgent): string | null {
    return snapshotFor(agent)?.error ?? null;
  }

  async function load(agent: UsageAgent = "claude") {
    loading.value = true;
    loadError.value = null;
    try {
      const data = await tokensApi.usageLive(agent);
      snapshots.value = { ...snapshots.value, [agent]: data.latest };
      if (data.interval_min) intervalMin.value = data.interval_min;
    } catch (e) {
      loadError.value = e instanceof Error ? e.message : String(e);
    } finally {
      loading.value = false;
    }
  }

  /** User-triggered probe. Long; caller should show progress. */
  async function probe(agent: UsageAgent = "claude") {
    if (probing.value) return;
    probing.value = true;
    loadError.value = null;
    try {
      const res = await tokensApi.usageLiveRefresh(agent);
      if (res.latest) {
        snapshots.value = { ...snapshots.value, [agent]: res.latest };
      }
    } catch (e) {
      loadError.value = e instanceof Error ? e.message : String(e);
    } finally {
      probing.value = false;
    }
  }

  return {
    snapshots,
    intervalMin,
    loading,
    probing,
    loadError,
    agent,
    claude,
    current,
    setAgent,
    cycleAgent,
    snapshotFor,
    isStale,
    probeError,
    load,
    probe,
  };
});
