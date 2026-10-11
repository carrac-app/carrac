/* Key Levels + Monday Range — Native KLineChart Indicators
 *
 * KEY_LEVELS: Dashed lines for PDH/PDL, PWH/PWL, PMH/PML anchored
 *             after the last real candle with labels at the right end.
 *
 * MON_RANGE:  Solid lines for Monday High/Low originating from the
 *             Monday candles, extending to the Key Levels anchor point.
 *
 * Both share a single KL_NATIVE registered indicator on candle_pane.
 */

// ============================================================
// State
// ============================================================
var _klData = null;
var _klLastKey = null;
var _klRegistered = false;
var _klIndicatorCreated = false;

// ============================================================
// Compute key levels from full chart dataset
// ============================================================
function _klComputeLevels(dataList) {
    if (!dataList || dataList.length < 2) return null;

    function toMs(ts) { return ts < 1e12 ? ts * 1000 : ts; }
    function utcDayStart(ms) {
        var d = new Date(ms);
        return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    }

    // Find last REAL bar (skip phantom future bars)
    var realEnd = (dataList._realLength || dataList.length) - 1;
    while (realEnd >= 0 && dataList[realEnd]._phantom) realEnd--;
    if (realEnd < 1) return null;

    var lastBar = dataList[realEnd];
    var lastMs = toMs(lastBar.timestamp);
    var lastDS = utcDayStart(lastMs);

    // Current week Monday (ISO: Mon=start)
    var lastDow = new Date(lastDS).getUTCDay();
    var daysToMon = lastDow === 0 ? 6 : lastDow - 1;
    var curWeekMon = lastDS - daysToMon * 86400000;
    var prevWeekMon = curWeekMon - 7 * 86400000;

    // Month boundaries
    var ld = new Date(lastMs);
    var curMonthStart = Date.UTC(ld.getUTCFullYear(), ld.getUTCMonth(), 1);
    var pmDate = new Date(curMonthStart - 1);
    var prevMonthStart = Date.UTC(pmDate.getUTCFullYear(), pmDate.getUTCMonth(), 1);

    // Tracking
    var pdH = -Infinity, pdL = Infinity, prevDayDS = null;
    var mH = -Infinity, mL = Infinity, mondayDS = null, mondayFirstIdx = -1;
    var pwH = -Infinity, pwL = Infinity;
    var pmH = -Infinity, pmL = Infinity;

    for (var i = realEnd; i >= 0; i--) {
        var bar = dataList[i];
        if (bar._phantom || bar.high == null || bar.low == null) continue;

        var barMs = toMs(bar.timestamp);
        var barDS = utcDayStart(barMs);
        var barDow = new Date(barDS).getUTCDay();

        // Previous Day H/L: first complete day before the last bar's day
        if (barDS < lastDS) {
            if (prevDayDS === null) prevDayDS = barDS;
            if (barDS === prevDayDS) {
                if (bar.high > pdH) pdH = bar.high;
                if (bar.low < pdL) pdL = bar.low;
            }
        }

        // Monday H/L: most recent Monday in the data
        if (barDow === 1) {
            if (mondayDS === null) mondayDS = barDS;
            if (barDS === mondayDS) {
                if (bar.high > mH) mH = bar.high;
                if (bar.low < mL) mL = bar.low;
                mondayFirstIdx = i; // keeps updating to earliest bar on Monday
            }
        }

        // Previous Week H/L (Mon–Sun before current week)
        if (barDS >= prevWeekMon && barDS < curWeekMon) {
            if (bar.high > pwH) pwH = bar.high;
            if (bar.low < pwL) pwL = bar.low;
        }

        // Previous Month H/L
        if (barDS >= prevMonthStart && barDS < curMonthStart) {
            if (bar.high > pmH) pmH = bar.high;
            if (bar.low < pmL) pmL = bar.low;
        }

        // Early exit: past prev month and all nearer periods found
        if (barDS < prevMonthStart && prevDayDS !== null && mondayDS !== null) break;
    }

    var result = { realEnd: realEnd };
    if (pdH > -Infinity) { result.pdh = pdH; result.pdl = pdL; }
    if (mH > -Infinity) { result.mh = mH; result.ml = mL; result.mondayFirstIdx = mondayFirstIdx; }
    if (pwH > -Infinity) { result.pwh = pwH; result.pwl = pwL; }
    if (pmH > -Infinity) { result.pmh = pmH; result.pml = pmL; }

    return Object.keys(result).length > 1 ? result : null; // >1 because realEnd is always present
}

// ============================================================
// Register native indicator at load time (before chart exists)
// ============================================================
(function klRegisterOnLoad() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'KL_NATIVE',
            shortName: '',
            precision: 8,
            figures: [],
            calc: function(dataList) {
                return dataList.map(function() { return {}; });
            },
            draw: function(params) {
                var needKL = klineActiveIndicators && klineActiveIndicators.KEY_LEVELS;
                var needMR = klineActiveIndicators && klineActiveIndicators.MON_RANGE;
                if (needKL || needMR) {
                    _klComputeAndDraw(params, needKL, needMR);
                }
                return true;
            }
        });
        _klRegistered = true;
    } catch (e) {
        console.warn('[KL] registerIndicator failed:', e);
    }
})();

// ============================================================
// Ensure KL_NATIVE indicator exists on candle pane
// ============================================================
function klEnsureIndicator() {
    if (!klineChart || _klIndicatorCreated) return;
    if (!_klRegistered) return;
    try {
        klineChart.createIndicator('KL_NATIVE', true, { id: 'candle_pane' });
        _klIndicatorCreated = true;
    } catch (e) {}
}

// ============================================================
// Start / Stop
// ============================================================
function klStart() {
    if (!klineChart) return;
    klEnsureIndicator();
    _klData = null;
    _klLastKey = null;
}

function klStop() {
    _klData = null;
    _klLastKey = null;
}

// ============================================================
// Compute + Draw — called every frame from draw callback
// ============================================================
function _klComputeAndDraw(params, drawKL, drawMR) {
    if (!klineChart) return;

    var dataList = params.kLineDataList || params.dataList;
    if (!dataList || dataList.length < 2) return;

    // Cache: recompute only when dataset changes (use real bar count)
    var realLen = dataList._realLength || dataList.length;
    var cacheKey = realLen + ':' + dataList[realLen - 1].timestamp;
    if (cacheKey !== _klLastKey) {
        _klLastKey = cacheKey;
        _klData = _klComputeLevels(dataList);
    }

    if (!_klData) return;

    // Shared anchor: x-pixel of last real bar
    var lastPx = klineChart.convertToPixel({ dataIndex: _klData.realEnd, value: 0 }, { paneId: 'candle_pane' });
    if (!lastPx) return;

    var gap = 40;
    var lineLen = 100;
    var anchorX = lastPx.x + gap; // where Key Levels lines start / Monday lines end

    if (drawMR) _klDrawMondayRange(params, anchorX + lineLen);
    if (drawKL) _klDrawKeyLevels(params, anchorX, lineLen);
}

// ============================================================
// Draw Key Levels — dashed lines after last candle
// ============================================================
function _klDrawKeyLevels(params, startX, lineLen) {
    var ctx = params.ctx;
    var bounding = params.bounding;
    var yAxis = params.yAxis;
    if (!ctx || !bounding || !yAxis) return;

    var endX = startX + lineLen;

    var blueLine  = 'rgba(66, 165, 245, 0.6)';
    var blueLabel = 'rgba(66, 165, 245, 0.8)';

    var levels = [
        ['pdh', 'PDH'],
        ['pdl', 'PDL'],
        ['pwh', 'PWH'],
        ['pwl', 'PWL'],
        ['pmh', 'PMH'],
        ['pml', 'PML']
    ];

    ctx.save();
    ctx.font = '10px JetBrains Mono, monospace';

    // Build resolved list
    var resolved = [];
    for (var i = 0; i < levels.length; i++) {
        var price = _klData[levels[i][0]];
        if (price == null) continue;
        var y = yAxis.convertToPixel(price);
        if (y == null || !isFinite(y)) continue;
        if (y < -50 || y > bounding.height + 50) continue;
        resolved.push({ y: y, label: levels[i][1] });
    }

    // Draw dashed lines
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = blueLine;
    for (var i = 0; i < resolved.length; i++) {
        ctx.beginPath();
        ctx.moveTo(startX, resolved[i].y);
        ctx.lineTo(endX, resolved[i].y);
        ctx.stroke();
    }

    // Group labels within 12px
    resolved.sort(function(a, b) { return a.y - b.y; });
    var groups = [];
    for (var i = 0; i < resolved.length; i++) {
        var last = groups.length ? groups[groups.length - 1] : null;
        if (last && Math.abs(resolved[i].y - last.y) < 12) {
            last.labels.push(resolved[i].label);
        } else {
            groups.push({ y: resolved[i].y, labels: [resolved[i].label] });
        }
    }

    // Draw labels at line end
    ctx.fillStyle = blueLabel;
    for (var i = 0; i < groups.length; i++) {
        ctx.fillText(groups[i].labels.join('·'), endX + 5, groups[i].y + 4);
    }

    ctx.restore();
}

// ============================================================
// Draw Monday Range — solid lines from Monday candles to anchor
// ============================================================
function _klDrawMondayRange(params, anchorX) {
    if (_klData.mh == null || _klData.mondayFirstIdx == null) return;

    var ctx = params.ctx;
    var bounding = params.bounding;
    var yAxis = params.yAxis;
    if (!ctx || !bounding || !yAxis || !klineChart) return;

    // Get x-pixel of first Monday bar
    var monPx = klineChart.convertToPixel({ dataIndex: _klData.mondayFirstIdx, value: 0 }, { paneId: 'candle_pane' });
    if (!monPx) return;

    var startX = monPx.x;
    var endX = anchorX; // far edge of Key Levels lines

    // Don't draw if Monday is off-screen to the right or line would be negative width
    if (startX >= endX) return;

    var lineColor = 'rgba(120, 190, 255, 0.45)';
    var labelColor = 'rgba(120, 190, 255, 0.65)';

    var mhY = yAxis.convertToPixel(_klData.mh);
    var mlY = yAxis.convertToPixel(_klData.ml);

    ctx.save();
    ctx.lineWidth = 1.5;
    ctx.setLineDash([]);
    ctx.strokeStyle = lineColor;
    ctx.font = '10px JetBrains Mono, monospace';

    // Draw MH line
    if (mhY != null && isFinite(mhY) && mhY > -50 && mhY < bounding.height + 50) {
        ctx.beginPath();
        ctx.moveTo(startX, mhY);
        ctx.lineTo(endX, mhY);
        ctx.stroke();
    }

    // Draw ML line
    if (mlY != null && isFinite(mlY) && mlY > -50 && mlY < bounding.height + 50) {
        ctx.beginPath();
        ctx.moveTo(startX, mlY);
        ctx.lineTo(endX, mlY);
        ctx.stroke();
    }

    // Labels at anchor end (right side, same position as key levels labels)
    ctx.fillStyle = labelColor;
    if (mhY != null && isFinite(mhY) && mlY != null && isFinite(mlY) && Math.abs(mhY - mlY) < 12) {
        // Merged label
        var avgY = (mhY + mlY) / 2;
        ctx.fillText('MH·ML', endX + 5, avgY + 4);
    } else {
        if (mhY != null && isFinite(mhY)) ctx.fillText('MH', endX + 5, mhY + 4);
        if (mlY != null && isFinite(mlY)) ctx.fillText('ML', endX + 5, mlY + 4);
    }

    ctx.restore();
}

// ============================================================
// Lifecycle hooks (called from chart-modal.js)
// ============================================================
function klOnChartClose() {
    klStop();
    _klIndicatorCreated = false;
}

function klOnDataChange() {
    _klIndicatorCreated = false;
    klEnsureIndicator();
    _klData = null;
    _klLastKey = null;
}
