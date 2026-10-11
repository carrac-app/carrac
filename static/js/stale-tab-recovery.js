// Tab-return recovery: detect a long-hidden tab, reload stale charts,
// reconnect sockets and reap stranded loading overlays; also owns the
// app-wide client log (window.carracLog / window.__carracLog). Needs
// app-core.js; reaches the chart and WebSocket files only at runtime.

// When the tab is backgrounded overnight, browsers throttle timers and may
// silently drop WebSocket connections.  The chart data accumulates in a
// corrupted state, producing visually broken candles.  Detect long hidden
// periods and force a clean reload on return.
(function() {
    var STALE_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes
    var _lastActiveTs = Date.now();
    var _resumeSweepTimers = []; // pending stranded-spinner sweeps; cleared on each return so rapid tab-flipping can't stack them
    var _resumeSweepRaf = null;  // pending rAF that schedules the sweeps; cancelled on each return so rapid tab-flipping can't stack them

    // === Client diagnostic ring buffer (survives the minified production build) =======
    // The production build runs terser with drop_console=true, so every console.* is compiled away and production
    // would have NO client logging at all. This buffer is NOT console-based, so it ships to production:
    // always-on, readable live via window.__carracLog, and beaconed to Bugsink
    // when the reaper kills a strand. Capped so a long-lived tab can't grow it.
    var _CLOG_MAX = 250;
    window.__carracLog = window.__carracLog || [];
    function _clog(cat, msg, data) {
        try {
            var e = { ts: Date.now(), rel: Math.round(performance.now()), cat: cat, msg: msg };
            if (data !== undefined) e.data = data;
            window.__carracLog.push(e);
            if (window.__carracLog.length > _CLOG_MAX) {
                window.__carracLog.splice(0, window.__carracLog.length - _CLOG_MAX);
            }
            if (localStorage.getItem('carrac_resume_debug') === '1') {
                // eslint-disable-next-line no-console -- flag-gated dev echo; production build strips it
                console.log('%c[carrac:' + cat + '] ' + msg, 'color:#e91e63', data != null ? data : '');
            }
        } catch (e) {}
    }
    // Shared app-wide logger so other modules (ai-panel.js, etc.) can drop
    // breadcrumbs into the same prod-surviving buffer: window.carracLog(cat,msg,data).
    try { window.carracLog = _clog; } catch (e) {}

    // Beacon a stranded-spinner report to Bugsink. The Sentry SDK is loaded +
    // tunnelled in index.html and is NOT consent-gated, so captureMessage reaches
    // Bugsink. Rate-limited so a pathological tab can't flood the issue tracker:
    // a few per session, deduped per selector.
    var _beaconCount = 0, _beaconLast = {};
    function _beaconStrand(sel, info) {
        try {
            var now = Date.now();
            if (_beaconCount >= 6) return;
            if (_beaconLast[sel] && now - _beaconLast[sel] < 120000) return;
            _beaconLast[sel] = now; _beaconCount++;
            if (typeof Sentry !== 'undefined' && Sentry.captureMessage) {
                Sentry.captureMessage('stranded-spinner reaped: ' + sel, {
                    level: 'warning',
                    tags: { kind: 'stranded_spinner', selector: sel, visibility: document.visibilityState },
                    extra: { info: info, breadcrumbs: window.__carracLog.slice(-40) }
                });
            }
        } catch (e) {}
    }

    // === Shared chart-recovery entry point ======================================
    // Every passive recovery path (sweep, reaper, error-overlay retry, countdown-
    // pending, ≥5min return, tick-driven deadlock/boundary-retry) funnels through
    // here so concurrent triggers share ONE cooldown, ONE badge and ONE beacon.
    // force=true is reserved for explicit user retries (never cooldown-blocked).
    var _recoverBeaconCount = 0, _recoverBeaconLast = {};
    function _recoverChart(reason, opts) {
        var o = opts || {};
        var symbol = o.symbol || (typeof klineChartSymbol !== 'undefined' ? klineChartSymbol : null);
        var tf = o.tf || (typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : null);
        if (!symbol || !tf || !klineChart || typeof loadKlineChart !== 'function') return false;
        var now = Date.now();
        // Pure gate lives in chart-live-tick.js (node-tested). If it's missing
        // (wiring break — the contract self-check beacons that), fail OPEN:
        // uncoordinated recovery beats no recovery.
        var gate = (typeof window.chartRecoverGate === 'function')
            ? window.chartRecoverGate(now, window._chartRecoverCooldownUntil || 0, !!o.force)
            : { allowed: true, until: now + 30000 };
        if (!gate.allowed) {
            _clog('recover', 'cooldown — skipped ' + reason, { resumesInMs: Math.round(gate.until - now) });
            return false;
        }
        window._chartRecoverCooldownUntil = gate.until;
        if (o.badge !== false && typeof window._showStaleChartBadge === 'function') {
            window._showStaleChartBadge('Out of sync — refreshing…');
        }
        _clog('recover', reason + ' → reload ' + symbol + ' ' + tf, { force: !!o.force, bustCache: !!o.bustCache });
        try {
            var key = String(reason).split('-')[0];
            if (_recoverBeaconCount < 8 && !(_recoverBeaconLast[key] && now - _recoverBeaconLast[key] < 120000)) {
                _recoverBeaconLast[key] = now; _recoverBeaconCount++;
                if (typeof Sentry !== 'undefined' && Sentry.captureMessage) {
                    Sentry.captureMessage('chart recovery: ' + reason, {
                        level: 'warning',
                        tags: { kind: 'stale_chart_recovery', reason: reason, symbol: symbol },
                        extra: { tf: tf, force: !!o.force, breadcrumbs: (window.__carracLog || []).slice(-40) }
                    });
                }
            }
        } catch (e) {}
        if (o.bustCache) {
            var ck = symbol + '_' + tf;
            if (typeof klineDataCache !== 'undefined') delete klineDataCache[ck];
            try { sessionStorage.removeItem('kc_' + ck); } catch (e) {}
        }
        _lastRenderedSymbol = null;
        _lastRenderedTimeframe = null;
        window._chartFetchReady = true; // debounce-skip: throttle-immune re-drive
        loadKlineChart(symbol, tf);
        return true;
    }
    try { window.recoverChart = _recoverChart; } catch (e) {}

    // Off-by-default resume diagnostics, now routed through the ring buffer above
    // (so they reach production, not just a flag-gated dev console).
    function _resumeDbg(msg, extra) {
        _clog('resume', msg, extra);
    }

    function _visibleEls(selector) {
        var out = [];
        var els = document.querySelectorAll(selector);
        for (var i = 0; i < els.length; i++) {
            var cs = getComputedStyle(els[i]);
            if (cs.display !== 'none' && cs.visibility !== 'hidden' &&
                (els[i].offsetParent !== null || cs.position === 'fixed')) {
                out.push(els[i]);
            }
        }
        return out;
    }

    // Centralized stranded-spinner recovery. Resume logic is spread across many
    // subsystems (chart, asset list, AI panel, side panels), each with its own
    // spinner and its own — sometimes missing — hide path. A spinner shown on
    // tab-return can therefore be orphaned if its load resolves via an uncovered
    // path, leaving a "spinner forever until manual refresh" state.
    // This guarantees that, a bounded time after the tab becomes visible, NO
    // loading spinner survives without an in-flight load — regardless of which
    // subsystem stranded it. force=false only logs (early sweeps, so a slow-but-
    // live load is never killed); force=true runs past every fetch watchdog
    // (CHART/ASSETS = 30s) and also clears + re-drives a clean reload. The AI
    // spinners have no watchdog of their own, so they are recovered as soon as
    // the stream is known to be over (aiPanelStreaming false), regardless of force.
    function _sweepStrandedSpinners(force) {
        var chart = _visibleEls('.chart-loading-overlay');
        var aiTyping = _visibleEls('#_ai-typing');
        var aiTool = _visibleEls('.ai-tool-spinner');
        var mainLoad = _visibleEls('#loading');
        var seen = [];
        if (chart.length) seen.push('chart-overlay×' + chart.length);
        if (aiTyping.length) seen.push('ai-typing×' + aiTyping.length);
        if (aiTool.length) seen.push('ai-tool-spinner×' + aiTool.length);
        if (mainLoad.length) seen.push('#loading×' + mainLoad.length);
        if (seen.length) _resumeDbg(force ? 'final sweep — recovering' : 'spinner still visible', seen.join(', '));

        // AI spinners: if the stream already ended, the indicator is orphaned —
        // recover immediately (no fetch could still be in flight for it).
        var aiDead = (typeof aiPanelStreaming !== 'undefined') ? !aiPanelStreaming : true;
        if (aiDead && (aiTyping.length || aiTool.length)) {
            aiTyping.forEach(function (e) { e.remove(); });
            // Remove the whole tool indicator (the spinner is a child <span>) so no
            // orphaned "Fetching …" text is left behind in the chat.
            aiTool.forEach(function (e) { (e.closest('.ai-tool-indicator') || e).remove(); });
        }

        if (!force) return;

        if (chart.length) {
            chart.forEach(function (e) { e.remove(); });
            if (typeof loadKlineChart === 'function' && klineChart && klineChartSymbol) {
                _recoverChart('sweep-force', { bustCache: false });
            }
        }
        if (mainLoad.length) {
            mainLoad.forEach(function (e) { e.style.display = 'none'; });
            if (!window.isStandaloneChart && typeof loadData === 'function') loadData(true);
        }
    }

    // === Universal stranded-overlay reaper ======================================
    // Guards against a "spinner never goes away" state. Per-subsystem hide
    // paths and the curated _sweepStrandedSpinners list above will always lag the
    // next overlay someone adds (the chart-error overlay and AI streaming both
    // slipped through that way). Instead, enforce ONE invariant: no loading/blocking
    // overlay may stay visible past a bounded TTL, whatever subsystem owns it.
    // Selector-agnostic — anything tagged [data-carrac-overlay] is auto-covered, so
    // new overlays need no change here; the explicit list is the backstop for the
    // ones that predate the convention. TTLs sit safely ABOVE the 30s fetch-abort
    // so a genuinely in-flight load is never killed. AI spinners are progress-based
    // (window.__aiLastActivityAt, bumped per stream chunk in ai-panel.js) so a long
    // legit answer streams uninterrupted while a zombied stream is still reaped.
    var REAP_TTL_MS = Math.max(CHART_FETCH_TIMEOUT_MS, ASSETS_FETCH_TIMEOUT_MS) + 12000; // 42s
    var AI_STALL_MS = 90000; // AI: reap only after this long with NO stream activity
    var _OVERLAY_CFG = [
        { sel: '.chart-loading-overlay',  ttl: REAP_TTL_MS, act: 'remove', redrive: true },
        { sel: '.chart-error-overlay',    ttl: REAP_TTL_MS, act: 'remove', redrive: true },
        { sel: '#loading',                ttl: REAP_TTL_MS, act: 'hide' },
        { sel: '#refresh-loading',        ttl: REAP_TTL_MS, act: 'hide' },
        { sel: '#report-loading',         ttl: 180000,      act: 'hide' },   // reports are LLM-slow
        { sel: '#_ai-typing',             ttl: AI_STALL_MS, act: 'remove', ai: true },
        { sel: '.ai-tool-spinner',        ttl: AI_STALL_MS, act: 'tool',   ai: true },
        { sel: '.stream-loading-spinner', ttl: 60000,       act: 'remove' },
        { sel: '.loading-spinner',        ttl: 60000,       act: 'remove' },
        { sel: '.api-loading',            ttl: 60000,       act: 'hide' },
        { sel: '[data-carrac-overlay]',   ttl: 60000,       act: 'remove' }
    ];
    var _reapFirstSeen = new WeakMap(); // element -> performance.now() when first seen visible
    function _reapStrandedOverlays(reason) {
        try {
            var now = performance.now();
            var redriven = false; // re-drive the chart at most once per pass
            for (var c = 0; c < _OVERLAY_CFG.length; c++) {
                var cfg = _OVERLAY_CFG[c];
                var els = _visibleEls(cfg.sel);
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    var first = _reapFirstSeen.get(el);
                    if (first === undefined) { _reapFirstSeen.set(el, now); continue; }
                    var ttl = cfg.ttl;
                    var attr = el.getAttribute && el.getAttribute('data-ttl');
                    if (attr && +attr > 0) ttl = +attr; // per-element override for the convention
                    if (cfg.ai) {
                        var last = window.__aiLastActivityAt || 0;
                        if (last && (Date.now() - last) < ttl) continue; // stream still alive
                    }
                    if ((now - first) < ttl) continue; // legit in-flight load still within budget
                    // Stranded past its deadline — log it (always), reap it, beacon it.
                    _clog('reap', 'reaped ' + cfg.sel, { ageMs: Math.round(now - first), reason: reason });
                    _beaconStrand(cfg.sel, { ageMs: Math.round(now - first), reason: reason });
                    if (cfg.act === 'remove') { el.remove(); }
                    else if (cfg.act === 'tool') { (el.closest('.ai-tool-indicator') || el).remove(); }
                    else { el.style.display = 'none'; }
                    if (cfg.ai && typeof aiPanelStreaming !== 'undefined') aiPanelStreaming = false;
                    // A reaped chart overlay means its owner load is dead —
                    // removing the spinner alone would leave a stale chart.
                    // Re-drive a clean, debounce-free (throttle-immune) reload.
                    // This makes recovery independent of the (parkable) sweep
                    // timers — the reaper's interval was armed at page load and
                    // unthrottles correctly on tab-return.
                    if (cfg.redrive && !redriven && typeof loadKlineChart === 'function' && klineChart && klineChartSymbol) {
                        redriven = true;
                        _recoverChart('reaper', { bustCache: false });
                    }
                }
            }
        } catch (e) { _clog('reap', 'reaper error', String((e && e.message) || e)); }
    }
    // Foreground sweep on a steady cadence. Background tabs throttle this timer,
    // which is fine — a hidden tab has no user to block; we also reap on return.
    setInterval(function () { if (!document.hidden) _reapStrandedOverlays('interval'); }, 5000);

    function _onVisibilityReturn() {
        if (document.hidden) {
            _lastActiveTs = Date.now();
            return;
        }
        // Page just became visible again
        var elapsed = Date.now() - _lastActiveTs;

        // Chrome's intensive background throttling keeps parking NEWLY-armed
        // setTimeouts for up to ~1 minute after the tab returns (minute-aligned
        // wake-up bursts of 18–51s, even after a
        // 150s absence, and rAF-scheduled timers are parked too). Any timer
        // armed in that window — including a USER click's 220ms chart-fetch
        // debounce — fires absurdly late. Open a bypass window during which
        // _loadKlineChartImpl skips the debounce and fetches synchronously.
        // 10s gate = Chrome's quick-intensive-throttling threshold; 70s cover
        // = worst observed park (51s) + margin.
        if (elapsed >= 10000) {
            window._resumeNoDebounceUntil = Date.now() + 70000;
        }

        // A candle closed while we were hidden — the countdown deferred its reload
        // (it suppresses reloads while hidden to avoid a stuck spinner). Catch up now
        // that timers run at full speed, even for short hidden periods under the
        // staleness threshold. Reset the per-candle guard so it can fire again.
        var countdownPending = window._countdownReloadPending;
        window._countdownReloadPending = false;
        window._countdownRefreshed = null;

        // Centralized stranded-spinner safety net. Whichever subsystem's resume
        // logic runs below, schedule recovery sweeps so no loading spinner can
        // outlive a bounded window without an in-flight load. Early sweeps only
        // log (and clear AI spinners whose stream already died); the final sweep
        // — past every fetch watchdog — force-clears and re-drives a clean reload.
        _resumeDbg('returned after ' + (elapsed / 1000).toFixed(1) + 's away');
        _resumeSweepTimers.forEach(clearTimeout);
        _resumeSweepTimers = [];
        if (_resumeSweepRaf !== null && typeof cancelAnimationFrame === 'function') { cancelAnimationFrame(_resumeSweepRaf); }
        _resumeSweepRaf = null;
        var _finalSweepMs = Math.max(CHART_FETCH_TIMEOUT_MS, ASSETS_FETCH_TIMEOUT_MS) + 5000;
        // These sweep timers are BEST-EFFORT ONLY. Chrome's post-return throttle
        // window parks timers armed up to ~1min after the tab returns, and
        // rAF-scheduled timers get parked just like
        // handler-scheduled ones — so these may fire late, in one batched burst.
        // That's acceptable: the RELIABLE recovery layers are (1) the synchronous
        // resume reload, (2) the _resumeNoDebounceUntil bypass for user switches,
        // and (3) the 5s interval reaper (armed at page load, unthrottles
        // correctly on return), which also re-drives a reaped chart overlay.
        // The rAF wrapper is kept for its cancel bookkeeping on rapid tab flips.
        var _scheduleResumeSweeps = function () {
            _resumeSweepRaf = null;
            _resumeSweepTimers.push(setTimeout(function () { _sweepStrandedSpinners(false); }, 3000));
            _resumeSweepTimers.push(setTimeout(function () { _sweepStrandedSpinners(false); }, 10000));
            _resumeSweepTimers.push(setTimeout(function () { _sweepStrandedSpinners(true); }, _finalSweepMs));
            _resumeSweepTimers.push(setTimeout(function () { _reapStrandedOverlays('return+'); }, _finalSweepMs + 8000));
        };
        if (typeof requestAnimationFrame === 'function') { _resumeSweepRaf = requestAnimationFrame(_scheduleResumeSweeps); }
        else { _scheduleResumeSweeps(); }
        // Universal reaper alongside the curated sweep: catches overlays the sweep
        // doesn't know about (scenario / intelligence / report spinners and any
        // future one). Immediate pass clears anything stranded through the whole
        // background; the rAF-scheduled follow-up catches a load that fails
        // shortly after return.
        _reapStrandedOverlays('return');

        // A chart error overlay left from a load that failed while we were away is a
        // dead end — only a click dismisses it, so it reads as "stuck". On return,
        // silently retry the load instead of leaving the stale error up.
        try {
            var _errOv = document.querySelector('.chart-error-overlay');
            if (_errOv && typeof loadKlineChart === 'function' && klineChart && klineChartSymbol) {
                _clog('resume', 'retrying stale chart-error-overlay on return');
                _errOv.remove();
                _recoverChart('error-overlay-return', { bustCache: false });
            }
        } catch (e) {}

        // Refresh the on-screen data on return. The only periodic refresh of the
        // asset list is display-options.js's setInterval(loadData, getCacheTTL()),
        // which is 15–30 min AND is throttled/paused while the tab is in the
        // background — and nothing was refreshing on tab-return — so the dashboard
        // sat showing stale metrics "confidently" until a manual page reload.
        // Do a silent background refresh (loadData(true) re-fetches without a
        // spinner when a cache is present) once we've been away long enough to
        // matter. Skip the standalone chart page (it has no asset list).
        var DATA_REFRESH_ON_RETURN_MS = 60 * 1000;
        if (elapsed >= DATA_REFRESH_ON_RETURN_MS && !window.isStandaloneChart && typeof loadData === 'function') {
            loadData(true);
        }

        if (elapsed < STALE_THRESHOLD_MS) {
            if (countdownPending && klineChart && klineChartSymbol) {
                // Lightweight catch-up: refresh just the active chart with fresh data.
                // No WS reconnect needed for a brief absence.
                _recoverChart('countdown-pending', { bustCache: true });
            } else if (elapsed >= DATA_REFRESH_ON_RETURN_MS && klineChart && klineChartSymbol && typeof _refreshStaleChart === 'function') {
                // No candle closed, but we've been away a while — silently refresh
                // the open chart's data too (this path doesn't touch the loading
                // overlay, so no spinner flicker). The >=5min branch below owns the
                // chart when the absence was longer.
                _refreshStaleChart(klineChartSymbol);
            }
            return;
        }

        // 1. Flush all chart caches so loadKlineChart hits the server
        klineDataCache = {};
        try {
            var keys = Object.keys(sessionStorage);
            for (var i = 0; i < keys.length; i++) {
                if (keys[i].indexOf('kc_') === 0) sessionStorage.removeItem(keys[i]);
            }
        } catch (e) {}

        // 2. Reconnect WebSockets cleanly — SYNCHRONOUSLY. A setTimeout armed
        // here is parked by Chrome's post-resume throttling for up to ~1min
        // (sync code is the ONLY immune path). Null the handlers before close so the
        // async onclose can't report a stale 'reconnecting' to CarracConn or
        // arm a ghost backoff timer.
        if (typeof initBinanceWebSocket === 'function') {
            try {
                if (binanceWS) { binanceWS.onclose = null; binanceWS.onerror = null; binanceWS.close(); binanceWS = null; }
            } catch (e) {}
            if (binanceWSReconnectTimer) { clearTimeout(binanceWSReconnectTimer); binanceWSReconnectTimer = null; }
            binanceWSReconnectDelay = 5000;
            initBinanceWebSocket();
        }
        if (typeof initHyperliquidWebSocket === 'function') {
            try {
                if (typeof hlWS !== 'undefined' && hlWS) { hlWS.onclose = null; hlWS.onerror = null; hlWS.close(); hlWS = null; }
            } catch (e) {}
            if (typeof hlWSReconnectTimer !== 'undefined' && hlWSReconnectTimer) { clearTimeout(hlWSReconnectTimer); hlWSReconnectTimer = null; }
            if (typeof hlWSReconnectDelay !== 'undefined') hlWSReconnectDelay = 5000;
            initHyperliquidWebSocket();
        }
        if (typeof window.resetTableTickers === 'function') window.resetTableTickers();

        // 3. Reload the active chart with fresh server data. If the chart on
        // screen already IS the reload target (same symbol+TF — the common
        // case, just stale data), arm the silent-revalidate hint so
        // _loadKlineChartImpl skips the loading overlay: the server now
        // answers this fetch in ~100-250ms (stale-while-revalidate), so a spinner over
        // an already-rendered chart for that round trip is a pointless flash
        // The cache flush above (step 1) is untouched — fresh
        // server data still replaces the rendered state, which is what
        // actually repairs the corrupted-candles state this handler
        // exists for.
        if (klineChart && klineChartSymbol) {
            if (_chartDisplayedSymbol === klineChartSymbol && _chartDisplayedTimeframe === klineChartTimeframe) {
                _chartSilentReloadOnce = true;
            }
            _recoverChart('stale-return', { bustCache: false });
        }
    }
    document.addEventListener('visibilitychange', _onVisibilityReturn);
    // BFcache restore (mobile Safari, aggressive Chrome): a page revived from the
    // back-forward cache fires pageshow(persisted) — visibilitychange may not fire.
    // Re-run the same tab-return recovery path. The handlers read live document
    // state, so a non-hidden restored page takes the "became visible" branches.
    window.addEventListener('pageshow', function (e) {
        if (e && e.persisted && !document.hidden) {
            try { _onVisibilityReturn(); } catch (err) {}
        }
        // Mobile fix: bfcache is a *browser-managed* navigation cache
        // (web.dev/articles/bfcache — "it doesn't work for soft navigations
        // within an SPA"), and this app's advanced chart modal never calls
        // history.pushState, so opening/closing it alone is never a bfcache
        // event by itself. The real path: the chart's bottom action bar can
        // navigate straight to /journal (a real cross-document nav,
        // data-nav="journal" in chart-modal.js) with NO check for whether a mobile
        // feature sheet (⋮ More / TF / Indicator) is open at the time. Hit
        // the browser/OS back button from /journal and bfcache restores that
        // exact prior DOM, reviving the full JS heap (module vars included)
        // without re-running any init or close-path code.
        // If the chart modal itself was also open (the common case — you
        // can only reach that Journal button from inside the chart) it gets
        // correctly restored open too, matching what the user actually saw
        // when they navigated away. This sweep exists for the
        // narrower residual case openMobileSheet()'s klineModal-nesting fix
        // doesn't structurally cover: klineModal wasn't around at sheet-open
        // time (falls back to <body> — see openMobileSheet above) or any
        // future code path re-introduces a body-level sheet/backdrop. Run
        // outside the !document.hidden gate above (unlike _onVisibilityReturn,
        // this is cheap, idempotent DOM cleanup, not a data-freshness
        // recovery) so a restored stray sheet/backdrop never has a chance to
        // sit there swallowing the header's taps even for a moment.
        if (e && e.persisted && typeof closeAllMobileSheets === 'function') {
            try { closeAllMobileSheets(); } catch (err) {}
        }
        // bfcache restores the chart modal's classList
        // exactly as it was (open stays open, closed stays closed — see
        // syncChartModalScrollLock()'s own comment), so this re-derives the
        // body scroll lock from that restored state rather than trusting
        // whatever inline style also came back with the snapshot. Not gated
        // on cleanLegacyInline: a legitimate OTHER modal (help/premium/etc.)
        // could be the one that's genuinely open post-restore with its own
        // inline lock, and this must never clobber that.
        if (e && e.persisted && typeof syncChartModalScrollLock === 'function') {
            try { syncChartModalScrollLock(); } catch (err) {}
        }
    });

    // ── WS supervisor ─────────────────────────────────────────────────────
    // Chrome's post-resume throttling parks every setTimeout armed while
    // hidden or within ~1min of tab-return — including all the WS
    // backoff reconnects — and releases them in minute-aligned bursts.
    // (Opening DevTools disables the throttling, which is why a dead feed
    // "comes back when I open the console".) PRE-EXISTING intervals are NOT
    // parked (in production the 5s reaper kept ticking on time), so this
    // interval is the guaranteed reconnect floor: any closed socket is
    // re-inited ≤10s after the tab is visible, regardless of parked timers.
    // All init fns are idempotent (no-op while open/connecting); a parked
    // backoff timer firing later is likewise a harmless no-op.
    function _superviseWS() {
        if (document.hidden) return;
        try {
            if (typeof initBinanceWebSocket === 'function' &&
                (typeof binanceWS === 'undefined' || !binanceWS || binanceWS.readyState >= 2)) {
                initBinanceWebSocket();
            }
            if (typeof initHyperliquidWebSocket === 'function' &&
                (typeof hlWS === 'undefined' || !hlWS || hlWS.readyState >= 2)) {
                initHyperliquidWebSocket();
            }
            if (typeof window.initTableTickers === 'function') window.initTableTickers();
            // finnhub is REST-polling with its own hidden-aware loop; no-op if running.
            if (typeof initFinnhubWebSocket === 'function') initFinnhubWebSocket();
        } catch (e) {}
    }
    setInterval(_superviseWS, 10000);
    // Network back = reconnect NOW (event handler code runs sync = unparkable).
    window.addEventListener('online', _superviseWS);
})();
