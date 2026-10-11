plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Base URL of the Carrac instance the widget talks to. Set `carracBaseUrl` in
// local.properties, ~/.gradle/gradle.properties, or with -PcarracBaseUrl=...
// The default is a placeholder that never resolves, so an unconfigured build
// cannot send a widget token to a domain you do not control.
val carracBaseUrl: String = (
    findProperty("carracBaseUrl") as String?
        ?: rootProject.file("local.properties").takeIf { it.exists() }?.let { f ->
            java.util.Properties().apply { f.inputStream().use { load(it) } }
                .getProperty("carracBaseUrl")
        }
        ?: "https://carrac.example.invalid"
    ).trim().trimEnd('/')

android {
    namespace = "cc.convexity.widget"
    compileSdk = 34

    defaultConfig {
        applicationId = "cc.convexity.widget"
        minSdk = 26
        targetSdk = 34
        versionCode = 7
        versionName = "2.3.0"
        buildConfigField("String", "BASE_URL", "\"$carracBaseUrl\"")
    }

    signingConfigs {
        create("release") {
            storeFile = file("../keystore/release.jks")
            storePassword = System.getenv("KEYSTORE_PASSWORD") ?: findProperty("KEYSTORE_PASSWORD") as? String ?: ""
            keyAlias = "convexity"
            keyPassword = System.getenv("KEYSTORE_PASSWORD") ?: findProperty("KEYSTORE_PASSWORD") as? String ?: ""
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("release")
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
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.8"
    }
}

dependencies {
    // Glance (Compose-based widgets)
    implementation("androidx.glance:glance-appwidget:1.0.0")
    implementation("androidx.glance:glance-material3:1.0.0")

    // WorkManager (periodic background updates)
    implementation("androidx.work:work-runtime-ktx:2.9.0")

    // Networking
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // Encrypted storage
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // JSON parsing
    implementation("org.json:json:20231013")

    // Core AndroidX
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.activity:activity-ktx:1.8.2")
}
