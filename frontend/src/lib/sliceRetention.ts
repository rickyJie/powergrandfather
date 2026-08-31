/**
 * Last-good-answer retention for a view assembled from several independent
 * queries.
 *
 * Written for the Sessions list, which fans out three `/api/sessions` queries
 * (live interactive, live auto, closed history) and used to await them with
 * `Promise.all` — so they died together. perf.log 2026-08-25 22:16:31 caught
 * the cost: two of the three came back normally while the third sat on a
 * wedged SSH-tunnel connection for its full 16s pollGet budget, and both good
 * answers were thrown away with it.
 *
 * Per-request wedge rate over that week was ~1.2% and it is a property of the
 * connection, not the query (identical for a 185KB response and a 200B one),
 * so fanning out N requests multiplies a view's failure rate by N. Keeping the
 * last good answer per slice turns "one connection wedged" into "one section
 * is a few seconds stale" instead of "the whole list failed".
 *
 * Two things still fail loudly, because hiding them would be a lie:
 *   - every slice failing at once — that is the channel, not a connection;
 *   - one slice failing `tolerance` rounds running — no longer a blip.
 */

export interface SliceReport<K extends string, T> {
  /** Freshest value per slice; a cached one wherever this round failed. */
  values: Record<K, T>
  /** Slices served from cache this round — i.e. the stale ones. */
  stale: Set<K>
}

export interface SliceRetention<K extends string, T> {
  /**
   * Fold one round of `Promise.allSettled` results, in the same order as the
   * `keys` this was built with.
   *
   * Throws the round's first rejection when the round is not survivable, so
   * the caller's existing retry ladder / error banner keeps owning outages.
   */
  apply(results: PromiseSettledResult<T>[]): SliceReport<K, T>
}

export function createSliceRetention<K extends string, T>(
  keys: readonly K[],
  tolerance: number,
): SliceRetention<K, T> {
  const cache = {} as Record<K, T | null>
  const misses = {} as Record<K, number>
  for (const key of keys) {
    cache[key] = null
    misses[key] = 0
  }

  return {
    apply(results) {
      if (results.length !== keys.length) {
        // Silently mis-keying the cache would be worse than a loud stop: every
        // slice would then be showing another slice's rows.
        throw new Error(`sliceRetention: expected ${keys.length} results, got ${results.length}`)
      }
      const stale = new Set<K>()
      let firstError: unknown = null
      let fatal = false
      results.forEach((result, i) => {
        const key = keys[i]
        if (result.status === 'fulfilled') {
          cache[key] = result.value
          misses[key] = 0
          return
        }
        if (firstError === null) firstError = result.reason
        misses[key] += 1
        stale.add(key)
        // Nothing to fall back on (first load), or it has missed long enough
        // that calling it a blip stops being true.
        if (cache[key] === null || misses[key] >= tolerance) fatal = true
      })
      if (fatal || stale.size === keys.length) {
        throw firstError ?? new Error('sliceRetention: no slices available')
      }
      // Safe: any slice that failed either had a cached value or set `fatal`.
      return { values: cache as Record<K, T>, stale }
    },
  }
}
