<script setup lang="ts">
import { computed, onMounted } from "vue";
import { showToast } from "vant";
import { useUsageStore } from "@/stores/usage";
import { formatTier, pctLevel } from "@/api/tokens";

/**
 * Plan quota, in the session drawer. This is the whole feature — there is no
 * detail screen. A separate route existed to hold the agent switcher and a
 * re-probe button, which is not enough to justify a second screen you have to
 * navigate to and back from on a phone.
 *
 * The two actions are split by COST, not by importance:
 *   - tapping the card cycles the agent (claude ↔ codex). Cheap, reversible,
 *     safe to fat-finger — so it gets the whole card as a target.
 *   - the refresh icon runs a real probe: spawns a CLI, blocks 10-30s, spends
 *     tokens. It gets a small, deliberate target of its own and never shares a
 *     gesture with the switch.
 *
 * Reads the cached snapshot on mount — a DB read, not a probe.
 */
const store = useUsageStore();

const agent = computed(() => store.agent);
const snap = computed(() => store.current);
const stale = computed(() => store.isStale(agent.value));
const failed = computed(() => store.probeError(agent.value));

const agentName = computed(() => (agent.value === "codex" ? "Codex" : "Claude"));
const tierLabel = computed(() => {
  const tier = formatTier(snap.value?.tier) || snap.value?.subscription_type;
  return tier ? `${agentName.value} ${tier}` : agentName.value;
});

/**
 * Codex has no 5h window at all — the backend leaves session_* null on purpose.
 * "No such window" is a different statement from "0% used" and from "the probe
 * failed", so it gets a line of its own rather than an empty bar reading as
 * plenty of headroom.
 */
const hasSessionWindow = computed(
  () => !(agent.value === "codex" && snap.value?.session_pct == null)
);

function relTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

async function reprobe() {
  if (store.probing) return;
  const a = agent.value;
  await store.probe(a);
  if (store.loadError) {
    showToast({ message: "Probe failed: " + store.loadError, type: "fail", duration: 2500 });
  } else if (store.probeError(a)) {
    showToast({ message: "The probe returned an error", type: "fail", duration: 2500 });
  } else {
    showToast({ message: "Updated", type: "success", duration: 1200 });
  }
}

onMounted(() => {
  // Cheap and cached; the drawer can afford it on every open.
  if (!snap.value) store.load(agent.value);
});
</script>

<template>
  <!-- A div, not a button: the refresh control is a button and nesting one
       inside another is invalid HTML (and swallows the inner tap on Safari). -->
  <div class="usage-card">
    <div class="uc-head">
      <button
        class="uc-switch"
        type="button"
        :aria-label="`Showing ${agentName} quota — tap to switch agent`"
        @click="store.cycleAgent()"
      >
        <span class="uc-tier">{{ tierLabel }}</span>
        <van-icon name="exchange" size="11" class="uc-swap" />
      </button>
      <span class="uc-right">
        <span class="uc-age" :class="{ stale }">
          <van-icon v-if="stale || failed" name="warning-o" size="12" />
          {{ relTime(snap?.ts) }}
        </span>
        <!-- Expensive: spawns a CLI and blocks. Small, deliberate target. -->
        <button
          class="uc-refresh"
          type="button"
          :disabled="store.probing"
          aria-label="Re-probe this agent's quota now"
          @click="reprobe"
        >
          <van-loading v-if="store.probing" size="13" />
          <van-icon v-else name="replay" size="14" />
        </button>
      </span>
    </div>

    <!-- Never probed: say so plainly rather than showing 0% bars, which would
         read as "no usage" — the opposite of "we don't know yet". -->
    <div v-if="!snap" class="uc-empty">
      {{ store.loading ? "Reading quota…" : "No probe has run yet" }}
    </div>

    <template v-else>
      <div class="uc-row">
        <span class="uc-label">5h</span>
        <div v-if="hasSessionWindow" class="uc-bar">
          <div
            class="uc-fill"
            :class="pctLevel(snap.session_pct)"
            :style="{ width: (snap.session_pct ?? 0) + '%' }"
          />
        </div>
        <span v-else class="uc-na">weekly quota only</span>
        <span v-if="hasSessionWindow" class="uc-pct" :class="pctLevel(snap.session_pct)">
          {{ snap.session_pct ?? "—" }}%
        </span>
      </div>

      <div class="uc-row">
        <span class="uc-label">wk</span>
        <div class="uc-bar">
          <div
            class="uc-fill"
            :class="pctLevel(snap.week_pct)"
            :style="{ width: (snap.week_pct ?? 0) + '%' }"
          />
        </div>
        <span class="uc-pct" :class="pctLevel(snap.week_pct)">
          {{ snap.week_pct ?? "—" }}%
        </span>
      </div>

      <div v-if="failed" class="uc-note danger">Probe failed · these numbers are stale</div>
      <div v-else-if="stale" class="uc-note warn">
        Scheduler may be stuck · tap ↻ to probe now
      </div>
      <div v-else-if="hasSessionWindow && snap.session_reset" class="uc-note">
        5h window resets {{ snap.session_reset }}
      </div>
      <div v-else-if="snap.week_reset" class="uc-note">
        Week resets {{ snap.week_reset }}
      </div>
    </template>
  </div>
</template>

<style scoped>
.usage-card {
  display: block;
  width: calc(100% - 24px);
  margin: 4px 12px 8px;
  padding: 10px 12px;
  text-align: left;
  background: var(--surface-1);
  border: 1px solid var(--outline-soft);
  border-radius: 12px;
  font: inherit;
  color: inherit;
}
.uc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
/* The agent switch. Stretches so the tap target covers the whole left half of
   the header rather than just the few characters of the label. */
.uc-switch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 1;
  min-width: 0;
  margin: -4px 0 -4px -4px;
  padding: 4px;
  border: none;
  background: transparent;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.uc-switch:active {
  background: var(--surface-2);
  border-radius: 6px;
}
.uc-swap {
  flex: none;
  color: var(--text-faint);
}
.uc-right {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.uc-refresh {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  margin: -4px -4px -4px 0;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.uc-refresh:active:not(:disabled) {
  background: var(--surface-2);
}
.uc-refresh:disabled {
  opacity: 0.5;
}
.uc-tier {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* "This window doesn't exist for this agent" — deliberately not a 0% bar. */
.uc-na {
  flex: 1;
  font-size: 11px;
  color: var(--text-faint);
}
.uc-age {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  color: var(--text-faint);
}
.uc-age.stale {
  color: var(--warning);
}
.uc-empty {
  font-size: 12px;
  color: var(--text-faint);
  padding: 2px 0 2px;
}
.uc-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 5px;
}
.uc-label {
  width: 20px;
  flex: none;
  font-size: 11px;
  color: var(--text-soft);
}
.uc-bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--surface-2);
  overflow: hidden;
}
.uc-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}
.uc-fill.ok {
  background: var(--success);
}
.uc-fill.warn {
  background: var(--warning);
}
.uc-fill.danger {
  background: var(--danger);
}
/* Unknown paints nothing — an empty track says "no data" without claiming 0%. */
.uc-fill.unknown {
  background: transparent;
}
.uc-pct {
  width: 38px;
  flex: none;
  text-align: right;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
}
.uc-pct.ok {
  color: var(--text);
}
.uc-pct.warn {
  color: var(--warning);
}
.uc-pct.danger {
  color: var(--danger);
}
.uc-pct.unknown {
  color: var(--text-faint);
}
.uc-note {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-faint);
}
.uc-note.warn {
  color: var(--warning);
}
.uc-note.danger {
  color: var(--danger);
}
</style>
