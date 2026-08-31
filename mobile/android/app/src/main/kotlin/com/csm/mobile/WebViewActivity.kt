package com.csm.mobile

import android.annotation.SuppressLint
import android.net.Uri
import android.os.Bundle
import android.view.MenuItem
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.DrawableCompat
import androidx.webkit.ServiceWorkerControllerCompat
import androidx.webkit.WebViewFeature
import com.csm.mobile.databinding.ActivityWebViewBinding

/**
 * Full-screen WebView. Loads `http://localhost:<localPort>/m/{path}`.
 *
 * Deep link: launcher passes `EXTRA_PATH` (e.g. "/missions") — we append
 * it to the base URL. If absent, we load `/m/` (SPA root).
 *
 * Access token: if the saved profile has one, we append `?token=xxx` to
 * the first load only. The mobile SPA's client.ts already knows how to
 * pluck this out of the URL and set the httpOnly cookie on subsequent
 * requests; the token stays out of the URL for later navigation.
 *
 * Tunnel service is NOT owned here — DashboardActivity owns it. This
 * activity just observes state for the top-right pill.
 */
class WebViewActivity : AppCompatActivity() {

    private lateinit var binding: ActivityWebViewBinding
    private var localPort: Int = 8000
    private var deepPath: String = "/"
    private var accessToken: String = ""
    private var firstLoadDone: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ConfigStore.init(this)
        binding = ActivityWebViewBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        val profile = ConfigStore.load() ?: run { finish(); return }
        localPort = profile.localPort
        accessToken = profile.accessToken
        deepPath = intent?.getStringExtra(EXTRA_PATH) ?: "/"

        configureWebView()
        // Pull-to-refresh is DISABLED: the SPA scrolls internally (it is a full
        // 100dvh app whose message list is the scroller, so webview.scrollY stays
        // 0), which made SwipeRefresh fire on every pull-down and STEAL the
        // upward scroll — you couldn't review chat history, and a full reload
        // would drop live WS state anyway. The app stays fresh over its WebSocket;
        // reconnect/relaunch is the manual "reload" path.
        binding.swipe.isEnabled = false
        observeTunnelState()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        val wv: WebView = binding.webview
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            useWideViewPort = true
            loadWithOverviewMode = true
            cacheMode = WebSettings.LOAD_DEFAULT
            userAgentString = "$userAgentString CSMTunnel/${BuildConfig.VERSION_NAME}"
        }
        // Enable service workers so the mobile PWA's Workbox precache registers
        // inside the WebView — without a ServiceWorkerController the SW silently
        // never installs, so the app can't warm-cache the shell or ride out a
        // laggy tunnel offline. http://localhost is a secure context, so SW is
        // permitted. Guarded: a no-op on WebView builds lacking the feature.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.SERVICE_WORKER_BASIC_USAGE)) {
            ServiceWorkerControllerCompat.getInstance().serviceWorkerWebSettings.apply {
                allowContentAccess = false
                allowFileAccess = false
            }
        }
        wv.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                binding.progressBar.show()
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                binding.progressBar.hide()
                binding.swipe.isRefreshing = false
                firstLoadDone = true
            }
        }
        wv.postDelayed({ wv.loadUrl(loadUrl(includeToken = accessToken.isNotBlank())) }, 300)
    }

    private fun observeTunnelState() {
        TunnelStateBus.state.observe(this) { snap ->
            val (text, bgColor) = when (snap.status) {
                TunnelStatus.CONNECTED -> "已连接" to R.color.status_connected
                TunnelStatus.CONNECTING -> "连接中" to R.color.status_connecting
                TunnelStatus.RECONNECTING -> "重连中" to R.color.status_connecting
                TunnelStatus.ERROR -> "错误" to R.color.status_error
                TunnelStatus.STOPPED -> "已断开" to R.color.status_idle
                TunnelStatus.IDLE -> "空闲" to R.color.status_idle
            }
            binding.statusPill.text = text
            val bg = ContextCompat.getDrawable(this, R.drawable.status_pill_bg)?.mutate()
            if (bg != null) {
                DrawableCompat.setTint(bg, ContextCompat.getColor(this, bgColor))
                binding.statusPill.background = bg
            }
        }
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == android.R.id.home) {
            finish()
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    @Suppress("OVERRIDE_DEPRECATION")
    override fun onBackPressed() {
        if (binding.webview.canGoBack()) {
            binding.webview.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onResume() {
        super.onResume()
        // Screen-on active use of a live session — ask the tunnel for the tight
        // keepalive so a cellular drop recovers in tens of seconds.
        AppForeground.set(true)
    }

    override fun onPause() {
        super.onPause()
        AppForeground.set(false)
    }

    override fun onDestroy() {
        binding.webview.stopLoading()
        binding.webview.destroy()
        super.onDestroy()
    }

    private fun loadUrl(includeToken: Boolean): String {
        val path = if (deepPath.startsWith("/")) deepPath else "/$deepPath"
        val base = "http://localhost:$localPort/m$path"
        return if (includeToken && accessToken.isNotBlank()) {
            "$base?token=${Uri.encode(accessToken)}"
        } else base
    }

    companion object {
        const val EXTRA_PATH = "csm_mobile_path"
    }
}
