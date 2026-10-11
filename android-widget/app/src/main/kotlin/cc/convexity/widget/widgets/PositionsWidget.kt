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

class PositionsWidget : GlanceAppWidget() {

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
            Log.e("PositionsWidget", "provideGlance failed", e)
            null
        }
        provideContent { PositionsContent(data, LocalSize.current) }
    }
}

@Composable
private fun PositionsContent(data: WidgetData?, size: DpSize) {
    val bg = Color(0xFF0D1117)
    val cardBg = Color(0x1AFFFFFF)
    val borderColor = Color(0x33FFFFFF)
    val dimColor = Color(0xFF8B949E)
    val green = Color(0xFF3FB950)
    val red = Color(0xFFF85149)

    val isWide = size.width >= 250.dp

    Column(
        modifier = GlanceModifier.fillMaxSize().background(ColorProvider(bg)).padding(10.dp),
    ) {
        if (data == null) {
            WidgetHeader("Positions")
            Spacer(GlanceModifier.height(6.dp))
            Text("No data", style = TextStyle(color = ColorProvider(red), fontSize = 11.sp, fontFamily = mono))
            Spacer(GlanceModifier.defaultWeight())
            WidgetFooter(0, PositionsWidget::class.java)
            return@Column
        }

        val positions = data.positions

        // Header
        Row(modifier = GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            WidgetHeader("Positions")
            Spacer(GlanceModifier.defaultWeight())
            Text("${positions.count} open", style = TextStyle(color = ColorProvider(dimColor), fontSize = 10.sp, fontFamily = mono))
        }

        Spacer(GlanceModifier.height(4.dp))

        if (positions.open.isEmpty()) {
            Column(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(borderColor)).cornerRadius(6.dp)) {
                Column(modifier = GlanceModifier.fillMaxWidth().padding(1.dp).background(ColorProvider(cardBg)).padding(horizontal = 6.dp, vertical = 8.dp)) {
                    Text("No open positions", style = TextStyle(color = ColorProvider(dimColor), fontSize = 11.sp, fontFamily = mono))
                    val stats = positions.stats7d
                    if (stats.trades > 0) {
                        Spacer(GlanceModifier.height(4.dp))
                        Text("7d: ${stats.trades} trades, ${String.format(Locale.US, "%.0f", stats.winRate)}% WR",
                            style = TextStyle(color = ColorProvider(dimColor), fontSize = 10.sp, fontFamily = mono))
                    }
                }
            }
            Spacer(GlanceModifier.defaultWeight())
            WidgetFooter(data.ts, PositionsWidget::class.java)
            return@Column
        }

        // Table
        Column(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(borderColor)).cornerRadius(6.dp)) {
            Column(modifier = GlanceModifier.fillMaxWidth().padding(1.dp)) {
                // Table header
                Row(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(Color(0xFF161B22))).padding(horizontal = 6.dp, vertical = 3.dp)) {
                    Text("Trade", modifier = GlanceModifier.width(56.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    Spacer(GlanceModifier.defaultWeight())
                    if (isWide) {
                        Text("TP/SL", modifier = GlanceModifier.width(80.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    }
                    Text("%", modifier = GlanceModifier.width(48.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    Text("R:R", modifier = GlanceModifier.width(60.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                }

                for ((i, pos) in positions.open.withIndex()) {
                    val dirColor = if (pos.direction == "long") green else red
                    val rrColor = if ((pos.rrNow ?: 0.0) >= 0) green else red
                    val pctColor = if ((pos.pct ?: 0.0) >= 0) green else red
                    val arrow = if (pos.direction == "long") "▲" else "▼"
                    val rowBg = if (i % 2 == 0) cardBg else Color(0x00000000)

                    Row(
                        modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(rowBg)).padding(horizontal = 6.dp, vertical = 3.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("$arrow ${pos.symbol}", modifier = GlanceModifier.width(56.dp), style = TextStyle(color = ColorProvider(dirColor), fontSize = 11.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                        Spacer(GlanceModifier.defaultWeight())
                        if (isWide) {
                            val tpText = if (pos.tp != null) fmtPrice(pos.tp) else "—"
                            val slText = if (pos.sl != null) fmtPrice(pos.sl) else "—"
                            Text("$tpText/$slText", modifier = GlanceModifier.width(80.dp), style = TextStyle(color = ColorProvider(dimColor), fontSize = 9.sp, fontFamily = mono))
                        }
                        Text(fmtPct(pos.pct), modifier = GlanceModifier.width(48.dp), style = TextStyle(color = ColorProvider(pctColor), fontSize = 10.sp, fontFamily = mono))
                        val rrText = if (pos.rrNow != null && pos.rrPlan != null) {
                            "${fmtR(pos.rrNow)}/${String.format(Locale.US, "%.1f", pos.rrPlan)}"
                        } else { fmtR(pos.rrNow) }
                        Text(rrText, modifier = GlanceModifier.width(60.dp), style = TextStyle(color = ColorProvider(rrColor), fontSize = 10.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
                    }
                }
            }
        }

        // Total R
        if (positions.count > 0) {
            Spacer(GlanceModifier.height(4.dp))
            val totalColor = if (positions.totalRR >= 0) green else red
            Text("Total: ${fmtR(positions.totalRR)}", style = TextStyle(color = ColorProvider(totalColor), fontSize = 10.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
        }

        Spacer(GlanceModifier.defaultWeight())
        WidgetFooter(data.ts, PositionsWidget::class.java)
    }
}

private fun fmtR(r: Double?): String {
    if (r == null) return "—"
    val sign = if (r >= 0) "+" else ""
    return "$sign${String.format(Locale.US, "%.2f", r)}R"
}

private fun fmtPct(p: Double?): String {
    if (p == null) return "—"
    val sign = if (p >= 0) "+" else ""
    return "$sign${String.format(Locale.US, "%.1f", p)}%"
}

private fun fmtPrice(p: Double?): String {
    if (p == null) return "—"
    return when {
        p >= 1000 -> String.format(Locale.US, "%,.0f", p)
        p >= 1 -> String.format(Locale.US, "%.2f", p)
        else -> String.format(Locale.US, "%.4f", p)
    }
}
