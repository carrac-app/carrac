// Chart data cache and background preloading: the in-memory + sessionStorage
// kline cache, the derivatives/quant query params a chart fetch carries, and
// the staggered preload of other timeframes and neighbouring assets.
// Needs app-core.js and indicator-prefs.js loaded first.

var klineDataCache = {};  // In-memory cache: { "BTC_1D": { data, timestamp }, ... }
var KLINE_CACHE_TTL = 15 * 60 * 1000;  // 15 min cache TTL
var KLINE_CACHE_VERSION = 3;  // Bump to invalidate all cached chart data
var _tfPreloadPending = 0;
var _tfPreloadTimer = null;
var _tfPreloadStartMs = 0;
var _tfPreloadShownDone = {};  // { 'BTC': true } — don't re-show per symbol

function _showTfStatus(msg, isDone) {
    var el = document.getElementById('tf-preload-status');
    if (!el) return;
    if (_tfPreloadTimer) { clearTimeout(_tfPreloadTimer); _tfPreloadTimer = null; }
    el.textContent = msg;
    el.style.opacity = '1';
    if (isDone) {
        _tfPreloadTimer = setTimeout(function() {
            el.style.opacity = '0';
            setTimeout(function() { el.textContent = ''; el.style.opacity = '1'; }, 400);
        }, 3000);
    }
}

// === Persistent kline cache (survives F5) ===
function klineCacheSave(cacheKey, data) {
    klineDataCache[cacheKey] = { data: data, timestamp: Date.now() };
    try {
        sessionStorage.setItem('kc_' + cacheKey, JSON.stringify({ d: data, t: Date.now(), v: KLINE_CACHE_VERSION }));
    } catch (e) {
        // Storage full — evict oldest entries
        var keys = [];
        for (var i = 0; i < sessionStorage.length; i++) {
            var k = sessionStorage.key(i);
            if (k && k.indexOf('kc_') === 0) keys.push(k);
        }
        keys.sort(); // evict first few alphabetically
        for (var j = 0; j < Math.min(3, keys.length); j++) sessionStorage.removeItem(keys[j]);
        try { sessionStorage.setItem('kc_' + cacheKey, JSON.stringify({ d: data, t: Date.now(), v: KLINE_CACHE_VERSION })); } catch (e2) {}
    }
}
function klineCacheGet(cacheKey) {
    // In-memory first
    var mem = klineDataCache[cacheKey];
    if (mem && (Date.now() - mem.timestamp < KLINE_CACHE_TTL)) return mem.data;
    // Fall back to sessionStorage
    try {
        var raw = sessionStorage.getItem('kc_' + cacheKey);
        if (raw) {
            var parsed = JSON.parse(raw);
            if (parsed && parsed.d && (Date.now() - parsed.t < KLINE_CACHE_TTL) && parsed.v === KLINE_CACHE_VERSION) {
                klineDataCache[cacheKey] = { data: parsed.d, timestamp: parsed.t };
                return parsed.d;
            }
        }
    } catch (e) {}
    return null;
}

// Build query string for active derivatives indicators (OI, CVD)
function _derivativesParam() {
    if (!window._isPremium) return '';
    var d = [];
    if (klineActiveIndicators.OI || klineActiveIndicators.DERIV_COMBINED) d.push('oi');
    if (klineActiveIndicators.CVD_USDT || klineActiveIndicators.DERIV_COMBINED) d.push('cvd_usdt');
    if (klineActiveIndicators.CVD_COIN || klineActiveIndicators.DERIV_COMBINED) d.push('cvd_coin');
    // Deduplicate
    d = d.filter(function(v, i, a) { return a.indexOf(v) === i; });
    return d.length > 0 ? '&derivatives=' + d.join(',') : '';
}

function _quantParam() {
    var macroInds = IndicatorRegistry.getByCategory('macro');
    var series = [];
    for (var k in macroInds) {
        if (klineActiveIndicators[k] && macroInds[k].quantSeries) {
            series.push(macroInds[k].quantSeries);
        }
    }
    return series.length > 0 ? '&quant=' + series.join(',') : '';
}

// === Background preloader ===
var _preloadTimeoutIds = [];   // Track staggered preload timeouts for cancellation
var _preloadAbort = null;      // AbortController for in-flight preload fetches

// Eager-preload scope: FULL. Each preloaded TF is a full ~49-indicator pass
// over 2500 bars, so on a CPU-bound host flip BOTH to false: sub-hourly frames and
// neighbour assets then load on demand (one ~1-2s cold load on first open).
var EAGER_PRELOAD_SUBHOURLY = true;    // 5m / 15m / 30m
var EAGER_PRELOAD_ADJACENT  = true;    // ±1 neighbour assets

function _cancelPendingPreloads() {
    // Cancel all scheduled-but-not-yet-fired preload timeouts
    _preloadTimeoutIds.forEach(function(id) { clearTimeout(id); });
    _preloadTimeoutIds = [];
    // Abort in-flight preload fetches
    if (_preloadAbort) { try { _preloadAbort.abort(); } catch(e) {} }
    _preloadAbort = typeof AbortController !== 'undefined' ? new AbortController() : null;
}

function preloadKlineData(symbol, tf, onDone) {
    if (!symbol) { if (onDone) onDone(); return; }
    // Skip sub-hourly for non-crypto
    if (['1m','3m','5m','15m','30m'].indexOf(tf) >= 0 && !isCryptoAsset(symbol)) { if (onDone) onDone(); return; }
    var cacheKey = symbol + '_' + tf;
    if (klineCacheGet(cacheKey)) { if (onDone) onDone(); return; } // already cached
    var ltfSet = ['1m','3m','5m','15m','30m','1h','4h'];
    var mode = ltfSet.indexOf(tf) >= 0 ? 'ltf' : 'htf';
    var maxBars = (mode === 'ltf') ? 2500 : 1500;
    // Nobody is waiting on a preload, so the server logs a cold
    // build here as `preload`, not `miss` — it stays out of the blocking-miss
    // share and the warm-tier ledger. Every 30 min this fires for every TF of
    // the open symbol from every open tab; untagged, it was 37% of the ledger.
    var fetchOpts = { credentials: 'same-origin', headers: { 'X-Carrac-Preload': '1' } };
    if (_preloadAbort) fetchOpts.signal = _preloadAbort.signal;
    fetch('/api/chart/' + encodeURIComponent(symbol) + '?days=' + maxBars + '&mode=' + mode + '&timeframe=' + encodeURIComponent(tf) + '&fmt=col', fetchOpts
    ).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.ohlcv) { klineCacheSave(cacheKey, d); }
    // SILENCE-OK: this is a speculative background preload — the cacheGet
    // check above already confirmed nothing is cached yet, and nobody is
    // waiting on this promise. If it fails, the real fetch happens (and
    // retries normally, with its own error handling) the moment the symbol
    // is actually opened.
    }).catch(function() {}).finally(function() { if (onDone) onDone(); });
}

function preloadAllTimeframes(symbol, currentTf) {
    // Cancel any pending preloads from a previous asset switch
    _cancelPendingPreloads();

    var htfTfs = ['1D', '3D', '1W', '1M'];
    var ltfTfs = isCryptoAsset(symbol)
        ? (EAGER_PRELOAD_SUBHOURLY ? ['5m', '15m', '30m', '1h', '4h'] : ['1h', '4h'])
        : ['1h', '4h'];
    // Free-tier: only preload 4H+
    if (!window._isPremium) ltfTfs = ['4h'];
    var all = htfTfs.concat(ltfTfs);
    // Sort by distance from current TF (nearest first)
    var tfOrder = ['1m','3m','5m','15m','30m','1h','4h','1D','3D','1W','1M'];
    var curIdx = tfOrder.indexOf(currentTf);
    all.sort(function(a, b) {
        return Math.abs(tfOrder.indexOf(a) - curIdx) - Math.abs(tfOrder.indexOf(b) - curIdx);
    });
    // Filter to TFs that actually need fetching
    var toLoad = all.filter(function(tf) {
        if (tf === currentTf) return false;
        if (['1m','3m','5m','15m','30m'].indexOf(tf) >= 0 && !isCryptoAsset(symbol)) return false;
        return !klineCacheGet(symbol + '_' + tf);
    });
    if (toLoad.length === 0) {
        // Only show "all cached" once per symbol — suppress on TF switch
        if (!_tfPreloadShownDone[symbol]) {
            _tfPreloadShownDone[symbol] = true;
            _showTfStatus('All timeframes cached', true);
        }
        return;
    }
    _tfPreloadPending = toLoad.length;
    _tfPreloadStartMs = Date.now();
    // Run preloads ONE AT A TIME, not 200ms-staggered parallel. On a 2-vCPU box,
    // overlapping cold computes (Binance fetch + indicators) saturate the server
    // and starve the chart the user is actually waiting on. _myAbort lets a newer
    // asset switch stop this chain mid-flight (it's the controller set by the
    // _cancelPendingPreloads() call at the top of this function).
    var _myAbort = _preloadAbort;
    var _pi = 0;
    var _runNextPreload = function() {
        if (_myAbort !== _preloadAbort || _pi >= toLoad.length) return;
        var tf = toLoad[_pi++];
        var elapsed = ((Date.now() - _tfPreloadStartMs) / 1000).toFixed(1);
        _showTfStatus('Loading ' + tf + ' (' + elapsed + 's)');
        preloadKlineData(symbol, tf, function() {
            if (_myAbort !== _preloadAbort) return;
            _tfPreloadPending--;
            if (_tfPreloadPending <= 0) {
                var total = ((Date.now() - _tfPreloadStartMs) / 1000).toFixed(1);
                _tfPreloadShownDone[symbol] = true;
                _showTfStatus('All timeframes cached (' + total + 's)', true);
            }
            _runNextPreload();
        });
    };
    // Start after the visible chart has had a moment to render + paint.
    var _tid = setTimeout(_runNextPreload, 500);
    _preloadTimeoutIds.push(_tid);
}

function preloadAdjacentAssets(currentSymbol, currentTf) {
    if (!EAGER_PRELOAD_ADJACENT) return;   // off on the 2-vCPU box — see flags above
    var list = getOrderedAssetList();
    if (list.length === 0) return;
    var idx = list.indexOf(currentSymbol);
    if (idx < 0) return;
    // Preload ±1 neighbor only (was ±2). Adjacent-asset prewarm is the most
    // speculative work we do — keep it minimal and sequential so it never
    // competes with the user's real chart requests on a 2-vCPU box.
    var neighbors = [];
    for (var d = -1; d <= 1; d++) {
        if (d === 0) continue;
        var ni = (idx + d + list.length) % list.length;
        neighbors.push(list[ni]);
    }
    var _myAbortN = _preloadAbort;
    var _runNextNeighbor = function(i) {
        if (_myAbortN !== _preloadAbort || i >= neighbors.length) return;
        preloadKlineData(neighbors[i], currentTf, function() { _runNextNeighbor(i + 1); });
    };
    var _ntid = setTimeout(function() { _runNextNeighbor(0); }, 3000);
    _preloadTimeoutIds.push(_ntid);
}
