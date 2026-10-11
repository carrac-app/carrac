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

class WatchlistWidget : GlanceAppWidget() {

    companion object {
        private const val TAG = "WatchlistWidget"
        private val SMALL = DpSize(180.dp, 110.dp)
        private val MEDIUM = DpSize(250.dp, 180.dp)
        private val LARGE = DpSize(320.dp, 280.dp)
    }

    override val sizeMode = SizeMode.Responsive(setOf(SMALL, MEDIUM, LARGE))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val data = try {
            withContext(Dispatchers.IO) {
                val symbols = SecurePrefs.getSymbols(context)
                ApiClient.fetchWidgetData(context, symbols)
            }
        } catch (e: Exception) {
            Log.e(TAG, "provideGlance failed", e)
            null
        }
        provideContent { WatchlistContent(data) }
    }
}

@Composable
private fun WatchlistContent(data: WidgetData?) {
    val bg = Color(0xFF0D1117)
    val cardBg = Color(0x1AFFFFFF)
    val borderColor = Color(0x33FFFFFF)
    val textColor = Color(0xFFE6EDF3)
    val dimColor = Color(0xFF8B949E)
    val green = Color(0xFF3FB950)
    val red = Color(0xFFF85149)

    Column(
        modifier = GlanceModifier.fillMaxSize().background(ColorProvider(bg)).padding(10.dp),
    ) {
        if (data == null) {
            WidgetHeader("Watchlist")
            Spacer(GlanceModifier.height(6.dp))
            Text("No data — check token", style = TextStyle(color = ColorProvider(red), fontSize = 11.sp, fontFamily = mono))
            Spacer(GlanceModifier.defaultWeight())
            WidgetFooter(0, WatchlistWidget::class.java)
            return@Column
        }

        WidgetHeader("Watchlist")
        Spacer(GlanceModifier.height(4.dp))

        // Table
        Column(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(borderColor)).cornerRadius(6.dp)) {
            Column(modifier = GlanceModifier.fillMaxWidth().padding(1.dp)) {
                // Table header
                Row(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(Color(0xFF161B22))).padding(horizontal = 6.dp, vertical = 3.dp)) {
                    Text("Asset", modifier = GlanceModifier.width(48.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    Spacer(GlanceModifier.defaultWeight())
                    Text("Price", style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    Spacer(GlanceModifier.width(10.dp))
                    Text("24h", modifier = GlanceModifier.width(52.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                }

                for ((i, price) in data.prices.withIndex()) {
                    val chgColor = if ((price.change ?: 0.0) >= 0) green else red
                    val rowBg = if (i % 2 == 0) cardBg else Color(0x00000000)

                    Row(
                        modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(rowBg)).padding(horizontal = 6.dp, vertical = 3.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(price.symbol, modifier = GlanceModifier.width(48.dp), style = TextStyle(color = ColorProvider(textColor), fontSize = 11.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                        Spacer(GlanceModifier.defaultWeight())
                        Text(fmtPrice(price.price), style = TextStyle(color = ColorProvider(textColor), fontSize = 11.sp, fontFamily = mono))
                        Spacer(GlanceModifier.width(10.dp))
                        Text(fmtChange(price.change), modifier = GlanceModifier.width(52.dp), style = TextStyle(color = ColorProvider(chgColor), fontSize = 10.sp, fontFamily = mono))
                    }
                }
            }
        }

        Spacer(GlanceModifier.defaultWeight())
        WidgetFooter(data.ts, WatchlistWidget::class.java)
    }
}

private fun fmtPrice(p: Double?): String {
    if (p == null) return "—"
    return when {
        p >= 1000 -> "$${String.format(Locale.US, "%,.2f", p)}"
        p >= 1 -> "$${String.format(Locale.US, "%.2f", p)}"
        p >= 0.01 -> "$${String.format(Locale.US, "%.4f", p)}"
        else -> "$${String.format(Locale.US, "%.6f", p)}"
    }
}

private fun fmtChange(c: Double?): String {
    if (c == null) return "—"
    val sign = if (c >= 0) "+" else ""
    return "$sign${String.format(Locale.US, "%.1f", c)}%"
}
