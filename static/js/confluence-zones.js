/* Confluence Zones — native KLineChart indicator.
 *
 * Fetches clustered multi-source S/R zones from /api/confluence for the current
 * symbol and draws them as shaded horizontal price bands:
 *   - opacity + minimum height scale with the zone's discrete tier (strong/medium/faint)
 *   - color by side (support = teal, resistance = red, neutral = grey)
 *   - one compact plain-English label per zone (strongest source + "+N"),
 *     de-collided in 12px steps (mirrors key-levels.js so labels never overlap)
 *   - solo (single-source) zones only render when strong enough (weight >= 2.0),
 *     matching SOLO_MIN_WEIGHT in carrac/scanner/labels.py
 *
 * Async model: key-levels.js computes synchronously in its draw callback; we
 * fetch from the backend, cache, then force a redraw via klineChart.resize().
 *
 * Hover tooltip (full per-source price list) is a deferred follow-up; the
 * scanner card and MCP carry the full breakdown.
 */

// ============================================================
// State
// ============================================================
var _confData = null;           // {symbol, price, supports:[], resistances:[]}
var _confSymbol = null;         // symbol the cached data is for
var _confFetching = false;
var _confRegistered = false;
var _confIndicatorCreated = false;

// Mirror carrac/scanner/labels.py SOLO_MIN_WEIGHT. A single-source zone shows
// only if its one level is at least this strong (for a solo zone strength == weight).
var _CONF_SOLO_MIN = 2.0;
var _CONF_MAX_PER_SIDE = 6;     // keep the chart readable

var _CONF_TIER = {
    strong: { minH: 7, alpha: 0.24, labelAlpha: 0.95 },
    medium: { minH: 4, alpha: 0.14, labelAlpha: 0.80 },
    faint:  { minH: 2, alpha: 0.07, labelAlpha: 0.55 },
};
var _CONF_SUPPORT_RGB = '38,166,154';
var _CONF_RESIST_RGB = '239,83,80';
var _CONF_NEUTRAL_RGB = '150,150,150';

// ============================================================
// Filtering / label helpers
// ============================================================
function _confShouldShow(z) {
    if (!z || !z.price) return false;
    if ((z.count || 1) >= 2) return true;
    return (z.strength || 0) >= _CONF_SOLO_MIN;   // solo: only if strong enough
}

function _confCompactLabel(z) {
    var members = z.members || [];
    if (!members.length) return z.label_friendly || z.label || '';
    var strongest = members.slice().sort(function(a, b) {
        return (b.weight || 0) - (a.weight || 0);
    })[0];
    var name = strongest.name || strongest.code || '';
    var extra = members.length - 1;
    return extra > 0 ? (name + ' +' + extra) : name;
}

// ============================================================
// Register native indicator at load (before chart exists)
// ============================================================
(function confRegisterOnLoad() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'CONFLUENCE_NATIVE',
            shortName: '',
            precision: 8,
            figures: [],
            calc: function(dataList) { return dataList.map(function() { return {}; }); },
            draw: function(params) {
                if (klineActiveIndicators && klineActiveIndicators.CONFLUENCE) {
                    _confDraw(params);
                }
                return true;
            },
        });
        _confRegistered = true;
    } catch (e) {
        console.warn('[CONF] registerIndicator failed:', e);
    }
})();

function confluenceEnsureIndicator() {
    if (!klineChart || _confIndicatorCreated || !_confRegistered) return;
    try {
        klineChart.createIndicator('CONFLUENCE_NATIVE', true, { id: 'candle_pane' });
        _confIndicatorCreated = true;
    } catch (e) {}
}

// ============================================================
// Lifecycle
// ============================================================
function confluenceStart() {
    if (!klineChart) return;
    confluenceEnsureIndicator();
    _confFetch(klineChartSymbol);
}

function confluenceStop() {
    _confData = null;
    _confSymbol = null;
    if (klineChart && klineChart.resize) { try { klineChart.resize(); } catch (e) {} }
}

function confluenceOnDataChange() {
    // Symbol/timeframe changed — refetch if the indicator is active.
    if (klineActiveIndicators && klineActiveIndicators.CONFLUENCE) {
        _confData = null;
        _confFetch(klineChartSymbol);
    }
}

function confluenceOnChartClose() {
    _confData = null;
    _confSymbol = null;
    _confIndicatorCreated = false;
}

// ============================================================
// Fetch zones from the backend, then force a redraw
// ============================================================
function _confFetch(symbol) {
    if (!symbol || _confFetching) return;
    _confFetching = true;
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
    fetch('/api/confluence?symbol=' + encodeURIComponent(symbol) + '&tf=' + encodeURIComponent(tf),
          { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            _confFetching = false;
            if (!data || data.symbol !== symbol) { return; }   // stale / error
            _confData = {
                symbol: data.symbol,
                price: data.price,
                supports: (data.supports || []).filter(_confShouldShow).slice(0, _CONF_MAX_PER_SIDE),
                resistances: (data.resistances || []).filter(_confShouldShow).slice(0, _CONF_MAX_PER_SIDE),
            };
            _confSymbol = symbol;
            if (klineChart && klineChart.resize) { try { klineChart.resize(); } catch (e) {} }
        })
        .catch(function() { _confFetching = false; });
}

// ============================================================
// Draw — runs every frame from the indicator draw callback
// ============================================================
function _confDraw(params) {
    if (!_confData || !klineChart) return;
    if (_confData.symbol !== klineChartSymbol) return;   // guard against asset switch mid-flight

    var ctx = params.ctx;
    var bounding = params.bounding;
    var yAxis = params.yAxis;
    if (!ctx || !bounding || !yAxis) return;

    var width = bounding.width;
    var zones = _confData.supports.concat(_confData.resistances);
    if (!zones.length) return;

    // when log-scale is on, candle data is stored as
    // Math.log(price) (see convertToKlineData in chart-render.js) so yAxis.convertToPixel
    // expects log-space values — but /api/confluence always returns raw
    // prices. Transform every price handed to convertToPixel the same way
    // the other overlays do (grep `_isLog`), otherwise zones render
    // at the wrong pixel row whenever log scale is active.
    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);

    ctx.save();
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.textAlign = 'right';

    // Pre-resolve y for label de-collision (top → bottom).
    var resolved = [];
    for (var i = 0; i < zones.length; i++) {
        var z = zones[i];
        var span = z.span || [z.price, z.price];
        var spanLo = span[0];
        var spanHi = span[1];
        if (_isLog) {
            if (spanLo > 0) spanLo = Math.log(spanLo);
            if (spanHi > 0) spanHi = Math.log(spanHi);
        }
        var yHi = yAxis.convertToPixel(spanHi);   // higher price → smaller y
        var yLo = yAxis.convertToPixel(spanLo);
        if (yHi == null || yLo == null || !isFinite(yHi) || !isFinite(yLo)) continue;
        var top = Math.min(yHi, yLo);
        var bot = Math.max(yHi, yLo);
        if (bot < -50 || top > bounding.height + 50) continue;
        resolved.push({ z: z, top: top, bot: bot, center: (top + bot) / 2 });
    }
    resolved.sort(function(a, b) { return a.center - b.center; });

    // Bands
    for (var j = 0; j < resolved.length; j++) {
        var r = resolved[j];
        var z2 = r.z;
        var tier = _CONF_TIER[z2.tier] || _CONF_TIER.faint;
        var rgb = z2.side === 'support' ? _CONF_SUPPORT_RGB
                : z2.side === 'resistance' ? _CONF_RESIST_RGB : _CONF_NEUTRAL_RGB;
        var h = Math.max(r.bot - r.top, tier.minH);
        var y = r.center - h / 2;
        ctx.fillStyle = 'rgba(' + rgb + ',' + tier.alpha + ')';
        ctx.fillRect(0, y, width, h);
        // thin edge line at zone center for stronger zones
        if (z2.tier === 'strong' || z2.tier === 'medium') {
            ctx.strokeStyle = 'rgba(' + rgb + ',' + (tier.alpha + 0.25) + ')';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(0, r.center);
            ctx.lineTo(width, r.center);
            ctx.stroke();
        }
    }

    // Labels (de-collided in 12px steps, drawn at the right edge)
    var lastY = -Infinity;
    for (var k = 0; k < resolved.length; k++) {
        var rr = resolved[k];
        var zz = rr.z;
        var tier2 = _CONF_TIER[zz.tier] || _CONF_TIER.faint;
        var rgb2 = zz.side === 'support' ? _CONF_SUPPORT_RGB
                 : zz.side === 'resistance' ? _CONF_RESIST_RGB : _CONF_NEUTRAL_RGB;
        var ly = rr.center + 3;
        if (ly - lastY < 12) ly = lastY + 12;
        lastY = ly;
        ctx.fillStyle = 'rgba(' + rgb2 + ',' + tier2.labelAlpha + ')';
        ctx.fillText(_confCompactLabel(zz), width - 5, ly);
    }

    ctx.restore();
}
