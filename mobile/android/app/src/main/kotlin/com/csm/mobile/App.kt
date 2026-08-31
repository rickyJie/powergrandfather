package com.csm.mobile

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        instance = this
        createNotificationChannel()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.notification_channel_desc)
                setShowBadge(false)
            }
            nm.createNotificationChannel(channel)

            // Separate HIGH-importance channel for session events (new message /
            // waiting for input) so they can pop a heads-up + badge, unlike the
            // silent ongoing tunnel notification.
            val msgChannel = NotificationChannel(
                MESSAGE_CHANNEL_ID,
                getString(R.string.notification_msg_channel_name),
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = getString(R.string.notification_msg_channel_desc)
                setShowBadge(true)
            }
            nm.createNotificationChannel(msgChannel)
        }
    }

    companion object {
        const val CHANNEL_ID = "csm_tunnel"
        const val MESSAGE_CHANNEL_ID = "csm_messages"
        lateinit var instance: App
            private set
    }
}
