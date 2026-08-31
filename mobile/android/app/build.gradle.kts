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

    buildTypes {
        release {
            // MVP: use debug-signed release too, so users can side-load a
            // release variant without needing an upload key. Real signing
            // is out of scope; add later if publishing.
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
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
