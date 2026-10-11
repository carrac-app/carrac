package cc.convexity.widget

import org.json.JSONObject

data class WidgetData(
    val ts: Long,
    val prices: List<PriceEntry>,
    val signals: List<SignalEntry>,
    val sentiment: SentimentData,
    val alerts: AlertData,
    val positions: PositionData,
    val report: ReportData,
) {
    companion object {
        fun fromJson(json: JSONObject): WidgetData {
            val pricesArr = json.optJSONArray("prices")
            val signalsArr = json.optJSONArray("signals")
            return WidgetData(
                ts = json.optLong("ts", 0),
                prices = (0 until (pricesArr?.length() ?: 0)).map { PriceEntry.fromJson(pricesArr!!.getJSONObject(it)) },
                signals = (0 until (signalsArr?.length() ?: 0)).map { SignalEntry.fromJson(signalsArr!!.getJSONObject(it)) },
                sentiment = SentimentData.fromJson(json.optJSONObject("sentiment") ?: JSONObject()),
                alerts = AlertData.fromJson(json.optJSONObject("alerts") ?: JSONObject()),
                positions = PositionData.fromJson(json.optJSONObject("positions") ?: JSONObject()),
                report = ReportData.fromJson(json.optJSONObject("report") ?: JSONObject()),
            )
        }
    }
}

private fun JSONObject.safeDouble(key: String): Double? {
    if (!has(key) || isNull(key)) return null
    val v = optDouble(key)
    return if (v.isNaN() || v.isInfinite()) null else v
}

data class PriceEntry(val symbol: String, val price: Double?, val change: Double?) {
    companion object {
        fun fromJson(j: JSONObject) = PriceEntry(
            symbol = j.optString("s", ""),
            price = j.safeDouble("p"),
            change = j.safeDouble("c"),
        )
    }
}

data class SignalEntry(val symbol: String, val consensio: List<Int>, val rsi: Int?) {
    companion object {
        fun fromJson(j: JSONObject): SignalEntry {
            val conArr = j.optJSONArray("con")
            return SignalEntry(
                symbol = j.optString("s", ""),
                consensio = (0 until (conArr?.length() ?: 0)).map { conArr!!.getInt(it) },
                rsi = if (j.has("rsi") && !j.isNull("rsi")) j.optInt("rsi") else null,
            )
        }
    }
}

data class SentimentData(
    val fg: Int?, val fgLabel: String?,
    val regime: String?, val regimeScore: Int?,
    val btcFunding: Double?, val lsRatio: Double?,
    val oiUsd: Double?, val oiChange: Double?,
    val mvrv: Double?, val mvrvZone: String?,
    val altSeason: Int?,
) {
    companion object {
        fun fromJson(j: JSONObject) = SentimentData(
            fg = if (j.has("fg")) j.optInt("fg") else null,
            fgLabel = j.optString("fg_label", null),
            regime = if (j.has("regime") && !j.isNull("regime")) j.optString("regime") else null,
            regimeScore = if (j.has("regime_score")) j.optInt("regime_score") else null,
            btcFunding = j.safeDouble("btc_funding"),
            lsRatio = j.safeDouble("ls_ratio"),
            oiUsd = j.safeDouble("oi_usd"),
            oiChange = j.safeDouble("oi_change"),
            mvrv = j.safeDouble("mvrv"),
            mvrvZone = if (j.has("mvrv_zone") && !j.isNull("mvrv_zone")) j.optString("mvrv_zone") else null,
            altSeason = if (j.has("alt_season") && !j.isNull("alt_season")) j.optInt("alt_season") else null,
        )
    }
}

data class AlertData(val active: Int, val total: Int, val nearest: NearestAlert?, val lastTriggered: TriggeredAlert?, val list: List<AlertItem>) {
    companion object {
        fun fromJson(j: JSONObject): AlertData {
            val listArr = j.optJSONArray("list")
            return AlertData(
                active = j.optInt("active", 0), total = j.optInt("total", 0),
                nearest = j.optJSONObject("nearest")?.let { NearestAlert.fromJson(it) },
                lastTriggered = j.optJSONObject("last_triggered")?.let { TriggeredAlert.fromJson(it) },
                list = (0 until (listArr?.length() ?: 0)).map { AlertItem.fromJson(listArr!!.getJSONObject(it)) },
            )
        }
    }
}

data class AlertItem(val symbol: String, val condition: String, val price: Double?, val pct: Double?) {
    companion object {
        fun fromJson(j: JSONObject) = AlertItem(j.optString("s", ""), j.optString("cond", ""), j.safeDouble("p"), j.safeDouble("pct"))
    }
}

data class NearestAlert(val symbol: String, val condition: String, val price: Double, val pct: Double) {
    companion object {
        fun fromJson(j: JSONObject) = NearestAlert(j.optString("s", ""), j.optString("cond", ""), j.optDouble("p", 0.0), j.optDouble("pct", 0.0))
    }
}

data class TriggeredAlert(val symbol: String, val condition: String, val price: Double, val at: String) {
    companion object {
        fun fromJson(j: JSONObject) = TriggeredAlert(j.optString("s", ""), j.optString("cond", ""), j.optDouble("p", 0.0), j.optString("at", ""))
    }
}

data class PositionData(val open: List<OpenPosition>, val totalRR: Double, val count: Int, val stats7d: TradeStats) {
    companion object {
        fun fromJson(j: JSONObject): PositionData {
            val openArr = j.optJSONArray("open")
            return PositionData(
                open = (0 until (openArr?.length() ?: 0)).map { OpenPosition.fromJson(openArr!!.getJSONObject(it)) },
                totalRR = j.optDouble("total_rr", 0.0), count = j.optInt("count", 0),
                stats7d = TradeStats.fromJson(j.optJSONObject("stats_7d") ?: JSONObject()),
            )
        }
    }
}

data class OpenPosition(
    val id: Int, val symbol: String, val direction: String,
    val entry: Double, val tp: Double?, val sl: Double?, val now: Double?,
    val rrPlan: Double?, val rrNow: Double?,
    val pct: Double?, val pctToTp: Double?, val pctToSl: Double?,
    val setup: String?, val opened: String?,
) {
    companion object {
        fun fromJson(j: JSONObject) = OpenPosition(
            id = j.optInt("id", 0), symbol = j.optString("s", ""), direction = j.optString("dir", "long"),
            entry = j.optDouble("entry", 0.0),
            tp = j.safeDouble("tp"), sl = j.safeDouble("sl"), now = j.safeDouble("now"),
            rrPlan = j.safeDouble("rr_plan"), rrNow = j.safeDouble("rr_now"),
            pct = j.safeDouble("pct"), pctToTp = j.safeDouble("pct_to_tp"), pctToSl = j.safeDouble("pct_to_sl"),
            setup = j.optString("setup", null), opened = j.optString("opened", null),
        )
    }
}

data class TradeStats(val trades: Int, val wins: Int, val winRate: Double, val avgRR: Double, val streak: String) {
    companion object {
        fun fromJson(j: JSONObject) = TradeStats(j.optInt("trades", 0), j.optInt("wins", 0), j.optDouble("win_rate", 0.0), j.optDouble("avg_rr", 0.0), j.optString("streak", "0"))
    }
}

data class ReportData(val date: String?, val snippet: String?) {
    companion object {
        fun fromJson(j: JSONObject) = ReportData(j.optString("date", null), j.optString("snippet", null))
    }
}
