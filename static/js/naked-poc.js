/* Naked POC — Native KLineChart Indicator
 *
 * Draws dashed horizontal levels for "naked" (untouched) points-of-control
 * from past sessions, piggy-backing on VP_NATIVE's draw callback (no new
 * klinecharts indicator plumbing needed — see volume-profile.js).
 *
 * Data comes from GET /api/naked-pocs/<symbol>?lookback=90, cached per
 * symbol for ~5 minutes. Purely a read/draw layer — no settings UI, no
 * alerts integration, no touched-POC rendering (all deferred).
 */

// ============================================================
// State
// ============================================================
var _npocData = null;          // Array of naked-POC levels for current symbol (or null)
var _npocFetchedFor = null;    // { symbol, ts } — what/when was last fetched
var _npocFetchInFlight = false;

var NPOC_MAX_LEVELS = 15;
var NPOC_STALE_MS = 300000; // 5 min

// ============================================================
// Fetch — invoked lazily from the draw path + explicitly from start/onDataChange
// ============================================================
function _npocFetchIfStale() {
    if (typeof klineChartSymbol === 'undefined' || !klineChartSymbol) return;
    if (_npocFetchInFlight) return;

    var symbolChanged = !_npocFetchedFor || _npocFetchedFor.symbol !== klineChartSymbol;
    var isStale = symbolChanged || (Date.now() - _npocFetchedFor.ts) > NPOC_STALE_MS;
    if (!isStale) return;

    if (symbolChanged) _npocData = null; // don't render stale-symbol levels while refetching

    _npocFetchInFlight = true;
    var fetchSymbol = klineChartSymbol;
    fetch('/api/naked-pocs/' + encodeURIComponent(fetchSymbol) + '?lookback=90', {
        credentials: 'same-origin'
    }).then(function(r) {
        return r.ok ? r.json() : null;
    }).then(function(json) {
        _npocFetchInFlight = false;
        // Always stamp the attempt (even on empty/error payload) — the draw path
        // runs every repaint, so an unstamped failure would retry at repaint rate.
        _npocFetchedFor = { symbol: fetchSymbol, ts: Date.now() };
        _npocData = (json && json.levels) ? json.levels.slice(0, NPOC_MAX_LEVELS) : null;
    }).catch(function() {
        _npocFetchInFlight = false;
        _npocFetchedFor = { symbol: fetchSymbol, ts: Date.now() };
        _npocData = null;
    });
}

// ============================================================
// Start / Stop (mirrors vpStart/vpStop)
// ============================================================
function npocStart() {
    if (!klineChart) return;
    _npocFetchIfStale();
    // No explicit redraw — draw callback picks up _npocData on next natural repaint
}

function npocStop() {
    // Non-destructive: data stays cached per symbol (~5 min) so re-enabling
    // doesn't need a refetch. Rendering already stops because volume-profile.js
    // gates the draw call on klineActiveIndicators.NPOC.
}

// ============================================================
// Lifecycle hook — called from volume-profile.js's vpOnDataChange
// ============================================================
function npocOnDataChange() {
    if (typeof klineChartSymbol !== 'undefined' && klineChartSymbol && _npocFetchedFor && _npocFetchedFor.symbol !== klineChartSymbol) {
        _npocData = null; // symbol switched — drop the old symbol's levels immediately
    }
    if (typeof klineActiveIndicators !== 'undefined' && klineActiveIndicators.NPOC) {
        _npocFetchIfStale();
    }
}

// ============================================================
// Draw — called from VP_NATIVE's draw callback every repaint (volume-profile.js)
// ============================================================
function _npocDrawNative(params) {
    // Lazy fetch: covers the case where NPOC was toggled on directly via the
    // indicator menu (not through npocStart) — cheap no-op when not stale.
    _npocFetchIfStale();

    if (!_npocData || !_npocData.length || !klineChart) return;
    // Cached levels must belong to the on-screen symbol (a late response for a
    // previous symbol may have landed after a switch)
    if (!_npocFetchedFor || typeof klineChartSymbol === 'undefined' || _npocFetchedFor.symbol !== klineChartSymbol) return;

    var ctx = params.ctx;
    var bounding = params.bounding;
    var yAxis = params.yAxis;
    var dataList = params.kLineDataList || params.dataList;
    if (!ctx || !bounding || !yAxis || !dataList || !dataList.length) return;

    // Real bar count, skipping any trailing phantom (future) bars
    var realEnd = (dataList._realLength || dataList.length) - 1;
    while (realEnd >= 0 && dataList[realEnd]._phantom) realEnd--;
    if (realEnd < 0) return;

    function toMs(ts) { return ts < 1e12 ? ts * 1000 : ts; }

    var firstMs = toMs(dataList[0].timestamp);
    var lastMs = toMs(dataList[realEnd].timestamp);

    // Binary search: first dataIndex in [0, realEnd] whose timestamp >= targetMs
    function findIdx(targetMs) {
        var lo = 0, hi = realEnd;
        while (lo < hi) {
            var mid = (lo + hi) >> 1;
            if (toMs(dataList[mid].timestamp) < targetMs) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    // Levels are RAW price; the axis maps ln(price) under log scale (mirrors _vpY)
    var _npocLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    function _npocY(p) { return yAxis.convertToPixel(_npocLog && p > 0 ? Math.log(p) : p); }

    var fmtP = (typeof _frvpFmtPrice === 'function') ? _frvpFmtPrice : function(p) { return String(p); };
    var cw = bounding.width;

    ctx.save();
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(255, 213, 79, 0.55)';
    ctx.fillStyle = 'rgba(255, 213, 79, 0.55)';

    var levels = _npocData.slice(0, NPOC_MAX_LEVELS);
    for (var i = 0; i < levels.length; i++) {
        var lvl = levels[i];
        if (!lvl || lvl.poc == null || !lvl.date) continue;

        var y = _npocY(lvl.poc);
        if (y == null || !isFinite(y) || y < 0 || y > bounding.height) continue;

        var parts = lvl.date.split('-');
        if (parts.length !== 3) continue;
        var targetMs = Date.UTC(+parts[0], (+parts[1]) - 1, +parts[2]);
        if (targetMs > lastMs) continue; // session start not yet in loaded data

        var startX;
        if (targetMs < firstMs) {
            // Session start predates the loaded window — anchor at the left edge
            startX = 0;
        } else {
            var idx = findIdx(targetMs);
            var px = klineChart.convertToPixel({ dataIndex: idx, value: 0 }, { paneId: 'candle_pane' });
            startX = (px && isFinite(px.x)) ? px.x : 0;
            if (startX < 0) startX = 0;
        }
        if (startX >= cw) continue; // off-screen to the right

        ctx.beginPath();
        ctx.moveTo(startX, y);
        ctx.lineTo(cw, y);
        ctx.stroke();

        var age = (lvl.age_days != null) ? lvl.age_days : '';
        var ageLabel = 'nPOC ' + age + 'd';
        ctx.textAlign = 'left';
        ctx.fillText(ageLabel, startX + 2, y - 3);
        ctx.textAlign = 'right';
        ctx.fillText('nPOC ' + fmtP(lvl.poc), cw - 4, y - 3);
        ctx.textAlign = 'left';
    }

    ctx.setLineDash([]);
    ctx.restore();
}
