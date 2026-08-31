package com.csm.mobile

import java.util.concurrent.CopyOnWriteArraySet

/**
 * Process-wide foreground/background signal, fed by the Activities' onResume /
 * onPause. The SSH tunnel service reads it to pick a keepalive cadence:
 *
 *  - FOREGROUND (screen on, user actively using the app over cellular): a TIGHT
 *    keepalive so a silently-dead TCP (carrier-NAT eviction, cell handoff with
 *    the network still "up" so no NetworkCallback fires) is detected in tens of
 *    seconds, not the ~4 min the loose cadence takes.
 *  - BACKGROUND (screen off / app not visible): the LOOSE, Doze-tolerant cadence
 *    so a legitimate radio-off nap (>60s is routine) doesn't tear down a HEALTHY
 *    session and cause reconnect churn / battery drain.
 *
 * This is deliberately dependency-free (no androidx.lifecycle-process) — two
 * Activity callbacks are enough for a single-Activity-at-a-time app.
 */
object AppForeground {
    @Volatile
    var isForeground: Boolean = false
        private set

    private val listeners = CopyOnWriteArraySet<(Boolean) -> Unit>()

    fun set(foreground: Boolean) {
        if (isForeground == foreground) return
        isForeground = foreground
        listeners.forEach { runCatching { it(foreground) } }
    }

    fun addListener(l: (Boolean) -> Unit) {
        listeners.add(l)
    }

    fun removeListener(l: (Boolean) -> Unit) {
        listeners.remove(l)
    }
}
