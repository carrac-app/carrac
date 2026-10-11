# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-keep class okhttp3.** { *; }
-keep class okio.** { *; }

# Keep data models for JSON parsing
-keep class cc.convexity.widget.** { *; }

# Glance
-keep class androidx.glance.** { *; }
