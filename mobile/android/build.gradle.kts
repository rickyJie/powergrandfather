// Root Gradle file — just declares the classpath plugins so :app can apply them.
// AGP 7.4.x is the sweet spot for local Gradle 7.5.
plugins {
    id("com.android.application") version "7.4.2" apply false
    kotlin("android") version "1.8.22" apply false
}
