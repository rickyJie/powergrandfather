package com.csm.mobile

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Persists the single SSH profile used for the tunnel. Backed by
 * EncryptedSharedPreferences (AES256 via Android KeyStore) — the raw
 * private key + optional access token never land on disk in plaintext.
 *
 * MVP scope: one profile total. Editing overwrites.
 */
object ConfigStore {
    private const val PREF_NAME = "csm_tunnel_secure"
    private const val K_HOST = "host"
    private const val K_PORT = "port"
    private const val K_USER = "user"
    private const val K_PKEY = "private_key"
    private const val K_PASSPHRASE = "passphrase"
    private const val K_REMOTE_PORT = "remote_port"
    private const val K_LOCAL_PORT = "local_port"
    private const val K_ACCESS_TOKEN = "access_token"
    private const val K_PUBKEY = "public_key"
    private const val K_BATTERY_PROMPT_SHOWN = "battery_prompt_shown"

    private lateinit var prefs: SharedPreferences

    fun init(context: Context) {
        if (::prefs.isInitialized) return
        val masterKey = MasterKey.Builder(context.applicationContext)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        prefs = EncryptedSharedPreferences.create(
            context.applicationContext,
            PREF_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    data class Profile(
        val host: String,
        val port: Int,
        val user: String,
        val privateKey: String,
        val passphrase: String,
        val remotePort: Int,
        val localPort: Int,
        val accessToken: String = "",
    ) {
        fun isValid(): Boolean =
            host.isNotBlank() &&
                port in 1..65535 &&
                user.isNotBlank() &&
                privateKey.isNotBlank() &&
                remotePort in 1..65535 &&
                localPort in 1..65535
    }

    fun load(): Profile? {
        if (!::prefs.isInitialized) return null
        val host = prefs.getString(K_HOST, null) ?: return null
        val user = prefs.getString(K_USER, null) ?: return null
        val pkey = prefs.getString(K_PKEY, null) ?: return null
        if (host.isBlank() || user.isBlank() || pkey.isBlank()) return null
        return Profile(
            host = host,
            port = prefs.getInt(K_PORT, 22),
            user = user,
            privateKey = pkey,
            passphrase = prefs.getString(K_PASSPHRASE, "") ?: "",
            remotePort = prefs.getInt(K_REMOTE_PORT, 8000),
            localPort = prefs.getInt(K_LOCAL_PORT, 8000),
            accessToken = prefs.getString(K_ACCESS_TOKEN, "") ?: "",
        )
    }

    fun save(profile: Profile) {
        prefs.edit()
            .putString(K_HOST, profile.host.trim())
            .putInt(K_PORT, profile.port)
            .putString(K_USER, profile.user.trim())
            .putString(K_PKEY, profile.privateKey.trim())
            .putString(K_PASSPHRASE, profile.passphrase)
            .putInt(K_REMOTE_PORT, profile.remotePort)
            .putInt(K_LOCAL_PORT, profile.localPort)
            .putString(K_ACCESS_TOKEN, profile.accessToken.trim())
            .apply()
    }

    /** Public key (OpenSSH authorized_keys line) for the in-app generated pair.
     *  Persisted so the user can re-copy it any time without regenerating. */
    fun savePublicKey(pub: String) {
        if (::prefs.isInitialized) prefs.edit().putString(K_PUBKEY, pub).apply()
    }

    fun loadPublicKey(): String? =
        if (::prefs.isInitialized) prefs.getString(K_PUBKEY, null) else null

    /** One-time gate for the battery-optimization exemption nudge — we ask
     *  once, not on every launch. */
    fun batteryPromptShown(): Boolean =
        if (::prefs.isInitialized) prefs.getBoolean(K_BATTERY_PROMPT_SHOWN, false) else true

    fun setBatteryPromptShown(shown: Boolean) {
        if (::prefs.isInitialized) prefs.edit().putBoolean(K_BATTERY_PROMPT_SHOWN, shown).apply()
    }

    fun clear() {
        if (::prefs.isInitialized) prefs.edit().clear().apply()
    }
}
