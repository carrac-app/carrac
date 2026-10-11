/* Chart live-tick application + stale-chart recovery.
   One shared live-tick→chart path for binance-ws.js / ccxt-ws.js /
   table-tickers-ws.js / finnhub-ws.js / hyperliquid-ws.js. It guards two
   recovery holes:
   (A) failed-switch deadlock — a klineChartSymbol != _lastRenderedSymbol
       mismatch must not block BOTH the in-place candle update AND the
       boundary reload forever;
   (B) boundary reload must be retryable — a reload that died on a zombie
       connection is attempted again after a cooldown. */

/**
 * Pure decision core — no DOM, no globals; unit-tested via `node --test tests/js/`.
 * @param {Object} c snapshot of chart/tick state:
 *   intended      {string|null} klineChartSymbol — what the user asked for
 *   rendered      {string|null} _lastRenderedSymbol — what is on screen ('' pre-render)
 *   tick          {string}      symbol of the incoming tick
 *   panning       {boolean}     any touch/mouse pan in progress
 *   now           {number}      Date.now()
 *   tf            {string}      klineChartTimeframe
 *   lastUpdateAt  {number}      last applied tick (shared 250ms throttle)
 *   lastBarTs     {number|null} timestamp of last REAL candle (phantom-skipped)
 *   barMs         {number}      timeframe duration in ms (0 = unknown)
 *   mismatchSince {number|null} when intended!==rendered was first observed
 *   boundary      {Object|null} {key, at} — last boundary-reload attempt
 *   recoverAt     {number}      last deadlock-recover attempt
 * @returns {Object} action: {type:'inplace'|'ignore'|'observe-mismatch'|
 *   'deadlock-recover'|'boundary-reload', symbol?, key?, retry?}
 */
function chartTickDecision(c) {
    var MISMATCH_GRACE_MS = 12000;   // normal switch renders <2s, slow-but-alive <10s;
                                     // past this the switch fetch is considered dead
    var RECOVER_COOLDOWN_MS = 30000; // one recovery per CHART_FETCH_TIMEOUT_MS window
    var THROTTLE_MS = 250;

    // (A) Failed-switch deadlock: intended symbol never rendered. Recover the INTENDED symbol once
    // the mismatch has outlived any legitimate in-flight switch.
    if (c.intended && c.rendered && c.intended !== c.rendered) {
        if (c.mismatchSince == null) return { type: 'observe-mismatch' };
        if (c.now - c.mismatchSince >= MISMATCH_GRACE_MS &&
            c.now - (c.recoverAt || 0) >= RECOVER_COOLDOWN_MS) {
            return { type: 'deadlock-recover', symbol: c.intended };
        }
        return { type: 'ignore' };
    }

    if (!c.intended || c.tick !== c.intended) return { type: 'ignore' };
    if (c.panning) return { type: 'ignore' };
    if (c.lastUpdateAt && c.now - c.lastUpdateAt < THROTTLE_MS) return { type: 'ignore' }; // also defers a crossed boundary by ≤250ms — intentional
    if (c.lastBarTs == null) return { type: 'ignore' };

    // (B) Candle boundary crossed → reload, RETRYABLE on a cooldown. Each attempt is
    // stamped with a time, not latched forever: the same boundary is retried
    // once RECOVER_COOLDOWN_MS passes, so a reload that fails (e.g. a dead
    // connection) does not leave the chart stale until the NEXT candle.
    if (c.barMs > 0 && c.lastBarTs + c.barMs <= c.now) {
        var key = c.tick + '|' + c.tf + '|' + c.lastBarTs;
        if (!c.boundary || c.boundary.key !== key) {
            return { type: 'boundary-reload', symbol: c.tick, key: key, retry: false };
        }
        if (c.now - c.boundary.at >= RECOVER_COOLDOWN_MS) {
            return { type: 'boundary-reload', symbol: c.tick, key: key, retry: true };
        }
        return { type: 'ignore' };
    }

    // Healthy: update the live candle in place.
    return { type: 'inplace' };
}

/**
 * Pure cooldown gate for the shared recovery entry point (_recoverChart in stale-tab-recovery.js).
 * force=true (explicit user retry) bypasses an active cooldown but still stamps
 * a fresh one so passive recoveries stay coordinated around the user's action.
 * @returns {{allowed: boolean, until: number}} until = the cooldown stamp to store
 */
function chartRecoverGate(now, cooldownUntil, force) {
    var RECOVERY_COOLDOWN_MS = 30000; // one recovery per fetch-timeout window
    if (!force && cooldownUntil && now < cooldownUntil) {
        return { allowed: false, until: cooldownUntil };
    }
    return { allowed: true, until: now + RECOVERY_COOLDOWN_MS };
}

/**
 * Pure viewport decision for a SAME-symbol chart reload (boundary reload /
 * recovery). The reload path rebuilds the chart and would otherwise snap to the
 * latest candle every candle-close — yanking the user back while they study
 * history. Snap only when they're at the live edge; preserve scroll+zoom
 * otherwise. Detection uses getVisibleRange() bounds (getOffsetRightDistance()
 * clamps to >=0 and can't tell "at edge" from "scrolled far left").
 * @param {Object} c { isSwitch, lastRealIdx, visibleRealTo } — from the
 *   pre-reload chart. isSwitch = the asset or timeframe actually changed (old
 *   timestamps wouldn't map, so always snap). visibleRealTo = getVisibleRange()
 *   .realTo (EXCLUSIVE upper real-data index). lastRealIdx = index of the last
 *   non-phantom candle.
 * @returns {{mode:'snap'|'preserve'}}
 */
function chartViewportDecision(c) {
    if (!c || c.isSwitch) return { mode: 'snap' };
    if (c.lastRealIdx == null || c.visibleRealTo == null) return { mode: 'snap' };
    var SLACK = 2; // within 2 bars of the latest real candle = "at edge"; avoids
                   // flapping when a freshly-closed candle grows the dataset by 1.
    var rightmostVisibleReal = c.visibleRealTo - 1; // realTo is exclusive
    var atEdge = rightmostVisibleReal >= (c.lastRealIdx - SLACK);
    return atEdge ? { mode: 'snap' } : { mode: 'preserve' };
}

/**
 * Pure gate: on a fetch-path reload, may we REUSE the existing chart (a light
 * in-place fast-swap of fresh data) instead of disposing+recreating it? Only
 * when the SAME symbol+timeframe is already on screen — i.e. a routine
 * candle-close refresh. Asset/TF switches and recovery paths (which null
 * _lastRenderedSymbol before reloading) fall through to a clean full rebuild.
 * @param {Object} c { hasChart, symbol, timeframe, lastRenderedSymbol, lastRenderedTimeframe }
 * @returns {boolean}
 */
function shouldReuseChart(c) {
    if (!c || !c.hasChart) return false;
    if (!c.symbol || !c.timeframe) return false; // need a concrete symbol+TF
    return c.symbol === c.lastRenderedSymbol && c.timeframe === c.lastRenderedTimeframe;
}

/**
 * Pure decision core for the chart-load error overlay.
 * A data 404 can be transient — the post-deploy rate-limit window after a
 * deploy heals in ~2 minutes — so HTTP failures get a retry, not a shrug.
 * Only a plain JS error (no status, not a timeout) stays dismiss-only:
 * retrying a TypeError would just loop.
 * @param {Object} e {message, name, status, timedOut, symbol}
 * @returns {Object} {retry, headline, sub, logMsg}
 */
function chartErrorOverlayModel(e) {
    e = e || {};
    if (e.timedOut || e.name === 'AbortError') {
        return { retry: true,
                 headline: 'Chart took too long to load',
                 sub: 'Server may be busy — click to retry',
                 logMsg: 'load timeout' };
    }
    if (e.status === 404) {
        return { retry: true,
                 headline: 'No chart data for ' + (e.symbol || 'this symbol') + ' right now',
                 sub: 'It may be temporarily unavailable — click to retry',
                 logMsg: 'data 404' };
    }
    if (e.status >= 400) {
        return { retry: true,
                 headline: 'The chart server hit an error',
                 sub: 'Click to retry',
                 logMsg: 'server error ' + e.status };
    }
    return { retry: false,
             headline: 'Chart failed to load',
             sub: (e.message ? e.message + ' — ' : '') + 'click to dismiss, try another timeframe',
             logMsg: 'js error: ' + (e.message || 'unknown') };
}

// ---------------------------------------------------------------------------
// Browser side-effects. Everything below reads chart-app globals at CALL time
// (klineChart, klineChartSymbol, klineChartTimeframe, _lastRenderedSymbol,
// _logScaleEnabled, klineDataCache, loadKlineChart) plus getTimeframeDurationMs
// from countdown-timer.js — all at CALL time, load order does not matter, only
// that this file is loaded before the first WS message is processed.

function _showStaleChartBadge(text) {
    try {
        var container = document.getElementById('kline-chart-container');
        if (!container) return;
        // .chart-loading-overlay relies on the container being positioned; be
        // defensive in case a layout variant leaves it static.
        if (getComputedStyle(container).position === 'static') {
            container.style.position = 'relative';
        }
        var b = document.getElementById('chart-stale-badge');
        if (!b) {
            b = document.createElement('div');
            b.id = 'chart-stale-badge';
            // Informational, non-blocking — deliberately NOT [data-carrac-overlay]:
            // the reaper must never remove it while the chart is genuinely stale.
            b.style.cssText = 'position:absolute;top:8px;left:50%;transform:translateX(-50%);' +
                'z-index:110;padding:4px 12px;border-radius:12px;font-size:11px;' + // z-index:110 keeps badge above .chart-loading-overlay (z-index 100 in main.css) — explains WHY a reload is happening
                'pointer-events:none;background:rgba(187,128,9,0.18);' +
                'border:1px solid #bb8009;color:#d29922;white-space:nowrap;';
            container.appendChild(b);
        }
        b.textContent = '⟳ ' + text;
    } catch (e) {}
}

function _hideStaleChartBadge() {
    var b = document.getElementById('chart-stale-badge');
    if (b) b.remove();
}

// Recovery = badge + breadcrumb + delegated to _recoverChart.
// The Sentry beacon lives in _recoverChart (unified, reason-grouped).
function _staleChartRecover(mode, symbol, tf) {
    _showStaleChartBadge('Out of sync — refreshing…');
    if (typeof window.carracLog === 'function') {
        window.carracLog('stale', mode + ' → reload ' + symbol + ' ' + tf);
    }
    if (typeof window.recoverChart === 'function') {
        window.recoverChart(mode, { symbol: symbol, tf: tf, bustCache: true, badge: false });
    } else {
        // window.recoverChart missing (stale cached script set after a deploy):
        // fall back to the inline recipe so recovery still happens.
        var ck = symbol + '_' + tf;
        if (typeof klineDataCache !== 'undefined') delete klineDataCache[ck];
        try { sessionStorage.removeItem('kc_' + ck); } catch (e) {}
        if (typeof loadKlineChart === 'function') {
            if (typeof _lastRenderedSymbol !== 'undefined') _lastRenderedSymbol = null;
            if (typeof _lastRenderedTimeframe !== 'undefined') _lastRenderedTimeframe = null;
            window._chartFetchReady = true;
            loadKlineChart(symbol, tf);
        }
    }
}

/**
 * Shared in-place bar mutation for the active-pane and companion-pane paths.
 * dataList/wi = pre-scanned last REAL bar (phantom-skipped). Mutates
 * close/high/low, repaints via the (possibly phantom) final bar, then
 * re-stamps _phantom on the tail.
 */
function _applyInplaceTick(chart, dataList, wi, price, now) {
    try {
        var chartPrice = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && price > 0)
            ? Math.log(price) : price;
        var last = dataList[wi];
        last.close = chartPrice;
        last.high = Math.max(last.high, chartPrice);
        last.low = Math.min(last.low, chartPrice);
        // Repaint + fix lastValueMark via the (possibly phantom) final bar.
        var lastPt = dataList[dataList.length - 1];
        chart.updateData({
            timestamp: lastPt.timestamp,
            open: chartPrice, high: chartPrice, low: chartPrice, close: chartPrice
        });
        // updateData replaces the phantom tail slot's object, dropping its
        // _phantom flag; re-stamp it so phantom-guarded consumers (VOL/MA/
        // EMA/BOLL/MACD + the CARRAC_LAST_PRICE mark) keep excluding it.
        try {
            var _tail = dataList[dataList.length - 1];
            if (_tail && _tail.timestamp > now) _tail._phantom = true;
        } catch (e) {}
    } catch (e) {
        // Was a silent `catch (e) {}` in all 5 old blocks — breadcrumb it.
        if (typeof window.carracLog === 'function') {
            window.carracLog('tick', 'inplace update failed', String((e && e.message) || e));
        }
    }
}

/**
 * Split-view fan-out: the globals consumed by applyChartTick only ever
 * describe the ACTIVE pane, so route the tick to every companion pane
 * showing this symbol (multi-chart.js keeps their instances in _mcPanes).
 * Reuses chartTickDecision with per-pane state (p._lastTickAt throttle);
 * intended===rendered, so the mismatch/deadlock cases are unreachable.
 * A 'boundary-reload' decision is deliberately a no-op here — the candle is
 * closed and writing a tick into it would corrupt the bar; companion
 * candle-close reloads are owned by the countdown loop (countdown-timer.js).
 * No-ops outside multi-chart mode (single chart, mobile).
 */
function _applyCompanionTicks(dashSymbol, price) {
    try {
        if (typeof _mcPanes === 'undefined' || !_mcPanes || _mcPanes.length < 2) return;
        if (window._mcCompanionLoading) return; // pane array is mid-ID-swap
        var activeIdx = (typeof _mcActivePaneIdx !== 'undefined') ? _mcActivePaneIdx : 0;
        var panning = !!(window._touchPanActive || window._chartMousePanning);
        var now = Date.now();
        for (var i = 0; i < _mcPanes.length; i++) {
            if (i === activeIdx) continue;
            var p = _mcPanes[i];
            if (!p || !p.chart || p.symbol !== dashSymbol) continue;
            var dataList = null, wi = -1, lastBarTs = null;
            try {
                dataList = p.chart.getDataList();
                if (dataList && dataList.length > 0) {
                    wi = dataList.length - 1;
                    while (wi > 0 && (dataList[wi].timestamp > now || dataList[wi]._phantom)) wi--;
                    lastBarTs = dataList[wi].timestamp;
                }
            } catch (e) {}
            var action = chartTickDecision({
                intended: p.symbol,
                rendered: p.symbol,
                tick: dashSymbol,
                panning: panning,
                now: now,
                tf: p.timeframe,
                lastUpdateAt: p._lastTickAt || 0,
                lastBarTs: lastBarTs,
                barMs: (typeof getTimeframeDurationMs === 'function') ? (getTimeframeDurationMs(p.timeframe) || 0) : 0,
                mismatchSince: null,
                boundary: null,
                recoverAt: 0
            });
            if (action.type === 'inplace') {
                p._lastTickAt = now; // per-pane throttle; never touch window._lastChartUpdate
                _applyInplaceTick(p.chart, dataList, wi, price, now);
            }
        }
    } catch (e) {}
}

/**
 * Single entry point for all 5 WS providers: apply a live tick to the open
 * chart, or trigger stale-chart recovery. Replaces the duplicated blocks.
 * @param {string} dashSymbol dashboard symbol, e.g. 'BTC'
 * @param {number} price last trade price
 */
function applyChartTick(dashSymbol, price) {
    try {
        if (!price || !isFinite(price)) return; // WS parse glitches must never corrupt a bar
        _applyCompanionTicks(dashSymbol, price); // split-view panes (no-op in single-chart)
        if (typeof klineChart === 'undefined' || !klineChart) return;
        var intended = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : null;
        var rendered = (typeof _lastRenderedSymbol !== 'undefined' && _lastRenderedSymbol != null)
            ? _lastRenderedSymbol : '';
        var now = Date.now();
        var tf = (typeof klineChartTimeframe !== 'undefined') ? klineChartTimeframe : '';

        // Healthy symbol state — forget any mismatch history.
        if (intended && rendered && intended === rendered && window._chartTickMismatchSince != null) {
            window._chartTickMismatchSince = null;
        }

        // Fast path: a tick for another symbol is only relevant while a failed
        // switch is being observed (the deadlock detector uses ANY tick as its
        // clock — see chartTickDecision case A). Otherwise skip the bar scan and
        // ctx allocation entirely (~141 of ~142 ticks/sec take this exit).
        if (dashSymbol !== intended && !(intended && rendered && intended !== rendered)) return;

        // Last REAL candle (phantom-skipped — phantom bars pad the axis even with
        // timestamps <= now when server data is stale; same scan as the old blocks).
        var dataList = null, wi = -1, lastBarTs = null;
        try {
            dataList = klineChart.getDataList();
            if (dataList && dataList.length > 0) {
                wi = dataList.length - 1;
                while (wi > 0 && (dataList[wi].timestamp > now || dataList[wi]._phantom)) wi--;
                lastBarTs = dataList[wi].timestamp;
            }
        } catch (e) {}

        var action = chartTickDecision({
            intended: intended,
            rendered: rendered,
            tick: dashSymbol,
            panning: !!(window._touchPanActive || window._chartMousePanning),
            now: now,
            tf: tf,
            lastUpdateAt: window._lastChartUpdate || 0,
            lastBarTs: lastBarTs,
            barMs: (typeof getTimeframeDurationMs === 'function') ? (getTimeframeDurationMs(tf) || 0) : 0,
            mismatchSince: (window._chartTickMismatchSince != null) ? window._chartTickMismatchSince : null,
            boundary: window._chartBoundaryAttempt || null,
            recoverAt: window._chartRecoverAt || 0
        });

        switch (action.type) {
        case 'observe-mismatch':
            window._chartTickMismatchSince = now;
            return;

        case 'deadlock-recover':
            window._chartRecoverAt = now;
            window._chartTickMismatchSince = null;
            _staleChartRecover('switch-deadlock', action.symbol, tf);
            return;

        case 'boundary-reload':
            window._lastChartUpdate = now;
            window._chartBoundaryAttempt = { key: action.key, at: now };
            if (action.retry) {
                _staleChartRecover('boundary-retry', action.symbol, tf);
            } else {
                // First attempt per candle close — normal operation, no badge.
                var ck = action.symbol + '_' + tf;
                if (typeof klineDataCache !== 'undefined') delete klineDataCache[ck];
                try { sessionStorage.removeItem('kc_' + ck); } catch (e) {}
                if (typeof loadKlineChart === 'function') loadKlineChart(action.symbol, tf);
            }
            return;

        case 'inplace':
            window._lastChartUpdate = now;
            _hideStaleChartBadge(); // ticks applying again = healthy
            _applyInplaceTick(klineChart, dataList, wi, price, now);
            return;
        }
    } catch (e) {}
}

// Explicit export, same convention as window.subscribeBinanceSymbol.
if (typeof window !== 'undefined') {
    window.applyChartTick = applyChartTick;
    window._hideStaleChartBadge = _hideStaleChartBadge;
    window.chartRecoverGate = chartRecoverGate;
    window.chartViewportDecision = chartViewportDecision;
    window.shouldReuseChart = shouldReuseChart;
    window.chartErrorOverlayModel = chartErrorOverlayModel;
}

// Node test hook (tests/js/chart-tick-decision.test.cjs); inert in the browser.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        chartTickDecision: chartTickDecision,
        chartRecoverGate: chartRecoverGate,
        chartViewportDecision: chartViewportDecision,
        shouldReuseChart: shouldReuseChart,
        chartErrorOverlayModel: chartErrorOverlayModel
    };
}
