// Imported rather than written as `java.util.Properties` inline: inside the
// `android { }` block `java` resolves to Gradle's java extension, not the
// package, and the script fails to compile.
import java.util.Properties

plugins {
    id("com.android.application")
    kotlin("android")
}

android {
    namespace = "com.csm.mobile"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.csm.mobile"
        // Android 8.0+ (Oreo). Required for adaptive-icon XML support
        // (mipmap-anydpi-v26). Also unlocks NotificationChannel and
        // modern WebView APIs without conditional code.
        minSdk = 26
        targetSdk = 34
        versionCode = 18
        versionName = "0.5.0"
    }

    // Release signing. The key that signs the published APK is deliberately
    // NOT in this repo; point at it from `local.properties` (gitignored):
    //
    //   pgfReleaseStoreFile=/abs/path/pgf-release.jks
    //   pgfReleaseKeyAlias=pgf-release
    //   pgfReleaseStorePasswordFile=/abs/path/pgf-release.pass
    //
    // The password is read from a FILE rather than written into
    // local.properties, because that file is created 0644 by every Android
    // tool that touches it while the password file can be 0600.
    //
    // With no keystore configured this falls back to the debug key, so a fresh
    // clone still builds. Such a build is fine to side-load but will NOT
    // upgrade over the published APK — Android refuses an update signed by a
    // different key, so uninstall first when switching.
    val releaseSigning = run {
        val props = Properties().apply {
            rootProject.file("local.properties").takeIf { it.exists() }
                ?.inputStream()?.use { load(it) }
        }
        fun setting(key: String, env: String) =
            (System.getenv(env) ?: props.getProperty(key))?.takeIf { it.isNotBlank() }

        val storePath = setting("pgfReleaseStoreFile", "PGF_RELEASE_STORE_FILE")
        val passPath = setting("pgfReleaseStorePasswordFile", "PGF_RELEASE_STORE_PASSWORD_FILE")
        val alias = setting("pgfReleaseKeyAlias", "PGF_RELEASE_KEY_ALIAS") ?: "pgf-release"
        val store = storePath?.let { file(it) }?.takeIf { it.isFile }
        val passFile = passPath?.let { file(it) }?.takeIf { it.isFile }
        if (store != null && passFile != null) {
            val secret = passFile.readText().trim()
            signingConfigs.create("release") {
                storeFile = store
                storePassword = secret
                keyAlias = alias
                keyPassword = secret
            }
        } else {
            logger.lifecycle(
                "release signing: no keystore configured, falling back to the " +
                    "debug key (see the comment in app/build.gradle.kts)"
            )
            null
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = releaseSigning ?: signingConfigs.getByName("debug")
        }
        debug {
            applicationIdSuffix = ".debug"
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }

    packagingOptions {
        resources {
            excludes += setOf(
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE*",
                "META-INF/NOTICE*",
                "META-INF/*.md",
            )
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.activity:activity-ktx:1.8.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")
    implementation("androidx.lifecycle:lifecycle-service:2.7.0")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("androidx.webkit:webkit:1.9.0")
    implementation("androidx.gridlayout:gridlayout:1.0.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")

    // OkHttp for the periodic unread-summary poll through the tunnel
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // mwiede JSch fork: actively maintained, supports OpenSSH 9.x algorithms
    // (curve25519, rsa-sha2, ed25519, strict-kex) — sshj 0.38 tripped on the
    // OpenSSH 9.6 handshake (TransportException). BouncyCastle (bcprov-jdk18on)
    // is registered at provider position 1 for the modern crypto JSch needs.
    // Do NOT add bcprov-jdk15on alongside (duplicate org.bouncycastle.* → dex fail).
    // sshj (pure-Java SSH). The OpenSSH 9.6 handshake needs curve25519 / eddsa,
    // which Android's stripped BouncyCastle can't do — SshTunnelService registers
    // the FULL bcprov-jdk18on at provider position 1 and points sshj at it
    // (see CryptoSupport). bcprov 1.77 is the version AGP 7.4.2's D8 can dex
    // (1.81 uses indy string-concat that this old D8 NPEs on).
    implementation("com.hierynomus:sshj:0.38.0")
    implementation("org.bouncycastle:bcprov-jdk18on:1.77")
    implementation("org.slf4j:slf4j-jdk14:2.0.7")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
}
