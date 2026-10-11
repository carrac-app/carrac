/* Table tickers — Gate.io (AKT, DEEP, ARC, CHEX, DSYNC, KIP, MLC, PIPPIN,
 * PRIME) + Kraken (XMR) + Bybit (CC) live watchlist prices.
 *
 * CoinCap is not used: its public WebSocket accepts the handshake, answers
 * "Unauthorized: API key is required" and closes, which keeps a provider in an
 * eternal reconnect loop (and the conn dot permanently amber).
 *
 * Coverage notes:
 *  - FARTCOIN is intentionally absent — hyperliquid-ws.js already ticks it
 *    on page load (HL_ALL_COINS).
 *  - XMR has no other free browser WS (Gate/Bybit delisted it, MEXC blocked
 *    its JSON channel, KuCoin needs a REST token) → Kraken public v2 WS.
 *  - CC is Bybit-routed — separate connector below,
 *    same makeConnector factory as gate/kraken.
 *  - AKT/DEEP chart-follow WS (ccxt-ws.js) is separate; this module keeps the
 *    watchlist TABLE ticking even when no chart is open.
 *
 * Mirrors the binance-ws.js pattern: backoff reconnect + silent-death
 * stale-check + CarracConn lifecycle reports. All init fns are idempotent —
 * the main.js WS supervisor calls initTableTickers() on a pre-existing
 * interval (escapes Chrome's resume timer parking, see round 7b).
 */

// ─── Pure parsers (node-tested in tests/js/table-tickers-parse.test.cjs) ──

// Gate.io: {"channel":"spot.candlesticks","event":"update",
//           "result":{"c":"0.6342","n":"1m_AKT_USDT",...}}
function parseGateTickMessage(msg) {
    if (!msg || msg.channel !== 'spot.candlesticks' || msg.event !== 'update') return null;
    var r = msg.result;
    if (!r || !r.n) return null;
    var parts = String(r.n).split('_');   // "1m_AKT_USDT" → [1m, AKT, USDT]
    if (parts.length < 3) return null;
    var price = parseFloat(r.c);
    if (isNaN(price) || price <= 0) return null;
    return { symbol: parts[1].toUpperCase(), price: price };
}

// Kraken v2: {"channel":"ticker","type":"snapshot"|"update",
//             "data":[{"symbol":"XMR/USD","last":306.18,...}]}
function parseKrakenTickMessage(msg) {
    if (!msg || msg.channel !== 'ticker' || !msg.data || !msg.data.length) return null;
    var d = msg.data[0];
    if (!d || !d.symbol) return null;
    var price = typeof d.last === 'number' ? d.last : parseFloat(d.last);
    if (isNaN(price) || price <= 0) return null;
    return { symbol: String(d.symbol).split('/')[0].toUpperCase(), price: price };
}

// Bybit v5 public spot: {"topic":"kline.1.CCUSDT","data":[{"close":"1.23",...}]}
// Non-kline frames (subscribe ack, pong) have no "topic" starting with
// "kline." and fall through to null (see ccxt-ws.js CCXT_WS_CONFIG.bybit,
// the reference this mirrors — not modified by this change).
function parseBybitTickMessage(msg) {
    if (!msg || typeof msg.topic !== 'string' || msg.topic.indexOf('kline.1.') !== 0) return null;
    var d = msg.data;
    if (!d || !d.length) return null;
    var c = d[0];
    if (!c) return null;
    var price = parseFloat(c.close);
    if (isNaN(price) || price <= 0) return null;
    return { symbol: msg.topic.split('.')[2].replace('USDT', ''), price: price };
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        parseGateTickMessage: parseGateTickMessage,
        parseKrakenTickMessage: parseKrakenTickMessage,
        parseBybitTickMessage: parseBybitTickMessage
    };
}

// ─── Browser wiring ────────────────────────────────────────────────────────

if (typeof window !== 'undefined') (function () {

    // Shared table-row updater (same DOM contract as binance-ws.js onmessage).
    function applyTableTick(dashSymbol, price) {
        var asset = (typeof allAssets !== 'undefined') ? allAssets[dashSymbol] : null;
        if (!asset) return;
        asset.latest_price = price;

        var rows = document.querySelectorAll('tr[data-symbol="' + dashSymbol + '"]');
        rows.forEach(function (row) {
            var priceCell = row.querySelector('.price');
            if (priceCell) {
                var _fmt = typeof formatPrice === 'function' ? formatPrice(price, dashSymbol, 'crypto') : String(price);
                if (typeof CarracTick !== 'undefined') {
                    priceCell.classList.add('tk-soft');
                    CarracTick.paint(priceCell, _fmt);
                } else {
                    priceCell.innerHTML = _fmt;
                    priceCell.classList.add('ws-flash');
                    setTimeout(function () { priceCell.classList.remove('ws-flash'); }, 600);
                }
            }
            var changeCell = row.querySelector('.change-col');
            if (changeCell) {
                var changePct = asset.realtime ? asset.realtime.change_pct : null;
                if (changePct !== null && changePct !== undefined && !isNaN(changePct)) {
                    var cls = changePct >= 0 ? 'price-change-up' : 'price-change-down';
                    var sign = changePct >= 0 ? '+' : '';
                    changeCell.innerHTML = '<span class="price-change ' + cls + '">' + sign + changePct.toFixed(2) + '%</span>';
                }
            }
            if (window._changevalVisible && typeof _assetChangeValue === 'function') {
                var cvCell = row.querySelector('.change-value-col');
                var cv = cvCell ? _assetChangeValue(asset) : null;
                if (cv !== null && typeof formatChangeValue === 'function') {
                    var cvCls = cv >= 0 ? 'price-change-up' : 'price-change-down';
                    cvCell.innerHTML = '<span class="price-change ' + cvCls + '">' + formatChangeValue(cv, dashSymbol, 'crypto') + '</span>';
                }
            }
            row.setAttribute('data-price', price);
        });

        // Live-update KLineChart candle / stale-chart recovery — centralized in chart-live-tick.js.
        if (typeof window.applyChartTick === 'function') {
            window.applyChartTick(dashSymbol, price);
        }
    }

    // Generic connector factory — both venues share the exact lifecycle:
    // idempotent init, backoff reconnect, silent-death stale-check, CarracConn.
    function makeConnector(cfg) {
        var ws = null, reconnectTimer = null, reconnectDelay = 5000;
        var lastMessage = 0, staleTimer = null, pingTimer = null;
        var MAX_DELAY = 60000;

        function scheduleReconnect() {
            if (reconnectTimer) return;
            reconnectTimer = setTimeout(function () {
                reconnectTimer = null;
                init();
                reconnectDelay = Math.min(reconnectDelay * 2, MAX_DELAY);
            }, reconnectDelay);
        }

        function clearTimers() {
            if (staleTimer) { clearInterval(staleTimer); staleTimer = null; }
            if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
        }

        function init() {
            if (ws && ws.readyState <= 1) return;   // already open/connecting

            try {
                ws = new WebSocket(cfg.url);
            } catch (e) {
                scheduleReconnect();
                return;
            }

            ws.onopen = function () {
                reconnectDelay = 5000;
                lastMessage = Date.now();
                if (typeof CarracConn !== 'undefined') CarracConn.report(cfg.provider, 'connected');

                cfg.subscribeMessages().forEach(function (m) { ws.send(m); });

                clearTimers();
                staleTimer = setInterval(function () {
                    if (!ws || ws.readyState !== WebSocket.OPEN) return;
                    if (Date.now() - lastMessage > cfg.staleMs) {
                        try { ws.close(); } catch (e) {}
                        if (typeof CarracConn !== 'undefined') CarracConn.report(cfg.provider, 'reconnecting');
                        // onclose triggers scheduleReconnect()
                    }
                }, Math.max(15000, cfg.staleMs / 2));
                if (cfg.pingMessage) {
                    pingTimer = setInterval(function () {
                        if (ws && ws.readyState === WebSocket.OPEN) ws.send(cfg.pingMessage());
                    }, 20000);
                }
            };

            ws.onmessage = function (event) {
                lastMessage = Date.now();
                if (typeof CarracConn !== 'undefined') CarracConn.heartbeat(cfg.provider);
                try {
                    var tick = cfg.parse(JSON.parse(event.data));
                    if (tick && cfg.symbols.indexOf(tick.symbol) !== -1) {
                        applyTableTick(tick.symbol, tick.price);
                    }
                } catch (e) {}
            };

            ws.onclose = function () {
                clearTimers();
                if (typeof CarracConn !== 'undefined') CarracConn.report(cfg.provider, 'reconnecting');
                scheduleReconnect();
            };

            ws.onerror = function () {
                if (typeof CarracConn !== 'undefined') CarracConn.report(cfg.provider, 'reconnecting');
                // onclose follows
            };
        }

        // Intentional teardown for the tab-return reset path: null the handlers
        // FIRST so the async onclose can't report a stale 'reconnecting' or arm
        // a ghost reconnect timer.
        function reset() {
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
            clearTimers();
            if (ws) {
                ws.onclose = null;
                ws.onerror = null;
                try { ws.close(); } catch (e) {}
                ws = null;
            }
            reconnectDelay = 5000;
            init();
        }

        return { init: init, reset: reset };
    }

    var _gate = makeConnector({
        provider: 'gate',
        url: 'wss://api.gateio.ws/ws/v4/',
        symbols: ['AKT', 'DEEP', 'ARC', 'CHEX', 'DSYNC', 'KIP', 'MLC', 'PIPPIN', 'PRIME'],
        staleMs: 60000,
        subscribeMessages: function () {
            return ['AKT_USDT', 'DEEP_USDT', 'ARC_USDT', 'CHEX_USDT', 'DSYNC_USDT', 'KIP_USDT', 'MLC_USDT', 'PIPPIN_USDT', 'PRIME_USDT'].map(function (pair) {
                return JSON.stringify({
                    time: Math.floor(Date.now() / 1000),
                    channel: 'spot.candlesticks',
                    event: 'subscribe',
                    payload: ['1m', pair]
                });
            });
        },
        // Low-volume pairs can go quiet between trades — app-level ping keeps
        // the stale-check honest (pong counts as a message).
        pingMessage: function () {
            return JSON.stringify({ time: Math.floor(Date.now() / 1000), channel: 'spot.ping' });
        },
        parse: parseGateTickMessage
    });

    var _kraken = makeConnector({
        provider: 'kraken',
        url: 'wss://ws.kraken.com/v2',
        symbols: ['XMR'],
        staleMs: 45000,   // Kraken v2 heartbeats ~1/s while subscribed
        subscribeMessages: function () {
            return [JSON.stringify({ method: 'subscribe', params: { channel: 'ticker', symbol: ['XMR/USD'] } })];
        },
        pingMessage: null,
        parse: parseKrakenTickMessage
    });

    // Bybit v5 public spot, CC only. Same factory as
    // gate/kraken above — makeConnector's cfg.pingMessage already sends on a
    // 20s interval (see ws.onopen), which matches the spec's "ping every
    // 20s" requirement with no factory changes needed.
    var _bybit = makeConnector({
        provider: 'bybit',
        url: 'wss://stream.bybit.com/v5/public/spot',
        symbols: ['CC'],
        staleMs: 60000,
        subscribeMessages: function () {
            return [JSON.stringify({ op: 'subscribe', args: ['kline.1.CCUSDT'] })];
        },
        pingMessage: function () {
            return JSON.stringify({ op: 'ping' });
        },
        parse: parseBybitTickMessage
    });

    // Idempotent — safe to call from page load, the WS supervisor interval,
    // and the online event. Dead sockets reconnect; live ones are untouched.
    window.initTableTickers = function () {
        _gate.init();
        _kraken.init();
        _bybit.init();
    };

    // Force-fresh sockets on tab-return (stale-return path in stale-tab-recovery.js) —
    // handler-nulling close + synchronous re-init, no parkable timers.
    window.resetTableTickers = function () {
        _gate.reset();
        _kraken.reset();
        _bybit.reset();
    };
})();
