// Carrac Widget for Scriptable
// Open /widget/setup on your Carrac instance for setup instructions.
//
// Widget parameter selects type: watchlist | positions | sentiment | alerts
// Auto-updates from server daily.

const VERSION = "2.1.0"
// Placeholder: the server rewrites this to its own origin when it serves the script.
const API_BASE = "https://carrac.example.invalid"
const SCRIPT_URL = API_BASE + "/widget/scriptable.js"
const TOKEN_KEY = "carrac_widget_token"
const CACHE_KEY = "carrac_widget_cache"
const UPDATE_KEY = "carrac_last_update_check"

// Keychain migration: convexity_widget_token → carrac_widget_token (one-time)
try {
    if (Keychain.contains("convexity_widget_token")) {
        var _legacyToken = Keychain.get("convexity_widget_token");
        if (_legacyToken && !Keychain.contains("carrac_widget_token")) {
            Keychain.set("carrac_widget_token", _legacyToken);
        }
        Keychain.remove("convexity_widget_token");
    }
} catch(e) {}

// Token pre-loaded via .scriptable download (replaced by server)
const PRELOADED_TOKEN = null

// ─── Colors ───
const C = {
    bg:       Color.dynamic(new Color("#ffffff"), new Color("#0d1117")),
    bgCard:   Color.dynamic(new Color("#f6f8fa"), new Color("#161b22")),
    text:     Color.dynamic(new Color("#1f2328"), new Color("#e6edf3")),
    textDim:  Color.dynamic(new Color("#656d76"), new Color("#8b949e")),
    textMute: Color.dynamic(new Color("#8b949e"), new Color("#484f58")),
    green:    new Color("#3fb950"),
    red:      new Color("#f85149"),
    yellow:   new Color("#d29922"),
    blue:     new Color("#58a6ff"),
    matrix:   new Color("#00ff41"),
}

// ─── Font Helpers ───
const F = {
    mono: (sz) => Font.monospacedSystemFont(sz, 0.5),
    monoBold: (sz) => Font.boldMonospacedSystemFont(sz),
}

// ─── Token Management ───
async function getToken() {
    if (Keychain.contains(TOKEN_KEY)) return Keychain.get(TOKEN_KEY)
    if (PRELOADED_TOKEN) {
        Keychain.set(TOKEN_KEY, PRELOADED_TOKEN)
        return PRELOADED_TOKEN
    }
    let a = new Alert()
    a.title = "Carrac Widget Setup"
    a.message = "Enter your widget token from Settings → Widgets"
    a.addSecureTextField("Token", "")
    a.addAction("Save")
    a.addCancelAction("Cancel")
    if (await a.present() === -1) return null
    let token = a.textFieldValue(0).trim()
    if (token && token.startsWith("cvx_w_")) {
        Keychain.set(TOKEN_KEY, token)
        return token
    }
    return null
}

// ─── Data Fetch ───
async function fetchData(token) {
    let url = API_BASE + "/api/widget/summary?token=" + encodeURIComponent(token)
    let req = new Request(url)
    req.timeoutInterval = 10
    try {
        let data = await req.loadJSON()
        if (data && data.ts) {
            let fm = FileManager.local()
            let cachePath = fm.joinPath(fm.temporaryDirectory(), CACHE_KEY + ".json")
            fm.writeString(cachePath, JSON.stringify(data))
        }
        return data
    } catch (e) {
        try {
            let fm = FileManager.local()
            let cachePath = fm.joinPath(fm.temporaryDirectory(), CACHE_KEY + ".json")
            if (fm.fileExists(cachePath)) {
                return JSON.parse(fm.readString(cachePath))
            }
        } catch (_) {}
        return null
    }
}

// ─── Widget Type from Parameter ───
function getWidgetType() {
    let param = (args.widgetParameter || "watchlist").toLowerCase().trim()
    if (["positions", "sentiment", "alerts"].includes(param)) return param
    return "watchlist"
}

// ─── Format Helpers (force US locale) ───
function fmtPrice(p) {
    if (p == null) return "—"
    if (p >= 1000) {
        let parts = p.toFixed(2).split(".")
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",")
        return "$" + parts.join(".")
    }
    if (p >= 1) return "$" + p.toFixed(2)
    if (p >= 0.01) return "$" + p.toFixed(4)
    return "$" + p.toFixed(6)
}
function fmtChange(c) {
    if (c == null) return "—"
    let s = c >= 0 ? "+" : ""
    return s + c.toFixed(1) + "%"
}
function fmtR(r) {
    if (r == null) return "—"
    let s = r >= 0 ? "+" : ""
    return s + r.toFixed(2) + "R"
}
function fmtPct(p) {
    if (p == null) return "—"
    let s = p >= 0 ? "+" : ""
    return s + p.toFixed(1) + "%"
}
function changeColor(c) { return c >= 0 ? C.green : C.red }
function fmtTime(ts) {
    if (!ts || ts <= 0) return "—"
    let d = new Date(ts * 1000)
    let h = String(d.getHours()).padStart(2, "0")
    let m = String(d.getMinutes()).padStart(2, "0")
    return h + ":" + m
}

// ─── Header: [Carrac.cc]: Title ───
function addHeader(w, title) {
    let row = w.addStack()
    row.centerAlignContent()
    let brand = row.addText("[Carrac.cc]")
    brand.font = F.monoBold(10)
    brand.textColor = C.matrix
    let sep = row.addText(": " + title)
    sep.font = F.monoBold(10)
    sep.textColor = C.textDim
    return row
}

// ─── Footer: timestamp ───
function addFooter(w, ts) {
    let row = w.addStack()
    row.addSpacer()
    let t = row.addText(fmtTime(ts))
    t.font = F.mono(9)
    t.textColor = C.textMute
}

// ─── Watchlist Widget ───
function buildWatchlistSmall(w, data) {
    let p = (data.prices || [])[0]
    w.backgroundColor = C.bg
    w.setPadding(10, 12, 10, 12)

    if (!p) { addError(w, "No data"); return }

    addHeader(w, "Watchlist")
    w.addSpacer(4)

    let sym = w.addText(p.s)
    sym.font = F.monoBold(14)
    sym.textColor = C.text

    let price = w.addText(fmtPrice(p.p))
    price.font = F.monoBold(20)
    price.textColor = C.text

    w.addSpacer(4)

    let chg = w.addText(fmtChange(p.c))
    chg.font = F.mono(12)
    chg.textColor = changeColor(p.c || 0)

    w.addSpacer()
    addFooter(w, data.ts)
}

function buildWatchlistMedium(w, data) {
    let prices = data.prices || []

    w.backgroundColor = C.bg
    w.setPadding(10, 14, 10, 14)

    addHeader(w, "Watchlist")
    w.addSpacer(4)

    // Column header
    let hdr = w.addStack()
    hdr.centerAlignContent()
    let h1 = hdr.addText("Asset")
    h1.font = F.monoBold(9)
    h1.textColor = C.textDim
    h1.size = new Size(48, 0)
    hdr.addSpacer()
    let h2 = hdr.addText("Price")
    h2.font = F.monoBold(9)
    h2.textColor = C.textDim
    hdr.addSpacer(10)
    let h3 = hdr.addText("24h")
    h3.font = F.monoBold(9)
    h3.textColor = C.textDim
    h3.size = new Size(52, 0)
    w.addSpacer(2)

    // All assets
    for (let i = 0; i < prices.length; i++) {
        let p = prices[i]
        let row = w.addStack()
        row.centerAlignContent()

        let sym = row.addText(p.s)
        sym.font = F.monoBold(11)
        sym.textColor = C.text
        sym.lineLimit = 1
        sym.size = new Size(48, 0)

        row.addSpacer()
        let pr = row.addText(fmtPrice(p.p))
        pr.font = F.mono(11)
        pr.textColor = C.text
        pr.lineLimit = 1

        row.addSpacer(10)
        let chg = row.addText(fmtChange(p.c))
        chg.font = F.mono(10)
        chg.textColor = changeColor(p.c || 0)
        chg.lineLimit = 1
        chg.size = new Size(52, 0)

        w.addSpacer(1)
    }

    w.addSpacer()
    addFooter(w, data.ts)
}

function buildWatchlistLarge(w, data) {
    let prices = data.prices || []

    w.backgroundColor = C.bg
    w.setPadding(12, 14, 12, 14)

    addHeader(w, "Watchlist")
    w.addSpacer(4)

    // Column header
    let hdr = w.addStack()
    hdr.centerAlignContent()
    let h1 = hdr.addText("Asset")
    h1.font = F.monoBold(9)
    h1.textColor = C.textDim
    h1.size = new Size(48, 0)
    hdr.addSpacer()
    let h2 = hdr.addText("Price")
    h2.font = F.monoBold(9)
    h2.textColor = C.textDim
    hdr.addSpacer(10)
    let h3 = hdr.addText("24h")
    h3.font = F.monoBold(9)
    h3.textColor = C.textDim
    h3.size = new Size(52, 0)
    w.addSpacer(2)

    for (let i = 0; i < prices.length; i++) {
        let p = prices[i]
        let row = w.addStack()
        row.centerAlignContent()

        let sym = row.addText(p.s)
        sym.font = F.monoBold(12)
        sym.textColor = C.text
        sym.lineLimit = 1
        sym.size = new Size(48, 0)

        row.addSpacer()
        let pr = row.addText(fmtPrice(p.p))
        pr.font = F.mono(12)
        pr.textColor = C.text
        pr.lineLimit = 1

        row.addSpacer(10)
        let chg = row.addText(fmtChange(p.c))
        chg.font = F.mono(11)
        chg.textColor = changeColor(p.c || 0)
        chg.size = new Size(52, 0)

        w.addSpacer(2)
    }

    w.addSpacer()
    addFooter(w, data.ts)
}

// ─── Positions Widget ───
function buildPositionsSmall(w, data) {
    let pos = data.positions || {}
    let open = pos.open || []

    w.backgroundColor = C.bg
    w.setPadding(10, 12, 10, 12)

    addHeader(w, "Positions")
    w.addSpacer(4)

    if (open.length === 0) {
        let t = w.addText("No open positions")
        t.font = F.mono(11)
        t.textColor = C.textDim
        let stats = pos.stats_7d || {}
        if (stats.trades > 0) {
            w.addSpacer(4)
            let s = w.addText("7d: " + stats.win_rate + "% WR")
            s.font = F.mono(10)
            s.textColor = C.textDim
        }
        w.addSpacer()
        addFooter(w, data.ts)
        return
    }

    let p = open[0]
    let dirColor = p.dir === "long" ? C.green : C.red
    let rColor = (p.rr_now || 0) >= 0 ? C.green : C.red
    let arrow = p.dir === "long" ? "▲" : "▼"

    let head = w.addText(arrow + " " + p.s)
    head.font = F.monoBold(13)
    head.textColor = dirColor

    w.addSpacer(2)
    let row = w.addStack()
    row.spacing = 8
    let rr = row.addText(fmtR(p.rr_now))
    rr.font = F.monoBold(14)
    rr.textColor = rColor
    if (p.pct != null) {
        let pct = row.addText(fmtPct(p.pct))
        pct.font = F.mono(12)
        pct.textColor = rColor
    }

    w.addSpacer()
    addFooter(w, data.ts)
}

function buildPositionsMedium(w, data) {
    let pos = data.positions || {}
    let open = pos.open || []

    w.backgroundColor = C.bg
    w.setPadding(10, 14, 10, 14)

    let header = w.addStack()
    header.centerAlignContent()
    addHeader(w, "Positions")
    header.addSpacer()
    let count = header.addText(open.length + " open")
    count.font = F.mono(10)
    count.textColor = C.textDim

    w.addSpacer(4)

    if (open.length === 0) {
        let t = w.addText("No open positions")
        t.font = F.mono(12)
        t.textColor = C.textDim
        let stats = pos.stats_7d || {}
        if (stats.trades > 0) {
            w.addSpacer(4)
            let s = w.addText("7d: " + stats.trades + " trades, " + stats.win_rate + "% WR")
            s.font = F.mono(10)
            s.textColor = C.textDim
        }
        w.addSpacer()
        addFooter(w, data.ts)
        return
    }

    // Column header
    let hdr = w.addStack()
    hdr.centerAlignContent()
    let ht = hdr.addText("Trade")
    ht.font = F.monoBold(9)
    ht.textColor = C.textDim
    ht.size = new Size(56, 0)
    hdr.addSpacer()
    let hp = hdr.addText("%")
    hp.font = F.monoBold(9)
    hp.textColor = C.textDim
    hp.size = new Size(48, 0)
    let hr = hdr.addText("R:R")
    hr.font = F.monoBold(9)
    hr.textColor = C.textDim
    hr.size = new Size(60, 0)
    w.addSpacer(2)

    for (let i = 0; i < open.length; i++) {
        let p = open[i]
        let dirColor = p.dir === "long" ? C.green : C.red
        let rColor = (p.rr_now || 0) >= 0 ? C.green : C.red
        let pctColor = (p.pct || 0) >= 0 ? C.green : C.red
        let arrow = p.dir === "long" ? "▲" : "▼"

        let row = w.addStack()
        row.centerAlignContent()

        let sym = row.addText(arrow + " " + p.s)
        sym.font = F.monoBold(11)
        sym.textColor = dirColor
        sym.size = new Size(56, 0)

        row.addSpacer()

        let pct = row.addText(fmtPct(p.pct))
        pct.font = F.mono(10)
        pct.textColor = pctColor
        pct.size = new Size(48, 0)

        let rrText = fmtR(p.rr_now)
        if (p.rr_plan != null) rrText += "/" + p.rr_plan.toFixed(1)
        let rr = row.addText(rrText)
        rr.font = F.monoBold(10)
        rr.textColor = rColor
        rr.size = new Size(60, 0)

        w.addSpacer(2)
    }

    // Total R
    if (pos.count > 0) {
        w.addSpacer(4)
        let total = w.addText("Total: " + fmtR(pos.total_rr))
        total.font = F.monoBold(10)
        total.textColor = (pos.total_rr || 0) >= 0 ? C.green : C.red
    }

    w.addSpacer()
    addFooter(w, data.ts)
}

function buildPositionsLarge(w, data) {
    buildPositionsMedium(w, data)
}

// ─── Sentiment Widget ───
function buildSentimentSmall(w, data) {
    let sent = data.sentiment || {}
    w.backgroundColor = C.bg
    w.setPadding(10, 12, 10, 12)

    addHeader(w, "Market Pulse")
    w.addSpacer(4)

    let fgVal = w.addText("" + (sent.fg || "—"))
    fgVal.font = F.monoBold(30)
    // `null <= 40` is true in JS — an unavailable index would paint red (fear).
    fgVal.textColor = sent.fg == null ? C.textDim : (sent.fg >= 60 ? C.green : sent.fg <= 40 ? C.red : C.yellow)
    fgVal.centerAlignText()

    let label = w.addText(sent.fg_label || "—")
    label.font = F.mono(11)
    label.textColor = C.textDim
    label.centerAlignText()

    w.addSpacer()
    addFooter(w, data.ts)
}

function buildSentimentMedium(w, data) {
    let sent = data.sentiment || {}
    w.backgroundColor = C.bg
    w.setPadding(10, 14, 10, 14)

    // Header with regime badge
    let header = w.addStack()
    header.centerAlignContent()
    addHeader(w, "Market Pulse")
    header.addSpacer()
    if (sent.regime) {
        let regimeColor = sent.regime === "Risk-On" ? C.green : sent.regime === "Cautious" ? C.yellow : sent.regime === "Risk-Off" ? C.red : C.textDim
        let reg = header.addText(sent.regime.toUpperCase())
        reg.font = F.monoBold(9)
        reg.textColor = regimeColor
    }
    w.addSpacer(4)

    function addRow(label, value, color) {
        let row = w.addStack()
        row.centerAlignContent()
        let l = row.addText(label)
        l.font = F.mono(11)
        l.textColor = C.text
        row.addSpacer()
        let v = row.addText(value)
        v.font = F.mono(11)
        v.textColor = color || C.textDim
        w.addSpacer(2)
    }

    let fgColor = sent.fg == null ? C.textDim : (sent.fg >= 60 ? C.green : sent.fg <= 40 ? C.red : C.yellow)
    addRow("Fear & Greed", (sent.fg || "—") + "  " + (sent.fg_label || ""), fgColor)

    let fundColor = sent.btc_funding != null && sent.btc_funding > 0.03 ? C.red : sent.btc_funding != null && sent.btc_funding < -0.03 ? C.green : C.textDim
    addRow("BTC Funding", sent.btc_funding != null ? (sent.btc_funding >= 0 ? "+" : "") + sent.btc_funding.toFixed(3) + "%" : "—", fundColor)

    let lsColor = sent.ls_ratio != null && sent.ls_ratio > 1.1 ? C.red : sent.ls_ratio != null && sent.ls_ratio < 0.9 ? C.green : C.textDim
    addRow("Long/Short", sent.ls_ratio != null ? sent.ls_ratio.toFixed(2) : "—", lsColor)

    // OI
    if (sent.oi_usd != null) {
        let b = (sent.oi_usd / 1e9).toFixed(1)
        let chg = sent.oi_change != null ? " " + (sent.oi_change >= 0 ? "+" : "") + sent.oi_change.toFixed(1) + "%" : ""
        let oiColor = sent.oi_change > 0 ? C.green : sent.oi_change < 0 ? C.red : C.textDim
        addRow("BTC OI", b + "B" + chg, oiColor)
    }

    // MVRV
    if (sent.mvrv != null) {
        let mvrvColor = sent.mvrv_zone === "extreme" ? C.red : sent.mvrv_zone === "overvalued" ? C.yellow : sent.mvrv_zone === "undervalued" ? C.green : C.textDim
        addRow("MVRV Z", sent.mvrv.toFixed(2), mvrvColor)
    }

    let altColor = sent.alt_season == null ? C.textDim : (sent.alt_season > 75 ? C.green : sent.alt_season < 25 ? C.red : C.textDim)
    addRow("Alt Season", sent.alt_season != null ? "" + sent.alt_season : "—", altColor)

    w.addSpacer()
    addFooter(w, data.ts)
}

// ─── Alerts Widget ───
function buildAlertsSmall(w, data) {
    let alerts = data.alerts || {}
    w.backgroundColor = C.bg
    w.setPadding(10, 12, 10, 12)

    let header = w.addStack()
    header.centerAlignContent()
    addHeader(w, "Alerts")
    header.addSpacer()
    let count = header.addText(alerts.active + " active")
    count.font = F.monoBold(10)
    count.textColor = C.text

    w.addSpacer(4)

    if (alerts.nearest) {
        let n = alerts.nearest
        let near = w.addText(n.s + " " + n.cond + " " + fmtPrice(n.p))
        near.font = F.mono(11)
        near.textColor = C.text
        let pct = w.addText(n.pct.toFixed(1) + "% away")
        pct.font = F.mono(10)
        pct.textColor = C.blue
    } else {
        let none = w.addText("No active alerts")
        none.font = F.mono(11)
        none.textColor = C.textDim
    }

    w.addSpacer()
    addFooter(w, data.ts)
}

function buildAlertsMedium(w, data) {
    let alerts = data.alerts || {}
    let list = alerts.list || []

    w.backgroundColor = C.bg
    w.setPadding(10, 14, 10, 14)

    let header = w.addStack()
    header.centerAlignContent()
    addHeader(w, "Alerts")
    header.addSpacer()
    let count = header.addText(alerts.active + " active")
    count.font = F.monoBold(10)
    count.textColor = C.text
    w.addSpacer(4)

    if (list.length === 0) {
        let none = w.addText("No active alerts")
        none.font = F.mono(11)
        none.textColor = C.textDim
        w.addSpacer()
        addFooter(w, data.ts)
        return
    }

    // Column header
    let hdr = w.addStack()
    hdr.centerAlignContent()
    let h1 = hdr.addText("Alert")
    h1.font = F.monoBold(9)
    h1.textColor = C.textDim
    h1.size = new Size(56, 0)
    hdr.addSpacer()
    let h2 = hdr.addText("Target")
    h2.font = F.monoBold(9)
    h2.textColor = C.textDim
    hdr.addSpacer(8)
    let h3 = hdr.addText("Dist")
    h3.font = F.monoBold(9)
    h3.textColor = C.textDim
    h3.size = new Size(44, 0)
    w.addSpacer(2)

    for (let i = 0; i < list.length; i++) {
        let a = list[i]
        let condSym = a.cond.includes(">=") || a.cond.includes("≥") ? "≥" :
                      a.cond.includes("<=") || a.cond.includes("≤") ? "≤" :
                      a.cond.includes(">") ? ">" :
                      a.cond.includes("<") ? "<" : a.cond

        let row = w.addStack()
        row.centerAlignContent()

        let sym = row.addText(a.s + " " + condSym)
        sym.font = F.monoBold(11)
        sym.textColor = C.text
        sym.size = new Size(56, 0)

        row.addSpacer()
        let target = row.addText(fmtPrice(a.p))
        target.font = F.mono(10)
        target.textColor = C.text

        row.addSpacer(8)
        let pct = row.addText(a.pct != null ? a.pct.toFixed(1) + "%" : "—")
        pct.font = F.mono(10)
        pct.textColor = C.blue
        pct.size = new Size(44, 0)

        w.addSpacer(1)
    }

    // Last triggered
    if (alerts.last_triggered) {
        let lt = alerts.last_triggered
        w.addSpacer(4)
        let trig = w.addText("Last: " + lt.s + " " + lt.cond + " " + fmtPrice(lt.p))
        trig.font = F.mono(9)
        trig.textColor = C.textDim
    }

    w.addSpacer()
    addFooter(w, data.ts)
}

// ─── Lock Screen Widgets (iOS 16+) ───
function buildLockInline(w, data, type) {
    let prices = data.prices || []
    let pos = (data.positions || {}).open || []

    if (type === "positions" && pos.length > 0) {
        let parts = pos.slice(0, 2).map(p => p.s + " " + fmtR(p.rr_now))
        w.addText(parts.join(" | "))
    } else if (prices.length > 0) {
        let p = prices[0]
        w.addText(p.s + " " + fmtPrice(p.p) + " " + fmtChange(p.c))
    }
}

function buildLockCircular(w, data, type) {
    let sent = data.sentiment || {}
    let pos = data.positions || {}

    w.addSpacer(2)
    if (type === "positions") {
        let count = w.addText("" + (pos.count || 0))
        count.font = Font.boldSystemFont(18)
        count.centerAlignText()
        let label = w.addText(fmtR(pos.total_rr || 0))
        label.font = Font.systemFont(9)
        label.centerAlignText()
    } else {
        let fg = w.addText("" + (sent.fg || "—"))
        fg.font = Font.boldSystemFont(20)
        fg.centerAlignText()
        let label = w.addText("F&G")
        label.font = Font.systemFont(9)
        label.centerAlignText()
    }
}

function buildLockRect(w, data, type) {
    let prices = data.prices || []
    let pos = (data.positions || {}).open || []

    if (type === "positions") {
        for (let i = 0; i < Math.min(pos.length, 3); i++) {
            let p = pos[i]
            let row = w.addStack()
            let sym = row.addText(p.s + " " + (p.dir === "long" ? "▲" : "▼") + " ")
            sym.font = Font.monospacedSystemFont(11, 0.5)
            row.addSpacer()
            let rr = row.addText(fmtR(p.rr_now))
            rr.font = Font.boldMonospacedSystemFont(11)
        }
    } else {
        for (let i = 0; i < Math.min(prices.length, 3); i++) {
            let p = prices[i]
            let row = w.addStack()
            let sym = row.addText(p.s + " ")
            sym.font = Font.monospacedSystemFont(11, 0.5)
            let pr = row.addText(fmtPrice(p.p) + " ")
            pr.font = Font.monospacedSystemFont(11, 0.5)
            row.addSpacer()
            let chg = row.addText(fmtChange(p.c))
            chg.font = Font.monospacedSystemFont(11, 0.5)
        }
    }
}

// ─── Error State ───
function addError(w, msg) {
    w.backgroundColor = C.bg
    w.setPadding(12, 14, 12, 14)
    addHeader(w, "Error")
    w.addSpacer(6)
    let e = w.addText(msg)
    e.font = F.mono(11)
    e.textColor = C.red
}

// ─── Auto-Update ───
async function checkForUpdate() {
    let fm = FileManager.local()
    let checkPath = fm.joinPath(fm.temporaryDirectory(), UPDATE_KEY)
    let now = Date.now()

    try {
        if (fm.fileExists(checkPath)) {
            let last = parseInt(fm.readString(checkPath))
            if (now - last < 86400000) return
        }
    } catch (_) {}

    try {
        let req = new Request(SCRIPT_URL)
        req.timeoutInterval = 10
        let remote = await req.loadString()
        let match = remote.match(/const VERSION = "(.+?)"/)
        if (match && match[1] !== VERSION) {
            let self = fm.joinPath(fm.documentsDirectory(), Script.name() + ".js")
            fm.writeString(self, remote)
            console.log("Updated to version " + match[1])
        }
        fm.writeString(checkPath, "" + now)
    } catch (e) {
        console.log("Update check failed: " + e)
    }
}

// ─── Main ───
async function main() {
    let token = await getToken()
    if (!token) {
        let w = new ListWidget()
        addError(w, "No token.\nGo to Settings → Widgets")
        Script.setWidget(w)
        Script.complete()
        return
    }

    let data = await fetchData(token)
    let type = getWidgetType()
    let family = config.widgetFamily || "medium"

    let w = new ListWidget()
    w.url = API_BASE + "/"

    if (!data || !data.ts) {
        addError(w, "Cannot reach server")
    } else if (family === "accessoryInline") {
        buildLockInline(w, data, type)
    } else if (family === "accessoryCircular") {
        buildLockCircular(w, data, type)
    } else if (family === "accessoryRectangular") {
        buildLockRect(w, data, type)
    } else {
        let builders = {
            watchlist:  { small: buildWatchlistSmall, medium: buildWatchlistMedium, large: buildWatchlistLarge },
            positions:  { small: buildPositionsSmall, medium: buildPositionsMedium, large: buildPositionsLarge },
            sentiment:  { small: buildSentimentSmall, medium: buildSentimentMedium, large: buildSentimentMedium },
            alerts:     { small: buildAlertsSmall, medium: buildAlertsMedium, large: buildAlertsMedium },
        }
        let build = (builders[type] || builders.watchlist)[family] || buildWatchlistMedium
        build(w, data)
    }

    w.refreshAfterDate = new Date(Date.now() + 300000)

    Script.setWidget(w)
    Script.complete()

    await checkForUpdate()
}

await main()
