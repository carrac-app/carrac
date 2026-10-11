/* Volume Profile & FRVP — Native KLineChart Indicator
 *
 * Draws directly on the chart's canvas via registerIndicator draw callback.
 * No separate canvas overlay, no polling, no ResizeObserver.
 *
 * VP computed client-side from visible candles — instant, zero network calls.
 * FRVP still fetches from server (sub-TF granularity for fixed ranges).
 *
 * VP_NATIVE is always active on candle_pane so FRVP (drawing tool) works
 * independently. Full-viewport VP only draws when toggled on via indicator panel.
 */

// ============================================================
// State
// ============================================================
var _vpData = null;         // Computed VP data for current viewport
var _vpLastVisKey = null;   // Cache key to avoid recomputing on every frame
var _vpRegistered = false;
var _vpIndicatorCreated = false;

// ============================================================
// Client-side VP computation (same algorithm as server)
// ============================================================
function _vpComputeFromCandles(dataList, startIdx, endIdx, numBins) {
    if (!numBins) numBins = 70;
    if (endIdx <= startIdx || !dataList || dataList.length < 2) return null;

    // Always work in RAW price space (matches the server engine, and keeps
    // POC/VA semantics identical regardless of axis scale). Under log scale
    // the chart's dataList is ln-transformed with _orig* fields preserved;
    // Heikin-Ashi log candles lack _orig* → exp() recovers raw.
    var _vpcLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    function P(d, key, origKey) {
        var v = d[key];
        if (!_vpcLog || v == null) return v;
        var o = d[origKey];
        return o != null ? o : Math.exp(v);
    }

    var priceLow = Infinity, priceHigh = -Infinity;
    var i, d, dh, dl_;
    for (i = startIdx; i <= endIdx; i++) {
        d = dataList[i];
        if (!d || d.high == null || d.low == null) continue;
        dh = P(d, 'high', '_origHigh');
        dl_ = P(d, 'low', '_origLow');
        if (dh > priceHigh) priceHigh = dh;
        if (dl_ < priceLow) priceLow = dl_;
    }
    if (priceHigh <= priceLow || priceHigh === 0) return null;

    var binSize = (priceHigh - priceLow) / numBins;
    var totalVol = new Float64Array(numBins);
    var buyVol = new Float64Array(numBins);

    for (i = startIdx; i <= endIdx; i++) {
        d = dataList[i];
        if (!d || d.high == null || d.low == null) continue;
        var v = d.volume;
        dh = P(d, 'high', '_origHigh');
        dl_ = P(d, 'low', '_origLow');
        var dOpen = P(d, 'open', '_origOpen');
        var dClose = P(d, 'close', '_origClose');
        if (!v || v <= 0 || dh <= dl_) continue;

        // Buy/sell estimate: close > open = more buy pressure
        var bv = dClose >= dOpen ? v * 0.6 : v * 0.4;

        var loBin = Math.max(0, Math.floor((dl_ - priceLow) / binSize));
        var hiBin = Math.min(numBins - 1, Math.floor((dh - priceLow) / binSize));
        var span = hiBin - loBin + 1;
        if (span <= 0) continue;

        // Triangular kernel centred on the OHLC midpoint (ported from the
        // server engine): uniform distribution over-allocates to
        // the tails on volatile bars and drifts the POC by up to 1 bin.
        var mid = (dOpen + dh + dl_ + dClose) / 4;
        var halfRange = dh > dl_ ? (dh - dl_) / 2 : 1;
        var weights = [];
        var weightSum = 0;
        for (var b = loBin; b <= hiBin; b++) {
            var binCentre = priceLow + (b + 0.5) * binSize;
            var w = Math.max(0, 1 - Math.abs(binCentre - mid) / halfRange);
            weights.push(w);
            weightSum += w;
        }
        if (weightSum <= 0) {
            // Degenerate bar — fall back to uniform
            weightSum = span;
            for (var wb = 0; wb < weights.length; wb++) weights[wb] = 1;
        }
        for (var b2 = loBin; b2 <= hiBin; b2++) {
            var frac = weights[b2 - loBin] / weightSum;
            totalVol[b2] += v * frac;
            buyVol[b2] += bv * frac;
        }
    }

    // POC
    var maxVol = 0, pocIdx = 0;
    for (i = 0; i < numBins; i++) {
        if (totalVol[i] > maxVol) { maxVol = totalVol[i]; pocIdx = i; }
    }
    if (maxVol <= 0) return null;

    var pocPrice = priceLow + (pocIdx + 0.5) * binSize;

    // VAH/VAL: CME algorithm — expand from POC until 70% captured
    var totalAll = 0;
    for (i = 0; i < numBins; i++) totalAll += totalVol[i];
    var target = totalAll * 0.70;
    var acc = totalVol[pocIdx];
    var loIdx = pocIdx, hiIdx = pocIdx;

    while (acc < target && (loIdx > 0 || hiIdx < numBins - 1)) {
        var upVol = hiIdx < numBins - 1 ? totalVol[hiIdx + 1] : 0;
        var dnVol = loIdx > 0 ? totalVol[loIdx - 1] : 0;
        if (upVol >= dnVol && hiIdx < numBins - 1) {
            hiIdx++;
            acc += totalVol[hiIdx];
        } else if (loIdx > 0) {
            loIdx--;
            acc += totalVol[loIdx];
        } else {
            hiIdx++;
            acc += totalVol[hiIdx];
        }
    }

    var vah = priceLow + (hiIdx + 1) * binSize;
    var val = priceLow + loIdx * binSize;

    // Build bins (only non-zero)
    var bins = [];
    for (i = 0; i < numBins; i++) {
        if (totalVol[i] > 0) {
            bins.push({
                price: priceLow + (i + 0.5) * binSize,
                total: totalVol[i],
                buy: buyVol[i],
                sell: totalVol[i] - buyVol[i]
            });
        }
    }

    return {
        bins: bins,
        poc: pocPrice,
        vah: vah,
        val: val,
        max_vol: maxVol,
        bin_size: binSize
    };
}

// ============================================================
// Register native indicator at load time (before chart exists)
// ============================================================
(function vpRegisterOnLoad() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'VP_NATIVE',
            shortName: '',
            precision: 8,
            figures: [],
            calc: function(dataList) {
                return dataList.map(function() { return {}; });
            },
            draw: function(params) {
                // VP: compute from visible candles and draw.
                // (FRVP is a standard 'frvp' overlay, drawn elsewhere.)
                if (klineActiveIndicators && klineActiveIndicators.VP) {
                    _vpComputeAndDraw(params);
                }
                // Naked POC: piggy-backs on this draw callback —
                // no separate klinecharts indicator needed.
                if (klineActiveIndicators && klineActiveIndicators.NPOC && typeof _npocDrawNative === 'function') _npocDrawNative(params);
                return true;
            }
        });
        _vpRegistered = true;
    } catch (e) {
        console.warn('[VP] registerIndicator failed:', e);
    }
})();

// ============================================================
// Ensure VP_NATIVE indicator exists on candle pane
// Called from chart init (always) — needed for FRVP to work
// ============================================================
function vpEnsureIndicator() {
    if (!klineChart || _vpIndicatorCreated) return;
    if (!_vpRegistered) return;
    try {
        klineChart.createIndicator('VP_NATIVE', true, { id: 'candle_pane' });
        _vpIndicatorCreated = true;
    } catch (e) {}
}

// ============================================================
// Start / Stop VP (now just triggers redraw, no network)
// ============================================================
function vpStart() {
    if (!klineChart) return;
    vpEnsureIndicator();
    _vpData = null;
    _vpLastVisKey = null;
    // No explicit redraw — draw callback computes VP on next natural repaint
}

function vpStop() {
    _vpData = null;
    _vpLastVisKey = null;
    // No explicit redraw needed — draw callback checks _vpData and skips if null
}

// ============================================================
// VP compute + draw — called every frame from draw callback
// ============================================================
function _vpComputeAndDraw(params) {
    if (!klineChart) return;

    var dataList = params.kLineDataList || params.dataList;
    var visibleRange = params.visibleRange;
    if (!dataList || !visibleRange) return;

    var startIdx = visibleRange.from != null ? visibleRange.from : visibleRange.realFrom;
    var endIdx = visibleRange.to != null ? visibleRange.to : visibleRange.realTo;
    if (startIdx == null || endIdx == null || endIdx <= startIdx) return;

    // Cache key: recompute only when visible range changes
    var visKey = startIdx + ':' + endIdx;
    if (visKey !== _vpLastVisKey) {
        _vpLastVisKey = visKey;
        _vpData = _vpComputeFromCandles(dataList, startIdx, endIdx);
    }

    if (_vpData) _vpDrawNative(params);
}

// ============================================================
// VP draw — full viewport histogram (only when VP toggled on)
// ============================================================
function _vpDrawNative(params) {
    if (!_vpData || !_vpData.bins || !_vpData.bins.length) return;

    var ctx = params.ctx;
    var bounding = params.bounding;
    var yAxis = params.yAxis;
    if (!ctx || !bounding || !yAxis) return;

    var cw = bounding.width;
    var maxBarW = cw * 0.25;
    var maxVol = _vpData.max_vol;
    if (!maxVol || maxVol <= 0) return;

    var bins = _vpData.bins;
    var binSize = _vpData.bin_size || 0;
    if (binSize <= 0) return;

    // VP data is raw-space; the axis maps ln(price) under log scale
    var _vpLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    function _vpY(p) { return yAxis.convertToPixel(_vpLog && p > 0 ? Math.log(p) : p); }

    ctx.save();

    for (var i = 0; i < bins.length; i++) {
        var bin = bins[i];
        var halfBin = binSize / 2;

        var yTop = _vpY(bin.price + halfBin);
        var yBot = _vpY(bin.price - halfBin);
        if (yTop == null || yBot == null || !isFinite(yTop) || !isFinite(yBot)) continue;

        var binH = Math.max(1, Math.abs(yBot - yTop));
        var y = Math.min(yTop, yBot);
        if (y + binH < 0 || y > bounding.height) continue;

        var ratio = bin.total / maxVol;
        var barW = ratio * maxBarW;

        var buyRatio = bin.buy / bin.total;
        var buyW = barW * buyRatio;
        var sellW = barW - buyW;

        if (buyW > 0.5) {
            ctx.fillStyle = 'rgba(66, 165, 245, 0.22)';
            ctx.fillRect(0, y, buyW, binH);
        }
        if (sellW > 0.5) {
            ctx.fillStyle = 'rgba(160, 130, 175, 0.22)';
            ctx.fillRect(buyW, y, sellW, binH);
        }
    }

    // POC line
    if (_vpData.poc) {
        var pocY = _vpY(_vpData.poc);
        if (pocY != null && isFinite(pocY)) {
            ctx.strokeStyle = 'rgba(255, 213, 79, 0.85)';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([]);
            ctx.beginPath();
            ctx.moveTo(0, pocY);
            ctx.lineTo(cw, pocY);
            ctx.stroke();
        }
    }

    // VAH / VAL lines
    ctx.strokeStyle = 'rgba(255, 213, 79, 0.3)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    if (_vpData.vah) {
        var vahY = _vpY(_vpData.vah);
        if (vahY != null && isFinite(vahY)) {
            ctx.beginPath(); ctx.moveTo(0, vahY); ctx.lineTo(cw, vahY); ctx.stroke();
        }
    }
    if (_vpData.val) {
        var valY = _vpY(_vpData.val);
        if (valY != null && isFinite(valY)) {
            ctx.beginPath(); ctx.moveTo(0, valY); ctx.lineTo(cw, valY); ctx.stroke();
        }
    }
    ctx.setLineDash([]);

    // Labels (left) + price labels docked at the axis edge (right)
    ctx.font = '9px JetBrains Mono, monospace';
    var _fmtP = (typeof _frvpFmtPrice === 'function') ? _frvpFmtPrice : function(p) { return String(p); };
    function _vpLabel(price, name) {
        var y = _vpY(price);
        if (y == null || !isFinite(y)) return;
        ctx.textAlign = 'left';
        ctx.fillStyle = 'rgba(255, 213, 79, 0.7)';
        ctx.fillText(name, 4, y - 3);
        ctx.textAlign = 'right';
        ctx.fillText(name + ' ' + _fmtP(price), cw - 4, y - 3);
        ctx.textAlign = 'left';
    }
    if (_vpData.poc) _vpLabel(_vpData.poc, 'POC');
    if (_vpData.vah) _vpLabel(_vpData.vah, 'VAH');
    if (_vpData.val) _vpLabel(_vpData.val, 'VAL');

    ctx.restore();
}

// ============================================================
// Lifecycle hooks (called from chart-modal.js)
// ============================================================
function vpOnChartClose() {
    vpStop();
    _vpIndicatorCreated = false;
    if (typeof frvpOnChartClose === 'function') frvpOnChartClose();
}

function vpOnDataChange() {
    // Chart may have been destroyed and recreated (full render path)
    _vpIndicatorCreated = false;
    vpEnsureIndicator();
    _vpData = null;
    _vpLastVisKey = null;
    if (typeof npocOnDataChange === 'function') npocOnDataChange();
}

