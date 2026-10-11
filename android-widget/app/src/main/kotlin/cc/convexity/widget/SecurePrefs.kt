package cc.convexity.widget

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

object SecurePrefs {
    private const val TAG = "SecurePrefs"
    private const val PREFS_NAME = "convexity_widget_prefs"
    private const val FALLBACK_PREFS = "convexity_widget_fallback"
    private const val KEY_TOKEN = "widget_token"
    private const val KEY_SYMBOLS = "widget_symbols"
    private const val KEY_CACHED_DATA = "cached_data"
    private const val KEY_LAST_UPDATE_CHECK = "last_update_check"

    private fun getPrefs(context: Context): SharedPreferences {
        return try {
            val masterKey = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()

            EncryptedSharedPreferences.create(
                context,
                PREFS_NAME,
                masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
        } catch (e: Exception) {
            // EncryptedSharedPreferences can corrupt after OS/app updates.
            // Fall back to plain prefs so the widget doesn't break completely.
            Log.e(TAG, "EncryptedSharedPreferences failed, using fallback", e)
            context.getSharedPreferences(FALLBACK_PREFS, Context.MODE_PRIVATE)
        }
    }

    fun getToken(context: Context): String? {
        return try {
            getPrefs(context).getString(KEY_TOKEN, null)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to read token", e)
            null
        }
    }

    fun setToken(context: Context, token: String) {
        try {
            getPrefs(context).edit().putString(KEY_TOKEN, token).apply()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write token", e)
        }
    }

    fun getSymbols(context: Context): String? {
        return try {
            getPrefs(context).getString(KEY_SYMBOLS, null)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to read symbols", e)
            null
        }
    }

    fun setSymbols(context: Context, symbols: String) {
        try {
            getPrefs(context).edit().putString(KEY_SYMBOLS, symbols).apply()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write symbols", e)
        }
    }

    fun getCachedData(context: Context): String? {
        return try {
            getPrefs(context).getString(KEY_CACHED_DATA, null)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to read cached data", e)
            null
        }
    }

    fun setCachedData(context: Context, json: String) {
        try {
            getPrefs(context).edit().putString(KEY_CACHED_DATA, json).apply()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write cached data", e)
        }
    }

    fun getLastUpdateCheck(context: Context): Long {
        return try {
            getPrefs(context).getLong(KEY_LAST_UPDATE_CHECK, 0L)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to read last update check", e)
            0L
        }
    }

    fun setLastUpdateCheck(context: Context, timestamp: Long) {
        try {
            getPrefs(context).edit().putLong(KEY_LAST_UPDATE_CHECK, timestamp).apply()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write last update check", e)
        }
    }
}
