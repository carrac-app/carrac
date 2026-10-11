// --- Candle Countdown Timer ---
function getTimeframeDurationMs(tf) {
    var map = { '1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '1D': 86400000, '3D': 259200000, '1W': 604800000 };
    return map[tf] || 0;
}

// Bars carry their OPEN timestamp, so the next open is always
// lastTimestamp + one bar — no special case for END-labelled weekly/monthly
// bars. See tests/js/countdown-next-open.test.cjs.
function getNextCandleOpenMs(lastTimestamp, tf) {
    if (tf === '1M') {
        var d = new Date(lastTimestamp);
        var next = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1)).getTime();
        while (next <= Date.now()) {
            d = new Date(next);
            next = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1)).getTime();
        }
        return next;
    }
    var duration = getTimeframeDurationMs(tf);
    if (duration <= 0) return lastTimestamp + 86400000;
    var nextOpen = lastTimestamp + duration;
    while (nextOpen <= Date.now()) {
        nextOpen += duration;
    }
    return nextOpen;
}

function formatCountdown(ms, tf) {
    var totalSec = Math.floor(ms / 1000);
    var days = Math.floor(totalSec / 86400);
    var hours = Math.floor((totalSec % 86400) / 3600);
    var mins = Math.floor((totalSec % 3600) / 60);
    var secs = totalSec % 60;
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    if (days > 0) return days + 'd ' + pad(hours) + ':' + pad(mins);
    return pad(hours) + ':' + pad(mins) + ':' + pad(secs);
}

// --- Axis price source ---------------------------------------
// The tick path deliberately STOPS writing to the last real bar once that
// bar's period has elapsed: chartTickDecision case B (chart-live-tick.js)
// returns 'boundary-reload' and never 'inplace', because writing a live tick
// into a CLOSED candle would corrupt its OHLC. Correct for the candle — but
// it also froze the price the y-axis tag renders, so the tag sat on a stale
// close until fresh server data landed (30s+ at best; minutes when the SWR
// layer keeps serving a payload older than one bar period). That is the
// "price label gets behind" report.
//
// Fix: the axis tag shows the last TRADED price (TradingView behaviour), not
// a stale candle close. When the bar is known-stale and a live feed price
// exists, prefer the feed. Display-only — no bar is mutated here, so the
// OHLC-integrity guard above is untouched.

// Chart-space close → linear price. Under log scale convertToKlineData()
// stores Math.log(c) in `close` (and _applyInplaceTick keeps writing log
// values), so the raw close is ~11.08 for BTC — printing it unconverted was
// wrong on every log-scale chart.
function _carracLinear(v) {
    if (v == null || !isFinite(v)) return null;
    if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled) return Math.exp(v);
    return v;
}

function _carracFeedPrice(symbol) {
    if (!symbol) return null;
    try {
        // No allAssets on the standalone chart page — callers fall back.
        var a = (typeof allAssets !== 'undefined' && allAssets) ? allAssets[symbol] : null;
        if (!a) return null;
        // latest_price is what the WS handlers write on every tick; realtime
        // .price only refreshes on the periodic REST load, so it is staler.
        var p = a.latest_price;
        if (p == null || !isFinite(p)) p = a.realtime && a.realtime.price;
        return (p != null && isFinite(p) && p > 0) ? p : null;
    } catch (e) { return null; }
}

/**
 * Price the y-axis should display for `bar`, in LINEAR space.
 * Falls back to the bar's own close whenever the bar is fresh, no feed price
 * exists, or staleness can't be determined — i.e. never worse than before.
 * @returns {{price: number|null, stale: boolean, open: number|null}}
 */
function carracAxisPrice(bar, tf, symbol) {
    var close = bar ? _carracLinear(bar.close) : null;
    var open = bar ? _carracLinear(bar.open) : null;
    var out = { price: close, stale: false, open: open };
    if (!bar) return out;
    var barMs = (typeof getTimeframeDurationMs === 'function') ? (getTimeframeDurationMs(tf) || 0) : 0;
    if (barMs <= 0) return out;                       // unknown TF — leave as-is
    if (bar.timestamp + barMs > Date.now()) return out; // bar still forming — ticks own it
    out.stale = true;
    var feed = _carracFeedPrice(symbol);
    if (feed != null) out.price = feed;
    return out;
}

// Linear price → chart space (inverse of _carracLinear). Used to position
// anything that must line up with the drawn candles.
function carracToChartSpace(v) {
    if (v == null || !isFinite(v)) return v;
    if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && v > 0) return Math.log(v);
    return v;
}

// One-shot diagnostic. The display fix above is deliberately
// path-independent — it hides the symptom no matter WHAT left the bar stale —
// so without this we'd never learn which path arms it (the 1D sightings are
// still unexplained: prod's 1D SWR staleness maxes at ~20min against an
// 86400s bar, so the boundary trap alone can't account for them). Beacons the
// decision-relevant state once per page load, after the staleness has held
// long enough to have been visible, and only when the feed actually disagrees.
var _carracStaleSince = null;
var _carracStaleBeaconed = false;
function _carracNoteStale(axis, bar, tf, symbol, elId) {
    try {
        if (elId !== 'kline-candle-countdown') return;       // active pane only
        // A hidden tab never repaints, so "stale" here would be a
        // false alarm — nobody is looking at a wrong price. The 1s countdown
        // timer keeps running (throttled to ~once/minute) in a backgrounded
        // tab, which is exactly why the persistence window landed at 61s in
        // every reported case: the first throttled tick after crossing the
        // 60s mark fired the beacon while document.hidden was true. Reset the
        // clock instead of beaconing, so staleness has to hold for the full
        // 60s window again AFTER the tab becomes visible — not carry over
        // time accrued while backgrounded.
        if (typeof document !== 'undefined' && document.hidden) { _carracStaleSince = null; return; }
        if (!axis || !axis.stale) { _carracStaleSince = null; return; }
        var now = Date.now();
        if (_carracStaleSince == null) { _carracStaleSince = now; return; }
        if (_carracStaleBeaconed || now - _carracStaleSince < 60000) return;
        var feed = _carracFeedPrice(symbol);
        var frozen = _carracLinear(bar.close);
        if (feed == null || frozen == null) return;
        if (Math.abs(feed - frozen) / feed < 0.001) return;  // no visible lag yet
        _carracStaleBeaconed = true;
        var snap = {
            sym: symbol, tf: tf,
            barAgeS: Math.round((now - bar.timestamp) / 1000),
            barPeriodS: Math.round((getTimeframeDurationMs(tf) || 0) / 1000),
            staleForS: Math.round((now - _carracStaleSince) / 1000),
            frozenClose: frozen, feedPrice: feed,
            lagPct: +(((feed - frozen) / feed) * 100).toFixed(3),
            tickAgeMs: now - (window._lastChartUpdate || 0),
            boundary: window._chartBoundaryAttempt || null,
            rendered: window._lastRenderedSymbol,
            renderedTf: window._lastRenderedTimeframe,
            pan: [!!window._touchPanActive, !!window._chartMousePanning],
            mismatchSince: window._chartTickMismatchSince,
            logScale: !!(typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled),
            hidden: document.hidden
        };
        if (typeof window.carracLog === 'function') {
            window.carracLog('stale', 'axis price stale ' + snap.barAgeS + 's lag ' + snap.lagPct + '%');
        }
        if (typeof Sentry !== 'undefined' && Sentry.captureMessage) {
            Sentry.captureMessage('axis price stale: ' + tf, {
                level: 'warning',
                tags: { kind: 'axis_stale', sym: symbol, tf: tf },
                // Stringified: Sentry's normalizer collapsed a plain object to
                // "[VueViewModel]" when this was first instrumented by hand.
                extra: { snap: JSON.stringify(snap), crumbs: JSON.stringify((window.__carracLog || []).slice(-25)) }
            });
        }
    } catch (e) {}
}

// render-loop positioning for the active pane's y-axis tag.
// The 1s timer owns creation, text, colour and width; POSITION is pushed by
// CARRAC_LAST_PRICE draw() (chart-indicators.js) on every chart repaint with the line's
// own y, so tag and line cannot drift apart for more than a frame. The timer
// alone lagged every axis rescale by up to 1s, froze for the whole gesture
// during touch pans (_touchPanActive gate), and silently kept a stale top
// whenever chart.convertToPixel returned {} while still updating the text —
// the observed "right text, wrong place" detachment. Heights are cached by
// the timer tick so this per-frame path does no layout reads.
var _carracTagH = 16;
var _carracTagRectH = 32;
function carracPositionAxisTag(y) {
    if (y == null || !isFinite(y)) return;
    var el = document.getElementById('kline-candle-countdown');
    if (!el || el.style.display === 'none') return;
    var top = y - _carracTagH / 2;
    if (el.style.top !== top + 'px') el.style.top = top + 'px';
    window._carracAxisTagRect = { y: top, h: _carracTagRectH };
}

if (typeof window !== 'undefined') {
    window.carracAxisPrice = carracAxisPrice;
    window.carracToChartSpace = carracToChartSpace;
    window.carracPositionAxisTag = carracPositionAxisTag;
    // Inverse of carracToChartSpace (chart space → linear price). Exported as
    // the pair's twin so overlay renderers (alertLine y-axis tag) use the SAME
    // log decision as the countdown tag — two independent copies of "what is
    // the price" is exactly how a past mismatch happened.
    window.carracFromChartSpace = _carracLinear;
}

// Render countdown badge on a single chart instance.
// paneIdx is only passed for companion panes (multi-chart mode) — it enables
// the per-pane candle-close reload below.
function _updateCountdownForChart(chart, tf, containerEl, elId, paneIdx) {
    var el = document.getElementById(elId);
    if (!chart || !containerEl) {
        if (el) el.style.display = 'none';
        return;
    }
    // Create element if missing, or re-parent if it drifted to wrong container
    if (!el) {
        el = document.createElement('div');
        el.id = elId;
        el.className = 'candle-countdown';
        containerEl.appendChild(el);
    } else if (el.parentNode !== containerEl) {
        el.style.display = 'none';
        containerEl.appendChild(el);
    }

    // Hide when a sub-pane is fullscreen
    if (typeof _fullscreenPaneId !== 'undefined' && _fullscreenPaneId) {
        el.style.display = 'none'; return;
    }
    var dataList = chart.getDataList ? chart.getDataList() : [];
    if (dataList.length === 0) { el.style.display = 'none'; return; }

    // Skip phantom bars — check both future timestamps AND _phantom flag
    // (phantom bars become <= now when server data is stale)
    var _li = dataList.length - 1;
    var _now = Date.now();
    while (_li > 0 && (dataList[_li].timestamp > _now || dataList[_li]._phantom)) _li--;
    var lastCandle = dataList[_li];
    var nextOpen = getNextCandleOpenMs(lastCandle.timestamp, tf);
    var remaining = nextOpen - Date.now();
    // Which symbol feeds this pane: companion panes carry their own, the
    // active pane uses the globals.
    var _sym = (typeof paneIdx === 'number' && typeof _mcPanes !== 'undefined' && _mcPanes[paneIdx])
        ? _mcPanes[paneIdx].symbol
        : ((typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : null);
    var _axis = carracAxisPrice(lastCandle, tf, _sym);
    // Colour by the price actually shown vs the bar's open, so tag and line
    // never disagree with the number printed on them.
    var isBullish = (_axis.price != null && _axis.open != null)
        ? (_axis.price >= _axis.open)
        : (lastCandle.close >= lastCandle.open);
    var dirColor = isBullish ? '#26a69a' : '#ef5350';
    _carracNoteStale(_axis, lastCandle, tf, _sym, elId);

    // Companion pane candle roll-over (multi-chart). Ticks never roll a
    // companion's candle (chart-live-tick.js drops boundary-crossed ticks for
    // panes), so this 1s loop owns the reload. NOTE: `remaining <= 0` is
    // useless as the trigger — getNextCandleOpenMs skips nextOpen past now —
    // so detect "last REAL bar is a full period old" directly, the same test
    // as chartTickDecision case B. Also catches up a long-stale pane, not
    // just fresh closes. Skips (retrying on a later tick): hidden tab —
    // mirrors the active pane's deferred reload; a companion load already in
    // flight — _mcLoadCompanionPane ID-swaps globals, one at a time. The
    // per-bar 30s cooldown stamp mirrors RECOVER_COOLDOWN_MS so a failed
    // fetch retries instead of hammering every second.
    if (typeof paneIdx === 'number' && typeof _mcPanes !== 'undefined' && _mcPanes[paneIdx]) {
        var _cp = _mcPanes[paneIdx];
        var _cbarMs = getTimeframeDurationMs(tf);
        var _cstamp = _cp._mcCountdownRefreshed;
        if (_cbarMs > 0 && lastCandle.timestamp + _cbarMs <= _now &&
            !document.hidden && !window._mcCompanionLoading &&
            typeof _mcLoadCompanionPane === 'function' &&
            !(_cstamp && _cstamp.bar === lastCandle.timestamp && _now - _cstamp.at < 30000)) {
            _cp._mcCountdownRefreshed = { bar: lastCandle.timestamp, at: _now };
            var _cck = _cp.symbol + '_' + _cp.timeframe;
            if (typeof klineDataCache !== 'undefined') delete klineDataCache[_cck];
            try { sessionStorage.removeItem('kc_' + _cck); } catch(e) {}
            _mcLoadCompanionPane(paneIdx);
        }
    }

    // Format countdown text
    var timerText;
    if (remaining <= 0) {
        timerText = '00:00';
        // Auto-reload only for the active pane (pane 0 / main)
        if (elId === 'kline-candle-countdown') {
            if (!window._countdownRefreshed || window._countdownRefreshed !== nextOpen) {
                window._countdownRefreshed = nextOpen;
                if (klineChartSymbol && klineChartTimeframe) {
                    // Don't auto-reload while the tab is hidden. Background tabs throttle
                    // timers, so this reload would show the loading spinner and kick off a
                    // slow fetch that can race the tab-return reload and leave the spinner
                    // stuck. Defer it: the visibilitychange handler reloads on return.
                    if (document.hidden) {
                        window._countdownReloadPending = true;
                    } else {
                        // Invalidate cache to force fresh server fetch for the new candle
                        var _ck = klineChartSymbol + '_' + klineChartTimeframe;
                        if (typeof klineDataCache !== 'undefined') delete klineDataCache[_ck];
                        try { sessionStorage.removeItem('kc_' + _ck); } catch(e) {}
                        loadKlineChart(klineChartSymbol, klineChartTimeframe);
                    }
                }
            }
        }
    } else {
        if (elId === 'kline-candle-countdown') window._countdownRefreshed = null;
        timerText = formatCountdown(remaining, tf);
    }

    // Format price text
    var priceText = String(_axis.price != null ? _axis.price : lastCandle.close);
    try {
        var p = (_axis.price != null) ? _axis.price : lastCandle.close;
        if (p >= 10000) priceText = p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        else if (p >= 100) priceText = p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        else if (p >= 1) priceText = p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        else priceText = p.toFixed(4);
    } catch(e) {}

    // Render both rows
    var priceRow = el.querySelector('.price-row');
    var timerRow = el.querySelector('.timer-row');
    if (!priceRow) {
        el.innerHTML = '<div class="price-row"></div><div class="timer-row"></div>';
        priceRow = el.querySelector('.price-row');
        timerRow = el.querySelector('.timer-row');
    }
    priceRow.textContent = priceText;
    priceRow.style.backgroundColor = dirColor;
    timerRow.textContent = timerText;
    timerRow.style.borderColor = dirColor;
    timerRow.style.color = dirColor;

    try {
        // Position by the price actually shown, converted back to chart space
        // (log when log scale is on) so the tag sits on the CARRAC_LAST_PRICE
        // line rather than on the stale candle's close.
        var pixel = chart.convertToPixel(
            { value: carracToChartSpace(_axis.price != null ? _axis.price : lastCandle.close) },
            { paneId: 'candle_pane' }
        );
        if (pixel && pixel.y !== undefined) {
            var _yAxisWidth = 78;
            try {
                var _wrap = containerEl.children[0];
                var _pane = _wrap.children[0];
                var _yAxisDiv = _pane.lastElementChild;
                _yAxisWidth = _yAxisDiv.offsetWidth || 78;
            } catch(e) {}

            var priceH = priceRow.offsetHeight || 16;
            el.style.display = 'block';
            el.style.top = (pixel.y - priceH / 2) + 'px';
            el.style.right = '0px';
            el.style.width = _yAxisWidth + 'px';
            // Publish the tag's occupied rect (main pane only) so canvas axis
            // tags (alertLine) can nudge clear — the DOM tag always paints on top.
            // Also refresh the height caches the render-loop setter uses
            // (carracPositionAxisTag must not read layout per frame).
            if (elId === 'kline-candle-countdown') {
                _carracTagH = priceH;
                _carracTagRectH = el.offsetHeight || priceH * 2;
                window._carracAxisTagRect = { y: pixel.y - priceH / 2, h: _carracTagRectH };
            }
        }
    } catch (e) {
        el.style.display = 'none';
        if (elId === 'kline-candle-countdown') window._carracAxisTagRect = null;
    }
}

// Main entry point — updates countdown on active pane + all companion panes
function updateCandleCountdown() {
    // Skip during touch interactions — avoid forced reflows competing with pan frames
    if (window._touchPanActive) return;

    var mainContainer = document.getElementById('kline-chart-container');
    var activeIdx = (typeof _mcActivePaneIdx !== 'undefined') ? _mcActivePaneIdx : 0;
    var isMulti = (typeof _mcPanes !== 'undefined' && _mcPanes.length > 1);

    // Dedup: ensure each container has at most ONE countdown element.
    // Remove any stale/orphaned countdowns before updating.
    if (isMulti) {
        for (var d = 0; d < _mcPanes.length; d++) {
            var ctr = _mcPanes[d].containerEl;
            if (!ctr) continue;
            var dupes = ctr.querySelectorAll('.candle-countdown');
            // Each container should have exactly one. Remove extras, keeping the right ID.
            var expectedId = (d === activeIdx) ? 'kline-candle-countdown' : ('mc-countdown-' + d);
            for (var k = 0; k < dupes.length; k++) {
                if (dupes[k].id !== expectedId) dupes[k].remove();
            }
        }
    }

    // Active pane (always uses the global klineChart / klineChartTimeframe)
    _updateCountdownForChart(klineChart, klineChartTimeframe, mainContainer, 'kline-candle-countdown');

    // Companion panes (multi-chart mode)
    if (isMulti) {
        for (var i = 0; i < _mcPanes.length; i++) {
            if (i === activeIdx) continue;
            var p = _mcPanes[i];
            if (p.chart && p.containerEl) {
                _updateCountdownForChart(p.chart, p.timeframe, p.containerEl, 'mc-countdown-' + i, i);
            }
        }
    }
}
