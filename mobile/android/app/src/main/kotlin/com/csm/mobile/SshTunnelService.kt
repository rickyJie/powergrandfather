package com.csm.mobile

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext
import net.schmizz.sshj.DefaultConfig
import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.common.SecurityUtils
import net.schmizz.keepalive.KeepAlive
import net.schmizz.keepalive.KeepAliveProvider
import net.schmizz.keepalive.KeepAliveRunner
import net.schmizz.sshj.connection.channel.direct.LocalPortForwarder
import net.schmizz.sshj.connection.channel.direct.Parameters
import net.schmizz.sshj.transport.verification.PromiscuousVerifier
import net.schmizz.sshj.userauth.keyprovider.OpenSSHKeyFile
import net.schmizz.sshj.userauth.password.PasswordFinder
import net.schmizz.sshj.userauth.password.Resource
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.net.BindException
import java.net.InetSocketAddress
import java.net.ServerSocket

/**
 * Persistent SSH tunnel with local port forwarding. Design:
 *
 *  - Runs as a foreground service (survives Activity destruction)
 *  - Uses sshj (pure-Java) with the FULL BouncyCastle provider registered at
 *    position 1 — Android ships a stripped BC that can't do curve25519 / eddsa,
 *    so an OpenSSH 9.x server's default handshake fails with TransportException.
 *    Registering bcprov-jdk18on + pointing sshj at it fixes the negotiation.
 *  - Reconnect on drop with capped exponential backoff (2s → 30s max)
 *  - Broadcasts state via TunnelStateBus for the UI
 *  - Wake lock while connected so the OS doesn't cull us during Doze
 *
 * MVP uses PromiscuousVerifier (no known-hosts) — SSH tunnel to your own
 * workstation on a trusted LAN, MITM is not the threat model.
 */
class SshTunnelService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var tunnelJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var netCallback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var connected = false
    // True only while the loop is sleeping between reconnect attempts. The
    // network callback wakes the tunnel ONLY in this state — never mid-connect.
    @Volatile private var inBackoff = false
    // The forwarder currently blocked in listen(). Teardown MUST close THIS
    // (it interrupts the listen thread AND closes the ServerSocket) — closing
    // only the ServerSocket makes sshj's listen() busy-spin at 100% CPU forever
    // (verified against 0.38.0 bytecode: accept() on a closed socket throws
    // SocketException every iteration and, the thread never being interrupted,
    // loops straight back to accept()). That busy-spin was the active-use hang
    // where a single interaction never completes.
    @Volatile private var activeForwarder: LocalPortForwarder? = null
    // The live SSH keepalive of the current connection, so a foreground↔background
    // transition can re-tune its cadence WITHOUT tearing down the tunnel.
    @Volatile private var activeKeepAlive: KeepAlive? = null
    // Re-tune keepalive when the app moves between foreground and background, and
    // wake a backed-off tunnel the moment the user returns to it.
    private val foregroundListener: (Boolean) -> Unit = { fg ->
        activeKeepAlive?.let { applyKeepAlive(it, fg) }
        if (fg && inBackoff) restartTunnelNow()
    }
    // Serialises every tunnel (re)start so at most ONE runTunnelLoop is ever
    // alive. Cooperative coroutine cancellation does not kill the old loop
    // instantly (it can be mid `withContext { listen() }`, a blocking native
    // call), so a naive cancel()+launch let the OLD loop still own the local
    // port while the NEW loop tried to bind it → BindException every reconnect,
    // accumulating across network flaps. launchTunnel() closes the forwarder,
    // cancelAndJoin()s the old loop (waits until it is FULLY dead and the port
    // is released), then starts the new one.
    private val restartMutex = Mutex()
    // Set once in shutdown(); every launchTunnel() checks it (under the mutex)
    // so a restart queued before teardown can't spin up a fresh loop after the
    // service has stopped.
    @Volatile private var stopped = false

    // Notification poller: while the tunnel is connected, poll the backend for
    // unread notifications and raise Android system notifications for new ones
    // (so backgrounded/screen-off gets alerted). Only meaningful while connected
    // (the tunnel IS the reachability); no FCM. Started once per service.
    @Volatile private var pollerStarted = false
    private val httpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(4, TimeUnit.SECONDS)
            .readTimeout(6, TimeUnit.SECONDS)
            .build()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                Log.i(TAG, "stop action received")
                shutdown()
                return START_NOT_STICKY
            }
        }

        // This instance has already been torn down (scope cancelled, resources
        // released, stopped never resets). A start command redelivered to the
        // SAME dying instance before onDestroy (e.g. a stop quickly followed by
        // a start) must NOT re-acquire the wakelock / net callback / foreground
        // listener — onDestroy's shutdown() early-returns on `stopped`, so those
        // would leak and a listener would fire restartTunnelNow() into a dead
        // scope. Drop it; Android delivers the next start to a FRESH instance.
        if (stopped) {
            Log.w(TAG, "start on an already-stopped instance; ignoring")
            return START_NOT_STICKY
        }

        val profile = ConfigStore.load()
        if (profile == null || !profile.isValid()) {
            Log.w(TAG, "no valid config; refusing to start")
            TunnelStateBus.post(TunnelStatus.ERROR, "No SSH profile configured")
            stopSelf()
            return START_NOT_STICKY
        }

        startForeground(NOTIF_ID, buildNotification(getString(R.string.notification_title_connecting)))
        acquireWakeLock()
        registerNetworkCallback()
        AppForeground.addListener(foregroundListener)

        launchTunnel(profile)
        startMessagePoller()
        return START_STICKY
    }

    /**
     * (Re)start the tunnel loop, guaranteeing the previous one is fully dead and
     * has released the local port before the new one binds. Serialised by
     * [restartMutex] so concurrent triggers (onStartCommand + network callback)
     * can't race two loops onto the same port.
     */
    private fun launchTunnel(profile: ConfigStore.Profile) {
        if (stopped || !scope.isActive) return
        scope.launch {
            restartMutex.withLock {
                if (stopped || !scope.isActive) return@withLock
                // 1) Interrupt a listen() blocked on accept() and close its
                //    ServerSocket — this both unblocks the old loop (so the join
                //    below can complete) and frees the port.
                runCatching { activeForwarder?.close() }
                // 2) Cancel AND WAIT for the old loop to finish unwinding. Order
                //    matters: without the close() above, the old loop could be
                //    stuck in the non-cancellable withContext { listen() } and
                //    this join would hang.
                runCatching { tunnelJob?.cancelAndJoin() }
                if (stopped || !scope.isActive) return@withLock
                // 3) Only now, with the port guaranteed free, start fresh.
                tunnelJob = scope.launch { runTunnelLoop(profile) }
            }
        }
    }

    override fun onDestroy() {
        shutdown()
        super.onDestroy()
    }

    private fun shutdown() {
        if (stopped) return
        stopped = true
        // Break a listen() that may be blocked on accept(): interrupt via the
        // forwarder, never a bare ServerSocket close (that busy-spins). Then
        // cancel the WHOLE scope — that kills the tunnel loop, the watcher, and
        // any launchTunnel() coroutine queued on the mutex, so nothing can bind
        // the port after teardown. (A loop still blocked in connect()/auth is
        // abandoned; its 10s timeout unwinds it and the `stopped` guard prevents
        // any restart.)
        runCatching { activeForwarder?.close() }
        scope.cancel()
        tunnelJob = null
        connected = false
        activeKeepAlive = null
        AppForeground.removeListener(foregroundListener)
        unregisterNetworkCallback()
        releaseWakeLock()
        TunnelStateBus.post(TunnelStatus.STOPPED)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    // ---- SSH loop ----

    private suspend fun runTunnelLoop(profile: ConfigStore.Profile) {
        // Register the full BouncyCastle and make sshj use it — MUST happen
        // before the first SSHClient()/DefaultConfig is built.
        CryptoSupport.ensureModernBouncyCastle()
        SecurityUtils.setRegisterBouncyCastle(true)
        SecurityUtils.setSecurityProvider(BouncyCastleProvider.PROVIDER_NAME)

        var attempt = 0
        while (coroutineContext.isActive) {
            attempt++
            TunnelStateBus.post(
                if (attempt == 1) TunnelStatus.CONNECTING else TunnelStatus.RECONNECTING,
                "connecting to ${profile.user}@${profile.host}:${profile.port}",
                attempt,
            )
            updateNotif(getString(R.string.notification_title_connecting))

            var ssh: SSHClient? = null
            try {
                // KEEP_ALIVE provider sends `keepalive@openssh.com` global
                // requests that EXPECT a reply, so a silently-dead mobile link
                // (NAT eviction / WiFi blip) is detected within a few intervals
                // and we reconnect fast — instead of isConnected staying stuck
                // "true" while requests hang for minutes.
                val config = DefaultConfig()
                config.setKeepAliveProvider(KeepAliveProvider.KEEP_ALIVE)
                val client = SSHClient(config).apply {
                    connectTimeout = 10_000
                    timeout = 10_000
                    addHostKeyVerifier(PromiscuousVerifier())
                    connect(profile.host, profile.port)
                    val ka = connection.keepAlive
                    // Adaptive cadence: TIGHT in foreground (fast death-detection
                    // for active cellular use), LOOSE in background (Doze-tolerant,
                    // so a >60s radio-off nap doesn't tear down a HEALTHY session).
                    // See applyKeepAlive + AppForeground.
                    applyKeepAlive(ka, AppForeground.isForeground)
                    activeKeepAlive = ka
                }
                ssh = client
                // Disable Nagle on the SSH transport: streamed assistant tokens
                // are many tiny writes; coalescing them adds 40–200ms of stutter
                // over a mobile link and can look "hung" mid-response.
                runCatching { client.socket?.tcpNoDelay = true }

                val key = tempKeyProvider(profile.privateKey, profile.passphrase)
                client.authPublickey(profile.user, key)

                TunnelStateBus.post(TunnelStatus.CONNECTED, "tunnel up: :${profile.localPort} → ${profile.host}:${profile.remotePort}")
                updateNotif(getString(R.string.notification_title_connected))
                attempt = 0
                connected = true

                // ONE ServerSocket bound for the WHOLE SSH session. The old shape
                // rebound per re-listen, so 127.0.0.1:localPort had NO listener
                // during the gap and the SPA's parallel cold-load connections got
                // connection-refused (a failed first interaction). Bind once and
                // re-listen on the SAME still-bound socket when an individual
                // forwarded connection fails.
                val ss = bindLocalPort(profile.localPort)
                try {
                    val params = Parameters(
                        "127.0.0.1", profile.localPort,
                        "127.0.0.1", profile.remotePort,
                    )
                    // Session-local forwarder handle. The watcher closes THIS, not
                    // the global `activeForwarder`, so a straggler watcher from an
                    // old session can never close a NEWER job's forwarder (which
                    // would cause a spurious drop / reconnect churn). The global
                    // field remains the cross-job handle used by launchTunnel /
                    // shutdown to break into whichever loop is current.
                    var sessionForwarder: LocalPortForwarder? = null
                    // coroutineScope makes the watcher a STRUCTURED child of this
                    // job: it dies with the job on cancelAndJoin and is awaited
                    // when this block leaves — no detached straggler that could
                    // fire after the job is gone.
                    coroutineScope {
                        // Watchdog: when the SSH session drops (keepalive death)
                        // while we're blocked in accept(), tear down via
                        // forwarder.close() — it interrupts the listen thread AND
                        // closes the socket, so listen() returns. A bare ss.close()
                        // would leave the thread un-interrupted → 100% CPU busy-spin.
                        val watcher = launch {
                            while (isActive && client.isConnected && !ss.isClosed) delay(2000)
                            runCatching { sessionForwarder?.close() }
                            runCatching { ss.close() }
                        }
                        try {
                            while (coroutineContext.isActive && client.isConnected && !ss.isClosed) {
                                val forwarder = client.newLocalPortForwarder(params, ss)
                                sessionForwarder = forwarder
                                activeForwarder = forwarder
                                try {
                                    withContext(Dispatchers.IO) { forwarder.listen() }
                                } catch (t: CancellationException) {
                                    throw t // never swallow cancellation
                                } catch (t: Throwable) {
                                    // A single forwarded-connection channel-open
                                    // failure (target port momentarily down) throws
                                    // ConnectionException OUT of listen() — that is
                                    // NOT an SSH disconnect. Re-listen on same socket.
                                    Log.w(TAG, "forwarder ended (ssh alive=${client.isConnected}): ${t.message}")
                                }
                                // Small yield so a hard-down target can't hot-loop.
                                if (coroutineContext.isActive && client.isConnected && !ss.isClosed) delay(250)
                            }
                        } finally {
                            watcher.cancelAndJoin()
                        }
                    }
                } finally {
                    // NOTE: do NOT null `activeForwarder` here — a concurrent
                    // restart may have already pointed it at the NEW job's
                    // forwarder, and nulling would orphan it. A stale ref to a
                    // closed forwarder is harmless: close() is a no-op once closed.
                    runCatching { ss.close() }
                }

                connected = false
                if (!coroutineContext.isActive) break
                Log.w(TAG, "ssh session dropped; will reconnect")
            } catch (t: CancellationException) {
                // Cooperative cancellation (launchTunnel/shutdown) — exit quietly,
                // do NOT post ERROR or retry. Rethrow so the coroutine ends and
                // cancelAndJoin() can complete.
                throw t
            } catch (t: Throwable) {
                connected = false
                if (!coroutineContext.isActive) break
                Log.e(TAG, "tunnel iteration failed: ${t.message}", t)
                TunnelStateBus.post(TunnelStatus.ERROR, t.message ?: "unknown error", attempt)
                updateNotif(getString(R.string.notification_title_error) + ": ${t.javaClass.simpleName}")
            } finally {
                connected = false
                activeKeepAlive = null
                runCatching { ssh?.disconnect() }
            }

            if (!coroutineContext.isActive) break
            // Capped exponential backoff. Clamp the exponent to [0,4] — a clean
            // mid-session drop leaves attempt=0, and the old `1L shl -1` only
            // yielded delay(0) by integer-overflow accident; any refactor could
            // flip that into a huge/negative value and thrash.
            val exp = (attempt - 1).coerceIn(0, 4)
            val backoffMs = (2000L * (1L shl exp)).coerceAtMost(30_000L)
            Log.i(TAG, "reconnect in ${backoffMs}ms (attempt=$attempt)")
            inBackoff = true
            try {
                delay(backoffMs)
            } finally {
                inBackoff = false
            }
        }
    }

    /**
     * Bind 127.0.0.1:[port], retrying briefly if it is momentarily busy. With
     * launchTunnel()'s serialisation the port should already be free, but a
     * freshly-closed socket can linger for a few ms at the OS level and a
     * killed-but-not-yet-reaped prior process can hold it — so self-heal instead
     * of surfacing BindException and dropping into the reconnect/error loop.
     */
    private suspend fun bindLocalPort(port: Int): ServerSocket {
        var lastErr: BindException? = null
        repeat(15) { attempt ->
            val ss = ServerSocket().apply { reuseAddress = true }
            try {
                ss.bind(InetSocketAddress("127.0.0.1", port))
                return ss
            } catch (e: BindException) {
                lastErr = e
                runCatching { ss.close() }
                Log.w(TAG, "127.0.0.1:$port busy, retry ${attempt + 1}/15: ${e.message}")
                delay(400)
            }
        }
        throw lastErr ?: BindException("could not bind 127.0.0.1:$port")
    }

    private fun tempKeyProvider(pem: String, passphrase: String): OpenSSHKeyFile {
        val kf = OpenSSHKeyFile()
        if (passphrase.isEmpty()) {
            kf.init(pem, null)
        } else {
            kf.init(pem, null, object : PasswordFinder {
                override fun reqPassword(resource: Resource<*>?): CharArray = passphrase.toCharArray()
                override fun shouldRetry(resource: Resource<*>?): Boolean = false
            })
        }
        return kf
    }

    // ---- notification ----

    private fun buildNotification(title: String): Notification {
        val openIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val contentPI = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = Intent(this, SshTunnelService::class.java).apply {
            action = ACTION_STOP
        }
        val stopPI = PendingIntent.getService(
            this, 1, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, App.CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentIntent(contentPI)
            .addAction(0, getString(R.string.stop_action), stopPI)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun updateNotif(title: String) {
        val nm = androidx.core.app.NotificationManagerCompat.from(this)
        try {
            nm.notify(NOTIF_ID, buildNotification(title))
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS not granted; silently skip
        }
    }

    // ---- wake lock ----

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "csm:tunnel").apply {
            setReferenceCounted(false)
            // No timeout: held for the service's whole lifetime, released in
            // shutdown(). The old 12h cap silently released mid-session, after
            // which the next screen-off could freeze the tunnel.
            @Suppress("WakelockTimeout")
            acquire()
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    // ---- network-change reconnect ----

    /**
     * Watch connectivity and kick an immediate reconnect the moment a network
     * comes back (Wi-Fi↔cellular handoff, Wi-Fi reassociation, radio back after
     * Doze). Without this, a dead socket is only noticed when keepalive times
     * out (up to ~4min) plus backoff — a long perceived outage.
     */
    private fun registerNetworkCallback() {
        if (netCallback != null) return
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val req = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // Wake ONLY a tunnel that is sitting in backoff. onAvailable
                // fires at registration and repeatedly while networks validate;
                // cancelling an in-flight connect() here left the tunnel stuck
                // in a perpetual "connecting" state.
                if (inBackoff) restartTunnelNow()
            }
        }
        runCatching { cm.registerNetworkCallback(req, cb) }
        netCallback = cb
    }

    private fun unregisterNetworkCallback() {
        netCallback?.let { cb ->
            val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
            runCatching { cm.unregisterNetworkCallback(cb) }
        }
        netCallback = null
    }

    /** Restart the tunnel loop immediately (cancels any in-flight backoff wait). */
    private fun restartTunnelNow() {
        if (!scope.isActive) return
        val profile = ConfigStore.load() ?: return
        if (!profile.isValid()) return
        Log.i(TAG, "network available → immediate reconnect")
        launchTunnel(profile)
    }

    /**
     * Apply the foreground/background keepalive cadence to a live connection.
     * Foreground: 15s × 3 ≈ 45s to notice a silently-dead link (and the 15s
     * heartbeat refreshes the carrier-NAT mapping, often preventing the death
     * outright). Background: 30s × 8 ≈ 4min, the Doze-tolerant cadence that does
     * not tear down a HEALTHY session during a routine radio-off nap.
     */
    private fun applyKeepAlive(ka: KeepAlive, foreground: Boolean) {
        val interval = if (foreground) KA_INTERVAL_FG else KA_INTERVAL_BG
        val maxCount = if (foreground) KA_MAX_COUNT_FG else KA_MAX_COUNT_BG
        runCatching {
            ka.keepAliveInterval = interval
            if (ka is KeepAliveRunner) ka.setMaxAliveCount(maxCount)
        }
    }

    // ---- message notification poller ----

    private fun startMessagePoller() {
        if (pollerStarted || !scope.isActive) return
        pollerStarted = true
        scope.launch {
            val seen = HashSet<String>()
            var primed = false
            while (isActive) {
                if (connected) {
                    try {
                        val items = fetchOpenNotifications()
                        if (!primed) {
                            // First poll after connect: mark the existing backlog
                            // as seen so we DON'T dump old notifications — only
                            // alert on what arrives after the app connects.
                            for (i in 0 until items.length()) {
                                seen.add(items.getJSONObject(i).optString("id"))
                            }
                            primed = true
                        } else {
                            // Oldest-first so multiple new items post in order.
                            for (i in items.length() - 1 downTo 0) {
                                val n = items.getJSONObject(i)
                                val id = n.optString("id")
                                if (id.isNotEmpty() && seen.add(id)) {
                                    // Only raise a system notification while the
                                    // app is backgrounded — in foreground the SPA
                                    // shows it in-app, a tray alert would be noise.
                                    if (!AppForeground.isForeground) postMessageNotification(n)
                                }
                            }
                        }
                    } catch (_: Throwable) {
                        // tunnel blip / parse issue — retry next tick
                    }
                }
                delay(POLL_INTERVAL_MS)
            }
        }
    }

    private fun fetchOpenNotifications(): JSONArray {
        val profile = ConfigStore.load() ?: return JSONArray()
        val url = "http://127.0.0.1:${profile.localPort}" +
            "/api/notifications?only_unread=true&limit=20"
        val b = Request.Builder().url(url).header("X-CSM-Client", "1")
        if (profile.accessToken.isNotBlank()) b.header("x-csm-token", profile.accessToken)
        httpClient.newCall(b.build()).execute().use { resp ->
            if (!resp.isSuccessful) return JSONArray()
            val body = resp.body?.string() ?: return JSONArray()
            return JSONObject(body).optJSONArray("items") ?: JSONArray()
        }
    }

    private fun postMessageNotification(n: JSONObject) {
        val nm = androidx.core.app.NotificationManagerCompat.from(this)
        if (!nm.areNotificationsEnabled()) return
        val sid = n.optString("session_id").takeIf { it.isNotEmpty() }
        val type = n.optString("type")
        val title = n.optString("title").takeIf { it.isNotEmpty() } ?: "PowerGrandFather"
        val text = when {
            n.optString("body").isNotEmpty() -> n.optString("body")
            type == "permission_prompt" -> getString(R.string.notif_needs_input)
            else -> getString(R.string.notif_new_message)
        }
        val tapIntent = Intent(this, WebViewActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            if (sid != null) putExtra(WebViewActivity.EXTRA_PATH, "/s/$sid")
        }
        // notifId grouped by session so a session's alerts collapse/replace.
        val notifId = (sid ?: n.optString("id")).hashCode()
        val pi = PendingIntent.getActivity(
            this, notifId, tapIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = NotificationCompat.Builder(this, App.MESSAGE_CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(pi)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .build()
        try {
            nm.notify(notifId, notif)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS revoked at runtime — silently skip.
        }
    }

    companion object {
        private const val TAG = "SshTunnelService"
        private const val NOTIF_ID = 3141
        private const val POLL_INTERVAL_MS = 20_000L
        const val ACTION_STOP = "com.csm.mobile.ACTION_STOP"
        // Foreground (active cellular use): tight, fast death-detection.
        private const val KA_INTERVAL_FG = 15
        private const val KA_MAX_COUNT_FG = 3
        // Background: loose, Doze-tolerant (unchanged from the prior fixed value).
        private const val KA_INTERVAL_BG = 30
        private const val KA_MAX_COUNT_BG = 8
    }
}
