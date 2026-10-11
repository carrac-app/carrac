package cc.convexity.widget.widgets

import android.content.Context
import android.util.Log
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionParametersOf
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.layout.*
import androidx.glance.text.*
import androidx.glance.unit.ColorProvider
import cc.convexity.widget.ApiClient
import cc.convexity.widget.SecurePrefs
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

val mono = FontFamily.Monospace
val matrixGreen = Color(0xFF00FF41)

/**
 * Render the branded header: Carrac.cc: <title>
 */
@Composable
fun WidgetHeader(title: String) {
    val dimColor = Color(0xFF8B949E)
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text("Carrac.cc", style = TextStyle(color = ColorProvider(matrixGreen), fontSize = 10.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
        Text(": $title", style = TextStyle(color = ColorProvider(dimColor), fontSize = 10.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
    }
}

/**
 * Render the footer with timestamp + refresh button.
 * @param ts epoch seconds from the API response
 * @param widgetClass the GlanceAppWidget class to refresh
 */
@Composable
fun <T : GlanceAppWidget> WidgetFooter(ts: Long, widgetClass: Class<T>) {
    val dimColor = Color(0xFF8B949E)
    val timeText = if (ts > 0) {
        val sdf = SimpleDateFormat("HH:mm", Locale.US)
        sdf.format(Date(ts * 1000))
    } else "—"

    Row(
        modifier = GlanceModifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalAlignment = Alignment.End,
    ) {
        Text(timeText, style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontFamily = mono))
        Spacer(GlanceModifier.width(6.dp))
        Text(
            "↻",
            modifier = GlanceModifier.clickable(
                actionRunCallback<RefreshAction>(
                    actionParametersOf(RefreshAction.widgetClassKey to widgetClass.name)
                )
            ),
            style = TextStyle(color = ColorProvider(matrixGreen), fontSize = 12.sp, fontWeight = FontWeight.Bold),
        )
    }
}

class RefreshAction : ActionCallback {
    companion object {
        private const val TAG = "RefreshAction"
        val widgetClassKey = ActionParameters.Key<String>("widgetClass")
    }

    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        val className = parameters[widgetClassKey]
        if (className == null) {
            Log.e(TAG, "No widget class name in parameters")
            return
        }
        Log.d(TAG, "Refresh triggered for $className")

        try {
            // Fetch fresh data first
            val symbols = SecurePrefs.getSymbols(context)
            val data = ApiClient.fetchWidgetData(context, symbols)
            Log.d(TAG, "Data fetched: ${data != null}, prices: ${data?.prices?.size ?: 0}")

            // Then update the widget
            @Suppress("UNCHECKED_CAST")
            val clazz = Class.forName(className) as Class<out GlanceAppWidget>
            val widget = clazz.getDeclaredConstructor().newInstance()
            widget.update(context, glanceId)
            Log.d(TAG, "Widget updated: $className")
        } catch (e: Exception) {
            Log.e(TAG, "Refresh failed for $className", e)
        }
    }
}
