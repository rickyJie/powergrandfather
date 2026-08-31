import { onBeforeUnmount, onMounted, ref } from "vue";
import { http } from "@/api/client";

/**
 * Reactive network + backend reachability status.
 *   - `online`: browser navigator.onLine
 *   - `tunnelAlive`: last `/api/health` probe succeeded (uses X-CSM-Client)
 *
 * We probe every 20s while the tab is visible; also on 'online' event.
 */
export function useNetworkStatus() {
  // NOTE: we deliberately IGNORE navigator.onLine. This app reaches the
  // backend over an SSH tunnel to localhost — navigator.onLine reflects public
  // internet reachability (and is notoriously wrong inside a WebView), which
  // has nothing to do with whether the tunnel is up. The /api/health probe is
  // the only trustworthy signal. `online` is kept for API compatibility but is
  // always mirrored from the probe.
  const online = ref<boolean>(true);
  const tunnelAlive = ref<boolean>(true);
  let timer: number | null = null;
  let consecutiveFails = 0;

  async function probe() {
    try {
      // Generous timeout for laggy mobile SSH tunnels (channel setup can stall
      // for seconds); cache-bust so a SW can never answer from stale cache.
      await http.get("/api/health", {
        timeout: 10000,
        params: { _t: Date.now() },
        // Opt out of the client's GET-retry interceptor: this probe is its own
        // debounce (5 consecutive misses below), so a per-probe ×3 retry would
        // stack to ~9 attempts and lag the banner by 60-90s.
        __noRetry: true,
      } as Parameters<typeof http.get>[1] & { __noRetry: boolean });
      consecutiveFails = 0;
      tunnelAlive.value = true;
      online.value = true;
    } catch {
      // Debounce transient tunnel blips — only declare "unreachable" after
      // FIVE consecutive misses (each miss is a single 10s attempt, not
      // retried). Mobile SSH tunnels stall for 10-30s regularly (Doze, radio
      // handoff); 3 misses tripped the banner on healthy-but-laggy links, so the
      // threshold is looser now — a real outage still surfaces within ~100s.
      consecutiveFails += 1;
      if (consecutiveFails >= 5) {
        tunnelAlive.value = false;
        online.value = false;
      }
    }
  }

  onMounted(() => {
    // Re-probe promptly when the tab/app returns to the foreground.
    document.addEventListener("visibilitychange", onVisible);
    probe();
    timer = window.setInterval(() => {
      if (document.visibilityState === "visible") probe();
    }, 20_000);
  });

  function onVisible() {
    if (document.visibilityState === "visible") probe();
  }

  onBeforeUnmount(() => {
    document.removeEventListener("visibilitychange", onVisible);
    if (timer !== null) clearInterval(timer);
  });

  return { online, tunnelAlive, probe };
}
