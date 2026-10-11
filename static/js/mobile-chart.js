// Phone and tablet chart UI: touch panning and pane-separator drag, the
// OHLCV readout, landscape pane sizing, and the bottom-sheet system and its
// tabs (chart, position, alerts, report, AI, plays, trade, more). Needs
// app-core.js loaded first; axis zoom and the touch crosshair are in
// chart-gestures.js.

// ============= Pane separator touch drag (mobile) =============
// Custom implementation: detect touch near pane boundary, drag to resize.
var _sepTouch = { active: false, lastY: 0, topPaneId: null, bottomPaneId: null, topH: 0, bottomH: 0 };
var SEP_GRAB_ZONE = 18; // px above/below pane boundary to detect grab

function _findPaneBoundary(touchY, container) {
    // Find if touchY is near a boundary between two panes
    if (!klineChart) return null;
    try {
        var drawPanes = klineChart.getAllDrawPanes();
        if (!drawPanes || drawPanes.length < 2) return null;
        var containerRect = container.getBoundingClientRect();
        for (var i = 0; i < drawPanes.length - 1; i++) {
            var topPane = drawPanes[i];
            var bottomPane = drawPanes[i + 1];
            var topBound = topPane.getBounding();
            // Boundary is at topBound.top + topBound.height relative to container
            var boundaryY = topBound.top + topBound.height;
            var localY = touchY - containerRect.top;
            if (Math.abs(localY - boundaryY) <= SEP_GRAB_ZONE) {
                return {
                    topPane: topPane,
                    bottomPane: bottomPane,
                    topId: topPane.getId(),
                    bottomId: bottomPane.getId(),
                    topH: topBound.height,
                    bottomH: bottomPane.getBounding().height
                };
            }
        }
    } catch(ex) {}
    return null;
}

// Apply accumulated pan delta — called synchronously (Chrome) or via rAF (Firefox)
function _applyPanDelta() {
    var dx = _chartTouch._pendingDx || 0;
    var dy = _chartTouch._pendingDy || 0;
    _chartTouch._pendingDx = 0;
    _chartTouch._pendingDy = 0;
    if (Math.abs(dx) > 0) {
        _chartTouch.accumOffset -= dx;
        try { klineChart.setOffsetRightDistance(_chartTouch.accumOffset); } catch(ex) {}
    }
    if (Math.abs(dy) > 1) {
        try {
            var axis = klineChart._candlePane.getAxisComponent();
            axis.setAutoCalcTickFlag(false);
            var r = axis.getRange();
            var shift = r.realRange * (dy / (_chartTouch.containerH || 500));
            axis.setRange({
                from: r.from + shift, to: r.to + shift, range: r.range,
                realFrom: r.realFrom + shift, realTo: r.realTo + shift, realRange: r.realRange,
                displayFrom: r.displayFrom + shift, displayTo: r.displayTo + shift, displayRange: r.displayRange
            });
        } catch(ex) {}
    }
}

function setupChartTouchPan() {
    var container = document.getElementById('kline-chart-container');
    if (!container || container._chartTouchSetup) return;
    container._chartTouchSetup = true;

    // Mouse pan detection — suppress live WS updates during mouse drag
    // (KlineChart handles mouse panning internally, we just track the state)
    container.addEventListener('mousedown', function(e) {
        if (e.button === 0) window._chartMousePanning = true;
    });
    document.addEventListener('mouseup', function() {
        if (window._chartMousePanning) {
            setTimeout(function() { window._chartMousePanning = false; }, 100);
        }
    });

    // Capture-phase touch events — fire BEFORE KLineChart's canvas listeners
    container.addEventListener('touchstart', function(e) {
        if (_chartTouch.active || _yTouch.active || _xTouch.active || _sepTouch.active) return;
        if (e.touches.length > 1) return;  // multi-touch = pinch

        // Desktop multi-chart: switch pane + check separator boundaries
        if (typeof _mcInitialized !== 'undefined' && _mcInitialized) {
            if (typeof mcActivatePaneForContainer === 'function') mcActivatePaneForContainer(container);
            var t0 = e.touches[0];
            var sepInfo = _findPaneBoundary(t0.clientY, container);
            if (sepInfo) {
                _sepTouch.active = true;
                _sepTouch.lastY = t0.clientY;
                _sepTouch.topPaneId = sepInfo.topId;
                _sepTouch.bottomPaneId = sepInfo.bottomId;
                _sepTouch.topH = sepInfo.topH;
                _sepTouch.bottomH = sepInfo.bottomH;
                e.stopPropagation();
                e.preventDefault();
                return;
            }
        }
        var t = e.touches[0];
        // Exclude Y-axis zone (right 60px) and X-axis zone (bottom 40px)
        var rect = container.getBoundingClientRect();
        var xFromRight = rect.right - t.clientX;
        var yFromBottom = rect.bottom - t.clientY;
        if (xFromRight <= Y_AXIS_WIDTH && xFromRight >= 0) return;
        if (yFromBottom <= X_AXIS_HEIGHT && yFromBottom >= 0) return;
        // Check for drawing handle hits (mobile handle editing)
        if (window._mobileHandleApi && klineSelectedOverlayId) {
            var _lx = t.clientX - rect.left, _ly = t.clientY - rect.top;
            var _rh = window._mobileHandleApi.getRectHit(_lx, _ly);
            if (_rh && _rh.handle !== 'BODY' && _rh.handle !== 'CENTER') {
                if (window._mobileHandleApi.startRectDrag(_rh)) {
                    e.stopPropagation(); e.preventDefault(); return;
                }
            }
            var _ch = window._mobileHandleApi.getChannelHit(_lx, _ly);
            if (_ch) {
                if (window._mobileHandleApi.startChannelDrag(_ch)) {
                    e.stopPropagation(); e.preventDefault(); return;
                }
            }
            var _fh = window._mobileHandleApi.getFrvpHit(_lx, _ly);
            if (_fh) {
                if (window._mobileHandleApi.startFrvpDrag(_fh)) {
                    e.stopPropagation(); e.preventDefault(); return;
                }
            }
            var _ph = window._mobileHandleApi.getPositionHit(_lx, _ly);
            // Allow body-level drag on mobile (was blocked by _ph.level !== 'body' guard)
            if (_ph) {
                if (window._mobileHandleApi.startPositionDrag(_ph, _lx, _ly)) {
                    e.stopPropagation(); e.preventDefault(); return;
                }
            }
        }
        // Enter pending state — don't commit to pan yet (might be a tap for crosshair)
        _chartTouch.active = true;
        _chartTouch.pending = true;
        _chartTouch.panning = false;
        _chartTouch.startX = t.clientX;
        _chartTouch.startY = t.clientY;
        _chartTouch.lastX = t.clientX;
        _chartTouch.lastY = t.clientY;
        _chartTouch.containerH = rect.height; // cache to avoid getBoundingClientRect in touchmove
        // Block KLineChart's touchstart to prevent its internal drag init
        e.stopPropagation();
        e.preventDefault();
    }, { capture: true, passive: false });

    container.addEventListener('touchmove', function(e) {
        // Handle active drawing handle drags
        if (window._mobileHandleApi && window._mobileHandleApi.hasActiveDrag()) {
            if (e.touches.length > 1) {
                // Pinch during drag — cancel drag
                window._mobileHandleApi.finalizeRect();
                window._mobileHandleApi.finalizeChannel();
                window._mobileHandleApi.finalizeFrvp();
                window._mobileHandleApi.finalizePosition();
                return;
            }
            var ht = e.touches[0];
            window._mobileHandleApi.handleRectResize(ht.clientX, ht.clientY, container);
            window._mobileHandleApi.handleChannelDrag(ht.clientX, ht.clientY, container);
            window._mobileHandleApi.handleFrvpDrag(ht.clientX, ht.clientY, container);
            window._mobileHandleApi.handlePositionDrag(ht.clientX, ht.clientY, container);
            e.stopPropagation(); e.preventDefault();
            return;
        }
        // Handle separator drag (desktop multi-chart only)
        if (_sepTouch.active) {
            e.stopPropagation();
            e.preventDefault();
            var t = e.touches[0];
            var dy = t.clientY - _sepTouch.lastY;
            _sepTouch.lastY = t.clientY;
            _sepTouch.topH += dy;
            _sepTouch.bottomH -= dy;
            if (_sepTouch.topH < 30) { _sepTouch.bottomH -= (30 - _sepTouch.topH); _sepTouch.topH = 30; }
            if (_sepTouch.bottomH < 30) { _sepTouch.topH -= (30 - _sepTouch.bottomH); _sepTouch.bottomH = 30; }
            try {
                klineChart.setPaneOptions({ id: _sepTouch.topPaneId, height: Math.round(_sepTouch.topH), minHeight: 20 });
                klineChart.setPaneOptions({ id: _sepTouch.bottomPaneId, height: Math.round(_sepTouch.bottomH), minHeight: 20 });
            } catch(ex) {}
            return;
        }
        if (!_chartTouch.active) return;
        // Cancel if pinch started
        if (e.touches.length > 1) {
            _chartTouch.active = false;
            _chartTouch.pending = false;
            _chartTouch.panning = false;
            window._touchPanActive = false;
            return;  // let KLineChart handle pinch
        }
        e.stopPropagation();
        e.preventDefault();
        var t = e.touches[0];

        // Check if we should commit to drag (crossed movement threshold)
        if (_chartTouch.pending) {
            var totalDx = t.clientX - _chartTouch.startX;
            var totalDy = t.clientY - _chartTouch.startY;
            if (Math.abs(totalDx) < PAN_THRESHOLD && Math.abs(totalDy) < PAN_THRESHOLD) return;
            _chartTouch.pending = false;
            _chartTouch.panning = true;
            // In crosshair mode, drags move the crosshair — don't suppress WS updates
            if (!_crosshairMode) {
                window._touchPanActive = true;
                // Snapshot current offset for accumulation
                try {
                    var tss = klineChart._chartStore.getTimeScaleStore();
                    _chartTouch.accumOffset = tss._lastBarRightSideDiffBarCount * tss._barSpace;
                    tss._scrollLimitRole = 0;
                    tss._minVisibleBarCount.right = 0;
                } catch(ex) {
                    _chartTouch.accumOffset = klineChart.getOffsetRightDistance ? klineChart.getOffsetRightDistance() : 80;
                }
            }
        }

        if (!_chartTouch.panning) return;

        // Crosshair mode: drag moves the crosshair by delta (no jump)
        if (_crosshairMode && _crosshairPos) {
            var dx = t.clientX - _chartTouch.lastX;
            var dy = t.clientY - _chartTouch.lastY;
            _chartTouch.lastX = t.clientX;
            _chartTouch.lastY = t.clientY;
            _crosshairPos.x += dx;
            _crosshairPos.y += dy;
            _mobileCrosshairUpdate(_crosshairPos.x, _crosshairPos.y, container);
            return;
        }

        // Pan mode
        var dx = t.clientX - _chartTouch.lastX;
        var dy = t.clientY - _chartTouch.lastY;
        _chartTouch.lastX = t.clientX;
        _chartTouch.lastY = t.clientY;
        _chartTouch._pendingDx = (_chartTouch._pendingDx || 0) + dx;
        _chartTouch._pendingDy = (_chartTouch._pendingDy || 0) + dy;

        if (_isFirefox) {
            // Firefox: batch redraws via rAF to avoid overwhelming its canvas pipeline
            if (!_chartTouch._rafPending) {
                _chartTouch._rafPending = true;
                requestAnimationFrame(function() {
                    _chartTouch._rafPending = false;
                    _applyPanDelta();
                });
            }
        } else {
            // Chrome/Safari: synchronous for lowest latency
            _applyPanDelta();
        }
    }, { capture: true, passive: false });

    function endChartTouch(e) {
        // Finalize any active handle drags
        if (window._mobileHandleApi && window._mobileHandleApi.hasActiveDrag()) {
            window._mobileHandleApi.finalizeRect();
            window._mobileHandleApi.finalizeChannel();
            window._mobileHandleApi.finalizeFrvp();
            window._mobileHandleApi.finalizePosition();
            e.stopPropagation();
            return;
        }
        // End separator drag
        if (_sepTouch.active) {
            _sepTouch.active = false;
            e.stopPropagation();
            return;
        }
        if (!_chartTouch.active) return;
        var wasPending = _chartTouch.pending;  // true = tap (no significant movement)
        var wasPanning = _chartTouch.panning;
        _chartTouch.active = false;
        _chartTouch.pending = false;
        _chartTouch.panning = false;
        // Flush any pending rAF delta before ending
        if (_chartTouch._pendingDx || _chartTouch._pendingDy) _applyPanDelta();
        _chartTouch._rafPending = false;
        e.stopPropagation();

        if (wasPending) {
            var now = Date.now();
            if (_crosshairMode) {
                // Single tap exits crosshair mode → enables pan on next drag
                _exitCrosshairMode();
                _lastTapTime = 0;
                return;
            }
            _lastTapTime = now;
            // First tap: place crosshair and enter crosshair mode
            var rect = container.getBoundingClientRect();
            var x = _chartTouch.startX - rect.left;
            var y = _chartTouch.startY - rect.top;
            _crosshairMode = true;
            _mobileCrosshairUpdate(x, y, container);
            // Manual overlay hit testing + KLineChart internal API for selection/deselection
            // (our capture-phase handler blocks KLineChart from receiving real touch events)
            if (typeof _findOverlayAtPoint === 'function') {
                var hitId = _findOverlayAtPoint(x, y);
                try {
                    var _ovStore = klineChart._chartStore.getOverlayStore();
                    if (hitId) {
                        var _ovInst = klineChart.getOverlayById(hitId);
                        if (Array.isArray(_ovInst)) _ovInst = _ovInst[0];
                        if (_ovInst) {
                            _ovStore.setClickInstanceInfo({ paneId: 'candle_pane', instance: _ovInst, figureType: 1, figureIndex: 0, figureKey: '' });
                            klineSelectedOverlayId = hitId;
                        }
                    } else {
                        _ovStore.setClickInstanceInfo({ paneId: 'candle_pane', instance: null, figureType: 0, figureIndex: -1, figureKey: '' });
                        klineSelectedOverlayId = null;
                    }
                } catch(ex) {
                    klineSelectedOverlayId = hitId || null;
                }
                // Show/hide mobile context bar for selected drawing
                if (typeof _showMobileDrawingContextBar === 'function') {
                    _showMobileDrawingContextBar(hitId);
                }
            }
        }
        if (wasPanning && !_crosshairMode) {
            // Delay clearing _touchPanActive so pending renders don't snap back
            setTimeout(function() { window._touchPanActive = false; }, 300);
        }
    }
    container.addEventListener('touchend', endChartTouch, { capture: true });
    container.addEventListener('touchcancel', endChartTouch, { capture: true });
}

// Mobile OHLCV display — show latest candle data on init + keep updated
function setupMobileOhlcvDisplay() {
    if (!klineChart) return;
    var el = document.getElementById('mobile-ohlcv');
    if (!el) return;
    // Overlay the OHLCV legend on the chart's top-left corner (TradingView-style)
    // instead of consuming a lane in the header. Reparent it into the chart body once
    // (CSS positions it absolute over the canvas).
    var _ohlcvBody = document.querySelector('#adv-chart-content .adv-chart-body') || document.querySelector('.adv-chart-body');
    if (_ohlcvBody && el.parentNode !== _ohlcvBody) { _ohlcvBody.appendChild(el); }
    function _showLatest() {
        if (_crosshairMode || window._touchPanActive) return;
        var d = _lastRealBar(klineChart.getDataList());
        if (d) {
            el.textContent = 'O:' + _mFmtP(d.open) + ' H:' + _mFmtP(d.high) +
                ' L:' + _mFmtP(d.low) + ' C:' + _mFmtP(d.close) + ' V:' + _mFmtV(d.volume);
        }
    }
    _showLatest();
    // Periodically update with latest candle when not in crosshair mode
    if (window._mobileOhlcvInterval) clearInterval(window._mobileOhlcvInterval);
    window._mobileOhlcvInterval = setInterval(_showLatest, 2000);
}

// Landscape mode: shrink indicator panes + toggle body class for CSS
var _landscapePanesApplied = false;
function _isLandscapeMobile() {
    return window.innerHeight <= 600 && window.innerWidth > window.innerHeight;
}
function _updateLandscapeClass() {
    if (_isLandscapeMobile()) {
        document.body.classList.add('mobile-landscape');
    } else {
        document.body.classList.remove('mobile-landscape');
    }
}
function _adjustLandscapePanes() {
    _updateLandscapeClass();
    if (!klineChart) return;
    var landscape = _isLandscapeMobile();
    if (landscape === _landscapePanesApplied) return;
    _landscapePanesApplied = landscape;
    var isMulti = (typeof _mcLayout !== 'undefined' && _mcLayout !== 'single');
    var subPanes = ['volume_pane', 'rsi_pane', 'macd_pane', 'deriv_combined_pane'];
    // Collect all chart instances to adjust
    var charts = [klineChart];
    if (typeof _mcPanes !== 'undefined') {
        for (var c = 0; c < _mcPanes.length; c++) {
            if (c !== (typeof _mcActivePaneIdx !== 'undefined' ? _mcActivePaneIdx : 0) && _mcPanes[c].chart) {
                charts.push(_mcPanes[c].chart);
            }
        }
    }
    for (var ci = 0; ci < charts.length; ci++) {
        var ch = charts[ci];
        if (!ch || !ch.setPaneOptions) continue;
        for (var i = 0; i < subPanes.length; i++) {
            var baseH = subPanes[i] === 'volume_pane' ? 60 : 70;
            var h = (landscape || isMulti) ? (subPanes[i] === 'volume_pane' ? 40 : 45) : baseH;
            var mh = (landscape || isMulti) ? 25 : 40;
            try {
                ch.setPaneOptions({ id: subPanes[i], height: h, minHeight: mh });
            } catch(ex) {}
        }
    }
    setTimeout(function() {
        if (typeof mcResizeAll === 'function') mcResizeAll();
        else if (klineChart) klineChart.resize();
    }, 50);
}
// Listen for orientation changes
if ('ontouchstart' in window || navigator.maxTouchPoints > 0) {
    _updateLandscapeClass(); // set on page load
    window.addEventListener('orientationchange', function() {
        setTimeout(function() { _adjustLandscapePanes(); }, 200);
    });
    window.addEventListener('resize', (function() {
        var _resizeTimer = null;
        return function() {
            if (_resizeTimer) clearTimeout(_resizeTimer);
            _resizeTimer = setTimeout(_adjustLandscapePanes, 200);
        };
    })());
}

// ═══════════════════════════════════════════════════════════════════
// Mobile PWA v2 — Unified Sheet Management System
// ═══════════════════════════════════════════════════════════════════

var _mobileActiveTab = null;
var _mobileSheetEl = null;
var _mobileBackdropEl = null;
var _mobileSheetDragState = null;
function openMobileIndicatorSheet() { openMobileSheet('chart'); }

function openMobileSheet(tab) {
    // Close existing sheet first. Unconditional (not gated on _mobileSheetEl)
    // so a DOM/state mismatch — e.g. after a bfcache restore leaves stray
    // .mobile-feature-sheet/.mobile-sheet-backdrop nodes but the module vars
    // got reset — can't result in a second sheet stacking on top instead of
    // replacing the first. closeAllMobileSheets() is a no-op when nothing is
    // open, so this is always safe.
    closeAllMobileSheets();

    _mobileActiveTab = tab;

    // Update tab bar active state
    var bar = document.getElementById('mobile-action-bar');
    if (bar) {
        bar.querySelectorAll('.mobile-action-btn').forEach(function(btn) {
            btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
        });
    }

    // Mobile fix: append into klineModal (the advanced-chart-modal
    // element) rather than <body>, so these die WITH the chart modal — no
    // JS teardown call required — the instant it gets `display:none`
    // (.advanced-chart-modal.visible is what shows it; closeAdvancedChartModal
    // removes 'visible'). As body-level siblings of the
    // chart modal they could survive it being hidden and go on
    // to eat every tap on the header underneath. Safe: .advanced-chart-modal
    // sets no transform/filter/perspective/contain, so nesting these
    // position:fixed elements here changes nothing about their sizing
    // (still viewport-relative) or effective stacking (klineModal is already
    // z-index:25000, above the whole page, so descendants at 25999-26001
    // land in exactly the same visual position as before). Falls back to
    // <body> only if klineModal somehow isn't around, which shouldn't
    // happen — openMobileSheet is only ever invoked from the chart's own
    // mobile UI, which requires the modal to already exist.
    var _mobileSheetHost = (typeof klineModal !== 'undefined' && klineModal) ? klineModal : document.body;

    // Create backdrop
    _mobileBackdropEl = document.createElement('div');
    _mobileBackdropEl.className = 'mobile-sheet-backdrop';
    _mobileBackdropEl.addEventListener('click', function() { closeMobileSheet(); });
    _mobileSheetHost.appendChild(_mobileBackdropEl);

    // Create sheet
    _mobileSheetEl = document.createElement('div');
    _mobileSheetEl.className = 'mobile-feature-sheet';
    _mobileSheetEl.id = 'mobile-feature-sheet';

    var title = _mobileSheetTitle(tab);
    _mobileSheetEl.innerHTML =
        '<div class="mobile-sheet-drag-handle" id="mobile-sheet-drag-handle"><div class="drag-handle-bar"></div></div>' +
        '<div class="mobile-sheet-header">' +
            '<span>' + title + '</span>' +
            '<button class="mobile-sheet-close" id="mobile-sheet-close">&times;</button>' +
        '</div>' +
        '<div class="mobile-sheet-content" id="mobile-sheet-content"></div>';
    _mobileSheetHost.appendChild(_mobileSheetEl);

    // Wire close button
    document.getElementById('mobile-sheet-close').addEventListener('click', function() { closeMobileSheet(); });

    // Wire drag handle
    _initMobileSheetDrag();

    // Render content for the tab
    _renderMobileSheetContent(tab);

    // Enable alert click mode when alerts sheet is open
    if (typeof klineAlertClickMode !== 'undefined') {
        klineAlertClickMode = (tab === 'alerts');
    }
}

function closeMobileSheet() {
    closeAllMobileSheets();
}

// Mobile hardening — single shared teardown for the mobile
// "feature sheet" system (PWA v2's ⋮ More / TF / Indicator sheets), called
// from every close path (closeMobileSheet() above -> closeAdvancedChartModal
// and ~10 other in-app close sites, openMobileSheet()'s own "replace
// existing sheet" step, and the pageshow(persisted) bfcache-restore hook
// further down). Why: .mobile-sheet-backdrop /
// .mobile-feature-sheet appended straight to <body> would be SIBLINGS
// of the chart modal (not descendants), at z-index 25999-26001, and would not
// disappear when the chart modal itself is hidden —
// openMobileSheet() below nests them inside klineModal instead
// (display:none on the modal kills the whole subtree), which is the actual
// fix for the leak. This function is the second layer: if the page ever
// leaves the "sheet open" state without any JS close path running at all —
// the one case that can happen is a bfcache restore (see the pageshow hook
// further down) — the nested-in-the-modal structure only self-heals if the
// modal itself also ends up hidden, so this still exists as an explicit,
// idempotent belt-and-suspenders sweep for every path, not just the
// bfcache one.
//
// Query-based (DOM-driven) rather than relying solely on the
// _mobileBackdropEl/_mobileSheetEl module vars, so it also cleans up
// correctly whenever JS state and DOM could have drifted apart — a bfcache
// restore preserves both together in practice (see the pageshow hook's
// comment), but querying the DOM directly costs nothing and doesn't depend
// on that guarantee holding. Idempotent: safe to call any time, including
// when nothing is open.
function closeAllMobileSheets() {
    document.querySelectorAll('.mobile-sheet-backdrop').forEach(function(el) { el.remove(); });
    document.querySelectorAll('.mobile-feature-sheet').forEach(function(el) { el.remove(); });
    _mobileBackdropEl = null;
    _mobileSheetEl = null;
    _mobileActiveTab = null;

    // Clear tab bar active state
    var bar = document.getElementById('mobile-action-bar');
    if (bar) {
        bar.querySelectorAll('.mobile-action-btn').forEach(function(btn) {
            btn.classList.remove('active');
        });
    }

    // Disable alert click mode
    if (typeof klineAlertClickMode !== 'undefined') {
        klineAlertClickMode = false;
    }
}

// Kept for call-site compatibility — forwards to the shared teardown above.
function _destroyMobileSheet() {
    closeAllMobileSheets();
}

function _mobileSheetTitle(tab) {
    var titles = { chart: 'Chart', position: 'Positions', alerts: 'Alerts', plays: 'Plays', trade: 'Trade', report: 'Morning Report', ai: 'AI Assistant', more: 'More', splitChart: 'Split Chart Layout' };
    return titles[tab] || tab;
}

// ── Drag handle for sheet resize ──
function _initMobileSheetDrag() {
    var handle = document.getElementById('mobile-sheet-drag-handle');
    if (!handle || !_mobileSheetEl) return;

    handle.addEventListener('touchstart', function(e) {
        if (e.touches.length !== 1) return;
        var startY = e.touches[0].clientY;
        var startHeight = _mobileSheetEl.offsetHeight;
        _mobileSheetDragState = { startY: startY, startHeight: startHeight };
        _mobileSheetEl.style.transition = 'none';
    }, { passive: true });

    handle.addEventListener('touchmove', function(e) {
        if (!_mobileSheetDragState || e.touches.length !== 1) return;
        var dy = _mobileSheetDragState.startY - e.touches[0].clientY;
        var newH = Math.max(100, Math.min(window.innerHeight * 0.85, _mobileSheetDragState.startHeight + dy));
        _mobileSheetEl.style.height = newH + 'px';
    }, { passive: true });

    handle.addEventListener('touchend', function() {
        if (!_mobileSheetDragState) return;
        _mobileSheetEl.style.transition = '';
        var h = _mobileSheetEl.offsetHeight;
        var vh = window.innerHeight;
        // Snap points: dismiss (<15vh), 60vh, 85vh
        if (h < vh * 0.15) {
            closeMobileSheet();
        } else if (h < vh * 0.72) {
            _mobileSheetEl.style.height = '60vh';
        } else {
            _mobileSheetEl.style.height = '85vh';
        }
        _mobileSheetDragState = null;
    });
}

// ── Inject search + draw icons in top toolbar on mobile ──
function _injectMobileTopBarIcons() {
    var navRow = document.querySelector('.toolbar-row.toolbar-nav');
    if (!navRow) return;
    // Check if already injected
    if (document.getElementById('mob-top-search')) return;

    // Back-to-list arrow (top-left). The X close button is hidden on mobile
    // (main.css body.mobile-action-bar-active .adv-chart-close) and the bottom
    // bar has no close, so this is the only affordance back to the asset list.
    var backBtn = document.createElement('button');
    backBtn.className = 'mob-back-btn';
    backBtn.id = 'mob-back-btn';
    backBtn.setAttribute('aria-label', 'Back to markets');
    backBtn.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>';
    backBtn.addEventListener('click', function(e) {
        e.preventDefault(); e.stopPropagation();
        // If in mobile draw mode, the back arrow exits draw first (which restores the
        // bottom bar) rather than closing the chart. Draw mode's only other exit is
        // the FAB "Done", which is hidden when the "show favourites" pref is off —
        // leaving the user trapped with the bottom bar slid off-screen until refresh.
        if (document.body.classList.contains('mobile-draw-mode') && typeof exitMobileDrawMode === 'function') {
            exitMobileDrawMode();
            return;
        }
        closeAdvancedChartModal();
    });
    navRow.insertBefore(backBtn, navRow.firstChild);

    // Draw lives on the bottom bar; the top-right keeps Search and adds a
    // ⋮ overflow → openMobileSheet('more') (Widgets, Report, Split Chart, Rotate, Settings).
    var container = document.createElement('span');
    container.className = 'mobile-top-icons';
    container.innerHTML =
        '<button class="tool-btn mobile-top-icon" id="mob-top-search" title="Search"><i data-lucide="search"></i></button>' +
        '<button class="tool-btn mobile-top-icon" id="mob-top-more" title="More" aria-label="More"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg></button>';
    navRow.appendChild(container);
    // Group the Share button into the right-side icon cluster so the lane reads
    // [back] … [asset ▾][TF ▾] … [share][search][⋮], all the same size.
    var _shareBtn = navRow.querySelector('#chart-share-btn');
    if (_shareBtn) { _shareBtn.classList.add('mobile-top-icon'); container.insertBefore(_shareBtn, container.firstChild); }
    if (typeof lucide !== 'undefined') lucide.createIcons({ nodes: [container], attrs: { 'stroke-width': 1.75 } });

    document.getElementById('mob-top-search').addEventListener('click', function(e) {
        e.preventDefault(); e.stopPropagation();
        if (typeof showAssetSearchPopup === 'function') showAssetSearchPopup();
    });
    document.getElementById('mob-top-more').addEventListener('click', function(e) {
        e.preventDefault(); e.stopPropagation();
        if (typeof openMobileSheet === 'function') openMobileSheet('more');
    });

    // Timeframe dropdown next to the asset switcher (a plain menu — the old
    // swipe carousel's horizontal drag got read as a symbol-swipe, changing the asset).
    _buildMobileTfDropdown();
}

// ── Mobile timeframe dropdown (header) ───────────────────────────────────
// A simple dropdown of the favourite timeframes, sitting beside the asset
// switcher. Replaces the scroll-snap carousel (awkward + its swipe collided
// with the chart's swipe-to-change-symbol gesture).
function _buildMobileTfDropdown() {
    if (document.getElementById('mob-tf-dd')) return;
    var title = document.querySelector('#adv-chart-content .adv-chart-title') || document.querySelector('.adv-chart-title');
    if (!title) return;
    var dd = document.createElement('div');
    dd.className = 'mob-tf-dd';
    dd.id = 'mob-tf-dd';
    dd.innerHTML =
        '<button class="mob-tf-dd-btn" id="mob-tf-dd-btn" aria-label="Timeframe">' +
            '<span id="mob-tf-current">' + escapeHtml(klineChartTimeframe || '') + '</span>' +
            '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>' +
        '</button>' +
        '<div class="mob-tf-dd-menu" id="mob-tf-dd-menu"></div>';
    var anchor = title.querySelector('.asset-switcher-dropdown');
    if (anchor) title.insertBefore(dd, anchor.nextSibling);
    else title.insertBefore(dd, title.firstChild);
    _wireMobileTfDropdown(dd);
}

function _wireMobileTfDropdown(dd) {
    var btn = dd.querySelector('#mob-tf-dd-btn');
    var menu = dd.querySelector('#mob-tf-dd-menu');
    var _outside = function(e) { if (!dd.contains(e.target) && !menu.contains(e.target)) _close(); };
    function _close() {
        dd.classList.remove('open');
        document.removeEventListener('click', _outside, true);
    }
    function _open() {
        var tfs = _mobileTfList();
        var html = '';
        tfs.forEach(function(tf) {
            html += '<button class="mob-tf-dd-item' + (tf === klineChartTimeframe ? ' active' : '') + '" data-tf="' + tf + '">' + escapeHtml(tf) + '</button>';
        });
        html += '<button class="mob-tf-dd-edit" id="mob-tf-dd-edit">Edit…</button>';
        menu.innerHTML = html;
        // Fixed positioning so the toolbar's overflow:auto doesn't clip the menu.
        var r = btn.getBoundingClientRect();
        menu.style.left = Math.round(r.left) + 'px';
        menu.style.top = Math.round(r.bottom + 4) + 'px';
        dd.classList.add('open');
        setTimeout(function() { document.addEventListener('click', _outside, true); }, 0);
    }
    btn.addEventListener('click', function(e) {
        e.preventDefault(); e.stopPropagation();
        if (dd.classList.contains('open')) _close(); else _open();
    });
    menu.addEventListener('click', function(e) {
        var item = e.target.closest('.mob-tf-dd-item');
        if (item) {
            e.preventDefault(); e.stopPropagation();
            var tf = item.getAttribute('data-tf');
            if (tf && tf !== klineChartTimeframe && typeof switchTimeframe === 'function') switchTimeframe(tf);
            _refreshMobileTfLabel();
            _close();
            return;
        }
        if (e.target.closest('#mob-tf-dd-edit')) {
            e.preventDefault(); e.stopPropagation();
            _close();
            if (typeof showTfEditor === 'function') showTfEditor(klineChartSymbol);
        }
    });
}

function _refreshMobileTfLabel() {
    var el = document.getElementById('mob-tf-current');
    if (el) el.textContent = klineChartTimeframe || '';
}

// ── Mobile timeframe picker ──────────────────────────────────────────────
// Horizontal CSS scroll-snap strip: the active TF sits centred (highlighted),
// you swipe to bring another to centre, and it snaps + switches. Tapping an item
// also snaps it to centre. The iOS-alarm-wheel pattern on a horizontal axis —
// CSS handles the snapping; JS just reads the centred item after scroll settles.
function _mobileTfList() {
    var tfs;
    if (!window._isPremium) tfs = ['4h', '1D', '3D', '1W', '1M'];
    else if (typeof isCryptoAsset === 'function' && isCryptoAsset(klineChartSymbol)) tfs = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1D', '3D', '1W', '1M'];
    else tfs = ['1h', '4h', '1D', '3D', '1W', '1M'];
    var favs = null;
    try { favs = JSON.parse(localStorage.getItem('tfFavourites')); } catch (e) {}
    if (favs && favs.length > 0) tfs = favs;
    return tfs;
}

// ═══════════════════════════════════════════════════════════════════
// Mobile Sheet Content Renderers
// ═══════════════════════════════════════════════════════════════════

function _renderMobileSheetContent(tab) {
    var content = document.getElementById('mobile-sheet-content');
    if (!content) return;

    switch (tab) {
        case 'chart': _renderMobileChartTab(content); break;
        case 'position': _renderMobilePositionTab(content); break;
        case 'alerts': _renderMobileAlertsTab(content); break;
        case 'report': _renderMobileReportTab(content); break;
        case 'ai': _renderMobileAITab(content); break;
        case 'plays': _renderMobilePlaysTab(content); break;
        case 'trade': _renderMobileTradeTab(content); break;
        case 'more': _renderMobileMoreTab(content); break;
        case 'splitChart': _renderMobileSplitChartTab(content); break;
        default: content.innerHTML = '<div class="drawer-empty">Unknown tab</div>';
    }
}

// ── Chart tab: TF grid + Indicators + Display options ──
function _renderMobileChartTab(content) {
    var html = '';

    // Section 1: Timeframes
    html += '<div class="mobile-chart-section">';
    html += '<div class="mobile-chart-section-title">Timeframe</div>';
    var tfs;
    if (!window._isPremium) {
        tfs = ['4h','1D','3D','1W','1M'];
    } else if (typeof isCryptoAsset === 'function' && isCryptoAsset(klineChartSymbol)) {
        tfs = ['1m','3m','5m','15m','30m','1h','4h','1D','3D','1W','1M'];
    } else {
        tfs = ['1h','4h','1D','3D','1W','1M'];
    }
    // Check for TF favourites
    var favs = null;
    try { favs = JSON.parse(localStorage.getItem('tfFavourites')); } catch(e) {}
    if (favs && favs.length > 0) tfs = favs;

    html += '<div class="mobile-tf-grid">';
    tfs.forEach(function(tf) {
        var active = tf === klineChartTimeframe ? ' active' : '';
        html += '<button class="mobile-tf-btn' + active + '" data-tf="' + tf + '">' + tf + '</button>';
    });
    html += '</div>';
    html += '<div style="text-align:right;padding:4px 0"><button class="mobile-link-btn" id="mob-edit-tf-favs">Edit Favourites</button></div>';
    html += '</div>';

    // Section 2: Indicators
    html += '<div class="mobile-chart-section">';
    html += '<div class="mobile-chart-section-title">Indicators</div>';

    // Group pills row
    if (typeof klineIndicatorGroups !== 'undefined' && klineIndicatorGroups.length > 0) {
        html += '<div class="mobile-ind-groups">';
        klineIndicatorGroups.forEach(function(group) {
            html += '<span class="mobile-ind-group-wrapper">' +
                '<button class="mobile-ind-group-pill' + (_isGroupActive(group) ? ' active' : '') + (_isGroupPartial(group) ? ' partial' : '') + '" data-group-id="' + group.id + '">' + escapeHtml(group.name) + '</button>' +
                '<button class="mobile-ind-group-edit" data-group-edit="' + group.id + '">&#9998;</button></span>';
        });
        html += '<button class="mobile-ind-group-pill mobile-ind-group-add" id="mobile-group-add">+</button>';
        html += '</div>';
    } else {
        html += '<div class="mobile-ind-groups"><button class="mobile-ind-group-pill mobile-ind-group-add" id="mobile-group-add">+ Group</button></div>';
    }

    // Filter box — jump to an indicator by name across all categories.
    html += '<input class="mobile-ind-filter" id="mob-ind-filter" type="text" placeholder="Filter indicators…" autocomplete="off">';

    // Collapsible category accordions (the registry tags each indicator with its
    // category as `section`). Categories with active indicators auto-expand; the
    // rest collapse so it's a short, scannable list instead of a ~49-row wall.
    var indicators = IndicatorRegistry.buildMobileIndicatorList();
    var _cats = [];
    var _curCat = null;
    indicators.forEach(function(ind) {
        if (ind.section) { _curCat = { label: ind.section, items: [] }; _cats.push(_curCat); }
        if (!_curCat) { _curCat = { label: 'Indicators', items: [] }; _cats.push(_curCat); }
        _curCat.items.push(ind);
    });
    html += '<div class="mobile-ind-accordion">';
    _cats.forEach(function(cat) {
        var onCount = cat.items.filter(function(it) { return klineActiveIndicators[it.key]; }).length;
        // Collapsed by default so the Chart sheet isn't a long wall of categories;
        // the "N on" badge still flags which categories have active indicators, and
        // the filter box re-expands matching categories on demand.
        var exp = false;
        html += '<div class="mobile-ind-cat-wrap' + (exp ? ' expanded' : '') + '">';
        html += '<button class="mobile-ind-cat" data-cat-toggle="1" aria-expanded="' + exp + '">' +
            '<span class="mobile-ind-cat-caret">▸</span>' +
            '<span class="mobile-ind-cat-name">' + escapeHtml(cat.label) + '</span>' +
            '<span class="mobile-ind-cat-count"' + (onCount ? '' : ' style="display:none"') + '>' + onCount + ' on</span>' +
        '</button>';
        html += '<div class="mobile-ind-cat-items">';
        cat.items.forEach(function(ind) {
            var active = klineActiveIndicators[ind.key] ? ' active' : '';
            html += '<button class="mobile-ind-item' + active + '" data-ind="' + ind.key + '" data-ind-label="' + escapeHtml((ind.label || '').toLowerCase()) + '">' +
                '<span class="mobile-ind-check">' + (klineActiveIndicators[ind.key] ? '✓' : '') + '</span>' +
                '<span>' + ind.label + (ind.pro ? ' <span style="opacity:0.4;font-size:10px">PRO</span>' : '') + '</span></button>';
        });
        html += '</div></div>';
    });
    html += '</div></div>';

    // Section 3: Display options
    html += '<div class="mobile-chart-section">';
    html += '<div class="mobile-chart-section-title">Display</div>';
    html += '<div class="mobile-display-options">';
    // Candle type
    var currentType = localStorage.getItem('klineCandleType') || 'candle_solid';
    var candleTypes = [
        { key: 'candle_solid', label: 'Candles' },
        { key: 'candle_stroke', label: 'Hollow' },
        { key: 'ohlc', label: 'OHLC' },
        { key: 'area', label: 'Area' }
    ];
    html += '<div class="mobile-display-row"><span class="mobile-display-label">Candle Type</span><div class="mobile-display-pills">';
    candleTypes.forEach(function(ct) {
        html += '<button class="mobile-display-pill' + (ct.key === currentType ? ' active' : '') + '" data-candle-type="' + ct.key + '">' + ct.label + '</button>';
    });
    html += '</div></div>';
    // Toggles
    var greenRed = localStorage.getItem('greenRedCandles') === '1';
    var whiteBg = localStorage.getItem('whiteBackground') === '1';
    var logScale = localStorage.getItem('klineLogScale') === '1';
    html += '<div class="mobile-display-row"><span class="mobile-display-label">Green/Red Candles</span><label class="mobile-toggle"><input type="checkbox" id="mob-green-red"' + (greenRed ? ' checked' : '') + '><span class="mobile-toggle-slider"></span></label></div>';
    html += '<div class="mobile-display-row"><span class="mobile-display-label">White Background</span><label class="mobile-toggle"><input type="checkbox" id="mob-white-bg"' + (whiteBg ? ' checked' : '') + '><span class="mobile-toggle-slider"></span></label></div>';
    html += '<div class="mobile-display-row"><span class="mobile-display-label">Log Scale</span><label class="mobile-toggle"><input type="checkbox" id="mob-log-scale"' + (logScale ? ' checked' : '') + '><span class="mobile-toggle-slider"></span></label></div>';
    html += '<div class="mobile-display-row"><span class="mobile-display-label">Invert Scale</span><label class="mobile-toggle"><input type="checkbox" id="mob-invert-scale"' + (_invertScaleEnabled ? ' checked' : '') + '><span class="mobile-toggle-slider"></span></label></div>';
    html += '</div></div>';

    content.innerHTML = html;

    // Wire TF buttons
    content.addEventListener('click', function(e) {
        var tfBtn = e.target.closest('[data-tf]');
        if (tfBtn) {
            if (typeof switchTimeframe === 'function') switchTimeframe(tfBtn.dataset.tf);
            content.querySelectorAll('.mobile-tf-btn').forEach(function(b) { b.classList.toggle('active', b.dataset.tf === tfBtn.dataset.tf); });
            // Close the sheet so the timeframe change is actually visible on the chart.
            if (typeof closeMobileSheet === 'function') closeMobileSheet();
            return;
        }
        // Indicator group toggle
        var groupPill = e.target.closest('.mobile-ind-group-pill[data-group-id]');
        if (groupPill) {
            e.stopPropagation();
            if (typeof toggleIndicatorGroup === 'function') toggleIndicatorGroup(groupPill.getAttribute('data-group-id'));
            _refreshMobileChartIndicators(content);
            return;
        }
        // Group edit
        var editBtn = e.target.closest('[data-group-edit]');
        if (editBtn) {
            e.stopPropagation();
            if (typeof showGroupEditor === 'function') showGroupEditor(editBtn.getAttribute('data-group-edit'));
            return;
        }
        // Group add
        if (e.target.closest('.mobile-ind-group-add')) {
            e.stopPropagation();
            if (typeof _createAndEditGroup === 'function') _createAndEditGroup();
            return;
        }
        // Category accordion toggle
        var catBtn = e.target.closest('[data-cat-toggle]');
        if (catBtn) {
            var catWrap = catBtn.closest('.mobile-ind-cat-wrap');
            if (catWrap) catBtn.setAttribute('aria-expanded', catWrap.classList.toggle('expanded'));
            return;
        }
        // Indicator toggle
        var indBtn = e.target.closest('[data-ind]');
        if (indBtn) {
            var ind = indBtn.getAttribute('data-ind');
            var _premiumInds = {CVD_USDT:1, CVD_COIN:1, OI:1, DERIV_COMBINED:1};
            if (_premiumInds[ind] && !window._isPremium) {
                if (typeof showToast === 'function') showToast('Derivatives indicators require Premium.');
                return;
            }
            // Macro & Quant indicators require premium/tester/admin — mirror
            // of the desktop gate (the indicator menu handler in chart-events.js) so a tap can't silently
            // toggle a premiumOnly indicator for a non-premium user.
            var _indMetaM = typeof IndicatorRegistry !== 'undefined' ? IndicatorRegistry.getIndicator(ind) : null;
            if (_indMetaM && _indMetaM.premiumOnly && !window._hasPremiumFeatures) {
                if (typeof showUpgradeModal === 'function') showUpgradeModal('Macro & Quant indicators are available with a Premium subscription.');
                else if (typeof showToast === 'function') showToast('Macro & Quant indicators require a Premium subscription.');
                return;
            }
            if (typeof toggleKlineIndicator === 'function') toggleKlineIndicator(ind);
            _refreshMobileChartIndicators(content);
            return;
        }
        // Candle type
        var ctBtn = e.target.closest('[data-candle-type]');
        if (ctBtn) {
            var type = ctBtn.dataset.candleType;
            localStorage.setItem('klineCandleType', type);
            if (klineChart) {
                klineChart.setStyles({ candle: { type: type } });
            }
            content.querySelectorAll('[data-candle-type]').forEach(function(b) { b.classList.toggle('active', b.dataset.candleType === type); });
            return;
        }
    });

    // Wire edit TF favourites
    var editTfBtn = document.getElementById('mob-edit-tf-favs');
    if (editTfBtn) editTfBtn.addEventListener('click', function() {
        if (typeof showTfEditor === 'function') showTfEditor(klineChartSymbol);
    });

    // Wire the indicator filter — show only matching items, expand categories that
    // have a match (and hide empty ones); clearing restores the active-only expand.
    var indFilter = document.getElementById('mob-ind-filter');
    if (indFilter) indFilter.addEventListener('input', function() {
        var q = this.value.toLowerCase().trim();
        content.querySelectorAll('.mobile-ind-cat-wrap').forEach(function(wrap) {
            var any = false;
            wrap.querySelectorAll('.mobile-ind-item').forEach(function(item) {
                var match = !q || (item.getAttribute('data-ind-label') || '').indexOf(q) !== -1;
                item.style.display = match ? '' : 'none';
                if (match) any = true;
            });
            if (q) {
                wrap.classList.toggle('expanded', any);
                wrap.style.display = any ? '' : 'none';
            } else {
                wrap.style.display = '';
                wrap.classList.toggle('expanded', wrap.querySelectorAll('.mobile-ind-item.active').length > 0);
            }
        });
    });

    // Wire display toggles
    var grCb = document.getElementById('mob-green-red');
    if (grCb) grCb.addEventListener('change', function() {
        localStorage.setItem('greenRedCandles', this.checked ? '1' : '0');
        if (typeof toggleGreenRedCandles === 'function') {
            var orig = document.getElementById('view-green-red');
            if (orig) orig.checked = this.checked;
            toggleGreenRedCandles();
        }
    });
    var wbCb = document.getElementById('mob-white-bg');
    if (wbCb) wbCb.addEventListener('change', function() {
        localStorage.setItem('whiteBackground', this.checked ? '1' : '0');
        if (typeof toggleWhiteBackground === 'function') {
            var orig = document.getElementById('view-white-bg');
            if (orig) orig.checked = this.checked;
            toggleWhiteBackground();
        }
    });
    var lsCb = document.getElementById('mob-log-scale');
    if (lsCb) lsCb.addEventListener('change', function() {
        localStorage.setItem('klineLogScale', this.checked ? '1' : '0');
        if (klineChart) {
            _applyChartPricePrecision(klineChart.getDataList());
            // Toggle yAxis type
            klineChart.setStyles({
                yAxis: { type: this.checked ? 'log' : 'normal' }
            });
        }
    });
    var invCb = document.getElementById('mob-invert-scale');
    if (invCb) invCb.addEventListener('change', function() {
        if (this.checked !== _invertScaleEnabled) toggleInvertScale();
    });
}

function _refreshMobileChartIndicators(content) {
    content.querySelectorAll('.mobile-ind-item[data-ind]').forEach(function(btn) {
        var k = btn.getAttribute('data-ind');
        var on = !!klineActiveIndicators[k];
        btn.classList.toggle('active', on);
        var check = btn.querySelector('.mobile-ind-check');
        if (check) check.textContent = on ? '✓' : '';
    });
    // Update per-category "N on" counts
    content.querySelectorAll('.mobile-ind-cat-wrap').forEach(function(wrap) {
        var n = wrap.querySelectorAll('.mobile-ind-item.active').length;
        var countEl = wrap.querySelector('.mobile-ind-cat-count');
        if (countEl) { countEl.textContent = n + ' on'; countEl.style.display = n ? '' : 'none'; }
    });
    content.querySelectorAll('.mobile-ind-group-pill[data-group-id]').forEach(function(pill) {
        var gid = pill.getAttribute('data-group-id');
        var grp = klineIndicatorGroups.find(function(g) { return g.id === gid; });
        if (!grp) return;
        pill.classList.toggle('active', !!grp.active);
    });
}

// ── Positions tab ──
function _renderMobilePositionTab(content) {
    content.innerHTML = '<div class="mobile-sheet-loading">Loading positions...</div>';
    // Reuse drawer position logic — render into our content area
    var allDrawingsStore = {};
    try { allDrawingsStore = JSON.parse(localStorage.getItem('klineDrawings') || '{}'); } catch(e) {}

    var posItems = [];
    var spotItems = [];
    Object.keys(allDrawingsStore).forEach(function(sym) {
        if (typeof _findAllPositionDrawings === 'function') {
            _findAllPositionDrawings(allDrawingsStore[sym] || []).forEach(function(pos) {
                posItems.push({ symbol: sym, drawing: pos });
            });
        }
        if (typeof _findAllSpotDrawings === 'function') {
            _findAllSpotDrawings(allDrawingsStore[sym] || []).forEach(function(spot) {
                spotItems.push({ symbol: sym, drawing: spot });
            });
        }
    });

    var posCount = posItems.length;
    var spotCount = spotItems.length;

    if (posCount === 0 && spotCount === 0) {
        content.innerHTML = '<div class="drawer-empty" style="padding:24px;text-align:center">No positions or spot buys logged.<br><button class="mobile-link-btn" style="margin-top:12px">Log a Trade</button></div>';
        var logTradeBtn = content.querySelector('.mobile-link-btn');
        if (logTradeBtn) logTradeBtn.addEventListener('click', function() {
            if (typeof openDrawer === 'function') { closeMobileSheet(); openDrawer('position'); }
        });
        return;
    }

    var html = '';
    // Spots (rendered first — per early user feedback)
    if (spotCount > 0) {
        html += '<div class="mobile-chart-section-title">Spot Buys (' + spotCount + ')</div>';
        spotItems.sort(function(a, b) {
            var aP = allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0;
            var bP = allAssets[b.symbol] ? allAssets[b.symbol].latest_price : 0;
            return ((b.drawing.extendData || {}).quantity || 0) * bP - ((a.drawing.extendData || {}).quantity || 0) * aP;
        });
        // Group spots by symbol for mobile display
        var spotsBySymbol = {};
        spotItems.forEach(function(item) {
            if (!spotsBySymbol[item.symbol]) spotsBySymbol[item.symbol] = [];
            spotsBySymbol[item.symbol].push(item);
        });
        Object.keys(spotsBySymbol).sort().forEach(function(sym) {
            var group = spotsBySymbol[sym];
            var cp = allAssets[sym] ? allAssets[sym].latest_price : 0;
            var totalQty = 0, totalUsd = 0;
            group.forEach(function(i) { var e = i.drawing.extendData || {}; totalQty += e.quantity || 0; totalUsd += e.usdAmount || 0; });
            var avgEntry = totalQty > 0 ? totalUsd / totalQty : 0;
            var currentVal = totalQty * cp;
            var pnl = currentVal - totalUsd;
            var pnlPct = totalUsd > 0 ? (pnl / totalUsd * 100) : 0;
            var pnlColor = pnl >= 0 ? '#26a69a' : '#ef5350';
            var pnlSign = pnl >= 0 ? '+' : '';
            var priceStr = typeof formatPriceSimple === 'function' ? formatPriceSimple(avgEntry) : avgEntry.toFixed(2);
            html += '<div class="mobile-position-card" style="padding:10px 12px">' +
                '<div style="display:flex;justify-content:space-between;align-items:center">' +
                    '<span style="color:#d1d4dc;font-weight:600;font-size:13px" data-symbol="' + escapeHtml(sym) + '">' + escapeHtml(sym) + '</span>' +
                    '<span style="color:' + pnlColor + ';font-size:12px;font-weight:500">' + pnlSign + pnlPct.toFixed(1) + '% · $' + Math.abs(pnl).toFixed(0) + '</span>' +
                '</div>' +
                '<div style="display:flex;gap:12px;margin-top:4px;color:#6e7681;font-size:10px">' +
                    '<span>Avg: $' + priceStr + '</span>' +
                    '<span>Qty: ' + totalQty.toFixed(4) + '</span>' +
                    '<span>Cost: $' + totalUsd.toFixed(0) + '</span>' +
                    '<span>' + group.length + ' buy' + (group.length !== 1 ? 's' : '') + '</span>' +
                '</div>' +
            '</div>';
        });
    }
    // Futures (rendered after Spots)
    if (posCount > 0) {
        var futuresMt = spotCount > 0 ? ' style="margin-top:12px"' : '';
        html += '<div class="mobile-chart-section-title"' + futuresMt + '>Futures (' + posCount + ')</div>';
        posItems.sort(function(a, b) { return a.symbol.localeCompare(b.symbol); });
        posItems.forEach(function(item) {
            try {
                if (typeof _renderSinglePosition === 'function') {
                    html += '<div class="mobile-position-card">' + _renderSinglePosition(item.drawing, item.symbol).html + '</div>';
                }
            } catch(e) {}
        });
    }
    content.innerHTML = html;

    // Wire position card clicks to navigate to asset
    content.addEventListener('click', function(e) {
        var card = e.target.closest('.mobile-position-card');
        if (!card) return;
        var sym = card.querySelector('[data-symbol]');
        if (sym) {
            var s = sym.getAttribute('data-symbol');
            if (s && s !== klineChartSymbol && typeof switchToAsset === 'function') {
                switchToAsset(s);
            }
        }
    });
}

// ── Alerts tab ──
function _renderMobileAlertsTab(content) {
    content.innerHTML = '<div class="mobile-sheet-loading">Loading alerts...</div>';

    fetch('/api/alerts?active=true', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var all = data.alerts || [];
        if (all.length === 0) {
            content.innerHTML = '<div class="drawer-empty" style="padding:24px;text-align:center">No active alerts.<br><span style="color:#6e7681;font-size:11px">Tap the chart to set a price alert.</span></div>';
            return;
        }

        // Group by symbol, current asset first
        var current = all.filter(function(a) { return a.symbol === klineChartSymbol; });
        var others = all.filter(function(a) { return a.symbol !== klineChartSymbol; });

        function alertSortKey(a) {
            var cp = allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0;
            return cp > 0 ? Math.abs(a.price - cp) / cp : 0;
        }
        current.sort(function(a, b) { return alertSortKey(a) - alertSortKey(b); });
        others.sort(function(a, b) { return a.symbol.localeCompare(b.symbol) || alertSortKey(a) - alertSortKey(b); });
        var sorted = current.concat(others);

        var html = '<div style="padding:4px 0 8px;color:#6e7681;font-size:11px">' + all.length + ' active alert' + (all.length !== 1 ? 's' : '') + ' · tap chart to add</div>';
        var lastSym = null;
        sorted.forEach(function(a) {
            if (a.symbol !== lastSym) {
                lastSym = a.symbol;
                var cp = allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0;
                html += '<div class="mobile-alert-group-header">' + escapeHtml(a.symbol) + (cp > 0 ? ' · ' + (typeof formatPriceSimple === 'function' ? formatPriceSimple(cp) : cp.toFixed(2)) : '') + '</div>';
            }
            var isAbove = a.price > (allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0);
            var cp = allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0;
            var dist = cp > 0 ? ((a.price - cp) / cp * 100).toFixed(1) : '--';
            var distColor = isAbove ? '#26a69a' : '#ef5350';
            var arrow = isAbove ? '&#9650;' : '&#9660;';
            var priceStr = typeof formatPriceSimple === 'function' ? formatPriceSimple(a.price) : a.price.toFixed(2);
            html += '<div class="mobile-alert-item" data-alert-id="' + a.id + '">' +
                '<span class="mobile-alert-dir" style="color:' + distColor + '">' + arrow + '</span>' +
                '<span class="mobile-alert-price">' + priceStr + '</span>' +
                '<span class="mobile-alert-dist" style="color:' + distColor + '">' + (dist > 0 ? '+' : '') + dist + '%</span>' +
                (a.note ? '<span class="mobile-alert-note">' + escapeHtml(a.note) + '</span>' : '') +
                '<button class="mobile-alert-delete" data-delete-alert="' + a.id + '">&times;</button>' +
            '</div>';
        });
        content.innerHTML = html;

        // Wire delete buttons
        content.addEventListener('click', function(e) {
            var delBtn = e.target.closest('[data-delete-alert]');
            if (delBtn) {
                e.stopPropagation();
                var id = delBtn.getAttribute('data-delete-alert');
                fetch('/api/alerts/' + id, { method: 'DELETE', headers: { 'X-CSRFToken': getCSRFToken() }, credentials: 'same-origin' })
                .then(function() {
                    var row = delBtn.closest('.mobile-alert-item');
                    if (row) row.style.display = 'none';
                    if (typeof renderAlertOverlays === 'function') renderAlertOverlays();
                    if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts();
                });
            }
        });
    })
    .catch(function() {
        content.innerHTML = '<div class="drawer-empty">Failed to load alerts.</div>';
    });
}

// ── Report tab — iframes the canonical /report/morning page ──
// The canonical page is embedded rather than re-parsed here: slicing
// `data.content` by keyword into hardcoded tabs drifts from the report
// structure and gives no audio player or mobile CSS.
//
// Iframe is boring, low-maintenance, and stays in sync with the canonical page
// automatically. All formatting, tabs, and audio live in templates/morning_report.html.
function _renderMobileReportTab(content) {
    // Use the full sheet height; morning report page is already responsive.
    content.innerHTML =
        '<div class="mobile-report-iframe-wrap">' +
            '<iframe class="mobile-report-iframe" id="mobile-report-iframe" ' +
                'src="/report/morning" ' +
                'loading="lazy" ' +
                'referrerpolicy="same-origin" ' +
                'title="Morning Report"></iframe>' +
        '</div>' +
        '<div class="mobile-report-footer">' +
            '<a href="/report/morning" target="_blank" class="mobile-link-btn">Open in new tab &#8599;</a>' +
        '</div>';
}

// ── AI tab ──
function _renderMobileAITab(content) {
    var html = '<div class="mobile-ai-quick-actions">' +
        '<button class="mobile-ai-action" data-ai-action="analyze">Analyze</button>' +
        '<button class="mobile-ai-action" data-ai-action="validate">Validate</button>' +
        '<button class="mobile-ai-action" data-ai-action="challenge">Challenge</button>' +
        '<button class="mobile-ai-action" data-ai-action="checklist">Checklist</button>' +
    '</div>';
    html += '<div class="mobile-ai-chat" id="mobile-ai-chat"></div>';
    html += '<div class="mobile-ai-input-row">' +
        '<textarea class="mobile-ai-input" id="mobile-ai-input" placeholder="Ask about this chart..." rows="1"></textarea>' +
        '<button class="mobile-ai-send" id="mobile-ai-send"><i data-lucide="send"></i></button>' +
    '</div>';
    content.innerHTML = html;
    if (typeof lucide !== 'undefined') lucide.createIcons({ nodes: [content], attrs: { 'stroke-width': 1.75 } });

    // Load existing conversation
    var chatDiv = document.getElementById('mobile-ai-chat');
    var existingChat = document.getElementById('ai-panel-chat');
    if (existingChat && chatDiv) {
        chatDiv.innerHTML = existingChat.innerHTML;
        chatDiv.scrollTop = chatDiv.scrollHeight;
    }

    // Wire quick actions
    content.addEventListener('click', function(e) {
        var actionBtn = e.target.closest('[data-ai-action]');
        if (actionBtn) {
            var action = actionBtn.dataset.aiAction;
            var input = document.getElementById('mobile-ai-input');
            if (input) {
                var prompts = {
                    analyze: 'Analyze ' + klineChartSymbol + ' on ' + klineChartTimeframe,
                    validate: 'Validate my current position on ' + klineChartSymbol,
                    challenge: 'Challenge my bias on ' + klineChartSymbol,
                    checklist: 'Pre-trade checklist for ' + klineChartSymbol
                };
                input.value = prompts[action] || action;
                _sendMobileAIMessage();
            }
        }
    });

    // Wire send
    var sendBtn = document.getElementById('mobile-ai-send');
    if (sendBtn) sendBtn.addEventListener('click', _sendMobileAIMessage);
    var inputEl = document.getElementById('mobile-ai-input');
    if (inputEl) inputEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            _sendMobileAIMessage();
        }
    });
}

function _sendMobileAIMessage() {
    var input = document.getElementById('mobile-ai-input');
    if (!input || !input.value.trim()) return;
    var msg = input.value.trim();
    input.value = '';

    // Delegate to existing AI panel send logic
    if (typeof _aiPanelDoSend === 'function') {
        _aiPanelDoSend(msg);
        // Sync chat to mobile after streaming updates
        var _syncInterval = setInterval(function() {
            var chatDiv = document.getElementById('mobile-ai-chat');
            var existingChat = document.getElementById('ai-panel-chat');
            if (existingChat && chatDiv) {
                chatDiv.innerHTML = existingChat.innerHTML;
                chatDiv.scrollTop = chatDiv.scrollHeight;
            }
            // Stop syncing when streaming ends
            if (typeof aiPanelStreaming !== 'undefined' && !aiPanelStreaming) {
                clearInterval(_syncInterval);
            }
        }, 300);
        // Safety: stop after 60s
        setTimeout(function() { clearInterval(_syncInterval); }, 60000);
    } else if (typeof showToast === 'function') {
        showToast('AI chat not available — open AI view first');
    }
}

// ── Plays tab (mobile) ──
function _renderMobilePlaysTab(content) {
    content.innerHTML = '<div style="padding:12px;color:#8b949e;font-size:13px;">Loading plays...</div>';
    fetch('/api/plays?include_resolved=false&limit=50', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var plays = data.plays || [];
        if (!plays.length) {
            content.innerHTML = '<div class="plays-empty-state">' +
                '<div class="plays-empty-title">No active plays</div>' +
                '<div class="plays-empty-desc">Create plays from the desktop chart to track your market beliefs.</div></div>';
            return;
        }
        var html = '';
        plays.forEach(function(p, i) {
            var dir = p.direction === 'long' ? '\u25B2' : p.direction === 'short' ? '\u25BC' : '\u25C6';
            var dirCls = p.direction === 'long' ? 'color:#3fb950' : p.direction === 'short' ? 'color:#ef5350' : 'color:#8b949e';
            html += '<div class="play-card mobile-play-card-tap" data-play-idx="' + i + '" role="button" tabindex="0" style="margin:6px 12px;cursor:pointer">';
            html += '<div class="play-card-header"><span class="play-direction-arrow" style="' + dirCls + '">' + dir + '</span>';
            html += '<span class="play-card-title">' + escapeHtml(p.title) + '</span><span class="mobile-play-chevron">\u203A</span></div>';
            html += '<div class="play-card-meta"><span class="play-type-badge">' + (p.type || 'directional') + '</span>';
            html += '<span class="play-approach-badge">' + (p.approach || 'single') + '</span>';
            if (p.primary_asset) html += '<span class="play-type-badge">' + p.primary_asset + '</span>';
            html += '</div>';
            html += '<div class="play-card-footer"><span class="play-status-pill ' + p.status + '">' + p.status + '</span></div>';
            html += '</div>';
        });
        content.innerHTML = html;
        // Tap a card \u2192 open a read-only detail (cards alone weren't openable).
        content.querySelectorAll('.mobile-play-card-tap').forEach(function(card) {
            card.addEventListener('click', function() {
                var idx = parseInt(this.getAttribute('data-play-idx'), 10);
                if (plays[idx]) _renderMobilePlayDetail(plays[idx], content);
            });
        });
    })
    .catch(function() {
        content.innerHTML = '<div class="drawer-empty" style="padding:24px;text-align:center">Failed to load plays</div>';
    });
}

// Read-only mobile play detail \u2014 tap a Plays card to read the full play. Reuses
// the play-* CSS; interactive controls (edit/backtest/share/status) stay desktop-only.
function _renderMobilePlayDetail(play, content) {
    var dirArrow = play.direction === 'long' ? '\u25B2' : play.direction === 'short' ? '\u25BC' : '\u25C6';
    var dirCls = play.direction === 'long' ? 'color:#3fb950' : play.direction === 'short' ? 'color:#ef5350' : 'color:#8b949e';
    var conds = (play.conditions && play.conditions.rules) || [];
    var met = conds.filter(function(r) { return r.met; }).length;
    var html = '<div class="mobile-play-detail">';
    html += '<button class="mobile-play-back" data-play-back="1">\u2039 Plays</button>';
    html += '<div class="play-card-header" style="margin-top:8px"><span class="play-direction-arrow" style="' + dirCls + '">' + dirArrow + '</span>';
    html += '<span class="play-card-title">' + escapeHtml(play.title || '') + '</span></div>';
    html += '<div class="play-card-meta"><span class="play-type-badge">' + (play.type || 'directional') + '</span>';
    html += '<span class="play-approach-badge">' + (play.approach || 'single') + '</span>';
    if (play.primary_asset) html += '<span class="play-type-badge">' + escapeHtml(play.primary_asset) + '</span>';
    html += '<span class="play-status-pill ' + play.status + '">' + play.status + '</span></div>';
    if (play.thesis) html += '<div class="play-thesis" style="margin-top:10px">' + escapeHtml(play.thesis) + '</div>';
    if (conds.length) {
        html += '<div class="play-conditions-label" style="margin-top:10px;display:block">Conditions ' + met + '/' + conds.length + '</div>';
        html += '<div class="play-conditions-list">';
        conds.forEach(function(r) {
            var cls = r.met ? 'met' : 'unmet';
            var icon = r.met ? '\u2713' : '\u25CB';
            var ev = r.evidence ? ' \u2014 ' + escapeHtml(r.evidence) : '';
            html += '<div class="play-condition ' + cls + '">' + icon + ' ' + escapeHtml(r.label || r.type || 'condition') + '<span class="play-condition-evidence">' + ev + '</span></div>';
        });
        html += '</div>';
    }
    if (play.invalidation && play.invalidation.type) {
        var im = play.invalidation.met;
        html += '<div class="play-invalidation" style="margin-top:8px"><span class="play-invalidation-label">Invalidation:</span> ';
        html += '<span class="play-condition ' + (im ? 'met' : 'unmet') + '">' + (im ? '\u26A0' : '\u25CB') + ' ' + escapeHtml(play.invalidation.label || play.invalidation.type);
        if (play.invalidation.evidence) html += ' \u2014 ' + escapeHtml(play.invalidation.evidence);
        html += '</span></div>';
    }
    if (play.action_plan) html += '<div class="play-action-plan" style="margin-top:8px">' + escapeHtml(play.action_plan) + '</div>';
    if (play.resolution) html += '<div class="play-thesis" style="margin-top:8px"><strong>Resolution:</strong> ' + escapeHtml(play.resolution) + '</div>';
    html += '</div>';
    content.innerHTML = html;
    var back = content.querySelector('[data-play-back]');
    if (back) back.addEventListener('click', function() { _renderMobilePlaysTab(content); });
}

// ── Trade tab (mobile): Positions + Plays sub-tabs ──
// Reuses the existing Positions/Plays renderers into a shared body so the bottom
// bar can collapse Pos + Plays (+ the Log-a-Trade path inside Positions) into one.
function _renderMobileTradeTab(content) {
    content.innerHTML =
        '<div class="mobile-trade-tabs">' +
            '<button class="mobile-trade-tab active" data-trade-tab="position">Positions</button>' +
            '<button class="mobile-trade-tab" data-trade-tab="plays">Plays</button>' +
        '</div>' +
        '<div class="mobile-trade-body" id="mobile-trade-body"></div>';
    var body = document.getElementById('mobile-trade-body');
    if (body) _renderMobilePositionTab(body);
    content.querySelectorAll('.mobile-trade-tab').forEach(function(tab) {
        tab.addEventListener('click', function() {
            content.querySelectorAll('.mobile-trade-tab').forEach(function(t) { t.classList.remove('active'); });
            this.classList.add('active');
            var b = document.getElementById('mobile-trade-body');
            if (!b) return;
            if (this.getAttribute('data-trade-tab') === 'plays') _renderMobilePlaysTab(b);
            else _renderMobilePositionTab(b);
        });
    });
}

// ── More menu ──
function _renderMobileMoreTab(content) {
    // Plays is already in the main tab bar (not duplicated here).
    // Scanner/Stream/Backtest are omitted — not useful on phone. Split Chart and
    // Rotate are separate items.
    // Inline SVGs — lucide.createIcons is unreliable on dynamically injected mobile
    // content (same reason the bottom bar uses inline SVG), which left Widgets/Report
    // iconless. Rotate Favourites/Plays dropped (redundant on mobile — swiping between
    // symbols already cycles); Log Trade now lives in the Trade sheet.
    var items = [
        { svg: '<rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>', label: 'Home Widgets', action: 'widgets' },
        { svg: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/>', label: 'Morning Report', action: 'report' },
        { svg: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>', label: 'Split Chart', action: 'splitChart' },
        { svg: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>', label: 'Settings', action: 'settings' }
    ];
    var html = '<div class="mobile-more-grid">';
    items.forEach(function(item) {
        html += '<button class="mobile-more-item" data-more-action="' + item.action + '">' +
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' + item.svg + '</svg>' +
            '<span>' + item.label + '</span>' +
        '</button>';
    });
    html += '</div>';
    content.innerHTML = html;

    content.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-more-action]');
        if (!btn) return;
        var action = btn.dataset.moreAction;
        closeMobileSheet();
        switch (action) {
            case 'widgets':
                // Home/lock-screen widget setup (tokens + Scriptable/APK) lives in
                // the Settings "Mobile Widget" tab; /widget/setup redirects here too.
                window.location.href = '/settings#widget';
                break;
            case 'report':
                // Morning report opens as its own (responsive) page — no more
                // frame-blocked iframe. Log Trade now lives in the Trade sheet.
                window.location.href = '/report/morning';
                break;
            case 'splitChart':
                setTimeout(function() { openMobileSheet('splitChart'); }, 100);
                break;
            case 'settings':
                window.location.href = '/settings';
                break;
        }
    });
}

// ── Split Chart layout picker (mobile) ──
// Mobile equivalent of the desktop layout selector. Calls the existing `mcSetLayout()`
// which already handles tier gating, pane creation/destruction, and responsive CSS
// (the `@media (max-width: 900px)` rules in main.css auto-stack tri/quad vertically
// in portrait and collapse back on landscape).
function _renderMobileSplitChartTab(content) {
    var layouts = [
        { key: 'single', label: 'Single',      desc: 'One full-screen chart', panes: 1 },
        { key: 'dual',   label: 'Dual',        desc: 'Two charts — split vertically in portrait, horizontally in landscape', panes: 2 },
        { key: 'tri',    label: 'Triple',      desc: 'Three charts — stacked on phones', panes: 3 },
        { key: 'quad',   label: 'Quad',        desc: 'Four charts — 2×2 grid on landscape, stacked in portrait', panes: 4 }
    ];
    var currentLayout = (typeof _mcLayout !== 'undefined') ? _mcLayout : 'single';
    var maxPanes = (typeof _mcTierMaxPanes !== 'undefined') ? _mcTierMaxPanes : 1;

    var html = '<div class="mobile-rotate-sheet">';
    html += '<div class="mobile-sheet-subtitle" style="color:#8b949e;font-size:11px;margin-bottom:10px">Tap a layout to switch. All layouts adapt to your phone\u2019s orientation automatically.</div>';
    layouts.forEach(function(l) {
        var isActive = (l.key === currentLayout);
        var isLocked = (l.panes > maxPanes);
        var cls = 'mobile-rotate-opt';
        if (isActive) cls += ' active';
        if (isLocked) cls += ' locked';
        var lockBadge = isLocked ? ' <span style="color:#f85149;font-size:10px;margin-left:6px">Premium</span>' : '';
        var activeBadge = isActive ? ' <span style="color:#3fb950;font-size:10px;margin-left:6px">Active</span>' : '';
        html += '<button class="' + cls + '" data-layout="' + l.key + '"' + (isLocked ? ' disabled' : '') + '>' +
            '<div style="display:flex;align-items:center;justify-content:space-between">' +
                '<span style="font-weight:600">' + l.label + activeBadge + lockBadge + '</span>' +
            '</div>' +
            '<div style="color:#6e7681;font-size:11px;margin-top:4px">' + l.desc + '</div>' +
        '</button>';
    });
    html += '</div>';
    content.innerHTML = html;

    content.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-layout]');
        if (!btn || btn.disabled) return;
        var layout = btn.dataset.layout;
        closeMobileSheet();
        if (typeof mcSetLayout === 'function') {
            mcSetLayout(layout);
        }
    });
}
