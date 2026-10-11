/* Binance WebSocket — JSON subscribe approach with dynamic symbol support */
// ============================================================
// Binance WebSocket - Live Crypto Prices
// ============================================================

var binanceWS = null;
var binanceWSReconnectTimer = null;
var binanceWSReconnectDelay = 5000;
var binanceWSMaxDelay = 60000;
var _binanceSubscribeId = 1;
var _binanceDynamicSubscriptions = {};  // symbol -> true (tracks runtime additions)

// Track last-message timestamp for silent-death detection. WebSockets can die
// without firing onclose (mobile sleep, ISP drops, etc.); without this, the
// ticker bar would read stale allAssets[...].latest_price forever.
var _binanceWSLastMessage = 0;
var _binanceWSStaleCheckTimer = null;
var _BINANCE_WS_STALE_MS = 30000;

// Map Binance stream names (lowercase) to dashboard symbols
// ~100 streams (Binance allows 200+ per connection)
var binanceSymbolMap = {
    // Major coins
    'btcusdt': 'BTC', 'ethusdt': 'ETH', 'xrpusdt': 'XRP',
    'solusdt': 'SOL', 'dogeusdt': 'DOGE', 'adausdt': 'ADA',
    'avaxusdt': 'AVAX', 'dotusdt': 'DOT', 'linkusdt': 'LINK',
    'polusdt': 'POL', 'shibusdt': 'SHIB', 'ltcusdt': 'LTC',
    'uniusdt': 'UNI', 'atomusdt': 'ATOM', 'nearusdt': 'NEAR',
    'aptusdt': 'APT', 'bnbusdt': 'BNB', 'trxusdt': 'TRX',
    'xlmusdt': 'XLM', 'algousdt': 'ALGO', 'vetusdt': 'VET',
    'filusdt': 'FIL', 'aaveusdt': 'AAVE', 'eosusdt': 'EOS',
    'xtzusdt': 'XTZ', 'suiusdt': 'SUI',
    // Layer 2s
    'arbusdt': 'ARB', 'opusdt': 'OP', 'imxusdt': 'IMX',
    'strkusdt': 'STRK', 'mantausdt': 'MANTA',
    // AI & Computing
    'fetusdt': 'FET', 'renderusdt': 'RENDER', 'taousdt': 'TAO',
    'oceanusdt': 'OCEAN', 'arusdt': 'AR',
    // DeFi
    'crvusdt': 'CRV', 'snxusdt': 'SNX', 'compusdt': 'COMP',
    'ldousdt': 'LDO', 'mkrusdt': 'MKR', 'runeusdt': 'RUNE',
    'jupusdt': 'JUP', 'enausdt': 'ENA', 'pendleusdt': 'PENDLE',
    '1inchusdt': '1INCH', 'gmxusdt': 'GMX',
    // Gaming & Metaverse
    'galausdt': 'GALA', 'sandusdt': 'SAND', 'manausdt': 'MANA',
    'axsusdt': 'AXS', 'enjusdt': 'ENJ',
    // Infrastructure
    'icpusdt': 'ICP', 'injusdt': 'INJ', 'hbarusdt': 'HBAR',
    'flowusdt': 'FLOW', 'seiusdt': 'SEI',
    'tiausdt': 'TIA', 'stxusdt': 'STX', 'thetausdt': 'THETA',
    'qntusdt': 'QNT', 'egldusdt': 'EGLD',
    // Memecoins
    'pepeusdt': 'PEPE', 'bonkusdt': 'BONK', 'wifusdt': 'WIF',
    'flokiusdt': 'FLOKI', 'turbousdt': 'TURBO',
    // Other existing
    'tonusdt': 'TON', 'ondousdt': 'ONDO', 'pythusdt': 'PYTH',
    'jtousdt': 'JTO', 'wusdt': 'W', 'biousdt': 'BIO',
    'cetususdt': 'CETUS', 'cotiusdt': 'COTI', 'penguusdt': 'PENGU',
    'susdt': 'S',
    // Major additions
    'bchusdt': 'BCH', 'etcusdt': 'ETC', 'neousdt': 'NEO', 'zecusdt': 'ZEC',
    // DeFi (extended)
    'dydxusdt': 'DYDX', 'ensusdt': 'ENS', 'grtusdt': 'GRT',
    'sushiusdt': 'SUSHI', 'cakeusdt': 'CAKE', 'lqtyusdt': 'LQTY',
    'listausdt': 'LISTA', 'rdntusdt': 'RDNT', 'rsrusdt': 'RSR',
    // Gaming (extended)
    'apeusdt': 'APE', 'pixelusdt': 'PIXEL', 'portalusdt': 'PORTAL',
    'bigtimeusdt': 'BIGTIME', 'magicusdt': 'MAGIC', 'ilvusdt': 'ILV',
    // AI (extended)
    'aixbtusdt': 'AIXBT', 'wldusdt': 'WLD', 'arkmusdt': 'ARKM',
    // Infrastructure (extended)
    'jasmyusdt': 'JASMY', 'chzusdt': 'CHZ', 'roseusdt': 'ROSE',
    'cfxusdt': 'CFX', 'kavausdt': 'KAVA', 'osmousdt': 'OSMO',
    'lunausdt': 'LUNA', 'zrousdt': 'ZRO', 'sagausdt': 'SAGA',
    'dymusdt': 'DYM', 'ntrnusdt': 'NTRN', 'rayusdt': 'RAY',
    'achusdt': 'ACH', 'blurusdt': 'BLUR', 'ssvusdt': 'SSV',
    'ethfiusdt': 'ETHFI', 'moveusdt': 'MOVE',
    // New L1/L2
    'berausdt': 'BERA', 'kaitousdt': 'KAITO',
    // Memecoins (extended)
    'ordiusdt': 'ORDI', 'notusdt': 'NOT', 'dogsusdt': 'DOGS',
    'neirousdt': 'NEIRO', 'pnutusdt': 'PNUT', 'trumpusdt': 'TRUMP',
    'virtualusdt': 'VIRTUAL', 'animeusdt': 'ANIME',
    // Narrative
    'eigenusdt': 'EIGEN'
};

function initBinanceWebSocket() {
    if (binanceWS && binanceWS.readyState <= 1) return;  // Already open/connecting

    // Use base endpoint + JSON subscribe (allows dynamic subscribe/unsubscribe)
    var url = 'wss://data-stream.binance.vision/ws';

    try {
        binanceWS = new WebSocket(url);
    } catch (e) {
        console.warn('[BinanceWS] Failed:', e);
        scheduleBinanceReconnect();
        return;
    }

    binanceWS.onopen = function() {
        binanceWSReconnectDelay = 5000;
        _binanceWSLastMessage = Date.now();
        if (typeof CarracConn !== 'undefined') CarracConn.report('binance', 'connected');

        if (_binanceWSStaleCheckTimer) clearInterval(_binanceWSStaleCheckTimer);
        _binanceWSStaleCheckTimer = setInterval(function() {
            if (!binanceWS || binanceWS.readyState !== WebSocket.OPEN) return;
            if (Date.now() - _binanceWSLastMessage > _BINANCE_WS_STALE_MS) {
                console.warn('[BinanceWS] No message for ' + _BINANCE_WS_STALE_MS + 'ms — closing for reconnect');
                try { binanceWS.close(); } catch (e) {}
                if (typeof CarracConn !== 'undefined') CarracConn.report('binance', 'reconnecting');
                // onclose handler will trigger reconnect via scheduleBinanceReconnect()
            }
        }, Math.max(15000, _BINANCE_WS_STALE_MS / 2));

        // Subscribe to all curated streams
        var streams = Object.keys(binanceSymbolMap).map(function(s) { return s + '@miniTicker'; });
        _binanceSendSubscribe(streams);

        // Re-subscribe dynamic streams
        Object.keys(_binanceDynamicSubscriptions).forEach(function(sym) {
            var pair = sym.toLowerCase() + 'usdt';
            _binanceSendSubscribe([pair + '@miniTicker']);
        });
    };

    binanceWS.onmessage = function(event) {
        _binanceWSLastMessage = Date.now();
        if (typeof CarracConn !== 'undefined') CarracConn.heartbeat('binance');
        try {
            var msg = JSON.parse(event.data);

            // Handle subscribe/unsubscribe responses
            if (msg.id !== undefined) return;

            // Handle miniTicker data
            var data = msg.e === '24hrMiniTicker' ? msg : (msg.data || msg);
            if (data && data.s && data.c) {
                var binSymbol = data.s.toLowerCase();  // e.g. "btcusdt"
                var dashSymbol = binanceSymbolMap[binSymbol];
                if (!dashSymbol) return;

                var price = parseFloat(data.c);
                var asset = allAssets[dashSymbol];
                if (asset) {
                    asset.latest_price = price;

                    // Update price cell in the table
                    var rows = document.querySelectorAll('tr[data-symbol="' + dashSymbol + '"]');
                    rows.forEach(function(row) {
                        var priceCell = row.querySelector('.price');
                        if (priceCell) {
                            var _fmt = formatPrice(price, dashSymbol, 'crypto');
                            if (typeof CarracTick !== 'undefined') {
                                priceCell.classList.add('tk-soft');
                                CarracTick.paint(priceCell, _fmt);
                            } else {
                                priceCell.innerHTML = _fmt;
                                priceCell.classList.add('ws-flash');
                                setTimeout(function() { priceCell.classList.remove('ws-flash'); }, 600);
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

                    // Live-update KLineChart candle / stale-chart recovery —
                    // centralized in chart-live-tick.js.
                    if (typeof window.applyChartTick === 'function') {
                        window.applyChartTick(dashSymbol, price);
                    }
                }
            }
            // Throttled drawer position update (live % distance)
            if (!window._drawerPosTimer) {
                window._drawerPosTimer = setTimeout(function() {
                    if (typeof updateDrawerPosition === 'function') updateDrawerPosition();
                    window._drawerPosTimer = null;
                }, 5000);
            }
        } catch (e) {}
    };

    binanceWS.onclose = function() {
        if (_binanceWSStaleCheckTimer) { clearInterval(_binanceWSStaleCheckTimer); _binanceWSStaleCheckTimer = null; }
        if (typeof CarracConn !== 'undefined') CarracConn.report('binance', 'reconnecting');
        scheduleBinanceReconnect();
    };

    binanceWS.onerror = function() {
        if (typeof CarracConn !== 'undefined') CarracConn.report('binance', 'reconnecting');
    };
}

function _binanceSendSubscribe(streams) {
    if (!binanceWS || binanceWS.readyState !== WebSocket.OPEN) return;
    binanceWS.send(JSON.stringify({
        method: 'SUBSCRIBE',
        params: streams,
        id: _binanceSubscribeId++
    }));
}

/**
 * Subscribe to a dynamic (non-curated) Binance crypto symbol at runtime.
 * Called when user opens chart for a dynamic Binance-listed crypto.
 * @param {string} symbol - Dashboard symbol (e.g. 'RIVN' won't work, 'BONK2' would)
 */
function subscribeBinanceSymbol(symbol) {
    var pair = symbol.toLowerCase() + 'usdt';
    if (binanceSymbolMap[pair]) return;  // Already subscribed

    // Add to symbol map so onmessage handler picks it up
    binanceSymbolMap[pair] = symbol.toUpperCase();
    _binanceDynamicSubscriptions[symbol.toUpperCase()] = true;

    // Send subscribe
    _binanceSendSubscribe([pair + '@miniTicker']);
}

// Expose to global scope for chart load code
window.subscribeBinanceSymbol = subscribeBinanceSymbol;

function scheduleBinanceReconnect() {
    if (binanceWSReconnectTimer) return;
    binanceWSReconnectTimer = setTimeout(function() {
        binanceWSReconnectTimer = null;
        initBinanceWebSocket();
        binanceWSReconnectDelay = Math.min(binanceWSReconnectDelay * 2, binanceWSMaxDelay);
    }, binanceWSReconnectDelay);
}

