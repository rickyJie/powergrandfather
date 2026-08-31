import { onBeforeUnmount, onMounted } from 'vue'
import { worktimeApi } from '../api/worktime'

/**
 * Fire a human-worktime heartbeat every 30s while:
 *   - the tab is visible (`document.visibilityState === 'visible'`), AND
 *   - a mouse or keyboard event has fired in the last 120s.
 *
 * Backend `HeartbeatManager` extends the current interval on each ping,
 * closes it via its 30s sweeper if pings stop, and reaps orphan rows on
 * boot — so the frontend contract is deliberately fire-and-forget.
 *
 * Attach ONCE at the app root (App.vue onMounted).
 */
const HEARTBEAT_INTERVAL_MS = 30_000
const IDLE_THRESHOLD_MS = 120_000

export function useWorktimeHeartbeat(): void {
  let timer: number | undefined
  let lastActivityMs = Date.now()

  function markActivity(): void {
    lastActivityMs = Date.now()
  }

  async function tick(): Promise<void> {
    if (document.visibilityState !== 'visible') return
    if (Date.now() - lastActivityMs > IDLE_THRESHOLD_MS) return
    try {
      await worktimeApi.heartbeat()
    } catch {
      // Silent failure — the widget renders 0s naturally when the server
      // is unreachable; nothing to alert the user about.
    }
  }

  onMounted(() => {
    // Passive listeners so we don't interfere with any downstream handler.
    window.addEventListener('mousemove', markActivity, { passive: true })
    window.addEventListener('keydown', markActivity, { passive: true })
    window.addEventListener('touchstart', markActivity, { passive: true })
    document.addEventListener('visibilitychange', markActivity)
    // Fire once immediately so a fresh page counts from load, not 30s later.
    void tick()
    timer = window.setInterval(tick, HEARTBEAT_INTERVAL_MS)
  })

  onBeforeUnmount(() => {
    if (timer !== undefined) window.clearInterval(timer)
    window.removeEventListener('mousemove', markActivity)
    window.removeEventListener('keydown', markActivity)
    window.removeEventListener('touchstart', markActivity)
    document.removeEventListener('visibilitychange', markActivity)
  })
}
