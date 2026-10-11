package cc.convexity.widget

import android.appwidget.AppWidgetManager
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.glance.appwidget.updateAll
import cc.convexity.widget.widgets.*
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class WidgetConfigActivity : AppCompatActivity() {

    private lateinit var tokenInput: EditText
    private lateinit var symbolsInput: EditText
    private lateinit var saveButton: Button
    private lateinit var pasteButton: Button
    private lateinit var statusText: TextView

    private var appWidgetId = AppWidgetManager.INVALID_APPWIDGET_ID

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setResult(RESULT_CANCELED)

        appWidgetId = intent?.extras?.getInt(
            AppWidgetManager.EXTRA_APPWIDGET_ID,
            AppWidgetManager.INVALID_APPWIDGET_ID
        ) ?: AppWidgetManager.INVALID_APPWIDGET_ID

        if (appWidgetId != AppWidgetManager.INVALID_APPWIDGET_ID) {
            val existingToken = SecurePrefs.getToken(this)
            if (existingToken != null && existingToken.startsWith("cvx_w_")) {
                confirmAndFinish()
                return
            }
        }

        setContentView(R.layout.activity_config)

        tokenInput = findViewById(R.id.token_input)
        symbolsInput = findViewById(R.id.symbols_input)
        saveButton = findViewById(R.id.save_button)
        pasteButton = findViewById(R.id.paste_button)
        statusText = findViewById(R.id.status_text)

        SecurePrefs.getToken(this)?.let { tokenInput.setText(it) }
        SecurePrefs.getSymbols(this)?.let { symbolsInput.setText(it) }

        handleDeepLink(intent)

        pasteButton.setOnClickListener {
            val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val clip = clipboard.primaryClip
            if (clip != null && clip.itemCount > 0) {
                val text = clip.getItemAt(0).text?.toString() ?: ""
                if (text.startsWith("cvx_w_")) {
                    tokenInput.setText(text)
                    statusText.text = "Token pasted"
                } else {
                    statusText.text = "Clipboard doesn't contain a valid token"
                }
            }
        }

        saveButton.setOnClickListener { saveAndFinish() }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    private fun handleDeepLink(intent: Intent?) {
        val uri = intent?.data ?: return
        if (uri.scheme == "convexity" && uri.host == "widget") {
            uri.getQueryParameter("token")?.let { t ->
                if (t.startsWith("cvx_w_")) {
                    tokenInput.setText(t)
                    statusText.text = "Token received from Carrac"
                }
            }
        }
    }

    private fun confirmAndFinish() {
        val resultValue = Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, appWidgetId)
        setResult(RESULT_OK, resultValue)

        CoroutineScope(Dispatchers.IO).launch {
            WatchlistWidget().updateAll(this@WidgetConfigActivity)
            PositionsWidget().updateAll(this@WidgetConfigActivity)
            SentimentWidget().updateAll(this@WidgetConfigActivity)
            AlertsWidget().updateAll(this@WidgetConfigActivity)
        }

        WidgetUpdateWorker.schedule(this)
        UpdateChecker.schedule(this)
        requestBatteryExemption()
        finish()
    }

    /**
     * OEM battery managers force-stop rarely-opened apps, which silences
     * both WorkManager and updatePeriodMillis — widgets then freeze on
     * cached data. Ask for the exemption once, right after a successful
     * setup, via the system dialog.
     */
    private fun requestBatteryExemption() {
        val pm = getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        try {
            startActivity(
                Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = android.net.Uri.parse("package:$packageName")
                }
            )
        } catch (e: Exception) {
            // Some OEMs block the direct dialog — widgets still work, just
            // subject to battery deferral. Nothing actionable here.
        }
    }

    private fun saveAndFinish() {
        val token = tokenInput.text.toString().trim()
        val symbols = symbolsInput.text.toString().trim()

        if (token.isEmpty() || !token.startsWith("cvx_w_")) {
            statusText.text = "Invalid token. Must start with cvx_w_"
            return
        }

        saveButton.isEnabled = false
        statusText.text = "Verifying token..."

        // Save token first so fetchWidgetData can use it
        SecurePrefs.setToken(this@WidgetConfigActivity, token)
        if (symbols.isNotBlank()) {
            SecurePrefs.setSymbols(this@WidgetConfigActivity, symbols)
        }

        CoroutineScope(Dispatchers.IO).launch {
            val data = ApiClient.fetchWidgetData(this@WidgetConfigActivity, symbols.ifBlank { null })

            runOnUiThread {
                if (data != null) {
                    confirmAndFinish()
                } else {
                    // Verification failed — clear token
                    SecurePrefs.setToken(this@WidgetConfigActivity, "")
                    statusText.text = "Could not verify token. Check your connection and token."
                    saveButton.isEnabled = true
                }
            }
        }
    }
}
