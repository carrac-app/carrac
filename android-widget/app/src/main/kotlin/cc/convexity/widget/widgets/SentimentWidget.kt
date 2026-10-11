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

class SentimentWidget : GlanceAppWidget() {

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
            Log.e("SentimentWidget", "provideGlance failed", e)
            null
        }
        provideContent { SentimentContent(data, LocalSize.current) }
    }
}

@Composable
private fun SentimentContent(data: WidgetData?, size: DpSize) {
    val bg = Color(0xFF0D1117)
    val cardBg = Color(0x1AFFFFFF)
    val borderColor = Color(0x33FFFFFF)
    val textColor = Color(0xFFE6EDF3)
    val dimColor = Color(0xFF8B949E)
    val green = Color(0xFF3FB950)
    val red = Color(0xFFF85149)
    val yellow = Color(0xFFD29922)

    val isWide = size.width >= 250.dp
    val isTall = size.height >= 180.dp

    Column(
        modifier = GlanceModifier.fillMaxSize().background(ColorProvider(bg)).padding(10.dp),
    ) {
        if (data == null) {
            WidgetHeader("Market Pulse")
            Spacer(GlanceModifier.height(6.dp))
            Text("No data", style = TextStyle(color = ColorProvider(red), fontSize = 11.sp, fontFamily = mono))
            Spacer(GlanceModifier.defaultWeight())
            WidgetFooter(0, SentimentWidget::class.java)
            return@Column
        }

        val s = data.sentiment

        // Header with regime badge
        Row(modifier = GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            WidgetHeader("Market Pulse")
            Spacer(GlanceModifier.defaultWeight())
            if (s.regime != null) {
                val regimeColor = when (s.regime) {
                    "Risk-On" -> green
                    "Cautious" -> yellow
                    "Risk-Off" -> red
                    "Crisis" -> Color(0xFFFF6B6B)
                    else -> dimColor
                }
                Text(s.regime.uppercase(), style = TextStyle(color = ColorProvider(regimeColor), fontSize = 9.sp, fontWeight = FontWeight.Bold, fontFamily = mono))
            }
        }

        Spacer(GlanceModifier.height(4.dp))

        // Table
        Column(modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(borderColor)).cornerRadius(6.dp)) {
            Column(modifier = GlanceModifier.fillMaxWidth().padding(1.dp)) {
                val fg = s.fg
                val fgColor = if (fg != null && fg >= 60) green else if (fg != null && fg <= 40) red else yellow
                val fgLabel = if (isWide) "Fear & Greed" else "F&G"
                val fgValue = if (fg != null) { if (isWide) "$fg  ${s.fgLabel ?: ""}" else "$fg" } else "—"
                SentRow(fgLabel, fgValue, fgColor, textColor, cardBg)

                val fundLabel = if (isWide) "BTC Funding" else "Fund"
                val fundText = if (s.btcFunding != null) "${if (s.btcFunding >= 0) "+" else ""}${String.format(Locale.US, "%.3f", s.btcFunding)}%" else "—"
                val fundColor = if (s.btcFunding != null && s.btcFunding > 0.03) red else if (s.btcFunding != null && s.btcFunding < -0.03) green else dimColor
                SentRow(fundLabel, fundText, fundColor, textColor, Color(0x00000000))

                val lsLabel = if (isWide) "Long/Short" else "L/S"
                val lsText = if (s.lsRatio != null) String.format(Locale.US, "%.2f", s.lsRatio) else "—"
                val lsColor = if (s.lsRatio != null && s.lsRatio > 1.1) red else if (s.lsRatio != null && s.lsRatio < 0.9) green else dimColor
                SentRow(lsLabel, lsText, lsColor, textColor, cardBg)

                if (isTall) {
                    val oiLabel = if (isWide) "BTC OI" else "OI"
                    val oiText = if (s.oiUsd != null) {
                        val b = s.oiUsd / 1_000_000_000.0
                        val chg = if (s.oiChange != null) " ${if (s.oiChange >= 0) "+" else ""}${String.format(Locale.US, "%.1f", s.oiChange)}%" else ""
                        "${String.format(Locale.US, "%.1f", b)}B$chg"
                    } else "—"
                    val oiColor = if (s.oiChange != null && s.oiChange > 0) green else if (s.oiChange != null && s.oiChange < 0) red else dimColor
                    SentRow(oiLabel, oiText, oiColor, textColor, Color(0x00000000))
                }

                if (isTall) {
                    val mvrvLabel = if (isWide) "MVRV Z" else "MVRV"
                    val mvrvText = if (s.mvrv != null) String.format(Locale.US, "%.2f", s.mvrv) else "—"
                    val mvrvColor = when (s.mvrvZone) { "extreme" -> red; "overvalued" -> yellow; "undervalued" -> green; else -> dimColor }
                    SentRow(mvrvLabel, mvrvText, mvrvColor, textColor, cardBg)
                }

                val altLabel = if (isWide) "Alt Season" else "Alts"
                val altText = "${s.altSeason ?: "—"}"
                val altColor = if (s.altSeason != null && s.altSeason > 75) green else if (s.altSeason != null && s.altSeason < 25) red else dimColor
                SentRow(altLabel, altText, altColor, textColor, if (isTall) Color(0x00000000) else cardBg)
            }
        }

        Spacer(GlanceModifier.defaultWeight())
        WidgetFooter(data.ts, SentimentWidget::class.java)
    }
}

@Composable
private fun SentRow(label: String, value: String, valueColor: Color, textColor: Color, rowBg: Color) {
    Row(
        modifier = GlanceModifier.fillMaxWidth().background(ColorProvider(rowBg)).padding(horizontal = 6.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, style = TextStyle(color = ColorProvider(textColor), fontSize = 11.sp, fontFamily = mono))
        Spacer(GlanceModifier.defaultWeight())
        Text(value, style = TextStyle(color = ColorProvider(valueColor), fontSize = 11.sp, fontFamily = mono))
    }
}
