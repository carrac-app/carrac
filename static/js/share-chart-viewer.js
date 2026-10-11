// Interactive frozen-chart share viewer.
// Renders the share snapshot (embedded as non-executable JSON) with the SAME
// stack the app uses — klinecharts + carrac-indicator-pack + the shared
// frozen-drawing renderer. Read-only and fully offline: pan/zoom/crosshair
// work, data never updates ("live = register"). On ANY boot failure it swaps
// in the static hero image, i.e. exactly the pre-viewer page.

/**
 * Pure viewport-restore decision — no DOM; tested via
 * tests/js/share-viewer-viewport.test.cjs.
 * @param {number[]} candleTimesSec frozen candle times (epoch seconds, asc)
 * @param {Object|null} viewport {from_ts, to_ts} epoch seconds
 * @param {number} chartWidth container width in px
 * @returns {{barSpace: number, scrollTs: number}|null} null = keep default fit
 */
function decideShareViewport(candleTimesSec, viewport, chartWidth) {
    if (!viewport || typeof viewport.from_ts !== 'number' ||
        typeof viewport.to_ts !== 'number' ||
        !Array.isArray(candleTimesSec) || !candleTimesSec.length) {
        return null;
    }
    var n = 0;
    for (var i = 0; i < candleTimesSec.length; i++) {
        if (candleTimesSec[i] >= viewport.from_ts &&
            candleTimesSec[i] <= viewport.to_ts) n++;
    }
    if (n < 2) return null;
    var barSpace = Math.max(2, Math.min(26, Math.round(chartWidth / n)));
    return { barSpace: barSpace, scrollTs: viewport.to_ts * 1000 };
}

(function() {
    'use strict';
    if (typeof window === 'undefined') return;   // node:test import path

    // Mirrors display-options.js CHART_THEMES (candle/bg/grid/axis subset the
    // viewer needs). Kept inline: display-options.js is app-session-coupled.
    var THEMES = {
        classic_dark: { bg: '#131722', up: '#d1d4dc', down: '#4a4e59', grid: '#2a2e39', axis: '#787b86', axisLine: '#2a2e39' },
        trading_dark: { bg: '#131722', up: '#26a69a', down: '#ef5350', grid: '#2a2e39', axis: '#787b86', axisLine: '#2a2e39' },
        light_trading: { bg: '#ffffff', up: '#26a69a', down: '#ef5350', grid: '#e0e0e0', axis: '#666666', axisLine: '#cccccc' },
        light_classic: { bg: '#ffffff', up: '#333333', down: '#aaaaaa', grid: '#e0e0e0', axis: '#666666', axisLine: '#cccccc' }
    };

    function _ph(ev, props) {
        try {
            if (window.posthog && typeof window.posthog.capture === 'function') {
                window.posthog.capture(ev, props || {});
            }
        } catch (e) {}
    }

    function _fallbackToHero(container) {
        var hero = container.getAttribute('data-hero');
        if (hero) {
            container.outerHTML =
                '<img class="capture" src="' + hero.replace(/"/g, '&quot;') +
                '" alt="Shared chart" loading="eager" style="width:100%;height:auto;display:block">';
        } else {
            container.innerHTML =
                '<div style="color:#8b949e;text-align:center;padding:60px 0">Chart unavailable.</div>';
        }
    }

    function init() {
        var container = document.getElementById('share-viewer');
        var dataEl = document.getElementById('chart-share-data');
        if (!container || !dataEl) return;
        try {
            boot(container, JSON.parse(dataEl.textContent || '{}'));
        } catch (e) {
            _fallbackToHero(container);
        }
    }

    function boot(container, snap) {
        var candles = snap.candles || [];
        if (!candles.length || typeof klinecharts === 'undefined') {
            _fallbackToHero(container);
            return;
        }
        var theme = THEMES[snap.theme] || THEMES.trading_dark;
        container.style.background = theme.bg;

        var chart = klinecharts.init(container, {
            styles: {
                grid: { show: true,
                        horizontal: { color: theme.grid },
                        vertical: { color: theme.grid } },
                candle: {
                    type: 'candle_solid',
                    priceMark: { show: true },
                    bar: { upColor: theme.up, downColor: theme.down,
                           upBorderColor: theme.up, downBorderColor: theme.down,
                           upWickColor: theme.up, downWickColor: theme.down }
                },
                indicator: { lastValueMark: { show: false } },
                xAxis: { axisLine: { color: theme.axisLine },
                         tickLine: { color: theme.axisLine },
                         tickText: { color: theme.axis } },
                yAxis: { axisLine: { color: theme.axisLine },
                         tickLine: { color: theme.axisLine },
                         tickText: { color: theme.axis } },
                separator: { size: 1, color: theme.axisLine }
            }
        });
        if (!chart) { _fallbackToHero(container); return; }
        window._shareChart = chart;   // debug access

        var klineData = candles.map(function(c) {
            return { timestamp: (c.time || 0) * 1000, open: c.open,
                     high: c.high, low: c.low, close: c.close,
                     volume: c.volume || 0 };
        });
        chart.applyNewData(klineData);

        // ── Indicators (frozen set; WYSIWYG) ──
        try { applyFrozenIndicators(chart, klineData, snap); } catch (e) {}

        // ── Drawings (frozen, PII-stripped) via the shared renderer ──
        try {
            if (window.KlineShareRender) {
                KlineShareRender.registerOverlays();
                KlineShareRender.renderFrozenDrawings(chart, snap.drawings || [], klineData);
            }
        } catch (e) {}

        // ── Viewport restore ──
        try {
            var times = candles.map(function(c) { return c.time || 0; });
            var d = decideShareViewport(times, snap.viewport, container.clientWidth || 800);
            if (d) {
                if (typeof chart.setBarSpace === 'function') chart.setBarSpace(d.barSpace);
                if (typeof chart.scrollToTimestamp === 'function') chart.scrollToTimestamp(d.scrollTs);
            }
        } catch (e) {}

        window.addEventListener('resize', function() {
            try { chart.resize(); } catch (e) {}
        });

        _ph('share_viewer_loaded', { symbol: snap.symbol, timeframe: snap.timeframe });
        var interacted = false;
        ['pointerdown', 'wheel', 'touchstart'].forEach(function(ev) {
            container.addEventListener(ev, function() {
                if (interacted) return;
                interacted = true;
                _ph('share_viewer_interacted', { symbol: snap.symbol, timeframe: snap.timeframe });
            }, { passive: true });
        });
    }

    // Built-in templates live in chart-indicator-defs.js (not loaded here) — klinecharts
    // bundles its own defaults for most, but under different names for
    // these two. STOCH_RSI has no bundled equivalent and stays absent.
    var BUILTIN_ALIASES = { WILLIAMS_R: 'WR', STOCH: 'KDJ' };

    function applyFrozenIndicators(chart, klineData, snap) {
        var frozen = Array.isArray(snap.indicators) ? snap.indicators : [];
        if (!frozen.length) return;
        var reg = window.IndicatorRegistry;
        var packMap = reg ? reg.getPackConfigMap() : {};
        var panes = reg ? reg.getBuiltInPanes() : {};
        var overlays = reg ? reg.getBuiltInOverlays() : {};
        var packOn = false;
        var missing = [];

        frozen.forEach(function(ind) {
            var key = ind && ind.key;
            if (!key) return;
            var params = ind.params || {};
            try {
                if (panes[key]) {
                    var nameP = BUILTIN_ALIASES[key] || key;
                    var cfgP = { name: nameP };
                    if (nameP === key && Array.isArray(params.calcParams)) cfgP.calcParams = params.calcParams;
                    var idP = chart.createIndicator(cfgP, false, { id: panes[key] });
                    if (!idP) missing.push(key);
                } else if (overlays[key]) {
                    var nameO = BUILTIN_ALIASES[key] || key;
                    var cfgO = { name: nameO };
                    if (nameO === key && Array.isArray(params.calcParams)) cfgO.calcParams = params.calcParams;
                    var idO = chart.createIndicator(cfgO, true, { id: 'candle_pane' });
                    if (!idO) missing.push(key);
                } else if (packMap[key] && window.indicatorPackConfig &&
                           window.indicatorPackConfig[packMap[key]]) {
                    var cfg = window.indicatorPackConfig[packMap[key]];
                    cfg.enabled = true;
                    Object.keys(params).forEach(function(pk) {
                        var v = params[pk];
                        if (typeof v === 'number' || typeof v === 'string' ||
                            typeof v === 'boolean') cfg[pk] = v;
                    });
                    packOn = true;
                } else {
                    missing.push(key);
                }
            } catch (e) {
                missing.push(key);
            }
        });

        if (missing.length) {
            _ph('share_viewer_indicator_missing', { keys: missing.join(','), symbol: snap.symbol, timeframe: snap.timeframe });
        }

        if (packOn && typeof drawIndicatorPack === 'function') {
            // Disable every pack entry NOT in the frozen set first (config
            // ships with defaults enabled).
            var wanted = {};
            frozen.forEach(function(ind) {
                if (ind && ind.key && packMap[ind.key]) wanted[packMap[ind.key]] = true;
            });
            Object.keys(window.indicatorPackConfig || {}).forEach(function(ck) {
                var entry = window.indicatorPackConfig[ck];
                if (entry && typeof entry === 'object' && 'enabled' in entry &&
                    !wanted[ck]) entry.enabled = false;
            });
            drawIndicatorPack(chart, klineData, snap.pack_data || {});
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

// Node test hook (tests/js/share-viewer-viewport.test.cjs); inert in browser.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { decideShareViewport: decideShareViewport };
}
