/**
 * Drawing Tools - Operation-Level Sync Module
 *
 * Per-drawing CRUD sync (no bulk replace). Each draw/edit/delete syncs
 * individually to the server. Polling detects remote changes.
 */

(function() {
    'use strict';

    // Configuration
    const API_BASE = '/api';
    const POLL_FAST_MS = 5000;       // Normal polling interval
    const POLL_SLOW_MS = 15000;      // Backoff after inactivity
    const POLL_IDLE_MS = 30000;      // Deep backoff after prolonged inactivity
    const BACKOFF_THRESHOLD = 6;     // Consecutive unchanged polls before slowing (30s)
    const IDLE_THRESHOLD = 18;       // Consecutive unchanged polls before deep idle (90s+)
    const SYNC_DEBOUNCE_MS = 500;

    // Per-tab session ID (used to filter out own changes in poll)
    const SYNC_SESSION_ID = 'tab_' + Math.random().toString(36).slice(2, 10) + '_' + Date.now();

    // State
    let _lastSyncTs = 0;
    let _pollTimer = null;
    let _syncQueue = {};  // local_id -> {drawing, asset, timer}
    let _unchangedCount = 0;         // Consecutive polls with changed=false
    let _currentPollMs = POLL_FAST_MS;
    let _recentlyDeleted = {};  // local_id -> expiry timestamp (prevents refresh re-adding)
    let _sessionExpiredHandled = false;
    let _lastSyncFailToastTs = 0;         // rate-limits the sync-failure toast (max 1 per 30s)
    const SYNC_FAIL_TOAST_COOLDOWN_MS = 30000;

    // Non-401 sync failures are otherwise console-only (stripped in production by
    // terser drop_console) — surface a rate-limited toast so a flaky
    // connection is visible instead of silently losing drawings.
    function _warnSyncFailure(message) {
        var now = Date.now();
        if (now - _lastSyncFailToastTs < SYNC_FAIL_TOAST_COOLDOWN_MS) return;
        _lastSyncFailToastTs = now;
        try { if (typeof showToast === 'function') showToast(message, 4000); } catch(e) {}
    }

    // Session-expiry handler: fires once per expiry, surfaces a sticky banner
    // and redirects to /login so silent logouts don't swallow drawings.
    function _handleAuthError(status) {
        if (status !== 401 || _sessionExpiredHandled) return true;
        _sessionExpiredHandled = true;

        // Stop polling so we don't keep hammering with a dead session
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }

        var existing = document.getElementById('session-expired-banner');
        if (!existing) {
            var el = document.createElement('div');
            el.id = 'session-expired-banner';
            el.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#b62324;color:#fff;padding:10px 18px;font:600 13px JetBrains Mono,monospace;text-align:center;z-index:99999;box-shadow:0 2px 8px rgba(0,0,0,0.4)';
            el.innerHTML = 'Session expired — your recent changes were not saved. ' +
                '<a href="/login" style="color:#fff;text-decoration:underline;margin-left:8px">Log in again</a>';
            document.body.appendChild(el);
        }
        try { if (typeof showToast === 'function') showToast('Session expired — log in again to save changes', 5000); } catch(e) {}
        // Give the user a moment to read, then redirect
        setTimeout(function() {
            try { window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search); } catch(e) {}
        }, 4000);
        return true;
    }

    // ==================== Map Drawing Types ====================

    function mapDrawingType(klineName) {
        const typeMap = {
            'horizontalStraightLine': 'horizontal_line',
            'horizontalRayLine': 'horizontal_line',
            'horizontalSegment': 'horizontal_line',
            'priceLine': 'horizontal_line',
            'straightLine': 'trendline',
            'rayLine': 'trendline',
            'segment': 'trendline',
            'parallelStraightLine': 'zone',
            'parallelChannel': 'zone',
            'priceChannelLine': 'zone',
            'boundedChannel': 'zone',
            'fibonacciRetrace': 'fibonacci',
            'fibonacciInfinite': 'fibonacci',
            'fibonacciLine': 'fibonacci',
            'fibonacciSegment': 'fibonacci',
            'fibonacciCircle': 'fibonacci',
            'fibonacciSpiral': 'fibonacci',
            'fibonacciSpeedResistanceFan': 'fibonacci',
            'fibonacciExtension': 'fibonacci',
            'gannBox': 'zone',
            'rect': 'zone',
            'frvp': 'zone',
            'simpleAnnotation': 'annotation',
            'simpleTag': 'annotation',
            'textAnnotation': 'annotation',
            'longPosition': 'long_position',
            'shortPosition': 'short_position',
            'position': 'position',
            'spot': 'spot',
            'scenarioPath': 'scenario',
            'arrow': 'annotation',
            'pitchfork': 'zone',
            'elliottWave': 'annotation',
            'anchoredVwap': 'annotation',
            'alertLine': 'annotation',
            // Fixed v1 annotation glyph set — all stamp-style, so
            // they fall in the same 'annotation' bucket as arrow/alertLine/
            // anchoredVwap above (no dedicated server-side drawing_type; the
            // fallback below would already map them here even if this block
            // were skipped, but list them explicitly for clarity).
            'arrowUpMarker': 'annotation',
            'arrowDownMarker': 'annotation',
            'flagPin': 'annotation',
            'circleMarker': 'annotation',
            'callout': 'annotation',
            'verticalStraightLine': 'annotation',
        };
        return typeMap[klineName] || 'annotation';
    }

    // ==================== Per-Drawing Sync ====================

    /**
     * Sync a single drawing to the server (debounced per drawing ID).
     * Called after every saveKlineDrawing() and on move/resize end.
     * Pass immediate=true to skip debounce (used during overlay recreate
     * to ensure upsert fires before/with the old-ID delete).
     */
    function syncDrawingToServer(drawing, asset, immediate) {
        if (!drawing || !drawing.id || !asset) return;
        // Skip scenarios — managed via /api/drawings directly
        if (drawing.name === 'scenarioPath') return;
        // Skip during remote refresh (prevent sync loop)
        if (window._suppressDrawingSync) return;

        // User is active — reset to fast polling
        _unchangedCount = 0;
        if (_currentPollMs !== POLL_FAST_MS) _adjustPollInterval(POLL_FAST_MS);

        var localId = drawing.id;

        // Debounce per drawing (rapid edits during drag collapse into one sync)
        if (_syncQueue[localId] && _syncQueue[localId].timer) {
            clearTimeout(_syncQueue[localId].timer);
        }

        if (immediate) {
            _doSyncDrawing(drawing, asset);
            delete _syncQueue[localId];
            return;
        }

        _syncQueue[localId] = {
            timer: setTimeout(function() {
                _doSyncDrawing(drawing, asset);
                delete _syncQueue[localId];
            }, SYNC_DEBOUNCE_MS)
        };
    }

    function _doSyncDrawing(drawing, asset) {
        var drawMeta = {
            kline_name: drawing.name,
            points: drawing.points || [],
            color: drawing.color || '#ff0000',
            width: drawing.width || 2,
            extend_data: drawing.extendData || {},
            local_id: drawing.id
        };
        if (drawing.paneId && drawing.paneId !== 'candle_pane') {
            drawMeta.pane_id = drawing.paneId;
        }
        if (drawing.opacity !== undefined) {
            drawMeta.opacity = drawing.opacity;
        }
        // persist z_order inside metadata (avoids DB/Pydantic migration)
        if (drawing.zOrder != null) {
            drawMeta.z_order = drawing.zOrder;
        }

        var body = {
            asset: asset,
            timeframe: 'ALL',
            drawing_type: mapDrawingType(drawing.name),
            price_level: drawing.points && drawing.points[0] ? drawing.points[0].value : null,
            price_level_upper: drawing.points && drawing.points[1] ? drawing.points[1].value : null,
            label: (drawing.extendData && drawing.extendData.text) || drawing.label || '',
            color: drawing.color || '#ff0000',
            line_style: 'solid',
            line_width: drawing.width || 2,
            metadata: drawMeta
        };

        fetch(API_BASE + '/drawings/upsert', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': (typeof getCSRFToken === 'function') ? getCSRFToken() : '',
                'X-Sync-Session': SYNC_SESSION_ID
            },
            credentials: 'same-origin',
            body: JSON.stringify(body)
        })
        .then(function(r) {
            if (!r.ok) {
                if (_handleAuthError(r.status)) return null;
                console.warn('[Sync] Upsert failed:', r.status);
                _warnSyncFailure('Drawing sync failed — your drawing may not be saved. Check your connection.');
            }
            return r.json();
        })
        .catch(function(e) {
            console.error('[Sync] Upsert error:', e);
            _warnSyncFailure('Drawing sync failed — your drawing may not be saved. Check your connection.');
        });
    }

    /**
     * Notify server of a drawing deletion (adds session header).
     * The actual DELETE call is in kline-drawings.js removeKlineDrawingFromStorage.
     * This is called as a supplement to ensure the header is set.
     */
    function deleteDrawingFromServer(drawingId, asset) {
        if (!drawingId || !asset) return;
        // Cancel any pending debounced UPSERT for this drawing — otherwise the
        // stale UPSERT fires 500ms after the DELETE and re-creates the drawing
        if (_syncQueue[drawingId] && _syncQueue[drawingId].timer) {
            clearTimeout(_syncQueue[drawingId].timer);
            delete _syncQueue[drawingId];
        }
        // Mark as recently deleted so refreshDrawingsFromServer won't re-add it
        _recentlyDeleted[drawingId] = Date.now() + 15000;  // 15s buffer
        fetch(API_BASE + '/drawings/by-local-id', {
            method: 'DELETE',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': (typeof getCSRFToken === 'function') ? getCSRFToken() : '',
                'X-Sync-Session': SYNC_SESSION_ID
            },
            credentials: 'same-origin',
            body: JSON.stringify({ asset: asset, local_id: drawingId })
        })
        .then(function(r) {
            if (!r.ok) {
                if (_handleAuthError(r.status)) return {};
                console.warn('[Sync][DELETE] Failed:', r.status);
                _warnSyncFailure('Drawing sync failed — your drawing may not be saved. Check your connection.');
            }
            return r.json().catch(function() { return {}; });
        })
        .catch(function(e) {
            console.error('[Sync][DELETE] Error:', e);
            _warnSyncFailure('Drawing sync failed — your drawing may not be saved. Check your connection.');
        });
    }

    // ==================== Remote Change Detection ====================

    /**
     * Poll server for remote changes every POLL_INTERVAL_MS.
     * Compares timestamp from Redis. If changed by a different tab,
     * triggers refreshDrawingsFromServer().
     */
    function checkForRemoteChanges() {
        var prefsSince = (window.PreferenceSync && PreferenceSync.getPrefsSyncTs) ? PreferenceSync.getPrefsSyncTs() : 0;
        var url = API_BASE + '/sync/poll?since=' + _lastSyncTs + '&pref_since=' + prefsSince + '&sid=' + encodeURIComponent(SYNC_SESSION_ID);
        fetch(url, { credentials: 'same-origin' })
        .then(function(r) {
            if (!r.ok) {
                if (_handleAuthError(r.status)) return null;
                return null;
            }
            return r.json();
        })
        .then(function(data) {
            if (!data) return;
            if (data.changed) {
                _unchangedCount = 0;
                _adjustPollInterval(POLL_FAST_MS);
                refreshDrawingsFromServer(data.assets || [], data.ts);
            } else {
                _unchangedCount++;
                if (_unchangedCount >= IDLE_THRESHOLD) {
                    _adjustPollInterval(POLL_IDLE_MS);
                } else if (_unchangedCount >= BACKOFF_THRESHOLD) {
                    _adjustPollInterval(POLL_SLOW_MS);
                }
                if (data.ts && data.ts > _lastSyncTs) {
                    _lastSyncTs = data.ts;
                }
            }
            // Delegate preference changes to PreferenceSync
            if (window.PreferenceSync && PreferenceSync.handlePrefsPollResult) {
                PreferenceSync.handlePrefsPollResult(data);
            }
        })
        .catch(function() { /* silent — network blip */ });
    }

    /**
     * Fetch drawings from server for given assets (or current symbol),
     * replace localStorage for those assets, re-render chart.
     */
    function refreshDrawingsFromServer(changedAssets, newTs) {
        var currentSymbol = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : null;
        if (!currentSymbol) return;

        // Only refresh if current symbol is in the changed assets (or no filter)
        if (changedAssets && changedAssets.length > 0) {
            var symbolUpper = currentSymbol.toUpperCase();
            var match = changedAssets.some(function(a) { return a.toUpperCase() === symbolUpper; });
            if (!match) return;
        }

        fetch(API_BASE + '/drawings?asset=' + encodeURIComponent(currentSymbol), { credentials: 'same-origin' })
        .then(function(r) {
            if (!r.ok) {
                return null;
            }
            return r.json();
        })
        .then(function(data) {
            if (!data || !data.success) return;

            var serverDrawings = data.drawings || [];
            var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            var localDrawings = allDrawings[currentSymbol] || [];

            // Build server drawing list in localStorage format
            var newLocalDrawings = [];
            var serverLocalIds = {};

            // Clean expired entries from recently deleted set
            var now = Date.now();
            Object.keys(_recentlyDeleted).forEach(function(k) {
                if (_recentlyDeleted[k] < now) delete _recentlyDeleted[k];
            });

            serverDrawings.forEach(function(serverDraw) {
                var meta = serverDraw.metadata || {};
                // Skip scenarios — managed separately
                if (serverDraw.drawing_type === 'scenario') return;
                // Same id loadDrawingsFromServer() (kline-drawings.js) and the
                // server-side backfill in save_drawing() give a drawing created
                // through the API, which carries no metadata.local_id.
                var localId = meta.local_id || ('srv_' + serverDraw.id);
                // Skip drawings that were just deleted locally (race condition guard)
                if (_recentlyDeleted[localId]) return;
                serverLocalIds[localId] = true;

                var drawObj = {
                    name: meta.kline_name || 'annotation',
                    id: localId,
                    points: meta.points || [],
                    color: meta.color || serverDraw.color || '#3fb950',
                    width: meta.width || serverDraw.line_width || 1
                };
                if (meta.extend_data && Object.keys(meta.extend_data).length > 0) {
                    drawObj.extendData = meta.extend_data;
                }
                if (meta.pane_id && meta.pane_id !== 'candle_pane') {
                    drawObj.paneId = meta.pane_id;
                }
                if (meta.opacity !== undefined) {
                    drawObj.opacity = meta.opacity;
                }
                if (meta.z_order != null) {
                    drawObj.zOrder = meta.z_order;
                }
                if (typeof migrateDrawingName === 'function') migrateDrawingName(drawObj);
                newLocalDrawings.push(drawObj);
            });

            // Preserve local-only scenario drawings (they're not in server sync)
            localDrawings.forEach(function(d) {
                if (d.name === 'scenarioPath') {
                    newLocalDrawings.push(d);
                }
            });

            // Suppress sync to prevent loop
            window._suppressDrawingSync = true;

            // Update localStorage with server state FIRST (before any chart ops)
            allDrawings[currentSymbol] = newLocalDrawings;
            localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));

            // KLineChart v9.8.12: removeOverlay() only schedules overlay-canvas
            // repaint (level 1). resize() forces updatePane(4) which repaints BOTH
            // main and overlay canvases — clearing ghost pixels without touching
            // data or scroll position.
            if (typeof klineChart !== 'undefined' && klineChart) {
                klineChart.removeOverlay();
                _alertOverlayIds = [];
                klineChart.resize();
            }

            // Re-add user drawings from updated localStorage
            if (typeof loadKlineDrawings === 'function') loadKlineDrawings();

            // Re-add indicator overlays (removed by nuclear cleanup)
            try {
                var cacheKey = currentSymbol + '_' + (typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '1D');
                var cached = (typeof klineDataCache !== 'undefined') ? klineDataCache[cacheKey] : null;
                if (cached && typeof applyKlineIndicators === 'function' && typeof convertToKlineData === 'function') {
                    applyKlineIndicators(cached.data, convertToKlineData(cached.data), true);
                }
            } catch(e) {  }

            // Re-add alert, scenario, and other overlays
            if (typeof renderAlertOverlays === 'function') try { renderAlertOverlays(); } catch(e) {}
            if (typeof loadPlaysForChart === 'function') try { loadPlaysForChart(currentSymbol, typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '1D'); } catch(e) {}

            // Invalidate drawing cache
            if (typeof _drawingsLoadedForSymbol !== 'undefined') {
                _drawingsLoadedForSymbol = currentSymbol;
            }

            // Re-enable sync after a brief delay
            setTimeout(function() { window._suppressDrawingSync = false; }, 500);

            // Advance timestamp only after successful refresh
            if (newTs && newTs > _lastSyncTs) {
                _lastSyncTs = newTs;
            }
        })
        .catch(function(e) {  window._suppressDrawingSync = false; });
    }

    // ==================== Adaptive Polling ====================

    function _adjustPollInterval(newMs) {
        if (newMs === _currentPollMs) return;
        _currentPollMs = newMs;
        if (_pollTimer) {
            clearInterval(_pollTimer);
            _pollTimer = setInterval(checkForRemoteChanges, _currentPollMs);
        }
    }

    // ==================== Initialization ====================

    function init() {
        // Start polling for remote changes
        _pollTimer = setInterval(checkForRemoteChanges, _currentPollMs);

        // Pause polling when tab is hidden, resume + catch up when visible
        document.addEventListener('visibilitychange', function() {
            if (document.visibilityState === 'hidden') {
                if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
            } else {
                _unchangedCount = 0;
                _adjustPollInterval(POLL_FAST_MS);
                if (!_pollTimer) _pollTimer = setInterval(checkForRemoteChanges, _currentPollMs);
                checkForRemoteChanges();
            }
        });

        // Flush pending syncs on unload
        window.addEventListener('beforeunload', function() {
            if (_pollTimer) clearInterval(_pollTimer);
            // Fire any pending debounced syncs immediately
            Object.keys(_syncQueue).forEach(function(id) {
                if (_syncQueue[id] && _syncQueue[id].timer) {
                    clearTimeout(_syncQueue[id].timer);
                }
            });
        });

    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    function isRecentlyDeleted(id) {
        return !!(_recentlyDeleted[id] && _recentlyDeleted[id] > Date.now());
    }

    // Expose for use by kline-drawings.js
    window.DrawingTools = {
        syncDrawingToServer: syncDrawingToServer,
        deleteDrawingFromServer: deleteDrawingFromServer,
        refreshDrawingsFromServer: refreshDrawingsFromServer,
        checkForRemoteChanges: checkForRemoteChanges,
        mapDrawingType: mapDrawingType,
        isRecentlyDeleted: isRecentlyDeleted,
        SYNC_SESSION_ID: SYNC_SESSION_ID,
    };

})();
