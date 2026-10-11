/* Liquidation Heatmap — native KLineChart indicator.
 *
 * Fetches the modeled liquidation-cluster TIME x PRICE grid from
 * /api/liq-heatmap for the current symbol (a *series* of compute cycles,
 * not just the latest one — see carrac/liq_heatmap.py) and paints it as a
 * viridis-gradient heat layer across the price pane: brighter/yellow =
 * higher modeled concentration of inferred long+short liquidation intensity,
 * dark purple/transparent = little inferred exposure. Long vs short stay
 * split in the API payload; this v1 combines them into one gradient for
 * rendering (Coinglass-style single heat layer).
 *
 * Non-advice: these are MODELED estimates from our own OI/price/long-short
 * history (see the endpoint + carrac/liq_heatmap.py docstring), never
 * CoinGlass and never observed order-book liquidity — the on-chart label
 * and the registry description both say so explicitly.
 *
 * Rendering strategy (perf-critical — Gold Standard chart-fluidity rules
 * apply to any chart-touching change): a naive per-cell-per-frame canvas
 * fill (buckets x candles) would redraw thousands of rects every single
 * animation frame during pan/zoom. Instead:
 *   1. On data change, rebuild ONE offscreen bitmap (width = loaded candle
 *      count, height = LIQ_ROWS fixed rows spanning the union of every
 *      cycle's own +/-band) by walking cycles forward in step-function
 *      fashion (each candle column takes the color of the most recent
 *      cycle at or before its timestamp).
 *   2. Every draw() frame is then a SINGLE ctx.drawImage() blit — O(1)
 *      regardless of zoom/pan/candle count, using a source rect resolved
 *      via a small binary search (candle timestamps are ascending) so the
 *      chart's own candle array growing between rebuilds (live ticks)
 *      never desyncs the column mapping.
 *   3. Log-scale correctness without a per-row fallback: the bitmap's row
 *      axis is pre-warped into LOG-price space at build time whenever log
 *      scale is on (same convention documented in confluence-zones.js —
 *      klinecharts' yAxis.convertToPixel expects log-space values when log
 *      scale is active). Since both the bitmap's row axis and the
 *      destination pixel axis are then linear transforms of the SAME
 *      log-price value, the single-blit fast path stays exactly correct in
 *      both modes — the bitmap just gets rebuilt once on a scale toggle
 *      (cheap, not per-frame).
 *
 * Registered on 'candle_pane' the same way confluence-zones.js/regime-ribbon
 * are — the only rendering mechanism this klinecharts version exposes for a
 * custom overlay; a custom indicator on a pane always draws AFTER that
 * pane's built-in candle series (confirmed by every existing example in this
 * codebase), so "background" here means "translucent viridis wash the
 * candles still show through", not literally behind the candle canvas layer.
 */

// ============================================================
// State
// ============================================================
var _liqData = null;             // {symbol, band_pct, n_buckets, paint_quantile, cycles:[...]}
var _liqSymbol = null;           // symbol the cached data is for
var _liqFetching = false;
var _liqLastFetchTs = 0;
var _liqRegistered = false;
var _liqIndicatorCreated = false;

var _liqCanvas = null;           // offscreen bitmap (not attached to DOM)
var _liqBuildTimestamps = null;  // candle timestamps (ms) at build time, parallel to canvas columns
var _liqOverallLowT = null;      // bitmap's row-axis low bound (log-transformed when built in log mode)
var _liqOverallHighT = null;
var _liqBuildIsLog = false;

var LIQ_ROWS = 200;              // fixed row resolution for the offscreen bitmap
var _LIQ_REFETCH_MS = 5 * 60 * 1000; // new cycles land server-side ~every 15 min; refetch opportunistically

// Coarse viridis anchor stops (matplotlib's default), linearly interpolated.
var _LIQ_VIRIDIS = [
    [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142],
    [38, 130, 142], [31, 158, 137], [53, 183, 121], [109, 205, 89], [253, 231, 37],
];

// ============================================================
// Colormap
// ============================================================
function _liqViridisRgb(t) {
    t = Math.max(0, Math.min(1, t));
    var n = _LIQ_VIRIDIS.length - 1;
    var pos = t * n;
    var i0 = Math.floor(pos);
    var i1 = Math.min(n, i0 + 1);
    var frac = pos - i0;
    var c0 = _LIQ_VIRIDIS[i0], c1 = _LIQ_VIRIDIS[i1];
    return [
        Math.round(c0[0] + (c1[0] - c0[0]) * frac),
        Math.round(c0[1] + (c1[1] - c0[1]) * frac),
        Math.round(c0[2] + (c1[2] - c0[2]) * frac),
    ];
}

function _liqColor(intensity, whiteBg) {
    // Bands are already threshold-filtered server-side (top ~15% only), so
    // every cell we paint is "significant" — color conveys relative
    // intensity, alpha stays fairly uniform. On a white background, skip
    // the darkest ~35% of the gradient (would otherwise read as dark
    // smudges) and cut alpha so it doesn't overpower candles/price text.
    var t = whiteBg ? (0.35 + intensity * 0.65) : intensity;
    var rgb = _liqViridisRgb(t);
    var alpha = whiteBg ? 0.5 : 0.8;
    return 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',' + alpha + ')';
}

// ============================================================
// Register native indicator at load (before chart exists)
// ============================================================
(function liqRegisterOnLoad() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'LIQ_HEATMAP_NATIVE',
            shortName: '',
            precision: 8,
            figures: [],
            calc: function(dataList) { return dataList.map(function() { return {}; }); },
            draw: function(params) {
                if (klineActiveIndicators && klineActiveIndicators.LIQ_HEATMAP) {
                    _liqDraw(params);
                }
                return true;
            },
        });
        _liqRegistered = true;
    } catch (e) {
        console.warn('[LiqHeatmap] registerIndicator failed:', e);
    }
})();

function liqHeatmapEnsureIndicator() {
    if (!klineChart || _liqIndicatorCreated || !_liqRegistered) return;
    try {
        klineChart.createIndicator('LIQ_HEATMAP_NATIVE', true, { id: 'candle_pane' });
        _liqIndicatorCreated = true;
    } catch (e) {}
}

// ============================================================
// Lifecycle
// ============================================================
function liqHeatmapStart() {
    if (!klineChart) return;
    liqHeatmapEnsureIndicator();
    _liqInvalidate();   // repaint whatever's already cached on the next frame
    _liqFetch(klineChartSymbol);
}

function liqHeatmapStop() {
    _liqData = null;
    _liqSymbol = null;
    _liqCanvas = null;
    _liqBuildTimestamps = null;
    if (klineChart && klineChart.resize) { try { klineChart.resize(); } catch (e) {} }
}

function liqHeatmapOnDataChange() {
    if (!(klineActiveIndicators && klineActiveIndicators.LIQ_HEATMAP)) return;
    // Drop the cached bitmap so the next draw rebuilds it against the freshest
    // candles (cheap, no network) — keeps the candle-index <-> timestamp
    // mapping correct across TF/symbol switches and live-tick candle growth
    // even between fetches. Same invalidate-then-rebuild-on-draw contract as
    // vpOnDataChange()/klOnDataChange(); the candle array is only reachable
    // from inside the draw callback, never as a global.
    _liqInvalidate();
    var symbolChanged = _liqSymbol !== klineChartSymbol;
    if (symbolChanged || (Date.now() - _liqLastFetchTs) > _LIQ_REFETCH_MS) {
        _liqFetch(klineChartSymbol);
    }
}

function liqHeatmapOnChartClose() {
    _liqData = null;
    _liqSymbol = null;
    _liqCanvas = null;
    _liqBuildTimestamps = null;
    _liqIndicatorCreated = false;
}

// ============================================================
// Fetch cycle series from the backend
// ============================================================
function _liqFetch(symbol) {
    if (!symbol || _liqFetching) return;
    _liqFetching = true;
    _liqLastFetchTs = Date.now();
    fetch('/api/liq-heatmap?symbol=' + encodeURIComponent(symbol), { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            _liqFetching = false;
            if (!data || data.symbol !== symbol) return;   // stale / 404 / cold — keep last-known-good
            _liqData = data;
            _liqSymbol = symbol;
            _liqInvalidate();
            if (klineChart && klineChart.resize) { try { klineChart.resize(); } catch (e) {} }
        })
        .catch(function() { _liqFetching = false; });
}

// ============================================================
// Offscreen bitmap build (data-change triggered, NOT per-frame)
// ============================================================
// Drop the cached bitmap; the next _liqDraw() frame rebuilds it from the
// candles klinecharts hands the draw callback. Mirrors _vpData/_klData = null.
function _liqInvalidate() {
    _liqCanvas = null;
    _liqBuildTimestamps = null;
}

function _liqRowFromT(priceT, lowT, highT) {
    if (highT <= lowT) return 0;
    var t = (highT - priceT) / (highT - lowT);   // row 0 = highest price (top)
    return Math.max(0, Math.min(LIQ_ROWS - 1, Math.round(t * LIQ_ROWS)));
}

// dataList comes from the draw callback (params.kLineDataList) -- the same
// source every other custom indicator here uses (volume-profile.js,
// key-levels.js). There is NO global `klineData`: chart-render.js and chart-loading.js keep it as a
// local, so a bare read from this file would throw a ReferenceError.
function _liqBuildOffscreen(dataList) {
    if (!dataList || !dataList.length || !_liqData || !_liqData.cycles || !_liqData.cycles.length) {
        _liqCanvas = null;
        return;
    }
    var isLog = !!(typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    var cycles = _liqData.cycles;   // ascending by ts (server-guaranteed)

    // Union of every cycle's own +/-band -- price recenters as spot moves
    // over the lookback window, so the bitmap's fixed row axis has to span
    // the full observed range, not any single cycle's band.
    var lowRaw = Infinity, highRaw = -Infinity;
    for (var c = 0; c < cycles.length; c++) {
        var cyc = cycles[c];
        if (cyc.band_low != null && cyc.band_low < lowRaw) lowRaw = cyc.band_low;
        if (cyc.band_high != null && cyc.band_high > highRaw) highRaw = cyc.band_high;
    }
    if (!isFinite(lowRaw) || !isFinite(highRaw) || highRaw <= lowRaw) { _liqCanvas = null; return; }

    var lowT = (isLog && lowRaw > 0) ? Math.log(lowRaw) : lowRaw;
    var highT = (isLog && highRaw > 0) ? Math.log(highRaw) : highRaw;

    var width = dataList.length;
    var canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = LIQ_ROWS;
    var ctx = canvas.getContext('2d');
    if (!ctx) { _liqCanvas = null; return; }
    ctx.clearRect(0, 0, width, LIQ_ROWS);

    var buildTs = new Array(width);
    for (var i = 0; i < width; i++) buildTs[i] = dataList[i].timestamp;

    var cycleTsMs = new Array(cycles.length);
    for (var k = 0; k < cycles.length; k++) cycleTsMs[k] = Date.parse(cycles[k].ts);

    var whiteBg = localStorage.getItem('whiteBackground') === '1';

    // Both buildTs and cycleTsMs are ascending -- one forward pointer walk
    // (step-function carry-forward: each column takes the most recent cycle
    // at or before its own timestamp).
    var cIdx = -1;
    for (var col = 0; col < width; col++) {
        var t = buildTs[col];
        while (cIdx + 1 < cycleTsMs.length && cycleTsMs[cIdx + 1] <= t) cIdx++;
        if (cIdx < 0) continue;   // before the first persisted cycle -- leave transparent
        var bands = cycles[cIdx].bands;
        if (!bands || !bands.length) continue;
        for (var b = 0; b < bands.length; b++) {
            var band = bands[b];
            var combined = Math.min(1, (band.intensity_long || 0) + (band.intensity_short || 0));
            if (combined <= 0) continue;
            var priceLoT = (isLog && band.price_low > 0) ? Math.log(band.price_low) : band.price_low;
            var priceHiT = (isLog && band.price_high > 0) ? Math.log(band.price_high) : band.price_high;
            var rowHi = _liqRowFromT(priceHiT, lowT, highT);   // higher price -> smaller row
            var rowLo = _liqRowFromT(priceLoT, lowT, highT);
            var top = Math.min(rowHi, rowLo);
            var h = Math.max(1, Math.abs(rowLo - rowHi));
            ctx.fillStyle = _liqColor(combined, whiteBg);
            ctx.fillRect(col, top, 1, h);
        }
    }

    _liqCanvas = canvas;
    _liqBuildTimestamps = buildTs;
    _liqOverallLowT = lowT;
    _liqOverallHighT = highT;
    _liqBuildIsLog = isLog;
}

// Largest index i such that buildTs[i] <= ts (0 if none qualify).
function _liqBinarySearchCol(ts) {
    var arr = _liqBuildTimestamps;
    if (!arr || !arr.length) return 0;
    var lo = 0, hi = arr.length - 1, ans = 0;
    while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        if (arr[mid] <= ts) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return ans;
}

// ============================================================
// Draw — one drawImage blit per frame (O(1) regardless of zoom/pan)
// ============================================================
function _liqDraw(params) {
    if (!klineChart) return;
    var ctx = params.ctx;
    var bounding = params.bounding;
    var xAxis = params.xAxis;
    var yAxis = params.yAxis;
    var visibleRange = params.visibleRange;
    if (!ctx || !bounding || !xAxis || !yAxis || !visibleRange) return;
    if (!bounding.width || !bounding.height) return;

    var dataList = params.kLineDataList || params.dataList;
    if (!dataList || !dataList.length) return;

    var isLog = !!(typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    // Rebuild when the bitmap is missing (invalidated by a data/symbol/TF
    // change or a fresh fetch) or when the row axis was baked in the OTHER
    // scale mode. Still once per change, never per frame: _liqBuildOffscreen()
    // sets _liqCanvas, so subsequent frames fall straight through to the blit.
    if (!_liqCanvas || isLog !== _liqBuildIsLog) {
        _liqBuildOffscreen(dataList);
    }
    if (!_liqCanvas) return;

    var fromIdx = Math.max(0, visibleRange.from);
    var toIdx = Math.min(dataList.length - 1, visibleRange.to - 1);
    if (toIdx < fromIdx) return;

    var fromTs = dataList[fromIdx].timestamp;
    var toTs = dataList[toIdx].timestamp;
    var srcFrom = _liqBinarySearchCol(fromTs);
    var srcTo = Math.min(_liqCanvas.width - 1, _liqBinarySearchCol(toTs));
    if (srcTo < srcFrom) srcTo = srcFrom;
    var srcW = Math.max(1, srcTo - srcFrom + 1);

    var barW = (params.barSpace && params.barSpace.bar) || 1;
    var destX0 = xAxis.convertToPixel(fromIdx) - barW / 2;
    var destX1 = xAxis.convertToPixel(toIdx) + barW / 2;
    var destW = Math.max(1, destX1 - destX0);

    var yTop = yAxis.convertToPixel(_liqOverallHighT);   // highest price -> smallest pixel y
    var yBot = yAxis.convertToPixel(_liqOverallLowT);
    if (yTop == null || yBot == null || !isFinite(yTop) || !isFinite(yBot)) return;
    var destY0 = Math.min(yTop, yBot);
    var destH = Math.max(1, Math.abs(yBot - yTop));

    try {
        ctx.save();
        ctx.imageSmoothingEnabled = false;   // crisp bucket edges, not a blurred gradient
        ctx.drawImage(_liqCanvas, srcFrom, 0, srcW, LIQ_ROWS, destX0, destY0, destW, destH);
        ctx.restore();
    } catch (e) {}

    // Non-advice label -- descriptive, not a signal.
    ctx.save();
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.fillStyle = 'rgba(190,190,200,0.7)';
    ctx.textAlign = 'left';
    ctx.fillText('Estimated liquidation clusters (model-based)', 6, bounding.height - 6);
    ctx.restore();
}
