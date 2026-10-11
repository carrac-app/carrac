// chart-position-overlays.js — the position/spot P&L figure cluster,
// Nothing here depends on renderKlineChart's scope (klineChart and friends
// are module globals). _selectionSquares lives here too; the overlay
// definitions in chart-overlay-defs.js resolve it as a global at call time.
// The registerOverlay calls for 'position'/'longPosition'/'shortPosition'/
// 'spot' stay in renderKlineChart's chart-init block (chart-render.js) and
// late-bind these names.
// Contract sentinel: createPositionFigures.

// Shared blue square selection handles — used by all drawing overlays
function _selectionSquares(coords, overlayId) {
if (!overlayId || (overlayId !== klineSelectedOverlayId && overlayId !== _hoveredOverlayId)) return [];
var hs = 5;
var hStyle = { style: 'fill', color: '#2962ff', borderColor: '#ffffff', borderSize: 1.5 };
var figs = [];
for (var i = 0; i < coords.length; i++) {
    if (coords[i]) figs.push({ type: 'rect', attrs: { x: coords[i].x - hs, y: coords[i].y - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
}
return figs;
}

// Fade an arbitrary drawing colour to a given alpha — used by the
// drag-guideline extension segments (createPositionFigures) so the
// full-pane-width preview reads as a faded echo of the bounded box
// rather than a second full-strength line. #rgb/#rrggbb → rgba(...);
// an existing rgba()/rgb() string keeps its own r,g,b and swaps in
// the new alpha; anything else falls back to the neutral grid grey.
function _fadeColor(color, alpha) {
    if (typeof color === 'string') {
        var hexM = color.match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
        if (hexM) {
            var hex = hexM[1];
            if (hex.length === 3) {
                hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
            }
            var r = parseInt(hex.substr(0, 2), 16);
            var g = parseInt(hex.substr(2, 2), 16);
            var b = parseInt(hex.substr(4, 2), 16);
            return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
        }
        var rgbM = color.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)$/i);
        if (rgbM) {
            return 'rgba(' + rgbM[1] + ',' + rgbM[2] + ',' + rgbM[3] + ',' + alpha + ')';
        }
    }
    return 'rgba(209,212,220,' + alpha + ')';
}

// Position R-math helper — RAW prices only (chart space is ln() under
// log scale; ratios of ln values are meaningless). Shared by the
// painter labels, the TP-zone hover readout and the armed-tool
// preview (kline-drawings.js reads it via window).
function carracPositionStats(priceRaw, entryRaw, slRaw) {
    var isLong = slRaw < entryRaw;
    var pct = isLong ? ((priceRaw - entryRaw) / entryRaw * 100)
                     : ((entryRaw - priceRaw) / entryRaw * 100);
    var risk = Math.abs(entryRaw - slRaw);
    var r = null, rLabel = '';
    if (entryRaw !== 0 && risk / Math.abs(entryRaw) >= 1e-9) {
        r = Math.abs(priceRaw - entryRaw) / risk;
        rLabel = (r >= 10 ? String(Math.round(r)) : r.toFixed(1).replace(/\.0$/, '')) + 'R';
    }
    return { pct: pct, r: r, rLabel: rLabel };
}
window.carracPositionStats = carracPositionStats;
// Chart-space → raw price (log-safe); identity fallback pre-load.
function _posRaw(v) {
    return (typeof carracFromChartSpace === 'function') ? carracFromChartSpace(v) : v;
}

function createPositionFigures(params) {
    var coordinates = params.coordinates;
    var overlay = params.overlay || {};
    var points = overlay.points || [];
    var ext = overlay.extendData || {};
    if (coordinates.length < 2 || points.length < 2) return [];

    var entryPrice = points[0].value;
    var slPrice = points[1].value;
    var hasTP = coordinates.length >= 3 && points.length >= 3;
    var tpPrice = hasTP ? points[2].value : (2 * entryPrice - slPrice);
    var entryY = coordinates[0].y;
    var slY = coordinates[1].y;
    var tpY = hasTP ? coordinates[2].y : (2 * entryY - slY);

    // Auto-detect direction from SL relative to entry
    var isLong = slPrice < entryPrice;

    // Raw-price twins for labels + metrics (log-safe: chart space is ln(price) under log scale — geometry keeps
    // chart-space values, every label/percent/R uses raw).
    var entryRaw = _posRaw(entryPrice), slRaw = _posRaw(slPrice), tpRaw = _posRaw(tpPrice);

    // Bounded x range
    // Use ext.rightTs for true right edge when present.
    // _positionXRight helper in kline-drawings.js holds the shared rule;
    // duplicate the logic here (cross-file call not possible from the painter closure).
    var xs = coordinates.map(function(c) { return c.x; });
    var xLeft = Math.min.apply(null, xs);
    var xPointsMax = Math.max.apply(null, xs);
    var xRight;
    var _extRightTs = ext && typeof ext.rightTs === 'number' && isFinite(ext.rightTs) ? ext.rightTs : null;
    if (_extRightTs !== null) {
        // Resolve rightTs pixel via window.klineChart (live during paint)
        try {
            var _rPx = window.klineChart.convertToPixel({ timestamp: _extRightTs, value: points[0].value }, { paneId: overlay.paneId || 'candle_pane' });
            xRight = (_rPx && typeof _rPx.x === 'number') ? _rPx.x : xPointsMax;
        } catch(e) { xRight = xPointsMax; }
    } else {
        // Legacy: no rightTs — synthetic 200px minimum width
        xRight = xPointsMax;
        if (xRight - xLeft < 60) xRight = xLeft + 200;
    }
    var boxW = xRight - xLeft;

    // Metrics
    var lossPct = isLong
        ? ((entryRaw - slRaw) / entryRaw * 100)
        : ((slRaw - entryRaw) / entryRaw * 100);

    // Use module-level helper (dedup)
    var fmtPrice = _fmtPriceSmart;

    var bounding = params.bounding;
    var figures = [];
    var iFont = "'JetBrains Mono', monospace";
    var centerX = (xLeft + xRight) / 2;
    var xEdge = bounding.width;

    // Full-width guidelines while this overlay's level/body is being
    // dragged (flag set by kline-drawings.js around _positionLevelDrag)
    var _guideOn = (typeof window !== 'undefined') && window._positionGuidelinesFor === overlay.id;

    // Level line + (while dragging) faded full-pane-width extension
    // segments. The bounded [xLeft→xRight] segment always keeps the
    // level's normal full-strength style; the [0→xLeft] and
    // [xRight→xEdge] extensions only appear while _guideOn, in the
    // same colour faded to `fadeAlpha`, and are skipped whenever
    // they'd have <= 0 width (e.g. xLeft already at the pane edge).
    function _pushLevelLine(y, color, size, dashedValue, fadeAlpha) {
        figures.push({
            type: 'line',
            attrs: { coordinates: [{x: xLeft, y: y}, {x: xRight, y: y}] },
            styles: { color: color, size: size, dashedValue: dashedValue }
        });
        if (_guideOn) {
            var _faded = _fadeColor(color, fadeAlpha);
            if (xLeft > 0) {
                figures.push({
                    type: 'line', ignoreEvent: true,
                    attrs: { coordinates: [{x: 0, y: y}, {x: xLeft, y: y}] },
                    styles: { color: _faded, size: size, dashedValue: dashedValue }
                });
            }
            if (xEdge > xRight) {
                figures.push({
                    type: 'line', ignoreEvent: true,
                    attrs: { coordinates: [{x: xRight, y: y}, {x: xEdge, y: y}] },
                    styles: { color: _faded, size: size, dashedValue: dashedValue }
                });
            }
        }
    }

    // All fill/text figures have ignoreEvent so clicks pass through to chart for panning.
    // Only corner handles (added below) are interactive.

    // SL zone (purple fill = loss) — always shown once SL is placed
    figures.push({
        type: 'rect',
        attrs: { x: xLeft, y: Math.min(entryY, slY), width: boxW, height: Math.abs(slY - entryY) },
        styles: { style: 'fill', color: 'rgba(171, 71, 188, 0.08)', borderColor: 'rgba(171, 71, 188, 0.25)', borderSize: 1 }
    });
    // Entry line — bounded dashed. Honour the user's picked colour
    // (extendData._color, set by recreateOverlayWithStyles) when present so
    // the drawing-context-bar colour swatches actually take effect (the
    // default is '#d1d4dc'). SL/TP keep their semantic colours
    // (purple = loss, gold = profit).
    var _userColor = (ext && ext._color) ? ext._color : '#d1d4dc';
    _pushLevelLine(entryY, _userColor, 1.5, [4, 4], 0.35);
    // SL line — bounded
    _pushLevelLine(slY, '#AB47BC', 1, [6, 3], 0.35);
    // Extension lines for entry + SL (ignoreEvent so Y-axis zoom works)
    // — skipped while guidelines are on (the faded drag-guideline
    // extensions from _pushLevelLine take over instead)
    if (!_guideOn) {
        figures.push({
            type: 'line', ignoreEvent: true,
            attrs: { coordinates: [{x: xRight, y: entryY}, {x: xEdge, y: entryY}] },
            styles: { color: 'rgba(209, 212, 220, 0.2)', size: 1, dashedValue: [3, 6] }
        });
        figures.push({
            type: 'line', ignoreEvent: true,
            attrs: { coordinates: [{x: xRight, y: slY}, {x: xEdge, y: slY}] },
            styles: { color: 'rgba(171, 71, 188, 0.2)', size: 1, dashedValue: [3, 6] }
        });
    }
    // SL label
    var slText = '[SL ' + fmtPrice(slRaw) + '  -' + Math.abs(lossPct).toFixed(2) + '%]';
    figures.push({
        type: 'text',
        attrs: { x: centerX - slText.length * 3.3, y: slY - 4, text: slText },
        styles: { color: '#AB47BC', size: 11, weight: 'italic', family: iFont, backgroundColor: 'rgba(48, 26, 54, 0.85)', borderColor: 'rgba(171, 71, 188, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
    });
    // Entry label (direction only shown once TP is placed)
    var dirLabel = isLong ? 'LONG' : 'SHORT';
    var entryText = hasTP ? '[' + dirLabel + ' ' + fmtPrice(entryRaw) + ']' : '[Entry ' + fmtPrice(entryRaw) + ']';
    figures.push({
        type: 'text',
        attrs: { x: centerX - entryText.length * 3.3, y: entryY - 4, text: entryText },
        styles: { color: _userColor, size: 11, weight: 'italic', family: iFont, backgroundColor: 'rgba(42, 46, 57, 0.85)', borderColor: 'rgba(99, 104, 116, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
    });

    // TP zone + labels only shown after 3rd click (TP placed)
    if (hasTP) {
        var profitPct = isLong
            ? ((tpRaw - entryRaw) / entryRaw * 100)
            : ((entryRaw - tpRaw) / entryRaw * 100);
        var rrRatio = lossPct !== 0 ? Math.abs(profitPct / lossPct) : Infinity;

        // TP zone (gold fill = profit)
        figures.push({
            type: 'rect',
            attrs: { x: xLeft, y: Math.min(entryY, tpY), width: boxW, height: Math.abs(tpY - entryY) },
            styles: { style: 'fill', color: 'rgba(255, 215, 0, 0.08)', borderColor: 'rgba(255, 215, 0, 0.25)', borderSize: 1 }
        });
        // TP line
        _pushLevelLine(tpY, '#FFD700', 1, [6, 3], 0.35);
        // TP extension line
        if (!_guideOn) {
            figures.push({
                type: 'line', ignoreEvent: true,
                attrs: { coordinates: [{x: xRight, y: tpY}, {x: xEdge, y: tpY}] },
                styles: { color: 'rgba(255, 215, 0, 0.2)', size: 1, dashedValue: [3, 6] }
            });
        }
        // Intermediate TP levels
        var intermediateTPs = ext.tps || [];
        intermediateTPs.forEach(function(itp, idx) {
            var tpFraction = (entryPrice !== tpPrice) ? (itp - entryPrice) / (tpPrice - entryPrice) : 0;
            var itpY = entryY + tpFraction * (tpY - entryY);
            var itpRaw = _posRaw(itp);
            var itpStats = carracPositionStats(itpRaw, entryRaw, slRaw);
            _pushLevelLine(itpY, 'rgba(255, 215, 0, 0.6)', 1, [4, 4], 0.25);
            var itpText = '[TP' + (idx + 1) + ' ' + fmtPrice(itpRaw) + '  ' + (itpStats.pct >= 0 ? '+' : '') + itpStats.pct.toFixed(2) + '%' + (itpStats.rLabel ? ' · ' + itpStats.rLabel : '') + ']';
            figures.push({
                type: 'text',
                attrs: { x: centerX - itpText.length * 3.3, y: itpY - 4, text: itpText },
                styles: { color: '#FFD700', size: 10, weight: 'italic', family: iFont, backgroundColor: 'rgba(58, 50, 10, 0.75)', borderColor: 'rgba(255, 215, 0, 0.2)', borderSize: 1, paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1 }
            });
        });
        // Final TP label
        var tpLabel = intermediateTPs.length > 0 ? 'TP' + (intermediateTPs.length + 1) : 'TP';
        var tpStats = carracPositionStats(tpRaw, entryRaw, slRaw);
        var tpText = '[' + tpLabel + ' ' + fmtPrice(tpRaw) + '  ' + (tpStats.pct >= 0 ? '+' : '') + tpStats.pct.toFixed(2) + '%' + (tpStats.rLabel ? ' · ' + tpStats.rLabel : '') + ']';
        figures.push({
            type: 'text',
            attrs: { x: centerX - tpText.length * 3.3, y: tpY - 4, text: tpText },
            styles: { color: '#FFD700', size: 11, weight: 'italic', family: iFont, backgroundColor: 'rgba(58, 50, 10, 0.85)', borderColor: 'rgba(255, 215, 0, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
        });
        // R:R label below entry
        var rrText = '[R:R ' + (rrRatio === Infinity ? '\u221E' : rrRatio.toFixed(2)) + ']';
        figures.push({
            type: 'text',
            attrs: { x: centerX - rrText.length * 3.3, y: entryY + 14, text: rrText },
            styles: { color: '#ff9800', size: 11, weight: 'italic bold', family: iFont, backgroundColor: 'rgba(58, 47, 26, 0.85)', borderColor: 'rgba(255, 152, 0, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 }
        });
    }

    // Width-gate center labels when box is too narrow to display them cleanly
    // (skip labels only, keep fills/lines/handles)
    if (boxW < 90) {
        // Remove text figures to avoid overflow on narrow boxes
        for (var _fi = figures.length - 1; _fi >= 0; _fi--) {
            if (figures[_fi].type === 'text') figures.splice(_fi, 1);
        }
    }

    // TradingView-style resize handles — shown on hover/selection
    // Handle squares grown 10×10 → 12×12 (hs=6 half-size); edge tabs added
    var isPosHovered = (typeof _hoveredPositionId !== 'undefined') && overlay.id === _hoveredPositionId;
    var isPosSelected = (typeof klineSelectedOverlayId !== 'undefined') && overlay.id === klineSelectedOverlayId;
    // multi-select outline — treat as hovered for visual purposes
    var isPosMultiSelected = (typeof window !== 'undefined' && window.klineMultiSelectIds && window.klineMultiSelectIds.indexOf(overlay.id) !== -1);
    // subtle full-box border tint on hover (not selected) — shows the box extent instantly
    if ((isPosHovered && !isPosSelected && !isPosMultiSelected) || isPosMultiSelected) {
        var _boxTop = hasTP ? Math.min(entryY, slY, tpY) : Math.min(entryY, slY);
        var _boxBot = hasTP ? Math.max(entryY, slY, tpY) : Math.max(entryY, slY);
        figures.push({
            type: 'rect', ignoreEvent: true,
            attrs: { x: xLeft, y: _boxTop, width: boxW, height: _boxBot - _boxTop },
            styles: { style: 'stroke_fill', color: 'transparent', borderColor: 'rgba(209,212,220,0.25)', borderSize: 1 }
        });
    }
    if (isPosHovered || isPosSelected || isPosMultiSelected) {
        var hs = 6; // handle half-size (12x12 squares, was 5/10x10)
        var hFill = '#2962ff';
        var hBorder = '#ffffff';
        var hBorderW = 1.5;
        var hStyle = { style: 'fill', color: hFill, borderColor: hBorder, borderSize: hBorderW };
        var boxMidY = (Math.min(entryY, slY) + Math.max(entryY, hasTP ? tpY : slY)) / 2;
        // Entry-left handle (same size as others now)
        figures.push({ type: 'rect', attrs: { x: xLeft - hs, y: entryY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
        // Entry-right handle (right edge dot)
        figures.push({ type: 'rect', attrs: { x: xRight - hs, y: entryY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
        // SL handle — bottom-left
        figures.push({ type: 'rect', attrs: { x: xLeft - hs, y: slY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
        if (hasTP) {
            // TP handle — top-left
            figures.push({ type: 'rect', attrs: { x: xLeft - hs, y: tpY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
            // Intermediate TP handles
            var _iTPs = ext.tps || [];
            for (var _ih = 0; _ih < _iTPs.length; _ih++) {
                var _itpFrac = (entryPrice !== tpPrice) ? (_iTPs[_ih] - entryPrice) / (tpPrice - entryPrice) : 0;
                var _itpY = entryY + _itpFrac * (tpY - entryY);
                figures.push({ type: 'rect', attrs: { x: xLeft - hs, y: _itpY - hs, width: hs * 2, height: hs * 2 }, styles: hStyle });
            }
        }
        // Edge tabs: 6×20px tabs centered vertically on the box at xLeft and xRight
        // These give a visible affordance for the edge drag strips (18px/15px hit zone)
        var tabH = 20, tabW = 6;
        var tabYTop = boxMidY - tabH / 2;
        var tabStyle = { style: 'fill', color: 'rgba(41,98,255,0.7)', borderColor: '#ffffff', borderSize: 1 };
        figures.push({ type: 'rect', attrs: { x: xLeft - tabW / 2, y: tabYTop, width: tabW, height: tabH }, styles: tabStyle });
        figures.push({ type: 'rect', attrs: { x: xRight - tabW / 2, y: tabYTop, width: tabW, height: tabH }, styles: tabStyle });
    }

    // Stamp ignoreEvent here (once, in place) instead of the registrations
    // cloning the whole array via .map(Object.assign) on every repaint — klinecharts v9
    // re-runs createPointFigures on every crosshair move/pan tick, so the old double-clone
    // (~11-25 objects, twice) was pure allocation churn.
    for (var _ignIdx = 0; _ignIdx < figures.length; _ignIdx++) { figures[_ignIdx].ignoreEvent = true; }
    return figures;
}

function createPositionYAxisFigures(params) {
    var coordinates = params.coordinates;
    var overlay = params.overlay || {};
    var points = overlay.points || [];
    var ext = overlay.extendData || {};
    if (coordinates.length < 2 || points.length < 2) return [];
    var entryPrice = points[0].value, slPrice = points[1].value;
    var hasTP = coordinates.length >= 3 && points.length >= 3;
    var tpPrice = hasTP ? points[2].value : (2 * entryPrice - slPrice);
    var entryY = coordinates[0].y, slY = coordinates[1].y;
    var tpY = hasTP ? coordinates[2].y : (2 * entryY - slY);
    function fp(p) {
        var s = Math.abs(p) >= 1000 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6));
        var parts = s.split('.'); parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); return parts.join('.');
    }
    var b = params.bounding, w = b.width, h = 16, figures = [];
    var ts = { size: 11, family: "'JetBrains Mono', monospace", backgroundColor: 'transparent', borderColor: 'transparent' };
    // brighter labels on hover/selection — full-alpha text + stronger border
    var _yIsActive = ((typeof _hoveredPositionId !== 'undefined') && overlay.id === _hoveredPositionId) ||
                     ((typeof klineSelectedOverlayId !== 'undefined') && overlay.id === klineSelectedOverlayId);
    var _entryBg   = _yIsActive ? 'rgba(42, 46, 57, 1.0)' : 'rgba(42, 46, 57, 0.9)';
    var _entryBdr  = _yIsActive ? 'rgba(99, 104, 116, 0.9)' : 'rgba(99, 104, 116, 0.4)';
    var _slBg      = _yIsActive ? 'rgba(48, 26, 54, 1.0)'   : 'rgba(48, 26, 54, 0.9)';
    var _slBdr     = _yIsActive ? 'rgba(171, 71, 188, 0.9)' : 'rgba(171, 71, 188, 0.4)';
    var _tpBg      = _yIsActive ? 'rgba(58, 50, 10, 1.0)'   : 'rgba(58, 50, 10, 0.9)';
    var _tpBdr     = _yIsActive ? 'rgba(255, 215, 0, 0.9)'  : 'rgba(255, 215, 0, 0.4)';
    var _entryTxt  = _yIsActive ? '#ffffff'   : '#d1d4dc';
    var _slTxt     = _yIsActive ? '#CE93D8'   : '#AB47BC';
    var _tpTxt     = _yIsActive ? '#FFE57F'   : '#FFD700';
    // All Y-axis figures are display-only — ignoreEvent prevents KLineChart drag
    // Prices routed through _posRaw so tags stay real prices under log scale
    // Entry
    figures.push({ type: 'rect', ignoreEvent: true, attrs: { x: 0, y: entryY - h / 2, width: w, height: h }, styles: { style: 'fill', color: _entryBg, borderColor: _entryBdr, borderSize: 1 } });
    figures.push({ type: 'text', ignoreEvent: true, attrs: { x: 4, y: entryY, text: fp(_posRaw(entryPrice)), baseline: 'middle' }, styles: Object.assign({ color: _entryTxt }, ts) });
    // SL
    figures.push({ type: 'rect', ignoreEvent: true, attrs: { x: 0, y: slY - h / 2, width: w, height: h }, styles: { style: 'fill', color: _slBg, borderColor: _slBdr, borderSize: 1 } });
    figures.push({ type: 'text', ignoreEvent: true, attrs: { x: 4, y: slY, text: fp(_posRaw(slPrice)), baseline: 'middle' }, styles: Object.assign({ color: _slTxt }, ts) });
    // TP + intermediate TPs only after 3rd click
    if (hasTP) {
        figures.push({ type: 'rect', ignoreEvent: true, attrs: { x: 0, y: tpY - h / 2, width: w, height: h }, styles: { style: 'fill', color: _tpBg, borderColor: _tpBdr, borderSize: 1 } });
        figures.push({ type: 'text', ignoreEvent: true, attrs: { x: 4, y: tpY, text: fp(_posRaw(tpPrice)), baseline: 'middle' }, styles: Object.assign({ color: _tpTxt }, ts) });
        var intermediateTPs = ext.tps || [];
        intermediateTPs.forEach(function(itp) {
            var tpFrac = (entryPrice !== tpPrice) ? (itp - entryPrice) / (tpPrice - entryPrice) : 0;
            var itpY = entryY + tpFrac * (tpY - entryY);
            var _itpBg  = _yIsActive ? 'rgba(58, 50, 10, 1.0)' : 'rgba(58, 50, 10, 0.7)';
            var _itpBdr = _yIsActive ? 'rgba(255, 215, 0, 0.7)' : 'rgba(255, 215, 0, 0.3)';
            figures.push({ type: 'rect', ignoreEvent: true, attrs: { x: 0, y: itpY - h / 2, width: w, height: h }, styles: { style: 'fill', color: _itpBg, borderColor: _itpBdr, borderSize: 1 } });
            figures.push({ type: 'text', ignoreEvent: true, attrs: { x: 4, y: itpY, text: fp(_posRaw(itp)), baseline: 'middle' }, styles: Object.assign({ color: _tpTxt }, ts) });
        });
    }
    return figures;
}

// Spot Buy overlay — triangle markers at entry + profit-colored avg line
function _getSpotAggregateData() {
    try {
        var store = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var spots = (store[klineChartSymbol] || []).filter(function(d) {
            return d.name === 'spot' && d.points && d.points.length >= 1 && !(d.extendData && d.extendData.sold);
        });
        if (spots.length === 0) return null;
        var totalQty = 0, totalUsd = 0;
        spots.forEach(function(s) {
            var e = s.extendData || {};
            totalQty += e.quantity || 0;
            totalUsd += e.usdAmount || 0;
        });
        var avgEntry = totalQty > 0 ? totalUsd / totalQty : spots[0].points[0].value;
        var sortedIds = spots.map(function(s) { return s.id; }).sort();
        return { avgEntry: avgEntry, totalQty: totalQty, totalUsd: totalUsd, firstId: sortedIds[0], count: spots.length };
    } catch(e) { return null; }
}

function createSpotFigures(params) {
    var coordinates = params.coordinates;
    var overlay = params.overlay || {};
    var points = overlay.points || [];
    var ext = overlay.extendData || {};
    if (!coordinates || coordinates.length < 1 || !points[0]) return [];

    var entryPrice = points[0].value;
    var entryX = coordinates[0].x;
    var entryY = coordinates[0].y;
    var bounding = params.bounding;
    var xEdge = bounding.width;
    var iFont = "'JetBrains Mono', monospace";

    var currentPrice = 0;
    if (typeof allAssets !== 'undefined' && typeof klineChartSymbol !== 'undefined' && allAssets[klineChartSymbol]) {
        currentPrice = allAssets[klineChartSymbol].latest_price || 0;
    }

    // Use module-level helper (dedup)
    var fmtPrice = _fmtPriceSmart;
    var pnlPct = currentPrice > 0 && entryPrice > 0 ? ((currentPrice - entryPrice) / entryPrice * 100) : 0;
    var pnlColor = Math.abs(pnlPct) < 0.1 ? '#787b86' : (pnlPct >= 0 ? '#26a69a' : '#ef5350');
    var spotColor = '#4db6ac';

    var figures = [];

    // Small label near entry
    var markerLabel = ext.label || '$' + fmtPrice(entryPrice);
    figures.push({
        type: 'text', ignoreEvent: true,
        attrs: { x: entryX + 10, y: entryY - 4, text: markerLabel },
        styles: {
            color: spotColor, size: 10, family: iFont,
            backgroundColor: 'rgba(26, 45, 42, 0.7)',
            borderColor: 'rgba(77, 182, 172, 0.15)', borderSize: 1,
            paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1
        }
    });

    // Triangle below the candle (profit-aware color)
    try {
        if (klineChart) {
            var chartData = klineChart.getDataList();
            if (chartData && chartData.length > 0) {
                var entryTs = points[0].timestamp;
                for (var di = 0; di < chartData.length; di++) {
                    if (chartData[di].timestamp >= entryTs) {
                        var candleLow = chartData[di].low;
                        var lowPx = klineChart.convertToPixel({ timestamp: chartData[di].timestamp, value: candleLow }, { paneId: 'candle_pane' });
                        if (lowPx && isFinite(lowPx.x) && isFinite(lowPx.y)) {
                            figures.push({
                                type: 'text', ignoreEvent: true,
                                attrs: { x: lowPx.x - 9, y: lowPx.y + 20, text: '\u25B2' },
                                styles: { color: currentPrice > 0 ? pnlColor : spotColor, size: 20, family: iFont }
                            });
                        }
                        break;
                    }
                }
            }
        }
    } catch(e) {}

    // Aggregate average line — drawn by first spot overlay only
    var agg = _getSpotAggregateData();
    if (agg && overlay.id === agg.firstId && agg.avgEntry > 0 && klineChart) {
        try {
            var data2 = klineChart.getDataList();
            var lastTs = data2 && data2.length > 0 ? data2[data2.length - 1].timestamp : points[0].timestamp;
            var avgPx = klineChart.convertToPixel({ timestamp: lastTs, value: agg.avgEntry }, { paneId: 'candle_pane' });
            if (avgPx && isFinite(avgPx.y)) {
                var avgY = avgPx.y;
                var avgPnl = currentPrice > 0 ? ((currentPrice - agg.avgEntry) / agg.avgEntry * 100) : 0;
                var avgLineColor = Math.abs(avgPnl) < 0.1 ? '#787b86' : (avgPnl >= 0 ? '#26a69a' : '#ef5350');
                // Full-width dashed profit-colored line (subtle)
                var _avgLineAlpha = (typeof hexToRgba === 'function') ? hexToRgba(avgLineColor, 0.6) : avgLineColor;
                figures.push({
                    type: 'line', ignoreEvent: true,
                    attrs: { coordinates: [{ x: 0, y: avgY }, { x: xEdge, y: avgY }] },
                    styles: { style: 'dashed', color: _avgLineAlpha, size: 1, dashedValue: [6, 5] }
                });
                // Avg label — above line when profit, below when loss
                // P&L label is rendered as a DOM element (_updateSpotPnlLabel)
                // so it can update live from WebSocket without overlay re-render.
            }
        } catch(e) {}
    }

    return figures.concat(_selectionSquares(coordinates, overlay.id));
}

function createSpotYAxisFigures(params) {
    var overlay = params.overlay || {};
    var points = overlay.points || [];
    if (!points[0]) return [];
    var iFont = "'JetBrains Mono', monospace";
    function fp(p) {
        var s = Math.abs(p) >= 1000 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6));
        var parts = s.split('.'); parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); return parts.join('.');
    }

    // Only show Y-axis label for the aggregate avg (drawn by first overlay)
    var agg = _getSpotAggregateData();
    if (agg && overlay.id === agg.firstId && agg.avgEntry > 0 && klineChart) {
        try {
            var data = klineChart.getDataList();
            var lastTs = data && data.length > 0 ? data[data.length - 1].timestamp : points[0].timestamp;
            var avgPx = klineChart.convertToPixel({ timestamp: lastTs, value: agg.avgEntry }, { paneId: 'candle_pane' });
            if (avgPx && isFinite(avgPx.y)) {
                var currentPrice = 0;
                if (typeof allAssets !== 'undefined' && typeof klineChartSymbol !== 'undefined' && allAssets[klineChartSymbol]) {
                    currentPrice = allAssets[klineChartSymbol].latest_price || 0;
                }
                var avgPnl = currentPrice > 0 ? ((currentPrice - agg.avgEntry) / agg.avgEntry * 100) : 0;
                var yColor = Math.abs(avgPnl) < 0.1 ? '#787b86' : (avgPnl >= 0 ? '#26a69a' : '#ef5350');
                var yBg = avgPnl >= 0 ? 'rgba(20, 45, 35, 0.65)' : 'rgba(50, 25, 25, 0.65)';
                var b = params.bounding, w = b.width, h = 16;
                var ts = { size: 11, family: iFont, backgroundColor: 'transparent', borderColor: 'transparent' };
                return [
                    { type: 'rect', ignoreEvent: true, attrs: { x: 0, y: avgPx.y - h / 2, width: w, height: h }, styles: { style: 'fill', color: yBg, borderColor: yColor + '44', borderSize: 1 } },
                    { type: 'text', ignoreEvent: true, attrs: { x: 4, y: avgPx.y, text: fp(agg.avgEntry), baseline: 'middle' }, styles: Object.assign({ color: yColor + 'BB' }, ts) }
                ];
            }
        } catch(e) {}
    }
    return [];
}

// Live spot P&L label — DOM element updated every 3s from WS price.
// Bypasses KLineChart's overlay render cache entirely.
function _updateSpotPnlLabel() {
    var el = document.getElementById('spot-pnl-label');
    var container = document.getElementById('kline-chart-container');
    if (!klineChart || !klineChartSymbol || !container) {
        if (el) el.style.display = 'none';
        return;
    }
    var agg = _getSpotAggregateData();
    if (!agg || agg.avgEntry <= 0) {
        if (el) el.style.display = 'none';
        return;
    }
    var currentPrice = 0;
    if (typeof allAssets !== 'undefined' && allAssets[klineChartSymbol]) {
        currentPrice = allAssets[klineChartSymbol].latest_price || 0;
    }
    if (currentPrice <= 0) { if (el) el.style.display = 'none'; return; }

    var avgPnl = ((currentPrice - agg.avgEntry) / agg.avgEntry * 100);
    var usdPnl = (currentPrice - agg.avgEntry) * agg.totalQty;
    var pnlSign = usdPnl >= 0 ? '+' : '-';
    var pctSign = avgPnl >= 0 ? '+' : '-';
    function _fc(n) { return Math.abs(n).toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
    function _fp(p) {
        var s = Math.abs(p) >= 1000 ? p.toFixed(2) : (Math.abs(p) >= 1 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6)));
        var parts = s.split('.'); parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); return parts.join('.');
    }
    var text = '[AVG $' + _fp(agg.avgEntry) + '  ' + pnlSign + '$' + _fc(usdPnl) + ' (' + pctSign + Math.abs(avgPnl).toFixed(1) + '%)]';
    var isProfit = avgPnl >= 0;
    var color = Math.abs(avgPnl) < 0.1 ? 'rgba(120,123,134,0.65)' : (isProfit ? 'rgba(38,166,154,0.65)' : 'rgba(239,83,80,0.65)');
    var bg = isProfit ? 'rgba(20,45,35,0.55)' : 'rgba(50,25,25,0.55)';
    var border = isProfit ? 'rgba(38,166,154,0.25)' : 'rgba(239,83,80,0.25)';

    // Position using convertToPixel
    try {
        var dl = klineChart.getDataList();
        if (!dl || !dl.length) { if (el) el.style.display = 'none'; return; }
        var li = dl.length - 1;
        while (li > 0 && dl[li]._phantom) li--;
        var avgPx = klineChart.convertToPixel({ timestamp: dl[li].timestamp, value: agg.avgEntry }, { paneId: 'candle_pane' });
        if (!avgPx || !isFinite(avgPx.y)) { if (el) el.style.display = 'none'; return; }

        if (!el) {
            el = document.createElement('div');
            el.id = 'spot-pnl-label';
            el.style.cssText = 'position:absolute;right:90px;pointer-events:none;z-index:15;font:italic bold 11px "JetBrains Mono",monospace;padding:2px 4px;border-radius:2px;border:1px solid;white-space:nowrap';
            container.appendChild(el);
        }
        el.textContent = text;
        el.style.color = color;
        el.style.backgroundColor = bg;
        el.style.borderColor = border;
        var _labelY = isProfit ? avgPx.y - 22 : avgPx.y + 4;
        // Hide if label would overflow into sub-panes
        // Use convertToPixel with extreme values to find candle pane visual bounds
        var _candlePaneBottom = container.offsetHeight;
        try {
            // Find the tallest, widest canvas — that's the candle pane main canvas
            var _allCanvases = container.querySelectorAll('canvas');
            var _maxArea = 0;
            for (var _ci = 0; _ci < _allCanvases.length; _ci++) {
                var _cArea = _allCanvases[_ci].offsetWidth * _allCanvases[_ci].offsetHeight;
                if (_cArea > _maxArea) {
                    _maxArea = _cArea;
                    _candlePaneBottom = _allCanvases[_ci].offsetHeight;
                }
            }
        } catch(e) {}
        if (_labelY + 16 > _candlePaneBottom || _labelY < 0) { el.style.display = 'none'; return; }
        el.style.top = _labelY + 'px';
        el.style.display = 'block';
    } catch(e) { if (el) el.style.display = 'none'; }
}
