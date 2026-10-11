/* Hyperliquid WebSocket — Live candle updates for HL-only coins */
// ============================================================
// Mirrors binance-ws.js pattern. Subscribes to 1m candle stream
// for the currently-displayed HL symbol and ticks the chart's
// last candle close/high/low in real time.
//
// The server /api/chart response includes ws_venue='hyperliquid'
// for HL coins — chart-loading.js checks this and calls subscribeHLSymbol()
// instead of subscribeBinanceSymbol().
// ============================================================

var hlWS = null;
var hlWSReconnectTimer = null;
var hlWSReconnectDelay = 5000;
var hlWSMaxDelay = 60000;

// Track last-message timestamp for silent-death detection. WebSockets can die
// without firing onclose (mobile sleep, ISP drops, etc.); without this, the
// ticker bar would read stale allAssets[...].latest_price forever.
var _hlWSLastMessage = 0;
var _hlWSStaleCheckTimer = null;
var _HL_WS_STALE_MS = 60000;
var _hlCurrentSymbol = null;      // The HL coin currently on the CHART (for chart updates)
var _hlCurrentApiCoin = null;     // The HL API name of the chart symbol

// Uppercase (internal) → HL API name for k-prefix coins.
// Must stay in sync with K_PREFIX_TRANSLATION in data_sources/hyperliquid.py.
var HL_K_PREFIX = {
    'KPEPE': 'kPEPE', 'KBONK': 'kBONK', 'KSHIB': 'kSHIB',
    'KFLOKI': 'kFLOKI', 'KDOGS': 'kDOGS'
};
// Reverse: HL API name → uppercase
var HL_K_REVERSE = {};
Object.keys(HL_K_PREFIX).forEach(function(k) { HL_K_REVERSE[HL_K_PREFIX[k]] = k; });

// All HL coins — subscribed on page load for table price ticking.
// Must stay in sync with HL_COINS in data_sources/hyperliquid.py.
var HL_ALL_COINS = [
    'HYPE', 'FARTCOIN', 'MON', 'LIT', 'KPEPE',
    'VVV', 'GRIFFAIN', 'KBONK', 'CHILLGUY',
    'AERO', 'GRASS', 'BRETT', 'PURR'
];


function initHyperliquidWebSocket() {
    if (hlWS && hlWS.readyState <= 1) return;  // Already open/connecting

    try {
        hlWS = new WebSocket('wss://api.hyperliquid.xyz/ws');
    } catch (e) {
        console.warn('[HL-WS] Failed:', e);
        scheduleHLReconnect();
        return;
    }

    hlWS.onopen = function() {
        hlWSReconnectDelay = 5000;
        _hlWSLastMessage = Date.now();
        if (typeof CarracConn !== 'undefined') CarracConn.report('hyperliquid', 'connected');

        if (_hlWSStaleCheckTimer) clearInterval(_hlWSStaleCheckTimer);
        _hlWSStaleCheckTimer = setInterval(function() {
            if (!hlWS || hlWS.readyState !== WebSocket.OPEN) return;
            if (Date.now() - _hlWSLastMessage > _HL_WS_STALE_MS) {
                console.warn('[HL-WS] No message for ' + _HL_WS_STALE_MS + 'ms — closing for reconnect');
                try { hlWS.close(); } catch (e) {}
                if (typeof CarracConn !== 'undefined') CarracConn.report('hyperliquid', 'reconnecting');
                // onclose handler will trigger reconnect via scheduleHLReconnect()
            }
        }, Math.max(15000, _HL_WS_STALE_MS / 2));

        // Subscribe to ALL HL coins for table price ticking (like Binance miniTicker)
        HL_ALL_COINS.forEach(function(sym) {
            _hlSendSubscribe(sym);
        });
    };

    hlWS.onmessage = function(event) {
        _hlWSLastMessage = Date.now();
        if (typeof CarracConn !== 'undefined') CarracConn.heartbeat('hyperliquid');
        try {
            var msg = JSON.parse(event.data);
            if (msg.channel !== 'candle') return;

            // HL sends data as a dict (single candle), not a list
            var candle = msg.data;
            if (!candle || !candle.s) return;

            var apiCoin = candle.s;
            var dashSymbol = HL_K_REVERSE[apiCoin] || apiCoin.toUpperCase();
            var price = parseFloat(candle.c);
            if (!price || price <= 0) return;

            // Update allAssets price (for table + ticker bar)
            var asset = (typeof allAssets !== 'undefined') ? allAssets[dashSymbol] : null;
            if (asset) {
                asset.latest_price = price;

                // Flash the price cell in the asset table
                var rows = document.querySelectorAll('tr[data-symbol="' + dashSymbol + '"]');
                rows.forEach(function(row) {
                    var priceCell = row.querySelector('.price');
                    if (priceCell) {
                        var _fmt = typeof formatPrice === 'function' ? formatPrice(price, dashSymbol, 'crypto') : String(price);
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
                window.applyChartTick(dashSymbol, price);
            }

            // Throttled drawer position update (same pattern as binance-ws.js)
            if (!window._drawerPosTimer) {
                window._drawerPosTimer = setTimeout(function() {
                    if (typeof updateDrawerPosition === 'function') updateDrawerPosition();
                    window._drawerPosTimer = null;
                }, 5000);
            }
        } catch (e) {}
    };

    hlWS.onclose = function() {
        if (_hlWSStaleCheckTimer) { clearInterval(_hlWSStaleCheckTimer); _hlWSStaleCheckTimer = null; }
        if (typeof CarracConn !== 'undefined') CarracConn.report('hyperliquid', 'reconnecting');
        scheduleHLReconnect();
    };

    hlWS.onerror = function() {
        if (typeof CarracConn !== 'undefined') CarracConn.report('hyperliquid', 'reconnecting');
    };
}


function _hlSendSubscribe(dashSymbol) {
    if (!hlWS || hlWS.readyState !== WebSocket.OPEN) return;
    var apiCoin = HL_K_PREFIX[dashSymbol] || dashSymbol;
    // Subscribe to 1m candle for maximum tick granularity
    hlWS.send(JSON.stringify({
        method: 'subscribe',
        subscription: { type: 'candle', coin: apiCoin, interval: '1m' }
    }));
}


/**
 * Mark an HL symbol as the current chart symbol for live candle updates.
 * Called from chart-loading.js when /api/chart returns ws_venue='hyperliquid'.
 * The WS is already connected + subscribed to all HL coins on page load;
 * this just tells onmessage which symbol should update the chart candle.
 */
function subscribeHLSymbol(symbol) {
    var upper = symbol.toUpperCase();
    _hlCurrentSymbol = upper;
    _hlCurrentApiCoin = HL_K_PREFIX[upper] || upper;

    // Ensure WS is connected (may have died since page load)
    if (!hlWS || hlWS.readyState > 1) {
        initHyperliquidWebSocket();
    }
}

// Expose globally for chart-loading.js
window.subscribeHLSymbol = subscribeHLSymbol;


function scheduleHLReconnect() {
    if (hlWSReconnectTimer) return;
    hlWSReconnectTimer = setTimeout(function() {
        hlWSReconnectTimer = null;
        initHyperliquidWebSocket();
        hlWSReconnectDelay = Math.min(hlWSReconnectDelay * 2, hlWSMaxDelay);
    }, hlWSReconnectDelay);
}

// Auto-init on page load — subscribe to all HL coins for table price ticking
// (mirrors initBinanceWebSocket() which is called on page load too)
initHyperliquidWebSocket();
