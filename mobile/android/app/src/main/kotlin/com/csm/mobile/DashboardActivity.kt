package com.csm.mobile

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.DrawableCompat
import com.csm.mobile.databinding.ActivityDashboardBinding
import com.google.android.material.snackbar.Snackbar

/**
 * Single-screen dashboard modeled on the reference PGF Connector:
 *   - Title / subtitle
 *   - Status pill + one-line detail (endpoint + uptime)
 *   - Primary "Connect & open" button
 *   - Secondary row: Open web / Disconnect
 *   - 6 shortcut mini-cards (Missions / Chat / Sessions / …)
 *   - SSH key card (paste + passphrase)
 *   - Server config card (host / user / port / remote / local / token)
 *   - Footer help
 *
 * There is no separate config Activity. Save writes to ConfigStore and
 * (re)starts the tunnel service; the primary button then opens the WebView.
 */
class DashboardActivity : AppCompatActivity() {

    private lateinit var binding: ActivityDashboardBinding

    private var currentStatus: TunnelStatus = TunnelStatus.IDLE
    private var connectedAtMs: Long = 0L

    private val uiTick = Handler(Looper.getMainLooper())
    private val uptimeTick = object : Runnable {
        override fun run() {
            updateStatusDetail()
            uiTick.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ConfigStore.init(this)
        binding = ActivityDashboardBinding.inflate(layoutInflater)
        setContentView(binding.root)

        showVersion()
        bindAdvancedToggle()
        bindPrimaryActions()
        bindConfigForm()
        bindKeyGen()
        observeTunnelState()
        maybeRequestNotificationPermission()

        // Auto-start service if we have a valid profile — user shouldn't
        // need to hit "Save" on every launch.
        if (ConfigStore.load()?.isValid() == true) {
            startTunnelService()
            maybeRequestBatteryExemption()
        }
    }

    /** Show the running app version in the header subtitle so it's obvious in
     *  the UI which build is installed (no more guessing from the APK name). */
    private fun showVersion() {
        val ver = try {
            packageManager.getPackageInfo(packageName, 0).versionName
        } catch (_: Throwable) {
            null
        }
        if (!ver.isNullOrBlank()) {
            binding.subtitle.text = getString(R.string.subtitle_product) + "  ·  v" + ver
        }
    }

    override fun onResume() {
        super.onResume()
        AppForeground.set(true)
        uiTick.post(uptimeTick)
    }

    override fun onPause() {
        super.onPause()
        AppForeground.set(false)
        uiTick.removeCallbacks(uptimeTick)
    }

    // ---- primary + secondary actions ----

    private fun bindPrimaryActions() {
        binding.btnConnectOpen.setOnClickListener {
            val profile = ConfigStore.load()
            if (profile == null || !profile.isValid()) {
                Snackbar.make(binding.root, R.string.toast_config_missing, Snackbar.LENGTH_LONG).show()
                return@setOnClickListener
            }
            startTunnelService()
            maybeRequestBatteryExemption()
            openWebView(null)
        }
        binding.btnOpenWeb.setOnClickListener { openWebView(null) }
        binding.btnDisconnect.setOnClickListener { stopTunnelService() }
    }

    // ---- config form (inline) ----

    private fun bindConfigForm() {
        ConfigStore.load()?.let { p ->
            binding.editHost.setText(p.host)
            binding.editPort.setText(p.port.toString())
            binding.editUser.setText(p.user)
            binding.editPkey.setText(p.privateKey)
            binding.editPassphrase.setText(p.passphrase)
            binding.editRemotePort.setText(p.remotePort.toString())
            binding.editLocalPort.setText(p.localPort.toString())
            binding.editAccessToken.setText(p.accessToken)
        }
        if (binding.editPort.text.isNullOrBlank()) binding.editPort.setText("22")
        if (binding.editRemotePort.text.isNullOrBlank()) binding.editRemotePort.setText("8000")
        if (binding.editLocalPort.text.isNullOrBlank()) binding.editLocalPort.setText("8000")

        binding.btnSave.setOnClickListener { onSave() }
    }

    // ---- in-app SSH key generation ----

    private fun bindKeyGen() {
        // Show a previously generated public key so the user can re-copy it.
        ConfigStore.loadPublicKey()?.let { showPublicKey(it) }

        binding.btnGenKey.setOnClickListener {
            binding.btnGenKey.isEnabled = false
            Thread {
                val gen = try {
                    KeyGen.generateRsa()
                } catch (t: Throwable) {
                    runOnUiThread {
                        binding.btnGenKey.isEnabled = true
                        Snackbar.make(binding.root, t.message ?: "key gen failed", Snackbar.LENGTH_LONG).show()
                    }
                    return@Thread
                }
                runOnUiThread {
                    binding.editPkey.setText(gen.privatePem)
                    binding.editPassphrase.setText("") // generated key has no passphrase
                    ConfigStore.savePublicKey(gen.publicOpenSsh)
                    showPublicKey(gen.publicOpenSsh)
                    copyToClipboard(gen.publicOpenSsh)
                    binding.btnGenKey.isEnabled = true
                    Snackbar.make(binding.root, R.string.toast_key_generated, Snackbar.LENGTH_LONG).show()
                }
            }.start()
        }

        binding.btnCopyPubkey.setOnClickListener {
            val pub = ConfigStore.loadPublicKey()
            if (pub.isNullOrBlank()) {
                Snackbar.make(binding.root, R.string.toast_no_pubkey, Snackbar.LENGTH_SHORT).show()
            } else {
                copyToClipboard(pub)
                Snackbar.make(binding.root, R.string.toast_pubkey_copied, Snackbar.LENGTH_SHORT).show()
            }
        }
    }

    private fun showPublicKey(pub: String) {
        binding.textPubkeyLabel.visibility = View.VISIBLE
        binding.textPubkey.visibility = View.VISIBLE
        binding.textPubkey.text = pub
    }

    private fun copyToClipboard(text: String) {
        val cm = getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager
        cm.setPrimaryClip(android.content.ClipData.newPlainText("pgf-ssh-pubkey", text))
    }

    private fun onSave() {
        val port = binding.editPort.text.toString().toIntOrNull() ?: 22
        val remotePort = binding.editRemotePort.text.toString().toIntOrNull() ?: 8000
        val localPort = binding.editLocalPort.text.toString().toIntOrNull() ?: remotePort
        val profile = ConfigStore.Profile(
            host = binding.editHost.text.toString().trim(),
            port = port,
            user = binding.editUser.text.toString().trim(),
            privateKey = binding.editPkey.text.toString().trim(),
            passphrase = binding.editPassphrase.text.toString(),
            remotePort = remotePort,
            localPort = localPort,
            accessToken = binding.editAccessToken.text.toString().trim(),
        )
        if (!profile.isValid()) {
            Snackbar.make(binding.root, R.string.toast_config_missing, Snackbar.LENGTH_LONG).show()
            return
        }
        ConfigStore.save(profile)
        Snackbar.make(binding.root, R.string.toast_saved, Snackbar.LENGTH_SHORT).show()
        // Reconnect with the new profile via a plain start: onStartCommand →
        // launchTunnel() already does an ATOMIC teardown+restart (restartMutex +
        // activeForwarder.close() + cancelAndJoin()) on the running instance. The
        // old stop()+postDelayed(start,400) raced a start onto a still-dying
        // instance (stopped=true) → zombie service that leaked resources and
        // never reconnected. Never call shutdown() for a mere profile change.
        startTunnelService()
    }

    // ---- advanced (collapsed) config ----

    private fun bindAdvancedToggle() {
        binding.advancedSection.visibility = View.GONE
        binding.btnAdvanced.setOnClickListener {
            val show = binding.advancedSection.visibility != View.VISIBLE
            binding.advancedSection.visibility = if (show) View.VISIBLE else View.GONE
            binding.btnAdvanced.setText(
                if (show) R.string.advanced_hide else R.string.advanced_show
            )
        }
    }

    private fun openWebView(path: String?) {
        if (currentStatus != TunnelStatus.CONNECTED) {
            Snackbar.make(binding.root, R.string.toast_not_connected, Snackbar.LENGTH_SHORT).show()
            return
        }
        val intent = Intent(this, WebViewActivity::class.java).apply {
            if (path != null) putExtra(WebViewActivity.EXTRA_PATH, path)
        }
        startActivity(intent)
    }

    // ---- tunnel state ----

    private fun observeTunnelState() {
        TunnelStateBus.state.observe(this) { snap ->
            currentStatus = snap.status
            when (snap.status) {
                TunnelStatus.CONNECTED -> if (connectedAtMs == 0L) connectedAtMs = System.currentTimeMillis()
                else -> connectedAtMs = 0L
            }
            renderStatus(snap)
        }
    }

    private fun renderStatus(snap: TunnelStateSnapshot) {
        binding.statusText.text = when (snap.status) {
            TunnelStatus.IDLE -> getString(R.string.status_idle)
            TunnelStatus.CONNECTING -> getString(R.string.status_connecting)
            TunnelStatus.CONNECTED -> getString(R.string.status_connected)
            TunnelStatus.RECONNECTING -> getString(R.string.status_reconnecting, snap.attempt)
            TunnelStatus.ERROR -> getString(R.string.status_error)
            TunnelStatus.STOPPED -> getString(R.string.status_stopped)
        }
        val bgColor = when (snap.status) {
            TunnelStatus.CONNECTED -> R.color.pgf_status_ok_bg
            TunnelStatus.CONNECTING, TunnelStatus.RECONNECTING -> R.color.pgf_status_warn_bg
            TunnelStatus.ERROR -> R.color.pgf_status_err_bg
            else -> R.color.pgf_status_bg
        }
        val bg = ContextCompat.getDrawable(this, R.drawable.status_background)?.mutate()
        if (bg != null) {
            DrawableCompat.setTint(bg, ContextCompat.getColor(this, bgColor))
            binding.statusText.background = bg
        }
        binding.btnOpenWeb.isEnabled = snap.status == TunnelStatus.CONNECTED
        updateStatusDetail(snap)
    }

    private fun updateStatusDetail(snap: TunnelStateSnapshot? = null) {
        val profile = ConfigStore.load()
        val parts = mutableListOf<String>()
        if (profile != null) {
            parts += "${profile.user}@${profile.host}:${profile.port} → :${profile.remotePort}"
        }
        if (currentStatus == TunnelStatus.CONNECTED && connectedAtMs > 0L) {
            val secs = (System.currentTimeMillis() - connectedAtMs) / 1000
            val uptime = when {
                secs < 60 -> "${secs}s"
                secs < 3600 -> "${secs / 60}m ${secs % 60}s"
                else -> "${secs / 3600}h ${(secs % 3600) / 60}m"
            }
            parts += "运行 $uptime"
        } else if (snap?.status == TunnelStatus.ERROR && snap.message.isNotBlank()) {
            parts += snap.message.take(80)
        }
        if (parts.isEmpty()) {
            binding.statusDetail.visibility = View.GONE
        } else {
            binding.statusDetail.text = parts.joinToString("  ·  ")
            binding.statusDetail.visibility = View.VISIBLE
        }
    }

    // ---- service control ----

    private fun startTunnelService() {
        val intent = Intent(this, SshTunnelService::class.java)
        ContextCompat.startForegroundService(this, intent)
    }

    /**
     * The foreground service + wakelock keep the CPU on, but Doze network
     * deferral and (especially) OEM battery-killers can still freeze/kill the
     * tunnel when the screen is off. A one-time nudge asks the user to grant a
     * battery-optimization exemption; we also point them at the OEM
     * autostart/background whitelist, which no permission can bypass.
     */
    /** Android 13+ requires a runtime grant to post notifications. Ask once on
     *  launch; if denied, the message poller's notify() calls are silently
     *  dropped by the system. */
    private fun maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        ActivityCompat.requestPermissions(
            this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF_PERM
        )
    }

    private fun maybeRequestBatteryExemption() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        if (ConfigStore.batteryPromptShown()) return
        ConfigStore.setBatteryPromptShown(true)
        Snackbar.make(binding.root, R.string.battery_opt_hint, Snackbar.LENGTH_LONG)
            .setAction(R.string.battery_opt_action) {
                val ok = runCatching {
                    startActivity(
                        Intent(
                            Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                            Uri.parse("package:$packageName"),
                        )
                    )
                }.isSuccess
                if (!ok) {
                    runCatching {
                        startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                    }
                }
                // Follow-up hint for OEM autostart whitelists.
                Snackbar.make(binding.root, R.string.battery_opt_oem_hint, Snackbar.LENGTH_LONG).show()
            }.show()
    }

    private fun stopTunnelService() {
        val intent = Intent(this, SshTunnelService::class.java).apply {
            action = SshTunnelService.ACTION_STOP
        }
        startService(intent)
    }

    private companion object {
        const val REQ_NOTIF_PERM = 7011
    }
}
