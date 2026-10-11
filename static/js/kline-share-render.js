// Shared frozen-drawing renderer for klinecharts share surfaces.
// Extracted VERBATIM from templates/chart_headless.html so the public interactive share viewer (share-chart-viewer.js)
// and the headless screenshot page render drawings through ONE code path
// instead of a fourth accidental renderer.
(function() {
    'use strict';

    function registerOverlays() {
        // Register position overlays for headless/share rendering.
        // Point order: [0]=Entry, [1]=SL, [2]=TP. Auto-detect long/short from SL vs entry.
        function createPositionFigures(params) {
            var coordinates = params.coordinates;
            var overlay = params.overlay || {};
            var points = overlay.points || [];
            var ext = overlay.extendData || {};
            if (coordinates.length < 2 || points.length < 2) return [];
            var entryPrice = points[0].value, slPrice = points[1].value;
            var tpPrice = points.length >= 3 ? points[2].value : (2 * entryPrice - slPrice);
            var entryY = coordinates[0].y, slY = coordinates[1].y;
            var tpY = coordinates.length >= 3 ? coordinates[2].y : (2 * entryY - slY);
            var isLong = slPrice < entryPrice;
            var xs = coordinates.map(function(c) { return c.x; });
            var xLeft = Math.min.apply(null, xs), xRight = Math.max.apply(null, xs);
            if (xRight - xLeft < 60) xRight = xLeft + 200;
            var boxW = xRight - xLeft;
            var profitPct = isLong ? ((tpPrice - entryPrice) / entryPrice * 100) : ((entryPrice - tpPrice) / entryPrice * 100);
            var lossPct = isLong ? ((entryPrice - slPrice) / entryPrice * 100) : ((slPrice - entryPrice) / entryPrice * 100);
            var rrRatio = lossPct !== 0 ? Math.abs(profitPct / lossPct) : Infinity;
            // R multiple per TP — mirrors live painter's carracPositionStats label
            function rLab(price) {
                var risk = Math.abs(entryPrice - slPrice);
                if (!entryPrice || risk / Math.abs(entryPrice) < 1e-9) return '';
                var r = Math.abs(price - entryPrice) / risk;
                return ' · ' + (r >= 10 ? String(Math.round(r)) : r.toFixed(1).replace(/\.0$/, '')) + 'R';
            }
            function fp(p) { return Math.abs(p) >= 1 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6)); }
            var b = params.bounding, xEdge = b.width, figures = [];
            figures.push({ type: 'rect', attrs: { x: xLeft, y: Math.min(entryY, tpY), width: boxW, height: Math.abs(tpY - entryY) }, styles: { style: 'fill', color: 'rgba(255,215,0,0.08)', borderColor: 'rgba(255,215,0,0.25)', borderSize: 1 } });
            figures.push({ type: 'rect', attrs: { x: xLeft, y: Math.min(entryY, slY), width: boxW, height: Math.abs(slY - entryY) }, styles: { style: 'fill', color: 'rgba(171,71,188,0.08)', borderColor: 'rgba(171,71,188,0.25)', borderSize: 1 } });
            figures.push({ type: 'line', attrs: { coordinates: [{x:xLeft,y:entryY},{x:xRight,y:entryY}] }, styles: { color: '#d1d4dc', size: 1.5, dashedValue: [4,4] } });
            figures.push({ type: 'line', attrs: { coordinates: [{x:xLeft,y:tpY},{x:xRight,y:tpY}] }, styles: { color: '#FFD700', size: 1, dashedValue: [6,3] } });
            figures.push({ type: 'line', attrs: { coordinates: [{x:xLeft,y:slY},{x:xRight,y:slY}] }, styles: { color: '#AB47BC', size: 1, dashedValue: [6,3] } });
            // Intermediate TP levels
            var intermediateTPs = ext.tps || [];
            var f = "'JetBrains Mono', monospace", cx = (xLeft + xRight) / 2;
            intermediateTPs.forEach(function(itp, idx) {
                var tpFrac = (entryPrice !== tpPrice) ? (itp - entryPrice) / (tpPrice - entryPrice) : 0;
                var itpY = entryY + tpFrac * (tpY - entryY);
                var itpPct = isLong ? ((itp - entryPrice) / entryPrice * 100) : ((entryPrice - itp) / entryPrice * 100);
                figures.push({ type: 'line', attrs: { coordinates: [{x:xLeft,y:itpY},{x:xRight,y:itpY}] }, styles: { color: 'rgba(255,215,0,0.6)', size: 1, dashedValue: [4,4] } });
                var itpT = '[TP' + (idx+1) + ' ' + fp(itp) + '  ' + (itpPct>=0?'+':'') + itpPct.toFixed(2) + '%' + rLab(itp) + ']';
                figures.push({ type: 'text', attrs: { x: cx - itpT.length*3.3, y: itpY-4, text: itpT }, styles: { color: '#FFD700', size: 10, weight: 'italic', family: f, backgroundColor: 'rgba(58,50,10,0.75)', borderColor: 'rgba(255,215,0,0.2)', borderSize: 1, paddingLeft: 3, paddingRight: 3, paddingTop: 1, paddingBottom: 1 } });
            });
            // Subtle extension lines to right edge
            figures.push({ type: 'line', attrs: { coordinates: [{x:xRight,y:entryY},{x:xEdge,y:entryY}] }, styles: { color: 'rgba(209,212,220,0.2)', size: 1, dashedValue: [3,6] } });
            figures.push({ type: 'line', attrs: { coordinates: [{x:xRight,y:tpY},{x:xEdge,y:tpY}] }, styles: { color: 'rgba(255,215,0,0.2)', size: 1, dashedValue: [3,6] } });
            figures.push({ type: 'line', attrs: { coordinates: [{x:xRight,y:slY},{x:xEdge,y:slY}] }, styles: { color: 'rgba(171,71,188,0.2)', size: 1, dashedValue: [3,6] } });
            // TP/SL labels
            var tpLabel = intermediateTPs.length > 0 ? 'TP' + (intermediateTPs.length+1) : 'TP';
            var tpT = '[' + tpLabel + ' ' + fp(tpPrice) + '  ' + (profitPct >= 0 ? '+' : '') + profitPct.toFixed(2) + '%' + rLab(tpPrice) + ']';
            var slT = '[SL ' + fp(slPrice) + '  -' + Math.abs(lossPct).toFixed(2) + '%]';
            figures.push({ type: 'text', attrs: { x: cx - tpT.length * 3.3, y: tpY - 4, text: tpT }, styles: { color: '#FFD700', size: 11, weight: 'italic', family: f, backgroundColor: 'rgba(58, 50, 10, 0.85)', borderColor: 'rgba(255, 215, 0, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 } });
            figures.push({ type: 'text', attrs: { x: cx - slT.length * 3.3, y: slY - 4, text: slT }, styles: { color: '#AB47BC', size: 11, weight: 'italic', family: f, backgroundColor: 'rgba(48, 26, 54, 0.85)', borderColor: 'rgba(171, 71, 188, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 } });
            // Entry + R:R centered, italic
            var dirLabel = isLong ? 'LONG' : 'SHORT';
            var et = '[' + dirLabel + ' ' + fp(entryPrice) + ']', rt = '[R:R ' + (rrRatio === Infinity ? '\u221E' : rrRatio.toFixed(2)) + ']';
            figures.push({ type: 'text', attrs: { x: cx - et.length * 3.3, y: entryY - 4, text: et }, styles: { color: '#d1d4dc', size: 11, weight: 'italic', family: f, backgroundColor: 'rgba(42, 46, 57, 0.85)', borderColor: 'rgba(99, 104, 116, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 } });
            figures.push({ type: 'text', attrs: { x: cx - rt.length * 3.3, y: entryY + 14, text: rt }, styles: { color: '#ff9800', size: 11, weight: 'italic bold', family: f, backgroundColor: 'rgba(58, 47, 26, 0.85)', borderColor: 'rgba(255, 152, 0, 0.3)', borderSize: 1, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 } });
            // Right-edge Y-axis price badges
            var bW = 74, bH = 16, bX = xEdge - bW, ts = { backgroundColor: 'transparent', borderColor: 'transparent' };
            figures.push({ type: 'rect', attrs: { x: bX, y: entryY - bH/2, width: bW, height: bH }, styles: { style: 'fill', color: 'rgba(42, 46, 57, 0.9)', borderColor: 'rgba(99, 104, 116, 0.4)', borderSize: 1 } });
            figures.push({ type: 'text', attrs: { x: bX + 4, y: entryY - 4, text: fp(entryPrice) }, styles: Object.assign({ color: '#d1d4dc', size: 10, family: f }, ts) });
            figures.push({ type: 'rect', attrs: { x: bX, y: tpY - bH/2, width: bW, height: bH }, styles: { style: 'fill', color: 'rgba(58, 50, 10, 0.9)', borderColor: 'rgba(255, 215, 0, 0.4)', borderSize: 1 } });
            figures.push({ type: 'text', attrs: { x: bX + 4, y: tpY - 4, text: fp(tpPrice) }, styles: Object.assign({ color: '#FFD700', size: 10, family: f }, ts) });
            figures.push({ type: 'rect', attrs: { x: bX, y: slY - bH/2, width: bW, height: bH }, styles: { style: 'fill', color: 'rgba(48, 26, 54, 0.9)', borderColor: 'rgba(171, 71, 188, 0.4)', borderSize: 1 } });
            figures.push({ type: 'text', attrs: { x: bX + 4, y: slY - 4, text: fp(slPrice) }, styles: Object.assign({ color: '#AB47BC', size: 10, family: f }, ts) });
            // Intermediate TP Y-axis badges
            intermediateTPs.forEach(function(itp) {
                var tpFrac2 = (entryPrice !== tpPrice) ? (itp - entryPrice) / (tpPrice - entryPrice) : 0;
                var itpY2 = entryY + tpFrac2 * (tpY - entryY);
                figures.push({ type: 'rect', attrs: { x: bX, y: itpY2 - bH/2, width: bW, height: bH }, styles: { style: 'fill', color: 'rgba(58,50,10,0.7)', borderColor: 'rgba(255,215,0,0.3)', borderSize: 1 } });
                figures.push({ type: 'text', attrs: { x: bX + 4, y: itpY2 - 4, text: fp(itp) }, styles: Object.assign({ color: '#FFD700', size: 10, family: f }, ts) });
            });
            return figures;
        }
        try { klinecharts.registerOverlay({ name: 'position', totalStep: 4, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(p) { return createPositionFigures(p); } }); } catch(e) {}
        // Backward compat: old names point to same renderer
        try { klinecharts.registerOverlay({ name: 'longPosition', totalStep: 4, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(p) { return createPositionFigures(p); } }); } catch(e) {}
        try { klinecharts.registerOverlay({ name: 'shortPosition', totalStep: 4, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(p) { return createPositionFigures(p); } }); } catch(e) {}
        try {
            klinecharts.registerOverlay({
                name: 'boundedChannel',
                totalStep: 4,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    try {
                        var coords = params.coordinates;
                        var overlay = params.overlay || {};
                        var ext = overlay.extendData || {};
                        if (!coords || coords.length < 2) return [];
                        var color =
                            ext._color ||
                            (overlay.styles && overlay.styles.line && overlay.styles.line.color) ||
                            '#3fb950';
                        var width =
                            ext._width ||
                            (overlay.styles && overlay.styles.line && overlay.styles.line.size) ||
                            1;
                        var figs = [];
                        figs.push({
                            type: 'line',
                            attrs: { coordinates: [coords[0], coords[1]] },
                            styles: { color: color, size: width }
                        });
                        if (coords.length >= 3) {
                            var dx = coords[1].x - coords[0].x;
                            var t = dx !== 0 ? (coords[2].x - coords[0].x) / dx : 0;
                            var lineY = coords[0].y + t * (coords[1].y - coords[0].y);
                            var offY = coords[2].y - lineY;
                            figs.push({
                                type: 'line',
                                attrs: {
                                    coordinates: [
                                        { x: coords[0].x, y: coords[0].y + offY },
                                        { x: coords[1].x, y: coords[1].y + offY }
                                    ]
                                },
                                styles: { color: color, size: width }
                            });
                            figs.push({
                                type: 'line',
                                attrs: {
                                    coordinates: [
                                        { x: coords[0].x, y: coords[0].y + offY / 2 },
                                        { x: coords[1].x, y: coords[1].y + offY / 2 }
                                    ]
                                },
                                styles: { style: 'dashed', color: color, size: 1, dashedValue: [4, 4] }
                            });
                        }
                        return figs;
                    } catch (err) {
                        console.error('boundedChannel error:', err);
                        return [];
                    }
                }
            });
        } catch (e) {}
        var hFibLevels = [[0,'0'],[0.236,'0.236'],[0.382,'0.382'],[0.5,'0.5'],[0.618,'0.618'],[0.786,'0.786'],[1,'1'],[1.618,'1.618'],[2.618,'2.618']];
        var hFibCore = hFibLevels.slice(0,7);
        function hCreateFib(coords, overlay, full) {
            var p1 = coords[0],
                p2 = coords[1];
            var lvs = full ? hFibLevels : hFibCore;
            var x1, x2;
            if (full) {
                x1 = 0;
                x2 = 9999;
            } else {
                x1 = Math.min(p1.x, p2.x);
                x2 = Math.max(p1.x, p2.x);
            }
            var figs = [];
            var ext = (overlay && overlay.extendData) || {};
            var col =
                ext._color ||
                (overlay && overlay.styles && overlay.styles.line && overlay.styles.line.color
                    ? overlay.styles.line.color
                    : '#787b86');
            var bw =
                ext._width ||
                (overlay && overlay.styles && overlay.styles.line && overlay.styles.line.size
                    ? overlay.styles.line.size
                    : 1);
            lvs.forEach(function (pr) {
                var lv = pr[0],
                    r = pr[1];
                var y = p2.y + (p1.y - p2.y) * lv;
                var lb = (lv * 100).toFixed(1) + '%  (' + r + ')';
                figs.push({
                    type: 'line',
                    attrs: {
                        coordinates: [
                            { x: x1, y: y },
                            { x: x2, y: y }
                        ]
                    },
                    styles: {
                        style: lv === 0.5 ? 'solid' : 'dashed',
                        color: col,
                        size: lv === 0 || lv === 1 ? bw * 1.5 : bw,
                        dashedValue: [4, 3]
                    }
                });
                figs.push({
                    type: 'text',
                    attrs: { x: full ? x1 + 5 : x2 + 5, y: y, text: lb, align: 'left', baseline: 'middle' },
                    styles: { color: col, size: 10, family: 'JetBrains Mono, monospace' }
                });
            });
            return figs;
        }
        try { klinecharts.registerOverlay({ name: 'fibonacciRetrace', totalStep: 3, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(p) { var c=p.coordinates; if(!c||c.length<2)return[]; return hCreateFib(c,p.overlay,false); } }); } catch(e) {}
        try { klinecharts.registerOverlay({ name: 'fibonacciInfinite', totalStep: 3, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(p) { var c=p.coordinates; if(!c||c.length<2)return[]; return hCreateFib(c,p.overlay,true); } }); } catch(e) {}
        try {
            klinecharts.registerOverlay({
                name: 'textAnnotation',
                totalStep: 2,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var c = params.coordinates;
                    if (c.length > 0) {
                        var o = params.overlay || {};
                        var ext = o.extendData || {};
                        var text = ext.text || 'Text';
                        var fs = ext.fontSize || 14;
                        var col = ext.color || (o.styles && o.styles.line && o.styles.line.color) || '#d1d4dc';
                        var w = ext.bold ? 'bold' : 'normal';
                        var figs = [
                            {
                                type: 'text',
                                attrs: { x: c[0].x, y: c[0].y, text: text },
                                styles: {
                                    color: col,
                                    size: fs,
                                    family: "'JetBrains Mono', monospace",
                                    weight: w,
                                    style: ext.italic ? 'italic' : 'normal'
                                }
                            }
                        ];
                        if (ext.underline) {
                            var aw = text.length * fs * 0.6;
                            figs.push({
                                type: 'line',
                                attrs: {
                                    coordinates: [
                                        { x: c[0].x, y: c[0].y + fs * 0.25 },
                                        { x: c[0].x + aw, y: c[0].y + fs * 0.25 }
                                    ]
                                },
                                styles: { color: col, size: 1 }
                            });
                        }
                        if (ext.strikethrough) {
                            var aw2 = text.length * fs * 0.6;
                            figs.push({
                                type: 'line',
                                attrs: {
                                    coordinates: [
                                        { x: c[0].x, y: c[0].y + fs * 0.1 },
                                        { x: c[0].x + aw2, y: c[0].y + fs * 0.1 }
                                    ]
                                },
                                styles: { color: col, size: 1 }
                            });
                        }
                        return figs;
                    }
                    return [];
                }
            });
        } catch (e) {}
        try {
            klinecharts.registerOverlay({
                name: 'scenarioPath',
                totalStep: 2,
                needDefaultPointFigure: true,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var coords = params.coordinates;
                    var ext = (params.overlay && params.overlay.extendData) || {};
                    if (!coords || coords.length < 2) return [];
                    var color;
                    if (ext._color) {
                        color = ext._color;
                    } else {
                        var isBull = ext.direction === 'bullish';
                        color = isBull ? '#26a69a' : ext.direction === 'neutral' ? '#787b86' : '#ef5350';
                    }
                    var lw = ext._width || 2;
                    var figs = [];
                    for (var i = 0; i < coords.length - 1; i++) {
                        figs.push({
                            type: 'line',
                            attrs: { coordinates: [coords[i], coords[i + 1]] },
                            styles: { color: color, size: lw }
                        });
                    }
                    if (ext.direction) {
                        for (var j = 0; j < coords.length; j++) {
                            figs.push({
                                type: 'circle',
                                attrs: { x: coords[j].x, y: coords[j].y, r: 6 },
                                styles: { style: 'fill', color: color }
                            });
                        }
                    }
                    if (coords.length >= 2) {
                        var last = coords[coords.length - 1],
                            prev = coords[coords.length - 2];
                        var angle = Math.atan2(last.y - prev.y, last.x - prev.x);
                        var sz = 8,
                            cos = Math.cos(angle),
                            sin = Math.sin(angle);
                        figs.push({
                            type: 'polygon',
                            attrs: {
                                coordinates: [
                                    { x: last.x + sz * cos, y: last.y + sz * sin },
                                    {
                                        x: last.x - sz * cos + sz * 0.6 * sin,
                                        y: last.y - sz * sin - sz * 0.6 * cos
                                    },
                                    {
                                        x: last.x - sz * cos - sz * 0.6 * sin,
                                        y: last.y - sz * sin + sz * 0.6 * cos
                                    }
                                ]
                            },
                            styles: { style: 'fill', color: color }
                        });
                    }
                    if (ext.name) {
                        figs.push({
                            type: 'text',
                            attrs: { x: coords[0].x + 8, y: coords[0].y - 14, text: ext.name },
                            styles: {
                                color: color,
                                size: 11,
                                family: "'JetBrains Mono', monospace",
                                weight: 'bold'
                            }
                        });
                    }
                    return figs;
                }
            });
        } catch (e) {}
        try {
            klinecharts.registerOverlay({
                name: 'elliottWave',
                totalStep: 2,
                needDefaultPointFigure: true,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var coords = params.coordinates;
                    var ext = (params.overlay && params.overlay.extendData) || {};
                    if (!coords || coords.length < 2) return [];
                    var color = ext._color || '#ff9800';
                    var lw = ext._width || 2;
                    var labels = ext._labels || [];
                    var r = 12;
                    var figs = [];
                    for (var i = 0; i < coords.length - 1; i++) {
                        figs.push({
                            type: 'line',
                            attrs: { coordinates: [coords[i], coords[i + 1]] },
                            styles: { color: color, size: lw }
                        });
                    }
                    for (var j = 0; j < coords.length; j++) {
                        var label = j < labels.length ? labels[j] : '';
                        if (label) {
                            figs.push({
                                type: 'circle',
                                attrs: { x: coords[j].x, y: coords[j].y, r: r },
                                styles: { style: 'fill', color: color }
                            });
                            figs.push({
                                type: 'text',
                                attrs: { x: coords[j].x, y: coords[j].y - 7, text: label },
                                styles: {
                                    color: '#fff',
                                    size: 11,
                                    weight: 'bold',
                                    family: "'JetBrains Mono', monospace"
                                }
                            });
                        } else {
                            figs.push({
                                type: 'circle',
                                attrs: { x: coords[j].x, y: coords[j].y, r: 5 },
                                styles: { style: 'fill', color: color }
                            });
                        }
                    }
                    if (ext._waveType) {
                        var tl =
                            ext._waveType === 'impulse'
                                ? 'Impulse'
                                : ext._waveType === 'correction'
                                  ? 'Correction'
                                  : 'Triangle';
                        figs.push({
                            type: 'text',
                            attrs: { x: coords[0].x + 10, y: coords[0].y - 16, text: tl },
                            styles: {
                                color: color,
                                size: 10,
                                family: "'JetBrains Mono', monospace",
                                style: 'italic'
                            }
                        });
                    }
                    return figs;
                }
            });
        } catch (e) {}
        // Fixed v1 annotation glyph set — duplicated here (not loaded from
        // chart-overlay-defs.js/kline-drawings.js) so drawings render on the share/headless
        // page too. needDefaultPointFigure is false (matches textAnnotation above, NOT
        // chart-overlay-defs.js's true) because recreateOverlayFromMetadata below always sets a
        // VISIBLE styles.point color, unlike the live app's always-invisible one.
        function hHexToRgba(hex, a) { if (!hex || hex.charAt(0) !== '#') return hex; var r=0,g=0,b=0; if (hex.length===4){r=parseInt(hex[1]+hex[1],16);g=parseInt(hex[2]+hex[2],16);b=parseInt(hex[3]+hex[3],16);} else if (hex.length===7){r=parseInt(hex.substring(1,3),16);g=parseInt(hex.substring(3,5),16);b=parseInt(hex.substring(5,7),16);} return 'rgba('+r+','+g+','+b+','+a+')'; }
        function hCreateArrowMarker(coords, dir, ext) {
            var color = ext._color || '#3fb950';
            var w = ext._width || 1;
            var size = 7 + w * 3;
            var x = coords[0].x,
                y = coords[0].y;
            var tip, baseY;
            if (dir === 'up') {
                tip = { x: x, y: y - size };
                baseY = y + size * 0.6;
            } else {
                tip = { x: x, y: y + size };
                baseY = y - size * 0.6;
            }
            return [
                {
                    type: 'polygon',
                    attrs: {
                        coordinates: [tip, { x: x - size * 0.7, y: baseY }, { x: x + size * 0.7, y: baseY }]
                    },
                    styles: { style: 'fill', color: color }
                }
            ];
        }
        try { klinecharts.registerOverlay({ name: 'arrowUpMarker', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false, createPointFigures: function(params) { var c = params.coordinates; if (!c || c.length < 1) return []; var ext = (params.overlay && params.overlay.extendData) || {}; return hCreateArrowMarker(c, 'up', ext); } }); } catch(e) {}
        try {
            klinecharts.registerOverlay({
                name: 'arrowDownMarker',
                totalStep: 2,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var c = params.coordinates;
                    if (!c || c.length < 1) return [];
                    var ext = (params.overlay && params.overlay.extendData) || {};
                    return hCreateArrowMarker(c, 'down', ext);
                }
            });
        } catch (e) {}
        try {
            klinecharts.registerOverlay({
                name: 'flagPin',
                totalStep: 2,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var c = params.coordinates;
                    if (!c || c.length < 1) return [];
                    var ext = (params.overlay && params.overlay.extendData) || {};
                    var color = ext._color || '#f59e0b';
                    var w = ext._width || 1;
                    var poleH = 16 + w * 4;
                    var x = c[0].x,
                        y = c[0].y,
                        topY = y - poleH;
                    return [
                        {
                            type: 'line',
                            attrs: {
                                coordinates: [
                                    { x: x, y: y },
                                    { x: x, y: topY }
                                ]
                            },
                            styles: { color: color, size: Math.max(1.5, w) }
                        },
                        {
                            type: 'polygon',
                            attrs: {
                                coordinates: [
                                    { x: x, y: topY },
                                    { x: x + 14, y: topY + 5 },
                                    { x: x, y: topY + 10 }
                                ]
                            },
                            styles: { style: 'fill', color: color }
                        }
                    ];
                }
            });
        } catch (e) {}
        try {
            klinecharts.registerOverlay({
                name: 'circleMarker',
                totalStep: 2,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var c = params.coordinates;
                    if (!c || c.length < 1) return [];
                    var ext = (params.overlay && params.overlay.extendData) || {};
                    var color = ext._color || '#58a6ff';
                    var w = ext._width || 1;
                    var r = 6 + w * 2;
                    return [
                        {
                            type: 'circle',
                            attrs: { x: c[0].x, y: c[0].y, r: r },
                            styles: {
                                style: 'stroke_fill',
                                color: hHexToRgba(color, 0.2),
                                borderColor: color,
                                borderSize: Math.max(1.5, w)
                            }
                        }
                    ];
                }
            });
        } catch (e) {}
        try {
            klinecharts.registerOverlay({
                name: 'callout',
                totalStep: 2,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function (params) {
                    var c = params.coordinates;
                    if (!c || c.length < 1) return [];
                    var o = params.overlay || {};
                    var ext = o.extendData || {};
                    var text = ext.text || 'Note';
                    var fs = ext.fontSize || 12;
                    var color = ext._color || ext.color || '#d1d4dc';
                    var weight = ext.bold ? 'bold' : 'normal';
                    var fstyle = ext.italic ? 'italic' : 'normal';
                    var offsetX = ext.offsetX != null ? ext.offsetX : 60;
                    var offsetY = ext.offsetY != null ? ext.offsetY : -60;
                    var ax = c[0].x,
                        ay = c[0].y;
                    var tx = ax + offsetX,
                        ty = ay + offsetY;
                    var approxW = text.length * fs * 0.6;
                    var padX = 5,
                        padY = 4;
                    var boxW = approxW + padX * 2,
                        boxH = fs + padY * 2;
                    var boxX = offsetX >= 0 ? tx : tx - boxW;
                    var boxY = offsetY >= 0 ? ty : ty - boxH;
                    var figs = [
                        {
                            type: 'circle',
                            attrs: { x: ax, y: ay, r: 3 },
                            styles: { style: 'fill', color: color }
                        },
                        {
                            type: 'line',
                            attrs: {
                                coordinates: [
                                    { x: ax, y: ay },
                                    { x: tx, y: ty }
                                ]
                            },
                            styles: { color: color, size: 1, style: 'dashed', dashedValue: [3, 3] }
                        },
                        {
                            type: 'rect',
                            attrs: { x: boxX, y: boxY, width: boxW, height: boxH },
                            styles: {
                                style: 'stroke_fill',
                                color: hHexToRgba(color, 0.12),
                                borderColor: color,
                                borderSize: 1
                            }
                        },
                        {
                            type: 'text',
                            attrs: { x: boxX + padX, y: boxY + boxH / 2, text: text, baseline: 'middle' },
                            styles: {
                                color: color,
                                size: fs,
                                family: "'JetBrains Mono', monospace",
                                weight: weight,
                                style: fstyle
                            }
                        }
                    ];
                    if (ext.underline) {
                        figs.push({
                            type: 'line',
                            attrs: {
                                coordinates: [
                                    { x: boxX + padX, y: boxY + boxH / 2 + fs * 0.35 },
                                    { x: boxX + padX + approxW, y: boxY + boxH / 2 + fs * 0.35 }
                                ]
                            },
                            styles: { color: color, size: 1 }
                        });
                    }
                    if (ext.strikethrough) {
                        figs.push({
                            type: 'line',
                            attrs: {
                                coordinates: [
                                    { x: boxX + padX, y: boxY + boxH / 2 },
                                    { x: boxX + padX + approxW, y: boxY + boxH / 2 }
                                ]
                            },
                            styles: { color: color, size: 1 }
                        });
                    }
                    return figs;
                }
            });
        } catch (e) {}
        // verticalStraightLine needs no registration here — it's a klinecharts
        // NATIVE overlay, rendered by the library itself in every context.
    }

    function renderFrozenDrawings(chart, drawings, klineData) {
        var v2ScenarioIds = [];
        if (!chart || !Array.isArray(drawings) || !drawings.length ||
            !Array.isArray(klineData) || !klineData.length) {
            return { v2ScenarioIds: v2ScenarioIds };
        }
        var firstTs = klineData[0].timestamp;
        var lastTs = klineData[klineData.length - 1].timestamp;

        // Rich overlay recreation from metadata (new format)
        function recreateOverlayFromMetadata(d) {
            var meta = d.metadata;
            if (!meta.kline_name || !Array.isArray(meta.points) || !meta.points.length) return false;
            // Map old parallel channel names to boundedChannel
            var klineName = meta.kline_name;
            if (klineName === 'parallelChannel' || klineName === 'parallelStraightLine') klineName = 'boundedChannel';
            // Backward compat: old position overlays had [Entry, TP, SL] → swap to [Entry, SL, TP]
            if ((klineName === 'longPosition' || klineName === 'shortPosition') && meta.points.length >= 3) {
                var ptmp = meta.points[1]; meta.points[1] = meta.points[2]; meta.points[2] = ptmp;
                klineName = 'position';
            }

            var overlayConfig = {
                name: klineName,
                lock: true,
                points: meta.points,
                styles: {
                    line: { color: meta.color || d.color || '#ff0000', size: meta.width || d.line_width || 2 },
                    point: { color: meta.color || d.color || '#ff0000' }
                }
            };

            // Restore extendData (fill, text, fontSize, etc.)
            if (meta.extend_data && Object.keys(meta.extend_data).length > 0) {
                overlayConfig.extendData = meta.extend_data;
            }
            // Scenario overlays: pass name/direction as extendData
            if (meta.kline_name === 'scenarioPath') {
                overlayConfig.extendData = { name: meta.name || d.label || '', direction: meta.direction || 'neutral' };
            }

            chart.createOverlay(overlayConfig);

            // Add label as separate annotation if present
            if (d.label && meta.kline_name !== 'textAnnotation') {
                var labelPt = meta.points[meta.points.length - 1];
                chart.createOverlay({
                    name: 'simpleAnnotation', lock: true,
                    points: [labelPt],
                    styles: { text: { color: meta.color || d.color || '#ff0000', size: 11, family: 'monospace' } },
                    extendData: d.label
                });
            }
            return true;
        }

        drawings.forEach(function(d) {
            // Skip archived scenarios
            if (d.metadata && d.metadata.archived) return;
            // v2 group scenarios: collect IDs, render children separately
            if (d.drawing_type === 'scenario' && d.metadata && d.metadata.is_group) {
                v2ScenarioIds.push(d.id);
                return;
            }
            // Try rich recreation from metadata first
            if (d.metadata && d.metadata.kline_name) {
                try {
                    if (recreateOverlayFromMetadata(d)) return;
                } catch (e) {
                    console.warn('[ShareRender] Metadata recreation failed:', e);
                }
            }

            // Legacy type-based rendering (for old/manually-added drawings)
            var lineStyleMap = { 'solid': 'solid', 'dashed': 'dashed', 'dotted': 'dashed' };
            var style = lineStyleMap[d.line_style] || 'solid';

            if (d.drawing_type === 'horizontal_line') {
                chart.createOverlay({
                    name: 'straightLine',
                    lock: true,
                    points: [
                        { timestamp: firstTs, value: d.price_level },
                        { timestamp: lastTs, value: d.price_level }
                    ],
                    styles: {
                        line: { color: d.color || '#ff0000', style: style, size: d.line_width || 2 }
                    }
                });
                if (d.label) {
                    chart.createOverlay({
                        name: 'simpleAnnotation',
                        lock: true,
                        points: [{ timestamp: lastTs, value: d.price_level }],
                        styles: { text: { color: d.color || '#ff0000', size: 11, family: 'monospace' } },
                        extendData: d.label
                    });
                }
            } else if (d.drawing_type === 'trendline') {
                // Trendline: use metadata timestamps if available, else span full chart
                var t1 = (d.metadata && d.metadata.point1_timestamp) ? d.metadata.point1_timestamp : firstTs;
                var t2 = (d.metadata && d.metadata.point2_timestamp) ? d.metadata.point2_timestamp : lastTs;
                var v1 = d.price_level;
                var v2 = (d.price_level_upper != null) ? d.price_level_upper : d.price_level;
                chart.createOverlay({
                    name: 'straightLine',
                    lock: true,
                    points: [
                        { timestamp: t1, value: v1 },
                        { timestamp: t2, value: v2 }
                    ],
                    styles: {
                        line: { color: d.color || '#ff0000', style: style, size: d.line_width || 2 }
                    }
                });
                if (d.label) {
                    chart.createOverlay({
                        name: 'simpleAnnotation',
                        lock: true,
                        points: [{ timestamp: t2, value: v2 }],
                        styles: { text: { color: d.color || '#ff0000', size: 11, family: 'monospace' } },
                        extendData: d.label
                    });
                }
            } else if (d.drawing_type === 'zone' && d.price_level_upper) {
                [d.price_level, d.price_level_upper].forEach(function(lvl) {
                    chart.createOverlay({
                        name: 'straightLine',
                        lock: true,
                        points: [
                            { timestamp: firstTs, value: lvl },
                            { timestamp: lastTs, value: lvl }
                        ],
                        styles: {
                            line: { color: d.color || '#ff0000', style: 'dashed', size: 1 }
                        }
                    });
                });
                if (d.label) {
                    var midPrice = (d.price_level + d.price_level_upper) / 2;
                    chart.createOverlay({
                        name: 'simpleAnnotation',
                        lock: true,
                        points: [{ timestamp: lastTs, value: midPrice }],
                        styles: { text: { color: d.color || '#ff0000', size: 11, family: 'monospace' } },
                        extendData: d.label
                    });
                }
            } else if (d.drawing_type === 'liquidation_zone' && d.price_level && d.price_level_upper) {
                var lMeta = d.metadata || {};
                var lDir = lMeta.direction || 'unknown';
                var lIcon = lDir === 'long' ? '\u25BC' : (lDir === 'short' ? '\u25B2' : '\u25C6');
                var lLabel = lIcon + ' Liq ' + (d.price_level/1000).toFixed(1) + 'K-' + (d.price_level_upper/1000).toFixed(1) + 'K (' + lDir + 's)';
                [d.price_level, d.price_level_upper].forEach(function(lvl) {
                    chart.createOverlay({
                        name: 'straightLine', lock: true,
                        points: [{ timestamp: firstTs, value: lvl }, { timestamp: lastTs, value: lvl }],
                        styles: { line: { color: 'rgba(255,215,0,0.4)', style: 'dashed', size: 1, dashedValue: [4,3] } }
                    });
                });
                var lMid = (d.price_level + d.price_level_upper) / 2;
                chart.createOverlay({
                    name: 'simpleAnnotation', lock: true,
                    points: [{ timestamp: lastTs, value: lMid }],
                    styles: { text: { color: '#FFD700', size: 10, family: 'monospace' } },
                    extendData: lLabel
                });
            }
        });

        return { v2ScenarioIds: v2ScenarioIds };
    }

    if (typeof window !== 'undefined') {
        window.KlineShareRender = {
            registerOverlays: registerOverlays,
            renderFrozenDrawings: renderFrozenDrawings
        };
    }
})();
