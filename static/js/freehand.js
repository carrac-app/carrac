/* Freehand Drawing Tool — Native KLineChart Indicator
 *
 * Uses the VP_NATIVE pattern: registerIndicator with empty figures + custom draw callback.
 * Freehand paths are stored as regular drawings in localStorage (klineDrawings)
 * with name='freehand' and points=[{timestamp, value}, ...].
 *
 * Ramer-Douglas-Peucker simplification reduces mousemove samples (~hundreds)
 * to ~30-50 points for efficient storage and rendering.
 *
 * Log scale: raw prices stored, Math.log() applied at render boundary.
 */

// ============================================================
// State
// ============================================================
var _freehandRegistered = false;
var _freehandIndicatorCreated = false;
var _freehandActive = false;       // Currently capturing a stroke
var _freehandRawPoints = [];       // Pixel samples during capture: [{x, y}]
var _freehandListeners = null;     // For cleanup
var _freehandPreviewCanvas = null; // Temp canvas for live preview

// ============================================================
// Ramer-Douglas-Peucker simplification
// ============================================================
function _rdpSimplify(points, epsilon) {
    if (points.length <= 2) return points;

    // Find the point with the maximum distance from the line (first → last)
    var first = points[0];
    var last = points[points.length - 1];
    var maxDist = 0;
    var maxIdx = 0;

    var dx = last.x - first.x;
    var dy = last.y - first.y;
    var lenSq = dx * dx + dy * dy;

    for (var i = 1; i < points.length - 1; i++) {
        var dist;
        if (lenSq === 0) {
            // first and last are same point
            var ddx = points[i].x - first.x;
            var ddy = points[i].y - first.y;
            dist = Math.sqrt(ddx * ddx + ddy * ddy);
        } else {
            // Perpendicular distance from point to line
            var t = ((points[i].x - first.x) * dx + (points[i].y - first.y) * dy) / lenSq;
            t = Math.max(0, Math.min(1, t));
            var projX = first.x + t * dx;
            var projY = first.y + t * dy;
            var pdx = points[i].x - projX;
            var pdy = points[i].y - projY;
            dist = Math.sqrt(pdx * pdx + pdy * pdy);
        }
        if (dist > maxDist) {
            maxDist = dist;
            maxIdx = i;
        }
    }

    if (maxDist > epsilon) {
        var left = _rdpSimplify(points.slice(0, maxIdx + 1), epsilon);
        var right = _rdpSimplify(points.slice(maxIdx), epsilon);
        return left.slice(0, left.length - 1).concat(right);
    } else {
        return [first, last];
    }
}

// ============================================================
// Register native indicator (before chart exists)
// ============================================================
(function freehandRegisterOnLoad() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'FREEHAND_NATIVE',
            shortName: '',
            precision: 8,
            figures: [],
            calc: function(dataList) {
                return dataList.map(function() { return {}; });
            },
            draw: function(params) {
                _freehandDrawAll(params);
                return true;
            }
        });
        _freehandRegistered = true;
    } catch (e) {
        console.warn('[Freehand] registerIndicator failed:', e);
    }
})();

// ============================================================
// Ensure indicator exists on candle pane
// ============================================================
function freehandEnsureIndicator() {
    if (!klineChart || _freehandIndicatorCreated) return;
    if (!_freehandRegistered) return;
    try {
        klineChart.createIndicator('FREEHAND_NATIVE', true, { id: 'candle_pane' });
        _freehandIndicatorCreated = true;
    } catch (e) {}
}

// ============================================================
// Draw all saved freehand paths
// ============================================================
// draw() runs on EVERY render frame (including each frame of a drag) — parsing the
// whole multi-symbol klineDrawings blob per frame is pure allocation/GC churn.
// Cache the parsed object and re-parse only when the stored string actually changes.
var _fhLsCache = { raw: null, parsed: null };

function _freehandDrawAll(params) {
    if (!klineChart || !klineChartSymbol) return;
    // Master hide-all (kline-drawings.js) covers freehand's native renderer too
    if (typeof _allDrawingsHidden !== 'undefined' && _allDrawingsHidden) return;

    var ctx = params.ctx;
    var yAxis = params.yAxis;
    if (!ctx || !yAxis) return;

    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);

    // Read freehand drawings from localStorage (cached parse, see _fhLsCache)
    var allDrawings;
    try {
        var _raw = localStorage.getItem('klineDrawings') || '{}';
        if (_raw !== _fhLsCache.raw) {
            _fhLsCache.parsed = JSON.parse(_raw);
            _fhLsCache.raw = _raw;
        }
        allDrawings = _fhLsCache.parsed;
    } catch (e) { return; }

    var drawings = allDrawings[klineChartSymbol] || [];
    var dataList = klineChart.getDataList();
    if (!dataList || !dataList.length) return;

    ctx.save();

    for (var d = 0; d < drawings.length; d++) {
        var drawing = drawings[d];
        if (drawing.name !== 'freehand' || !drawing.points || drawing.points.length < 2) continue;
        if (drawing.extendData && drawing.extendData._hidden) continue;

        var color = drawing.color || '#3fb950';
        var width = drawing.width || 2;
        var opacity = drawing.opacity !== undefined ? drawing.opacity : 1.0;

        var isHovered = drawing.id === _freehandHoveredId;
        var isSelected = drawing.id === _freehandSelectedId;
        if (isHovered || isSelected) {
            // Draw glow behind the path
            ctx.strokeStyle = 'rgba(41, 98, 255, 0.3)';
            ctx.lineWidth = width + 6;
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.setLineDash([]);
            ctx.beginPath();
            var glowStarted = false;
            for (var gi = 0; gi < drawing.points.length; gi++) {
                var gpt = drawing.points[gi];
                if (gpt.timestamp == null || gpt.value == null) continue;
                var gdi = _fhTimestampToDataIndex(dataList, gpt.timestamp);
                var gpixel = klineChart.convertToPixel({ dataIndex: gdi, value: 0 }, { paneId: 'candle_pane' });
                if (!gpixel) continue;
                var gchartVal = (_isLog && gpt.value > 0) ? Math.log(gpt.value) : gpt.value;
                var gyPx = yAxis.convertToPixel(gchartVal);
                if (gyPx == null || !isFinite(gyPx)) continue;
                if (!glowStarted) { ctx.moveTo(gpixel.x, gyPx); glowStarted = true; }
                else ctx.lineTo(gpixel.x, gyPx);
            }
            if (glowStarted) ctx.stroke();
        }

        ctx.strokeStyle = opacity < 1.0 ? _hexToRgbaFH(color, opacity) : color;
        ctx.lineWidth = width;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.setLineDash([]);

        ctx.beginPath();
        var started = false;

        for (var i = 0; i < drawing.points.length; i++) {
            var pt = drawing.points[i];
            var ts = pt.timestamp;
            var rawVal = pt.value;
            if (ts == null || rawVal == null) continue;

            // Timestamp → dataIndex → pixel X
            var di = _fhTimestampToDataIndex(dataList, ts);
            var pixel = klineChart.convertToPixel({ dataIndex: di, value: 0 }, { paneId: 'candle_pane' });
            if (!pixel) continue;

            // Value → pixel Y (log transform at render boundary)
            var chartVal = (_isLog && rawVal > 0) ? Math.log(rawVal) : rawVal;
            var yPx = yAxis.convertToPixel(chartVal);
            if (yPx == null || !isFinite(yPx)) continue;

            if (!started) {
                ctx.moveTo(pixel.x, yPx);
                started = true;
            } else {
                ctx.lineTo(pixel.x, yPx);
            }
        }

        if (started) ctx.stroke();
    }

    ctx.restore();
}

// Binary search: timestamp → closest dataIndex
function _fhTimestampToDataIndex(dataList, ts) {
    if (ts < 1e12) ts = ts * 1000;
    var lo = 0, hi = dataList.length - 1;
    while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (dataList[mid].timestamp < ts) lo = mid + 1;
        else hi = mid;
    }
    if (lo > 0 && Math.abs(dataList[lo - 1].timestamp - ts) < Math.abs(dataList[lo].timestamp - ts)) {
        return lo - 1;
    }
    return lo;
}

// Hex color to rgba string
function _hexToRgbaFH(hex, alpha) {
    hex = hex.replace('#', '');
    if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    var r = parseInt(hex.substring(0, 2), 16);
    var g = parseInt(hex.substring(2, 4), 16);
    var b = parseInt(hex.substring(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

// ============================================================
// Activate freehand capture mode
// ============================================================
function freehandActivate() {
    if (!klineChart) return;
    if (_freehandActive) { freehandDeactivate(); return; } // Toggle off

    _freehandActive = true;
    _freehandRawPoints = [];

    var container = document.getElementById('kline-chart-container');
    if (!container) return;

    // Hint overlay
    var hint = document.createElement('div');
    hint.id = 'freehand-hint';
    hint.style.cssText = 'position:absolute;top:36px;left:50%;transform:translateX(-50%);z-index:10001;background:rgba(16,48,16,0.9);border:1px solid #3fb950;color:#3fb950;padding:4px 14px;border-radius:4px;font:12px JetBrains Mono,monospace;pointer-events:none;white-space:nowrap';
    hint.textContent = 'Draw freehand \u2022 Esc to cancel';
    var chartContent = document.getElementById('adv-chart-content');
    if (chartContent) chartContent.appendChild(hint);

    // Temp canvas overlay for live preview
    var canvas = document.createElement('canvas');
    canvas.id = 'freehand-preview-canvas';
    canvas.width = container.offsetWidth;
    canvas.height = container.offsetHeight;
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;z-index:10000;pointer-events:none';
    container.appendChild(canvas);
    _freehandPreviewCanvas = canvas;

    container.style.cursor = 'crosshair';

    var drawing = false;

    function onMouseDown(e) {
        if (e.button !== 0) return; // Left click only
        e.preventDefault();
        e.stopPropagation();
        drawing = true;
        _freehandRawPoints = [];
        var rect = container.getBoundingClientRect();
        _freehandRawPoints.push({ x: e.clientX - rect.left, y: e.clientY - rect.top });
        // Clear preview canvas
        var pctx = canvas.getContext('2d');
        pctx.clearRect(0, 0, canvas.width, canvas.height);
    }

    function onMouseMove(e) {
        if (!drawing) return;
        e.preventDefault();
        e.stopPropagation();
        var rect = container.getBoundingClientRect();
        var px = e.clientX - rect.left;
        var py = e.clientY - rect.top;
        _freehandRawPoints.push({ x: px, y: py });

        // Draw live preview
        var pctx = canvas.getContext('2d');
        pctx.clearRect(0, 0, canvas.width, canvas.height);
        var color = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
        var width = (typeof klineDrawingWidth !== 'undefined') ? klineDrawingWidth : 2;
        pctx.strokeStyle = color;
        pctx.lineWidth = width;
        pctx.lineCap = 'round';
        pctx.lineJoin = 'round';
        pctx.beginPath();
        pctx.moveTo(_freehandRawPoints[0].x, _freehandRawPoints[0].y);
        for (var i = 1; i < _freehandRawPoints.length; i++) {
            pctx.lineTo(_freehandRawPoints[i].x, _freehandRawPoints[i].y);
        }
        pctx.stroke();
    }

    function onMouseUp(e) {
        if (!drawing) return;
        e.preventDefault();
        e.stopPropagation();
        drawing = false;

        if (_freehandRawPoints.length < 3) {
            // Too short, discard
            var pctx = canvas.getContext('2d');
            pctx.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }

        // Simplify with RDP
        var epsilon = 2.0; // pixel tolerance
        var simplified = _rdpSimplify(_freehandRawPoints, epsilon);

        // Convert pixel points to {timestamp, value} for storage
        var chartPoints = _freehandPixelsToChartCoords(simplified);
        if (chartPoints && chartPoints.length >= 2) {
            _freehandSave(chartPoints);
        }

        // Clear preview — the native draw callback will render the saved path
        var pctx2 = canvas.getContext('2d');
        pctx2.clearRect(0, 0, canvas.width, canvas.height);

        // Stay active for more strokes. Deactivate with Esc or re-click tool.
    }

    // Touch support
    function onTouchStart(e) {
        if (e.touches.length !== 1) return;
        e.preventDefault();
        e.stopPropagation();
        drawing = true;
        _freehandRawPoints = [];
        var rect = container.getBoundingClientRect();
        var t = e.touches[0];
        _freehandRawPoints.push({ x: t.clientX - rect.left, y: t.clientY - rect.top });
        var pctx = canvas.getContext('2d');
        pctx.clearRect(0, 0, canvas.width, canvas.height);
    }

    function onTouchMove(e) {
        if (!drawing || e.touches.length !== 1) return;
        e.preventDefault();
        e.stopPropagation();
        var rect = container.getBoundingClientRect();
        var t = e.touches[0];
        var px = t.clientX - rect.left;
        var py = t.clientY - rect.top;
        _freehandRawPoints.push({ x: px, y: py });

        // Live preview
        var pctx = canvas.getContext('2d');
        pctx.clearRect(0, 0, canvas.width, canvas.height);
        var color = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
        var width = (typeof klineDrawingWidth !== 'undefined') ? klineDrawingWidth : 2;
        pctx.strokeStyle = color;
        pctx.lineWidth = width;
        pctx.lineCap = 'round';
        pctx.lineJoin = 'round';
        pctx.beginPath();
        pctx.moveTo(_freehandRawPoints[0].x, _freehandRawPoints[0].y);
        for (var i = 1; i < _freehandRawPoints.length; i++) {
            pctx.lineTo(_freehandRawPoints[i].x, _freehandRawPoints[i].y);
        }
        pctx.stroke();
    }

    function onTouchEnd(e) {
        if (!drawing) return;
        e.preventDefault();
        e.stopPropagation();
        drawing = false;

        if (_freehandRawPoints.length < 3) {
            var pctx = canvas.getContext('2d');
            pctx.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }

        var epsilon = 2.0;
        var simplified = _rdpSimplify(_freehandRawPoints, epsilon);
        var chartPoints = _freehandPixelsToChartCoords(simplified);
        if (chartPoints && chartPoints.length >= 2) {
            _freehandSave(chartPoints);
        }

        var pctx2 = canvas.getContext('2d');
        pctx2.clearRect(0, 0, canvas.width, canvas.height);
    }

    // Use capture phase to intercept before KLineChart's own handlers
    container.addEventListener('mousedown', onMouseDown, true);
    container.addEventListener('mousemove', onMouseMove, true);
    container.addEventListener('mouseup', onMouseUp, true);
    container.addEventListener('touchstart', onTouchStart, { capture: true, passive: false });
    container.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
    container.addEventListener('touchend', onTouchEnd, { capture: true, passive: false });

    _freehandListeners = {
        container: container,
        mousedown: onMouseDown,
        mousemove: onMouseMove,
        mouseup: onMouseUp,
        touchstart: onTouchStart,
        touchmove: onTouchMove,
        touchend: onTouchEnd
    };
}

// ============================================================
// Deactivate freehand mode
// ============================================================
function freehandDeactivate() {
    _freehandActive = false;
    _freehandRawPoints = [];

    if (_freehandListeners) {
        var c = _freehandListeners.container;
        c.removeEventListener('mousedown', _freehandListeners.mousedown, true);
        c.removeEventListener('mousemove', _freehandListeners.mousemove, true);
        c.removeEventListener('mouseup', _freehandListeners.mouseup, true);
        c.removeEventListener('touchstart', _freehandListeners.touchstart, true);
        c.removeEventListener('touchmove', _freehandListeners.touchmove, true);
        c.removeEventListener('touchend', _freehandListeners.touchend, true);
        c.style.cursor = '';
        _freehandListeners = null;
    }

    var hint = document.getElementById('freehand-hint');
    if (hint) hint.remove();

    if (_freehandPreviewCanvas) {
        _freehandPreviewCanvas.remove();
        _freehandPreviewCanvas = null;
    }
}

// ============================================================
// Convert pixel coords to chart coords (timestamp + raw price)
// ============================================================
function _freehandPixelsToChartCoords(pixelPoints) {
    if (!klineChart) return null;
    var dataList = klineChart.getDataList();
    if (!dataList || !dataList.length) return null;

    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    var result = [];

    for (var i = 0; i < pixelPoints.length; i++) {
        var pp = pixelPoints[i];
        // Convert pixel → chart coordinates
        var coord = klineChart.convertFromPixel(
            { x: pp.x, y: pp.y },
            { paneId: 'candle_pane' }
        );
        if (!coord || coord.dataIndex == null || coord.value == null) continue;

        // Resolve dataIndex → timestamp
        var di = coord.dataIndex;
        var ts;
        var lastIdx = dataList.length - 1;
        if (di >= 0 && di <= lastIdx) {
            ts = dataList[di].timestamp;
        } else if (di > lastIdx) {
            var interval = dataList.length > 1 ? dataList[lastIdx].timestamp - dataList[lastIdx - 1].timestamp : 86400000;
            ts = dataList[lastIdx].timestamp + (di - lastIdx) * interval;
        } else {
            var interval2 = dataList.length > 1 ? dataList[1].timestamp - dataList[0].timestamp : 86400000;
            ts = dataList[0].timestamp + di * interval2;
        }

        // Convert value from log space back to raw for storage
        var rawVal = (_isLog && coord.value != null) ? Math.exp(coord.value) : coord.value;

        result.push({ timestamp: Math.round(ts), value: rawVal });
    }

    return result;
}

// ============================================================
// Save freehand stroke to localStorage (same storage as other drawings)
// ============================================================
function _freehandSave(chartPoints) {
    if (!klineChartSymbol) return;

    // Push undo snapshot before mutation
    if (typeof _pushUndoSnapshot === 'function') _pushUndoSnapshot();

    var color = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
    var width = (typeof klineDrawingWidth !== 'undefined') ? klineDrawingWidth : 2;
    var opacity = (typeof klineDrawingOpacity !== 'undefined') ? klineDrawingOpacity : 1.0;

    var drawing = {
        id: 'freehand_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6),
        name: 'freehand',
        points: chartPoints,
        color: color,
        width: width,
        opacity: opacity
    };

    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        if (!allDrawings[klineChartSymbol]) allDrawings[klineChartSymbol] = [];
        allDrawings[klineChartSymbol].push(drawing);
        localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
    } catch (e) {
        console.warn('[Freehand] Save error:', e);
    }

    // Trigger redraw
    _freehandRedraw();
}

// Trigger chart redraw
function _freehandRedraw() {
    if (!klineChart) return;
    try {
        var dl = klineChart.getDataList();
        if (dl && dl.length) klineChart.updateData(dl[dl.length - 1]);
    } catch (e) {}
}

// ============================================================
// Freehand selection — hit-test, hover, right-click delete
// ============================================================
var _freehandHoveredId = null;
var _freehandSelectedId = null;

// Convert a freehand drawing's points to pixel coords
function _freehandToPixels(drawing) {
    if (!klineChart || !drawing || !drawing.points) return null;
    var dataList = klineChart.getDataList();
    if (!dataList || !dataList.length) return null;
    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    var pixels = [];
    for (var i = 0; i < drawing.points.length; i++) {
        var pt = drawing.points[i];
        if (pt.timestamp == null || pt.value == null) continue;
        var di = _fhTimestampToDataIndex(dataList, pt.timestamp);
        var px = klineChart.convertToPixel({ dataIndex: di, value: 0 }, { paneId: 'candle_pane' });
        if (!px) continue;
        var chartVal = (_isLog && pt.value > 0) ? Math.log(pt.value) : pt.value;
        try {
            var yAxis = klineChart.getYAxisByPaneId ? klineChart.getYAxisByPaneId('candle_pane') : null;
            // Fall back to convertToPixel for Y
            var px2 = klineChart.convertToPixel({ dataIndex: di, value: chartVal }, { paneId: 'candle_pane' });
            if (px2 && isFinite(px2.x) && isFinite(px2.y)) pixels.push({ x: px2.x, y: px2.y });
        } catch(e) {}
    }
    return pixels.length >= 2 ? pixels : null;
}

// Check if point (mx, my) is within threshold of any segment in a freehand path
function _freehandPointNearPath(mx, my, pixels, threshold) {
    if (!pixels) return false;
    for (var i = 1; i < pixels.length; i++) {
        var ax = pixels[i - 1].x, ay = pixels[i - 1].y;
        var bx = pixels[i].x, by = pixels[i].y;
        var dx = bx - ax, dy = by - ay;
        var lenSq = dx * dx + dy * dy;
        var t = lenSq > 0 ? Math.max(0, Math.min(1, ((mx - ax) * dx + (my - ay) * dy) / lenSq)) : 0;
        var projX = ax + t * dx, projY = ay + t * dy;
        var dist = Math.sqrt((mx - projX) * (mx - projX) + (my - projY) * (my - projY));
        if (dist <= threshold) return true;
    }
    return false;
}

// Hit-test: returns freehand drawing ID if mouse is near any path, else null
function freehandHitTest(localX, localY) {
    if (!klineChart || !klineChartSymbol) return null;
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawings = allDrawings[klineChartSymbol] || [];
        for (var d = 0; d < drawings.length; d++) {
            if (drawings[d].name !== 'freehand') continue;
            var pixels = _freehandToPixels(drawings[d]);
            if (pixels && _freehandPointNearPath(localX, localY, pixels, 8)) {
                return drawings[d].id;
            }
        }
    } catch(e) {}
    return null;
}

// Delete a freehand drawing by ID
function freehandDelete(id) {
    if (!klineChartSymbol || !id) return;
    if (typeof _pushUndoSnapshot === 'function') _pushUndoSnapshot();
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawings = allDrawings[klineChartSymbol] || [];
        allDrawings[klineChartSymbol] = drawings.filter(function(d) { return d.id !== id; });
        localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
    } catch(e) {}
    _freehandHoveredId = null;
    _freehandSelectedId = null;
    _freehandRedraw();
}
