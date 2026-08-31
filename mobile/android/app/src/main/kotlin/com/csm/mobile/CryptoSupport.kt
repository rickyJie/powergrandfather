package com.csm.mobile

import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

/**
 * Android ships a stripped BouncyCastle ("BC") provider; JSch needs the full
 * one for OpenSSH 9.x crypto (curve25519, ed25519, rsa-sha2). Register the
 * bundled bcprov-jdk18on at position 1, replacing the platform stub. Mirrors
 * the reference android-connector's CryptoSupport.
 */
object CryptoSupport {
    @Synchronized
    fun ensureModernBouncyCastle() {
        val current = Security.getProvider(BouncyCastleProvider.PROVIDER_NAME)
        if (current != null && current.javaClass.name == BouncyCastleProvider::class.java.name) {
            return
        }
        if (current != null) {
            Security.removeProvider(BouncyCastleProvider.PROVIDER_NAME)
        }
        Security.insertProviderAt(BouncyCastleProvider(), 1)
    }
}
