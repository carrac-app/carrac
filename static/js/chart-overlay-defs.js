// chart-overlay-defs.js — every custom klinecharts OVERLAY registration
// (rect, smcText, scenarioPath, fib, the annotation glyph set,
// arrow markers, position/spot wrappers, watched + handled line variants,
// measureTool, the alert family, milestonePin, elliottWave, anchored VWAP …),
// registered once, from renderKlineChart's one-time `if (!klineChart)` block.
//
// The block reads nothing from renderKlineChart's scope, and its own
// top-level declarations (createFibFigures, _cxMarkerColor,
// createWatchedOverlay, the _alert* helpers, _vwapSeriesCache …) are used
// only here.
//
// Everything here runs when registerCarracOverlayDefs() is CALLED from
// renderKlineChart (chart-render.js), so every chart-app global it reads
// (klineChart, klineChartSymbol, _logScaleEnabled, drawings …) is late-bound —
// load order does not matter beyond both files being present.
// It ends by setting window._chartTypesRegistered, the same flag that
// gates registerCarracIndicatorDefs(), so registration retries per
// render until a chart instance exists.
//
// Loaded via <script defer> before the chart-app files in index.html and
// chart_standalone.html. Contract sentinel: registerCarracOverlayDefs.

function registerCarracOverlayDefs() {
    try {
        klinecharts.registerOverlay({
            name: 'rect',
            totalStep: 3,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: true,
            needDefaultYAxisFigure: true,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                if (coordinates.length > 1) {
                    var overlay = params.overlay || {};
                    var ext = overlay.extendData || {};
                    var poly = (overlay.styles && overlay.styles.polygon) || {};
                    var ln = (overlay.styles && overlay.styles.line) || {};
                    var borderColor = poly.borderColor || ln.color || '#3fb950';
                    var borderWidth = poly.borderSize || ln.size || 1;
                    var fillColor = poly.color || ext.fillColor || 'rgba(63,185,80,0.15)';
                    var noBorder = ext.noBorder;
                    if (noBorder === undefined && overlay.points && overlay.points.length >= 2) {
                        noBorder = (overlay.points[0].value < overlay.points[1].value);
                    }
                    var rx = Math.min(coordinates[0].x, coordinates[1].x);
                    var ry = Math.min(coordinates[0].y, coordinates[1].y);
                    var rw = Math.abs(coordinates[1].x - coordinates[0].x);
                    var rh = Math.abs(coordinates[1].y - coordinates[0].y);
                    var figures = [{
                        type: 'rect',
                        attrs: { x: rx, y: ry, width: rw, height: rh },
                        styles: {
                            style: noBorder ? 'fill' : 'stroke_fill',
                            color: fillColor,
                            borderColor: borderColor,
                            borderSize: borderWidth
                        }
                    }];
                    // Text inside rectangle
                    if (ext.rectText) {
                        var tp = ext.rectTextPos || 'center';
                        var ts = ext.rectTextSize || 12;
                        var tc = ext.rectTextColor || '#d1d4dc';
                        var pad = 6;
                        var tx, ty, align, baseline;
                        if (tp === 'top-left')       { tx = rx + pad; ty = ry + pad; align = 'left'; baseline = 'top'; }
                        else if (tp === 'top-right')  { tx = rx + rw - pad; ty = ry + pad; align = 'right'; baseline = 'top'; }
                        else if (tp === 'bottom-left') { tx = rx + pad; ty = ry + rh - pad; align = 'left'; baseline = 'bottom'; }
                        else if (tp === 'bottom-right'){ tx = rx + rw - pad; ty = ry + rh - pad; align = 'right'; baseline = 'bottom'; }
                        else                          { tx = rx + rw / 2; ty = ry + rh / 2; align = 'center'; baseline = 'middle'; }
                        var fontWeight = ext.rectTextBold ? 'bold' : 'normal';
                        var fontStyle = ext.rectTextItalic ? 'italic' : 'normal';
                        figures.push({
                            type: 'text',
                            attrs: { x: tx, y: ty, text: ext.rectText, align: align, baseline: baseline },
                            styles: { color: tc, size: ts, family: "'JetBrains Mono', monospace", weight: fontWeight, style: fontStyle }
                        });
                    }
                    // hover highlight — thicken border + outer glow line (hovered only, not selected)
                    var isHovered = (typeof _hoveredRectId !== 'undefined') && overlay.id === _hoveredRectId;
                    var isSelected = (typeof klineSelectedOverlayId !== 'undefined') && overlay.id === klineSelectedOverlayId;
                    // multi-select membership — accent ring so the set is unmistakable
                    var isMsSel = (typeof window !== 'undefined' && window.klineMultiSelectIds && window.klineMultiSelectIds.indexOf(overlay.id) !== -1);
                    if (isMsSel) {
                        figures[0].styles.borderSize = borderWidth + 1;
                        figures.splice(0, 0, {
                            type: 'rect',
                            attrs: { x: rx - 3, y: ry - 3, width: rw + 6, height: rh + 6 },
                            styles: { style: 'stroke_fill', color: 'transparent', borderColor: '#2962ff', borderSize: 2 }
                        });
                    }
                    if (isHovered && !isSelected && !isMsSel) {
                        // Overwrite the first figure with a thicker border
                        figures[0].styles.borderSize = borderWidth + 1;
                        // Outer glow: 1px border-only rect expanded 1px out, same color at reduced alpha
                        figures.splice(0, 0, {
                            type: 'rect',
                            attrs: { x: rx - 1, y: ry - 1, width: rw + 2, height: rh + 2 },
                            styles: { style: 'stroke_fill', color: 'transparent', borderColor: borderColor, borderSize: 1 }
                        });
                    }
                    // Corner + edge handles — blue squares on hover/selection/multi-select
                    if (isHovered || isSelected || isMsSel) {
                        var x1 = rx, y1 = ry, x2 = rx + rw, y2 = ry + rh;
                        var midX = (x1 + x2) / 2, midY = (y1 + y2) / 2;
                        var pts = [{x:x1,y:y1},{x:x2,y:y1},{x:x1,y:y2},{x:x2,y:y2},{x:midX,y:y1},{x:midX,y:y2},{x:x1,y:midY},{x:x2,y:midY}];
                        var hs = 5;
                        var hStyle = { style: 'fill', color: '#2962ff', borderColor: '#ffffff', borderSize: 1.5 };
                        for (var ci = 0; ci < pts.length; ci++) {
                            figures.push({ type: 'rect', attrs: { x: pts[ci].x - hs, y: pts[ci].y - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
                        }
                    }
                    return figures;
                }
                return [];
            }
        });
    } catch (e) {
        // Already registered from previous chart load
    }

    // SMC text label overlay — draws text at a point (simpleAnnotation doesn't support visible text)
    try {
        klinecharts.registerOverlay({
            name: 'smcLabel',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                if (!params.coordinates || params.coordinates.length < 1) return [];
                var coord = params.coordinates[0];
                var overlay = params.overlay || {};
                var ext = overlay.extendData || {};
                var text = ext.text || '';
                var color = ext.color || '#d1d4dc';
                var size = ext.size || 10;
                var baseline = ext.baseline || 'bottom';
                var align = ext.align || 'center';
                return [{
                    type: 'text',
                    attrs: { x: coord.x, y: coord.y, text: text, align: align, baseline: baseline },
                    styles: { color: color, size: size, family: 'JetBrains Mono, monospace', weight: 'bold' }
                }];
            }
        });
    } catch (e) {}

    // Parallel Channel: 3 bounded lines (top, bottom, dashed midline) as single overlay
    // Name 'boundedChannel' avoids any collision with KLineChart built-in overlay names
    try {
        klinecharts.registerOverlay({
            name: 'boundedChannel',
            totalStep: 4,
            // No native point figures: they were invisible (transparent point style)
            // yet draggable — ghost hotspots that fought the custom handle system.
            // All point editing goes through the p0/p1/off handles in kline-drawings.js.
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            // Magnet-snap the point being placed during the 3-click creation flow
            // (mirrors _magnetPerform in kline-drawings.js; proximity gating —
            // the weak/strong px caps — lives inside _magnetSnapValue).
            performEventMoveForDrawing: function(params) {
                try {
                    if (typeof _magnetSnapValue !== 'function' || typeof klineChart === 'undefined' || !klineChart) return;
                    var pp = params && params.performPoint;
                    if (!pp || pp.dataIndex == null || pp.value == null) return;
                    var ctrl = (typeof _magnetCtrlHeld !== 'undefined') && _magnetCtrlHeld;
                    var snapped = _magnetSnapValue(pp.dataIndex, pp.value, 'candle_pane', ctrl);
                    if (snapped !== pp.value) pp.value = snapped;
                } catch (e) {}
            },
            createPointFigures: function(params) {
                try {
                    var coords = params.coordinates;
                    var overlay = params.overlay || {};
                    var ext = overlay.extendData || {};
                    if (!coords || coords.length < 2) return [];
                    var color = ext._color || ((overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#3fb950');
                    var width = ext._width || ((overlay.styles && overlay.styles.line && overlay.styles.line.size) || 1);
                    // thicken lines +1 on hover (not selected)
                    var _isChanHov = (typeof _hoveredChannelId !== 'undefined') && overlay.id === _hoveredChannelId;
                    var _isChanSel = (typeof klineSelectedOverlayId !== 'undefined') && overlay.id === klineSelectedOverlayId;
                    var _isChanMs = (typeof window !== 'undefined' && window.klineMultiSelectIds && window.klineMultiSelectIds.indexOf(overlay.id) !== -1);
                    var drawWidth = _isChanMs ? width + 2 : ((_isChanHov && !_isChanSel) ? width + 1 : width);
                    var figures = [];
                    // Invisible wide hit-test line for easier hover/click
                    figures.push({ type: 'line', attrs: { coordinates: [coords[0], coords[1]] }, styles: { color: 'transparent', size: 12 } });
                    // Line 1: p1 -> p2 (main trendline)
                    figures.push({
                        type: 'line',
                        attrs: { coordinates: [coords[0], coords[1]] },
                        styles: { color: color, size: drawWidth }
                    });
                    var channelCorners = [coords[0], coords[1]];
                    if (coords.length >= 3) {
                        // Calculate offset: interpolate line p1->p2 at p3's x position, get vertical distance
                        var dx = coords[1].x - coords[0].x;
                        var t = dx !== 0 ? (coords[2].x - coords[0].x) / dx : 0;
                        var lineY = coords[0].y + t * (coords[1].y - coords[0].y);
                        var offY = coords[2].y - lineY;
                        var bl = { x: coords[0].x, y: coords[0].y + offY };
                        var br = { x: coords[1].x, y: coords[1].y + offY };
                        // Invisible wide hit-test for parallel line
                        figures.push({ type: 'line', attrs: { coordinates: [bl, br] }, styles: { color: 'transparent', size: 12 } });
                        // Line 2: offset parallel line (solid)
                        figures.push({
                            type: 'line',
                            attrs: { coordinates: [bl, br] },
                            styles: { color: color, size: drawWidth }
                        });
                        // Line 3: midline (dashed)
                        figures.push({
                            type: 'line',
                            attrs: { coordinates: [
                                { x: coords[0].x, y: coords[0].y + offY / 2 },
                                { x: coords[1].x, y: coords[1].y + offY / 2 }
                            ] },
                            styles: { style: 'dashed', color: color, size: 1, dashedValue: [4, 4] }
                        });
                        // Offset handle: middle of the parallel line (TradingView-style).
                        // Drag semantics live in kline-drawings.js (_getChannelCornerHit /
                        // _handleChannelCornerDrag): endpoints pivot around the opposite
                        // end preserving the price offset; this handle adjusts width only.
                        channelCorners.push({ x: (bl.x + br.x) / 2, y: (bl.y + br.y) / 2 });
                    }
                    return figures.concat(_selectionSquares(channelCorners, (params.overlay || {}).id));
                } catch (err) {
                    console.error('boundedChannel createPointFigures error:', err);
                    return [];
                }
            }
        });
    } catch (e) {
        console.error('boundedChannel registration error:', e);
    }

    // Fibonacci — all available levels (TradingView-style)
    var fibAllLevels = [
        0, 0.236, 0.382, 0.5, 0.618, 0.786, 1,
        1.272, 1.414, 1.618, 2, 2.272, 2.414, 2.618,
        3, 3.272, 3.414, 3.618, 4, 4.236, 4.272, 4.414, 4.618, 4.764
    ];
    var fibDefaultEnabled = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.414, 1.618, 2, 2.272, 2.414, 2.618];
    // Load global fib defaults from localStorage
    function _getFibDefaults() {
        try { var s = localStorage.getItem('fibEnabledLevels'); if (s) return JSON.parse(s); } catch(e) {}
        return fibDefaultEnabled;
    }

    function _fmtFibPrice(v) {
        if (v >= 10000) return v.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        if (v >= 100) return v.toFixed(2);
        if (v >= 1) return v.toFixed(2);
        if (v >= 0.01) return v.toFixed(4);
        return v.toFixed(6);
    }

    function createFibFigures(coords, overlay, fullWidth, bounding) {
        var p1 = coords[0], p2 = coords[1];
        var ext = (overlay && overlay.extendData) || {};
        var points = (overlay && overlay.points) || [];
        // Extend: per-fib override from editor
        var isExtended = fullWidth || ext._fibExtend;
        // Determine which levels to show: per-fib override > global defaults
        var enabledLevels = ext._fibLevels || _getFibDefaults();
        // For bounded fib, cap at 1 unless user explicitly enabled higher levels
        var levels = fibAllLevels.filter(function(lv) {
            if (enabledLevels.indexOf(lv) === -1) return false;
            if (!isExtended && !ext._fibLevels && lv > 1) return false;
            return true;
        });
        var x1, x2;
        if (isExtended) {
            x1 = 0; x2 = (bounding && bounding.width) || 9999;
        } else {
            x1 = Math.min(p1.x, p2.x); x2 = Math.max(p1.x, p2.x);
        }
        var figures = [];
        var color = ext._color || ((overlay && overlay.styles && overlay.styles.line && overlay.styles.line.color) ? overlay.styles.line.color : '#787b86');
        var baseWidth = ext._width || ((overlay && overlay.styles && overlay.styles.line && overlay.styles.line.size) ? overlay.styles.line.size : 1);
        // Invisible hit-test rect
        var allYs = levels.map(function(lv) { return p2.y + (p1.y - p2.y) * lv; });
        var fyMin = Math.min.apply(null, allYs.concat([p1.y, p2.y]));
        var fyMax = Math.max.apply(null, allYs.concat([p1.y, p2.y]));
        figures.push({ type: 'rect', attrs: { x: x1, y: fyMin, width: x2 - x1, height: Math.max(fyMax - fyMin, 12) }, styles: { color: 'transparent', borderColor: 'transparent', borderSize: 0 } });
        // Price values at anchor points
        var p1Val = points.length >= 1 ? points[0].value : null;
        var p2Val = points.length >= 2 ? points[1].value : null;
        // Log scale: convert from log space to real price
        if (p1Val != null && p2Val != null && typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled) {
            p1Val = Math.exp(p1Val); p2Val = Math.exp(p2Val);
        }
        levels.forEach(function(lv) {
            var y = p2.y + (p1.y - p2.y) * lv;
            var ratio = lv % 1 === 0 ? lv.toString() : lv.toString();
            // Label: ratio + (price)
            var label = ratio;
            if (p1Val != null && p2Val != null) {
                var price = p2Val + (p1Val - p2Val) * lv;
                label = ratio + ' (' + _fmtFibPrice(price) + ')';
            }
            figures.push({
                type: 'line',
                attrs: { coordinates: [{ x: x1, y: y }, { x: x2, y: y }] },
                styles: { style: lv === 0.5 ? 'solid' : 'dashed', color: color, size: lv === 0 || lv === 1 ? baseWidth * 1.5 : baseWidth, dashedValue: [4, 3] }
            });
            figures.push({
                type: 'text',
                attrs: { x: (fullWidth ? x1 + 5 : x2 + 5), y: y, text: label, align: 'left', baseline: 'middle' },
                styles: { color: color, size: 10, family: 'JetBrains Mono, monospace' }
            });
        });
        return figures;
    }

    // Finite Fibonacci retracement (bounded between 2 points, levels 0-1)
    try {
        klinecharts.registerOverlay({
            name: 'fibonacciRetrace',
            totalStep: 3,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                if (!coords || coords.length < 2) return [];
                return createFibFigures(coords, params.overlay, false, params.bounding).concat(_selectionSquares(coords, (params.overlay || {}).id));
            }
        });
    } catch (e) {}

    // Infinite Fibonacci (full chart width, includes extension levels 1.618, 2.618)
    try {
        klinecharts.registerOverlay({
            name: 'fibonacciInfinite',
            totalStep: 3,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                if (!coords || coords.length < 2) return [];
                return createFibFigures(coords, params.overlay, true, params.bounding).concat(_selectionSquares(coords, (params.overlay || {}).id));
            }
        });
    } catch (e) {}

    // Text annotation overlay
    try {
        klinecharts.registerOverlay({
            name: 'textAnnotation',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                if (coordinates.length > 0) {
                    var overlay = params.overlay || {};
                    var ext = overlay.extendData || {};
                    var text = ext.text || 'Text';
                    var fontSize = ext.fontSize || 14;
                    var color = ext._color || ext.color || (overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#d1d4dc';
                    var weight = ext.bold ? 'bold' : 'normal';
                    var approxW = text.length * fontSize * 0.6;
                    var figures = [
                    // Invisible hit-test rect behind text so it can be clicked/selected
                    {
                        type: 'rect',
                        attrs: {
                            x: coordinates[0].x,
                            y: coordinates[0].y - fontSize * 0.8,
                            width: approxW,
                            height: fontSize * 1.2
                        },
                        styles: { color: 'transparent', borderColor: 'transparent', borderSize: 0 }
                    },
                    {
                        type: 'text',
                        attrs: {
                            x: coordinates[0].x,
                            y: coordinates[0].y,
                            text: text
                        },
                        styles: {
                            color: color,
                            size: fontSize,
                            family: "'JetBrains Mono', monospace",
                            weight: weight,
                            style: ext.italic ? 'italic' : 'normal'
                        }
                    }];
                    // Underline: draw a line under the text
                    if (ext.underline) {
                        var approxW = text.length * fontSize * 0.6;
                        figures.push({
                            type: 'line',
                            attrs: { coordinates: [{ x: coordinates[0].x, y: coordinates[0].y + fontSize * 0.25 }, { x: coordinates[0].x + approxW, y: coordinates[0].y + fontSize * 0.25 }] },
                            styles: { color: color, size: 1 }
                        });
                    }
                    // Strikethrough: draw a line through the middle of the text
                    if (ext.strikethrough) {
                        var approxW2 = text.length * fontSize * 0.6;
                        figures.push({
                            type: 'line',
                            attrs: { coordinates: [{ x: coordinates[0].x, y: coordinates[0].y + fontSize * 0.1 }, { x: coordinates[0].x + approxW2, y: coordinates[0].y + fontSize * 0.1 }] },
                            styles: { color: color, size: 1 }
                        });
                    }
                    return figures.concat(_selectionSquares(coordinates, overlay.id));
                }
                return [];
            }
        });
    } catch (e) {}

    // ---------------------------------------------------------------
    // Fixed v1 annotation glyph set — single-click stamps
    // (arrow up/down, flag pin, circle) + a callout (anchored text with
    // a leader line). All follow the 'spot'/'textAnnotation' pattern:
    // one point, needDefaultPointFigure:true (native drag, same as
    // textAnnotation) PLUS our own invisible hit-test shape underneath
    // the visible (ignoreEvent) glyph — belt-and-suspenders, matches
    // alertLine's "invisible wide hit line + visible line" idiom.
    // Color/width come from extendData._color/_width, which
    // recreateOverlayWithStyles() (drawing-editing.js) always sets — this is what
    // wires these overlays into the existing drawing-context-bar
    // (color dots + width buttons) with zero extra plumbing.
    //
    // IMPORTANT: shared/frozen chart pages render these overlays via
    // static/js/kline-share-render.js (KlineShareRender.registerOverlays,
    // loaded by templates/chart_headless.html — NOT this file) — any
    // change to these renderers must be mirrored there too (share
    // parity).
    // ---------------------------------------------------------------
    function _cxMarkerColor(overlay) {
        var ext = overlay.extendData || {};
        return ext._color || (overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#3fb950';
    }
    function _cxMarkerWidth(overlay) {
        var ext = overlay.extendData || {};
        return ext._width || 1;
    }
    function createArrowMarkerFigures(params, direction) {
        var coordinates = params.coordinates;
        if (!coordinates || coordinates.length < 1) return [];
        var overlay = params.overlay || {};
        var color = _cxMarkerColor(overlay);
        var w = _cxMarkerWidth(overlay);
        var size = 7 + w * 3; // half-height: 10/13/16/19 for width 1-4
        var x = coordinates[0].x, y = coordinates[0].y;
        var figures = [
            { type: 'circle', attrs: { x: x, y: y, r: size + 6 }, styles: { style: 'fill', color: 'transparent' } }
        ];
        var tip, baseY;
        if (direction === 'up') { tip = { x: x, y: y - size }; baseY = y + size * 0.6; }
        else { tip = { x: x, y: y + size }; baseY = y - size * 0.6; }
        figures.push({
            type: 'polygon', ignoreEvent: true,
            attrs: { coordinates: [tip, { x: x - size * 0.7, y: baseY }, { x: x + size * 0.7, y: baseY }] },
            styles: { style: 'fill', color: color }
        });
        return figures.concat(_selectionSquares(coordinates, overlay.id));
    }
    try {
        klinecharts.registerOverlay({
            name: 'arrowUpMarker',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) { return createArrowMarkerFigures(params, 'up'); }
        });
    } catch (e) {}
    try {
        klinecharts.registerOverlay({
            name: 'arrowDownMarker',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) { return createArrowMarkerFigures(params, 'down'); }
        });
    } catch (e) {}

    // Flag pin — vertical pole + pennant, TradingView-style chart stamp
    try {
        klinecharts.registerOverlay({
            name: 'flagPin',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                if (!coordinates || coordinates.length < 1) return [];
                var overlay = params.overlay || {};
                var color = _cxMarkerColor(overlay);
                var w = _cxMarkerWidth(overlay);
                var poleH = 16 + w * 4;
                var x = coordinates[0].x, y = coordinates[0].y;
                var topY = y - poleH;
                var figures = [
                    { type: 'rect', attrs: { x: x - 10, y: topY - 6, width: 28, height: poleH + 12 }, styles: { style: 'fill', color: 'transparent' } },
                    { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: x, y: y }, { x: x, y: topY }] }, styles: { color: color, size: Math.max(1.5, w) } },
                    { type: 'polygon', ignoreEvent: true, attrs: { coordinates: [{ x: x, y: topY }, { x: x + 14, y: topY + 5 }, { x: x, y: topY + 10 }] }, styles: { style: 'fill', color: color } }
                ];
                return figures.concat(_selectionSquares(coordinates, overlay.id));
            }
        });
    } catch (e) {}

    // Circle marker — plain ring/fill stamp
    try {
        klinecharts.registerOverlay({
            name: 'circleMarker',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                if (!coordinates || coordinates.length < 1) return [];
                var overlay = params.overlay || {};
                var color = _cxMarkerColor(overlay);
                var w = _cxMarkerWidth(overlay);
                var r = 6 + w * 2;
                var x = coordinates[0].x, y = coordinates[0].y;
                var fillColor = (typeof hexToRgba === 'function') ? hexToRgba(color, 0.2) : color;
                var figures = [
                    { type: 'circle', attrs: { x: x, y: y, r: r + 8 }, styles: { style: 'fill', color: 'transparent' } },
                    { type: 'circle', ignoreEvent: true, attrs: { x: x, y: y, r: r }, styles: { style: 'stroke_fill', color: fillColor, borderColor: color, borderSize: Math.max(1.5, w) } }
                ];
                return figures.concat(_selectionSquares(coordinates, overlay.id));
            }
        });
    } catch (e) {}

    // Callout — anchored point + a leader line to a fixed-offset text box.
    // The offset (extendData.offsetX/offsetY, px) defaults to up-right of
    // the anchor; users reposition it via the right-click "Text Position"
    // preset picker (mirrors the existing rect-text-position pattern)
    // rather than a bespoke continuous pixel-drag — dragging the overlay
    // itself (anchor OR text box, both hit-testable) moves the whole
    // callout, preserving the offset. A full custom drag
    // branch was descoped.
    try {
        klinecharts.registerOverlay({
            name: 'callout',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                if (!coordinates || coordinates.length < 1) return [];
                var overlay = params.overlay || {};
                var ext = overlay.extendData || {};
                var text = ext.text || 'Note';
                var fontSize = ext.fontSize || 12;
                var color = ext._color || ext.color || (overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#d1d4dc';
                var weight = ext.bold ? 'bold' : 'normal';
                var fstyle = ext.italic ? 'italic' : 'normal';
                var offsetX = (ext.offsetX != null) ? ext.offsetX : 60;
                var offsetY = (ext.offsetY != null) ? ext.offsetY : -60;
                var ax = coordinates[0].x, ay = coordinates[0].y;
                var tx = ax + offsetX, ty = ay + offsetY;
                var approxW = text.length * fontSize * 0.6;
                var padX = 5, padY = 4;
                var boxW = approxW + padX * 2, boxH = fontSize + padY * 2;
                var boxX = offsetX >= 0 ? tx : tx - boxW;
                var boxY = offsetY >= 0 ? ty : ty - boxH;
                var iFont = "'JetBrains Mono', monospace";
                var figures = [
                    // Anchor hit-test + dot
                    { type: 'circle', attrs: { x: ax, y: ay, r: 10 }, styles: { style: 'fill', color: 'transparent' } },
                    { type: 'circle', ignoreEvent: true, attrs: { x: ax, y: ay, r: 3 }, styles: { style: 'fill', color: color } },
                    // Leader line anchor → text box
                    { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: ax, y: ay }, { x: tx, y: ty }] }, styles: { color: color, size: 1, style: 'dashed', dashedValue: [3, 3] } },
                    // Text box (also the click/drag target for the whole overlay)
                    { type: 'rect', attrs: { x: boxX, y: boxY, width: boxW, height: boxH }, styles: { style: 'stroke_fill', color: (typeof hexToRgba === 'function') ? hexToRgba(color, 0.12) : 'rgba(209,212,220,0.12)', borderColor: color, borderSize: 1 } },
                    { type: 'text', ignoreEvent: true, attrs: { x: boxX + padX, y: boxY + boxH / 2, text: text, baseline: 'middle' }, styles: { color: color, size: fontSize, family: iFont, weight: weight, style: fstyle } }
                ];
                if (ext.underline) {
                    figures.push({ type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: boxX + padX, y: boxY + boxH / 2 + fontSize * 0.35 }, { x: boxX + padX + approxW, y: boxY + boxH / 2 + fontSize * 0.35 }] }, styles: { color: color, size: 1 } });
                }
                if (ext.strikethrough) {
                    figures.push({ type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: boxX + padX, y: boxY + boxH / 2 }, { x: boxX + padX + approxW, y: boxY + boxH / 2 }] }, styles: { color: color, size: 1 } });
                }
                return figures.concat(_selectionSquares(coordinates, overlay.id));
            }
        });
    } catch (e) {}
    // Note: verticalLine reuses klinecharts' NATIVE 'verticalStraightLine'
    // overlay (same one already harvested by _installMagnetNativeHooks in
    // kline-drawings.js) — no custom registerOverlay needed; it renders via
    // the generic drawing-tool fallback in handleKlineToolClick.

    // Unified Position overlay — auto-detects long/short from SL vs entry
    // Points: [0]=Entry, [1]=SL, [2]=TP. Direction: SL < Entry = long, SL > Entry = short

    // Position figure builders + carracPositionStats + _fadeColor moved to chart-position-overlays.js.

    try {
        klinecharts.registerOverlay({
            name: 'position',
            totalStep: 4,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) { return createPositionFigures(params); },
            createYAxisFigures: function(params) { return createPositionYAxisFigures(params); }
        });
    } catch (e) {}
    // Backward compat: register old names pointing to same renderer
    try { klinecharts.registerOverlay({ name: 'longPosition', totalStep: 4, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(params) { return createPositionFigures(params); }, createYAxisFigures: function(params) { return createPositionYAxisFigures(params); } }); } catch (e) {}
    try { klinecharts.registerOverlay({ name: 'shortPosition', totalStep: 4, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(params) { return createPositionFigures(params); }, createYAxisFigures: function(params) { return createPositionYAxisFigures(params); } }); } catch (e) {}

    // Spot figure builders moved to chart-position-overlays.js.

    try {
        klinecharts.registerOverlay({
            name: 'spot',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) { return createSpotFigures(params); },
            createYAxisFigures: function(params) { return createSpotYAxisFigures(params); }
        });
    } catch (e) {}

    // _updateSpotPnlLabel moved to chart-position-overlays.js.

    // rAF loop for sticky positioning (cheap — just convertToPixel + DOM update).
    // Skip the work when the tab is hidden so we don't burn mobile battery
    // for an off-screen label.
    if (!window._spotPnlRaf) {
        window._spotPnlRaf = true;
        (function _spotPnlLoop() {
            if (!document.hidden) {
                _updateSpotPnlLabel();
            }
            requestAnimationFrame(_spotPnlLoop);
        })();
    }

    // Register watched overlay variants (segment/straightLine/rayLine + bell icon)
    // These are NEW overlay types — built-in segment/straightLine/rayLine stay untouched
    function createWatchedOverlay(baseName) {
        return {
            name: 'watched_' + baseName,
            totalStep: 3,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: true,
            needDefaultYAxisFigure: true,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                var overlay = params.overlay || {};
                var bounding = params.bounding;
                if (!coords || coords.length < 2) return [];
                var figures = [];

                var lnColor = (overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#3fb950';
                var lnWidth = (overlay.styles && overlay.styles.line && overlay.styles.line.size) || 1;
                var lnStyles = { color: lnColor, size: lnWidth };
                if (overlay.styles && overlay.styles.line && overlay.styles.line.style) lnStyles.style = overlay.styles.line.style;
                if (overlay.styles && overlay.styles.line && overlay.styles.line.dashedValue) lnStyles.dashedValue = overlay.styles.line.dashedValue;

                if (baseName === 'segment') {
                    figures.push({ type: 'line', attrs: { coordinates: [coords[0], coords[1]] }, styles: lnStyles });
                } else if (baseName === 'straightLine' || baseName === 'rayLine') {
                    var x1 = coords[0].x, y1 = coords[0].y;
                    var x2 = coords[1].x, y2 = coords[1].y;
                    if (x1 !== x2) {
                        var slope = (y2 - y1) / (x2 - x1);
                        if (baseName === 'straightLine') {
                            figures.push({ type: 'line', attrs: { coordinates: [{ x: 0, y: y1 + slope * (0 - x1) }, { x: bounding.width, y: y1 + slope * (bounding.width - x1) }] }, styles: lnStyles });
                        } else {
                            var endX = x2 > x1 ? bounding.width : 0;
                            figures.push({ type: 'line', attrs: { coordinates: [{ x: x1, y: y1 }, { x: endX, y: y1 + slope * (endX - x1) }] }, styles: lnStyles });
                        }
                    } else {
                        if (baseName === 'straightLine') {
                            figures.push({ type: 'line', attrs: { coordinates: [{ x: x1, y: 0 }, { x: x1, y: bounding.height }] }, styles: lnStyles });
                        } else {
                            figures.push({ type: 'line', attrs: { coordinates: [{ x: x1, y: y1 }, { x: x1, y: (y2 > y1 ? bounding.height : 0) }] }, styles: lnStyles });
                        }
                    }
                }

                // Alert indicator at rightmost point (monochrome)
                var rightPt = coords[0].x >= coords[1].x ? coords[0] : coords[1];
                figures.push(
                    { type: 'circle', attrs: { x: rightPt.x, y: rightPt.y, r: 8 }, styles: { style: 'stroke_fill', color: 'rgba(209,212,220,0.1)', borderColor: '#787b86', borderSize: 1 } },
                    { type: 'text', attrs: { x: rightPt.x, y: rightPt.y - 1, text: 'A', align: 'center', baseline: 'middle' }, styles: { color: '#d1d4dc', size: 10, weight: 'bold', family: "'JetBrains Mono', monospace" } }
                );
                return figures.concat(_selectionSquares(coords, (params.overlay || {}).id));
            }
        };
    }
    ['segment', 'straightLine', 'rayLine'].forEach(function(name) {
        try { klinecharts.registerOverlay(createWatchedOverlay(name)); } catch (e) { console.error('[overlays] Failed to register watched_' + name, e); }
    });

    // Register custom line overlays (segment, straightLine, rayLine) with selection handles
    // Replaces KLineChart built-ins so blue square handles show on hover/select
    function createHandledLineOverlay(baseName) {
        return {
            name: baseName,
            totalStep: 3,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: true,
            needDefaultYAxisFigure: true,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                var overlay = params.overlay || {};
                var bounding = params.bounding;
                if (!coords || coords.length < 2) return [];
                var figures = [];

                var lnColor = (overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#3fb950';
                var lnWidth = (overlay.styles && overlay.styles.line && overlay.styles.line.size) || 1;
                var lnStyles = { color: lnColor, size: lnWidth };
                if (overlay.styles && overlay.styles.line && overlay.styles.line.style) lnStyles.style = overlay.styles.line.style;
                if (overlay.styles && overlay.styles.line && overlay.styles.line.dashedValue) lnStyles.dashedValue = overlay.styles.line.dashedValue;

                var linePts;
                if (baseName === 'segment') {
                    linePts = [coords[0], coords[1]];
                } else {
                    var x1 = coords[0].x, y1 = coords[0].y;
                    var x2 = coords[1].x, y2 = coords[1].y;
                    if (x1 !== x2) {
                        var slope = (y2 - y1) / (x2 - x1);
                        if (baseName === 'straightLine') {
                            linePts = [{ x: 0, y: y1 + slope * (0 - x1) }, { x: bounding.width, y: y1 + slope * (bounding.width - x1) }];
                        } else {
                            var endX = x2 > x1 ? bounding.width : 0;
                            linePts = [{ x: x1, y: y1 }, { x: endX, y: y1 + slope * (endX - x1) }];
                        }
                    } else {
                        if (baseName === 'straightLine') {
                            linePts = [{ x: x1, y: 0 }, { x: x1, y: bounding.height }];
                        } else {
                            linePts = [{ x: x1, y: y1 }, { x: x1, y: (y2 > y1 ? bounding.height : 0) }];
                        }
                    }
                }
                // Invisible wide hit-test line for hover detection
                figures.push({ type: 'line', attrs: { coordinates: linePts }, styles: { color: 'transparent', size: 12 } });
                figures.push({ type: 'line', attrs: { coordinates: linePts }, styles: lnStyles });
                return figures.concat(_selectionSquares(coords, overlay.id));
            }
        };
    }
    ['segment', 'straightLine', 'rayLine'].forEach(function(name) {
        try { klinecharts.registerOverlay(createHandledLineOverlay(name)); } catch (e) { console.error('[overlays] Failed to register ' + name, e); }
    });

    // Register custom horizontalStraightLine with selection handles
    try {
        klinecharts.registerOverlay({
            name: 'horizontalStraightLine',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: true,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                var overlay = params.overlay || {};
                var bounding = params.bounding;
                if (!coords || coords.length < 1) return [];
                var lnColor = (overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#3fb950';
                var lnWidth = (overlay.styles && overlay.styles.line && overlay.styles.line.size) || 1;
                var lnStyles = { color: lnColor, size: lnWidth };
                if (overlay.styles && overlay.styles.line && overlay.styles.line.style) lnStyles.style = overlay.styles.line.style;
                if (overlay.styles && overlay.styles.line && overlay.styles.line.dashedValue) lnStyles.dashedValue = overlay.styles.line.dashedValue;
                var y = coords[0].y;
                var linePts = [{ x: 0, y: y }, { x: bounding.width, y: y }];
                return [
                    { type: 'line', attrs: { coordinates: linePts }, styles: { color: 'transparent', size: 12 } },
                    { type: 'line', attrs: { coordinates: linePts }, styles: lnStyles }
                ].concat(_selectionSquares(coords, overlay.id));
            }
        });
    } catch (e) {}

    // Measure tool overlay — blue rectangle with L-shaped arrows showing % + price + time
    try {
        klinecharts.registerOverlay({
            name: 'measureTool',
            totalStep: 3,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                var overlay = params.overlay || {};
                var points = overlay.points || [];
                if (coordinates.length < 2 || points.length < 2) return [];

                var x1 = coordinates[0].x, y1 = coordinates[0].y;
                var x2 = coordinates[1].x, y2 = coordinates[1].y;
                // Log scale stores ln(price) in chart space — exp() back
                // before any price math (as the axis-tag code does)
                var _isLogM = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                var price1 = _isLogM ? Math.exp(points[0].value) : points[0].value;
                var price2 = _isLogM ? Math.exp(points[1].value) : points[1].value;

                var xLeft = Math.min(x1, x2), xRight = Math.max(x1, x2);
                var yTop = Math.min(y1, y2), yBottom = Math.max(y1, y2);

                // Price change
                var priceDiff = price2 - price1;
                var pctChange = price1 !== 0 ? ((priceDiff / price1) * 100) : 0;
                var isUp = priceDiff >= 0;
                var arrowColor = isUp ? '#26a69a' : '#ef5350';

                // Format price
                function fp(p) {
                    var a = Math.abs(p);
                    if (a >= 1000) return p.toFixed(2);
                    if (a >= 1) return p.toFixed(2);
                    if (a >= 0.01) return p.toFixed(4);
                    return p.toFixed(6);
                }

                // Bar count — use dataIndex if available
                var bars = 0;
                var data = null;
                try { data = klineChart.getDataList(); } catch(e) {}
                if (points[0].dataIndex !== undefined && points[1].dataIndex !== undefined) {
                    bars = Math.abs(points[1].dataIndex - points[0].dataIndex);
                } else if (data && data.length && points[0].timestamp && points[1].timestamp) {
                    // Estimate from timestamps
                    var dt = Math.abs(points[1].timestamp - points[0].timestamp);
                    if (data.length >= 2) {
                        var barMs = Math.abs(data[1].timestamp - data[0].timestamp);
                        if (barMs > 0) bars = Math.round(dt / barMs);
                    }
                }

                // Time duration string
                var timeStr = '';
                if (points[0].timestamp && points[1].timestamp) {
                    var dtMs = Math.abs(points[1].timestamp - points[0].timestamp);
                    var mins = Math.floor(dtMs / 60000);
                    var hrs = Math.floor(mins / 60);
                    var days = Math.floor(hrs / 24);
                    if (days > 0) {
                        timeStr = days + 'd ' + (hrs % 24) + 'h';
                    } else if (hrs > 0) {
                        timeStr = hrs + 'h ' + (mins % 60) + 'm';
                    } else {
                        timeStr = mins + 'm';
                    }
                }

                var figures = [];
                var blue = 'rgba(33, 150, 243, 0.12)';
                var blueLine = '#2196F3';

                // Background rect
                figures.push({
                    type: 'rect',
                    attrs: { x: xLeft, y: yTop, width: xRight - xLeft, height: yBottom - yTop },
                    styles: { style: 'fill', color: blue, borderColor: 'rgba(33, 150, 243, 0.3)', borderSize: 1 }
                });

                // Vertical line (price axis) — from point1 Y to point2 Y at point1 X
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: x1, y: y1 }, { x: x1, y: y2 }] },
                    styles: { color: arrowColor, size: 1.5 }
                });
                // Vertical arrowhead at y2
                var aw = 5, ah = 8;
                if (y2 > y1) {
                    // Arrow pointing down
                    figures.push({ type: 'polygon', attrs: { coordinates: [{ x: x1, y: y2 }, { x: x1 - aw, y: y2 - ah }, { x: x1 + aw, y: y2 - ah }] }, styles: { style: 'fill', color: arrowColor } });
                } else {
                    // Arrow pointing up
                    figures.push({ type: 'polygon', attrs: { coordinates: [{ x: x1, y: y2 }, { x: x1 - aw, y: y2 + ah }, { x: x1 + aw, y: y2 + ah }] }, styles: { style: 'fill', color: arrowColor } });
                }

                // Horizontal line (time axis) — from point1 X to point2 X at point2 Y
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: x1, y: y2 }, { x: x2, y: y2 }] },
                    styles: { color: blueLine, size: 1.5 }
                });
                // Horizontal arrowhead at x2
                if (x2 > x1) {
                    figures.push({ type: 'polygon', attrs: { coordinates: [{ x: x2, y: y2 }, { x: x2 - ah, y: y2 - aw }, { x: x2 - ah, y: y2 + aw }] }, styles: { style: 'fill', color: blueLine } });
                } else {
                    figures.push({ type: 'polygon', attrs: { coordinates: [{ x: x2, y: y2 }, { x: x2 + ah, y: y2 - aw }, { x: x2 + ah, y: y2 + aw }] }, styles: { style: 'fill', color: blueLine } });
                }

                // Price label (vertical axis) — positioned at midpoint of vertical line
                var midY = (y1 + y2) / 2;
                var pctText = (pctChange >= 0 ? '+' : '') + pctChange.toFixed(2) + '%';
                var priceText = (priceDiff >= 0 ? '+' : '') + fp(priceDiff);
                // Multiple ("2.50x") after the pct — how many x the move is
                var multText = (price1 > 0 && price2 > 0) ? '  ' + (price2 / price1).toFixed(2) + 'x' : '';
                var vertLabel = pctText + multText + '  (' + priceText + ')';
                var iFont = "'JetBrains Mono', monospace";
                var labelBg = isUp ? 'rgba(38, 166, 154, 0.85)' : 'rgba(239, 83, 80, 0.85)';
                figures.push({
                    type: 'text',
                    attrs: { x: x1 + 6, y: midY - 5, text: vertLabel },
                    styles: { color: '#fff', size: 11, weight: 'bold', family: iFont, backgroundColor: labelBg, borderColor: 'transparent', borderSize: 0, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
                });

                // Time label (horizontal axis) — positioned at midpoint of horizontal line
                var midX = (x1 + x2) / 2;
                var timeLabel = bars + ' bars';
                if (timeStr) timeLabel += '  ' + timeStr;
                figures.push({
                    type: 'text',
                    attrs: { x: midX - timeLabel.length * 3.2, y: y2 + 6, text: timeLabel },
                    styles: { color: '#d1d4dc', size: 11, weight: 'bold', family: iFont, backgroundColor: 'rgba(33, 150, 243, 0.85)', borderColor: 'transparent', borderSize: 0, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
                });

                return figures.concat(_selectionSquares(coordinates, overlay.id));
            }
        });
    } catch (e) { console.error('measureTool register error:', e); }

    // Alert line overlays — TWO tools share the alert machinery:
    //   'alertLine'  — the two-click BOUNDED segment (deliberately not
    //                  infinite);
    //   'alertLevel' — one-click INFINITE horizontal level.
    // Both pin a price tag on the y-axis strip and render extendData.note.
    // Each serves both producers (drawing tool + renderAlertOverlays'
    // locked alert_ov_* lines) — read state only from overlay/extendData.
    function _alertFp(p) {
        var a = Math.abs(p);
        if (a >= 1) return p.toFixed(2);
        if (a >= 0.01) return p.toFixed(4);
        return p.toFixed(6);
    }
    // The DOM last-price tag (.candle-countdown, z-index 10) always wins the
    // z-fight — a canvas tag under it is invisible. countdown-timer.js
    // publishes its rect; nudge our tag just clear of it, leaving the line
    // itself at the true price y.
    function _alertAxisTagY(y, tagRect) {
        if (!tagRect || tagRect.y == null) return y;
        var overlaps = (y + 8) >= tagRect.y && (y - 8) <= (tagRect.y + tagRect.h);
        if (!overlaps) return y;
        var mid = tagRect.y + tagRect.h / 2;
        return (y <= mid) ? (tagRect.y - 9) : (tagRect.y + tagRect.h + 9);
    }
    // Shared y-axis price tag for both alert overlays. Chart space is
    // ln(price) under log scale — use the same inverse the countdown tag
    // uses so the two can never disagree.
    function _alertYAxisFigures(params) {
        try {
        var overlay = params.overlay || {};
        var points = overlay.points || [];
        var coordinates = params.coordinates || [];
        var ext = overlay.extendData || {};
        if (!coordinates[0] || points.length < 1) return [];
        var b = params.bounding || { width: 0, height: 0 };
        var y = coordinates[0].y;
        if (b.height && (y < 0 || y > b.height)) return [];
        var chartVal = points[0].value;
        if (chartVal == null || !isFinite(chartVal)) return [];
        var price = (typeof carracFromChartSpace === 'function') ? carracFromChartSpace(chartVal) : chartVal;
        if (price == null || !isFinite(price)) return [];
        var color = ext._color || '#ff9800';

        // Chevron vs live price — linear vs linear
        var cur = 0;
        try { var _la = allAssets[klineChartSymbol]; cur = _la ? _la.latest_price : 0; } catch(e) {}
        var arrow = (cur > 0) ? (price >= cur ? '▲ ' : '▼ ') : '';

        var active = overlay.id === klineSelectedOverlayId || overlay.id === _hoveredOverlayId;
        var tagY = _alertAxisTagY(y, (typeof window !== 'undefined') ? window._carracAxisTagRect : null);
        var iFont = "'JetBrains Mono', monospace";
        return [
            { type: 'rect', ignoreEvent: true,
              attrs: { x: 0, y: tagY - 8, width: b.width, height: 16 },
              styles: { style: 'fill', color: color, borderColor: active ? '#ffffff' : 'transparent', borderSize: active ? 1 : 0 } },
            { type: 'text', ignoreEvent: true,
              attrs: { x: 4, y: tagY, baseline: 'middle', text: arrow + _alertFp(price) },
              styles: { color: '#ffffff', size: 10, weight: 'bold', family: iFont, backgroundColor: color, borderColor: 'transparent', borderSize: 0, paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0 } }
        ];
        } catch (err) { return []; }
    }
    // User note (right-click → Edit Note) — also synced onto the server
    // alert's note field so AI/MCP tools can read it
    function _alertNoteFigure(ext, x, y, color) {
        if (!ext.note) return null;
        return {
            type: 'text', ignoreEvent: true,
            attrs: { x: x, y: y - 7, text: String(ext.note) },
            styles: { color: color, size: 11, family: "'JetBrains Mono', monospace", backgroundColor: 'rgba(19,23,34,0.72)', borderColor: 'transparent', borderSize: 0, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
        };
    }
    try {
        klinecharts.registerOverlay({
            name: 'alertLine',
            totalStep: 3,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                try {
                var coordinates = params.coordinates;
                var overlay = params.overlay || {};
                var points = overlay.points || [];
                var ext = overlay.extendData || {};
                if (coordinates.length < 2 || points.length < 2) return [];

                var chartVal = points[0].value;
                if (chartVal == null || !isFinite(chartVal)) return [];
                var color = ext._color || '#ff9800';
                var x1 = coordinates[0].x, x2 = coordinates[1].x;
                // Force both Y coords to the same price level (horizontal line)
                var y = coordinates[0].y;

                var xLeft = Math.min(x1, x2), xRight = Math.max(x1, x2);
                if (xRight - xLeft < 30) xRight = xLeft + 100;

                var figures = [];
                // Invisible wide hit-test line for hover/click
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: xLeft, y: y }, { x: xRight, y: y }] },
                    styles: { color: 'transparent', size: 12 }
                });
                // Main line — bounded segment (alertLine is never infinite)
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: xLeft, y: y }, { x: xRight, y: y }] },
                    styles: { color: color, size: ext._width || 1.5 }
                });
                // Endpoint dots
                figures.push({ type: 'circle', attrs: { x: xLeft, y: y, r: 3 }, styles: { style: 'fill', color: color } });
                figures.push({ type: 'circle', attrs: { x: xRight, y: y, r: 3 }, styles: { style: 'fill', color: color } });

                // Floating price label at the right end — LINEAR price (chart
                // space is ln under log scale; a raw print would show the ln value)
                var price = (typeof carracFromChartSpace === 'function') ? carracFromChartSpace(chartVal) : chartVal;
                if (price != null && isFinite(price)) {
                    var cur = 0;
                    try { cur = allAssets[klineChartSymbol] ? allAssets[klineChartSymbol].latest_price : 0; } catch(e) {}
                    var dirArrow = (cur > 0) ? (price >= cur ? '▲ ' : '▼ ') : '';
                    figures.push({
                        type: 'text',
                        attrs: { x: xRight + 8, y: y - 6, text: dirArrow + _alertFp(price) },
                        styles: { color: '#fff', size: 11, weight: 'bold', family: "'JetBrains Mono', monospace", backgroundColor: color, borderColor: 'transparent', borderSize: 0, paddingLeft: 5, paddingRight: 5, paddingTop: 2, paddingBottom: 2 }
                    });
                }
                var noteFig = _alertNoteFigure(ext, xLeft + 8, y, color);
                if (noteFig) figures.push(noteFig);
                return figures.concat(_selectionSquares(coordinates, overlay.id));
                } catch (err) { console.error('alertLine render error:', err); return []; }
            },
            createYAxisFigures: _alertYAxisFigures
        });
    } catch (e) { console.error('alertLine register error:', e); }

    // One-click infinite alert level — single point, full-width
    // dashed line, same y-axis tag + note as alertLine.
    try {
        klinecharts.registerOverlay({
            name: 'alertLevel',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                try {
                var coordinates = params.coordinates;
                var overlay = params.overlay || {};
                var points = overlay.points || [];
                var ext = overlay.extendData || {};
                if (!coordinates || coordinates.length < 1 || points.length < 1) return [];
                var chartVal = points[0].value;
                if (chartVal == null || !isFinite(chartVal)) return [];
                var color = ext._color || '#ff9800';
                var y = coordinates[0].y;
                var w = (params.bounding && params.bounding.width) || 0;

                var figures = [];
                // Invisible wide hit-test line for hover/click
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: 0, y: y }, { x: w, y: y }] },
                    styles: { color: 'transparent', size: 12 }
                });
                // Main line — full pane width; honours the context-bar width picker
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: 0, y: y }, { x: w, y: y }] },
                    styles: { color: color, size: ext._width || 1.5, style: 'dashed', dashedValue: [8, 4] }
                });
                var noteFig = _alertNoteFigure(ext, 8, y, color);
                if (noteFig) figures.push(noteFig);
                return figures.concat(_selectionSquares(coordinates, overlay.id));
                } catch (err) { console.error('alertLevel render error:', err); return []; }
            },
            createYAxisFigures: _alertYAxisFigures
        });
    } catch (e) { console.error('alertLevel register error:', e); }

    // Register milestonePin overlay for milestone price markers
    try {
        klinecharts.registerOverlay({
            name: 'milestonePin',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                var overlay = params.overlay || {};
                var ext = overlay.extendData || {};
                if (!coordinates.length) return [];

                var y = coordinates[0].y;
                var x = coordinates[0].x;
                var color = ext.color || '#787b86';
                var status = ext.status || 'dormant';
                var label = ext.label || '';
                var actionLabel = ext.actionLabel || '';
                var iFont = "'JetBrains Mono', monospace";

                // Opacity based on status
                var alpha = status === 'dormant' ? 0.25 : status === 'watching' ? 0.5 : status === 'triggered' ? 1.0 : status === 'completed' ? 0.7 : 0.2;
                var lineAlpha = alpha * 0.5;

                // Get chart width from bounding (approximate via coordinates range)
                var chartW = 2000; // fallback
                try {
                    var container = document.getElementById('kline-chart-container');
                    if (container) chartW = container.offsetWidth - 60; // minus y-axis
                } catch(e) {}

                var figures = [];

                // Dashed horizontal line from pin to right edge
                figures.push({
                    type: 'line',
                    attrs: { coordinates: [{ x: Math.max(x, 40), y: y }, { x: chartW, y: y }] },
                    styles: { color: color, size: 0.8, style: 'dashed', dashedValue: [3, 3] }
                });

                // Pin dot
                var dotR = status === 'triggered' ? 5 : 4;
                var dotStyle = (status === 'completed' || status === 'triggered') ? 'fill' : 'stroke';
                figures.push({
                    type: 'circle',
                    attrs: { x: chartW, y: y, r: dotR },
                    styles: { style: dotStyle, color: color, borderColor: color, borderSize: 1.5 }
                });

                // Label at right edge
                var dispLabel = actionLabel + (label ? ': ' + label : '');
                if (dispLabel.length > 24) dispLabel = dispLabel.substring(0, 22) + '..';
                figures.push({
                    type: 'text',
                    attrs: { x: chartW + 4, y: y - 5, text: dispLabel },
                    styles: { color: '#fff', size: 9, family: iFont, backgroundColor: color, borderColor: 'transparent', borderSize: 0, paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1 }
                });

                return figures;
            },
            createYAxisFigures: function(params) {
                var overlay = params.overlay || {};
                var ext = overlay.extendData || {};
                var coordinates = params.coordinates;
                if (!coordinates.length) return [];

                var y = coordinates[0].y;
                var price = (overlay.points || [])[0];
                if (!price) return [];

                var color = ext.color || '#787b86';
                var status = ext.status || 'dormant';
                if (status === 'dormant' || status === 'skipped') return []; // Don't clutter y-axis for inactive pins

                function fp(p) {
                    var a = Math.abs(p);
                    if (a >= 1000) return p.toFixed(0);
                    if (a >= 1) return p.toFixed(2);
                    if (a >= 0.01) return p.toFixed(4);
                    return p.toFixed(6);
                }

                return [{
                    type: 'text',
                    attrs: { x: 1, y: y - 5, text: fp(price.value) },
                    styles: { color: '#fff', size: 9, family: "'JetBrains Mono', monospace", backgroundColor: color, borderColor: 'transparent', borderSize: 0, paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1 }
                }];
            }
        });
    } catch (e) { console.error('milestonePin register error:', e); }

    // Arrow overlay — 2-point line with arrowhead at endpoint
    try {
        klinecharts.registerOverlay({
            name: 'arrow',
            totalStep: 3,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                try {
                var coords = params.coordinates;
                var overlay = params.overlay || {};
                var ext = overlay.extendData || {};
                if (!coords || coords.length < 2) return [];
                var color = ext._color || ((overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#d1d4dc');
                var lw = ext._width || ((overlay.styles && overlay.styles.line && overlay.styles.line.size) || 1.5);
                var figures = [];
                var p1 = coords[0], p2 = coords[1];
                // Invisible hit-test
                figures.push({ type: 'line', attrs: { coordinates: [p1, p2] }, styles: { color: 'transparent', size: 12 } });
                // Main line
                figures.push({ type: 'line', attrs: { coordinates: [p1, p2] }, styles: { color: color, size: lw } });
                // Arrowhead at p2
                var angle = Math.atan2(p2.y - p1.y, p2.x - p1.x);
                var sz = Math.max(8, lw * 4);
                var cos = Math.cos(angle), sin = Math.sin(angle);
                figures.push({
                    type: 'polygon',
                    attrs: {
                        coordinates: [
                            { x: p2.x, y: p2.y },
                            { x: p2.x - sz * cos + sz * 0.4 * sin, y: p2.y - sz * sin - sz * 0.4 * cos },
                            { x: p2.x - sz * cos - sz * 0.4 * sin, y: p2.y - sz * sin + sz * 0.4 * cos }
                        ]
                    },
                    styles: { style: 'fill', color: color }
                });
                return figures.concat(_selectionSquares(coords, overlay.id));
                } catch(e) { return []; }
            }
        });
    } catch (e) {}

    // Pitchfork (Andrews') — 3-point median line with parallel channels
    try {
        klinecharts.registerOverlay({
            name: 'pitchfork',
            totalStep: 4,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                try {
                var coords = params.coordinates;
                var overlay = params.overlay || {};
                var ext = overlay.extendData || {};
                var bounding = params.bounding;
                if (!coords || coords.length < 3) return [];
                var color = ext._color || ((overlay.styles && overlay.styles.line && overlay.styles.line.color) || '#d1d4dc');
                var lw = ext._width || ((overlay.styles && overlay.styles.line && overlay.styles.line.size) || 1.5);
                var figures = [];
                // Points: P0 = pivot, P1 = left swing, P2 = right swing
                var p0 = coords[0], p1 = coords[1], p2 = coords[2];
                // Median point between P1 and P2
                var midX = (p1.x + p2.x) / 2, midY = (p1.y + p2.y) / 2;
                // Median line: P0 → mid, extended to chart edge
                var dx = midX - p0.x, dy = midY - p0.y;
                var len = Math.sqrt(dx * dx + dy * dy);
                if (len < 1) return [];
                // Extend median line to bounding edge
                var t = Math.max(bounding.width, bounding.height) * 2 / len;
                var medEnd = { x: p0.x + dx * t, y: p0.y + dy * t };
                // Parallel offsets: P1 and P2 offsets from median
                var offX1 = p1.x - midX, offY1 = p1.y - midY;
                var offX2 = p2.x - midX, offY2 = p2.y - midY;
                // Upper parallel: through P1, parallel to median
                var upper1 = { x: p1.x, y: p1.y };
                var upper2 = { x: p1.x + dx * t, y: p1.y + dy * t };
                // Lower parallel: through P2, parallel to median
                var lower1 = { x: p2.x, y: p2.y };
                var lower2 = { x: p2.x + dx * t, y: p2.y + dy * t };
                // Invisible hit-test
                figures.push({ type: 'line', attrs: { coordinates: [p0, medEnd] }, styles: { color: 'transparent', size: 12 } });
                // Median line (main)
                figures.push({ type: 'line', attrs: { coordinates: [p0, medEnd] }, styles: { color: color, size: lw } });
                // Upper parallel
                figures.push({ type: 'line', attrs: { coordinates: [upper1, upper2] }, styles: { color: color, size: lw * 0.8 } });
                // Lower parallel
                figures.push({ type: 'line', attrs: { coordinates: [lower1, lower2] }, styles: { color: color, size: lw * 0.8 } });
                // Connection lines P0→P1, P0→P2 (dashed)
                figures.push({ type: 'line', attrs: { coordinates: [p0, p1] }, styles: { color: color, size: 0.8, style: 'dashed', dashedValue: [4, 3] } });
                figures.push({ type: 'line', attrs: { coordinates: [p0, p2] }, styles: { color: color, size: 0.8, style: 'dashed', dashedValue: [4, 3] } });
                // Fill between upper and lower (subtle)
                figures.push({
                    type: 'polygon',
                    attrs: { coordinates: [upper1, upper2, lower2, lower1] },
                    styles: { style: 'fill', color: (typeof hexToRgba === 'function') ? hexToRgba(color, 0.04) : 'rgba(150,150,150,0.04)' }
                });
                return figures.concat(_selectionSquares(coords, overlay.id));
                } catch(e) { return []; }
            }
        });
    } catch (e) {}

    // Register scenarioPath overlay for expected price scenarios
    try {
        klinecharts.registerOverlay({
            name: 'scenarioPath',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                var ext = (params.overlay && params.overlay.extendData) || {};
                if (!coords || coords.length < 2) return [];

                // Color: prefer explicit _color (toolbar), fall back to direction-based (legacy v1)
                var color;
                if (ext._color) {
                    color = ext._color;
                } else {
                    var isBullish = ext.direction === 'bullish';
                    color = isBullish ? '#26a69a' : (ext.direction === 'neutral' ? '#787b86' : '#ef5350');
                }
                var dimmed = ext._dimmed;
                if (dimmed) {
                    // Parse color to rgba with low opacity
                    color = color.startsWith('rgba') ? color : (color.startsWith('#') ? hexToRgba(color, 0.25) : 'rgba(150,150,150,0.25)');
                }
                var figures = [];
                var lw = dimmed ? 1 : (ext._width || 2);

                // Solid lines between consecutive waypoints
                for (var i = 0; i < coords.length - 1; i++) {
                    figures.push({
                        type: 'line',
                        attrs: { coordinates: [coords[i], coords[i + 1]] },
                        styles: { color: color, size: lw }
                    });
                }

                // Circle at each waypoint — scenario furniture only. AI
                // scenarios always stamp extendData.direction; the
                // hand-drawn Path tool reuses this overlay WITHOUT it and
                // must not look permanently selected (its selection
                // feedback is _selectionSquares below).
                if (ext.direction) {
                    for (var j = 0; j < coords.length; j++) {
                        figures.push({
                            type: 'circle',
                            attrs: { x: coords[j].x, y: coords[j].y, r: dimmed ? 3 : 6 },
                            styles: { style: 'fill', color: color }
                        });
                    }
                }

                // Arrow at the END of the last segment only
                if (coords.length >= 2) {
                    var last = coords[coords.length - 1];
                    var prev = coords[coords.length - 2];
                    var angle = Math.atan2(last.y - prev.y, last.x - prev.x);
                    var sz = dimmed ? 5 : 8;
                    var cos = Math.cos(angle), sin = Math.sin(angle);
                    figures.push({
                        type: 'polygon',
                        attrs: {
                            coordinates: [
                                { x: last.x + sz * cos, y: last.y + sz * sin },
                                { x: last.x - sz * cos + sz * 0.6 * sin, y: last.y - sz * sin - sz * 0.6 * cos },
                                { x: last.x - sz * cos - sz * 0.6 * sin, y: last.y - sz * sin + sz * 0.6 * cos }
                            ]
                        },
                        styles: { style: 'fill', color: color }
                    });
                }

                // Name label near first point
                if (ext.name && !dimmed) {
                    figures.push({
                        type: 'text',
                        attrs: { x: coords[0].x + 8, y: coords[0].y - 14, text: ext.name },
                        styles: { color: color, size: 11, family: "'JetBrains Mono', monospace", weight: 'bold' }
                    });
                }

                return figures.concat(_selectionSquares(coords, (params.overlay || {}).id));
            }
        });
    } catch (e) {}

    // Register elliottWave overlay for Elliott Wave annotations
    try {
        klinecharts.registerOverlay({
            name: 'elliottWave',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                var ext = (params.overlay && params.overlay.extendData) || {};
                if (!coords || coords.length < 2) return [];

                var color = ext._color || '#ff9800';
                var dimmed = ext._dimmed;
                if (dimmed) {
                    color = color.startsWith('#') ? hexToRgba(color, 0.25) : 'rgba(150,150,150,0.25)';
                }
                var figures = [];
                var lw = dimmed ? 1 : (ext._width || 2);
                var labels = ext._labels || [];
                var r = dimmed ? 8 : 12;

                // Lines between consecutive points
                for (var i = 0; i < coords.length - 1; i++) {
                    figures.push({
                        type: 'line',
                        attrs: { coordinates: [coords[i], coords[i + 1]] },
                        styles: { color: color, size: lw }
                    });
                }

                // Circled labels at each point
                for (var j = 0; j < coords.length; j++) {
                    var label = (j < labels.length) ? labels[j] : '';
                    if (label) {
                        // Labeled point: circle bg + text
                        figures.push({
                            type: 'circle',
                            attrs: { x: coords[j].x, y: coords[j].y, r: r },
                            styles: { style: 'fill', color: color }
                        });
                        figures.push({
                            type: 'text',
                            attrs: { x: coords[j].x, y: coords[j].y, text: label, align: 'center', baseline: 'middle' },
                            styles: { color: '#fff', size: dimmed ? 9 : 11, weight: 'bold', family: "'JetBrains Mono', monospace" }
                        });
                    } else {
                        // Origin point: small circle, no label
                        figures.push({
                            type: 'circle',
                            attrs: { x: coords[j].x, y: coords[j].y, r: dimmed ? 3 : 5 },
                            styles: { style: 'fill', color: color }
                        });
                    }
                }

                // Wave type label near origin
                if (ext._waveType && !dimmed) {
                    var typeLabel = ext._waveType === 'impulse' ? 'Impulse' : (ext._waveType === 'correction' ? 'Correction' : 'Triangle');
                    figures.push({
                        type: 'text',
                        attrs: { x: coords[0].x + 10, y: coords[0].y - 16, text: typeLabel },
                        styles: { color: color, size: 10, family: "'JetBrains Mono', monospace", style: 'italic' }
                    });
                }

                return figures.concat(_selectionSquares(coords, (params.overlay || {}).id));
            }
        });
    } catch (e) {}

    // Register Anchored VWAP overlay — single click to anchor, VWAP line extends forward
    // The cumulative VWAP series only depends on the candles + anchor (not the
    // viewport), so it's memoized per-overlay and only recomputed when the anchor, bar count,
    // or the still-forming last bar (ts/close) actually changes — crosshair moves and pans
    // just reuse it and re-project to pixels. The per-bar segments are also collapsed into a
    // single polyline figure (klinecharts v9 'line' figures draw through N-point coordinates
    // arrays natively — verified against klinecharts.min.js) instead of one figure per bar.
    var _vwapSeriesCache = {}; // overlay.id -> { key, series: [{timestamp, value}, ...] }
    try {
        klinecharts.registerOverlay({
            name: 'anchoredVwap',
            totalStep: 2,
            needDefaultPointFigure: true,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coordinates = params.coordinates;
                var overlay = params.overlay || {};
                var points = overlay.points || [];
                if (!coordinates || coordinates.length < 1 || !points[0]) return [];
                if (!klineChart) return [];

                var data = null;
                try { data = klineChart.getDataList(); } catch(e) { return []; }
                if (!data || data.length === 0) return [];

                // Find anchor index — try timestamp first, fall back to dataIndex
                var anchorIdx = -1;
                var anchorTs = points[0].timestamp;
                if (anchorTs) {
                    for (var i = 0; i < data.length; i++) {
                        if (data[i].timestamp >= anchorTs) { anchorIdx = i; break; }
                    }
                }
                if (anchorIdx < 0 && points[0].dataIndex != null) {
                    anchorIdx = Math.max(0, Math.min(points[0].dataIndex, data.length - 1));
                }
                if (anchorIdx < 0) return [];

                var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);

                // Memo key: anchor + bar count + the still-forming last bar's ts/close (once a
                // bar closes its OHLCV is final, so only the last bar can still move the sum) +
                // log-scale mode (the sum below reinterprets hi/lo/cl under log scale, so a
                // toggle must invalidate too — it always implicitly did by recomputing every repaint).
                var _lastBar = data[data.length - 1];
                var _vwapKey = (anchorTs != null ? ('t' + anchorTs) : ('i' + points[0].dataIndex)) + '|' + data.length + '|' + (_lastBar ? _lastBar.timestamp : '') + '|' + (_lastBar ? _lastBar.close : '') + '|' + (_isLog ? 1 : 0);
                var _cachedVwap = _vwapSeriesCache[overlay.id];
                var series;
                if (_cachedVwap && _cachedVwap.key === _vwapKey) {
                    series = _cachedVwap.series;
                } else {
                    // Calculate VWAP from anchor to last real candle only (data-space, no pixels yet)
                    series = [];
                    var cumPV = 0, cumVol = 0;
                    var _now = Date.now();
                    for (var j = anchorIdx; j < data.length; j++) {
                        var d = data[j];
                        // Stop at future/placeholder bars
                        if (!d.volume || d.volume <= 0) continue;
                        if (d.timestamp && d.timestamp > _now + 86400000) break;
                        var hi = d.high, lo = d.low, cl = d.close;
                        if (_isLog && hi > 0) { hi = Math.exp(hi); lo = Math.exp(lo); cl = Math.exp(cl); }
                        var tp = (hi + lo + cl) / 3;
                        var vol = d.volume;
                        cumPV += tp * vol;
                        cumVol += vol;
                        var vwap = cumPV / cumVol;
                        var plotVal = _isLog ? Math.log(vwap) : vwap;
                        series.push({ timestamp: d.timestamp, value: plotVal });
                    }
                    _vwapSeriesCache[overlay.id] = { key: _vwapKey, series: series };
                }
                if (series.length < 1) return [];

                // Project the (memoized) series to pixels — viewport-dependent, so this still
                // runs every repaint regardless of cache hit/miss, but it's now just the cheap
                // coordinate transform (no re-summing, no re-branching on log scale per bar).
                var vwapPoints = [];
                for (var k = 0; k < series.length; k++) {
                    try {
                        var px = klineChart.convertToPixel({ timestamp: series[k].timestamp, value: series[k].value }, { paneId: 'candle_pane' });
                        if (px && isFinite(px.x) && isFinite(px.y)) {
                            vwapPoints.push({ x: px.x, y: px.y });
                        }
                    } catch(ex) {}
                }
                if (vwapPoints.length < 1) return [];

                var ext = overlay.extendData || {};
                var color = ext._color || '#2196F3';
                var lw = ext._width || 2.5;
                var figures = [];

                // Draw VWAP as ONE polyline through every point (no extension beyond last candle).
                // klinecharts' 'line' figure draws AND hit-tests through every adjacent pair in
                // `coordinates` (see ie/$t in klinecharts.min.js), so this is the same per-segment
                // hit surface as the old one-figure-per-bar version, just without the per-bar
                // figure-object allocation on every repaint.
                if (vwapPoints.length >= 2) {
                    figures.push({
                        type: 'line',
                        attrs: { coordinates: vwapPoints },
                        styles: { color: color, size: lw }
                    });
                }

                // Anchor marker
                var anchorPx = vwapPoints[0];
                figures.push({
                    type: 'circle',
                    attrs: { x: anchorPx.x, y: anchorPx.y, r: 5 },
                    styles: { style: 'fill', color: color }
                });

                // Label near anchor
                figures.push({
                    type: 'text',
                    attrs: { x: anchorPx.x + 8, y: anchorPx.y - 12, text: 'VWAP' },
                    styles: { color: color, size: 10, family: "'JetBrains Mono', monospace", weight: 'bold' }
                });

                // Invisible hit-test line along the VWAP path for hover/selection
                if (vwapPoints.length >= 2) {
                    figures.push({ type: 'line', attrs: { coordinates: [vwapPoints[0], vwapPoints[vwapPoints.length - 1]] }, styles: { color: 'transparent', size: 12 } });
                }

                return figures.concat(_selectionSquares(coordinates, overlay.id));
            }
        });
    } catch (e) {}

    window._chartTypesRegistered = true;
}
