package cc.convexity.widget.widgets

import android.content.Context
import android.util.Log
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.*
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.layout.*
import androidx.glance.text.*
import androidx.glance.unit.ColorProvider
import cc.convexity.widget.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Locale

class AlertsWidget : GlanceAppWidget() {

    companion object {
        private val SMALL = DpSize(180.dp, 110.dp)
        private val MEDIUM = DpSize(250.dp, 180.dp)
        private val LARGE = DpSize(320.dp, 280.dp)
    }

    override val sizeMode = SizeMode.Responsive(setOf(SMALL, MEDIUM, LARGE))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val data = try {
            withContext(Dispatchers.IO) { ApiClient.fetchWidgetData(context) }
        } catch (e: Exception) {
            Log.e("AlertsWidget", "provideGlance failed", e)
            null
        }
        provideContent { AlertsContent(data) }
    }
}

@Composable
private fun AlertsContent(data: WidgetData?) {
    val bg = Color(0xFF0D1117)
    val cardBg = Color(0x1AFFFFFF)
    val borderColor = Color(0x33FFFFFF)
    val textColor = Color(0xFFE6EDF3)
    val dimColor = Color(0xFF8B949E)
    val red = Color(0xFFF85149)
    val blue = Color(0xFF58A6FF)

    Column(
        modifier = GlanceModifier.fillMaxSize().background(ColorProvider(bg)).padding(10.dp),
    ) {
        if (data == null) {
            WidgetHeader("Alerts")
            Spacer(GlanceModifier.height(6.dp))
            Text("No data", style = TextStyle(color = ColorProvider(red), fontSize = 11.sp, fontFamily = mono))
            Spacer(GlanceModifier.defaultWeight())
            WidgetFooter(0, AlertsWidget::class.java)
            return@Column
        }

        val alerts = data.alerts

        // Header
        Row(modifier = GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            WidgetHeader("Alerts")
            Spacer(GlanceModifier.defaultWeight())
            Text("${alerts.active} active", style = TextStyle(color = ColorProvider(textColor), fontSize = 10.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
        }

        Spacer(GlanceModifier.height(4.dp))

        if (alerts.list.isEmpty() && alerts.active == 0) {
            Column(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(borderColor)).cornerRadius(6.dp)) {
                Column(modifier = GlanceModifier.fillMaxWidth().padding(1.dp).background(ColorProvider(cardBg)).padding(horizontal = 6.dp, vertical = 8.dp)) {
                    Text("No active alerts", style = TextStyle(color = ColorProvider(dimColor), fontSize = 11.sp, fontFamily = mono))
                }
            }
            Spacer(GlanceModifier.defaultWeight())
            WidgetFooter(data.ts, AlertsWidget::class.java)
            return@Column
        }

        // Alerts table
        Column(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(borderColor)).cornerRadius(6.dp)) {
            Column(modifier = GlanceModifier.fillMaxWidth().padding(1.dp)) {
                // Table header
                Row(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(Color(0xFF161B22))).padding(horizontal = 6.dp, vertical = 3.dp)) {
                    Text("Alert", modifier = GlanceModifier.width(56.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    Spacer(GlanceModifier.defaultWeight())
                    Text("Target", style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    Spacer(GlanceModifier.width(8.dp))
                    Text("Dist", modifier = GlanceModifier.width(44.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                }

                for ((i, alert) in alerts.list.withIndex()) {
                    val rowBg = if (i % 2 == 0) cardBg else Color(0x00000000)
                    val condSymbol = when {
                        alert.condition.contains(">=") || alert.condition.contains("≥") -> "≥"
                        alert.condition.contains("<=") || alert.condition.contains("≤") -> "≤"
                        alert.condition.contains(">") -> ">"
                        alert.condition.contains("<") -> "<"
                        else -> alert.condition
                    }

                    Row(
                        modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(rowBg)).padding(horizontal = 6.dp, vertical = 3.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("${alert.symbol} $condSymbol", modifier = GlanceModifier.width(56.dp), style = TextStyle(color = ColorProvider(textColor), fontSize = 11.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                        Spacer(GlanceModifier.defaultWeight())
                        Text(fmtPrice(alert.price), style = TextStyle(color = ColorProvider(textColor), fontSize = 10.sp, fontFamily = mono))
                        Spacer(GlanceModifier.width(8.dp))
                        val pctText = if (alert.pct != null) "${String.format(Locale.US, "%.1f", alert.pct)}%" else "—"
                        Text(pctText, modifier = GlanceModifier.width(44.dp), style = TextStyle(color = ColorProvider(blue), fontSize = 10.sp, fontFamily = mono))
                    }
                }
            }
        }

        // Last triggered
        val last = alerts.lastTriggered
        if (last != null) {
            Spacer(GlanceModifier.height(4.dp))
            Text("Last: ${last.symbol} ${last.condition} ${fmtPrice(last.price)}",
                style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontFamily = mono))
        }

        Spacer(GlanceModifier.defaultWeight())
        WidgetFooter(data.ts, AlertsWidget::class.java)
    }
}

private fun fmtPrice(p: Double?): String {
    if (p == null) return "—"
    return when {
        p >= 1000 -> "$${String.format(Locale.US, "%,.2f", p)}"
        p >= 1 -> "$${String.format(Locale.US, "%.2f", p)}"
        else -> "$${String.format(Locale.US, "%.4f", p)}"
    }
}
