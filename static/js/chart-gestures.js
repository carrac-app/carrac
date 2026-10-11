/* chart-gestures.js — keyboard and touch navigation for the kline chart.
 *
 * Arrow-key pan/zoom, the Y-axis and X-axis touch-drag zoom overlays, and the
 * mobile crosshair + single-finger pan.
 *
 * LOAD ORDER: after klinecharts.min.js, before the chart-app files. Nothing here runs at
 * module scope — it only declares — so the ordering is about `klineChart` and
 * friends existing by the time a gesture actually fires, not about load time.
 *
 * Everything below shares the chart app's global scope. These are
 * deliberately NOT wrapped in an IIFE: other chart-app files and
 * mobile-draw.js reach several of these by bare name, and hiding them would
 * turn a working page into a silent no-op.
 *
 * If you add a script tag for this file, add it to BOTH templates/index.html
 * AND templates/chart_standalone.html. Missing the second one leaves the
 * standalone chart page without gestures.
 */

// ============= Arrow key chart navigation =============
var _nav = { left: false, right: false, up: false, down: false, raf: null, speed: 0, yPanSpeed: 0 };
var _yGapOffset = 0; // legacy, kept for G/J key gap reset

// Reset Y-axis to auto-scale by toggling internal _autoCalcTickFlag (KLineChart v9.8.12)
function resetYAxisScale() {
    if (!klineChart) return;
    try {
        var axis = klineChart._candlePane.getAxisComponent();
        if (!axis.getAutoCalcTickFlag()) {
            axis.setAutoCalcTickFlag(true);
            klineChart.adjustPaneViewport(false, true, true, true);
        }
    } catch (e) {}
}

// Auto-size yAxis width based on the widest element that lives in the axis area:
// tick labels, crosshair badge, last-price badge, and the countdown timer overlay.
function autoSizeYAxis() {
    if (!klineChart) return;
    try {
        var dataList = klineChart.getDataList();
        if (!dataList || dataList.length === 0) return;
        // Find max price in dataset (determines widest tick label)
        var maxPrice = 0;
        for (var i = 0; i < dataList.length; i++) {
            if (dataList[i].high > maxPrice) maxPrice = dataList[i].high;
        }

        // Delegates to module-level _fmtPriceSmart with explicit decimals
        function fmtPrice(p, decimals) {
            var s = p.toFixed(decimals);
            var parts = s.split('.');
            parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            return parts.join('.');
        }
        // Note: this local 2-param variant intentionally kept (needs explicit decimals for Y-axis sizing)

        // Tick label: same format as customApi.formatPrice + thousands sep
        var tickLabel;
        if (maxPrice >= 100) tickLabel = fmtPrice(maxPrice, 2);
        else if (maxPrice >= 1) tickLabel = fmtPrice(maxPrice, 2);
        else if (maxPrice >= 0.01) tickLabel = maxPrice.toFixed(4);
        else tickLabel = maxPrice.toPrecision(4);

        // Crosshair badge: may show more decimals (KLineChart auto-precision)
        var crossLabel = tickLabel;
        if (maxPrice >= 100) {
            var cl = fmtPrice(maxPrice, 2);
            if (cl.length > crossLabel.length) crossLabel = cl;
        }

        // Countdown price row: uses toLocaleString with specific formatting
        var countdownLabel;
        if (maxPrice >= 100) countdownLabel = fmtPrice(maxPrice, 2);
        else if (maxPrice >= 1) countdownLabel = fmtPrice(maxPrice, 2);
        else countdownLabel = maxPrice.toFixed(4);

        // Countdown timer row: worst case "23:59:59" (8 chars) or "30d 23:59" (9 chars)
        var timerLabel = '30d 23:59';

        // Measure all candidates
        var canvas = document.createElement('canvas');
        var ctx = canvas.getContext('2d');
        var font = typeof chartFont !== 'undefined' ? chartFont : 'Helvetica Neue';
        var monoFont = "'JetBrains Mono', monospace";

        // Tick/crosshair text uses chartFont at 11px
        ctx.font = '11px ' + font;
        var tickW = ctx.measureText(tickLabel).width;
        var crossW = ctx.measureText(crossLabel).width;

        // Countdown uses JetBrains Mono at 11px
        ctx.font = '11px ' + monoFont;
        var countdownPriceW = ctx.measureText(countdownLabel).width;
        var countdownTimerW = ctx.measureText(timerLabel).width;

        // Compute required width for each element:
        // Tick label: marginStart(2) + text + marginEnd(4) + tickLine(3) + axisLine(1) = text + 10
        var needTick = tickW + 10;
        // Crosshair badge: same as tick + badge padding (paddingLeft 4 + paddingRight 4 + border 2) = text + 20
        var needCross = crossW + 20;
        // Countdown: padding (4+4) + border (2+2 for timer row) = text + 12
        var needCountdownPrice = countdownPriceW + 12;
        var needCountdownTimer = countdownTimerW + 12;

        var size = Math.ceil(Math.max(needTick, needCross, needCountdownPrice, needCountdownTimer));
        // Clamp: min 50, max 100
        size = Math.max(50, Math.min(100, size));
        klineChart.setStyles({ yAxis: { size: size } });
    } catch(e) {}
}

function _navTick() {
    var anyKey = _nav.left || _nav.right || _nav.up || _nav.down;
    if (!anyKey) {
        _nav.raf = null;
        _nav.speed = 0; _nav.yPanSpeed = 0;
        return;
    }
    if (!klineChart) { _nav.raf = null; return; }
    // Left/Right: horizontal pan
    if (_nav.left || _nav.right) {
        _nav.speed = Math.min(240, _nav.speed + 12);
        if (_nav.left) klineChart.scrollByDistance(_nav.speed);
        if (_nav.right) klineChart.scrollByDistance(-_nav.speed);
    } else { _nav.speed = 0; }
    // Up/Down: free Y-axis pan via setRange (no constraints, true drag feel)
    if (_nav.up || _nav.down) {
        _nav.yPanSpeed = Math.min(0.08, _nav.yPanSpeed + 0.006);
        try {
            var axis = klineChart._candlePane.getAxisComponent();
            var r = axis.getRange();
            var shift = r.range * _nav.yPanSpeed;
            if (_nav.down) shift = -shift;
            axis.setRange({
                from: r.from + shift, to: r.to + shift, range: r.range,
                realFrom: r.realFrom + shift, realTo: r.realTo + shift, realRange: r.realRange,
                displayFrom: r.displayFrom + shift, displayTo: r.displayTo + shift, displayRange: r.displayRange
            });
            klineChart.adjustPaneViewport(false, true, true, true);
        } catch(ex) {}
    } else { _nav.yPanSpeed = 0; }
    _nav.raf = requestAnimationFrame(_navTick);
}


function _navStart(dir) {
    _nav[dir] = true;
    if (!_nav.raf) _nav.raf = requestAnimationFrame(_navTick);
}

function _navStop(dir) {
    _nav[dir] = false;
    if (!_nav.left && !_nav.right) _nav.speed = 0;
    if (!_nav.up && !_nav.down) _nav.yPanSpeed = 0;
}

// ============= Y-axis touch drag zoom (mobile) =============
// KLineChart v9.8.12 ignores touch events on Y-axis area (dead zone).
// This overlay captures single-finger vertical drags on the rightmost 60px
// and uses axis.getRange()/setRange() to zoom the price axis (same API as arrow keys).
var _yTouch = { active: false, lastY: 0, pointerId: null, pane: null, dyAccum: 0, raf: null };
var _activeTouchCount = 0; // Track concurrent touches to detect pinch
var Y_AXIS_WIDTH = 60; // px from right edge

function _findPaneAtY(touchY, container) {
    // Find which pane the touch Y coordinate falls in
    if (!klineChart) return null;
    try {
        var drawPanes = klineChart.getAllDrawPanes();
        if (!drawPanes) return null;
        var containerRect = container.getBoundingClientRect();
        var localY = touchY - containerRect.top;
        for (var i = 0; i < drawPanes.length; i++) {
            var b = drawPanes[i].getBounding();
            if (localY >= b.top && localY <= b.top + b.height) {
                return drawPanes[i];
            }
        }
    } catch(ex) {}
    return null;
}

function setupYAxisTouchZoom() {
    var container = document.getElementById('kline-chart-container');
    if (!container || container._yTouchSetup) return;
    container._yTouchSetup = true;

    // Track touch count to distinguish single-finger drag from pinch
    container.addEventListener('pointerdown', function(e) {
        if (e.pointerType === 'touch' || e.pointerType === 'pen') _activeTouchCount++;
    });
    container.addEventListener('pointerup', function(e) {
        if (e.pointerType === 'touch' || e.pointerType === 'pen') _activeTouchCount = Math.max(0, _activeTouchCount - 1);
    });
    container.addEventListener('pointercancel', function(e) {
        if (e.pointerType === 'touch' || e.pointerType === 'pen') _activeTouchCount = Math.max(0, _activeTouchCount - 1);
    });

    container.addEventListener('pointerdown', function(e) {
        if (_yTouch.active) return;
        // Only activate for single-finger touch (not part of pinch)
        if (_activeTouchCount > 1) return;
        var rect = container.getBoundingClientRect();
        var xFromRight = rect.right - e.clientX;
        if (xFromRight > Y_AXIS_WIDTH || xFromRight < 0) return;
        // Let A/L scale toggle buttons receive taps
        var tgt = e.target;
        if (tgt && (tgt.classList.contains('yaxis-scale-btn') || (tgt.parentElement && tgt.parentElement.classList.contains('yaxis-scale-toggle')))) return;
        // Mouse too (left button only) — same pointer-capture upgrade as the
        // X axis: the engine's native y-rescale drops the drag the moment the
        // cursor leaves the axis strip.
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        if (e.pointerType === 'touch' || e.pointerType === 'pen' || e.pointerType === 'mouse') {
            if (_crosshairMode) _exitCrosshairMode();
            // Switch to this pane if in multi-chart mode (desktop only)
            if (typeof _mcInitialized !== 'undefined' && _mcInitialized && typeof mcActivatePaneForContainer === 'function') mcActivatePaneForContainer(container);
            var _ch = (typeof _mcInitialized !== 'undefined' && _mcInitialized && typeof mcGetChartForContainer === 'function') ? mcGetChartForContainer(container) : klineChart;
            // Find which pane the touch is in
            var pane = _findPaneAtY(e.clientY, container);
            if (!pane) return;
            _yTouch.active = true;
            _yTouch.lastY = e.clientY;
            _yTouch.pointerId = e.pointerId;
            _yTouch.pane = pane;
            _yTouch.chart = _ch;
            // Disable auto-scaling so KLineChart doesn't fight our manual range
            try {
                pane.getAxisComponent().setAutoCalcTickFlag(false);
            } catch(ex) {}
            try { container.setPointerCapture(e.pointerId); } catch(ex) {}
            e.preventDefault();
            e.stopPropagation();
        }
    }, { passive: false });

    container.addEventListener('pointermove', function(e) {
        if (!_yTouch.active || e.pointerId !== _yTouch.pointerId) return;
        if (_activeTouchCount > 1) {
            _yTouch.active = false;
            _yTouch.pointerId = null;
            _yTouch.pane = null;
            _yTouch.chart = null;
            try { container.releasePointerCapture(e.pointerId); } catch(ex) {}
            return;
        }
        var dy = e.clientY - _yTouch.lastY;
        _yTouch.lastY = e.clientY;
        try {
            var axis = _yTouch.pane.getAxisComponent();
            var r = axis.getRange();
            var factor = dy * 0.006;
            var mid = (r.realFrom + r.realTo) / 2;
            var halfRange = r.realRange / 2;
            var newHalf = halfRange * (1 + factor);
            if (newHalf < halfRange * 0.02) return;
            var newFrom = mid - newHalf;
            var newTo = mid + newHalf;
            axis.setRange({
                from: newFrom, to: newTo, range: newTo - newFrom,
                realFrom: newFrom, realTo: newTo, realRange: newTo - newFrom,
                displayFrom: newFrom, displayTo: newTo, displayRange: newTo - newFrom
            });
            (_yTouch.chart || klineChart).adjustPaneViewport(false, true, true, false);
        } catch(ex) {}
        e.preventDefault();
        e.stopPropagation();
    }, { passive: false });

    function endYTouch(e) {
        if (!_yTouch.active || e.pointerId !== _yTouch.pointerId) return;
        _yTouch.active = false;
        _yTouch.pointerId = null;
        _yTouch.pane = null;
        _yTouch.chart = null;
        try { container.releasePointerCapture(e.pointerId); } catch(ex) {}
    }
    container.addEventListener('pointerup', endYTouch);
    container.addEventListener('pointercancel', endYTouch);
}

// ============= X-axis touch drag zoom (mobile) =============
// Horizontal drag on bottom X-axis area to zoom candle width in/out.
var _xTouch = { active: false, lastX: 0, pointerId: null };
var X_AXIS_HEIGHT = 40; // px from bottom edge

function setupXAxisTouchZoom() {
    var container = document.getElementById('kline-chart-container');
    if (!container || container._xTouchSetup) return;
    container._xTouchSetup = true;

    container.addEventListener('pointerdown', function(e) {
        if (_xTouch.active) return;
        if (_activeTouchCount > 1) return;
        var rect = container.getBoundingClientRect();
        var yFromBottom = rect.bottom - e.clientY;
        if (yFromBottom > X_AXIS_HEIGHT || yFromBottom < 0) return;
        // Mouse too (left button only): the engine's own axis drag needs the
        // cursor to STAY inside its ~23px strip every move — this handler
        // pointer-captures, so once grabbed the drag works from anywhere.
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        if (e.pointerType === 'touch' || e.pointerType === 'pen' || e.pointerType === 'mouse') {
            if (_crosshairMode) _exitCrosshairMode();
            if (typeof _mcInitialized !== 'undefined' && _mcInitialized && typeof mcActivatePaneForContainer === 'function') mcActivatePaneForContainer(container);
            _xTouch.active = true;
            _xTouch.lastX = e.clientX;
            _xTouch.pointerId = e.pointerId;
            _xTouch.chart = (typeof _mcInitialized !== 'undefined' && _mcInitialized && typeof mcGetChartForContainer === 'function') ? mcGetChartForContainer(container) : klineChart;
            try { container.setPointerCapture(e.pointerId); } catch(ex) {}
            e.preventDefault();
            e.stopPropagation();
        }
    }, { passive: false });

    container.addEventListener('pointermove', function(e) {
        if (!_xTouch.active || e.pointerId !== _xTouch.pointerId) return;
        if (_activeTouchCount > 1) {
            _xTouch.active = false;
            _xTouch.pointerId = null;
            _xTouch.chart = null;
            try { container.releasePointerCapture(e.pointerId); } catch(ex) {}
            return;
        }
        var dx = e.clientX - _xTouch.lastX;
        _xTouch.lastX = e.clientX;
        // Drag left = zoom in (wider candles), drag right = zoom out (narrower candles)
        if (Math.abs(dx) > 0) {
            var factor = 1 - dx * 0.008;
            factor = Math.max(0.9, Math.min(1.1, factor));
            try { (_xTouch.chart || klineChart).zoomAtCoordinate(factor); } catch(ex) {}
        }
        e.preventDefault();
        e.stopPropagation();
    }, { passive: false });

    function endXTouch(e) {
        if (!_xTouch.active || e.pointerId !== _xTouch.pointerId) return;
        _xTouch.active = false;
        _xTouch.pointerId = null;
        _xTouch.chart = null;
        try { container.releasePointerCapture(e.pointerId); } catch(ex) {}
    }
    container.addEventListener('pointerup', endXTouch);
    container.addEventListener('pointercancel', endXTouch);
}

// ============= Main chart touch pan (mobile) =============
// Single-finger drag on the main chart area to pan horizontally and vertically.
// KLineChart v9.8.12 uses touchstart/touchmove (NOT pointer events), so we must
// intercept TOUCH events in capture phase to prevent KLineChart's internal scroll.
var _chartTouch = { active: false, pending: false, panning: false, lastX: 0, lastY: 0, startX: 0, startY: 0, accumOffset: 0 };
var PAN_THRESHOLD = 6; // px of movement before committing to pan (vs tap for crosshair)
// Crosshair mode: when active, drags move the crosshair instead of panning
var _crosshairMode = false;
var _crosshairPos = null; // {x, y} in chart-relative coords — survives axis manipulations
var _lastTapTime = 0; // for double-tap detection

function _mobileCrosshairUpdate(x, y, container) {
    // Place crosshair at (x,y) relative to container, update OHLCV + color
    if (!klineChart) return;
    _crosshairPos = { x: x, y: y };
    try {
        klineChart._chartStore.getTooltipStore().setCrosshair({ x: x, y: y, paneId: 'candle_pane' });
        klineChart.updatePane(1);
    } catch(ex) {}
    // Look up candle data at this position and update OHLCV display + crosshair color
    _mobileCrosshairDataUpdate(x, y, container);
}

function _mobileCrosshairDataUpdate(x, y, container) {
    if (!klineChart) return;
    try {
        var point = klineChart.convertFromPixel({ x: x, y: y }, { paneId: 'candle_pane' });
        if (!point || point.dataIndex == null) return;
        var dataList = klineChart.getDataList();
        var candle = dataList && dataList[point.dataIndex];
        if (!candle) return;
        // Update OHLCV display
        var el = document.getElementById('mobile-ohlcv');
        if (el) {
            el.textContent = 'O:' + _mFmtP(candle.open) + ' H:' + _mFmtP(candle.high) +
                ' L:' + _mFmtP(candle.low) + ' C:' + _mFmtP(candle.close) + ' V:' + _mFmtV(candle.volume);
        }
        // Update crosshair color (green/red inside candle body, gray outside)
        var cursorPrice = point.value;
        var bodyHigh = Math.max(candle.open, candle.close);
        var bodyLow = Math.min(candle.open, candle.close);
        if (cursorPrice !== undefined && cursorPrice >= bodyLow && cursorPrice <= bodyHigh) {
            _mobileCrosshairColor(candle.close >= candle.open ? '#26a69a' : '#ef5350');
        } else {
            _mobileCrosshairColor('#787b86');
        }
    } catch(ex) {}
}

var _mLastCrosshairColor = '#787b86';
function _mobileCrosshairColor(color) {
    if (color === _mLastCrosshairColor) return;
    _mLastCrosshairColor = color;
    var isDefault = color === '#787b86';
    var textBg = isDefault ? '#373b42' : color;
    klineChart.setStyles({
        crosshair: {
            horizontal: {
                line: { color: color },
                text: { backgroundColor: textBg, borderColor: textBg }
            },
            vertical: {
                line: { color: color },
                text: { backgroundColor: textBg, borderColor: textBg }
            }
        }
    });
}

function _mFmtP(v) {
    if (v == null) return '-';
    if (_logScaleEnabled && v > 0) v = Math.exp(v);
    if (v >= 10000) return v.toFixed(0);
    if (v >= 100) return v.toFixed(1);
    if (v >= 1) return v.toFixed(2);
    if (v >= 0.01) return v.toFixed(4);
    return v.toPrecision(4);
}
function _mFmtV(v) {
    if (v == null) return '-';
    if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return v.toFixed(0);
}

// Last REAL bar (phantom-skipped). Phantom bars pad the axis with future/blank
// timestamps and carry NO volume, so reading dataList[len-1] shows a dash for V
// and stale OHLC once the tick path clobbers the tail. Mirrors the phantom-skip
// scan in chart-live-tick.js. The mobile OHLCV/volume readout
// was sticking on the phantom tail.
function _lastRealBar(dl) {
    if (!dl || !dl.length) return null;
    var now = Date.now();
    var i = dl.length - 1;
    while (i > 0 && (dl[i].timestamp > now || dl[i]._phantom)) i--;
    return dl[i];
}

// Format a timestamp for the desktop candle-tooltip idle-legend substitution,
// matching KLineChart's default candle tooltip date format ("YYYY-MM-DD HH:mm",
// local timezone — no custom timezone is configured on the chart).
function _fmtTooltipTime(ts) {
    if (ts == null) return '--';
    var d = new Date(ts);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
        ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
// Format a price the way KLineChart renders the candle-tooltip {open}/{high}/…
// tokens: fixed to the chart's price precision, thousands-separated. No log-exp
// here — the tokens render the raw stored value, so leaving it untransformed
// preserves parity in log-scale mode too.
function _fmtTooltipPrice(v, dec) {
    if (v == null) return '--';
    var parts = Number(v).toFixed(dec).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
}

function _exitCrosshairMode() {
    _crosshairMode = false;
    _crosshairPos = null;
    _mLastCrosshairColor = '#787b86';
    // Clear crosshair from chart
    if (klineChart) {
        try {
            klineChart._chartStore.getTooltipStore().setCrosshair();
            klineChart.updatePane(1);
        } catch(ex) {}
        _mobileCrosshairColor('#787b86');
    }
    // Show latest OHLCV
    var el = document.getElementById('mobile-ohlcv');
    if (el && klineChart) {
        var d = _lastRealBar(klineChart.getDataList());
        if (d) {
            el.textContent = 'O:' + _mFmtP(d.open) + ' H:' + _mFmtP(d.high) +
                ' L:' + _mFmtP(d.low) + ' C:' + _mFmtP(d.close) + ' V:' + _mFmtV(d.volume);
        }
    }
}
