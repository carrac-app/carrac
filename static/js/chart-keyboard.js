// Chart keyboard shortcuts: handleKlineKeydown/handleKlineKeyup, the
// user-rebindable Alt+key drawing-tool map, and the shortcuts help modal.
// Needs app-core.js loaded first; arrow-key panning comes from chart-gestures.js.

function handleKlineKeyup(e) {
    if (e.key === 'ArrowLeft') _navStop('left');
    if (e.key === 'ArrowRight') _navStop('right');
    if (e.key === 'ArrowUp') _navStop('up');
    if (e.key === 'ArrowDown') _navStop('down');
    if (e.key === 's' || e.key === 'S') {
        clearTimeout(_sKeyTimer);
        _sKeyDown = false;
        _sArrowUsed = false;
    }
}

// Customizable keybindings for Alt+key drawing tools
var _DEFAULT_ALT_BINDINGS = {
    'segment': 'KeyQ',
    'parallelChannel': 'KeyW',
    'rect': 'KeyE',
    'fibonacciRetrace': 'KeyR',
    'textAnnotation': 'KeyT',
    'alertLine': 'KeyA',
    'alertLevel': 'KeyL',
    'measureTool': 'KeyD',
    'volumeProfileRange': 'KeyF',
    'freehand': 'KeyV',
    'longPosition': 'KeyB',
    'horizontalStraightLine': 'KeyH'
};
var _ALT_TOOL_LABELS = {
    'segment': 'Trendline', 'parallelChannel': 'Parallel channel', 'rect': 'Rectangle',
    'fibonacciRetrace': 'Fibonacci', 'textAnnotation': 'Text annotation',
    'alertLine': 'Alert line', 'alertLevel': 'Horizontal alert line', 'measureTool': 'Measure / distance',
    'volumeProfileRange': 'VP range (FRVP)', 'freehand': 'Freehand draw',
    'longPosition': 'Long position', 'shortPosition': 'Short position', 'horizontalStraightLine': 'Horizontal line'
};
var _ALT_TOOL_SHORT = {
    'segment': 'Line', 'parallelChannel': 'Chan', 'rect': 'Rect',
    'fibonacciRetrace': 'Fib', 'textAnnotation': 'Text',
    'alertLine': 'Alert', 'alertLevel': 'H.Alert', 'measureTool': 'Meas',
    'volumeProfileRange': 'FRVP', 'freehand': 'Free',
    'longPosition': 'Long', 'shortPosition': 'Short', 'horizontalStraightLine': 'HLine'
};

function _getAltDrawMap() {
    var map = {};
    for (var tool in _DEFAULT_ALT_BINDINGS) map[_DEFAULT_ALT_BINDINGS[tool]] = tool;
    return map;
}

function handleKlineKeydown(e) {
    if (!klineModal || !klineModal.classList.contains('visible')) return;
    var notInput = e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA';

    // Multi-chart layout shortcuts (Ctrl/Cmd + 1/2/3/4)
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && notInput) {
        var _mcMap = { '1': 'single', '2': 'dual', '3': 'stack', '4': 'tri', '5': 'quad' };
        if (_mcMap[e.key] && typeof mcSetLayout === 'function') {
            e.preventDefault();
            mcSetLayout(_mcMap[e.key]);
            return;
        }
        if (e.key === '6' && typeof mcToggleRotation === 'function') {
            e.preventDefault();
            mcToggleRotation('plays');
            return;
        }
        if (e.key === '7' && typeof mcToggleRotation === 'function') {
            e.preventDefault();
            mcToggleRotation('favourites');
            return;
        }
        if (e.key === '8' && typeof mcSpreadTimeframes === 'function') {
            e.preventDefault();
            mcSpreadTimeframes();
            return;
        }
    }
    // Tab — cycle multi-chart panes
    if (e.key === 'Tab' && notInput && typeof mcIsMultiChart === 'function' && mcIsMultiChart()) {
        e.preventDefault();
        mcCyclePanes(e.shiftKey ? -1 : 1);
        return;
    }

    // Alt/Option + key — Drawing tools (customizable, left-hand centric)
    if (e.altKey && notInput && !e.ctrlKey && !e.metaKey) {
        var _altDrawMap = _getAltDrawMap();
        if (_altDrawMap[e.code]) {
            e.preventDefault();
            var _altTool = _altDrawMap[e.code];
            if (_altTool === 'volumeProfileRange') {
                // frvpActivateTool() resolves the '[data-tool="volumeProfileRange"]'
                // element itself (the tool-menu-item, first in DOM order) and
                // calls handleKlineToolClick internally — active-state is correct;
                // we only need to record the group memory + chip label here.
                frvpActivateTool();
                _klineRememberShortcutTool(_altTool);
            } else if (_altTool === 'longPosition') {
                // Alt+B cycles: Long → Short → off (repeated presses). Most tools
                // have no top-level '.tool-btn[data-tool]' element (they
                // live inside the group flyouts as '.tool-menu-item[data-tool]'),
                // so resolve against both that AND a favourited '.fav-tool-btn'.
                var _longActive = klineModal && klineModal.querySelector('.tool-menu-item[data-tool="longPosition"].active, .fav-tool-btn[data-tool="longPosition"].active');
                var _shortActive = klineModal && klineModal.querySelector('.tool-menu-item[data-tool="shortPosition"].active, .fav-tool-btn[data-tool="shortPosition"].active');
                if (_longActive) {
                    // Long armed → arm Short instead
                    _klineFireToolFromShortcut('shortPosition', e);
                } else if (_shortActive) {
                    // Short armed → deactivate (press Escape conceptually — just remove active)
                    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
                } else {
                    // Nothing armed → arm Long
                    _klineFireToolFromShortcut('longPosition', e);
                }
            } else {
                _klineFireToolFromShortcut(_altTool, e);
            }
            return;
        }
    }

    // Shift + number — Timeframes (short → long: 1=15m … 7=1M)
    if (e.shiftKey && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        var _tfCodeMap = { 'Digit1': '15m', 'Digit2': '1h', 'Digit3': '4h', 'Digit4': '1D', 'Digit5': '3D', 'Digit6': '1W', 'Digit7': '1M' };
        if (_tfCodeMap[e.code]) {
            var _tfTarget = _tfCodeMap[e.code];
            var _blockedTfs = ['1m','3m','5m','15m','30m','1h'];
            if (!window._isPremium && _blockedTfs.indexOf(_tfTarget) >= 0) { if (typeof showUpgradeModal === 'function') showUpgradeModal('Lower timeframes (1m–1h) require a subscription'); return; }
            if (['1m','3m','5m','15m','30m'].indexOf(_tfTarget) >= 0 && !isCryptoAsset(klineChartSymbol)) return;
            e.preventDefault();
            switchTimeframe(_tfTarget);
            return;
        }
    }

    // S key — open scanner drawer tab
    if ((e.key === 's' || e.key === 'S') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        toggleDrawer('scanner');
        return;
    }

    // A key - toggle alerts drawer
    if ((e.key === 'a' || e.key === 'A') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        toggleDrawer('alerts');
        return;
    }

    // Shift+D - clear ALL drawings (alias for Shift+Delete)
    if (e.key === 'D' && e.shiftKey && notInput && !e.ctrlKey && !e.metaKey && klineChart) {
        e.preventDefault();
        if (confirm('Clear all drawings for ' + klineChartSymbol + '?')) {
            // Clean up any promoted spot trades before the whole bucket is
            // wiped — otherwise "clear all" orphans every one of them at
            // once, silently.
            if (typeof _cleanupPromotedSpotTradesForSymbol === 'function') {
                _cleanupPromotedSpotTradesForSymbol(klineChartSymbol);
            }
            klineChart.removeOverlay();
            _alertOverlayIds = [];
            clearKlineDrawingsFromStorage();
            var _clearAllFailCount = 0;
            fetch('/api/drawings?asset=' + encodeURIComponent(klineChartSymbol), { credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.drawings) return;
                // Await every DELETE before re-arming the loader — a TF-switch
                // or tab-return re-fetch racing ahead of the deletes pulls the
                // not-yet-deleted rows straight back onto the chart.
                return Promise.all(data.drawings.map(function(d) {
                    return fetch('/api/drawings/' + d.id, { method: 'DELETE', headers: { 'X-CSRFToken': getCSRFToken() }, credentials: 'same-origin' }).catch(function(e) {
                        _clearAllFailCount++;
                        if (typeof window.carracLog === 'function') window.carracLog('drawing', 'clear-all: server delete failed for drawing ' + d.id + ' — it may resurrect via cross-device sync', String((e && e.message) || e));
                    });
                }));
            }).then(function() {
                if (typeof _drawingsLoadedForSymbol !== 'undefined') _drawingsLoadedForSymbol = null;
                if (_clearAllFailCount > 0 && typeof showToast === 'function') {
                    showToast('Cleared locally, but ' + _clearAllFailCount + ' drawing(s) could not be deleted on the server — they may come back');
                }
            }).catch(function(e) {
                if (typeof window.carracLog === 'function') window.carracLog('drawing', 'clear-all drawings failed', String((e && e.message) || e));
                if (typeof showToast === 'function') showToast('Cleared locally, but the server delete failed — drawings may come back');
            });
            renderAlertOverlays();
        }
        return;
    }
    // D key (no shift) — toggle the drawing TOOLBAR (not drawings
    // visibility, which would blink all drawings off on the way). If drawings are hidden, restore them
    // (which also opens the toolbar).
    if ((e.key === 'd') && notInput && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        if (typeof klineDrawingsHidden !== 'undefined' && klineDrawingsHidden) {
            toggleDrawingsVisibility();
            return;
        }
        var _dtContent = document.getElementById('adv-chart-content');
        if (_dtContent && _dtContent.classList.contains('drawing-toolbar-visible')) {
            hideDrawingToolbar();
        } else {
            showDrawingToolbar();
        }
        return;
    }

    // ESC key - close topmost panel first, chart last (priority order)
    if (e.key === 'Escape') {
        // Close formula editor if open
        var feModal = document.getElementById('formula-editor-modal');
        if (feModal && feModal.classList.contains('active')) {
            if (typeof CustomIndicators !== 'undefined') CustomIndicators.closeEditor();
            return;
        }
        // Clear drawing cursor on any ESC
        if (typeof _setDrawingCursor === 'function') _setDrawingCursor(false);
        // Exit fullscreen sub-pane
        if (typeof _fullscreenPaneId !== 'undefined' && _fullscreenPaneId) {
            exitPaneFullscreen();
            // Don't return — let ESC also cancel active drawing tool
        }
        // Cancel freehand drawing mode. _freehandActive is declared in
        // freehand.js — guarded the same way as klineContextMenu above
        // a failed/late load of freehand.js must not
        // throw and kill the rest of this keydown handler (undo/redo, drawer
        // close, timeframe shortcuts, etc. all live below it).
        if (typeof _freehandActive !== 'undefined' && _freehandActive) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            freehandDeactivate();
            return;
        }
        // Cancel an in-progress 2-point drawing (any standard tool, FRVP included)
        if (typeof _cancelPendingDraw === 'function' && _cancelPendingDraw()) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            return;
        }
        // Close bottom drawer
        if (drawerOpen) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            if (typeof closeDrawer === 'function') closeDrawer();
            return;
        }
        var textDialog = document.getElementById('text-annotation-dialog');
        if (textDialog) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            textDialog.remove();
            return;
        }
        var shortcutsModal = document.getElementById('shortcuts-modal');
        if (shortcutsModal) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            shortcutsModal.remove();
            return;
        }
        var searchPopup = document.getElementById('asset-search-popup');
        if (searchPopup && searchPopup.classList.contains('visible')) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            hideAssetSearchPopup();
            return;
        }
        // Close drawer if open on Escape
        if (drawerOpen) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            if (typeof closeDrawer === 'function') closeDrawer();
            return;
        }
        // Close floating AI panel if open (state machine handles cascade)
        if (typeof _aiPanelState !== 'undefined' && _aiPanelState !== 'fab') {
            // Let ai-panel.js global ESC handler deal with it
            return;
        }
        // Close Journal panel (switch back to default screener)
        var _rp = document.getElementById('right-panel');
        if (_rp && _rp.className.indexOf('view-journal') !== -1) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            setViewMode('consensio');
            return;
        }
        var asSwitcher = document.getElementById('asset-switcher-menu');
        if (asSwitcher && asSwitcher.classList.contains('visible')) {
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            asSwitcher.classList.remove('visible');
            return;
        }
        // In split layout, ESC should not destroy the chart panel
        if (!splitLayoutActive) {
            closeAdvancedChartModal();
        }
        return;
    }

    // Shift+Delete - clear ALL drawings for current asset (with confirmation)
    if ((e.key === 'Delete' || e.key === 'Backspace') && e.shiftKey && notInput && klineChart) {
        e.preventDefault();
        if (confirm('Clear all drawings for ' + klineChartSymbol + '?')) {
            // Clean up any promoted spot trades before the whole bucket is
            // wiped — otherwise "clear all" orphans every one of them at
            // once, silently.
            if (typeof _cleanupPromotedSpotTradesForSymbol === 'function') {
                _cleanupPromotedSpotTradesForSymbol(klineChartSymbol);
            }
            klineChart.removeOverlay();
            _alertOverlayIds = [];
            clearKlineDrawingsFromStorage();
            // Also delete all from server for this asset
            var _clearAllFailCount2 = 0;
            fetch('/api/drawings?asset=' + encodeURIComponent(klineChartSymbol), { credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.drawings) return;
                // Await every DELETE before re-arming the loader — a TF-switch
                // or tab-return re-fetch racing ahead of the deletes pulls the
                // not-yet-deleted rows straight back onto the chart.
                return Promise.all(data.drawings.map(function(d) {
                    return fetch('/api/drawings/' + d.id, { method: 'DELETE', headers: { 'X-CSRFToken': getCSRFToken() }, credentials: 'same-origin' }).catch(function(e) {
                        _clearAllFailCount2++;
                        if (typeof window.carracLog === 'function') window.carracLog('drawing', 'clear-all: server delete failed for drawing ' + d.id + ' — it may resurrect via cross-device sync', String((e && e.message) || e));
                    });
                }));
            }).then(function() {
                if (typeof _drawingsLoadedForSymbol !== 'undefined') _drawingsLoadedForSymbol = null;
                if (_clearAllFailCount2 > 0 && typeof showToast === 'function') {
                    showToast('Cleared locally, but ' + _clearAllFailCount2 + ' drawing(s) could not be deleted on the server — they may come back');
                }
            }).catch(function(e) {
                if (typeof window.carracLog === 'function') window.carracLog('drawing', 'clear-all drawings failed', String((e && e.message) || e));
                if (typeof showToast === 'function') showToast('Cleared locally, but the server delete failed — drawings may come back');
            });
            renderAlertOverlays();
        }
        return;
    }

    // Delete/Backspace - remove the selected drawing (FRVP included — it is
    // a standard overlay now, selected via klineSelectedOverlayId)
    if ((e.key === 'Delete' || e.key === 'Backspace') && notInput) {
        if (klineSelectedOverlayId && klineChart) {
            e.preventDefault();
            var _delDrawing = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(klineSelectedOverlayId) : null;
            if (_delDrawing && _delDrawing.extendData && _delDrawing.extendData._locked) {
                showToast('Locked — right-click → Unlock to delete');
                return;
            }
            // Route through the context-menu delete — the ONE implementation
            // that also cleans up a linked server alert (_alertId) + refreshes
            // the drawer. A duplicate path would leave orphaned
            // alerts behind when deleting alert lines with Del.
            if (typeof handleContextMenuAction === 'function') {
                handleContextMenuAction('delete', klineSelectedOverlayId);
                klineSelectedOverlayId = null;
            } else {
                klineChart.removeOverlay(klineSelectedOverlayId);
                removeKlineDrawingFromStorage(klineSelectedOverlayId);
                klineSelectedOverlayId = null;
            }
        } else if (window._lastLockedClickTs && Date.now() - window._lastLockedClickTs < 4000) {
            // Locked overlays deselect on click ("clicks pass through"), so by
            // the time Delete arrives nothing is selected — explain instead of
            // silently doing nothing.
            e.preventDefault();
            window._lastLockedClickTs = 0;
            showToast('Drawing is locked — right-click it → Unlock to delete');
        }
        return;
    }

    // Ctrl+Z / Cmd+Z = undo, Ctrl+Y / Cmd+Y / Ctrl+Shift+Z = redo
    // Ctrl+C / Cmd+C = copy drawing, Ctrl+V / Cmd+V = paste drawing
    if ((e.ctrlKey || e.metaKey) && notInput) {
        if (e.key === 'z' && !e.shiftKey) {
            e.preventDefault();
            handleKlineToolClick('undo', null, [], e);
            return;
        }
        if (e.key === 'y' || (e.key === 'z' && e.shiftKey) || (e.key === 'Z' && e.shiftKey)) {
            e.preventDefault();
            handleKlineToolClick('redo', null, [], e);
            return;
        }
        if (e.key === 'c' && !e.shiftKey) {
            if (klineSelectedOverlayId && typeof copySelectedDrawing === 'function') {
                e.preventDefault();
                copySelectedDrawing();
            }
            return;
        }
        if (e.key === 'v' && !e.shiftKey) {
            if (typeof _drawingClipboard !== 'undefined' && _drawingClipboard && typeof pasteDrawing === 'function') {
                e.preventDefault();
                pasteDrawing();
            }
            return;
        }
    }

    // Alt+I - invert price scale (TradingView parity; bare I = indicators)
    if ((e.key === 'i' || e.key === 'I' || e.code === 'KeyI') && e.altKey && !e.ctrlKey && !e.metaKey && notInput) {
        e.preventDefault();
        toggleInvertScale();
        return;
    }

    // I key - toggle all indicators
    if ((e.key === 'i' || e.key === 'I') && notInput && !e.altKey) {
        e.preventDefault();
        toggleAllIndicators();
        return;
    }

    // H key - hide/show all drawings (logic lives in lazily-loaded kline-drawings.js)
    if ((e.key === 'h' || e.key === 'H') && notInput && !e.altKey && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        ensureKLineDrawings().then(function() {
            if (typeof toggleAllDrawingsVisibility === 'function') toggleAllDrawingsVisibility();
        });
        return;
    }

    // P key - open Plays panel
    if ((e.key === 'p' || e.key === 'P') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        if (typeof setViewMode === 'function') {
            var current = localStorage.getItem('panelViewMode');
            if (current === 'plays' && typeof _panelCollapsed !== 'undefined' && !_panelCollapsed) {
                if (typeof toggleRightPanel === 'function') toggleRightPanel();
            } else {
                if (typeof _panelCollapsed !== 'undefined' && _panelCollapsed && typeof toggleRightPanel === 'function') toggleRightPanel();
                setViewMode('plays');
            }
        }
        return;
    }

    // V key - toggle Volume Profile (visible range)
    if ((e.key === 'v') && notInput && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        toggleKlineIndicator('VP');
        return;
    }

    // Space key - open asset search popup
    if (e.key === ' ' && notInput) {
        e.preventDefault();
        showAssetSearchPopup();
        return;
    }

    // , / . keys - previous / next asset
    if (e.key === ',' && notInput) { e.preventDefault(); switchAsset(-1); return; }
    if (e.key === '.' && notInput) { e.preventDefault(); switchAsset(1); return; }

    // ? key - show shortcuts modal
    if (e.key === '?' && notInput) {
        e.preventDefault();
        showShortcutsModal();
        return;
    }

    // G key - reset chart view (default zoom + scroll to latest + reset Y-axis)
    if ((e.key === 'g' || e.key === 'G') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        if (klineChart) {
            _yGapOffset = 0;
            klineChart.setPaneOptions({ id: 'candle_pane', gap: { top: 0.08, bottom: 0.08 } });
            klineChart.setBarSpace(8);
            _scrollToLastReal();
            resetYAxisScale();
        }
        return;
    }

    // J key - center current price on both axes
    if ((e.key === 'j' || e.key === 'J') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        if (klineChart) {
            _yGapOffset = 0;
            klineChart.setPaneOptions({ id: 'candle_pane', gap: { top: 0.08, bottom: 0.08 } });
            // Push latest candle to horizontal center, then scroll back past phantoms
            var ctr = document.getElementById('kline-chart-container');
            if (ctr) {
                var halfW = Math.round(ctr.getBoundingClientRect().width / 2);
                try { klineChart.setOffsetRightDistance(halfW); } catch(e2) {}
            }
            klineChart.scrollToRealTime();
            var _dl = klineChart.getDataList();
            if (_dl && _dl.length) {
                var _ji = _dl.length - 1;
                var _jNow = Date.now();
                while (_ji > 0 && _dl[_ji].timestamp > _jNow) _ji--;
                var _jPhantom = _dl.length - 1 - _ji;
                if (_jPhantom > 0) {
                    var _jbs = klineChart.getBarSpace ? klineChart.getBarSpace() : 8;
                    klineChart.scrollByDistance(_jPhantom * _jbs);
                }
            }
            resetYAxisScale();
        }
        return;
    }

    // O key - toggle logarithmic scale
    if ((e.key === 'o' || e.key === 'O') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        toggleLogScale();
        return;
    }

    // C key - cycle color on selected drawing
    if ((e.key === 'c' || e.key === 'C') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        cycleSelectedDrawingColor();
        return;
    }

    // + / - keys - thickness when drawing selected, zoom when not
    if ((e.key === '+' || e.key === '=' || e.code === 'Equal' || e.code === 'NumpadAdd') && notInput && klineSelectedOverlayId) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        changeSelectedDrawingWidth(1);
        return;
    }
    if ((e.key === '-' || e.key === '_' || e.code === 'Minus' || e.code === 'NumpadSubtract') && notInput && klineSelectedOverlayId) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        changeSelectedDrawingWidth(-1);
        return;
    }

    // + / - keys - zoom when no drawing selected
    if ((e.key === '+' || e.key === '=' || e.code === 'Equal' || e.code === 'NumpadAdd') && notInput && !klineSelectedOverlayId) {
        e.preventDefault();
        if (klineChart) klineChart.zoomAtCoordinate(1.15);
        return;
    }
    if ((e.key === '-' || e.key === '_' || e.code === 'Minus' || e.code === 'NumpadSubtract') && notInput && !klineSelectedOverlayId) {
        e.preventDefault();
        if (klineChart) klineChart.zoomAtCoordinate(0.85);
        return;
    }

    // [ / ] keys - change opacity on selected drawing
    if ((e.key === '[' || e.code === 'BracketLeft') && notInput && klineSelectedOverlayId) {
        e.preventDefault();
        changeSelectedDrawingOpacity(-0.1);
        return;
    }
    if ((e.key === ']' || e.code === 'BracketRight') && notInput && klineSelectedOverlayId) {
        e.preventDefault();
        changeSelectedDrawingOpacity(0.1);
        return;
    }

    // T key - add intermediate TP to selected position (bare key — drawing tools moved to Alt)
    if ((e.key === 't' || e.key === 'T') && notInput && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        if (klineSelectedOverlayId && klineChart) {
            var selOverlays = klineChart.getOverlayById(klineSelectedOverlayId);
            var selOv = Array.isArray(selOverlays) ? selOverlays[0] : selOverlays;
            if (selOv && (selOv.name === 'position' || selOv.name === 'longPosition' || selOv.name === 'shortPosition') && selOv.points && selOv.points.length >= 3) {
                e.preventDefault();
                enterAddTPMode(selOv);
                return;
            }
        }
    }

    // Q - cycle bottom drawer tabs (Alerts → Position → Scanner → Filter), collapse if last tab open
    // Allow Q even from drawer inputs (alert price, etc.) so cycling always works
    var _qFromDrawer = !notInput && e.target.closest && e.target.closest('.chart-drawer');
    if ((e.key === 'q' || e.key === 'Q') && (notInput || _qFromDrawer) && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        if (_qFromDrawer) e.target.blur();
        var _drawerTabs = ['position', 'alerts', 'scanner', 'stream', 'backtest'];
        if (typeof drawerOpen !== 'undefined' && drawerOpen) {
            var curIdx = _drawerTabs.indexOf(drawerActiveTab);
            if (curIdx >= 0 && curIdx < _drawerTabs.length - 1) {
                openDrawer(_drawerTabs[curIdx + 1]);
            } else {
                if (typeof closeDrawer === 'function') closeDrawer();
            }
        } else {
            openDrawer(_drawerTabs[0]);
        }
        return;
    }

    // N - open/toggle news panel
    if ((e.key === 'n' || e.key === 'N') && notInput && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        if (typeof setViewMode === 'function') {
            var current = localStorage.getItem('panelViewMode');
            if (current === 'news' && typeof _panelCollapsed !== 'undefined' && !_panelCollapsed) {
                if (typeof toggleRightPanel === 'function') toggleRightPanel();
            } else {
                if (typeof _panelCollapsed !== 'undefined' && _panelCollapsed && typeof toggleRightPanel === 'function') toggleRightPanel();
                setViewMode('news');
            }
        }
        return;
    }

    // U - open custom indicator editor
    if ((e.key === 'u' || e.key === 'U') && notInput && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        if (typeof CustomIndicators !== 'undefined') CustomIndicators.openEditor();
        return;
    }

    // / - open AI panel (lazy-loads ai-panel.js on first use)
    if (e.key === '/' && notInput && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        var _lastAI2 = 'small';
        try { _lastAI2 = localStorage.getItem('aiPanelLastOpen') || 'small'; } catch(ex) {}
        ensureAIPanel().then(function() {
            if (typeof setAIPanelState === 'function') {
                setAIPanelState(_lastAI2);
                setTimeout(function() {
                    var ta = document.getElementById('ai-panel-textarea');
                    if (ta) ta.focus();
                }, 200);
            }
        });
        return;
    }

    // E / Shift+E - cycle sidebar view modes
    if ((e.key === 'e' || e.key === 'E') && notInput && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        if (typeof cycleViewMode === 'function') cycleViewMode(e.shiftKey ? -1 : 1);
        return;
    }

    // Arrow keys - pan in all directions (grab-and-drag feel)
    if (e.key === 'ArrowLeft' && notInput) { e.preventDefault(); _navStart('left'); return; }
    if (e.key === 'ArrowRight' && notInput) { e.preventDefault(); _navStart('right'); return; }
    if (e.key === 'ArrowUp' && notInput) { e.preventDefault(); _navStart('up'); return; }
    if (e.key === 'ArrowDown' && notInput) { e.preventDefault(); _navStart('down'); return; }
}

function showShortcutsModal() {
    var existing = document.getElementById('shortcuts-modal');
    if (existing) { existing.remove(); return; }

    var isMac = /Mac|iPod|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    var ctl = isMac ? '\u2318' : 'Ctrl';
    var alt = isMac ? '\u2325 Opt' : 'Alt';
    var altS = isMac ? '\u2325' : 'Alt';
    var cAlt = '#4fc3f7', cShift = '#ffb74d', cCtrl = '#ce93d8';

    // Build key maps from default bindings
    var keyToTool = {};
    for (var tool in _DEFAULT_ALT_BINDINGS) {
        var letter = _DEFAULT_ALT_BINDINGS[tool].replace('Key', '');
        keyToTool[letter] = _ALT_TOOL_SHORT[tool] || tool;
    }
    var toolToKey = {};
    for (var t in _DEFAULT_ALT_BINDINGS) toolToKey[t] = _DEFAULT_ALT_BINDINGS[t].replace('Key', '');

    // Visual keyboard key builder
    function kk(label, sub, color, w) {
        var width = w || 36;
        var bg, bdr;
        if (color === cAlt) { bg = 'rgba(79,195,247,0.12)'; bdr = cAlt; }
        else if (color === cShift) { bg = 'rgba(255,183,77,0.12)'; bdr = cShift; }
        else if (color === cCtrl) { bg = 'rgba(206,147,216,0.12)'; bdr = cCtrl; }
        else { bg = '#2a2e39'; bdr = '#363a45'; }
        var tc = color || '#d1d4dc';
        var sc = color || '#787b86';
        return '<div style="width:'+width+'px;height:42px;background:'+bg+';border:1px solid '+bdr+';border-radius:1px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px">' +
            '<span style="font-size:11px;font-weight:600;color:'+tc+'">'+label+'</span>' +
            (sub ? '<span style="font-size:8px;color:'+sc+';opacity:0.85">'+sub+'</span>' : '') +
            '</div>';
    }
    // Dual-function key (Shift + Ctrl on same number key)
    function kkDual(label, sub1, sub2) {
        return '<div style="width:36px;height:48px;background:linear-gradient(180deg,rgba(255,183,77,0.10) 0%,rgba(206,147,216,0.10) 100%);border:1px solid #555;border-radius:1px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0">' +
            '<span style="font-size:10px;font-weight:600;color:#d1d4dc">'+label+'</span>' +
            '<span style="font-size:7px;color:'+cShift+'">'+sub1+'</span>' +
            '<span style="font-size:7px;color:'+cCtrl+'">'+sub2+'</span>' +
            '</div>';
    }
    // Dynamic Alt key — reads from current bindings
    function kkAlt(letter) {
        var sub = keyToTool[letter];
        return kk(letter, sub || '', sub ? cAlt : null);
    }

    // Build keyboard rows
    var numRow = kkDual('1','15m','1ch') + kkDual('2','1H','2ch') + kkDual('3','4H','stk') +
                 kkDual('4','1D','tri') + kkDual('5','3D','2\u00d72') +
                 kkDual('6','1W','rotS') + kkDual('7','1M','rotF') + kk('8','TFsp',cCtrl);
    var qRow = kkAlt('Q') + kkAlt('W') + kkAlt('E') + kkAlt('R') + kkAlt('T');
    var aRow = kkAlt('A') + kkAlt('S') + kkAlt('D') + kkAlt('F') + kkAlt('G') + kkAlt('H');
    var zRow = kkAlt('Z') + kkAlt('X') + kkAlt('C') + kkAlt('V') + kkAlt('B');

    var keyboard =
        '<div style="display:flex;flex-direction:column;gap:3px;padding:14px 16px;background:#181c24;border-radius:0;border:1px solid #2a2e39">' +
            '<div style="display:flex;gap:3px">' + numRow + '</div>' +
            '<div style="display:flex;gap:3px;margin-left:12px">' + qRow + '</div>' +
            '<div style="display:flex;gap:3px;margin-left:20px">' + aRow + '</div>' +
            '<div style="display:flex;gap:3px;margin-left:32px">' + zRow + '</div>' +
            '<div style="display:flex;gap:8px;align-items:center;margin-left:32px;margin-top:3px">' +
                kk('Space','Search',null,140) +
                '<span style="font-size:9px;color:#555;margin-left:4px">\u2190 \u2191 \u2193 \u2192 pan &nbsp; ? help</span>' +
            '</div>' +
        '</div>';

    var legend =
        '<div style="display:flex;gap:14px;flex-wrap:wrap;margin:8px 0 4px">' +
            '<span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:'+cAlt+'"></span><span style="color:'+cAlt+';font-size:11px">'+alt+' = Draw</span></span>' +
            '<span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:'+cShift+'"></span><span style="color:'+cShift+';font-size:11px">Shift = Time</span></span>' +
            '<span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:'+cCtrl+'"></span><span style="color:'+cCtrl+';font-size:11px">'+ctl+' = Workspace</span></span>' +
            '<span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:#555"></span><span style="color:#787b86;font-size:11px">Bare = Panels</span></span>' +
        '</div>';

    function sr(keys, desc) {
        var ks = keys.split(' ').map(function(k) {
            return '<span style="background:#2a2e39;border:1px solid #363a45;border-radius:1px;padding:1px 6px;font-size:10px;min-width:16px;text-align:center;display:inline-block">'+k+'</span>';
        }).join(' ');
        return '<div style="display:flex;align-items:center;justify-content:space-between;padding:2px 0">' +
            '<span style="color:#787b86;font-size:11px">'+desc+'</span>' +
            '<span style="display:flex;gap:3px">'+ks+'</span></div>';
    }
    function sec(title, color) {
        return '<div style="margin-top:10px;margin-bottom:3px;font-size:10px;font-weight:600;color:'+color+';text-transform:uppercase;letter-spacing:0.5px">'+title+'</div>';
    }

    // Build Alt tool list dynamically from current bindings
    var toolOrder = ['segment','parallelChannel','rect','fibonacciRetrace','textAnnotation',
                     'horizontalStraightLine','alertLine','measureTool','volumeProfileRange','freehand','longPosition'];
    var altRows = '';
    toolOrder.forEach(function(t) {
        altRows += sr(altS+'+'+toolToKey[t], _ALT_TOOL_LABELS[t]);
    });

    var colL =
        sec(altS + ' + Key \u2014 Drawing Tools', cAlt) +
        altRows +
        sec('Shift + Number \u2014 Timeframes', cShift) +
        sr('\u21e7+1', '15 min') + sr('\u21e7+2', '1 hour') + sr('\u21e7+3', '4 hour') +
        sr('\u21e7+4', '1 day') + sr('\u21e7+5', '3 day') + sr('\u21e7+6', '1 week') + sr('\u21e7+7', '1 month');

    var colR =
        sec('No Modifier \u2014 Panels & Navigation', '#787b86') +
        sr('Space', 'Search assets') +
        sr(', .', 'Prev / next favourite') +
        sr('A', 'Alerts drawer') +
        sr('S', 'Scanner drawer') +
        sr('P', 'Plays panel') +
        sr('D', 'Drawing toolbar') +
        sr('I', 'Toggle indicators') +
        sr('H', 'Hide / show drawings') +
        sr('V', 'Volume Profile') +
        sr('O', 'Log / linear scale') +
        sr('Alt+I', 'Invert price scale') +
        sr('Q', 'Cycle drawer tabs') +
        sr('E', 'Cycle sidebar views') +
        sr('N', 'News panel') +
        sr('U', 'Custom indicator editor') +
        sr('G', 'Reset view') +
        sr('J', 'Center price') +
        sr('T', 'Add TP to position') +
        sr('C', 'Cycle drawing color') +
        sr('+ -', 'Thickness / zoom') +
        sr('[ ]', 'Opacity') +
        sr('\u2190\u2192\u2191\u2193', 'Nudge selected drawing (\u21e7 \u00d710)') +
        sr(ctl+'+Click', 'Multi-select drawings') +
        sr(ctl+'+Drag', 'Clone rect / channel') +
        sr('Del', 'Delete selected / set') +
        sr('\u21e7+D', 'Clear ALL drawings') +
        sr('Esc', 'Close / back') +
        sec(ctl + ' \u2014 Workspace & Layouts', cCtrl) +
        sr(ctl+'+1', 'Single chart') +
        sr(ctl+'+2', 'Side by side') +
        sr(ctl+'+3', 'Top / bottom') +
        sr(ctl+'+4', '1 + 2 stack') +
        sr(ctl+'+5', '2\u00d72 grid') +
        sr(ctl+'+6', 'Rotate plays') +
        sr(ctl+'+7', 'Rotate favourites') +
        sr(ctl+'+8', 'TF spread') +
        sr(ctl+'+C', 'Copy drawing') +
        sr(ctl+'+V', 'Paste drawing') +
        sr(ctl+'+Z', 'Undo') +
        sr(ctl+'+Y', 'Redo') +
        sr('Tab', 'Next pane') +
        sr('\u21e7+Tab', 'Prev pane') +
        '<div style="height:6px"></div>' +
        sr('?', 'This help');

    var modal = document.createElement('div');
    modal.id = 'shortcuts-modal';
    modal.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:100000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)';
    modal.innerHTML =
        '<div style="background:#1e222d;border:1px solid #484f58;border-radius:0;padding:20px 24px;max-width:840px;width:94%;max-height:88vh;overflow-y:auto;font-family:JetBrains Mono,monospace;font-size:12px;color:#d1d4dc;box-shadow:6px 6px 0 #000">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
                '<span style="font-size:15px;font-weight:600">Keyboard Shortcuts</span>' +
                '<button style="background:none;border:none;color:#787b86;cursor:pointer;font-size:18px">&times;</button>' +
            '</div>' +
            '<div style="color:#787b86;font-size:11px;margin-bottom:10px;line-height:1.5">' +
                '<span style="color:#d1d4dc">Pro layout</span> \u2014 left hand on keyboard, right hand on mouse, working in harmony.<br>' +
                'One modifier = one purpose: ' +
                '<span style="color:'+cAlt+'">'+alt+'</span> draws, ' +
                '<span style="color:'+cShift+'">Shift</span> shifts time, ' +
                '<span style="color:'+cCtrl+'">'+ctl+'</span> controls workspace.' +
            '</div>' +
            legend +
            keyboard +
            '<div style="display:flex;gap:20px;margin-top:14px">' +
                '<div style="flex:1;display:flex;flex-direction:column;gap:2px">' + colL + '</div>' +
                '<div style="width:1px;background:#2a2e39;flex-shrink:0"></div>' +
                '<div style="flex:1;display:flex;flex-direction:column;gap:2px">' + colR + '</div>' +
            '</div>' +
            '<div style="margin-top:12px;padding-top:10px;border-top:1px solid #2a2e39;color:#787b86;font-size:11px;text-align:center">' +
                'Column and badge meanings live in the <span id="shortcuts-help-link" style="color:#4fc3f7;cursor:pointer;text-decoration:underline">Help</span> modal.' +
            '</div>' +
        '</div>';
    var shortcutsCloseBtn = modal.querySelector('button');
    if (shortcutsCloseBtn) shortcutsCloseBtn.addEventListener('click', function(e) { e.currentTarget.closest('#shortcuts-modal').remove(); });
    var shortcutsHelpLink = modal.querySelector('#shortcuts-help-link');
    if (shortcutsHelpLink) shortcutsHelpLink.addEventListener('click', function() { modal.remove(); openHelpModal(); });
    modal.addEventListener('click', function(e) { if (e.target === modal) modal.remove(); });
    document.body.appendChild(modal);
}
