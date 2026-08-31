import { ref, type Ref } from 'vue'
import { filesApi, type FileTouch } from '../api/files'

/**
 * Recent-files fetch + LRU cache + preview helper (H1 P1 extraction).
 *
 * Motivation (J4): Sessions.vue currently calls `filesApi.recent(sid, ...)`
 * on every session switch AND on every SSE tick that could have touched
 * a file. That's one HTTP round-trip per switch — cheap individually,
 * but adds up when the user is rapidly hopping between rows. This
 * composable adds a per-sid cache so repeat reads within the same
 * session are O(1), and exposes `invalidate(sid)` so the SSE watcher
 * can drop stale entries when a NEW_MESSAGE event lands.
 *
 * The cache is a simple LRU keyed by `sid`. Default cap is 20 sids —
 * roughly the working set a user shuffles between in one sitting. On
 * overflow the least-recently-touched entry is evicted; nothing else
 * is persisted across reloads (matches EventStream's in-memory-only
 * spine).
 *
 * `openPreview` / `openOssRedirect` open in a fresh browser tab so the
 * running xterm session stays put, mirroring `useXtermFileLinks.ts`.
 * They are pure `window.open` shims — no state, no cache — but live
 * here so the caller has one import for the whole "recent files"
 * surface.
 */

export interface FileSystem {
  /** Files touched by the *currently loaded* sid. Empty until first load. */
  recentFiles: Ref<FileTouch[]>
  /** Total count from the last successful load (may exceed `recentFiles.length` when a smaller limit is passed). */
  recentFilesCount: Ref<number>
  /** True while a fetch is in flight (for skeletons / disabled buttons). */
  isLoading: Ref<boolean>
  /**
   * Fetch (or serve from cache) the recent-files list for `sid`.
   * Sets `recentFiles` + `recentFilesCount` as a side effect.
   *
   * @param opts.force — bypass cache and re-fetch. Use after a
   *   NEW_MESSAGE tick, or when the user explicitly hits "refresh".
   * @param opts.limit — pass through to the API (default 50, matching
   *   the popover usage in Sessions.vue).
   */
  loadRecentFiles(sid: string, opts?: { force?: boolean; limit?: number }): Promise<void>
  /** Drop the cache entry for one sid (e.g. from an SSE NEW_MESSAGE handler). */
  invalidate(sid: string): void
  /** Nuke the entire cache (e.g. on global "refresh all" or logout). */
  invalidateAll(): void
  /** Open a local-file preview in a new tab. Pure `window.open` shim.
   *  Pass `sid` for relative-path resolution against session.cwd. */
  openPreview(path: string, sid?: string | null): void
  /** Open an s3:// URI via the server-side redirect in a new tab. */
  openOssRedirect(uri: string): void
}

interface CacheEntry {
  count: number
  items: FileTouch[]
  ts: number  // epoch ms of last successful fetch; kept for future TTL if we want one
}

const DEFAULT_CACHE_MAX_SIDS = 20

export function useFileSystem(opts?: { cacheMaxSids?: number }): FileSystem {
  const maxSids = opts?.cacheMaxSids ?? DEFAULT_CACHE_MAX_SIDS

  const recentFiles = ref<FileTouch[]>([])
  const recentFilesCount = ref(0)
  const isLoading = ref(false)

  // Map preserves insertion order → we use that as LRU order.
  // On hit: delete + re-set to bump to newest. On overflow: shift the
  // oldest key. Simple, no extra data structure, O(1) per op.
  const cache = new Map<string, CacheEntry>()

  // In-flight dedupe: if two callers ask for the same sid concurrently,
  // share the same promise so we don't fire the request twice.
  const inflight = new Map<string, Promise<void>>()

  function _bumpLru(sid: string, entry: CacheEntry): void {
    cache.delete(sid)
    cache.set(sid, entry)
    // Evict oldest if we're over the cap.
    while (cache.size > maxSids) {
      const oldestKey = cache.keys().next().value
      if (oldestKey === undefined) break
      cache.delete(oldestKey)
    }
  }

  async function loadRecentFiles(
    sid: string,
    { force = false, limit = 50 }: { force?: boolean; limit?: number } = {},
  ): Promise<void> {
    if (!sid) {
      recentFiles.value = []
      recentFilesCount.value = 0
      return
    }

    // Cache hit — serve synchronously, bump LRU.
    if (!force) {
      const hit = cache.get(sid)
      if (hit) {
        _bumpLru(sid, hit)
        recentFiles.value = hit.items
        recentFilesCount.value = hit.count
        return
      }
    }

    // Dedupe concurrent fetches for the same sid.
    const existing = inflight.get(sid)
    if (existing) return existing

    isLoading.value = true
    const p = (async () => {
      try {
        const r = await filesApi.recent(sid, limit)
        const entry: CacheEntry = { count: r.count, items: r.items, ts: Date.now() }
        _bumpLru(sid, entry)
        recentFiles.value = r.items
        recentFilesCount.value = r.count
      } catch (_) {
        // Match Sessions.vue behavior: swallow 404 / 5xx / network flap
        // silently. Callers that want to surface toasts should wrap.
        // Leave prior state intact so a transient blip doesn't blank
        // the popover.
      } finally {
        isLoading.value = false
        inflight.delete(sid)
      }
    })()
    inflight.set(sid, p)
    return p
  }

  function invalidate(sid: string): void {
    cache.delete(sid)
  }

  function invalidateAll(): void {
    cache.clear()
  }

  function openPreview(path: string, sid?: string | null): void {
    window.open(filesApi.previewUrl(path, sid), '_blank', 'noopener,noreferrer')
  }

  function openOssRedirect(uri: string): void {
    window.open(filesApi.ossRedirectUrl(uri), '_blank', 'noopener,noreferrer')
  }

  return {
    recentFiles,
    recentFilesCount,
    isLoading,
    loadRecentFiles,
    invalidate,
    invalidateAll,
    openPreview,
    openOssRedirect,
  }
}
