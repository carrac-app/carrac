/* CCXT Exchange WebSocket — Live candle updates for Gate.io + Bybit coins */
// ============================================================
// Browser-direct WebSocket connections to exchange endpoints.
// The chart API returns ws_venue='ccxt' + ws_exchange='bybit'|'gateio'
// and this module routes to the correct exchange WS.
//
// KuCoin requires a REST token for WS (too complex for browser) —
// XMR (kucoin-routed) ticks via the Kraken table ticker instead
// (table-tickers-ws.js). MEXC is not supported: its public JSON kline
// channel answers every subscribe with "Blocked!" — mexc-routed coins get
// REST candles, no browser WS.
// ============================================================

var _ccxtWS = null;
var _ccxtWSReconnectTimer = null;
var _ccxtWSReconnectDelay = 5000;
var _ccxtWSMaxDelay = 60000;
var _ccxtCurrentSymbol = null;
var _ccxtCurrentExchange = null;

// Track last-message timestamp for silent-death detection. WebSockets can die
// without firing onclose (mobile sleep, ISP drops, etc.); without this, the
// ticker bar would read stale allAssets[...].latest_price forever.
var _ccxtWSLastMessage = 0;
var _ccxtWSStaleCheckTimer = null;
var _CCXT_WS_STALE_MS = 60000;

// Exchange-specific WS configuration
var CCXT_WS_CONFIG = {
    'bybit': {
        url: 'wss://stream.bybit.com/v5/public/spot',
        // Subscribe: {"op":"subscribe","args":["kline.1.XMRUSDT"]}
        subscribe: function(symbol) {
            return JSON.stringify({
                op: 'subscribe',
                args: ['kline.1.' + symbol + 'USDT']
            });
        },
        // Parse: {"topic":"kline.1.XMRUSDT","data":[{"start":...,"open":"...","close":"...","high":"...","low":"...","volume":"...","confirm":false}]}
        parse: function(msg) {
            if (!msg.topic || !msg.topic.startsWith('kline.')) return null;
            var d = msg.data;
            if (!d || !d.length) return null;
            var c = d[0];
            return {
                symbol: msg.topic.split('.')[2].replace('USDT', ''),
                close: parseFloat(c.close),
                high: parseFloat(c.high),
                low: parseFloat(c.low),
                open: parseFloat(c.open),
                volume: parseFloat(c.volume),
                timestamp: parseInt(c.start)
            };
        },
        ping: function() { return JSON.stringify({op: 'ping'}); }
    },
    // No 'mexc' entry: its public JSON kline channel answers every subscribe
    // with "Blocked!" (protobuf-only), which would receive nothing and loop the
    // 60s stale-reconnect.
    'gateio': {
        url: 'wss://api.gateio.ws/ws/v4/',
        // Subscribe: {"time":1234,"channel":"spot.candlesticks","event":"subscribe","payload":["1m","XMR_USDT"]}
        subscribe: function(symbol) {
            return JSON.stringify({
                time: Math.floor(Date.now() / 1000),
                channel: 'spot.candlesticks',
                event: 'subscribe',
                payload: ['1m', symbol + '_USDT']
            });
        },
        // Parse: {"channel":"spot.candlesticks","event":"update","result":{"t":"1234","v":"100","c":"150.5","h":"151","l":"149","o":"150","n":"1m_XMR_USDT"}}
        parse: function(msg) {
            if (msg.channel !== 'spot.candlesticks' || msg.event !== 'update') return null;
            var r = msg.result;
            if (!r) return null;
            var nameParts = (r.n || '').split('_');
            var sym = nameParts.length >= 2 ? nameParts[1] : '';
            return {
                symbol: sym,
                close: parseFloat(r.c),
                high: parseFloat(r.h),
                low: parseFloat(r.l),
                open: parseFloat(r.o),
                volume: parseFloat(r.v),
                timestamp: parseInt(r.t) * 1000 // Gate uses seconds
            };
        },
        ping: null // Gate.io doesn't need manual ping
    }
};


function initCcxtWebSocket(exchange, symbol) {
    // Close existing connection if switching exchanges. Null the handlers FIRST:
    // this close is intentional — the old socket's async onclose would otherwise
    // fire AFTER the new socket's onopen and report a stale 'reconnecting' to
    // CarracConn (permanent amber dot) + arm a ghost reconnect timer.
    if (_ccxtWS && _ccxtWS.readyState <= 1) {
        _ccxtWS.onclose = null;
        _ccxtWS.onerror = null;
        _ccxtWS.close();
        _ccxtWS = null;
    }
    if (_ccxtWSReconnectTimer) {
        clearTimeout(_ccxtWSReconnectTimer);
        _ccxtWSReconnectTimer = null;
    }
    if (_ccxtWSStaleCheckTimer) { clearInterval(_ccxtWSStaleCheckTimer); _ccxtWSStaleCheckTimer = null; }

    var config = CCXT_WS_CONFIG[exchange];
    if (!config) {
        console.warn('[CCXT-WS] No WS config for exchange:', exchange);
        return;
    }

    _ccxtCurrentSymbol = symbol.toUpperCase();
    _ccxtCurrentExchange = exchange;

    try {
        _ccxtWS = new WebSocket(config.url);
    } catch (e) {
        console.warn('[CCXT-WS] Failed:', e);
        _scheduleCcxtReconnect();
        return;
    }

    _ccxtWS.onopen = function() {
        _ccxtWSReconnectDelay = 5000;
        _ccxtWSLastMessage = Date.now();
        if (typeof CarracConn !== 'undefined') CarracConn.report('ccxt', 'connected');

        if (_ccxtWSStaleCheckTimer) clearInterval(_ccxtWSStaleCheckTimer);
        _ccxtWSStaleCheckTimer = setInterval(function() {
            if (!_ccxtWS || _ccxtWS.readyState !== WebSocket.OPEN) return;
            if (Date.now() - _ccxtWSLastMessage > _CCXT_WS_STALE_MS) {
                console.warn('[CCXT-WS] No message for ' + _CCXT_WS_STALE_MS + 'ms — closing for reconnect');
                try { _ccxtWS.close(); } catch (e) {}
                if (typeof CarracConn !== 'undefined') CarracConn.report('ccxt', 'reconnecting');
                // onclose handler will trigger reconnect via _scheduleCcxtReconnect()
            }
        }, Math.max(15000, _CCXT_WS_STALE_MS / 2));

        // Subscribe to kline for current symbol
        var subMsg = config.subscribe(_ccxtCurrentSymbol);
        _ccxtWS.send(subMsg);

        // Start ping interval if needed
        if (config.ping) {
            if (_ccxtWS._pingInterval) clearInterval(_ccxtWS._pingInterval);
            _ccxtWS._pingInterval = setInterval(function() {
                if (_ccxtWS && _ccxtWS.readyState === WebSocket.OPEN) {
                    _ccxtWS.send(config.ping());
                }
            }, 20000);
        }
    };

    _ccxtWS.onmessage = function(event) {
        _ccxtWSLastMessage = Date.now();
        if (typeof CarracConn !== 'undefined') CarracConn.heartbeat('ccxt');
        try {
            var msg = JSON.parse(event.data);
            var parsed = config.parse(msg);
            if (!parsed || !parsed.close || parsed.close <= 0) return;

            var dashSymbol = parsed.symbol.toUpperCase();
            var price = parsed.close;

            // Update allAssets price (for table + ticker bar)
            var asset = (typeof allAssets !== 'undefined') ? allAssets[dashSymbol] : null;
            if (asset) {
                asset.latest_price = price;

                // Flash the price cell
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

            // Throttled drawer position update
            if (!window._drawerPosTimer) {
                window._drawerPosTimer = setTimeout(function() {
                    if (typeof updateDrawerPosition === 'function') updateDrawerPosition();
                    window._drawerPosTimer = null;
                }, 5000);
            }
        } catch (e) {}
    };

    _ccxtWS.onclose = function() {
        if (_ccxtWSStaleCheckTimer) { clearInterval(_ccxtWSStaleCheckTimer); _ccxtWSStaleCheckTimer = null; }
        if (_ccxtWS && _ccxtWS._pingInterval) {
            clearInterval(_ccxtWS._pingInterval);
        }
        if (typeof CarracConn !== 'undefined') CarracConn.report('ccxt', 'reconnecting');
        _scheduleCcxtReconnect();
    };

    _ccxtWS.onerror = function() {
        if (typeof CarracConn !== 'undefined') CarracConn.report('ccxt', 'reconnecting');
    };
}


function subscribeCcxtSymbol(symbol, exchange) {
    if (!exchange || !CCXT_WS_CONFIG[exchange]) {
        // KuCoin/MEXC/unknown — no browser WS for this venue. XMR still ticks
        // via the Kraken table ticker (table-tickers-ws.js); others rely on
        // periodic REST refreshes.
        return;
    }
    initCcxtWebSocket(exchange, symbol);
}
window.subscribeCcxtSymbol = subscribeCcxtSymbol;


function _scheduleCcxtReconnect() {
    if (_ccxtWSReconnectTimer) return;
    _ccxtWSReconnectTimer = setTimeout(function() {
        _ccxtWSReconnectTimer = null;
        if (_ccxtCurrentExchange && _ccxtCurrentSymbol) {
            initCcxtWebSocket(_ccxtCurrentExchange, _ccxtCurrentSymbol);
        }
        _ccxtWSReconnectDelay = Math.min(_ccxtWSReconnectDelay * 2, _ccxtWSMaxDelay);
    }, _ccxtWSReconnectDelay);
}
