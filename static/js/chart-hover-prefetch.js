/* Hover-intent chart prefetch. Classic script, global scope — reads
   klineChartSymbol and klineChartTimeframe (app-core.js), klineCacheGet and
   preloadKlineData (chart-cache.js) and
   showChartTooltip/hideChartTooltip from chart-preview.js, all resolved at
   call time so script order does not matter beyond "after klinecharts". */
// === hover-intent chart prefetch (desktop only) ================
// Hovering a row for ~120ms telegraphs an upcoming click. Warm the client
// chart cache for (symbol, current klineChartTimeframe) during that window so
// the click's loadKlineChart call finds a cache hit and renders instantly
// instead of round-tripping /api/chart. Reuses preloadKlineData — the same
// single-symbol fetch+cache-store helper preloadAllTimeframes/
// preloadAdjacentAssets already call (~2614 above) — so the cache key + query
// shape (fmt=col, days, mode) exactly match what a real click looks up, and
// its existing guards (skip if already cached, skip sub-hourly for non-crypto)
// apply here for free. matchMedia is evaluated ONCE at load, not per hover.
var _hoverPrefetchDesktop = (typeof window.matchMedia === 'function') &&
    window.matchMedia('(hover: hover) and (pointer: fine)').matches;
var _hoverPrefetchTimer = null;
var _hoverPrefetchInFlight = false;  // at most one hover-prefetch in flight globally
function _armHoverPrefetch(symbol) {
    if (!_hoverPrefetchDesktop || !symbol) return;
    if (symbol === klineChartSymbol) return;      // already the open chart
    if (_hoverPrefetchInFlight) return;            // one in flight globally — skip
    var tf = klineChartTimeframe;
    if (klineCacheGet(symbol + '_' + tf)) return;  // already warm
    _hoverPrefetchTimer = setTimeout(function() {
        _hoverPrefetchTimer = null;
        _hoverPrefetchInFlight = true;
        if (typeof window.carracLog === 'function') window.carracLog('prefetch', 'hover ' + symbol + ' ' + tf);
        preloadKlineData(symbol, tf, function() { _hoverPrefetchInFlight = false; });
    }, 120);
}
function _cancelHoverPrefetch() {
    if (_hoverPrefetchTimer) { clearTimeout(_hoverPrefetchTimer); _hoverPrefetchTimer = null; }
}

// Chart preview hover events
document.addEventListener('mouseenter', function(e) {
    if (!e.target || !e.target.closest) return;
    var target = e.target.closest('[data-action="chart-preview"]');
    if (target) {
        var symbol = target.getAttribute('data-symbol');
        if (symbol) { showChartTooltip(symbol, e); _armHoverPrefetch(symbol); }
    }
}, true);

document.addEventListener('mouseleave', function(e) {
    if (!e.target || !e.target.closest) return;
    var target = e.target.closest('[data-action="chart-preview"]');
    if (target) {
        hideChartTooltip();
        _cancelHoverPrefetch();
    }
}, true);
