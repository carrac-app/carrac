/**
 * Carrac — Mobile Drawing Tools v2
 * Custom touch drawing with blue crosshair, tap-to-anchor, drag-to-shape, tap-to-finalize.
 * Creates/updates KLineChart overlays programmatically — no reliance on KLineChart's internal drawing mode.
 */
(function() {
    'use strict';

    // ── Constants ──
    var LS_FAV_VISIBLE = 'mobileDrawFavVisible';
    var LS_TOOLBAR_POS = 'mobileDrawToolbarPos';
    var IDLE_MS = 3000;
    var TAP_THRESHOLD = 12;       // px — movement below this = tap
    var DOUBLE_TAP_MS = 350;      // ms — taps within this window = double-tap
    var CROSSHAIR_COLOR = '#58a6ff';

    var TOOL_CATEGORIES = [
        { id: 'favorites', label: 'Favorites', tools: null },
        { id: 'lines', label: 'Lines', tools: ['segment', 'horizontalStraightLine', 'horizontalRayLine', 'rayLine', 'straightLine', 'arrow'] },
        { id: 'shapes', label: 'Shapes', tools: ['rect', 'parallelChannel', 'pitchfork'] },
        { id: 'trading', label: 'Trading', tools: ['longPosition', 'shortPosition', 'spot', 'alertLine', 'alertLevel', 'priceLine'] },
        // Fixed v1 annotation glyph set added to the Annotate bucket
        { id: 'annotate', label: 'Annotate', tools: ['textAnnotation', 'measureTool', 'freehand', 'path', 'arrowUpMarker', 'arrowDownMarker', 'flagPin', 'circleMarker', 'callout', 'verticalStraightLine'] },
        { id: 'advanced', label: 'Advanced', tools: ['fibonacciRetrace', 'elliottImpulse', 'elliottCorrective', 'volumeProfileRange', 'anchoredVwap'] }
    ];

    // Single-point tools: tap once to place and finalize
    // longPosition and shortPosition are one-tap tools (ATR-based SL/TP auto-placed)
    var SINGLE_POINT_TOOLS = ['alertLine', 'alertLevel', 'priceLine', 'anchoredVwap', 'spot', 'horizontalStraightLine', 'horizontalRayLine', 'longPosition', 'shortPosition',
        'arrowUpMarker', 'arrowDownMarker', 'flagPin', 'circleMarker', 'verticalStraightLine'];
    // Multi-point tools: tap at each pivot, double-tap to finalize
    var MULTI_POINT_TOOLS = ['pitchfork', 'parallelChannel', 'elliottImpulse', 'elliottCorrective', 'path'];
    // Tools that skip our custom handler (have their own dialogs)
    var DIALOG_TOOLS = ['textAnnotation', 'freehand', 'volumeProfileRange', 'callout'];

    // ── State ──
    var _active = false;
    var _activeTool = null;
    var _fabEl = null;
    var _sheetEl = null;
    var _backdropEl = null;
    var _dragState = null;
    var _idleTimer = null;
    var _pollTimer = null;
    var _lastSelId = null;

    // ── Touch drawing state ──
    var _drawPhase = 'idle';  // 'idle' | 'crosshair' | 'shaping'
    var _touchOverlay = null; // transparent div over chart for touch capture
    var _crossCanvas = null;  // canvas for blue crosshair
    var _crossCtx = null;
    var _priceLabel = null;   // floating price label div
    var _previewId = null;    // KLineChart overlay ID for preview
    var _drawPoints = [];     // accumulated points [{timestamp, value}]
    var _touchStart = null;   // {x, y, time} for tap detection
    var _lastTapTime = 0;     // for double-tap detection
    var _currentFingerPos = null; // {x, y} last known finger position (local to container)

    // ── Helpers ──
    function _pixelToData(localX, localY) {
        if (typeof klineChart === 'undefined' || !klineChart) return null;
        try {
            var coord = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: 'candle_pane' });
            if (!coord || coord.dataIndex == null || coord.value == null) return null;
            var dataList = klineChart.getDataList();
            var ts = (coord.dataIndex >= 0 && coord.dataIndex < dataList.length)
                ? dataList[coord.dataIndex].timestamp : Date.now();
            var val = coord.value;
            if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled) val = Math.exp(val);
            return { timestamp: ts, value: val };
        } catch (e) { return null; }
    }

    function _isMultiPoint(tool) { return MULTI_POINT_TOOLS.indexOf(tool) >= 0; }
    function _isSinglePoint(tool) { return SINGLE_POINT_TOOLS.indexOf(tool) >= 0; }
    function _isDialogTool(tool) { return DIALOG_TOOLS.indexOf(tool) >= 0; }

    function _getMaxPoints(tool) {
        if (tool === 'pitchfork') return 3;
        if (tool === 'parallelChannel') return 3;
        if (tool === 'elliottImpulse') return 6;
        if (tool === 'elliottCorrective') return 4;
        if (tool === 'path') return 0; // unlimited
        return 2;
    }

    // ── Overlay hit testing (for selecting existing drawings on mobile) ──
    function _findOverlayAtPoint(localX, localY) {
        if (typeof klineChart === 'undefined' || !klineChart || typeof klineChartSymbol === 'undefined') return null;

        // Use the unified z-order-aware hitTest first for position drawings.
        // hitTestAtPoint handles position boxes (body + handles) correctly;
        // fall through to segment-distance logic for other shape types.
        if (window._mobileHandleApi && typeof window._mobileHandleApi.hitTest === 'function') {
            try {
                var _unifiedHit = window._mobileHandleApi.hitTest(localX, localY);
                if (_unifiedHit && _unifiedHit.overlayId) return _unifiedHit.overlayId;
            } catch(e) {}
        }

        var drawings;
        try {
            drawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        } catch (e) { return null; }
        var list = drawings[klineChartSymbol] || [];
        if (list.length === 0) return null;

        var HIT_PX = 25; // pixel tolerance for hit detection
        var bestId = null, bestDist = HIT_PX;

        for (var i = 0; i < list.length; i++) {
            var d = list[i];
            if (!d.points || d.points.length === 0) continue;

            // Convert data points to pixels (apply log scale if active)
            var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
            var px = [];
            for (var j = 0; j < d.points.length; j++) {
                try {
                    var pt = d.points[j];
                    var val = (_isLog && pt.value > 0) ? Math.log(pt.value) : pt.value;
                    var p = klineChart.convertToPixel({ timestamp: pt.timestamp, value: val }, { paneId: d.paneId || 'candle_pane' });
                    if (p && p.x != null && p.y != null) px.push(p);
                } catch (e) {}
            }
            if (px.length === 0) continue;

            var dist = Infinity;

            if (d.name === 'rect' && px.length >= 2) {
                // Rectangle: check if point is inside
                var minX = Math.min(px[0].x, px[1].x), maxX = Math.max(px[0].x, px[1].x);
                var minY = Math.min(px[0].y, px[1].y), maxY = Math.max(px[0].y, px[1].y);
                if (localX >= minX - HIT_PX && localX <= maxX + HIT_PX && localY >= minY - HIT_PX && localY <= maxY + HIT_PX) {
                    dist = 0;
                    if (localX >= minX && localX <= maxX && localY >= minY && localY <= maxY) dist = 0;
                    else dist = HIT_PX - 1; // near edge
                }
            } else if (px.length >= 2) {
                // Line-based: point-to-segment distance
                for (var k = 0; k < px.length - 1; k++) {
                    var segDist = _pointToSegmentDist(localX, localY, px[k].x, px[k].y, px[k + 1].x, px[k + 1].y);
                    if (segDist < dist) dist = segDist;
                }
            } else {
                // Single point
                dist = Math.sqrt(Math.pow(localX - px[0].x, 2) + Math.pow(localY - px[0].y, 2));
            }

            // For horizontal lines, also check full-width at that Y
            if ((d.name === 'horizontalStraightLine' || d.name === 'horizontalRayLine' || d.name === 'priceLine' || d.name === 'alertLine' || d.name === 'alertLevel') && px.length >= 1) {
                dist = Math.abs(localY - px[0].y);
            }

            if (dist < bestDist) {
                bestDist = dist;
                bestId = d.id;
            }
        }
        return bestId;
    }

    function _pointToSegmentDist(px, py, ax, ay, bx, by) {
        var dx = bx - ax, dy = by - ay;
        var lenSq = dx * dx + dy * dy;
        if (lenSq === 0) return Math.sqrt(Math.pow(px - ax, 2) + Math.pow(py - ay, 2));
        var t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lenSq));
        var projX = ax + t * dx, projY = ay + t * dy;
        return Math.sqrt(Math.pow(px - projX, 2) + Math.pow(py - projY, 2));
    }

    // ════════════════════════════════════════════
    // Drawing Sheet (Bottom) — supports minimized state
    // ════════════════════════════════════════════

    function _openDrawSheet(minimized) {
        if (_sheetEl) return;

        _backdropEl = document.createElement('div');
        _backdropEl.className = 'mds-backdrop';
        if (minimized) _backdropEl.style.display = 'none';
        _backdropEl.addEventListener('click', _closeDrawSheet);

        _sheetEl = document.createElement('div');
        _sheetEl.className = 'mds-sheet';
        if (minimized) _sheetEl.classList.add('mds-minimized');
        _sheetEl.innerHTML = _renderSheetHTML();

        document.body.appendChild(_backdropEl);
        document.body.appendChild(_sheetEl);

        _wireSheetEvents();
        _initSheetDrag();
    }

    function _closeDrawSheet() {
        if (_backdropEl) { _backdropEl.remove(); _backdropEl = null; }
        if (_sheetEl) { _sheetEl.remove(); _sheetEl = null; }
    }

    function _renderSheetHTML() {
        var favs = (typeof klineFavouriteTools !== 'undefined') ? klineFavouriteTools : [];
        var icons = (typeof klineToolIcons !== 'undefined') ? klineToolIcons : {};
        var names = (typeof klineToolNames !== 'undefined') ? klineToolNames : {};
        var activeCat = favs.length > 0 ? 'favorites' : 'lines';

        var html = '<div class="mds-drag-handle"><div class="drag-handle-bar"></div></div>';

        // Category pills (always visible, even minimized)
        html += '<div class="mds-categories">';
        TOOL_CATEGORIES.forEach(function(cat) {
            if (cat.id === 'favorites' && favs.length === 0) return;
            html += '<button class="mds-cat' + (cat.id === activeCat ? ' active' : '') + '" data-cat="' + cat.id + '">' + escapeHtml(cat.label) + '</button>';
        });
        html += '</div>';

        // Tool grids (hidden when minimized, shown when expanded)
        html += '<div class="mds-grid-container">';
        TOOL_CATEGORIES.forEach(function(cat) {
            var tools = cat.id === 'favorites' ? favs : cat.tools;
            if (!tools || tools.length === 0) return;
            html += '<div class="mds-grid" data-cat-grid="' + cat.id + '"' + (cat.id !== activeCat ? ' style="display:none"' : '') + '>';
            tools.forEach(function(tool) {
                var icon = icons[tool] || '';
                var name = names[tool] || tool;
                var isFav = favs.indexOf(tool) >= 0;
                html += '<button class="mds-tool" data-tool="' + tool + '">' +
                    '<span class="mds-tool-icon">' + icon + '</span>' +
                    '<span class="mds-tool-label">' + escapeHtml(name) + '</span>' +
                    '<span class="mds-tool-star' + (isFav ? ' active' : '') + '" data-star="' + tool + '">' + (isFav ? '\u2605' : '\u2606') + '</span>' +
                    '</button>';
            });
            html += '</div>';
        });
        html += '</div>';

        // Footer toggle
        var favVisible = localStorage.getItem(LS_FAV_VISIBLE) !== 'false';
        html += '<div class="mds-footer">' +
            '<span>Show favourites on chart</span>' +
            '<label class="mobile-toggle"><input type="checkbox" id="mds-fav-toggle"' + (favVisible ? ' checked' : '') + '><span class="mobile-toggle-slider"></span></label>' +
            '</div>';

        return html;
    }

    function _wireSheetEvents() {
        if (!_sheetEl) return;
        // All button actions (tools, categories, stars) handled by global coordinate handler
        // Only the favourites toggle needs a direct listener
        // Favourites toggle
        var toggle = _sheetEl.querySelector('#mds-fav-toggle');
        if (toggle) {
            toggle.addEventListener('change', function() {
                localStorage.setItem(LS_FAV_VISIBLE, this.checked ? 'true' : 'false');
                if (_fabEl) _fabEl.style.display = this.checked ? '' : 'none';
            });
        }
    }

    function _initSheetDrag() {
        if (!_sheetEl) return;
        var handle = _sheetEl.querySelector('.mds-drag-handle');
        if (!handle) return;
        var state = null;

        handle.addEventListener('touchstart', function(e) {
            state = { startY: e.touches[0].clientY, startH: _sheetEl.offsetHeight };
            _sheetEl.style.transition = 'none';
        }, { passive: true });

        handle.addEventListener('touchmove', function(e) {
            if (!state) return;
            var dy = state.startY - e.touches[0].clientY;
            var h = Math.max(60, Math.min(window.innerHeight * 0.85, state.startH + dy));
            _sheetEl.style.height = h + 'px';
            // If dragging up past threshold, remove minimized state
            if (h > 120) {
                _sheetEl.classList.remove('mds-minimized');
                if (_backdropEl) _backdropEl.style.display = '';
            }
        }, { passive: true });

        handle.addEventListener('touchend', function() {
            if (!state) return;
            state = null;
            _sheetEl.style.transition = '';
            var h = _sheetEl.offsetHeight;
            var vh = window.innerHeight;
            if (h < vh * 0.12) {
                // Snap to minimized
                _sheetEl.classList.add('mds-minimized');
                _sheetEl.style.height = '';
                if (_backdropEl) _backdropEl.style.display = 'none';
            } else if (h < vh * 0.15) {
                _closeDrawSheet();
            } else if (h < vh * 0.72) {
                _sheetEl.style.height = '55vh';
            } else {
                _sheetEl.style.height = '85vh';
            }
        });
    }

    // ════════════════════════════════════════════
    // Floating Toolbar
    // ════════════════════════════════════════════

    function _createFloatingToolbar() {
        if (_fabEl) return;
        _fabEl = document.createElement('div');
        _fabEl.className = 'mobile-draw-fab';
        _fabEl.id = 'mobile-draw-fab';

        var html = '<div class="mdf-grip" id="mdf-grip">' +
            '<svg width="16" height="10" viewBox="0 0 16 10" fill="currentColor" opacity="0.4">' +
            '<circle cx="4" cy="2" r="1.5"/><circle cx="12" cy="2" r="1.5"/>' +
            '<circle cx="4" cy="8" r="1.5"/><circle cx="12" cy="8" r="1.5"/></svg></div>';
        html += '<div class="mdf-tools" id="mdf-tools"></div>';
        html += '<button class="mdf-btn mdf-add-btn" id="mdf-add" title="More tools">' +
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>';
        html += '<div class="mdf-sep"></div>';

        var curColor = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
        html += '<button class="mdf-btn mdf-ctx-btn" id="mdf-color" title="Color"><span class="mdf-color-dot" style="background:' + curColor + '"></span></button>';
        html += '<button class="mdf-btn mdf-ctx-btn" id="mdf-lock" title="Lock" disabled>' +
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></button>';
        html += '<button class="mdf-btn mdf-ctx-btn mdf-delete-btn" id="mdf-delete" title="Delete" disabled>' +
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>';
        html += '<button class="mdf-btn mdf-ctx-btn" id="mdf-undo" title="Undo">' +
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg></button>';
        html += '<div class="mdf-sep"></div>';
        html += '<button class="mdf-btn mdf-done-btn" id="mdf-done" title="Done">' +
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg></button>';

        _fabEl.innerHTML = html;
        document.body.appendChild(_fabEl);
        _rebuildToolButtons();
        _restoreToolbarPosition();
        _initToolbarDrag();
        _wireToolbarEvents();
        _resetIdleTimer();
        var favVisible = localStorage.getItem(LS_FAV_VISIBLE) !== 'false';
        _fabEl.style.display = favVisible ? '' : 'none';
    }

    function _destroyFloatingToolbar() {
        if (_fabEl) { _fabEl.remove(); _fabEl = null; }
        if (_idleTimer) { clearTimeout(_idleTimer); _idleTimer = null; }
    }

    function _rebuildToolButtons() {
        if (!_fabEl) return;
        var container = _fabEl.querySelector('#mdf-tools');
        if (!container) return;
        var favs = (typeof klineFavouriteTools !== 'undefined') ? klineFavouriteTools : [];
        var icons = (typeof klineToolIcons !== 'undefined') ? klineToolIcons : {};
        var names = (typeof klineToolNames !== 'undefined') ? klineToolNames : {};
        var html = '';
        favs.forEach(function(tool) {
            var icon = icons[tool] || '?';
            var name = names[tool] || tool;
            html += '<button class="mdf-btn mdf-tool-btn' + (_activeTool === tool ? ' active' : '') + '" data-tool="' + tool + '" title="' + escapeHtml(name) + '">' + icon + '</button>';
        });
        container.innerHTML = html;
        // Event handling is done by the global coordinate-based handler at the bottom of this file
    }

    function _wireToolbarEvents() {
        if (!_fabEl) return;
        // Only idle timer reset — all button actions handled by global coordinate handler
        _fabEl.addEventListener('touchstart', function() { _resetIdleTimer(); }, { passive: true });
    }

    function _toggleFullSheet() {
        if (_sheetEl && !_sheetEl.classList.contains('mds-minimized')) {
            // Expanded → minimize
            _sheetEl.classList.add('mds-minimized');
            _sheetEl.style.height = '';
            if (_backdropEl) _backdropEl.style.display = 'none';
        } else if (_sheetEl && _sheetEl.classList.contains('mds-minimized')) {
            // Minimized → expand
            _sheetEl.classList.remove('mds-minimized');
            _sheetEl.style.height = '55vh';
            if (_backdropEl) _backdropEl.style.display = '';
        } else {
            // No sheet → create expanded
            _openDrawSheet(false);
        }
    }

    function _initToolbarDrag() {
        if (!_fabEl) return;
        var grip = _fabEl.querySelector('#mdf-grip');
        if (!grip) return;
        grip.addEventListener('touchstart', function(e) {
            e.preventDefault();
            var touch = e.touches[0];
            var rect = _fabEl.getBoundingClientRect();
            _dragState = { offX: touch.clientX - rect.left, offY: touch.clientY - rect.top };
            _fabEl.classList.add('mdf-dragging');
            _fabEl.classList.remove('mdf-idle');
            _fabEl.style.transition = 'none';
        }, { passive: false });
        document.addEventListener('touchmove', function(e) {
            if (!_dragState || !_fabEl) return;
            var touch = e.touches[0];
            var x = Math.max(0, Math.min(touch.clientX - _dragState.offX, window.innerWidth - _fabEl.offsetWidth));
            var y = Math.max(0, Math.min(touch.clientY - _dragState.offY, window.innerHeight - _fabEl.offsetHeight));
            _fabEl.style.left = x + 'px';
            _fabEl.style.top = y + 'px';
            _fabEl.style.right = 'auto';
        }, { passive: true });
        document.addEventListener('touchend', function() {
            if (!_dragState || !_fabEl) return;
            _dragState = null;
            _fabEl.classList.remove('mdf-dragging');
            _fabEl.style.transition = '';
            _snapToEdge();
            _persistToolbarPosition();
            _resetIdleTimer();
        });
    }

    function _snapToEdge() {
        if (!_fabEl) return;
        _fabEl.style.left = '4px';
        _fabEl.style.right = 'auto';
        var rect = _fabEl.getBoundingClientRect();
        var maxY = window.innerHeight - rect.height - 4;
        if (rect.top < 4) _fabEl.style.top = '4px';
        else if (rect.top > maxY) _fabEl.style.top = maxY + 'px';
    }

    function _restoreToolbarPosition() {
        if (!_fabEl) return;
        try {
            var pos = JSON.parse(localStorage.getItem(LS_TOOLBAR_POS));
            if (pos && pos.topPct != null) {
                var top = Math.max(4, Math.min(pos.topPct * window.innerHeight, window.innerHeight - 200));
                _fabEl.style.top = top + 'px';
            } else {
                _fabEl.style.top = (window.innerHeight * 0.3) + 'px';
            }
        } catch (e) {
            _fabEl.style.top = (window.innerHeight * 0.3) + 'px';
        }
        _fabEl.style.left = '4px';
        _fabEl.style.right = 'auto';
    }

    function _persistToolbarPosition() {
        if (!_fabEl) return;
        var rect = _fabEl.getBoundingClientRect();
        try { localStorage.setItem(LS_TOOLBAR_POS, JSON.stringify({ edge: 'left', topPct: rect.top / window.innerHeight })); } catch (e) {}
    }

    function _resetIdleTimer() {
        if (!_fabEl) return;
        _fabEl.classList.remove('mdf-idle');
        if (_idleTimer) clearTimeout(_idleTimer);
        _idleTimer = setTimeout(function() {
            if (_fabEl && !_dragState) _fabEl.classList.add('mdf-idle');
        }, IDLE_MS);
    }

    // ════════════════════════════════════════════
    // Draw Mode Lifecycle
    // ════════════════════════════════════════════

    function enterMobileDrawMode() {
        if (_active) {
            exitMobileDrawMode();
            return;
        }
        _active = true;
        if (typeof closeMobileSheet === 'function') closeMobileSheet();
        document.body.classList.add('mobile-draw-mode');
        _createFloatingToolbar();
        _openDrawSheet(true); // minimized
        _startContextPoll();
        _installDrawExitTap();
    }

    // Tap-to-exit: when in draw mode with NO tool armed, a tap on the chart area
    // means "I don't want to draw" → leave draw mode. Skipped while a tool is
    // active (those taps draw, handled by the touch overlay) and on drags/long-
    // presses (panning the chart).
    var _drawExitTapStart = null;
    function _drawExitTouchStart(e) {
        if (!e.touches || e.touches.length !== 1) { _drawExitTapStart = null; return; }
        _drawExitTapStart = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() };
    }
    function _drawExitTouchEnd(e) {
        var s = _drawExitTapStart; _drawExitTapStart = null;
        if (!s || !_active || _activeTool) return;
        var ch = (e.changedTouches && e.changedTouches[0]) || null;
        if (!ch) return;
        if (Math.abs(ch.clientX - s.x) + Math.abs(ch.clientY - s.y) > 12) return; // drag/pan
        if (Date.now() - s.t > 500) return; // long-press
        exitMobileDrawMode();
    }
    function _installDrawExitTap() {
        var c = document.getElementById('kline-chart-container');
        if (!c) return;
        c.addEventListener('touchstart', _drawExitTouchStart, { passive: true });
        c.addEventListener('touchend', _drawExitTouchEnd, { passive: true });
    }
    function _removeDrawExitTap() {
        var c = document.getElementById('kline-chart-container');
        if (c) {
            c.removeEventListener('touchstart', _drawExitTouchStart);
            c.removeEventListener('touchend', _drawExitTouchEnd);
        }
        _drawExitTapStart = null;
    }

    function exitMobileDrawMode() {
        if (!_active) return;
        _active = false;
        _deactivateTool();
        _closeDrawSheet();
        _destroyFloatingToolbar();
        _stopContextPoll();
        _removeDrawExitTap();
        document.body.classList.remove('mobile-draw-mode');
        // Guarantee the bottom action bar comes back. The CSS hide is
        // `body.mobile-draw-mode .mobile-action-bar { transform: translateY(100%) }`,
        // which reverts when the class is dropped — but re-assert .visible and clear
        // any lingering transform so leaving draw mode always restores the bar.
        var _bar = document.getElementById('mobile-action-bar');
        if (_bar) { _bar.classList.add('visible'); _bar.style.transform = ''; }
    }

    function _activateTool(toolName) {
        _deactivateTool();
        _activeTool = toolName;

        // Update toolbar highlights
        if (_fabEl) {
            _fabEl.querySelectorAll('.mdf-tool-btn').forEach(function(b) {
                b.classList.toggle('active', b.getAttribute('data-tool') === toolName);
            });
        }

        // Dialog tools use existing desktop handler (they show a modal first)
        if (_isDialogTool(toolName)) {
            if (typeof handleKlineToolClick === 'function') {
                var synth = document.createElement('button');
                synth.setAttribute('data-tool', toolName);
                setTimeout(function() { handleKlineToolClick(toolName, synth, [], null); }, 100);
            }
            return;
        }

        // Install touch drawing overlay
        try {
            _installTouchOverlay();
        } catch (err) {
            console.error('[MobileDraw] overlay error:', err);
            return;
        }
        _drawPhase = 'crosshair';
        _drawPoints = [];
        _previewId = null;
        if (typeof showToast === 'function') showToast(_touchOverlay ? 'Touch chart to draw' : 'Error: no overlay', 2000);
    }

    function _deactivateTool() {
        _activeTool = null;
        _drawPhase = 'idle';
        _drawPoints = [];
        _crossPos = null;
        _removeTouchOverlay();
        _clearPreview();
        if (_fabEl) {
            _fabEl.querySelectorAll('.mdf-tool-btn.active').forEach(function(b) { b.classList.remove('active'); });
        }
    }

    // ════════════════════════════════════════════
    // Touch Drawing System
    // ════════════════════════════════════════════

    function _installTouchOverlay() {
        _removeTouchOverlay();
        var container = document.getElementById('kline-chart-container');
        if (!container) {
            alert('[Draw] Chart container not found');
            return;
        }
        var rect = container.getBoundingClientRect();

        // Use position:fixed on document.body — guarantees overlay is above everything
        // (avoids stacking context issues inside KLineChart's DOM)
        _touchOverlay = document.createElement('div');
        _touchOverlay.id = 'mds-touch-overlay';
        _touchOverlay.style.cssText = 'position:fixed;top:' + rect.top + 'px;left:' + rect.left + 'px;width:' + rect.width + 'px;height:' + rect.height + 'px;z-index:25000;touch-action:none;background:transparent;';
        document.body.appendChild(_touchOverlay);

        // Canvas for crosshair — inside the overlay
        _crossCanvas = document.createElement('canvas');
        _crossCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;';
        var dpr = window.devicePixelRatio || 1;
        _crossCanvas.width = rect.width * dpr;
        _crossCanvas.height = rect.height * dpr;
        _crossCtx = _crossCanvas.getContext('2d');
        if (_crossCtx) _crossCtx.scale(dpr, dpr);
        _touchOverlay.appendChild(_crossCanvas);

        // Price label — inside the overlay
        _priceLabel = document.createElement('div');
        _priceLabel.style.cssText = 'position:absolute;z-index:1;pointer-events:none;display:none;background:rgba(88,166,255,0.15);border:1px solid #58a6ff;color:#58a6ff;font-size:10px;padding:1px 6px;font-family:monospace;border-radius:2px;white-space:nowrap;right:0;';
        _touchOverlay.appendChild(_priceLabel);

        // Touch handlers
        _touchOverlay.addEventListener('touchstart', _onTouchStart, { passive: false });
        _touchOverlay.addEventListener('touchmove', _onTouchMove, { passive: false });
        _touchOverlay.addEventListener('touchend', _onTouchEnd, { passive: false });
        _touchOverlay.addEventListener('touchcancel', _onTouchEnd, { passive: false });
    }

    function _removeTouchOverlay() {
        if (_touchOverlay) { _touchOverlay.remove(); _touchOverlay = null; }
        if (_crossCanvas) { _crossCanvas.remove(); _crossCanvas = null; _crossCtx = null; }
        if (_priceLabel) { _priceLabel.remove(); _priceLabel = null; }
        _currentFingerPos = null;
    }

    // Crosshair position (independent of finger — delta-based like gray crosshair)
    var _crossPos = null; // {x, y} — crosshair position in container-local coords
    var _lastFingerX = 0, _lastFingerY = 0;

    function _initCrosshairPos() {
        // Start crosshair at chart center
        var container = document.getElementById('kline-chart-container');
        if (!container) return;
        var rect = container.getBoundingClientRect();
        _crossPos = { x: rect.width / 2, y: rect.height / 2 };
        _drawCrosshair(_crossPos.x, _crossPos.y);
    }

    function _onTouchStart(e) {
        if (e.touches.length > 1) return; // let pinch through
        e.preventDefault();
        e.stopPropagation();
        var touch = e.touches[0];
        var container = document.getElementById('kline-chart-container');
        if (!container) return;
        var rect = container.getBoundingClientRect();
        var fingerX = touch.clientX - rect.left;
        var fingerY = touch.clientY - rect.top;
        _lastFingerX = fingerX;
        _lastFingerY = fingerY;
        _touchStart = { x: fingerX, y: fingerY, time: Date.now() };
        _currentFingerPos = { x: fingerX, y: fingerY };
        // Initialize crosshair if first touch
        if (!_crossPos) _initCrosshairPos();
    }

    function _onTouchMove(e) {
        if (e.touches.length > 1) return;
        e.preventDefault();
        e.stopPropagation();
        var touch = e.touches[0];
        var container = document.getElementById('kline-chart-container');
        if (!container) return;
        var rect = container.getBoundingClientRect();
        var fingerX = touch.clientX - rect.left;
        var fingerY = touch.clientY - rect.top;

        // Delta-based crosshair movement (like gray crosshair)
        var dx = fingerX - _lastFingerX;
        var dy = fingerY - _lastFingerY;
        _lastFingerX = fingerX;
        _lastFingerY = fingerY;

        if (_crossPos) {
            _crossPos.x = Math.max(0, Math.min(_crossPos.x + dx, rect.width));
            _crossPos.y = Math.max(0, Math.min(_crossPos.y + dy, rect.height));
            _drawCrosshair(_crossPos.x, _crossPos.y);
        }

        _currentFingerPos = { x: fingerX, y: fingerY };

        // If shaping and dragging, update preview using crosshair position
        if (_drawPhase === 'shaping' && _previewId && _drawPoints.length > 0 && _crossPos) {
            var data = _pixelToData(_crossPos.x, _crossPos.y);
            if (data) {
                var pts = _drawPoints.slice();
                pts.push(data);
                _updatePreview(pts);
            }
        }
    }

    function _onTouchEnd(e) {
        e.preventDefault();
        e.stopPropagation();
        if (!_touchStart) return;

        var dx = _currentFingerPos ? _currentFingerPos.x - _touchStart.x : 0;
        var dy = _currentFingerPos ? _currentFingerPos.y - _touchStart.y : 0;
        var dist = Math.sqrt(dx * dx + dy * dy);
        var isTap = dist < TAP_THRESHOLD;
        var now = Date.now();
        var isDoubleTap = isTap && (now - _lastTapTime < DOUBLE_TAP_MS);

        if (isTap) _lastTapTime = now;

        // Use CROSSHAIR position for data, not finger position
        var pos = _crossPos || _currentFingerPos || _touchStart;
        var data = _pixelToData(pos.x, pos.y);

        if (_drawPhase === 'crosshair') {
            if (isTap && data) {
                // Place first point
                _drawPoints = [data];

                if (_isSinglePoint(_activeTool)) {
                    // Single-point tool: create and finalize immediately
                    _createAndFinalize([data]);
                    return;
                }

                // Create preview overlay with correct number of initial points
                var maxPts = _getMaxPoints(_activeTool);
                var pts = [data, data];
                // 3-point tools (channel, pitchfork) need 3 points from the start
                if (maxPts >= 3) {
                    pts = [data, data, data];
                }
                _previewId = _createPreview(_activeTool, pts);
                _drawPhase = 'shaping';
                if (typeof showToast === 'function') {
                    var hint = _isMultiPoint(_activeTool) ? 'Tap to add points, double-tap to finish' : 'Drag to shape, tap to finish';
                    showToast(hint, 2000);
                }
            }
        } else if (_drawPhase === 'shaping') {
            if (isDoubleTap && _isMultiPoint(_activeTool) && _drawPoints.length >= 2) {
                // Double-tap finalizes multi-point
                _finalizeDrawing();
            } else if (isTap && !_isMultiPoint(_activeTool)) {
                // Tap finalizes two-point tool
                if (data) {
                    _drawPoints.push(data);
                    _updatePreview(_drawPoints);
                }
                _finalizeDrawing();
            } else if (isTap && _isMultiPoint(_activeTool)) {
                // Tap adds next point for multi-point tool
                if (data) {
                    _drawPoints.push(data);
                    // Extend preview with new point
                    var nextPts = _drawPoints.slice();
                    nextPts.push(data); // duplicate last for preview
                    _updatePreview(nextPts);

                    var max = _getMaxPoints(_activeTool);
                    if (max > 0 && _drawPoints.length >= max) {
                        _finalizeDrawing();
                    }
                }
            } else if (!isTap) {
                // Drag ended — update last point position
                if (data && !_isMultiPoint(_activeTool)) {
                    var pts2 = _drawPoints.slice();
                    pts2.push(data);
                    _updatePreview(pts2);
                } else if (data && _isMultiPoint(_activeTool)) {
                    // Update last point position after drag
                    var ptsCopy = _drawPoints.slice();
                    ptsCopy[ptsCopy.length - 1] = data;
                    _drawPoints = ptsCopy;
                    _updatePreview(ptsCopy.concat([data]));
                }
            }
        }

        _touchStart = null;
    }

    // ── Crosshair ──

    function _drawCrosshair(x, y) {
        if (!_crossCtx || !_crossCanvas) return;
        var w = _crossCanvas.width / (window.devicePixelRatio || 1);
        var h = _crossCanvas.height / (window.devicePixelRatio || 1);
        _crossCtx.clearRect(0, 0, w, h);

        _crossCtx.strokeStyle = CROSSHAIR_COLOR;
        _crossCtx.lineWidth = 1;
        _crossCtx.setLineDash([4, 3]);

        // Vertical line
        _crossCtx.beginPath();
        _crossCtx.moveTo(x, 0);
        _crossCtx.lineTo(x, h);
        _crossCtx.stroke();

        // Horizontal line
        _crossCtx.beginPath();
        _crossCtx.moveTo(0, y);
        _crossCtx.lineTo(w, y);
        _crossCtx.stroke();

        _crossCtx.setLineDash([]);

        // Price label
        if (_priceLabel) {
            var data = _pixelToData(x, y);
            if (data) {
                var price = data.value;
                var formatted = (typeof formatPrice === 'function') ? formatPrice(price) : price.toFixed(2);
                _priceLabel.textContent = formatted;
                _priceLabel.style.display = '';
                _priceLabel.style.top = Math.max(0, Math.min(y - 12, h - 24)) + 'px';
                _priceLabel.style.right = '0';
            }
        }
    }

    // ── Preview Overlays ──

    function _createPreview(toolName, points) {
        if (typeof klineChart === 'undefined' || !klineChart) return null;
        var color = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
        var width = (typeof klineDrawingWidth !== 'undefined') ? klineDrawingWidth : 1;
        var overlayName = toolName;
        if (toolName === 'parallelChannel') overlayName = 'boundedChannel';
        var extData = {};
        if (toolName === 'rect') {
            extData.fillColor = (typeof hexToRgba === 'function') ? hexToRgba(color, 0.15) : color;
        }
        if (toolName === 'parallelChannel') {
            extData._color = color;
            extData._width = width;
        }
        try {
            var callbacks = (typeof getOverlayCallbacks === 'function') ? getOverlayCallbacks() : {};
            var id = klineChart.createOverlay(Object.assign({
                name: overlayName,
                points: points,
                extendData: extData,
                styles: { line: { color: color, size: width }, point: { color: color, borderColor: color, borderSize: 1, radius: 4, activeColor: color, activeBorderColor: color, activeBorderSize: 2, activeRadius: 6 } }
            }, callbacks), 'candle_pane');
            return id;
        } catch (e) {
            console.error('[MobileDraw] createPreview error:', e);
            return null;
        }
    }

    function _updatePreview(points) {
        if (!_previewId || typeof klineChart === 'undefined' || !klineChart) return;
        try {
            klineChart.overrideOverlay({ id: _previewId, points: points });
        } catch (e) {
            console.error('[MobileDraw] updatePreview error:', e);
        }
    }

    function _clearPreview() {
        if (_previewId && typeof klineChart !== 'undefined' && klineChart) {
            try { klineChart.removeOverlay(_previewId); } catch (e) {}
        }
        _previewId = null;
    }

    function _createAndFinalize(points) {
        // longPosition / shortPosition: one-tap placement with ATR defaults
        if (_activeTool === 'longPosition' || _activeTool === 'shortPosition') {
            var _posToolName = _activeTool;
            var _posData = points && points[0];
            if (!_posData || typeof klineChart === 'undefined' || !klineChart) {
                _drawPoints = [];
                _drawPhase = 'crosshair';
                _deactivateTool();
                return;
            }
            var _posDir = (_posToolName === 'longPosition') ? 'long' : 'short';
            var _posColor = (_posDir === 'long') ? '#26a69a' : '#ef5350';
            var _dataList = klineChart.getDataList ? klineChart.getDataList() : [];
            var _atr = (typeof _computeATR === 'function') ? _computeATR(_dataList, 14) :
                       (window._mobileHandleApi ? window._mobileHandleApi.computeATR(_dataList, 14) : null);
            // Crosshair value is chart-space (log-transformed on log charts); ATR is raw —
            // convert entry to raw before mixing (mirror of the desktop _posPlace guard).
            var _isLogM = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
            var _entry = (_isLogM && _posData.value != null) ? Math.exp(_posData.value) : _posData.value;
            var _sl, _tp;
            if (_atr && _atr > 0) {
                if (_posDir === 'long') {
                    _sl = Math.max(_entry - 1.5 * _atr, 0.0000001);
                    _tp = _entry + 3 * _atr;
                } else {
                    _sl = _entry + 1.5 * _atr;
                    _tp = Math.max(_entry - 3 * _atr, 0.0000001);
                }
            } else {
                if (_posDir === 'long') {
                    _sl = Math.max(_entry * 0.98, 0.0000001);
                    _tp = _entry * 1.04;
                } else {
                    _sl = _entry * 1.02;
                    _tp = Math.max(_entry * 0.96, 0.0000001);
                }
            }
            var _pts = [
                { timestamp: _posData.timestamp, value: _entry },
                { timestamp: _posData.timestamp, value: _sl },
                { timestamp: _posData.timestamp, value: _tp }
            ];
            // Create through the proven path (same as desktop _posPlace / load / paste):
            // createOverlayFromDrawing handles {timestamp,value} + log-space transform.
            var _newDrawing = {
                name: _posToolName,
                points: _pts,
                color: _posColor,
                width: 1,
                extendData: {}
            };
            var _newId = null;
            try {
                if (typeof createOverlayFromDrawing === 'function') _newId = createOverlayFromDrawing(_newDrawing);
            } catch(e) {}
            if (_newId && typeof saveKlineDrawing === 'function') {
                _newDrawing.id = _newId;
                saveKlineDrawing(_newDrawing);
                if (typeof klineSelectedOverlayId !== 'undefined') klineSelectedOverlayId = _newId;
            }
            _drawPoints = [];
            _drawPhase = 'crosshair';
            _deactivateTool();
            return;
        }
        // For other single-point tools, use the existing handler which has special logic
        if (typeof handleKlineToolClick === 'function') {
            var synth = document.createElement('button');
            synth.setAttribute('data-tool', _activeTool);
            handleKlineToolClick(_activeTool, synth, [], null);
        }
        // Stay in crosshair mode for next drawing
        _drawPoints = [];
        _drawPhase = 'crosshair';
    }

    function _finalizeDrawing() {
        if (!_previewId || _drawPoints.length < 2) {
            _clearPreview();
            _drawPoints = [];
            _drawPhase = 'crosshair';
            return;
        }

        // Save the drawing
        var color = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
        var width = (typeof klineDrawingWidth !== 'undefined') ? klineDrawingWidth : 1;
        var toolName = _activeTool;
        if (toolName === 'parallelChannel') toolName = 'boundedChannel';

        var extData = { _color: color, _width: width };
        if (_activeTool === 'rect') {
            extData.fillColor = (typeof hexToRgba === 'function') ? hexToRgba(color, 0.15) : color;
            if (_drawPoints.length >= 2) {
                extData.noBorder = (_drawPoints[0].value < _drawPoints[1].value);
            }
        }
        if (_activeTool === 'parallelChannel') {
            extData._color = color;
            extData._width = width;
        }

        if (typeof saveKlineDrawing === 'function') {
            saveKlineDrawing({
                id: _previewId,
                name: toolName,
                points: (typeof resolvePointTimestamps === 'function') ? resolvePointTimestamps(_drawPoints) : _drawPoints,
                color: color,
                width: width,
                extendData: extData
            });
        }

        // Select the drawing
        if (typeof klineSelectedOverlayId !== 'undefined') {
            klineSelectedOverlayId = _previewId;
        }

        // Deactivate tool — return to normal crosshair
        _previewId = null;
        _drawPoints = [];
        _deactivateTool();
    }

    // ════════════════════════════════════════════
    // Context Actions
    // ════════════════════════════════════════════

    function _onColorTap() {
        if (typeof klineSelectedOverlayId !== 'undefined' && klineSelectedOverlayId) {
            if (typeof cycleSelectedDrawingColor === 'function') cycleSelectedDrawingColor();
        } else {
            if (typeof klineDrawingColors !== 'undefined' && typeof klineDrawingColor !== 'undefined') {
                var idx = klineDrawingColors.indexOf(klineDrawingColor);
                klineDrawingColor = klineDrawingColors[(idx + 1) % klineDrawingColors.length];
            }
        }
        _updateColorDot();
    }

    function _onLockTap() {
        if (typeof klineSelectedOverlayId === 'undefined' || !klineSelectedOverlayId) return;
        if (typeof handleContextMenuAction === 'function') handleContextMenuAction('toggle-lock', klineSelectedOverlayId);
        _updateLockIcon();
    }

    function _onDeleteTap() {
        if (typeof klineSelectedOverlayId === 'undefined' || !klineSelectedOverlayId) return;
        if (typeof handleContextMenuAction === 'function') handleContextMenuAction('delete', klineSelectedOverlayId);
    }

    function _onUndoTap() {
        if (typeof undoKlineDrawings === 'function') undoKlineDrawings();
    }

    function _updateColorDot() {
        if (!_fabEl) return;
        var dot = _fabEl.querySelector('.mdf-color-dot');
        if (!dot) return;
        var color = (typeof klineDrawingColor !== 'undefined') ? klineDrawingColor : '#3fb950';
        if (typeof klineSelectedOverlayId !== 'undefined' && klineSelectedOverlayId && typeof getKlineDrawingById === 'function') {
            var d = getKlineDrawingById(klineSelectedOverlayId);
            if (d && d.color) color = d.color;
        }
        dot.style.background = color;
    }

    function _updateLockIcon() {
        if (!_fabEl) return;
        var lockBtn = _fabEl.querySelector('#mdf-lock');
        if (!lockBtn) return;
        var isLocked = false;
        if (typeof klineSelectedOverlayId !== 'undefined' && klineSelectedOverlayId && typeof klineChart !== 'undefined' && klineChart) {
            try {
                var ov = klineChart.getOverlayById(klineSelectedOverlayId);
                if (Array.isArray(ov)) ov = ov[0];
                if (ov && ov.lock) isLocked = true;
            } catch (e) {}
        }
        lockBtn.classList.toggle('active', isLocked);
    }

    // ════════════════════════════════════════════
    // Context Polling
    // ════════════════════════════════════════════

    function _startContextPoll() {
        _stopContextPoll();
        _pollTimer = setInterval(function() {
            if (!_fabEl) return;
            var selId = (typeof klineSelectedOverlayId !== 'undefined') ? klineSelectedOverlayId : null;
            if (selId !== _lastSelId) {
                _lastSelId = selId;
                var has = !!selId;
                var lockBtn = _fabEl.querySelector('#mdf-lock');
                var delBtn = _fabEl.querySelector('#mdf-delete');
                if (lockBtn) lockBtn.disabled = !has;
                if (delBtn) delBtn.disabled = !has;
                _updateColorDot();
                _updateLockIcon();
            }
        }, 200);
    }

    function _stopContextPoll() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
        _lastSelId = null;
    }

    // ════════════════════════════════════════════
    // Resize
    // ════════════════════════════════════════════

    var _resizeTimer = null;
    window.addEventListener('resize', function() {
        if (_fabEl) {
            if (_resizeTimer) clearTimeout(_resizeTimer);
            _resizeTimer = setTimeout(_snapToEdge, 300);
        }
        // Resize crosshair canvas
        if (_crossCanvas) {
            var container = document.getElementById('kline-chart-container');
            if (container) {
                var rect = container.getBoundingClientRect();
                _crossCanvas.width = rect.width * (window.devicePixelRatio || 1);
                _crossCanvas.height = rect.height * (window.devicePixelRatio || 1);
                _crossCanvas.style.width = rect.width + 'px';
                _crossCanvas.style.height = rect.height + 'px';
                _crossCtx = _crossCanvas.getContext('2d');
                _crossCtx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
            }
        }
    });

    // ════════════════════════════════════════════
    // Expose
    // ════════════════════════════════════════════

    // ════════════════════════════════════════════
    // Mobile Drawing Context Bar
    // Shows color/lock/delete when a drawing is selected (works in and out of draw mode)
    // ════════════════════════════════════════════
    var _ctxBar = null;

    function _showMobileDrawingContextBar(overlayId) {
        _hideMobileDrawingContextBar();
        if (!overlayId) return;
        // Only on touch/mobile devices
        if (typeof isTouchDevice === 'function' && !isTouchDevice()) return;

        _ctxBar = document.createElement('div');
        _ctxBar.id = 'mobile-drawing-ctx-bar';
        _ctxBar.style.cssText = 'position:fixed;bottom:' + (_active ? '70px' : '65px') + ';left:50%;transform:translateX(-50%);z-index:26005;display:flex;gap:6px;padding:8px 14px;background:rgba(22,27,34,0.95);border:1px solid #30363d;border-radius:10px;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);box-shadow:0 2px 12px rgba(0,0,0,0.4);align-items:center;';

        // Get current drawing info
        var drawingData = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(overlayId) : null;
        var curColor = (drawingData && drawingData.color) ? drawingData.color : '#3fb950';

        // Add Bring to Front / Send to Back buttons (parity with desktop context menu)
        _ctxBar.innerHTML =
            '<button class="mctx-btn" data-action="color" title="Color" style="width:32px;height:32px;border:none;border-radius:6px;background:#21262d;cursor:pointer;display:flex;align-items:center;justify-content:center;">' +
                '<span style="width:16px;height:16px;border-radius:50%;background:' + curColor + ';border:2px solid #484f58;display:block;"></span></button>' +
            '<button class="mctx-btn" data-action="width" title="Thickness" style="width:32px;height:32px;border:none;border-radius:6px;background:#21262d;color:#8b949e;cursor:pointer;display:flex;align-items:center;justify-content:center;">' +
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round"><line x1="4" y1="6" x2="20" y2="6" stroke-width="1"/><line x1="4" y1="12" x2="20" y2="12" stroke-width="2"/><line x1="4" y1="18" x2="20" y2="18" stroke-width="3.5"/></svg></button>' +
            '<button class="mctx-btn" data-action="lock" title="Lock" style="width:32px;height:32px;border:none;border-radius:6px;background:#21262d;color:#8b949e;cursor:pointer;display:flex;align-items:center;justify-content:center;">' +
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></button>' +
            '<button class="mctx-btn" data-action="bring-to-front" title="Bring to Front" style="width:32px;height:32px;border:none;border-radius:6px;background:#21262d;color:#8b949e;cursor:pointer;display:flex;align-items:center;justify-content:center;">' +
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="7" width="18" height="14" rx="1"/><rect x="6" y="3" width="15" height="12" rx="1" fill="currentColor" opacity="0.3"/><path d="M12 7v-4M12 3l2 2M12 3l-2 2"/></svg></button>' +
            '<button class="mctx-btn" data-action="send-to-back" title="Send to Back" style="width:32px;height:32px;border:none;border-radius:6px;background:#21262d;color:#8b949e;cursor:pointer;display:flex;align-items:center;justify-content:center;">' +
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="14" rx="1"/><rect x="6" y="7" width="15" height="14" rx="1" fill="currentColor" opacity="0.3"/><path d="M12 17v4M12 21l2-2M12 21l-2-2"/></svg></button>' +
            '<button class="mctx-btn" data-action="delete" title="Delete" style="width:32px;height:32px;border:none;border-radius:6px;background:#21262d;color:#f85149;cursor:pointer;display:flex;align-items:center;justify-content:center;">' +
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>' +
            '<button class="mctx-btn" data-action="close" title="Dismiss" style="width:32px;height:32px;border:none;border-radius:6px;background:none;color:#484f58;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:18px;">&times;</button>';

        document.body.appendChild(_ctxBar);

        // Wire actions via coordinate hit testing (same pattern as toolbar)
        // Handled by the global touchend handler below
    }

    function _hideMobileDrawingContextBar() {
        if (_ctxBar) { _ctxBar.remove(); _ctxBar = null; }
    }

    window.enterMobileDrawMode = enterMobileDrawMode;
    window.exitMobileDrawMode = exitMobileDrawMode;
    window._findOverlayAtPoint = _findOverlayAtPoint;
    window._showMobileDrawingContextBar = _showMobileDrawingContextBar;
    window._hideMobileDrawingContextBar = _hideMobileDrawingContextBar;

    // ════════════════════════════════════════════
    // Global fallback: coordinate-based hit detection
    // Bypasses all DOM traversal issues (closest, SVG namespaces, etc.)
    // ════════════════════════════════════════════
    function _hitTest(x, y, selector) {
        var els = document.querySelectorAll(selector);
        for (var i = 0; i < els.length; i++) {
            var r = els[i].getBoundingClientRect();
            if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return els[i];
        }
        return null;
    }

    var _gStartX = 0, _gStartY = 0;
    document.addEventListener('touchstart', function(e) {
        if (!e.touches[0]) return;
        _gStartX = e.touches[0].clientX;
        _gStartY = e.touches[0].clientY;
    }, { passive: true });

    var _lastActionTime = 0;
    document.addEventListener('touchend', function(e) {
        if (!e.changedTouches[0]) return;
        var now = Date.now();
        if (now - _lastActionTime < 400) return;
        var t = e.changedTouches[0];
        var tx = t.clientX, ty = t.clientY;
        if (Math.abs(tx - _gStartX) > 15 || Math.abs(ty - _gStartY) > 15) return;

        // Context bar buttons (works in AND out of draw mode)
        var ctxHit = _hitTest(tx, ty, '.mctx-btn');
        if (ctxHit) {
            e.preventDefault(); _lastActionTime = now;
            var action = ctxHit.getAttribute('data-action');
            if (action === 'delete' && klineSelectedOverlayId) {
                if (typeof handleContextMenuAction === 'function') handleContextMenuAction('delete', klineSelectedOverlayId);
                _hideMobileDrawingContextBar();
            } else if (action === 'color' && klineSelectedOverlayId) {
                if (typeof cycleSelectedDrawingColor === 'function') cycleSelectedDrawingColor();
                // Update color dot in context bar
                var d = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(klineSelectedOverlayId) : null;
                if (d && d.color && _ctxBar) {
                    var dot = _ctxBar.querySelector('[data-action="color"] span');
                    if (dot) dot.style.background = d.color;
                }
            } else if (action === 'width' && klineSelectedOverlayId) {
                // Cycle thickness 1→2→3→4→1 on the selected drawing
                var dw = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(klineSelectedOverlayId) : null;
                if (dw && typeof recreateOverlayWithStyles === 'function') {
                    dw.width = ((dw.width || 1) % 4) + 1;
                    recreateOverlayWithStyles(dw);
                }
            } else if (action === 'lock' && klineSelectedOverlayId) {
                if (typeof handleContextMenuAction === 'function') handleContextMenuAction('toggle-lock', klineSelectedOverlayId);
            } else if (action === 'bring-to-front' && klineSelectedOverlayId) {
                // Parity with desktop context menu
                if (typeof handleContextMenuAction === 'function') handleContextMenuAction('bring-to-front', klineSelectedOverlayId);
            } else if (action === 'send-to-back' && klineSelectedOverlayId) {
                if (typeof handleContextMenuAction === 'function') handleContextMenuAction('send-to-back', klineSelectedOverlayId);
            } else if (action === 'close') {
                _hideMobileDrawingContextBar();
                klineSelectedOverlayId = null;
                try { klineChart._chartStore.getOverlayStore().setClickInstanceInfo({ paneId: 'candle_pane', instance: null, figureType: 0, figureIndex: -1, figureKey: '' }); } catch(ex) {}
            }
            return;
        }

        if (!_active) return;

        // Toolbar tool buttons
        var hit = _hitTest(tx, ty, '.mdf-tool-btn');
        if (hit) {
            e.preventDefault(); _lastActionTime = now;
            var tool = hit.getAttribute('data-tool');
            if (_activeTool === tool) _deactivateTool();
            else _activateTool(tool);
            return;
        }

        // Star toggles \u2014 MUST be checked before .mds-tool: the \u2605 span sits INSIDE
        // the .mds-tool row, so a .mds-tool hit-test otherwise swallows the star tap
        // (selecting the tool) and the favourite toggle never fires.
        hit = _hitTest(tx, ty, '.mds-tool-star');
        if (hit) {
            e.preventDefault(); _lastActionTime = now;
            var starTool = hit.getAttribute('data-star');
            if (starTool && typeof toggleFavouriteTool === 'function') {
                toggleFavouriteTool(starTool);
                var isFav = (typeof klineFavouriteTools !== 'undefined') && klineFavouriteTools.indexOf(starTool) >= 0;
                if (_sheetEl) _sheetEl.querySelectorAll('.mds-tool-star[data-star="' + starTool + '"]').forEach(function(s) {
                    s.textContent = isFav ? '\u2605' : '\u2606';
                    s.classList.toggle('active', isFav);
                });
                if (_fabEl) _rebuildToolButtons();
            }
            return;
        }

        // Sheet tool buttons
        hit = _hitTest(tx, ty, '.mds-tool');
        if (hit) {
            e.preventDefault(); _lastActionTime = now;
            var tool2 = hit.getAttribute('data-tool');
            if (_sheetEl) { _sheetEl.classList.add('mds-minimized'); _sheetEl.style.height = ''; if (_backdropEl) _backdropEl.style.display = 'none'; }
            _activateTool(tool2);
            return;
        }

        // Category pills
        hit = _hitTest(tx, ty, '.mds-cat');
        if (hit && _sheetEl) {
            e.preventDefault(); _lastActionTime = now;
            var cat = hit.getAttribute('data-cat');
            _sheetEl.querySelectorAll('.mds-cat').forEach(function(b) { b.classList.toggle('active', b === hit); });
            _sheetEl.querySelectorAll('.mds-grid').forEach(function(g) { g.style.display = g.getAttribute('data-cat-grid') === cat ? '' : 'none'; });
            if (_sheetEl.classList.contains('mds-minimized')) { _sheetEl.classList.remove('mds-minimized'); if (_backdropEl) _backdropEl.style.display = ''; }
            return;
        }

        // Fixed toolbar buttons (by ID)
        var ids = [
            ['#mdf-add', _toggleFullSheet],
            ['#mdf-color', _onColorTap],
            ['#mdf-undo', _onUndoTap],
            ['#mdf-done', exitMobileDrawMode],
            ['#mdf-lock', _onLockTap],
            ['#mdf-delete', _onDeleteTap]
        ];
        for (var i = 0; i < ids.length; i++) {
            hit = _hitTest(tx, ty, ids[i][0]);
            if (hit && !hit.disabled) { e.preventDefault(); _lastActionTime = now; ids[i][1](); return; }
        }
    }, { passive: false });
})();
