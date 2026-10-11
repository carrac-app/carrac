/* Fixed Range Volume Profile (FRVP) — standard KLineChart overlay
 *
 * Rebuilt as overlay 'frvp' (was: bespoke click-to-pick + raw-canvas draw,
 * immutable after creation, localStorage-only). Now:
 * - 2 points = bar-range anchors (time only — the vertical extent always
 *   derives from the profile data, like TradingView).
 * - Edge drag (capture-phase system in kline-drawings.js) re-anchors ONE
 *   boundary, the opposite edge stays pinned; the profile refetches
 *   (debounced) while a client-side approximation renders instantly.
 * - Body drag = native whole-overlay translate via the transparent hit rect.
 * - Persistence via the standard drawing pipeline (saveKlineDrawing →
 *   /api/drawings) — old localStorage ranges migrate once (frvpMigrateLegacy).
 * - Profile data (bins with real taker buy/sell) is fetched from
 *   /api/volume-profile and cached at runtime per overlay id. It is NEVER
 *   stored in extendData (that would sync ~200 bins to the backend on every
 *   edit). extendData holds settings only: { vpMode, vaPct, vpRows }.
 * - Volume modes: 'total' | 'updown' | 'delta'. Delta is REAL taker
 *   buy−sell per bin (TradingView's delta is a candle-direction proxy).
 * - Value Area % and display rows are client-side recomputes over the
 *   fetched bins — no refetch needed when they change.
 */

// ============================================================
// State
// ============================================================
var _frvpDataCache = {};   // overlayId -> {key, vp, meta, pendingKey, timer, approxKey, approxVp}
var _frvpIdCounter = 0;
var FRVP_FETCH_DEBOUNCE_MS = 200;
var FRVP_DEFAULT_SETTINGS = { vpMode: 'updown', vaPct: 70, vpRows: 100 };

// Data-space math memo (dense grid / display rows / value area) — see
// _frvpGetProfileMath below. overlayId -> {vpRef, paramsKey, grid, display,
// valueArea}. Bounded + FIFO-evicted: overlay deletion happens in
// kline-drawings.js (not this file), so we can't hook a delete event here —
// a hard cap is the guard against leaking entries for abandoned profiles.
var _frvpMathCache = new Map();
var FRVP_MATH_CACHE_MAX = 20;

// ============================================================
// Overlay registration (at load, like VP_NATIVE)
// ============================================================
(function frvpRegisterOnLoad() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerOverlay({
            name: 'frvp',
            totalStep: 3,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: _frvpCreateFigures,
            createYAxisFigures: _frvpCreateAxisFigures
        });
    } catch (e) {
        console.warn('[FRVP] registerOverlay failed:', e);
    }
})();

// ============================================================
// Settings / data resolution
// ============================================================
function _frvpSettings(overlay) {
    var ext = (overlay && overlay.extendData) || {};
    return {
        vpMode: ext.vpMode || FRVP_DEFAULT_SETTINGS.vpMode,
        vaPct: (ext.vaPct >= 50 && ext.vaPct <= 95) ? ext.vaPct : FRVP_DEFAULT_SETTINGS.vaPct,
        vpRows: (ext.vpRows >= 24 && ext.vpRows <= 200) ? ext.vpRows : FRVP_DEFAULT_SETTINGS.vpRows
    };
}

// Last REAL bar index — the dataList tail is padded with future `_phantom`
// bars (see the loader's _phantomMap in chart-render.js); Extend Right must anchor
// to the latest closed/live candle, never the phantom runway.
function _frvpLastRealDi(dl) {
    if (!dl || !dl.length) return null;
    var now = Date.now();
    var i = dl.length - 1;
    while (i > 0 && (dl[i].timestamp > now || dl[i]._phantom)) i--;
    return i;
}

function _frvpRangeTs(overlay) {
    var pts = overlay && overlay.points;
    if (!pts || pts.length < 2) return null;
    var ts0 = pts[0].timestamp, ts1 = pts[1].timestamp;
    // During drags points may momentarily carry only dataIndex
    if ((ts0 == null || ts1 == null) && klineChart) {
        try {
            var dl = klineChart.getDataList();
            if (ts0 == null && pts[0].dataIndex != null && dl[pts[0].dataIndex]) ts0 = dl[pts[0].dataIndex].timestamp;
            if (ts1 == null && pts[1].dataIndex != null && dl[pts[1].dataIndex]) ts1 = dl[pts[1].dataIndex].timestamp;
        } catch (e) {}
    }
    if (ts0 == null || ts1 == null) return null;
    // Extend Right: the effective END tracks the last REAL bar (it absorbs
    // new candles as they close); the stored anchors stay untouched, so
    // turning the flag off reverts to them. The fetch-cache key embeds this
    // end ts, so a candle close changes the key and the existing debounced
    // fetch in _frvpResolveData refetches automatically — no new machinery.
    if (overlay.extendData && overlay.extendData.extendRight && klineChart) {
        try {
            var dlEnd = klineChart.getDataList();
            var lastDi = _frvpLastRealDi(dlEnd);
            if (lastDi != null) {
                var startTs = Math.min(ts0, ts1);
                var lastTs = dlEnd[lastDi].timestamp;
                return { start: Math.min(startTs, lastTs), end: lastTs };
            }
        } catch (e) {}
    }
    return { start: Math.min(ts0, ts1), end: Math.max(ts0, ts1) };
}

// Resolve profile data for an overlay: exact server data when fetched for the
// current range, else an instant client approximation (and schedule the fetch).
function _frvpResolveData(overlay) {
    var range = _frvpRangeTs(overlay);
    if (!range) return null;
    var entry = _frvpDataCache[overlay.id] || (_frvpDataCache[overlay.id] = {});
    var key = klineChartSymbol + '|' + klineChartTimeframe + '|' + range.start + '|' + range.end;

    if (entry.key === key && entry.vp) {
        return { vp: entry.vp, meta: entry.meta || null, approx: false };
    }

    // Not drawing → want the real profile for this range
    var isDrawing = overlay.currentStep !== undefined && overlay.currentStep !== -1;
    if (!isDrawing) _frvpScheduleFetch(overlay.id, key, range.start, range.end);

    // Instant approximation from the loaded chart candles
    if (entry.approxKey !== key) {
        entry.approxKey = key;
        entry.approxVp = null;
        try {
            var dl = klineChart ? klineChart.getDataList() : null;
            var pts = overlay.points;
            var di0 = _frvpPointDi(pts[0]), di1 = _frvpPointDi(pts[1]);
            // Extend Right: approx from the earlier stored anchor to the last
            // REAL bar (mirrors the effective range in _frvpRangeTs above).
            if (overlay.extendData && overlay.extendData.extendRight && dl && dl.length && di0 != null && di1 != null) {
                var lastRealDi = _frvpLastRealDi(dl);
                if (lastRealDi != null) { di0 = Math.min(di0, di1); di1 = lastRealDi; }
            }
            if (dl && dl.length && di0 != null && di1 != null) {
                var i0 = Math.max(0, Math.min(dl.length - 1, Math.min(di0, di1)));
                var i1 = Math.max(0, Math.min(dl.length - 1, Math.max(di0, di1)));
                if (i1 > i0 && typeof _vpComputeFromCandles === 'function') {
                    entry.approxVp = _vpComputeFromCandles(dl, i0, i1, 120);
                }
            }
        } catch (e) {}
    }
    if (entry.approxVp) return { vp: entry.approxVp, meta: null, approx: true };
    // Last resort: stale server data from the previous range (better than blank)
    if (entry.vp) return { vp: entry.vp, meta: entry.meta || null, approx: true };
    return null;
}

function _frvpScheduleFetch(overlayId, key, startTs, endTs) {
    var entry = _frvpDataCache[overlayId] || (_frvpDataCache[overlayId] = {});
    if (entry.key === key || entry.pendingKey === key) return;
    entry.pendingKey = key;
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = setTimeout(function() { _frvpFetch(overlayId, key, startTs, endTs); }, FRVP_FETCH_DEBOUNCE_MS);
}

function _frvpFetch(overlayId, key, startTs, endTs) {
    fetch('/api/volume-profile/' + encodeURIComponent(klineChartSymbol) +
          '?tf=' + encodeURIComponent(klineChartTimeframe) +
          '&start=' + startTs + '&end=' + endTs + '&num_bins=200',
          { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var entry = _frvpDataCache[overlayId];
        if (!entry || entry.pendingKey !== key) return; // stale response
        entry.pendingKey = null;
        entry.key = key;
        if (data && data.bins && data.bins.length) {
            entry.vp = data;
            entry.meta = {
                source_interval: data.source_interval || null,
                bar_count: data.bar_count || null,
                proxy: data.proxy || null
            };
        } else {
            entry.vp = null;
            entry.meta = null;
        }
        frvpRedrawAll();
    })
    .catch(function(e) {
        var entry = _frvpDataCache[overlayId];
        if (entry && entry.pendingKey === key) entry.pendingKey = null;
        console.warn('FRVP fetch error:', e);
    });
}

// ============================================================
// Profile math (client-side, over fetched bins)
// ============================================================
// Server bins are sparse (non-zero only). Reconstruct the dense grid so the
// CME value-area expansion and row aggregation see gaps correctly.
function _frvpDenseGrid(vp) {
    var bins = vp.bins;
    if (!bins || !bins.length) return null;
    var binSize = vp.bin_size;
    if (!binSize || binSize <= 0) return null;
    var firstPrice = bins[0].price;
    var lastPrice = bins[bins.length - 1].price;
    var n = Math.round((lastPrice - firstPrice) / binSize) + 1;
    if (!isFinite(n) || n < 1 || n > 5000) return null;
    var total = new Float64Array(n);
    var buy = new Float64Array(n);
    var sell = new Float64Array(n);
    for (var i = 0; i < bins.length; i++) {
        var idx = Math.round((bins[i].price - firstPrice) / binSize);
        if (idx < 0 || idx >= n) continue;
        total[idx] += bins[i].total;
        buy[idx] += bins[i].buy || 0;
        sell[idx] += bins[i].sell || 0;
    }
    return { n: n, firstPrice: firstPrice, binSize: binSize, total: total, buy: buy, sell: sell };
}

// CME expand-from-POC value area at an arbitrary percentage. `grid` is
// optional — pass the already-built _frvpDenseGrid result (e.g. from
// _frvpGetProfileMath) to avoid rebuilding it a second time per repaint.
function _frvpValueArea(vp, pct, grid) {
    var g = grid || _frvpDenseGrid(vp);
    if (!g) return { vah: vp.vah, val: vp.val };
    var pocIdx = 0, maxVol = 0, i;
    for (i = 0; i < g.n; i++) if (g.total[i] > maxVol) { maxVol = g.total[i]; pocIdx = i; }
    if (maxVol <= 0) return { vah: vp.vah, val: vp.val };
    var totalAll = 0;
    for (i = 0; i < g.n; i++) totalAll += g.total[i];
    var target = totalAll * (pct / 100);
    var acc = g.total[pocIdx], loIdx = pocIdx, hiIdx = pocIdx;
    while (acc < target && (loIdx > 0 || hiIdx < g.n - 1)) {
        var upVol = hiIdx < g.n - 1 ? g.total[hiIdx + 1] : 0;
        var dnVol = loIdx > 0 ? g.total[loIdx - 1] : 0;
        if (upVol >= dnVol && hiIdx < g.n - 1) { hiIdx++; acc += g.total[hiIdx]; }
        else if (loIdx > 0) { loIdx--; acc += g.total[loIdx]; }
        else { hiIdx++; acc += g.total[hiIdx]; }
    }
    // firstPrice is bin 0's CENTER; server convention: vah = top edge of the
    // highest VA bin, val = bottom edge of the lowest VA bin.
    return {
        vah: g.firstPrice + hiIdx * g.binSize + g.binSize / 2,
        val: g.firstPrice + loIdx * g.binSize - g.binSize / 2
    };
}

// Aggregate the dense grid down to a display row count (TV "Number of Rows").
// Takes the already-built grid (see _frvpGetProfileMath) — callers that don't
// have one yet should build it via _frvpDenseGrid(vp) first.
function _frvpDisplayRows(g, rowCount) {
    if (!g) return null;
    var group = Math.max(1, Math.ceil(g.n / rowCount));
    var rows = [];
    var maxTotal = 0, maxAbsNet = 0;
    for (var start = 0; start < g.n; start += group) {
        var end = Math.min(g.n - 1, start + group - 1);
        var t = 0, b = 0, s = 0;
        for (var i = start; i <= end; i++) { t += g.total[i]; b += g.buy[i]; s += g.sell[i]; }
        var pLow = g.firstPrice + start * g.binSize - g.binSize / 2;
        var pHigh = g.firstPrice + end * g.binSize + g.binSize / 2;
        rows.push({ pLow: pLow, pHigh: pHigh, pMid: (pLow + pHigh) / 2, total: t, buy: b, sell: s });
        if (t > maxTotal) maxTotal = t;
        var net = Math.abs(b - s);
        if (net > maxAbsNet) maxAbsNet = net;
    }
    return { rows: rows, maxTotal: maxTotal, maxAbsNet: maxAbsNet };
}

// klinecharts v9 re-runs createPointFigures (and createYAxisFigures) on every
// crosshair move / pan tick, but the grid/rows/value-area above are pure
// DATA-SPACE math — viewport-independent. Memoize per overlay, invalidated by
// `vp` object identity (a fresh object is handed out whenever the underlying
// data actually changes: _frvpFetch assigns a brand-new `entry.vp` on
// response, and the approx branch in _frvpResolveData assigns a brand-new
// `entry.approxVp` whenever its range key changes — including an edge-drag
// resize or Extend Right absorbing a new bar) plus a small params key (only
// the knobs that feed this math: vpRows, vaPct — vpMode does NOT, it only
// picks a per-row pixel/color formula in _frvpCreateFigures, so it's
// deliberately excluded to avoid needless misses on mode toggles).
function _frvpMathParamsKey(settings) {
    return settings.vpRows + '|' + settings.vaPct;
}

function _frvpGetProfileMath(overlay, vp, settings) {
    var id = overlay.id;
    var paramsKey = _frvpMathParamsKey(settings);
    var cached = _frvpMathCache.get(id);
    if (cached && cached.vpRef === vp && cached.paramsKey === paramsKey) return cached;

    var grid = _frvpDenseGrid(vp);
    var entry = {
        vpRef: vp,
        paramsKey: paramsKey,
        grid: grid,
        display: grid ? _frvpDisplayRows(grid, settings.vpRows) : null,
        valueArea: (grid && settings.vaPct !== 70) ? _frvpValueArea(vp, settings.vaPct, grid) : null
    };
    if (!cached && _frvpMathCache.size >= FRVP_MATH_CACHE_MAX) {
        _frvpMathCache.delete(_frvpMathCache.keys().next().value); // evict oldest
    }
    _frvpMathCache.set(id, entry);
    return entry;
}

// ============================================================
// Rendering helpers
// ============================================================
function _frvpPriceY(yAxis, price) {
    var isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    return yAxis.convertToPixel(isLog && price > 0 ? Math.log(price) : price);
}

function _frvpFmtPrice(p) {
    if (p == null || !isFinite(p)) return '';
    if (p >= 1000) return Math.round(p).toLocaleString('en-US');
    if (p >= 1) return p.toFixed(2);
    return p.toPrecision(4);
}

// Resolve a point's dataIndex: engine-populated when drawn interactively or
// restored via snapPointsToData, but timestamp-only points exist too (the
// engine never back-fills dataIndex from timestamp) — binary-search then.
function _frvpPointDi(p) {
    if (!p) return null;
    if (p.dataIndex != null) return p.dataIndex;
    if (p.timestamp == null || !klineChart) return null;
    var dl = klineChart.getDataList();
    if (!dl || !dl.length) return null;
    var lo = 0, hi = dl.length - 1;
    while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        if (dl[mid].timestamp < p.timestamp) lo = mid + 1;
        else if (dl[mid].timestamp > p.timestamp) hi = mid - 1;
        else return mid;
    }
    if (lo >= dl.length) return hi;
    if (hi < 0) return lo;
    return (Math.abs(dl[lo].timestamp - p.timestamp) < Math.abs(dl[hi].timestamp - p.timestamp)) ? lo : hi;
}

// Geometry for the edge-drag hit test in kline-drawings.js:
// { x1, x2, yTop, yBot } in pane pixels, or null.
function frvpGetEdgeGeometry(overlayId) {
    if (!klineChart) return null;
    try {
        var ovs = klineChart.getOverlayById(overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (!ov || !ov.points || ov.points.length < 2) return null;
        var di0 = _frvpPointDi(ov.points[0]);
        var di1 = _frvpPointDi(ov.points[1]);
        if (di0 == null || di1 == null) return null;
        var c0 = klineChart.convertToPixel({ dataIndex: di0, value: 0 }, { paneId: 'candle_pane' });
        var c1 = klineChart.convertToPixel({ dataIndex: di1, value: 0 }, { paneId: 'candle_pane' });
        if (!c0 || !c1) return null;
        var geo = { x1: Math.min(c0.x, c1.x), x2: Math.max(c0.x, c1.x), yTop: null, yBot: null };
        // Extend Right: hit-testing must land on the VISIBLE live boundary
        // (last real bar), not the stored anchor.
        if (ov.extendData && ov.extendData.extendRight) {
            try {
                var dlg = klineChart.getDataList();
                var lastDig = _frvpLastRealDi(dlg);
                if (lastDig != null) {
                    var cE = klineChart.convertToPixel({ dataIndex: lastDig, value: 0 }, { paneId: 'candle_pane' });
                    if (cE && typeof cE.x === 'number' && isFinite(cE.x) && cE.x > geo.x1) geo.x2 = cE.x;
                }
            } catch (eG) {}
        }
        var entry = _frvpDataCache[overlayId];
        var vp = (entry && (entry.vp || entry.approxVp)) || null;
        if (vp && vp.bins && vp.bins.length) {
            var half = (vp.bin_size || 0) / 2;
            var isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
            var hi = vp.bins[vp.bins.length - 1].price + half;
            var lo = vp.bins[0].price - half;
            var pTop = klineChart.convertToPixel({ dataIndex: di0, value: isLog && hi > 0 ? Math.log(hi) : hi }, { paneId: 'candle_pane' });
            var pBot = klineChart.convertToPixel({ dataIndex: di0, value: isLog && lo > 0 ? Math.log(lo) : lo }, { paneId: 'candle_pane' });
            if (pTop && pBot) { geo.yTop = Math.min(pTop.y, pBot.y); geo.yBot = Math.max(pTop.y, pBot.y); }
        }
        return geo;
    } catch (e) { return null; }
}

// ============================================================
// Main renderer (overlay createPointFigures)
// ============================================================
function _frvpCreateFigures(params) {
    try {
        var coords = params.coordinates;
        var overlay = params.overlay || {};
        var yAxis = params.yAxis;
        var bounding = params.bounding;
        if (!coords || !coords.length || !yAxis || !bounding) return [];

        var figures = [];
        var edgeColor = 'rgba(255, 213, 79, 0.55)';

        // Single point so far (creation step 2): vertical guide at the anchor
        if (coords.length < 2 || !coords[1]) {
            figures.push({ type: 'line', ignoreEvent: true,
                attrs: { coordinates: [{ x: coords[0].x, y: 0 }, { x: coords[0].x, y: bounding.height }] },
                styles: { style: 'dashed', color: edgeColor, size: 1, dashedValue: [4, 4] } });
            return figures;
        }

        var x1 = Math.min(coords[0].x, coords[1].x);
        var x2 = Math.max(coords[0].x, coords[1].x);

        // Extend Right: the visible right boundary sits on the last REAL bar,
        // not the stored anchor (coordinates only reflect stored points).
        // Histogram/box/lines/hit-rect all derive from x2 below, so they all
        // extend with it.
        var extendRight = !!(overlay.extendData && overlay.extendData.extendRight);
        if (extendRight && klineChart) {
            try {
                var dlx = klineChart.getDataList();
                var lastDix = _frvpLastRealDi(dlx);
                if (lastDix != null) {
                    var pxEnd = klineChart.convertToPixel({ dataIndex: lastDix, value: 0 }, { paneId: overlay.paneId || 'candle_pane' });
                    if (pxEnd && typeof pxEnd.x === 'number' && isFinite(pxEnd.x) && pxEnd.x > x1) x2 = pxEnd.x;
                }
            } catch (e) {}
        }
        var rangeW = x2 - x1;

        var resolved = _frvpResolveData(overlay);
        var settings = _frvpSettings(overlay);

        // Vertical span of the profile (from data; falls back to full pane)
        var yTop = 0, yBot = bounding.height;
        var vp = resolved && resolved.vp;
        if (vp && vp.bins && vp.bins.length) {
            var half = (vp.bin_size || 0) / 2;
            var tY = _frvpPriceY(yAxis, vp.bins[vp.bins.length - 1].price + half);
            var bY = _frvpPriceY(yAxis, vp.bins[0].price - half);
            if (tY != null && bY != null && isFinite(tY) && isFinite(bY)) {
                yTop = Math.min(tY, bY); yBot = Math.max(tY, bY);
            }
        }

        // Edge boundary lines (span the profile height). With Extend Right on,
        // the right boundary renders "open" — same hue, low alpha + dashed —
        // so it reads as a live edge that keeps absorbing new candles.
        figures.push({ type: 'line', ignoreEvent: true,
            attrs: { coordinates: [{ x: x1, y: yTop }, { x: x1, y: yBot }] },
            styles: { color: edgeColor, size: 1 } });
        figures.push({ type: 'line', ignoreEvent: true,
            attrs: { coordinates: [{ x: x2, y: yTop }, { x: x2, y: yBot }] },
            styles: extendRight
                ? { style: 'dashed', color: 'rgba(255, 213, 79, 0.25)', size: 1, dashedValue: [4, 4] }
                : { color: edgeColor, size: 1 } });

        if (!vp || !vp.bins || !vp.bins.length || rangeW < 4) {
            return figures; // loading / degenerate — edges only
        }

        // Data-space math (grid/rows/value-area) — memoized, see
        // _frvpGetProfileMath. Only the pixel mapping below runs per repaint.
        var math = _frvpGetProfileMath(overlay, vp, settings);

        // Value area (client recompute when non-default %)
        var poc = vp.poc, vah = vp.vah, val = vp.val;
        if (settings.vaPct !== 70 && math.valueArea) {
            vah = math.valueArea.vah; val = math.valueArea.val;
        }

        // Subtle box fill over the profile area
        figures.push({ type: 'rect', ignoreEvent: true,
            attrs: { x: x1, y: yTop, width: rangeW, height: yBot - yTop },
            styles: { style: 'fill', color: 'rgba(255, 255, 255, 0.03)' } });

        // Histogram rows
        var display = math.display;
        if (display && display.rows.length) {
            var maxBarW = rangeW * 0.35;
            var mode = settings.vpMode;
            for (var i = 0; i < display.rows.length; i++) {
                var row = display.rows[i];
                if (row.total <= 0) continue;
                var rTop = _frvpPriceY(yAxis, row.pHigh);
                var rBot = _frvpPriceY(yAxis, row.pLow);
                if (rTop == null || rBot == null || !isFinite(rTop) || !isFinite(rBot)) continue;
                var ry = Math.min(rTop, rBot);
                var rh = Math.max(Math.abs(rBot - rTop) - 1, 1);
                var inVA = vah != null && val != null && row.pMid <= vah && row.pMid >= val;
                var isPoc = poc != null && poc >= row.pLow && poc <= row.pHigh;
                var alpha = isPoc ? 0.55 : (inVA ? 0.35 : 0.18);

                if (mode === 'delta') {
                    var net = row.buy - row.sell;
                    var w = display.maxAbsNet > 0 ? (Math.abs(net) / display.maxAbsNet) * maxBarW : 0;
                    if (w > 0.5) {
                        figures.push({ type: 'rect', ignoreEvent: true,
                            attrs: { x: x1, y: ry, width: Math.max(w, 1), height: rh },
                            styles: { style: 'fill', color: (net >= 0 ? 'rgba(63, 185, 80, ' : 'rgba(248, 81, 73, ') + Math.min(alpha + 0.1, 0.7) + ')' } });
                    }
                } else {
                    var barW = display.maxTotal > 0 ? Math.max((row.total / display.maxTotal) * maxBarW, 1.5) : 0;
                    if (mode === 'total') {
                        figures.push({ type: 'rect', ignoreEvent: true,
                            attrs: { x: x1, y: ry, width: barW, height: rh },
                            styles: { style: 'fill', color: 'rgba(220, 225, 235, ' + alpha + ')' } });
                    } else { // updown
                        var buyW = row.total > 0 ? (row.buy / row.total) * barW : barW / 2;
                        var sellW = barW - buyW;
                        if (buyW > 0.4) figures.push({ type: 'rect', ignoreEvent: true,
                            attrs: { x: x1, y: ry, width: buyW, height: rh },
                            styles: { style: 'fill', color: 'rgba(220, 225, 235, ' + alpha + ')' } });
                        if (sellW > 0.4) figures.push({ type: 'rect', ignoreEvent: true,
                            attrs: { x: x1 + buyW, y: ry, width: sellW, height: rh },
                            styles: { style: 'fill', color: 'rgba(100, 110, 130, ' + alpha + ')' } });
                    }
                }
            }
        }

        // POC / VAH / VAL lines (range-scoped)
        function hLine(price, color, dashed, size) {
            var y = _frvpPriceY(yAxis, price);
            if (y == null || !isFinite(y)) return;
            figures.push({ type: 'line', ignoreEvent: true,
                attrs: { coordinates: [{ x: x1, y: y }, { x: x2, y: y }] },
                styles: dashed ? { style: 'dashed', color: color, size: size, dashedValue: [4, 4] }
                               : { color: color, size: size } });
        }
        if (poc != null) hLine(poc, 'rgba(255, 255, 255, 0.6)', false, 1.5);
        if (vah != null) hLine(vah, 'rgba(255, 255, 255, 0.45)', true, 1);
        if (val != null) hLine(val, 'rgba(255, 255, 255, 0.45)', true, 1);

        // Transparent body hit-rect over the histogram bar area only —
        // native select / whole-range drag / right-click land here. The rest
        // of the range stays free for chart panning (matches old left-area UX).
        figures.push({ type: 'rect',
            attrs: { x: x1, y: yTop, width: Math.max(rangeW * 0.35, 12), height: yBot - yTop },
            styles: { style: 'fill', color: 'transparent' } });

        // Selection / hover adornments
        var isSelected = (typeof klineSelectedOverlayId !== 'undefined') && overlay.id === klineSelectedOverlayId;
        var isHovered = (typeof _hoveredOverlayId !== 'undefined') && overlay.id === _hoveredOverlayId;
        if (isSelected || isHovered) {
            var midY = (yTop + yBot) / 2;
            var hs = 5;
            var hStyle = { style: 'fill', color: '#2962ff', borderColor: '#ffffff', borderSize: 1.5 };
            figures.push({ type: 'rect', ignoreEvent: true, attrs: { x: x1 - hs, y: midY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
            figures.push({ type: 'rect', ignoreEvent: true, attrs: { x: x2 - hs, y: midY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
        }
        if (isSelected) {
            var meta = resolved.meta;
            var badge = '';
            if (resolved.approx) badge = 'approx — refining…';
            else if (meta && meta.source_interval && meta.bar_count) badge = 'built from ' + meta.source_interval + ' × ' + meta.bar_count.toLocaleString('en-US') + ' bars';
            if (meta && meta.proxy) badge += (badge ? ' • ' : '') + 'vol: ' + meta.proxy;
            if (badge) {
                figures.push({ type: 'text', ignoreEvent: true,
                    attrs: { x: x1 + 3, y: yTop - 6, text: badge },
                    styles: { color: 'rgba(255, 213, 79, 0.75)', size: 10, family: 'JetBrains Mono, monospace' } });
            }
        }

        return figures;
    } catch (err) {
        console.error('frvp createPointFigures error:', err);
        return [];
    }
}

// Y-axis docked price labels for POC / VAH / VAL (Sierra-style "value labels").
function _frvpCreateAxisFigures(params) {
    try {
        var overlay = params.overlay || {};
        var yAxis = params.yAxis;
        if (!yAxis || !overlay.points || overlay.points.length < 2) return [];
        var entry = _frvpDataCache[overlay.id];
        var vp = entry && entry.vp; // exact data only — no labels for approximations
        if (!vp || !vp.bins || !vp.bins.length) return [];
        var settings = _frvpSettings(overlay);
        var poc = vp.poc, vah = vp.vah, val = vp.val;
        if (settings.vaPct !== 70) {
            // Shares the same memo as _frvpCreateFigures (same overlay + vp +
            // params key), so this is a cache hit in the common case.
            var math = _frvpGetProfileMath(overlay, vp, settings);
            if (math.valueArea) { vah = math.valueArea.vah; val = math.valueArea.val; }
        }
        var figures = [];
        function axisLabel(price, bg, fg) {
            var y = _frvpPriceY(yAxis, price);
            if (y == null || !isFinite(y)) return;
            figures.push({ type: 'text', ignoreEvent: true,
                attrs: { x: 0, y: y, text: _frvpFmtPrice(price), baseline: 'middle' },
                styles: { color: fg, size: 9, family: 'JetBrains Mono, monospace',
                          backgroundColor: bg, paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1 } });
        }
        if (poc != null) axisLabel(poc, 'rgba(255, 213, 79, 0.9)', '#000000');
        if (vah != null) axisLabel(vah, 'rgba(120, 123, 134, 0.85)', '#ffffff');
        if (val != null) axisLabel(val, 'rgba(120, 123, 134, 0.85)', '#ffffff');
        return figures;
    } catch (e) { return []; }
}

// ============================================================
// Tool activation (toolbar button + Alt+F both land here)
// ============================================================
function frvpActivateTool() {
    var btn = document.querySelector('[data-tool="volumeProfileRange"]');
    if (btn && typeof handleKlineToolClick === 'function') {
        handleKlineToolClick('volumeProfileRange', btn, [], null);
    }
}

// ============================================================
// Redraw poke (no explicit redraw API — nudge with the last candle)
// ============================================================
function frvpRedrawAll() {
    if (!klineChart) return;
    try {
        var dl = klineChart.getDataList();
        if (dl && dl.length) klineChart.updateData(dl[dl.length - 1]);
    } catch (e) {}
}

// ============================================================
// Legacy migration: pre-overlay ranges lived in localStorage under
// 'frvp_<symbol>_<tf>' as [{startTs, endTs}]. Convert them ONCE into
// standard 'frvp' drawings (synced via /api/drawings), then remove the keys.
// Ranges are timestamp-based, so the per-TF split collapses (dedupe).
// ============================================================
function frvpMigrateLegacy() {
    if (!klineChart || !klineChartSymbol) return;
    try {
        var prefix = 'frvp_' + klineChartSymbol + '_';
        var toRemove = [], entries = [];
        for (var i = 0; i < localStorage.length; i++) {
            var k = localStorage.key(i);
            if (!k || k.indexOf(prefix) !== 0) continue;
            toRemove.push(k);
            try {
                var arr = JSON.parse(localStorage.getItem(k) || '[]');
                if (Array.isArray(arr)) entries = entries.concat(arr);
            } catch (e2) {}
        }
        if (!toRemove.length) return;
        var seen = {};
        entries.forEach(function(item) {
            if (!item || item.startTs == null || item.endTs == null) return;
            var s = Math.min(item.startTs, item.endTs);
            var en = Math.max(item.startTs, item.endTs);
            var dk = s + ':' + en;
            if (seen[dk]) return;
            seen[dk] = true;
            var d = {
                id: 'frvp_m_' + s + '_' + en + '_' + (++_frvpIdCounter),
                name: 'frvp',
                points: [{ timestamp: s, value: 0 }, { timestamp: en, value: 0 }],
                color: '#ffd54f',
                width: 1,
                paneId: 'candle_pane',
                extendData: Object.assign({}, FRVP_DEFAULT_SETTINGS)
            };
            var ov = (typeof createOverlayFromDrawing === 'function') ? createOverlayFromDrawing(d) : null;
            if (ov && typeof saveKlineDrawing === 'function') saveKlineDrawing(d);
        });
        toRemove.forEach(function(k) { try { localStorage.removeItem(k); } catch (e3) {} });
    } catch (e) {}
}

// Back-compat entry point — old call sites (symbol/TF switch, chart open)
// invoked frvpLoad(); overlays now restore via the standard drawing loader,
// so the only remaining job is the one-time legacy migration.
function frvpLoad() {
    frvpMigrateLegacy();
}

// ============================================================
// Lifecycle
// ============================================================
function frvpOnChartClose() {
    for (var id in _frvpDataCache) {
        if (_frvpDataCache[id] && _frvpDataCache[id].timer) {
            try { clearTimeout(_frvpDataCache[id].timer); } catch (e) {}
        }
    }
    _frvpDataCache = {};
    _frvpMathCache.clear();
}
