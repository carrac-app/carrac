/* Finnhub quotes — polled via server-side proxy. */
// ============================================================
// The server-side worker maintains the Finnhub WebSocket and writes ticks to
// Redis; this module polls /api/finnhub/quotes every few seconds. The browser
// never opens the upstream socket, so the API key is never sent to clients.
//
// Proxy response shape:
//   { "ticks": { "AAPL": {"symbol":"AAPL","price":258.9,"ts":...}, ... } }
// ============================================================

var finnhubPollTimer = null;
var finnhubPollIntervalMs = 3000;  // 3s — smooth enough for stocks; adjustable
var _finnhubPollIntervalDefault = 3000;
var _finnhubConsecutiveErrors = 0;
var finnhubLastKnown = {};  // sym -> last ts we processed

// US stocks + ETFs that Finnhub covers (matches stooq.stock_map + etf_map keys)
var FINNHUB_SYMBOLS = [
    // Stocks
    'AAPL', 'GOOG', 'AMZN', 'META', 'TSLA', 'NVDA', 'MSFT',
    'AMD', 'INTC', 'QCOM',
    'BA', 'LMT', 'RTX', 'NOC', 'GD', 'HON',
    'XOM', 'CVX',
    'JPM', 'GS', 'V', 'MA',
    'UNH', 'JNJ',
    'PG', 'KO', 'WMT',
    'MSTR',
    'COIN', 'RIOT', 'MARA', 'HOOD',
    'SOFI', 'SQ', 'PYPL',
    'PLTR', 'ARM', 'SMCI', 'CRWD', 'NET', 'SNOW',
    'NFLX', 'DIS', 'UBER',
    'LLY', 'PFE',
    'TNK',
    // ETFs
    'VTI', 'QQQ', 'IWM', 'SPY',
    'XLE', 'XLF', 'XLK', 'XLV',
    'GDX', 'URA', 'MAGS',
    'TLT', 'HYG', 'LQD', 'SHY',
    'ARKK', 'IBIT', 'BITO',
    'SOXX', 'SMH', 'IGV',
    'XBI', 'VNQ', 'XLP', 'XLI'
];


function initFinnhubWebSocket() {
    // Retained name for backward compat with call sites elsewhere; now starts
    // the polling loop instead of opening a WS.
    if (finnhubPollTimer) return;
    _pollFinnhub();  // fire once immediately
    finnhubPollTimer = setInterval(function () {
        if (document.hidden) return;  // pause on background tabs (mobile battery)
        _pollFinnhub();
    }, finnhubPollIntervalMs);
}


function _applyFinnhubTick(symbol, price) {
    if (!price || price <= 0) return;

    // Update allAssets price (for table + ticker bar)
    var asset = (typeof allAssets !== 'undefined') ? allAssets[symbol] : null;
    if (asset) {
        asset.latest_price = price;

        // Flash the price cell
        var rows = document.querySelectorAll('tr[data-symbol="' + symbol + '"]');
        rows.forEach(function(row) {
            var priceCell = row.querySelector('.price');
            if (priceCell) {
                var _fmt = typeof formatPrice === 'function' ? formatPrice(price, symbol, 'stock') : String(price);
                if (typeof CarracTick !== 'undefined') {
                    priceCell.classList.add('tk-soft');
                    CarracTick.paint(priceCell, _fmt);
                } else {
                    priceCell.innerHTML = _fmt;
                    priceCell.classList.add('ws-flash');
                    setTimeout(function() { priceCell.classList.remove('ws-flash'); }, 600);
                }
            }
            row.setAttribute('data-price', price);
        });
    }

    // Live-update KLineChart candle / stale-chart recovery — centralized in chart-live-tick.js.
    if (typeof window.applyChartTick === 'function') {
        window.applyChartTick(symbol, price);
    }
}


function _pollFinnhub() {
    fetch('/api/finnhub/quotes', { credentials: 'same-origin' })
        .then(function (r) {
            if (!r.ok) { _finnhubConsecutiveErrors++; return null; }
            _finnhubConsecutiveErrors = 0;
            return r.json();
        })
        .then(function (data) {
            if (!data || !data.ticks) return;
            if (typeof CarracConn !== 'undefined') { CarracConn.report('finnhub', 'connected'); CarracConn.heartbeat('finnhub'); }
            // Reset polling interval to default on successful data
            if (finnhubPollIntervalMs !== _finnhubPollIntervalDefault) {
                clearInterval(finnhubPollTimer);
                finnhubPollIntervalMs = _finnhubPollIntervalDefault;
                finnhubPollTimer = setInterval(function() {
                    if (document.hidden) return;
                    _pollFinnhub();
                }, finnhubPollIntervalMs);
            }
            var changed = false;
            Object.keys(data.ticks).forEach(function (sym) {
                var t = data.ticks[sym];
                if (!t || t.ts == null) return;
                // De-dupe: only apply if the ts moved since last poll.
                if (finnhubLastKnown[sym] === t.ts) return;
                finnhubLastKnown[sym] = t.ts;
                _applyFinnhubTick(sym, t.price);
                changed = true;
            });

            // Throttled drawer position update (same as the old handler)
            if (changed && !window._drawerPosTimer) {
                window._drawerPosTimer = setTimeout(function () {
                    if (typeof updateDrawerPosition === 'function') updateDrawerPosition();
                    window._drawerPosTimer = null;
                }, 5000);
            }
        })
        .catch(function () {
            _finnhubConsecutiveErrors++;
            if (_finnhubConsecutiveErrors >= 3) {
                if (typeof CarracConn !== 'undefined') CarracConn.report('finnhub', 'reconnecting');
                clearInterval(finnhubPollTimer);
                finnhubPollIntervalMs = Math.min(finnhubPollIntervalMs * 2, 30000);
                finnhubPollTimer = setInterval(function() {
                    if (document.hidden) return;
                    _pollFinnhub();
                }, finnhubPollIntervalMs);
            }
        });
}


function subscribeFinnhubSymbol(symbol) {
    // Kick the polling loop if it hasn't started yet. Symbols aren't
    // subscribed per-call anymore — the worker keeps every curated symbol
    // in Redis; this just makes sure the browser is polling.
    initFinnhubWebSocket();
}
window.subscribeFinnhubSymbol = subscribeFinnhubSymbol;

// ─── Market Status Dot ───────────────────────────────
// Small dot AFTER the symbol: green = market open, gray = closed.
// Covers all tradfi assets with region-aware trading hours.

// Symbol → market region mapping
var MARKET_REGION = {};
// US stocks + ETFs + indices + forex ETFs + commodities (all trade on US exchanges)
var US_SYMBOLS = FINNHUB_SYMBOLS.concat([
    'SPX', 'SPY', 'NDX', 'DJI', 'RUT', 'VIX',
    'US10Y', 'US30Y', 'US5Y',
    'OIL', 'GOLD', 'SILVER', 'COPPER', 'NATGAS', 'PLATINUM',
    'CORN', 'WHEAT', 'SOYBEANS',
    'DXY', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD',
    'EURHUF', 'USDHUF'
]);
US_SYMBOLS.forEach(function(s) { MARKET_REGION[s] = 'us'; });
// European
['DAX', 'FTSE', 'CAC', 'BUX', 'RHM'].forEach(function(s) { MARKET_REGION[s] = 'eu'; });
// Asian
['NIKKEI'].forEach(function(s) { MARKET_REGION[s] = 'jp'; });
['HSI', 'HSTECH'].forEach(function(s) { MARKET_REGION[s] = 'hk'; });
['KOSPI'].forEach(function(s) { MARKET_REGION[s] = 'kr'; });

function _isWeekday(d) { var day = d.getDay(); return day > 0 && day < 6; }

function isMarketOpenForSymbol(sym) {
    var region = MARKET_REGION[sym];
    if (!region) return null;  // Not a tradfi symbol

    var now = new Date();
    if (region === 'us') {
        var et = new Date(now.toLocaleString('en-US', {timeZone: 'America/New_York'}));
        if (!_isWeekday(et)) return false;
        var m = et.getHours() * 60 + et.getMinutes();
        return m >= 570 && m < 960;  // 9:30-16:00 ET
    }
    if (region === 'eu') {
        var cet = new Date(now.toLocaleString('en-US', {timeZone: 'Europe/Berlin'}));
        if (!_isWeekday(cet)) return false;
        var m = cet.getHours() * 60 + cet.getMinutes();
        return m >= 540 && m < 1050;  // 9:00-17:30 CET
    }
    if (region === 'jp') {
        var jst = new Date(now.toLocaleString('en-US', {timeZone: 'Asia/Tokyo'}));
        if (!_isWeekday(jst)) return false;
        var m = jst.getHours() * 60 + jst.getMinutes();
        return (m >= 540 && m < 690) || (m >= 750 && m < 900);  // 9:00-11:30 + 12:30-15:00 JST
    }
    if (region === 'hk') {
        var hkt = new Date(now.toLocaleString('en-US', {timeZone: 'Asia/Hong_Kong'}));
        if (!_isWeekday(hkt)) return false;
        var m = hkt.getHours() * 60 + hkt.getMinutes();
        return (m >= 570 && m < 720) || (m >= 780 && m < 960);  // 9:30-12:00 + 13:00-16:00 HKT
    }
    if (region === 'kr') {
        var kst = new Date(now.toLocaleString('en-US', {timeZone: 'Asia/Seoul'}));
        if (!_isWeekday(kst)) return false;
        var m = kst.getHours() * 60 + kst.getMinutes();
        return m >= 540 && m < 930;  // 9:00-15:30 KST
    }
    return null;
}


function updateMarketDots() {
    document.querySelectorAll('.market-dot').forEach(function(dot) {
        var sym = dot.getAttribute('data-mkt-symbol');
        if (!sym) return;
        var open = isMarketOpenForSymbol(sym);
        if (open === null) return;
        dot.className = 'market-dot ' + (open ? 'market-open' : 'market-closed');
        dot.title = open ? 'Market open' : 'Market closed';
    });
}

function injectMarketDots() {
    // One pass over the rows, not one document-wide query per tradfi symbol.
    // A per-symbol lookup is O(symbols x DOM): ~200 querySelectorAll() calls over
    // a 40k-node document per invocation, and the observer below invokes this on
    // every price repaint.
    document.querySelectorAll('tr[data-symbol] td.symbol').forEach(function(td) {
        var row = td.parentNode;
        var sym = row && row.getAttribute('data-symbol');
        if (!sym || !Object.prototype.hasOwnProperty.call(MARKET_REGION, sym)) return;
        if (td.querySelector('.market-dot')) return;
        var open = isMarketOpenForSymbol(sym);
        if (open === null) return;
        var dot = document.createElement('span');
        dot.className = 'market-dot ' + (open ? 'market-open' : 'market-closed');
        dot.title = open ? 'Market open' : 'Market closed';
        dot.setAttribute('data-mkt-symbol', sym);
        // Insert AFTER the symbol text span (not before)
        var symbolSpan = td.querySelector('.symbol-chart');
        if (symbolSpan && symbolSpan.nextSibling) {
            td.insertBefore(dot, symbolSpan.nextSibling);
        } else {
            td.appendChild(dot);
        }
    });
}

var _dotObserver = null;
var _dotPending = null;
function startDotObserver() {
    var table = document.querySelector('.asset-table tbody') || document.querySelector('table tbody');
    if (!table || _dotObserver) return;
    _dotObserver = new MutationObserver(function(records) {
        // Dots only need (re)injecting when ROWS are (re)rendered. A price
        // repaint replaces spans inside a cell and our own dot insert adds a
        // span — both are subtree childList mutations, neither is a new row.
        // Reacting to every one of them re-ran the full scan ~10x per second.
        var rowAdded = false;
        for (var i = 0; i < records.length && !rowAdded; i++) {
            var added = records[i].addedNodes;
            for (var j = 0; j < added.length; j++) {
                if (added[j].nodeName === 'TR') { rowAdded = true; break; }
            }
        }
        if (!rowAdded || _dotPending) return;
        _dotPending = setTimeout(function() { _dotPending = null; injectMarketDots(); }, 100);
    });
    _dotObserver.observe(table, {childList: true, subtree: true});
}

var _marketDotsTimer = setInterval(updateMarketDots, 30000);
document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
        if (_marketDotsTimer) { clearInterval(_marketDotsTimer); _marketDotsTimer = null; }
    } else if (!_marketDotsTimer) {
        updateMarketDots();
        _marketDotsTimer = setInterval(updateMarketDots, 30000);
    }
});
setTimeout(function() { injectMarketDots(); startDotObserver(); }, 2000);

// Auto-init on page load
initFinnhubWebSocket();
