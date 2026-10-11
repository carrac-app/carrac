package cc.convexity.widget

import android.content.Context
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

object ApiClient {
    private const val TAG = "ApiClient"
    private const val BASE_URL = BuildConfig.BASE_URL

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    fun fetchWidgetData(context: Context, symbols: String? = null): WidgetData? {
        val token = SecurePrefs.getToken(context)
        if (token.isNullOrBlank()) {
            Log.w(TAG, "No token configured — returning cached data")
            return loadCached(context)
        }

        val url = buildString {
            append("$BASE_URL/api/widget/summary?token=")
            append(token)
            if (!symbols.isNullOrBlank()) {
                append("&symbols=")
                append(symbols)
            }
        }

        val request = Request.Builder()
            .url(url)
            .header("User-Agent", "CarracWidget-Android/2.3")
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "API returned ${response.code}: ${response.message}")
                    return loadCached(context)
                }
                val body = response.body?.string() ?: return loadCached(context)
                Log.d(TAG, "Fetched ${body.length} bytes from API")
                SecurePrefs.setCachedData(context, body)
                WidgetData.fromJson(JSONObject(body))
            }
        } catch (e: Exception) {
            Log.e(TAG, "API fetch failed", e)
            loadCached(context)
        }
    }

    private fun loadCached(context: Context): WidgetData? {
        val cached = SecurePrefs.getCachedData(context) ?: return null
        return try {
            WidgetData.fromJson(JSONObject(cached))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse cached data", e)
            null
        }
    }

    data class VersionInfo(val versionCode: Int, val versionName: String)

    fun checkVersion(): VersionInfo? {
        val request = Request.Builder()
            .url("$BASE_URL/widget/android/version")
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return null
                val json = JSONObject(response.body?.string() ?: return null)
                VersionInfo(json.optInt("version_code", 0), json.optString("version_name", ""))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Version check failed", e)
            null
        }
    }
}
