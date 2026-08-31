package com.csm.mobile

import android.util.Base64
import java.io.ByteArrayOutputStream
import java.math.BigInteger
import java.security.KeyPairGenerator
import java.security.interfaces.RSAPrivateCrtKey
import java.security.interfaces.RSAPublicKey

/**
 * In-app SSH keypair generator. Generate ONCE on the phone: the private key is
 * kept locally (stored encrypted by ConfigStore) and used for the tunnel; the
 * public key is shown to the user to paste into the workstation's
 * `~/.ssh/authorized_keys` a single time.
 *
 * Output formats are chosen for zero-dependency compatibility:
 *   - private key: PKCS#1 PEM ("BEGIN RSA PRIVATE KEY") — parsed directly by
 *     sshj's OpenSSHKeyFile (see SshTunnelService.tempKeyProvider).
 *   - public key: OpenSSH authorized_keys line ("ssh-rsa AAAA... comment").
 *
 * Everything is pure JCA + a tiny hand-rolled DER writer — no BouncyCastle on
 * the app compile classpath (avoids duplicate org.bouncycastle.* dexing).
 */
object KeyGen {

    data class Generated(val privatePem: String, val publicOpenSsh: String)

    fun generateRsa(comment: String = "pgf-connector", bits: Int = 3072): Generated {
        val kpg = KeyPairGenerator.getInstance("RSA")
        kpg.initialize(bits)
        val kp = kpg.generateKeyPair()

        val priv = kp.private as RSAPrivateCrtKey
        val pkcs1 = derSequence(
            derInteger(BigInteger.ZERO) +           // version
                derInteger(priv.modulus) +
                derInteger(priv.publicExponent) +
                derInteger(priv.privateExponent) +
                derInteger(priv.primeP) +
                derInteger(priv.primeQ) +
                derInteger(priv.primeExponentP) +
                derInteger(priv.primeExponentQ) +
                derInteger(priv.crtCoefficient)
        )
        val privatePem = pem("RSA PRIVATE KEY", pkcs1)

        val pub = kp.public as RSAPublicKey
        val blob = rsaOpensshBlob(pub.publicExponent, pub.modulus)
        val publicOpenSsh =
            "ssh-rsa " + Base64.encodeToString(blob, Base64.NO_WRAP) + " " + comment

        return Generated(privatePem, publicOpenSsh)
    }

    // ---- PEM ----

    private fun pem(type: String, der: ByteArray): String {
        val b64 = Base64.encodeToString(der, Base64.NO_WRAP)
        val sb = StringBuilder("-----BEGIN $type-----\n")
        var i = 0
        while (i < b64.length) {
            val end = minOf(i + 64, b64.length)
            sb.append(b64, i, end).append('\n')
            i = end
        }
        sb.append("-----END $type-----\n")
        return sb.toString()
    }

    // ---- minimal DER ----

    private fun derLen(len: Int): ByteArray {
        if (len < 0x80) return byteArrayOf(len.toByte())
        val bytes = ArrayList<Byte>()
        var v = len
        while (v > 0) {
            bytes.add(0, (v and 0xff).toByte())
            v = v ushr 8
        }
        val out = ByteArray(bytes.size + 1)
        out[0] = (0x80 or bytes.size).toByte()
        for (i in bytes.indices) out[i + 1] = bytes[i]
        return out
    }

    private fun derInteger(v: BigInteger): ByteArray {
        // BigInteger.toByteArray() is minimal two's-complement big-endian, with
        // a leading 0x00 when the MSB would otherwise flip the sign — exactly
        // what a DER positive INTEGER needs.
        val b = v.toByteArray()
        return byteArrayOf(0x02) + derLen(b.size) + b
    }

    private fun derSequence(content: ByteArray): ByteArray {
        return byteArrayOf(0x30) + derLen(content.size) + content
    }

    // ---- OpenSSH public key wire format: string "ssh-rsa" + mpint e + mpint n ----

    private fun rsaOpensshBlob(e: BigInteger, n: BigInteger): ByteArray {
        val out = ByteArrayOutputStream()
        writeLenPrefixed(out, "ssh-rsa".toByteArray(Charsets.US_ASCII))
        writeLenPrefixed(out, e.toByteArray()) // mpint: same minimal signed big-endian
        writeLenPrefixed(out, n.toByteArray())
        return out.toByteArray()
    }

    private fun writeLenPrefixed(out: ByteArrayOutputStream, bytes: ByteArray) {
        val len = bytes.size
        out.write((len ushr 24) and 0xff)
        out.write((len ushr 16) and 0xff)
        out.write((len ushr 8) and 0xff)
        out.write(len and 0xff)
        out.write(bytes)
    }
}
