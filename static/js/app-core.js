// Chart app foundation: error reporting, the lazy-module loader and the
// cross-file state (asset list, chart, drawing and layout globals) that the
// other app files read. First of the app files; needs watchlist.js
// (loadWatchlistSymbols) loaded before it.

// Thousands-separated price, precision scaled to its magnitude.
function _fmtPriceSmart(p) {
    var abs = Math.abs(p);
    var s = abs >= 1000 ? p.toFixed(2) : (abs >= 1 ? p.toFixed(2) : (abs >= 0.01 ? p.toFixed(4) : p.toFixed(6)));
    var parts = s.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
}

// error_shown — a lightweight helper so callers that surface a
// user-visible failure (fetch error toast, form validation) can annotate
// the PostHog timeline with what went wrong and where.
// A 30s same-context dedupe so a retry/error loop (e.g. a stuck chart
// fetch) can't flood PostHog with repeat events — module-scoped, not
// per-caller, so it dedupes across all callers sharing a context string.
// It lives here, not in help-modal.js, because chart_standalone.html does
// not load help-modal.js and the chart_load reports would silently vanish.
var _cvxTrackErrorLastSeen = Object.create(null);
function cvxTrackError(context, detail) {
    try {
        var ctx = context || 'unknown';
        var now = Date.now();
        if (_cvxTrackErrorLastSeen[ctx] && (now - _cvxTrackErrorLastSeen[ctx]) < 30000) return;
        _cvxTrackErrorLastSeen[ctx] = now;
        if (typeof posthog !== 'undefined' && posthog.capture) {
            posthog.capture('error_shown', {
                context: ctx,
                detail: typeof detail === 'string' ? detail.slice(0, 200) : null
            });
        }
    } catch(e) {}
}
window.cvxTrackError = cvxTrackError;

// ─── Lazy-module loader ───────────────────────────────────
// Injects a <script> tag once and resolves on load. Safe with global-script pattern.
// Cache-bust token for dynamically-injected scripts. Template <script> tags use
// ?v={{ cache_version(...) }} (per-file md5); lazily-loaded modules can't, so they
// bust on the app version instead (release checklist always bumps APP_VERSION).
// Without this, CF edge-caches the bare /static/js/<module>.js (immutable, 1yr) and
// a deploy's changes to a lazy module never reach users until a manual CF purge.
var _assetVersion;
function _assetV() {
    if (_assetVersion === undefined) {
        var m = document.querySelector('meta[name="app-version"]');
        _assetVersion = (m && m.content) ? m.content : '';
    }
    return _assetVersion;
}
// `opts.integrity`/`opts.crossOrigin` are for a pinned third-party CDN URL
// (echarts, see ensurePortfolioPanel below) — never cache-busted with the
// app-version query param below, since that would invalidate the pinned hash.
function _loadScript(src, opts) {
    return new Promise(function(resolve, reject) {
        opts = opts || {};
        if (!opts.integrity) {
            var v = _assetV();
            if (v) src += (src.indexOf('?') >= 0 ? '&' : '?') + 'v=' + encodeURIComponent(v);
            // `bust`: a retry after a load failure — a different URL so neither
            // the edge cache nor the browser/SW cache can replay the bad copy.
            if (opts.bust) src += '&r=' + Date.now();
        }
        var existing = document.querySelector('script[src="' + src + '"]');
        if (existing) { resolve(); return; }
        var s = document.createElement('script');
        s.src = src;
        if (opts.integrity) s.integrity = opts.integrity;
        if (opts.crossOrigin) s.crossOrigin = opts.crossOrigin;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

// Lazy same-origin module: one automatic retry on a cache-busting URL.
// Error reports (Chrome/Windows and iPhone Safari) showed kline-drawings.js + carrac-indicator-pack.js fire onerror on a
// page load minutes after the same files had loaded fine — a cache (CF edge,
// SW, or browser) replayed a bad copy. Both files are immutable-cached under
// ?v=<app version>, so the only way past a poisoned entry is a different URL.
function _loadModule(src) {
    return _loadScript(src).catch(function() {
        return _loadScript(src, { bust: true });
    });
}

// Memoised promises — second call returns the same promise (already resolved).
// Pre-resolved if the script was already loaded eagerly (e.g. chart_standalone.html).
// A REJECTED promise is never memoised: `.catch` clears the slot so the next
// call tries again.
var _carracPackPromise = (typeof window.drawIndicatorPack === 'function') ? Promise.resolve() : null;
function ensureCarracPack() {
    if (!_carracPackPromise) {
        _carracPackPromise = _loadModule('/static/js/carrac-indicator-pack.js').catch(function(e) {
            _carracPackPromise = null;
            throw e;
        });
    }
    return _carracPackPromise;
}

var _klineDrawingsPromise = (typeof window.handleKlineToolClick === 'function') ? Promise.resolve() : null;
function ensureKLineDrawings() {
    if (!_klineDrawingsPromise) {
        _klineDrawingsPromise = _loadModule('/static/js/kline-drawings.js').catch(function(e) {
            _klineDrawingsPromise = null;
            throw e;
        });
    }
    return _klineDrawingsPromise;
}

var _aiPanelPromise = (typeof window.setAIPanelState === 'function') ? Promise.resolve() : null;
function ensureAIPanel() {
    if (!_aiPanelPromise) {
        _aiPanelPromise = _loadModule('/static/js/ai-panel.js').catch(function(e) {
            _aiPanelPromise = null;
            throw e;
        });
    }
    return _aiPanelPromise;
}

// Portfolio panel view — echarts CDN (same pinned build + SRI
// hash templates/portfolio.html uses) then portfolio-charts.js then
// portfolio.js, the same pair that drives the standalone /portfolio page.
// Loading portfolio.js this way (after DOMContentLoaded has already fired)
// is what keeps its `document.addEventListener('DOMContentLoaded', init)`
// a no-op here — setViewMode('portfolio') in split-layout.js calls
// Portfolio.init() itself once this promise resolves.
var _portfolioPanelPromise = (typeof window.Portfolio !== 'undefined') ? Promise.resolve() : null;
function ensurePortfolioPanel() {
    if (!_portfolioPanelPromise) {
        var echartsReady = (typeof window.echarts !== 'undefined') ? Promise.resolve() :
            _loadScript('https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js', {
                integrity: 'sha384-pPi0zxBAoDu6+JXW/C68UZLvBUUtU+7zonhif43rqj7pxsGyqyqzcian2Rj37Rss',
                crossOrigin: 'anonymous'
            });
        _portfolioPanelPromise = echartsReady
            .then(function() { return _loadScript('/static/js/portfolio-charts.js'); })
            .then(function() { return _loadScript('/static/js/portfolio.js'); });
    }
    return _portfolioPanelPromise;
}
// ─────────────────────────────────────────────────────────────────────────────

// Browser detection
var _isFirefox = navigator.userAgent.indexOf('Firefox') !== -1;

// State
var allAssets = {};
// Watchlist (flat ordered list)
var watchlistSymbols = loadWatchlistSymbols();

// Chart preview state
var chartTooltip = null;
var chartInstance = null;
var chartDataCache = {};  // Cache chart data by symbol
var chartCacheTTL = 30 * 60 * 1000;  // 30 minutes
var chartCacheMaxSize = 50;  // Max cached charts to prevent memory leak
var chartHoverTimeout = null;
var chartHoverDelay = 300;  // ms before showing chart (debounce)
var chartAbortController = null;  // For cancelling in-flight requests
var CHART_FETCH_TIMEOUT_MS = 30000;  // Abort a stalled /api/chart fetch so the loading overlay can't hang forever (see _loadKlineChartImpl)
var ASSETS_FETCH_TIMEOUT_MS = 30000;  // Per-attempt abort for /api/assets so the asset-list spinner can't hang (loadData / manualRefresh); 202-retry waits happen between attempts, not within one fetch
var drawings = [];  // Active drawings on chart
var priceAlerts = loadPriceAlerts();  // Load alerts from localStorage

// Scenario System State
var scenarioList = [];           // Active (non-archived) scenarios for current asset/TF
var scenarioOverlayIds = [];     // IDs of scenario overlays on chart
var scenariosVisible = true;     // Whether scenario overlays are shown
var activeScenarioIndex = -1;    // Currently highlighted scenario (-1 = none)
var _sKeyDown = false;           // S key held state
var _sKeyTimer = null;           // S key tap detection timer
var _sArrowUsed = false;         // Whether arrow was used during S hold
var drawerOpen = false;          // Bottom drawer expanded state
var drawerActiveTab = 'position';  // Active drawer tab: 'position', 'alerts', 'scanner', 'stream'

// Local price alerts — kept in localStorage and checked by screener-table.js
// after every asset-list refresh.
function loadPriceAlerts() {
    try {
        var stored = localStorage.getItem('priceAlerts');
        return stored ? JSON.parse(stored) : {};
    } catch (e) {
        console.error('Failed to load price alerts:', e);
        return {};
    }
}

function savePriceAlerts() {
    try {
        localStorage.setItem('priceAlerts', JSON.stringify(priceAlerts));
    } catch (e) {
        console.error('Failed to save price alerts:', e);
    }
}

function checkPriceAlerts() {
    Object.keys(priceAlerts).forEach(function(symbol) {
        var asset = allAssets[symbol];
        if (!asset || !asset.latest_price) return;
        var currentPrice = asset.latest_price;

        priceAlerts[symbol].forEach(function(alert) {
            if (alert.triggered) return;

            var shouldTrigger = (alert.direction === 'above' && currentPrice >= alert.price) ||
                               (alert.direction === 'below' && currentPrice <= alert.price);

            if (shouldTrigger) {
                alert.triggered = true;
                savePriceAlerts();
                showAlertNotification(symbol, alert, currentPrice);
            }
        });
    });
}

function showAlertNotification(symbol, alert, currentPrice) {
    // Browser notification
    if (Notification && Notification.permission === 'granted') {
        new Notification('Price Alert: ' + symbol, {
            body: symbol + ' has reached ' + formatPriceSimple(currentPrice) +
                  ' (' + alert.direction + ' ' + formatPriceSimple(alert.price) + ')',
            icon: '/static/favicon.svg'
        });
    }
    // Visual notification
    var notif = document.createElement('div');
    notif.className = 'price-alert-notification';
    notif.innerHTML = '<strong>' + escapeHtml(symbol) + '</strong> ' + escapeHtml(alert.direction) + ' ' +
                      formatPriceSimple(alert.price) + ' (now: ' + formatPriceSimple(currentPrice) + ')';
    document.body.appendChild(notif);
    setTimeout(function() { notif.classList.add('visible'); }, 10);
    setTimeout(function() {
        notif.classList.remove('visible');
        setTimeout(function() { notif.remove(); }, 300);
    }, 5000);
}

// Request notification permission
function requestNotificationPermission() {
    // iOS Safari outside an installed PWA has no Notification global at all —
    // a bare reference throws ReferenceError inside window.onload and kills
    // loadData/WebSocket/split-layout init.
    if (typeof Notification !== 'undefined' && Notification && Notification.permission === 'default') {
        Notification.requestPermission();
    }
}

// Timeframe mode state
var timeframeMode = localStorage.getItem('timeframeMode') || 'htf';
var LTF_REFRESH_MS = 15 * 60 * 1000;  // 15 minutes for LTF
var HTF_REFRESH_MS = 30 * 60 * 1000;  // 30 minutes for HTF
var refreshIntervalId = null;

// Chart Modal for fullscreen view
var chartModal = null;
var chartModalInstance = null;

// Chart modal (KLineChart) state

var klineChart = null;
var klineChartSymbol = null;
var klineChartTimeframe = '1D';
var klineModal = null;

var klineModalSize = { width: null, height: null };
var klineIsFullscreen = true;

// Split layout state
var splitLayoutActive = false;
var chartPanelReady = false;
var activeTab = 'screener';

// Drawing defaults, keyboard handler refs and the current selection
var klineDrawingsHidden = false;  // Master drawing visibility toggle
var klineDrawingColor = '#3fb950';  // Default drawing color
var klineDrawingWidth = 1;  // Default line thickness
var klineKeydownHandler = null;  // Store handler reference for cleanup
var klineKeyupHandler = null;
var klineDrawingColors = ['#3fb950', '#26a69a', '#ef5350', '#f59e0b', '#8b5cf6', '#ec4899', '#ffffff'];
var klineDrawingWidths = [1, 2, 3, 4];  // Line thickness options
var klineSelectedOverlayId = null;  // Currently selected overlay for keyboard delete

function isCryptoAsset(symbol) {
    // Check allAssets type first (covers ALL crypto including BINANCE_UNAVAILABLE)
    if (typeof allAssets !== 'undefined' && allAssets[symbol] && allAssets[symbol].type) {
        return allAssets[symbol].type === 'crypto' || allAssets[symbol].type === 'crypto_metric';
    }
    // Fallback to binanceSymbolMap if allAssets not loaded yet
    if (typeof binanceSymbolMap !== 'undefined') {
        var cryptoValues = [];
        for (var k in binanceSymbolMap) { cryptoValues.push(binanceSymbolMap[k]); }
        if (cryptoValues.indexOf(symbol) >= 0) return true;
    }
    return symbol === 'TOTAL' || symbol === 'BTC.D';
}
