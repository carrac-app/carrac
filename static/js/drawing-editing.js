// Editing drawings that are already on the chart: show/hide all, colour,
// width and opacity changes on the selection, locked-drawing hit-testing,
// overlay recreation, and the text-annotation dialog. Needs app-core.js
// loaded first; the drawing store itself lives in kline-drawings.js.

function toggleDrawingsVisibility() {
    if (!klineChart) return;
    klineDrawingsHidden = !klineDrawingsHidden;
    if (klineDrawingsHidden) {
        // Remove all drawing overlays (including alerts)
        klineChart.removeOverlay();
        _alertOverlayIds = [];
        // Also collapse drawing toolbar
        hideDrawingToolbar();
    } else {
        // Restore drawings from storage
        if (typeof loadKlineDrawings === 'function') loadKlineDrawings();
        if (typeof loadDrawingsFromServer === 'function') loadDrawingsFromServer();
        // Re-render alert overlays for non-drawn alerts
        if (typeof renderAlertOverlays === 'function') renderAlertOverlays();
        // Re-apply custom indicator overlays (indicator pack, SMC) that were wiped by removeOverlay()
        var cacheKey = klineChartSymbol + '_' + klineChartTimeframe;
        var cached = klineDataCache[cacheKey];
        if (cached) {
            applyKlineIndicators(cached.data, convertToKlineData(cached.data), true);
        }
        // Expand drawing toolbar
        showDrawingToolbar();
    }
    // Update draw toggle button state
    var btn = document.getElementById('draw-toggle-btn');
    if (btn) btn.classList.toggle('drawings-hidden', klineDrawingsHidden);
}

function cycleSelectedDrawingColor() {
    if (!klineChart || !klineSelectedOverlayId) return;
    var drawing = getOrCreateDrawingRecord(klineSelectedOverlayId);
    if (!drawing) return;
    var idx = klineDrawingColors.indexOf(drawing.color);
    drawing.color = klineDrawingColors[(idx + 1) % klineDrawingColors.length];
    recreateOverlayWithStyles(drawing);
}

function getOrCreateDrawingRecord(overlayId) {
    var drawing = getKlineDrawingById(overlayId);
    if (drawing) return drawing;
    // Drawing not in localStorage yet — create a stub from the live overlay
    try {
        var overlays = klineChart.getOverlayById(overlayId);
        var overlay = Array.isArray(overlays) ? overlays[0] : overlays;
        if (!overlay) {
            // Fallback: create minimal record so overrideOverlay still works
            drawing = { id: overlayId, name: 'unknown', points: [], color: klineDrawingColor, width: klineDrawingWidth, extendData: {} };
            saveKlineDrawing(drawing);
            return drawing;
        }
        var lineStyles = (overlay.styles && overlay.styles.line) || {};
        drawing = {
            id: overlayId,
            name: overlay.name || 'unknown',
            points: resolvePointTimestamps(overlay.points || []),
            color: lineStyles.color || klineDrawingColor,
            width: lineStyles.size || klineDrawingWidth,
            extendData: overlay.extendData || {}
        };
    } catch (e) {
        // getOverlayById not available or failed — use defaults
        drawing = { id: overlayId, name: 'unknown', points: [], color: klineDrawingColor, width: klineDrawingWidth, extendData: {} };
    }
    saveKlineDrawing(drawing);
    return drawing;
}

// Hit-test all locked drawings at a given chart-local pixel position.
// Returns the overlay ID of the first hit, or null.
function getLockedOverlayAtPoint(mx, my) {
    if (!klineChart || !klineChartSymbol) return null;
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawings = all[klineChartSymbol] || [];
        var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
        var _lp = function(v) { return (_isLog && v > 0) ? Math.log(v) : v; };
        for (var i = 0; i < drawings.length; i++) {
            var d = drawings[i];
            if (!d.id || !d.points || d.points.length === 0) continue;
            // Only test drawings that are locked (always-locked types + user-locked)
            var isPosition = d.name === 'position' || d.name === 'longPosition' || d.name === 'shortPosition';
            var isSpot = d.name === 'spot';
            var isUserLocked = d.extendData && d.extendData._locked;
            if (!isPosition && !isSpot && !isUserLocked) continue;

            // Convert all points to pixel coordinates
            var pxPoints = [];
            var valid = true;
            for (var j = 0; j < d.points.length; j++) {
                var p = d.points[j];
                var px = klineChart.convertToPixel({ timestamp: p.timestamp, value: _lp(p.value) }, { paneId: d.paneId || 'candle_pane' });
                if (!px || isNaN(px.x) || isNaN(px.y)) { valid = false; break; }
                pxPoints.push(px);
            }
            if (!valid || pxPoints.length === 0) continue;

            // Position: bounding box of entry/SL/TP
            if (isPosition && pxPoints.length >= 3) {
                var minY = Math.min(pxPoints[0].y, pxPoints[1].y, pxPoints[2].y);
                var maxY = Math.max(pxPoints[0].y, pxPoints[1].y, pxPoints[2].y);
                var minX = Math.min(pxPoints[0].x, pxPoints[1].x, pxPoints[2].x);
                if (my >= minY - 5 && my <= maxY + 5 && mx >= minX - 20) return d.id;
                continue;
            }

            // Single-point types (spot, text annotation): proximity check
            if (pxPoints.length === 1) {
                var dx = mx - pxPoints[0].x, dy = my - pxPoints[0].y;
                if (Math.sqrt(dx * dx + dy * dy) <= 20) return d.id;
                continue;
            }

            // Two-point types (trendline, horizontal line, rect, channel segments):
            // Check proximity to the line segment between points, or bounding box for rects
            if (d.name === 'rect' && pxPoints.length >= 2) {
                var rMinX = Math.min(pxPoints[0].x, pxPoints[1].x);
                var rMaxX = Math.max(pxPoints[0].x, pxPoints[1].x);
                var rMinY = Math.min(pxPoints[0].y, pxPoints[1].y);
                var rMaxY = Math.max(pxPoints[0].y, pxPoints[1].y);
                if (mx >= rMinX - 5 && mx <= rMaxX + 5 && my >= rMinY - 5 && my <= rMaxY + 5) return d.id;
                continue;
            }

            // Lines/channels/fibs: check distance to each segment between consecutive points
            var HIT_DIST = 10;
            for (var k = 0; k < pxPoints.length; k++) {
                // Check proximity to each point
                var pdx = mx - pxPoints[k].x, pdy = my - pxPoints[k].y;
                if (Math.sqrt(pdx * pdx + pdy * pdy) <= HIT_DIST) { return d.id; }
                // Check proximity to segment between consecutive points
                if (k < pxPoints.length - 1) {
                    var ax = pxPoints[k].x, ay = pxPoints[k].y;
                    var bx = pxPoints[k + 1].x, by = pxPoints[k + 1].y;
                    var abx = bx - ax, aby = by - ay;
                    var len2 = abx * abx + aby * aby;
                    if (len2 > 0) {
                        var t = Math.max(0, Math.min(1, ((mx - ax) * abx + (my - ay) * aby) / len2));
                        var projX = ax + t * abx, projY = ay + t * aby;
                        var dist = Math.sqrt((mx - projX) * (mx - projX) + (my - projY) * (my - projY));
                        if (dist <= HIT_DIST) return d.id;
                    }
                }
            }
        }
    } catch (e) {}
    return null;
}

var _isRecreatingOverlay = false;  // Suppress onDeselected during overlay recreation

function recreateOverlayWithStyles(drawing) {
    if (!klineChart || !drawing || !drawing.id) return;
    var color = drawing.color || '#3fb950';
    var opacity = drawing.opacity !== undefined ? drawing.opacity : (drawing.name === 'rect' ? 0.15 : 1.0);
    var lineColor = opacity < 1.0 ? hexToRgba(color, opacity) : color;
    var w = drawing.width || 1;
    drawing.extendData = drawing.extendData || {};
    drawing.extendData._color = lineColor;
    drawing.extendData._width = w;
    if (drawing.name === 'rect') {
        // For rects: opacity controls FILL only, border always stays full color
        var fillOpacity = Math.max(0.02, opacity);
        drawing.extendData.fillColor = hexToRgba(color, fillOpacity);
        drawing.extendData.fillOpacity = fillOpacity;
        // Override lineColor to keep border fully opaque
        lineColor = color;
    }
    var oldId = drawing.id;
    _isRecreatingOverlay = true;
    klineChart.removeOverlay(oldId);
    var lineStyles = { color: lineColor, size: w };
    // Position overlays are always locked in KLineChart — our custom handler manages all interaction
    var _isPos = drawing.name === 'position' || drawing.name === 'longPosition' || drawing.name === 'shortPosition';
    var config = {
        name: drawing.name,
        lock: _isPos ? true : !!(drawing.extendData && drawing.extendData._locked),
        points: snapPointsToData(drawing.points),
        styles: {
            line: lineStyles,
            point: _invisiblePointStyle
        },
        extendData: drawing.extendData
    };
    var callbacks = getOverlayCallbacks();
    // Preserve paneId for sub-pane drawings
    var recreatePaneId = drawing.paneId || 'candle_pane';
    if (typeof getActivePaneIds === 'function') {
        var activePanes = getActivePaneIds();
        if (recreatePaneId !== 'candle_pane' && activePanes.indexOf(recreatePaneId) === -1) {
            recreatePaneId = 'candle_pane';
        }
    }
    var newId = klineChart.createOverlay(Object.assign(config, callbacks), recreatePaneId);
    removeKlineDrawingFromStorage(oldId);
    drawing.id = newId;
    klineSelectedOverlayId = newId;
    saveKlineDrawing(drawing);
    // Clean up orphaned server record for the old ID
    if (oldId !== newId && typeof DrawingTools !== 'undefined' && DrawingTools.deleteDrawingFromServer) {
        DrawingTools.deleteDrawingFromServer(oldId, klineChartSymbol);
    }
    // Keep flag active for longer to catch all async deselect events
    var savedId = newId;
    setTimeout(function() {
        _isRecreatingOverlay = false;
        // Restore selection if it was cleared by async onDeselected
        if (!klineSelectedOverlayId) klineSelectedOverlayId = savedId;
    }, 100);
}

function changeSelectedDrawingWidth(delta) {
    if (!klineChart || !klineSelectedOverlayId) return;
    var drawing = getOrCreateDrawingRecord(klineSelectedOverlayId);
    if (!drawing) return;
    drawing.width = Math.max(1, Math.min(4, (drawing.width || 1) + delta));
    recreateOverlayWithStyles(drawing);
}

function changeSelectedDrawingOpacity(delta) {
    if (!klineChart || !klineSelectedOverlayId) return;
    var drawing = getOrCreateDrawingRecord(klineSelectedOverlayId);
    if (!drawing) return;
    var isRect = drawing.name === 'rect';
    var defaultOp = isRect ? 0.15 : 1.0;
    var opacity = drawing.opacity !== undefined ? drawing.opacity : defaultOp;
    // Rects: finer steps (0.05), range 0.05–0.9. Others: 0.1 steps, 0.1–1.0
    var step = isRect ? 0.05 : 0.1;
    var minOp = isRect ? 0.05 : 0.1;
    var maxOp = isRect ? 0.9 : 1.0;
    var raw = opacity + (delta > 0 ? step : -step);
    drawing.opacity = Math.round(Math.max(minOp, Math.min(maxOp, raw)) * 100) / 100;
    recreateOverlayWithStyles(drawing);
}

function hexToRgba(hex, alpha) {
    var r = 0, g = 0, b = 0;
    if (hex.length === 4) { r = parseInt(hex[1]+hex[1], 16); g = parseInt(hex[2]+hex[2], 16); b = parseInt(hex[3]+hex[3], 16); }
    else if (hex.length === 7) { r = parseInt(hex.substring(1,3), 16); g = parseInt(hex.substring(3,5), 16); b = parseInt(hex.substring(5,7), 16); }
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

function showTextAnnotationDialog(callback, editData) {
    var existing = document.getElementById('text-annotation-dialog');
    if (existing) existing.remove();

    var ed = editData || {};
    var dialog = document.createElement('div');
    dialog.id = 'text-annotation-dialog';
    dialog.className = 'text-annotation-dialog visible';
    dialog.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:340px;background:#1e222d;border:1px solid #3fb950;border-radius:8px;z-index:26003;display:flex;flex-direction:column;padding:16px;box-shadow:0 0 20px rgba(63,185,80,0.15),0 8px 32px rgba(0,0,0,0.6);font-family:JetBrains Mono,monospace';
    dialog.innerHTML =
        '<div style="color:#d1d4dc;font-size:13px;font-weight:600;margin-bottom:10px">' + (ed.text ? 'Edit Text' : 'Add Text') + '</div>' +
        '<input type="text" id="text-dialog-input" placeholder="Enter text..." autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other" role="presentation" value="' + escapeHtml(ed.text || '') + '" style="background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:8px 10px;color:#d1d4dc;font-size:14px;outline:none;margin-bottom:10px;font-family:inherit">' +
        '<div style="display:flex;gap:6px;align-items:center;margin-bottom:10px">' +
            '<select id="text-dialog-size" style="background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:4px 6px;color:#d1d4dc;font-size:12px;outline:none;font-family:inherit">' +
                [10,12,14,16,18,22,28].map(function(s) { return '<option value="'+s+'"'+(s===(ed.fontSize||14)?' selected':'')+'>'+s+'px</option>'; }).join('') +
            '</select>' +
            '<div style="display:flex;gap:2px;margin-left:4px" id="text-format-btns">' +
                '<button type="button" data-fmt="bold" title="Bold" style="background:'+(ed.bold?'#3fb950':'#2a2e39')+';border:1px solid #363a45;border-radius:3px;color:#d1d4dc;cursor:pointer;width:26px;height:26px;font-weight:bold;font-size:13px;font-family:inherit">B</button>' +
                '<button type="button" data-fmt="italic" title="Italic" style="background:'+(ed.italic?'#3fb950':'#2a2e39')+';border:1px solid #363a45;border-radius:3px;color:#d1d4dc;cursor:pointer;width:26px;height:26px;font-style:italic;font-size:13px;font-family:inherit">I</button>' +
                '<button type="button" data-fmt="underline" title="Underline" style="background:'+(ed.underline?'#3fb950':'#2a2e39')+';border:1px solid #363a45;border-radius:3px;color:#d1d4dc;cursor:pointer;width:26px;height:26px;text-decoration:underline;font-size:13px;font-family:inherit">U</button>' +
                '<button type="button" data-fmt="strikethrough" title="Strikethrough" style="background:'+(ed.strikethrough?'#3fb950':'#2a2e39')+';border:1px solid #363a45;border-radius:3px;color:#d1d4dc;cursor:pointer;width:26px;height:26px;text-decoration:line-through;font-size:13px;font-family:inherit">S</button>' +
            '</div>' +
            '<div style="display:flex;gap:3px;margin-left:auto" id="text-color-dots">' +
                klineDrawingColors.map(function(c) {
                    return '<div data-color="'+c+'" style="width:16px;height:16px;border-radius:50%;background:'+c+';cursor:pointer;border:2px solid '+(c===(ed.color||klineDrawingColor)?'#fff':'transparent')+'"></div>';
                }).join('') +
            '</div>' +
        '</div>' +
        (ed.showPosition ?
            '<div style="display:flex;gap:4px;align-items:center;margin-bottom:10px">' +
                '<span style="color:#787b86;font-size:11px;margin-right:4px">Position:</span>' +
                [['top-left','↖ TL'],['top-right','↗ TR'],['center','◉'],['bottom-left','↙ BL'],['bottom-right','↘ BR']].map(function(p) {
                    var active = p[0] === (ed.textPosition || 'center');
                    return '<button type="button" data-textpos="'+p[0]+'" title="'+p[0]+'" style="background:'+(active?'#3fb950':'#2a2e39')+';border:1px solid #363a45;border-radius:3px;color:'+(active?'#131722':'#d1d4dc')+';cursor:pointer;padding:3px 6px;font-size:11px;font-family:inherit">'+p[1]+'</button>';
                }).join('') +
            '</div>' : '') +
        '<div style="display:flex;gap:8px;justify-content:flex-end">' +
            '<button class="text-dialog-cancel" style="background:#2a2e39;border:1px solid #363a45;border-radius:4px;padding:6px 14px;color:#787b86;cursor:pointer;font-size:12px;font-family:inherit">Cancel</button>' +
            '<button class="text-dialog-ok" style="background:#3fb950;border:none;border-radius:4px;padding:6px 14px;color:#131722;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit">OK</button>' +
        '</div>';
    document.body.appendChild(dialog);

    var input = document.getElementById('text-dialog-input');
    input.focus();
    if (ed.text) input.select();

    // Format state
    var fmt = { bold: !!ed.bold, italic: !!ed.italic, underline: !!ed.underline, strikethrough: !!ed.strikethrough };
    var selectedColor = ed.color || klineDrawingColor;
    var selectedPosition = ed.textPosition || 'center';

    // Position buttons (rect text only)
    var posBtns = dialog.querySelectorAll('[data-textpos]');
    posBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
            selectedPosition = btn.getAttribute('data-textpos');
            posBtns.forEach(function(b) {
                var active = b.getAttribute('data-textpos') === selectedPosition;
                b.style.background = active ? '#3fb950' : '#2a2e39';
                b.style.color = active ? '#131722' : '#d1d4dc';
            });
        });
    });

    // Toggle format buttons
    dialog.querySelector('#text-format-btns').addEventListener('click', function(e) {
        var btn = e.target.closest('[data-fmt]');
        if (!btn) return;
        var f = btn.getAttribute('data-fmt');
        fmt[f] = !fmt[f];
        btn.style.background = fmt[f] ? '#3fb950' : '#2a2e39';
    });

    // Color dots
    dialog.querySelector('#text-color-dots').addEventListener('click', function(e) {
        var dot = e.target.closest('[data-color]');
        if (!dot) return;
        selectedColor = dot.getAttribute('data-color');
        dialog.querySelectorAll('#text-color-dots [data-color]').forEach(function(d) {
            d.style.borderColor = d.getAttribute('data-color') === selectedColor ? '#fff' : 'transparent';
        });
    });

    function submit() {
        var text = input.value.trim();
        var size = parseInt(document.getElementById('text-dialog-size').value, 10) || 14;
        dialog.remove();
        if (text) callback(text, size, selectedColor, fmt, selectedPosition);
    }

    dialog.querySelector('.text-dialog-ok').addEventListener('click', submit);
    dialog.querySelector('.text-dialog-cancel').addEventListener('click', function() { dialog.remove(); });
    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); dialog.remove(); }
        e.stopPropagation();
    });
}
