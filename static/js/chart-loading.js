// Loading a chart: loadKlineChart() and its fetch path (request dedupe,
// load generations, timeouts), the fast same-instance data swap, viewport
// capture and restore, the stale-data badge with server-stale revalidation,
// and auto refresh. Needs app-core.js and chart-cache.js loaded first.

// Fast path: reuse existing chart instance, just swap data + redraw indicators
function fastSwapChartData(symbol, data, timeframe) {
    if (!klineChart) return false;
    if (typeof _hideStaleChartBadge === 'function') _hideStaleChartBadge(); // fresh data renders below — clear the stale badge
    var t0 = performance.now();
    // Capture viewport BEFORE _lastRendered* is overwritten — a same-symbol refresh
    // (candle-close reload) should keep the user's scroll/zoom if they've scrolled back.
    var _vpSnap = _captureChartViewport(symbol, timeframe);
    // Clear quant data cache on symbol/TF change
    if (typeof CustomIndicators !== 'undefined' && CustomIndicators.clearQuantCache) CustomIndicators.clearQuantCache();
    // Track last rendered symbol+TF so the same-symbol guard in loadKlineChart stays accurate
    _lastRenderedSymbol = symbol;
    _lastRenderedTimeframe = timeframe;
    _chartDisplayedSymbol = symbol;       // recovery does NOT null this (viewport-preserve key)
    _chartDisplayedTimeframe = timeframe;
    _lastRenderKlineChartTime = Date.now();

    // Reset Y-axis pan offset
    _yGapOffset = 0;

    // Exit fullscreen pane if active — must do synchronously (no requestAnimationFrame)
    // because data swap calls applyNewData() immediately after.
    if (typeof _fullscreenPaneId !== 'undefined' && _fullscreenPaneId) {
        _fullscreenPaneId = null;
        if (typeof _toggleCollapsedPanes === 'function') _toggleCollapsedPanes(true, null);
        if (typeof _hidePaneFsBtn === 'function') _hidePaneFsBtn();
        // Restore indicator pane heights synchronously
        try {
            getActivePaneIds().forEach(function(pid) {
                if (pid === 'candle_pane') return;
                var defaultH = (typeof PANE_DEFAULT_HEIGHTS !== 'undefined' && PANE_DEFAULT_HEIGHTS[pid]) || (pid === 'volume_pane' ? 100 : (pid === 'rsi_pane' ? 90 : 70));
                klineChart.setPaneOptions({ id: pid, minHeight: 40, height: defaultH });
            });
            klineChart.resize();
        } catch(e) {}
    }

    var klineData = convertToKlineData(data);
    _storeKlineData(klineData);

    // Store server-side RSI data for custom indicator (must be set BEFORE applyNewData)
    window._serverRsiData = _normalizeRsi(data.rsi) || [];

    // Clear custom overlays (drawings, indicator pack overlays, alerts, SMC)
    // Built-in indicator panes (VOL/MACD/MA/EMA/BOLL) auto-recalculate from candle data.
    // Custom RSI reads from _serverRsiData — must be force-recreated to pick up new data.
    klineChart.removeOverlay();
    _alertOverlayIds = [];

    // Swap candle data (use HA if that chart type is active)
    var _isHA = false;
    try { _isHA = localStorage.getItem('klineChartType') === 'heikin_ashi'; } catch(e) {}
    if (_isHA) {
        klineChart.applyNewData(klineData);
        _applyHeikinAshi(true);
    } else {
        klineChart.applyNewData(klineData);
    }
    _applyChartPricePrecision(klineData);

    // Clear any loading spinner left over from the fetch path. renderKlineChart
    // removes the overlay, but fast-swap never did — so if a load showed the
    // spinner (cache miss) and a later same-symbol call resolved via this
    // cache-hit path (e.g. an in-flight fetch repopulated the cache during a
    // tab-return reload), the spinner stayed stuck until a manual refresh.
    var _swapContainer = document.getElementById('kline-chart-container');
    if (_swapContainer) {
        var _swapOverlay = _swapContainer.querySelector('.chart-loading-overlay');
        if (_swapOverlay) _swapOverlay.remove();
        if (window._chartOverlayWatchdog) { clearTimeout(window._chartOverlayWatchdog); window._chartOverlayWatchdog = null; }
        var _swapErr = _swapContainer.querySelector('.chart-error-overlay');
        if (_swapErr) _swapErr.remove();
    }

    // Update overlay filter Y-axis cap for new asset (without this, the filter
    // retains yMax from the last renderKlineChart call — if that was a lower-priced asset,
    // all pack overlays for higher-priced assets like BTC get silently blocked)
    _updateOverlayYMax();

    // Force-recreate custom RSI so its calc() re-runs with the new _serverRsiData
    if (klineActiveIndicators.RSI) {
        try {
            klineChart.removeIndicator('rsi_pane', 'RSI');
            var _rsiMeta = IndicatorRegistry.getIndicator('RSI');
            klineChart.createIndicator({ name: 'RSI', calcParams: (_rsiMeta && _rsiMeta.calcParams ? _rsiMeta.calcParams.slice() : [14]) }, false, { id: 'rsi_pane', height: 90, minHeight: 40 });
            klineChart.setPaneOptions({ id: 'rsi_pane', gap: { top: 0.05, bottom: 0.05 } });
        } catch (e) { console.warn('[FastSwap] RSI recreate:', e); }
    }

    // Auto-size yAxis width to fit price labels for this asset
    autoSizeYAxis();

    // TradingView-like viewport: consistent bar spacing + right margin + scroll to latest.
    // Zoom (barSpace) is the user's setting — keep it on ANY same-symbol reload (candle-close
    // refresh, recovery), even when they're at the live edge; only a real asset/TF switch
    // falls back to the default. The at-edge "snap" only concerns horizontal scroll.
    klineChart.setBarSpace(_vpSnap && !_vpSnap.isSwitch && _vpSnap.barSpace ? _vpSnap.barSpace : 8);
    try { klineChart.setLeftMinVisibleBarCount(2); klineChart.setRightMinVisibleBarCount(0); } catch(e) {}
    // Preserve the user's scroll/zoom on a same-symbol refresh if they scrolled back; else snap.
    var _vpRestored = _restoreChartViewport(_vpSnap);
    if (!_vpRestored) _scrollToLastReal();
    // Deferred scroll: applyNewData triggers async layout, first scrollToRealTime may fire too early
    // Chain ALL overlay creation AFTER viewport settles to avoid stale dataIndex mapping
    // and pane desync (indicators/drawings frozen while price moves)
    var _swapGen = _chartLoadGeneration;
    requestAnimationFrame(function() {
        if (!klineChart || _swapGen !== _chartLoadGeneration) return;
        if (_vpRestored) { _restoreChartViewport(_vpSnap); } else { _scrollToLastReal(); }
        // Only reset a manually-dragged Y axis on a real switch — a routine same-symbol
        // refresh must not silently discard the user's Y-scale.
        if (!_vpSnap || _vpSnap.isSwitch) resetYAxisScale();
        // Re-apply custom overlays after viewport is settled (prevents "stuck" indicators)
        applyKlineIndicators(data, klineData, true, _swapGen);

        // Re-trigger Volume Profile for new data
        vpOnDataChange();
        // Re-trigger Key Levels for new data
        klOnDataChange();
        // Re-trigger Confluence Zones for new data
        confluenceOnDataChange();
        // Re-trigger Liquidation Heatmap for new data
        liqHeatmapOnDataChange();
        // Reload FRVP ranges for new symbol/TF
        frvpLoad();

        // Refresh drawer + alerts + drawings — MUST be after viewport settles
        // (scenarios.js is not loaded on the standalone chart page)
        if (typeof updateDrawerAll === 'function') updateDrawerAll();
        updateAlertBadge();
        if (!klineDrawingsHidden) {
            if (typeof loadKlineDrawings === 'function') loadKlineDrawings();
            if (typeof loadDrawingsFromServer === 'function') loadDrawingsFromServer();
        }
        renderAlertOverlays();
        if (typeof loadPlaysForChart === 'function') loadPlaysForChart(symbol, timeframe);

        // Resize chart after data swap — ResizeObserver handles ongoing layout shifts
        if (klineChart) {
            _ensureChartResizeObserver();
            klineChart.resize();
            // Force KLineChart to recalculate internal coordinate system (fixes crosshair offset).
            // KLineChart caches bounding rects internally; after pane removal/recreation the cache
            // goes stale but resize() alone won't invalidate it because the container size hasn't
            // changed. A physical height nudge forces a full re-read.
            _nudgeChartContainer();
        }

        // WS1c: emit switch-complete breadcrumb for the cache-hit (fast-swap) path.
        // _switchT0 is set in loadKlineChart; this rAF fires after viewport + overlays settle.
        if (typeof window.carracLog === 'function') {
            window.carracLog('switch', symbol + ' ' + timeframe + ' hit ' + Math.round(performance.now() - _switchT0) + 'ms', { sym: symbol, tf: timeframe, cacheHit: true });
        }
    });

    // Reset countdown timer for new TF
    // NOTE: do NOT call updateCandleCountdown() synchronously here — if the last candle
    // is expired (stocks after close, stale cache), it calls loadKlineChart which increments
    // _chartLoadGeneration, causing the rAF callback above to bail out without creating
    // indicators.  Let the interval handle it after the rAF has completed.
    if (window._candleCountdownInterval) clearInterval(window._candleCountdownInterval);
    window._candleCountdownInterval = setInterval(updateCandleCountdown, 1000);

    // Log when deferred overlays complete (indicator pack runs in setTimeout(0))
    setTimeout(function() {
        if (typeof updatePaneFullscreenButtons === 'function') updatePaneFullscreenButtons();
    }, 0);
    // Sync multi-chart pane state
    if (typeof mcSyncActiveChart === 'function') mcSyncActiveChart();
    return true;
}

// Scroll to last REAL candle (not phantom future bars).
// Phantom bars extend the X-axis into the future but scrollToRealTime()
// positions at the last phantom bar. We scroll there, then scroll back.
function _scrollToLastReal() {
    if (!klineChart) return;
    var dl = klineChart.getDataList();
    if (!dl || !dl.length) return;
    // Find last real bar (skip phantom future bars — timestamps > now)
    var idx = dl.length - 1;
    var _now = Date.now();
    while (idx > 0 && dl[idx].timestamp > _now) idx--;
    var phantomCount = dl.length - 1 - idx;
    // Normal scroll + offset
    klineChart.setOffsetRightDistance(80);
    klineChart.scrollToRealTime();
    // Scroll back, leaving ~15% of chart width as visible future dates
    if (phantomCount > 0) {
        var bs = klineChart.getBarSpace ? klineChart.getBarSpace() : 8;
        var ctr = document.getElementById('kline-chart-container');
        var chartW = ctr ? ctr.offsetWidth : 1000;
        var futureSpace = Math.round(chartW * 0.15);
        var scrollBack = Math.max(0, phantomCount * bs - futureSpace);
        klineChart.scrollByDistance(scrollBack);
    }
}

// Viewport preservation across SAME-symbol reloads — candle-close refresh AND
// recovery (boundary-retry / stale-return / reaper). Both rebuild or swap the
// chart and would otherwise _scrollToLastReal, yanking the user to the latest
// candle while they study history. Capture the pre-reload viewport, and if they're
// NOT at the live edge, restore their scroll + zoom instead of snapping. Real
// asset/TF switches (incl. deadlock-recover) still snap — old timestamps don't map.
// `isSwitch` keys off _chartDisplayedSymbol (NOT _lastRenderedSymbol, which recovery
// nulls — keying off it would make every recovery look like a switch and jump).
// MUST be called BEFORE the chart is swapped/disposed.
function _captureChartViewport(symbol, timeframe) {
    var snap = { isSwitch: (symbol !== _chartDisplayedSymbol || timeframe !== _chartDisplayedTimeframe) };
    try {
        if (klineChart && !snap.isSwitch && klineChart.getVisibleRange && klineChart.getDataList) {
            var vr = klineChart.getVisibleRange();
            var dl = klineChart.getDataList();
            if (vr && dl && dl.length) {
                var idx = dl.length - 1, now = Date.now();
                while (idx > 0 && dl[idx].timestamp > now) idx--; // last REAL bar (skip phantoms)
                snap.lastRealIdx = idx;
                snap.visibleRealTo = vr.realTo;
                var rt = Math.min(vr.realTo - 1, idx); // rightmost visible real bar
                snap.anchorTs = (rt >= 0 && rt < dl.length) ? dl[rt].timestamp : null;
                snap.barSpace = klineChart.getBarSpace ? klineChart.getBarSpace() : null;
            }
        }
    } catch (e) {}
    return snap;
}

// Returns true if a preserved viewport was restored (caller then SKIPS its own
// scroll-to-latest). Falls through to false on switch / at-edge / any error so
// the caller's existing _scrollToLastReal() runs — i.e. unchanged behavior.
function _restoreChartViewport(snap) {
    try {
        var decision = (typeof chartViewportDecision === 'function')
            ? chartViewportDecision(snap) : { mode: 'snap' };
        if (decision.mode === 'preserve' && snap && snap.anchorTs != null && klineChart) {
            if (snap.barSpace) { try { klineChart.setBarSpace(snap.barSpace); } catch (e) {} }
            klineChart.scrollToTimestamp(snap.anchorTs); // brings that bar to the right edge
            return true;
        }
    } catch (e) {}
    return false;
}

var _lastRenderKlineChartTime = 0;
var _lastRenderedSymbol = '';       // Which symbol the last full render was for
var _lastRenderedTimeframe = '';    // Which timeframe the last full render was for
// What the chart is currently DISPLAYING. Unlike _lastRenderedSymbol — which the
// recovery path NULLS to bypass the dedup guard + force a full rebuild — this is
// never nulled by recovery. The viewport-preserve decision keys off THIS so a
// same-symbol recovery (boundary-retry / stale-return / reaper) preserves the
// user's scroll, while a real asset/TF switch (incl. deadlock-recover, where the
// displayed symbol genuinely differs from the intended one) still snaps to latest.
var _chartDisplayedSymbol = '';
var _chartDisplayedTimeframe = '';
// consumed-once "silent revalidate" hint. Set by the tab-return
// handler (_onVisibilityReturn, further below) immediately before its >=5min-
// away _recoverChart('stale-return') call, ONLY when the chart on screen
// already matches the reload target. _loadKlineChartImpl reads + clears it on
// EVERY call (not just the intended one) so a rapid switch landing before the
// fetch resolves can never inherit stale silence, and re-verifies the same-
// symbol+TF-already-rendered condition itself rather than trusting the caller.
var _chartSilentReloadOnce = false;
var _chartLoadGeneration = 0;       // Incremented on each load — stale async callbacks bail out
var _overlayYMax = 0;               // Mutable Y-axis cap for overlay outlier filter — updated on every data swap

// WS1c: switch start timestamp — set in loadKlineChart, read in fastSwapChartData
// and renderKlineChart for the switch-timing breadcrumb. Module-level so it survives
// the call chain without needing parameter threading.
var _switchT0 = 0;

// T4.3 — dedupFetch: short-TTL in-flight coalescer for GET-only requests.
// Multiple callers that fire the same URL within ttlMs share ONE underlying
// network request, but EACH caller gets its own resp.clone() so they can all
// read the body independently. (A Response body is a one-shot stream — handing
// the SAME Response to two callers that both call .json() throws "body already
// read" on the second; cloning per caller avoids that.)
// Rules:
//   • Only for GETs — never pass POST/PUT/DELETE through this.
//   • On reject, the entry is cleared immediately so the next caller retries fresh.
//   • On resolve, the entry stays alive for ttlMs; callers within the window share
//     the one network response (each receives a fresh independent clone).
//   • After ttlMs the entry is evicted so the next call goes to the network again.
// Usage: dedupFetch(url, {credentials:'same-origin'}, 2000)
//   — drop-in for fetch(url, opts) for GET endpoints only.
var _dedupFetchMap = {};  // url -> { promise: Promise<Response>, timer: timeoutId }
function dedupFetch(url, opts, ttlMs) {
    var ttl = (typeof ttlMs === 'number') ? ttlMs : 2000;
    var entry = _dedupFetchMap[url];
    if (!entry) {
        var raw = fetch(url, opts || {}).then(
            function(resp) {
                // Schedule eviction after TTL so the next call after the window hits network
                if (_dedupFetchMap[url]) {
                    _dedupFetchMap[url].timer = setTimeout(function() {
                        delete _dedupFetchMap[url];
                    }, ttl);
                }
                return resp;
            },
            function(err) {
                // Errors are never cached — clear immediately so the next caller retries
                delete _dedupFetchMap[url];
                return Promise.reject(err);
            }
        );
        entry = { promise: raw, timer: null };
        _dedupFetchMap[url] = entry;
    }
    // Each caller (incl. the first) reads an independent clone; the original
    // Response is never consumed directly, so every clone is independently readable.
    return entry.promise.then(function(resp) { return resp.clone(); });
}

// Recompute _overlayYMax from the current chart's dataList.
// Must be called after applyNewData() in fastSwapChartData AND in renderKlineChart
// so the overlay filter always reflects the CURRENT asset's price range.
function _updateOverlayYMax() {
    if (!klineChart) return;
    var dl = klineChart.getDataList();
    if (!dl || !dl.length) return;
    var maxHigh = 0;
    for (var i = 0; i < dl.length; i++) {
        if (dl[i].high > maxHigh) maxHigh = dl[i].high;
    }
    _overlayYMax = maxHigh * 3;
}
function loadKlineChart(symbol, timeframe) {
    // WS1c: record switch start time for timing breadcrumbs emitted later in
    // fastSwapChartData (cache-hit path) and renderKlineChart (fetch path).
    _switchT0 = performance.now();
    if (typeof window.carracLog === 'function') {
        window.carracLog('switch', 'start ' + symbol + ' ' + timeframe, { sym: symbol, tf: timeframe });
    }

    // Lazy-load chart modules if not yet loaded.
    // This path fires on split-layout or standalone chart pages.
    if (!_carracPackPromise || !_klineDrawingsPromise) {
        var _sym = symbol, _tf = timeframe;
        Promise.all([ensureCarracPack(), ensureKLineDrawings()]).then(function() {
            loadKlineChart(_sym, _tf);
        }).catch(function(e) {
            console.error('[lazy] chart module load failed in loadKlineChart:', e);
            _loadKlineChartImpl(_sym, _tf);
        });
        return;
    }
    _loadKlineChartImpl(symbol, timeframe);
}

function _loadKlineChartImpl(symbol, timeframe) {
    var container = document.getElementById('kline-chart-container');
    if (!container) return;
    // Consume the silent-revalidate hint (if any) right away, on
    // EVERY call, so it can never leak into a later, unrelated load (e.g. a
    // rapid asset/TF switch that re-enters this fn before the hinted fetch
    // resolves). Only honored if the chart already showing IS this exact
    // reload target — re-checked here instead of trusted from the caller.
    var _silentRevalidate = _chartSilentReloadOnce;
    _chartSilentReloadOnce = false;
    if (_silentRevalidate && !(klineChart && _chartDisplayedSymbol === symbol && _chartDisplayedTimeframe === timeframe)) {
        _silentRevalidate = false;
    }
    // Consume the "this load IS the one server-stale follow-up
    // fetch" marker the same way — read+clear on every call so it can't leak
    // into an unrelated later load. Used below to make sure THIS fetch's own
    // response, even if it comes back stale again, never re-arms another
    // follow-up (one follow-up per organic stale serve, never a chain).
    var _isServerStaleRevalidateAttempt = _chartServerStaleRevalidateInFlight;
    _chartServerStaleRevalidateInFlight = false;
    // Rapid asset/TF switching: cancel any pending debounced fetch — this new
    // load supersedes it. The cached fast-path below renders instantly; the
    // fetch path re-arms the debounce. Stops the server-flooding backlog.
    if (window._chartFetchDebounce) { clearTimeout(window._chartFetchDebounce); window._chartFetchDebounce = null; }
    _yGapOffset = 0; // Reset Y-axis pan on chart load
    // Clear quant data cache on chart load
    if (typeof CustomIndicators !== 'undefined' && CustomIndicators.clearQuantCache) CustomIndicators.clearQuantCache();

    // Update URL for standalone chart page
    if (window.isStandaloneChart) {
        history.replaceState(null, '', '/chart/' + symbol + '/' + timeframe);
        document.title = symbol + ' ' + timeframe + ' \u2014 Carrac';
    }

    // Check cache (in-memory + sessionStorage)
    var cacheKey = symbol + '_' + timeframe;
    var cached = klineCacheGet(cacheKey);

    // Invalidate cache if a candle boundary has been crossed since it was populated
    if (cached && !cached._lite) {
        var _bMs = (typeof getTimeframeDurationMs === 'function') ? getTimeframeDurationMs(timeframe) : 0;
        if (_bMs > 0) {
            var _cLastTs = 0;
            var _ohlcv = cached.ohlcv;
            if (_ohlcv && _ohlcv.t && _ohlcv.t.length > 0) {
                // Columnar format: ohlcv = {t:[], o:[], h:[], l:[], c:[]}
                // Timestamps are in seconds — convert to ms for comparison
                _cLastTs = _ohlcv.t[_ohlcv.t.length - 1] * 1000;
            } else if (Array.isArray(_ohlcv) && _ohlcv.length > 0) {
                // Array-of-objects format: ohlcv = [{time:..., open:..., ...}, ...]
                _cLastTs = (_ohlcv[_ohlcv.length - 1].time || 0) * 1000;
            }
            if (_cLastTs > 0 && _cLastTs + _bMs <= Date.now()) {
                delete klineDataCache[cacheKey];
                try { sessionStorage.removeItem('kc_' + cacheKey); } catch(e) {}
                cached = null;
            }
        }
    }

    if (cached && !cached._lite) {
        // Fast path: reuse chart if chart exists (full data only, not lite preloader)
        if (klineChart) {
            // Guard: skip if this exact symbol+TF was just fully rendered (prevents double-render
            // that wipes overlays immediately after initial indicator draw)
            if (_lastRenderedSymbol === symbol && _lastRenderedTimeframe === timeframe && Date.now() - _lastRenderKlineChartTime < 2000) {
                // We're doing no work — but an EARLIER superseded load for this same
                // symbol+TF may have shown the loading overlay then deferred via the
                // fetch debounce (it returns before fetching). If THIS identical render
                // resolved first (trivially common when a service worker serves the
                // chart response from cache instantly), that earlier call's debounced
                // re-entry lands here and would return with the spinner still up — and
                // since it never reached the fetch, no abort watchdog ever armed, so the
                // overlay strands FOREVER (the permanent tab-return spinner). Clear it.
                var _strandedOvl = container.querySelector('.chart-loading-overlay');
                if (_strandedOvl) _strandedOvl.style.display = 'none';
                return;  // No generation bump — no new work started, must not invalidate pending rAF
            }
            // Bump generation NOW — after the early-return guard, before actual work.
            // Bumping at the top of loadKlineChart instead would let the countdown
            // timer's no-op reloads silently invalidate pending rAF indicator draws.
            _chartLoadGeneration++;
            try {
                fastSwapChartData(symbol, cached, timeframe);
            } catch (_swapErr) {
                // A malformed/stale cached payload (common right after a long tab
                // sleep) can throw inside fastSwapChartData AFTER it set
                // _lastRenderedSymbol but BEFORE it removed the overlay — and this
                // cache-hit path armed no fetch watchdog, so the overlay would
                // strand forever. Clear it, drop the poison cache entry, and fall
                // back to a clean fetch reload.
                console.error('[chart] fast-swap from cache failed, reloading fresh:', _swapErr);
                var _swErrOvl = container.querySelector('.chart-loading-overlay');
                if (_swErrOvl) _swErrOvl.remove();
                delete klineDataCache[cacheKey];
                try { sessionStorage.removeItem('kc_' + cacheKey); } catch (_e) {}
                _lastRenderedSymbol = null;
                _lastRenderedTimeframe = null;
                _loadKlineChartImpl(symbol, timeframe);
                return;
            }
        } else {
            _chartLoadGeneration++;
            renderKlineChart(symbol, cached, timeframe);
        }
        // Preload other TFs even on cache hit
        preloadAllTimeframes(symbol, timeframe);
        return;
    }

    // Bump generation for fetch path (must happen before async fetch)
    _chartLoadGeneration++;

    // A silent revalidate skips the overlay entirely — the chart
    // already on screen IS this fetch's target, and the server now answers
    // the stale-serve fetch in ~100-250ms (stale-while-revalidate), so painting a
    // spinner over an already-rendered chart for that round trip is a
    // pointless flash. Success below (fastSwap/full render) is unchanged; on
    // failure the .catch below still shows the error overlay as normal — the
    // silence only covers this optimistic window, not a genuine failure.
    if (!_silentRevalidate) {
        // Show loading overlay on top of existing chart (don't destroy it).
        // Always create a FRESH element: the universal reaper's first-seen stamp
        // (_reapFirstSeen) is keyed by element, so reusing the node would carry the
        // PREVIOUS load's age across shows — a re-shown overlay could then be reaped
        // instantly even though its load is live.
        var overlay = container.querySelector('.chart-loading-overlay');
        if (overlay) overlay.remove();
        overlay = document.createElement('div');
        overlay.className = 'chart-loading-overlay';
        overlay.innerHTML = '<div class="chart-loading-spinner"></div>';
        container.appendChild(overlay);
        overlay.style.display = 'flex';
        // Fallback watchdog armed HERE, at the moment the overlay is shown — before
        // the 220ms debounce return below. The per-fetch abort timer is armed only
        // AFTER that debounce, so when this fetch-path call returns via the debounce
        // and its debounced re-entry then resolves through a path that never reaches
        // the abort timer (the cache-hit fast-swap, a generation/pane guard, or a
        // throw inside fastSwapChartData), nothing else guarantees the overlay is
        // hidden and it strands forever (the recurring tab-return spinner). This
        // bounds the overlay's lifetime on EVERY path. Re-armed on each show, so only
        // the latest load's timer is ever pending; cleared by render/fast-swap/catch.
        if (window._chartOverlayWatchdog) clearTimeout(window._chartOverlayWatchdog);
        window._chartOverlayWatchdog = setTimeout(function () {
            var _ovl = document.querySelectorAll('.chart-loading-overlay');
            for (var _i = 0; _i < _ovl.length; _i++) {
                if (_ovl[_i].style.display !== 'none') _ovl[_i].remove();
            }
            window._chartOverlayWatchdog = null;
        }, CHART_FETCH_TIMEOUT_MS + 8000);
    } else if (typeof window.carracLog === 'function') {
        window.carracLog('resume', 'silent revalidate ' + symbol + ' ' + timeframe);
    }

    // If no chart exists yet, show fallback text
    if (!klineChart) {
        container.innerHTML = '<div class="chart-loading">Loading chart...</div>';
    }

    // Debounce the actual fetch. Rapid asset/TF switching otherwise fires one
    // cold compute per intermediate selection; on a 2-vCPU box those pile up and
    // serialize, so the chart you finally land on waits behind the backlog
    // ("stuck for minutes"). Defer the fetch ~220ms and let a newer load cancel
    // it (clearTimeout at the top of this fn). The loading overlay is already
    // showing, so the user gets immediate feedback.
    // Skip the debounce timer entirely inside the post-resume throttle window
    // (see the visibilitychange handler) — a timer armed now could be parked
    // ~1min by Chrome. Synchronous fetch is the only throttle-immune path.
    var _noDebounce = window._chartFetchReady ||
        (window._resumeNoDebounceUntil && Date.now() < window._resumeNoDebounceUntil) ||
        // PERF: the very first chart load of the session has no backlog to pile up
        // behind, so the rapid-switch debounce is pure latency here — fetch immediately
        // (saves ~220ms on the first open). The full debounce still applies afterwards.
        !window._firstChartLoadDone;
    if (!_noDebounce) {
        var _debounceArmedAt = Date.now();
        window._chartFetchDebounce = setTimeout(function() {
            // Telemetry: a healthy debounce fires ~220ms after arming. Chrome's
            // background-timer throttling can hold timers armed around the
            // hidden→visible transition until a 1-minute-aligned wake-up (a
            // tab-return spinner of ~50s) — log it so any recurrence is
            // visible in __carracLog and in Bugsink beacon context.
            var _lateMs = Date.now() - _debounceArmedAt - 220;
            if (_lateMs > 5000 && typeof window.carracLog === 'function') {
                window.carracLog('switch', 'debounce fired ' + Math.round(_lateMs / 1000) + 's late', { lateMs: _lateMs });
            }
            window._chartFetchReady = true;
            try { _loadKlineChartImpl(symbol, timeframe); }
            finally { window._chartFetchReady = false; }
        }, 220);
        return;
    }
    window._chartFetchReady = false;
    window._firstChartLoadDone = true;

    // Fetch chart data with specific timeframe
    var _fetchGen = _chartLoadGeneration;
    var ltfSet = ['1m','3m','5m','15m','30m','1h','4h'];
    var mode = ltfSet.indexOf(timeframe) >= 0 ? 'ltf' : 'htf';
    var maxBars = (mode === 'ltf') ? 2500 : 1500;
    // Watchdog: the browser has no built-in fetch timeout, so a saturated worker
    // or a stalled data source keeps the request open indefinitely and the loading
    // overlay above stays stuck "forever" until a manual page refresh. Abort after a bounded wait —
    // the rejection lands in .catch below, which clears the spinner and offers a
    // one-click retry.
    var _chartFetchTimedOut = false;
    var _chartAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var _chartFetchTimer = _chartAbort ? setTimeout(function() {
        _chartFetchTimedOut = true;
        try { _chartAbort.abort(); } catch (e) {}
    }, CHART_FETCH_TIMEOUT_MS) : null;
    fetch('/api/chart/' + encodeURIComponent(symbol) + '?days=' + maxBars + '&mode=' + mode + '&timeframe=' + encodeURIComponent(timeframe) + '&fmt=col' + _derivativesParam() + _quantParam(), {
        credentials: 'same-origin',
        signal: _chartAbort ? _chartAbort.signal : undefined
    })
    .then(function(response) {
        if (_chartFetchTimer) { clearTimeout(_chartFetchTimer); _chartFetchTimer = null; }
        if (!response.ok) {
            var _httpErr = new Error('HTTP ' + response.status);
            _httpErr.status = response.status;
            throw _httpErr;
        }
        // Stale markers (same-origin — headers fully readable). Captured here
        // because they're gone once the body is parsed. TWO
        // distinct sources both set X-Carrac-Stale/X-Carrac-Age —
        //  - the SW's offline fallback (static/service-worker.js ~129-141),
        //    which only fires when the network fetch itself failed/timed out.
        //    It rebuilds its Headers FROM the cached response, which carries
        //    X-Carrac-Cached-At (stamped at cache-write time on a successful
        //    fetch, static/service-worker.js line 115).
        //  - the server's stale-while-revalidate serve (carrac/charts/routes.py
        //    ~1317-1318) — a REAL 200 straight from the network, no SW
        //    involvement, and it never sets X-Carrac-Cached-At. The background
        //    rebuild it kicks off there (_spawn_chart_refresh) self-heals in
        //    ~1-6s, so this is by-design, not a connection issue.
        // X-Carrac-Cached-At presence is therefore an airtight discriminator
        // (verified by reading both header-setting sites above).
        var _staleHdr = response.headers.get('X-Carrac-Stale') === '1';
        var _swCachedAt = response.headers.get('X-Carrac-Cached-At');
        var _swStale = _staleHdr && !!_swCachedAt;
        var _serverStale = _staleHdr && !_swCachedAt;
        var _staleAgeS = _staleHdr ? parseInt(response.headers.get('X-Carrac-Age') || '0', 10) : 0;
        return response.json().then(function(data) {
            if (_swStale) {
                data._sw_stale = true;
                data._sw_age_s = _staleAgeS;
                if (typeof window.carracLog === 'function') {
                    window.carracLog('stale', 'sw-stale served age=' + _staleAgeS + 's');
                }
            } else if (_serverStale) {
                data._server_stale = true;
                data._server_age_s = _staleAgeS;
                if (typeof window.carracLog === 'function') {
                    window.carracLog('stale', 'server-stale served age=' + _staleAgeS + 's');
                }
            }
            return data;
        });
    })
    .then(function(data) {
        // Cache the response (in-memory + sessionStorage) — always cache even if stale,
        // but STRIP the stale-provenance flags first: a cached payload carrying
        // _sw_stale/_server_stale would show a false badge if a future cache-hit
        // path ever reaches _updateStalenessUI. The same guard covers the
        // server-stale flags.
        if (data._sw_stale || data._server_stale) {
            var _toCache = Object.assign({}, data);
            delete _toCache._sw_stale;
            delete _toCache._sw_age_s;
            delete _toCache._server_stale;
            delete _toCache._server_age_s;
            klineCacheSave(cacheKey, _toCache);
        } else {
            klineCacheSave(cacheKey, data);
        }
        // Guard: user switched to a different asset while this fetch was in-flight.
        // A newer load owns the spinner now — clear the one this superseded fetch
        // left up so it can't strand if the newer load resolves via a path that
        // never touches this container.
        if (_fetchGen !== _chartLoadGeneration) {
            var _staleOvl = container.querySelector('.chart-loading-overlay');
            if (_staleOvl) _staleOvl.style.display = 'none';
            return;
        }
        // Guard: if user switched panes during async fetch, don't render in wrong container
        // (data is already cached above for when user switches back)
        if (typeof mcIsMultiChart === 'function' && mcIsMultiChart()) {
            var _activeContainer = document.getElementById('kline-chart-container');
            if (_activeContainer !== container) {
                var _paneOvl = container.querySelector('.chart-loading-overlay');
                if (_paneOvl) _paneOvl.style.display = 'none';
                return;
            }
        }
        // Same-symbol candle-close reload → REUSE the chart (light in-place swap of
        // fresh data) instead of dispose+recreate: no flicker, far less CPU, no full
        // teardown. Switches and recovery null _lastRenderedSymbol first, so they fall
        // through to the full rebuild. _chartLoadGeneration was already bumped above
        // (before the fetch), so fastSwapChartData's rAF generation-guard stays valid.
        var _reuse = (typeof shouldReuseChart === 'function') && shouldReuseChart({
            hasChart: !!klineChart, symbol: symbol, timeframe: timeframe,
            lastRenderedSymbol: _lastRenderedSymbol, lastRenderedTimeframe: _lastRenderedTimeframe
        });
        if (_reuse) {
            try {
                fastSwapChartData(symbol, data, timeframe);
            } catch (_reuseErr) {
                // Fast-swap of fresh data failed — fall back to a clean full rebuild
                // (renderKlineChart disposes the chart first) rather than stranding the
                // user behind the fetch .catch error overlay. Mirrors the cache-hit guard.
                if (typeof window.carracLog === 'function') {
                    window.carracLog('chart', 'fast-swap reload failed, full render', String((_reuseErr && _reuseErr.message) || _reuseErr));
                }
                renderKlineChart(symbol, data, timeframe);
            }
        } else {
            // Full render (first load, asset/TF switch, or recovery) — creates the
            // chart + all indicator/overlay types fresh.
            renderKlineChart(symbol, data, timeframe);
        }
        // Live WS subscribe — route to correct venue based on ws_venue field
        if (!window._wsVenueBySymbol) window._wsVenueBySymbol = {};
        window._wsVenueBySymbol[symbol] = { venue: data.ws_venue || null, exchange: data.ws_exchange || null };
        if (data.live_ws && data.ws_venue === 'hyperliquid' && typeof subscribeHLSymbol === 'function') {
            subscribeHLSymbol(symbol);
        } else if (data.live_ws && data.ws_venue === 'ccxt' && data.ws_exchange && typeof subscribeCcxtSymbol === 'function') {
            // Companion-pane loads must not steal the single page-wide CCXT
            // socket from the active pane; mcSetActivePane re-points it on
            // focus change (Binance/HL/Finnhub subscribes are additive — safe).
            if (!window._mcCompanionLoading) subscribeCcxtSymbol(symbol, data.ws_exchange);
        } else if (data.live_ws && data.ws_venue === 'finnhub' && typeof subscribeFinnhubSymbol === 'function') {
            subscribeFinnhubSymbol(symbol);
        } else if (data.tier === 'dynamic' && data.live_ws !== false && typeof subscribeBinanceSymbol === 'function') {
            subscribeBinanceSymbol(symbol);
        }
        // Show staleness UI for non-WS assets
        _updateStalenessUI(data);
        // A server-SWR stale serve already kicked off a background
        // rebuild server-side (~1-6s) — follow up once with a silent revalidate
        // so the slightly-old chart doesn't linger until the user interacts.
        // Skip if THIS response IS already that follow-up (still stale, rebuild
        // still running/failed) — one follow-up per stale serve, never a chain.
        if (data._server_stale && !_isServerStaleRevalidateAttempt) {
            _scheduleServerStaleRevalidate(symbol, timeframe);
        }
        // Preload all other TFs + adjacent assets in background
        // Skip during rotation — user can't switch TFs, saves ~80% of requests
        if (!(typeof mcIsRotating === 'function' && mcIsRotating())) {
            preloadAllTimeframes(symbol, timeframe);
            preloadAdjacentAssets(symbol, timeframe);
        }
    })
    .catch(function(error) {
        if (_chartFetchTimer) { clearTimeout(_chartFetchTimer); _chartFetchTimer = null; }
        // Always clear the loading spinner — without this it stays stuck.
        var lo = container.querySelector('.chart-loading-overlay');
        if (lo) lo.style.display = 'none';
        if (window._chartOverlayWatchdog) { clearTimeout(window._chartOverlayWatchdog); window._chartOverlayWatchdog = null; }
        var errOverlay = container.querySelector('.chart-error-overlay');
        if (!errOverlay) {
            errOverlay = document.createElement('div');
            errOverlay.className = 'chart-error-overlay';
            errOverlay.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.7);z-index:100;cursor:pointer';
            container.appendChild(errOverlay);
        }
        // One pure model (chart-live-tick.js) decides wording and
        // whether retry is on offer. A data 404 can be the transient post-deploy
        // rate-limit window that heals in ~2min, so HTTP failures get a retry
        // instead of "Error: HTTP 404" and a shrug; only plain JS errors stay
        // dismiss-only (retrying a TypeError just loops).
        var _m = (typeof chartErrorOverlayModel === 'function')
            ? chartErrorOverlayModel({ message: error && error.message, name: error && error.name,
                                       status: error && error.status, timedOut: _chartFetchTimedOut,
                                       symbol: symbol })
            : { retry: _chartFetchTimedOut || (error && error.name === 'AbortError'),
                headline: 'Error: ' + ((error && error.message) || error),
                sub: 'Click to dismiss, try another timeframe',
                logMsg: 'load failed (model missing)' };
        errOverlay.title = _m.retry ? 'Click to retry' : 'Click to dismiss';
        errOverlay.onclick = _m.retry
            ? function() { errOverlay.remove(); window.recoverChart('user-retry', { bustCache: true, force: true }); }
            : function() { errOverlay.remove(); };
        errOverlay.innerHTML = '<div style="color:#ef5350;font-size:14px;text-align:center">' +
            escapeHtml(_m.headline) + '<br><span style="color:#888;font-size:12px">' +
            escapeHtml(_m.sub) + '</span></div>';
        if (typeof window.carracLog === 'function') {
            window.carracLog('chart', 'load failed: ' + _m.logMsg, { sym: symbol, tf: timeframe });
        }
        window.cvxTrackError && window.cvxTrackError('chart_load', (error && error.message) || String(error));
    });
}

// ============================================================
// Chart Staleness UI — for non-WebSocket assets
// ============================================================
var _stalenessAutoRefreshTimer = null;

function _updateStalenessUI(data) {
    // Remove existing staleness badge
    var existing = document.getElementById('staleness-badge');
    if (existing) existing.remove();
    if (_stalenessAutoRefreshTimer) {
        clearInterval(_stalenessAutoRefreshTimer);
        _stalenessAutoRefreshTimer = null;
    }

    // Only show for non-WS assets, SW-stale, or server-stale data
    if (data.live_ws !== false && !data._sw_stale && !data._server_stale) return;

    var toolbar = document.querySelector('.toolbar-nav');
    if (!toolbar) return;

    // Use server timestamp if available (handles browser-side caching correctly)
    var displayDate = data._server_ts ? new Date(data._server_ts * 1000) : new Date();
    var timeStr = displayDate.getHours().toString().padStart(2, '0') + ':' + displayDate.getMinutes().toString().padStart(2, '0');

    var badge = document.createElement('div');
    badge.id = 'staleness-badge';

    if (data._sw_stale) {
        // Amber variant: SW served a stale cached response (connection issue / timeout)
        badge.style.cssText = 'display:inline-flex;align-items:center;gap:6px;margin-left:auto;padding:3px 10px;' +
            'background:rgba(187,128,9,0.15);border:1px solid #bb8009;border-radius:6px;font-size:11px;color:#d29922;';
        badge.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#d29922" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
            '<span id="staleness-time">Data as of ' + timeStr + ' (connection issue)</span>';
    } else if (data._server_stale) {
        // Neutral variant: the server served a stale-while-revalidate
        // payload — by design, self-heals via the background rebuild it already
        // kicked off (see _scheduleServerStaleRevalidate). NOT a connection
        // issue, so same calm styling as the default badge below, just with an
        // "updating" note instead of a flat timestamp.
        badge.style.cssText = 'display:inline-flex;align-items:center;gap:6px;margin-left:auto;padding:3px 10px;' +
            'background:#21262d;border:1px solid #30363d;border-radius:6px;font-size:11px;color:#8b949e;';
        badge.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#8b949e" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
            '<span id="staleness-time">Data as of ' + timeStr + ' (updating…)</span>';
    } else {
        badge.style.cssText = 'display:inline-flex;align-items:center;gap:6px;margin-left:auto;padding:3px 10px;' +
            'background:#21262d;border:1px solid #30363d;border-radius:6px;font-size:11px;color:#8b949e;';
        // Clock icon + time
        badge.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#8b949e" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
            '<span id="staleness-time">Data as of ' + timeStr + '</span>';
    }

    // Refresh button
    var refreshBtn = document.createElement('button');
    refreshBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:2px;color:#58a6ff;display:flex;align-items:center;';
    refreshBtn.title = 'Refresh chart data';
    refreshBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
    refreshBtn.onclick = function() {
        _refreshStaleChart(data.symbol);
    };
    badge.appendChild(refreshBtn);

    // Auto-refresh dropdown
    var autoSelect = document.createElement('select');
    autoSelect.style.cssText = 'background:#0d1117;border:1px solid #30363d;border-radius:3px;color:#8b949e;font-size:10px;padding:1px 4px;cursor:pointer;';
    autoSelect.innerHTML = '<option value="0">Auto: Off</option><option value="900">15m</option><option value="1800">30m</option><option value="3600">1h</option>';
    var savedInterval = localStorage.getItem('chartAutoRefresh_' + data.symbol);
    if (savedInterval) autoSelect.value = savedInterval;
    autoSelect.onchange = function() {
        var val = parseInt(this.value);
        localStorage.setItem('chartAutoRefresh_' + data.symbol, val);
        _setupAutoRefresh(data.symbol, val);
    };
    badge.appendChild(autoSelect);

    toolbar.appendChild(badge);

    // Start auto-refresh if one was saved
    if (savedInterval && parseInt(savedInterval) > 0) {
        _setupAutoRefresh(data.symbol, parseInt(savedInterval));
    }
}

// One silent follow-up fetch per server-stale serve, ~7s later
// (the server's background rebuild — carrac/charts/routes.py
// _spawn_chart_refresh — takes ~1-6s). Timestamp-guarded so rapid TF/asset
// flipping can't stack timers: every call cancels any pending timer and
// re-arms, so only the LATEST stale serve's follow-up ever survives to fire.
// Only matters while the tab is visible (the user just loaded this chart), so
// a plain setTimeout is fine — no rAF machinery needed.
var _serverStaleRevalidateTimer = null;
var _serverStaleRevalidateArmedAt = 0;
// consumed-once marker (mirrors _chartSilentReloadOnce) — true only while the ONE follow-up fetch below is in flight,
// so its own response can never re-arm another follow-up. Declared here
// (used by _loadKlineChartImpl above, which reads+clears it on every call).
var _chartServerStaleRevalidateInFlight = false;

function _scheduleServerStaleRevalidate(symbol, timeframe) {
    if (_serverStaleRevalidateTimer) {
        clearTimeout(_serverStaleRevalidateTimer);
        _serverStaleRevalidateTimer = null;
    }
    var _armedAt = Date.now();
    _serverStaleRevalidateArmedAt = _armedAt;
    _serverStaleRevalidateTimer = setTimeout(function() {
        _serverStaleRevalidateTimer = null;
        // Superseded by a newer stale serve's own follow-up (guard, not just
        // belt-and-suspenders — clearTimeout above already prevents overlap).
        if (_armedAt !== _serverStaleRevalidateArmedAt) return;
        // Same-symbol-still-displayed guard, mirroring _onVisibilityReturn's
        // silent-reload arming: the user may have
        // switched asset/TF in the 7s window — don't revalidate a chart no
        // one is looking at, and don't fight whatever load replaced it.
        if (!klineChart || _chartDisplayedSymbol !== symbol || _chartDisplayedTimeframe !== timeframe) return;
        if (typeof window.carracLog === 'function') {
            window.carracLog('stale', 'revalidate after stale ' + symbol + ' ' + timeframe);
        }
        // Bust just this symbol+TF's cache entry first — it was cache.save()'d
        // moments ago with the STALE payload under this same key (KLINE_CACHE_TTL
        // is 15min), so without this the reload would hit the in-memory/
        // sessionStorage cache and silently replay the same stale data instead
        // of re-checking the server.
        var _ck = symbol + '_' + timeframe;
        delete klineDataCache[_ck];
        try { sessionStorage.removeItem('kc_' + _ck); } catch (e) {}
        // Arm the once-flag then use the normal load path (mirrors
        // _onVisibilityReturn in stale-tab-recovery.js): _loadKlineChartImpl
        // self-verifies the same displayed-symbol+TF guard again and, if it
        // holds, skips the loading-overlay flash. No fresh=1 — that forces a
        // synchronous rebuild; this just re-checks whether the background
        // rebuild the original stale serve triggered has landed yet.
        _chartSilentReloadOnce = true;
        // Also mark THIS fetch as the follow-up attempt (read+cleared at the
        // top of _loadKlineChartImpl) so that if it ALSO comes back stale
        // (rebuild still running or failed), the response handler does not
        // call this function again — one follow-up per stale serve, no chain.
        _chartServerStaleRevalidateInFlight = true;
        // Force the debounce-skip path (mirrors _recoverChart in
        // stale-tab-recovery.js): without this, _loadKlineChartImpl would arm its normal
        // 220ms fetch debounce and re-enter itself on a SEPARATE call, which
        // would already have consumed+cleared both once-flags above on THIS
        // call — losing the silence and the no-chain marker on the call that
        // actually fetches.
        window._chartFetchReady = true;
        loadKlineChart(symbol, timeframe);
    }, 7000);
}

function _refreshStaleChart(symbol) {
    if (!symbol) return;
    // Skip while user is touch-panning the chart
    if (window._touchPanActive) return;
    // Stamp the switch stopwatch: renderKlineChart logs its completion
    // breadcrumb as (now - _switchT0). This silent-refresh path must reset
    // _switchT0, or it keeps the PREVIOUS switch's value and produces bogus
    // "miss 208502ms"-style telemetry entries.
    _switchT0 = performance.now();
    var tf = klineChartTimeframe || '1D';
    var ltfSet = ['1m','3m','5m','15m','30m','1h','4h'];
    var mode = ltfSet.indexOf(tf) >= 0 ? 'ltf' : 'htf';
    var maxBars = (mode === 'ltf') ? 2500 : 1500;
    fetch('/api/chart/' + encodeURIComponent(symbol) + '?days=' + maxBars + '&mode=' + mode + '&timeframe=' + encodeURIComponent(tf) + '&fresh=1', {
        credentials: 'same-origin'
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var cacheKey = symbol + '_' + tf;
        klineCacheSave(cacheKey, data);
        renderKlineChart(symbol, data, tf);
        _updateStalenessUI(data);
    })
    .catch(function(e) {
        console.warn('[Staleness] Refresh failed:', e);
    });
}

function _setupAutoRefresh(symbol, intervalSec) {
    if (_stalenessAutoRefreshTimer) {
        clearInterval(_stalenessAutoRefreshTimer);
        _stalenessAutoRefreshTimer = null;
    }
    if (intervalSec <= 0) return;
    _stalenessAutoRefreshTimer = setInterval(function() {
        // Only auto-refresh if chart is still open for same symbol
        if (klineChartSymbol === symbol) {
            _refreshStaleChart(symbol);
        } else {
            clearInterval(_stalenessAutoRefreshTimer);
            _stalenessAutoRefreshTimer = null;
        }
    }, intervalSec * 1000);
}
