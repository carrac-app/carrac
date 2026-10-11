// Drawing a chart: API rows to KLineChart bars (Heikin Ashi, price
// precision), log/inverted price scales, and renderKlineChart(), which builds
// the chart and its panes. Needs app-core.js, indicator-prefs.js and
// chart-indicators.js loaded first.

var _logScaleEnabled = false;  // Logarithmic scale for candle chart
var _invertScaleEnabled = false;  // Inverted (upside-down) price scale — session-only lens, not persisted
var _logScaleDataMin = 0, _logScaleDataMax = 0;  // Min/max of log-transformed data for formatPrice heuristic

// Heikin Ashi support — always work from stored original kline data
var _storedKlineData = null;

function _storeKlineData(klineData) {
    // Deep-copy OHLCV+timestamp from the fresh server data
    _storedKlineData = klineData.map(function(d) {
        return { timestamp: d.timestamp, open: d.open, high: d.high, low: d.low, close: d.close, volume: d.volume };
    });
}

// Chart-level price precision from the loaded data's magnitude. klinecharts
// defaults to 2 decimals and nothing ever overrode it, so sub-$0.01 symbols
// (TOTAL3BTC, BOME, SHIB…) hit the library's degenerate-range branch (visible
// high-low span < 10^-precision forces a ±0.04-0.08 pad) and rendered as a
// flat line with all-"0.00" axis labels.
function _computeChartPricePrecision(dataList) {
    var maxPrice = 0;
    if (dataList) {
        for (var i = 0; i < dataList.length; i++) {
            var h = dataList[i] && dataList[i].high;
            if (h != null && h > maxPrice) maxPrice = h;
        }
    }
    // Log-scale mode transforms data to ln(price) (can be <= 0) — keep the
    // stock 2-decimal behavior there.
    if (!(maxPrice > 0) || maxPrice >= 1) return 2;
    var decimals = 3 - Math.floor(Math.log10(maxPrice)); // ~4 significant figures
    return Math.max(2, Math.min(decimals, 12));
}

function _applyChartPricePrecision(dataList) {
    if (!klineChart) return;
    try { klineChart.setPriceVolumePrecision(_computeChartPricePrecision(dataList), 0); } catch (e) {}
}

function _applyHeikinAshi(enable) {
    if (!klineChart || !_storedKlineData || _storedKlineData.length === 0) return;
    var src = _storedKlineData;

    if (enable) {
        var haData = [];
        for (var i = 0; i < src.length; i++) {
            var haClose = (src[i].open + src[i].high + src[i].low + src[i].close) / 4;
            var haOpen = i === 0
                ? (src[i].open + src[i].close) / 2
                : (haData[i - 1].open + haData[i - 1].close) / 2;
            haData.push({
                timestamp: src[i].timestamp,
                open: haOpen,
                close: haClose,
                high: Math.max(src[i].high, haOpen, haClose),
                low: Math.min(src[i].low, haOpen, haClose),
                volume: src[i].volume
            });
        }
        klineChart.applyNewData(haData);
    } else {
        // Re-apply original data
        klineChart.applyNewData(src.map(function(d) {
            return { timestamp: d.timestamp, open: d.open, high: d.high, low: d.low, close: d.close, volume: d.volume };
        }));
    }
    // applyNewData resets scroll — restore viewport position
    if (typeof _scrollToLastReal === 'function') _scrollToLastReal();
    if (typeof resetYAxisScale === 'function') requestAnimationFrame(function() { resetYAxisScale(); });
}

// Convert raw API data to KLineChart format
function convertToKlineData(data) {
    var useLog = _logScaleEnabled;
    var logMin = Infinity, logMax = -Infinity;
    var ohlcv = data.ohlcv;
    var klineData;

    // Columnar format: ohlcv = {t:[],o:[],h:[],l:[],c:[]}
    if (ohlcv && ohlcv.t) {
        var len = ohlcv.t.length;
        klineData = new Array(len);
        for (var ci = 0; ci < len; ci++) {
            var o = ohlcv.o[ci], h = ohlcv.h[ci], l = ohlcv.l[ci], c = ohlcv.c[ci];
            if (useLog && o > 0 && h > 0 && l > 0 && c > 0) {
                var lH = Math.log(h), lL = Math.log(l);
                if (lL < logMin) logMin = lL;
                if (lH > logMax) logMax = lH;
                klineData[ci] = { timestamp: ohlcv.t[ci] * 1000, open: Math.log(o), high: lH, low: lL, close: Math.log(c), volume: 0, _origOpen: o, _origHigh: h, _origLow: l, _origClose: c };
            } else {
                klineData[ci] = { timestamp: ohlcv.t[ci] * 1000, open: o, high: h, low: l, close: c, volume: 0 };
            }
        }
        // Columnar volume: {t:[], v:[], d:[]}
        if (data.volume && data.volume.v) {
            var vol = data.volume;
            for (var vi = 0; vi < Math.min(len, vol.v.length); vi++) {
                if (klineData[vi]) klineData[vi].volume = vol.v[vi] || 0;
            }
        }
    } else {
        // Legacy object-per-bar format
        klineData = ohlcv.map(function(c) {
            if (useLog && c.open > 0 && c.high > 0 && c.low > 0 && c.close > 0) {
                var lHigh = Math.log(c.high), lLow = Math.log(c.low);
                if (lLow < logMin) logMin = lLow;
                if (lHigh > logMax) logMax = lHigh;
                return {
                    timestamp: c.time * 1000,
                    open: Math.log(c.open), high: lHigh,
                    low: lLow, close: Math.log(c.close),
                    volume: 0,
                    _origOpen: c.open, _origHigh: c.high, _origLow: c.low, _origClose: c.close
                };
            }
            return { timestamp: c.time * 1000, open: c.open, high: c.high, low: c.low, close: c.close, volume: 0 };
        });
        if (data.volume && data.volume.length > 0) {
            data.volume.forEach(function(v, i) {
                if (klineData[i]) klineData[i].volume = v.value || 0;
            });
        }
    }

    if (useLog && logMin !== Infinity) {
        _logScaleDataMin = logMin;
        _logScaleDataMax = logMax;
    }

    // Append phantom future bars so the X-axis extends beyond today's data.
    // Phantom bars use the last real close for OHLCV (avoids NaN in Y-axis)
    // and volume=0. They render as invisible doji at the last price level.
    var realLen = klineData.length;
    if (realLen > 0) {
        var tf = klineChartTimeframe || '1D';
        var _lastReal = klineData[realLen - 1];
        var lastTs = _lastReal.timestamp;
        var lastC = _lastReal.close;
        var barMs = (typeof getTimeframeDurationMs === 'function') ? getTimeframeDurationMs(tf) : 0;
        if (barMs === 0 && tf === '1M') barMs = 30 * 86400000;
        if (barMs > 0) {
            // Right-margin runway only — enough to centre the latest candle plus
            // a few months of projection space. The old map padded up
            // to ~1000 future bars (1D → end of 2028), inflating the array
            // KLineChart recalculates ALL indicators over by ~40% at a measured
            // +58% cost per recalc — for future scroll-space almost nobody used.
            // Drawings past the runway still position via KLineChart's timestamp
            // extrapolation; only far-future *scrolling* is bounded.
            var _phantomMap = { '1m': 120, '3m': 120, '5m': 120, '15m': 120, '30m': 120, '1h': 120, '4h': 120, '1D': 120, '3D': 100, '1W': 80, '1M': 24 };
            var phantomCount = _phantomMap[tf] || 100;
            for (var _ph = 1; _ph <= phantomCount; _ph++) {
                klineData.push({
                    timestamp: lastTs + barMs * _ph,
                    open: lastC, high: lastC, low: lastC, close: lastC,
                    _phantom: true
                });
            }
        }
    }
    klineData._realLength = realLen;

    return klineData;
}

// Toggle logarithmic scale and reload chart
function toggleLogScale() {
    _logScaleEnabled = !_logScaleEnabled;
    // Update toolbar button state
    var btn = document.getElementById('log-scale-btn');
    if (btn) btn.classList.toggle('active', _logScaleEnabled);
    // Update Y-axis A/L toggle buttons
    var aBtn = document.getElementById('yaxis-auto-btn');
    var lBtn = document.getElementById('yaxis-log-btn');
    if (aBtn) aBtn.classList.toggle('active', !_logScaleEnabled);
    if (lBtn) lBtn.classList.toggle('active', _logScaleEnabled);
    // Force full chart re-render (need to reinit formatPrice context)
    if (klineChartSymbol && klineChartTimeframe) {
        _lastRenderKlineChartTime = 0; // Bypass guard
        var cacheKey = klineChartSymbol + '_' + klineChartTimeframe;
        var cached = klineCacheGet(cacheKey);
        if (cached) {
            renderKlineChart(klineChartSymbol, cached, klineChartTimeframe);
        } else {
            loadKlineChart(klineChartSymbol, klineChartTimeframe);
        }
    }
}

// Invert (flip) the candle-pane price scale — klinecharts native yAxis.reverse,
// scoped to the candle pane by the library (sub-panes unaffected). Pure render
// style: no data reload needed. Deliberately NOT persisted — it's a temporary
// analytical lens (TradingView Alt+I), and persisting would leave normal assets
// upside-down after closing an inverted ratio view.
function toggleInvertScale() {
    _invertScaleEnabled = !_invertScaleEnabled;
    if (klineChart) { try { klineChart.setStyles({ yAxis: { reverse: _invertScaleEnabled } }); } catch(e) {} }
    var iBtn = document.getElementById('yaxis-invert-btn');
    if (iBtn) iBtn.classList.toggle('active', _invertScaleEnabled);
    var mobCb = document.getElementById('mob-invert-scale');
    if (mobCb) mobCb.checked = _invertScaleEnabled;
    if (typeof showToast === 'function') showToast(_invertScaleEnabled ? 'Inverted scale — Alt+I to restore' : 'Normal scale');
}

// Inject TradingView-style A/L toggle on the Y-axis area of the candle pane
function injectYAxisScaleToggle() {
    if (!klineChart) return;
    var existing = document.getElementById('yaxis-scale-toggle');
    if (existing) existing.remove();
    // Attach to candle pane's yaxis DOM so buttons sit at the bottom of the candle pane,
    // right above the sub-pane indicators (VOL/RSI/MACD)
    var target = null;
    try { target = klineChart.getDom('candle_pane', 'yAxis'); } catch(e) {}
    if (!target) try { target = klineChart.getDom({ paneId: 'candle_pane', position: 'yAxis' }); } catch(e) {}
    if (!target) { target = document.getElementById('kline-chart-container'); }
    var div = document.createElement('div');
    div.id = 'yaxis-scale-toggle';
    div.className = 'yaxis-scale-toggle';
    div.innerHTML =
        '<button class="yaxis-scale-btn' + (!_logScaleEnabled ? ' active' : '') + '" id="yaxis-auto-btn" title="Auto (linear) scale">A</button>' +
        '<button class="yaxis-scale-btn' + (_logScaleEnabled ? ' active' : '') + '" id="yaxis-log-btn" title="Logarithmic scale (O)">L</button>' +
        '<button class="yaxis-scale-btn' + (_invertScaleEnabled ? ' active' : '') + '" id="yaxis-invert-btn" title="Invert scale (Alt+I)">&#8645;</button>';
    // Ensure Y-axis container allows overflow so buttons aren't clipped
    if (target) target.style.overflow = 'visible';
    target.appendChild(div);
    // Use pointerup instead of click — on mobile, the Y-axis pointerdown handler
    // captures the pointer and preventDefault/stopPropagation kills click events.
    // Also add touchstart stopPropagation on the buttons themselves to prevent
    // KLineChart's canvas touchstart from consuming the gesture.
    var _aBtn = document.getElementById('yaxis-auto-btn');
    var _lBtn = document.getElementById('yaxis-log-btn');
    function _btnBlockTouch(e) { e.stopPropagation(); }
    _aBtn.addEventListener('touchstart', _btnBlockTouch, { passive: false });
    _lBtn.addEventListener('touchstart', _btnBlockTouch, { passive: false });
    _aBtn.addEventListener('pointerdown', _btnBlockTouch, { passive: false });
    _lBtn.addEventListener('pointerdown', _btnBlockTouch, { passive: false });
    _aBtn.addEventListener('pointerup', function(e) {
        e.stopPropagation();
        if (_logScaleEnabled) toggleLogScale();
    });
    _lBtn.addEventListener('pointerup', function(e) {
        e.stopPropagation();
        if (!_logScaleEnabled) toggleLogScale();
    });
    // Keep click for desktop mouse
    _aBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (_logScaleEnabled) toggleLogScale();
    });
    _lBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (!_logScaleEnabled) toggleLogScale();
    });
    // Invert button. Unlike A/L (state-guarded, so pointerup+click double-fire
    // is harmless) this is a pure toggle — debounce the double-fire.
    var _iBtn = document.getElementById('yaxis-invert-btn');
    var _invLastTs = 0;
    function _invToggleOnce(e) {
        e.stopPropagation();
        var now = Date.now();
        if (now - _invLastTs < 350) return;
        _invLastTs = now;
        toggleInvertScale();
    }
    _iBtn.addEventListener('touchstart', _btnBlockTouch, { passive: false });
    _iBtn.addEventListener('pointerdown', _btnBlockTouch, { passive: false });
    _iBtn.addEventListener('pointerup', _invToggleOnce);
    _iBtn.addEventListener('click', _invToggleOnce);
    // Chart rebuilds reset styles to defaults while the session flag survives —
    // reassert so the pill and the axis can't drift apart.
    if (_invertScaleEnabled && klineChart) { try { klineChart.setStyles({ yAxis: { reverse: true } }); } catch(e) {} }
}

function renderKlineChart(symbol, data, timeframe) {
    // Capture viewport from the OUTGOING chart BEFORE _lastRendered* is overwritten
    // and BEFORE the dispose below — a same-symbol candle-close reload should keep
    // the user's scroll/zoom if they've scrolled back into history.
    var _vpSnap = _captureChartViewport(symbol, timeframe);
    _lastRenderKlineChartTime = Date.now();
    _lastRenderedSymbol = symbol;
    _lastRenderedTimeframe = timeframe;
    _chartDisplayedSymbol = symbol;       // recovery does NOT null this (viewport-preserve key)
    _chartDisplayedTimeframe = timeframe;
    var container = document.getElementById('kline-chart-container');
    if (!container || !data.ohlcv || data.ohlcv.length === 0) {
        if (container) container.innerHTML = '<div class="chart-loading">No data available</div>';
        if (window._chartOverlayWatchdog) { clearTimeout(window._chartOverlayWatchdog); window._chartOverlayWatchdog = null; }
        return;
    }

    // Remove loading/error overlays if present
    var loadingOverlay = container.querySelector('.chart-loading-overlay');
    if (loadingOverlay) loadingOverlay.remove();
    if (window._chartOverlayWatchdog) { clearTimeout(window._chartOverlayWatchdog); window._chartOverlayWatchdog = null; }
    var errOverlay = container.querySelector('.chart-error-overlay');
    if (errOverlay) errOverlay.remove();

    // Reset fullscreen state (chart is about to be destroyed and recreated)
    if (typeof _fullscreenPaneId !== 'undefined' && _fullscreenPaneId) {
        _fullscreenPaneId = null;
        if (typeof _toggleCollapsedPanes === 'function') _toggleCollapsedPanes(true, null);
        if (typeof _hidePaneFsBtn === 'function') _hidePaneFsBtn();
    }

    container.innerHTML = '';

    // Check if KLineChart is available
    if (typeof klinecharts === 'undefined') {
        container.innerHTML = '<div class="chart-loading">Chart library not loaded</div>';
        return;
    }

    // Dispose existing chart
    if (klineChart && container) {
        try {
            klinecharts.dispose(container);
        } catch (e) {
        }
        klineChart = null;
        _crosshairMode = false; _crosshairPos = null;
    }

    // Store server-side RSI data for custom RSI indicator
    window._serverRsiData = _normalizeRsi(data.rsi) || [];

    // Register custom RSI indicator (server-side Wilder's RSI + RSI-MA).
    // Replaces the built-in RSI with a TradingView-style two-line version.
    // Register derivative line indicators for CVD/OI panes.
    // createOverlay cannot create new panes in KLineChart v9.8.12 — only createIndicator can.
    // Each pane type gets its own indicator with a visible shortName and value display.
    // Actual colored segments are drawn as overlays on top.
    // PERF: these are GLOBAL klinecharts registrations (survive dispose) whose calc/draw
    // closures read live window._* globals + static lists — so register ONCE per page load
    // instead of on every chart open (was re-running 35+ registrations each open).
    // The 14 custom indicator registrations moved VERBATIM to
    // chart-indicator-defs.js — same one-time gate,
    // still re-tried per render until window._chartTypesRegistered is set
    // at the end of the chart-init block below.
    registerCarracIndicatorDefs();

    // _selectionSquares moved to chart-position-overlays.js.

    // Register rect overlay for interactive drawing (not built-in in KLineChart v9)
    if (!klineChart) {
        // The whole one-time overlay-registration block (1,579 lines) moved
        // VERBATIM to chart-overlay-defs.js. It still
        // sets window._chartTypesRegistered at the end, so the per-render
        // retry semantics are unchanged.
        registerCarracOverlayDefs();
    }

    // Freehand context menu (delete)
    function _showFreehandContextMenu(x, y, fhId) {
        var old = document.getElementById('freehand-ctx-menu');
        if (old) old.remove();
        var menu = document.createElement('div');
        menu.id = 'freehand-ctx-menu';
        menu.className = 'drawing-context-menu visible';
        menu.innerHTML = '<div class="ctx-menu-item" data-action="delete-freehand"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg> Delete Freehand</div>';
        menu.style.left = Math.min(x, window.innerWidth - 170) + 'px';
        menu.style.top = Math.min(y, window.innerHeight - 40) + 'px';
        document.body.appendChild(menu);
        menu.addEventListener('click', function(e) {
            var item = e.target.closest('[data-action]');
            if (item && item.getAttribute('data-action') === 'delete-freehand') {
                if (typeof freehandDelete === 'function') freehandDelete(fhId);
            }
            menu.remove();
        });
        setTimeout(function() {
            document.addEventListener('mousedown', function _fhClose(e) {
                if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('mousedown', _fhClose); }
            });
        }, 50);
    }

    // Track mouse position on chart for context menu placement
    container.addEventListener('mousemove', function(e) {
        container._lastMouseX = e.clientX;
        container._lastMouseY = e.clientY;
    });

    // Prevent browser context menu on chart; show drawing context menu if an overlay is selected
    container.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        container._lastMouseX = e.clientX;
        container._lastMouseY = e.clientY;
        // Check freehand hit first
        if (typeof freehandHitTest === 'function') {
            var rect = container.getBoundingClientRect();
            var fhId = freehandHitTest(e.clientX - rect.left, e.clientY - rect.top);
            if (fhId) {
                _showFreehandContextMenu(e.clientX, e.clientY, fhId);
                return;
            }
        }
        // Hit-test all locked overlays (locked overlays don't fire KLineChart onRightClick)
        var rect2 = container.getBoundingClientRect();
        var hitId = getLockedOverlayAtPoint(e.clientX - rect2.left, e.clientY - rect2.top);
        if (hitId) {
            showDrawingContextMenu(e.clientX, e.clientY, hitId);
            return;
        }
        // Fallback: if an overlay is selected, show context menu on right-click anywhere on chart
        if (klineSelectedOverlayId) {
            showDrawingContextMenu(e.clientX, e.clientY, klineSelectedOverlayId);
            return;
        }
        // Empty chart area: show Paste if clipboard has content for this symbol
        if (typeof _drawingClipboard !== 'undefined' && _drawingClipboard && _drawingClipboard.symbol === klineChartSymbol) {
            showChartContextMenu(e.clientX, e.clientY);
        }
    });

    // Create KLineChart with TradingView-like dark theme
    var chartFont = "'JetBrains Mono', monospace";
    // Secondary multi-chart panes don't have a toolbar overlay, so reduce tooltip offset
    var _mcPane = container.closest('.mc-pane');
    var _isSecondaryPane = _mcPane && _mcPane.getAttribute('data-mc-pane') !== '0';
    var _isMultiChart = typeof mcIsMultiChart === 'function' && mcIsMultiChart();
    var _compactOHLCV = _isSecondaryPane || _isMultiChart;
    var _tooltipOffsetTop = _isSecondaryPane ? 6 : 36;
    // Refine with the measured toolbar/pane overlap so there's no wrong-offset
    // flash before the first chrome-inset sync (mcUpdateChromeInsets) runs.
    try {
        var _tbEl = document.querySelector('.adv-chart-toolbar');
        if (_tbEl && _mcPane) {
            var _ovl = Math.max(0, Math.round(_tbEl.getBoundingClientRect().bottom - _mcPane.getBoundingClientRect().top));
            _tooltipOffsetTop = Math.max(6, _ovl + 4);
        }
    } catch (e) {}
    klineChart = klinecharts.init(container, {
        customApi: {
            formatDate: function(dateTimeFormat, timestamp, format, type) {
                // type: 0 = tooltip, 1 = crosshair, 2 = xAxis
                // This is called for ALL visible positions including the empty right area,
                // enabling future date labels on the X-axis and crosshair.
                var d = new Date(timestamp);
                var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                var mon = months[d.getMonth()];
                var day = d.getDate();
                var year = d.getFullYear();
                var hh = ('0' + d.getHours()).slice(-2);
                var mm = ('0' + d.getMinutes()).slice(-2);
                var tf = klineChartTimeframe || '1D';
                var isIntraday = ['1m','3m','5m','15m','30m','1h','4h'].indexOf(tf) >= 0;

                if (type === 1) {
                    // Crosshair: always show full date + time for intraday, date for daily+
                    return isIntraday
                        ? mon + ' ' + day + ', ' + year + '  ' + hh + ':' + mm
                        : mon + ' ' + day + ', ' + year;
                }
                // xAxis ticks (type 2) and tooltip (type 0)
                if (isIntraday) {
                    // Show date when it changes, otherwise just time
                    return mon + ' ' + day + '  ' + hh + ':' + mm;
                }
                return mon + ' ' + day;
            },
            formatBigNumber: function(value) {
                var v = parseFloat(value);
                if (isNaN(v)) return value;
                if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
                if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
                if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
                return v.toFixed(0);
            },
            formatPrice: function(price, precision) {
                // In log scale mode, candle OHLC values are in ln() space.
                // Use tracked data range to distinguish candle values from sub-pane
                // indicators (RSI 0-100, volume in thousands, MACD near zero).
                if (_logScaleEnabled && _logScaleDataMax > _logScaleDataMin) {
                    var range = _logScaleDataMax - _logScaleDataMin;
                    var margin = Math.max(range * 0.5, 1);
                    if (price >= _logScaleDataMin - margin && price <= _logScaleDataMax + margin) {
                        var real = Math.exp(price);
                        if (real >= 100) return real.toFixed(2);
                        if (real >= 1) return real.toFixed(2);
                        if (real >= 0.01) return real.toFixed(4);
                        return real.toPrecision(4);
                    }
                }
                // Enforce .00 for prices >= 100 (BTC, ETH, etc.)
                if (price >= 100) return price.toFixed(Math.max(precision, 2));
                if (price >= 1) return price.toFixed(Math.max(precision, 2));
                if (price >= 0.01) return price.toFixed(Math.max(precision, 4));
                return price.toFixed(precision);
            }
        },
        styles: {
            grid: {
                show: true,
                horizontal: { color: '#2a2e39' },
                vertical: { color: '#2a2e39' }
            },
            candle: {
                type: (function() { try { var t = localStorage.getItem('klineChartType') || 'candle_solid'; return t === 'heikin_ashi' ? 'candle_solid' : t; } catch(e) { return 'candle_solid'; } })(),
                priceMark: {
                    show: true,
                    high: { show: true, color: '#787b86', textFamily: chartFont, textSize: 10 },
                    low: { show: true, color: '#787b86', textFamily: chartFont, textSize: 10 },
                    last: {
                        // Disabled — the native last-price mark anchors to
                        // the phantom tail bar (open==close after each tick → collapses
                        // to gray/absent). Replaced by the custom CARRAC_LAST_PRICE
                        // indicator, which reads the real (phantom-skipped) last bar.
                        show: false,
                        upColor: '#26a69a',
                        downColor: '#ef5350',
                        noChangeColor: '#787b86',
                        line: { show: true, style: 'dashed', dashedValue: [4, 4], size: 1 },
                        text: {
                            show: false,
                            style: 'fill',
                            color: '#ffffff',
                            size: 11,
                            family: chartFont,
                            weight: 'bold',
                            paddingLeft: 4,
                            paddingRight: 4,
                            paddingTop: 2,
                            paddingBottom: 2,
                            borderRadius: 2
                        }
                    }
                },
                bar: (typeof CHART_THEMES !== 'undefined' && CHART_THEMES[getThemeId()]) ? CHART_THEMES[getThemeId()].candle : {
                    upColor: '#d1d4dc',
                    downColor: '#4a4e59',
                    upBorderColor: '#d1d4dc',
                    downBorderColor: '#4a4e59',
                    upWickColor: '#d1d4dc',
                    downWickColor: '#4a4e59'
                },
                tooltip: {
                    offsetTop: _tooltipOffsetTop,
                    offsetLeft: 10,
                    text: { family: chartFont, size: _compactOHLCV ? 10 : 12, weight: 'italic' },
                    showRule: ('ontouchstart' in window || navigator.maxTouchPoints > 0) ? 'none' : 'always',
                    // Desktop candle tooltip is showRule:'always', so when the crosshair
                    // goes idle (mouse off the plot, e.g. reaching for the toolbar) KLineChart
                    // falls back to dataList[len-1] — a PHANTOM future bar (flat O==H==L==C,
                    // no volume, future date). Resolve the target bar in a custom fn and, if
                    // it's a phantom, substitute the last REAL bar (phantom-skipped, via
                    // _lastRealBar) so the idle legend shows today's real bar, TradingView-
                    // style. For real (hovered) bars we return the library's own {token}s
                    // untouched, so formatting stays byte-identical to the old static array.
                    // Signature verified against the bundled klinecharts v9.8.12 min build:
                    // custom is invoked as custom(data, styles) with data = {prev, current, next}.
                    custom: function(data, styles) {
                        var titles = _compactOHLCV
                            ? ['T: ', 'O: ', 'H: ', 'L: ', 'C: ', 'V: ']
                            : ['time', 'open', 'high', 'low', 'close', 'volume'];
                        var vals = ['{time}', '{open}', '{high}', '{low}', '{close}', '{volume}'];
                        var bar = data && data.current;
                        var isPhantom = !!(bar && (bar._phantom ||
                            (typeof bar.timestamp === 'number' && bar.timestamp > Date.now())));
                        if (isPhantom && klineChart && typeof klineChart.getDataList === 'function') {
                            var dl = klineChart.getDataList();
                            var real = _lastRealBar(dl);
                            if (real) {
                                var dec = _computeChartPricePrecision(dl);
                                vals = [
                                    _fmtTooltipTime(real.timestamp),
                                    _fmtTooltipPrice(real.open, dec),
                                    _fmtTooltipPrice(real.high, dec),
                                    _fmtTooltipPrice(real.low, dec),
                                    _fmtTooltipPrice(real.close, dec),
                                    _mFmtV(real.volume)
                                ];
                            }
                        }
                        var rows = [];
                        for (var _r = 0; _r < 6; _r++) rows.push({title: titles[_r], value: vals[_r]});
                        return rows;
                    }
                }
            },
            indicator: {
                lastValueMark: {
                    show: false,
                    text: {
                        show: true,
                        style: 'fill',
                        color: '#ffffff',
                        size: 11,
                        family: chartFont,
                        weight: 'normal',
                        paddingLeft: 4,
                        paddingRight: 4,
                        paddingTop: 2,
                        paddingBottom: 2,
                        borderRadius: 2
                    }
                },
                tooltip: {
                    text: { family: chartFont, size: 12 }
                }
            },
            xAxis: {
                axisLine: { color: '#2a2e39' },
                tickLine: { color: '#2a2e39' },
                tickText: { color: '#787b86', family: chartFont, size: 11 }
            },
            yAxis: {
                size: 'auto',
                axisLine: { color: '#2a2e39' },
                tickLine: { color: '#2a2e39' },
                tickText: { color: '#787b86', family: chartFont, size: 11, marginStart: 2, marginEnd: 4 }
            },
            crosshair: {
                show: true,
                horizontal: {
                    line: { color: '#787b86', style: 'dashed' },
                    text: { family: chartFont, size: 11 }
                },
                vertical: {
                    line: { color: '#787b86', style: 'dashed' },
                    text: { family: chartFont, size: 11 }
                }
            },
            overlay: {
                point: { color: '#2962ff', borderColor: '#ffffff', borderSize: 1.5, radius: 5, activeColor: '#2962ff', activeBorderColor: '#ffffff', activeBorderSize: 1.5, activeRadius: 6 },
                text: { family: chartFont, size: 12, backgroundColor: 'transparent', borderColor: 'transparent' },
                rectText: {
                    style: 'fill',
                    color: '#d1d4dc',
                    backgroundColor: '#2a2e39',
                    borderColor: '#363a45',
                    borderSize: 1,
                    borderRadius: 2,
                    size: 10,
                    family: chartFont
                }
            },
            separator: {
                size: 1,
                color: '#2a2e39',
                activeBackgroundColor: 'rgba(63,185,80,0.3)'
            }
        }
    });

    // Convert data format for KLineChart
    var klineData = convertToKlineData(data);
    _storeKlineData(klineData);

    // Apply data (use HA if that chart type is active)
    var _isHA = false;
    try { _isHA = localStorage.getItem('klineChartType') === 'heikin_ashi'; } catch(e) {}
    if (_isHA) {
        klineChart.applyNewData(klineData);
        _applyHeikinAshi(true);
    } else {
        klineChart.applyNewData(klineData);
    }
    _applyChartPricePrecision(klineData);

    // Auto-size yAxis width to fit price labels for this asset
    autoSizeYAxis();

    // Apply full theme from the theme system (handles colors, background, grid, etc.)
    if (typeof _applyThemeToChart === 'function') {
        _applyThemeToChart(getThemeId());
    }

    // TradingView-like viewport: consistent bar spacing + right margin + scroll to latest.
    // Same zoom-preservation rule as fastSwapChartData: keep the user's barSpace on any
    // same-symbol rebuild (recovery, tab-return); default only on a real asset/TF switch.
    klineChart.setBarSpace(_vpSnap && !_vpSnap.isSwitch && _vpSnap.barSpace ? _vpSnap.barSpace : 8);
    try { klineChart.setLeftMinVisibleBarCount(2); klineChart.setRightMinVisibleBarCount(0); } catch(e) {}
    // Preserve the user's scroll/zoom on a same-symbol refresh if they scrolled back; else snap.
    var _vpRestoredFull = _restoreChartViewport(_vpSnap);
    if (!_vpRestoredFull) _scrollToLastReal();
    // Deferred re-apply — applyNewData triggers async layout, the first restore can fire
    // too early on a fresh init (same hardening fastSwapChartData already has; recovery
    // and tab-return force THIS full-rebuild path, so it needs it most).
    var _renderGen = _chartLoadGeneration;
    requestAnimationFrame(function() {
        if (!klineChart || _renderGen !== _chartLoadGeneration) return;
        if (_vpRestoredFull) { _restoreChartViewport(_vpSnap); } else { _scrollToLastReal(); }
    });

    // Setup Y-axis touch drag zoom for mobile
    setupYAxisTouchZoom();
    setupXAxisTouchZoom();
    setupChartTouchPan();

    // Mobile: update HTML OHLCV display (canvas tooltip hidden)
    if ('ontouchstart' in window || navigator.maxTouchPoints > 0) {
        setupMobileOhlcvDisplay();
    }

    // Inject A/L scale toggle on Y-axis (TradingView-style)
    injectYAxisScaleToggle();

    // Amplify y-axis drag zoom on small sub-panes (RSI, VOL, MACD, derivatives).
    // KLineChart's zoom uses (currentY - startY) / paneHeight — small panes need
    // amplified movement to feel responsive. On mousedown in a sub-pane, we intercept
    // subsequent mousemove events and re-dispatch with amplified clientY delta.
    (function() {
        var _ampDrag = null;
        var _ampEvents = new WeakSet();
        var MULT = 3;
        container.addEventListener('mousedown', function(e) {
            if (e.button !== 0) { _ampDrag = null; return; }
            var el = e.target;
            while (el && el.parentElement !== container) el = el.parentElement;
            if (!el || el.offsetHeight >= 200) { _ampDrag = null; return; }
            // Only amplify on Y-axis area (right edge) of sub-panes, not chart area
            var rect = el.getBoundingClientRect();
            var xFromRight = rect.right - e.clientX;
            if (xFromRight > 60) { _ampDrag = null; return; }  // Not on Y-axis
            _ampDrag = { anchorY: e.clientY };
        }, true);
        container.addEventListener('mousemove', function(e) {
            if (!_ampDrag || _ampEvents.has(e) || e.buttons !== 1) {
                if (e.buttons !== 1) _ampDrag = null;
                return;
            }
            var realDelta = e.clientY - _ampDrag.anchorY;
            var ampY = _ampDrag.anchorY + realDelta * MULT;
            e.stopImmediatePropagation();
            e.preventDefault();
            var synth = new MouseEvent('mousemove', {
                clientX: e.clientX, clientY: ampY,
                screenX: e.screenX, screenY: e.screenY + (ampY - e.clientY),
                buttons: e.buttons, bubbles: true, cancelable: true
            });
            _ampEvents.add(synth);
            e.target.dispatchEvent(synth);
        }, true);
        document.addEventListener('mouseup', function() { _ampDrag = null; }, true);
    })();

    // --- Candle Countdown Timer ---
    var countdownEl = document.getElementById('kline-candle-countdown');
    if (!countdownEl) {
        countdownEl = document.createElement('div');
        countdownEl.id = 'kline-candle-countdown';
        countdownEl.className = 'candle-countdown';
        container.appendChild(countdownEl);
    }
    if (window._candleCountdownInterval) clearInterval(window._candleCountdownInterval);
    window._candleCountdownInterval = setInterval(updateCandleCountdown, 1000);
    updateCandleCountdown();

    // Dynamic crosshair color: green/red only when cursor is inside candle body
    var _lastCrosshairColor = '#787b86';
    var _crosshairRaf = null;
    function setCrosshairColor(color, chart) {
        var ch = chart || (typeof mcGetChartForContainer === 'function' ? mcGetChartForContainer(container) : klineChart);
        if (!ch) return;
        // Track per-chart to avoid redundant setStyles
        if (!chart && color === _lastCrosshairColor) return;
        if (!chart) _lastCrosshairColor = color;
        var isDefault = color === '#787b86';
        var textBg = isDefault ? '#373b42' : color;
        ch.setStyles({
            crosshair: {
                horizontal: {
                    line: { color: color },
                    text: { backgroundColor: textBg, borderColor: textBg }
                },
                vertical: {
                    line: { color: color },
                    text: { backgroundColor: textBg, borderColor: textBg }
                }
            }
        });
    }
    container.addEventListener('mousemove', function(e) {
        if (_crosshairRaf) return;
        _crosshairRaf = requestAnimationFrame(function() {
            _crosshairRaf = null;
            var ch = (typeof mcGetChartForContainer === 'function') ? mcGetChartForContainer(container) : klineChart;
            if (!ch) return;
            try {
                var rect = container.getBoundingClientRect();
                var x = e.clientX - rect.left;
                var y = e.clientY - rect.top;
                var point = ch.convertFromPixel({ x: x, y: y }, { paneId: 'candle_pane' });
                if (!point || point.dataIndex === undefined || point.dataIndex === null) {
                    setCrosshairColor('#787b86');
                    return;
                }
                var dataList = ch.getDataList();
                var candle = dataList && dataList[point.dataIndex];
                if (!candle) { setCrosshairColor('#787b86'); return; }
                var cursorPrice = point.value;
                var bodyHigh = Math.max(candle.open, candle.close);
                var bodyLow = Math.min(candle.open, candle.close);
                if (cursorPrice !== undefined && cursorPrice >= bodyLow && cursorPrice <= bodyHigh) {
                    setCrosshairColor(candle.close >= candle.open ? '#26a69a' : '#ef5350');
                } else {
                    setCrosshairColor('#787b86');
                }
            } catch(ex) { setCrosshairColor('#787b86'); }
        });
    });
    container.addEventListener('mouseleave', function() {
        setCrosshairColor('#787b86');
    });
    // Expose crosshair color helper for multi-chart crosshair sync
    window._setCrosshairColorForChart = function(chart, color) {
        if (!chart || !chart.setStyles) return;
        var isDefault = color === '#787b86';
        var textBg = isDefault ? '#373b42' : color;
        chart.setStyles({
            crosshair: {
                horizontal: { line: { color: color }, text: { backgroundColor: textBg, borderColor: textBg } },
                vertical: { line: { color: color }, text: { backgroundColor: textBg, borderColor: textBg } }
            }
        });
    };

    // Overlay outlier filter — block overlays with extreme values that would
    // squeeze the Y-axis (e.g., negative prices, CPR levels far from current price).
    // This intercepts ALL createOverlay calls regardless of source.
    // NOTE: Uses the mutable _overlayYMax global (updated by _updateOverlayYMax) so that
    // fastSwapChartData can refresh the cap for a new asset without reinstalling the filter.
    (function _installOverlayFilter() {
        if (!klineChart || klineChart._overlayFilterInstalled) return;
        _updateOverlayYMax();
        var _origCreate = klineChart.createOverlay.bind(klineChart);
        klineChart.createOverlay = function(opts, paneId) {
            // Only filter candle_pane overlays (sub-pane CVD/OI values can be negative/large)
            if (_overlayYMax > 0 && (!paneId || paneId === 'candle_pane') && opts && opts.points) {
                for (var pi = 0; pi < opts.points.length; pi++) {
                    var v = opts.points[pi].value;
                    if (v != null && (v < 0 || v > _overlayYMax)) {
                        console.warn('[OverlayFilter] BLOCKED:', opts.id || opts.name, 'value=' + v, 'yMax=' + _overlayYMax);
                        return null;
                    }
                }
            }
            // Hide control points on locked overlays (indicators, not user drawings)
            if (opts && opts.lock) {
                opts.styles = opts.styles || {};
                opts.styles.point = {
                    color: 'transparent', borderColor: 'transparent', borderSize: 0,
                    radius: 0, activeColor: 'transparent', activeBorderColor: 'transparent',
                    activeBorderSize: 0, activeRadius: 0
                };
            }
            return _origCreate(opts, paneId);
        };
        klineChart._overlayFilterInstalled = true;
    })();

    // Add indicators (shared helper)
    applyKlineIndicators(data, klineData);

    // VP_NATIVE indicator on candle_pane — create BEFORE resize cascade
    // so createIndicator doesn't fire after layout is settled
    _vpIndicatorCreated = false;
    vpEnsureIndicator();
    if (klineActiveIndicators.VP) vpStart();

    // Freehand native indicator on candle_pane
    _freehandIndicatorCreated = false;
    freehandEnsureIndicator();

    // Key Levels native indicator on candle_pane
    _klIndicatorCreated = false;
    klEnsureIndicator();
    if (klineActiveIndicators.KEY_LEVELS || klineActiveIndicators.MON_RANGE) klStart();
    if (klineActiveIndicators.CONFLUENCE) confluenceStart();
    if (klineActiveIndicators.LIQ_HEATMAP) liqHeatmapStart();

    // Force resize after DOM settles — critical for split view where container
    // dimensions may not be final when init() runs. Without this, indicator panes
    // (RSI, VOL, etc.) can render at 0 height.
    // ResizeObserver handles ongoing layout shifts; single rAF resize for immediate fit
    _ensureChartResizeObserver();
    requestAnimationFrame(function() {
        if (klineChart) klineChart.resize();
    });

    // Landscape mode: compact indicator panes to maximize chart area
    _adjustLandscapePanes();

    // Load saved FRVP ranges (always, independent of VP toggle)
    setTimeout(function() { frvpLoad(); }, 400);

    // Refresh drawer + alerts
    if (typeof updateDrawerAll === 'function') updateDrawerAll(); // scenarios.js absent on the standalone page
    updateAlertBadge();

    // Click handler for the play path tool.
    container.addEventListener('click', function(e) {
        if (typeof _playPathDrawMode !== 'undefined' && _playPathDrawMode) {
            _handlePlayPathClick(e);
        }
    });

    // Click-to-set-price for alert panel
    container.addEventListener('click', function(e) {
        if (!klineAlertClickMode || !klineChart) return;
        var rect = container.getBoundingClientRect();
        var y = e.clientY - rect.top;
        try {
            var price = klineChart.convertFromPixel({ y: y }, { paneId: 'candle_pane' });
            if (price && price.value !== undefined) {
                var input = document.getElementById('alert-price-input');
                if (input) input.value = price.value.toFixed(price.value >= 1000 ? 2 : price.value >= 1 ? 4 : 6);
            }
        } catch(ex) { /* convertFromPixel may not support this format */ }
    });

    // Migrate old per-TF drawing keys to asset-only keys (one-time), then load.
    // These live in LAZILY-loaded kline-drawings.js; the .catch fallbacks in
    // openAdvancedChartModal/loadKlineChart run this impl even when that load
    // failed, so a bare call here was the ReferenceError seen in error reports. Guard like klineContextMenu above.
    if (typeof migrateKlineDrawingsToAssetKeys === 'function') migrateKlineDrawingsToAssetKeys();
    if (!klineDrawingsHidden) {
        if (typeof loadKlineDrawings === 'function') loadKlineDrawings();
        // Fetch any drawings from server that may have been created on another device
        if (typeof loadDrawingsFromServer === 'function') loadDrawingsFromServer(true);
    }

    // Load scenarios for current asset+TF
    if (typeof loadPlaysForChart === 'function') loadPlaysForChart(klineChartSymbol, klineChartTimeframe);
    // Render alert overlays AFTER drawings to avoid stale overlays or duplicates
    renderAlertOverlays();

    // WS1c: emit switch-complete breadcrumb for the full-render (fetch/miss) path.
    // _switchT0 is set in loadKlineChart; renderKlineChart is the synchronous
    // render from the fetch response so this fires after all overlays are scheduled.
    if (typeof window.carracLog === 'function') {
        window.carracLog('switch', klineChartSymbol + ' ' + klineChartTimeframe + ' miss ' + Math.round(performance.now() - _switchT0) + 'ms', { sym: klineChartSymbol, tf: klineChartTimeframe, cacheHit: false });
    }

    // Add fullscreen toggle icons to sub-panes (after layout settles)
    setTimeout(function() { if (typeof updatePaneFullscreenButtons === 'function') updatePaneFullscreenButtons(); }, 500);
    // Init overlay hover cursor detection
    if (typeof _initOverlayHoverDetection === 'function') _initOverlayHoverDetection();
    // Sync multi-chart pane state
    if (typeof mcSyncActiveChart === 'function') mcSyncActiveChart();
}
