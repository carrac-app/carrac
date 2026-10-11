// Page entry point for the chart app: page-wide keyboard and click
// delegation, the standalone chart bootstrap, window.onload for the
// dashboard, and the load-time contract self-check. Loads after every other
// chart-app file (see the script order in templates/index.html).

// Close modal on Escape key (only if no sub-panel is open)
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (document.getElementById('text-annotation-dialog')) return;
        if (document.getElementById('shortcuts-modal')) return;
        var sp = document.getElementById('asset-search-popup');
        if (sp && sp.classList.contains('visible')) return;
        if (typeof drawerOpen !== 'undefined' && drawerOpen) return;
        var as2 = document.getElementById('asset-switcher-menu');
        if (as2 && as2.classList.contains('visible')) return;
        // Close main asset search if open
        var mainSearch = document.getElementById('main-asset-search-popup');
        if (mainSearch && mainSearch.classList.contains('visible')) {
            hideMainAssetSearch();
            return;
        }
        if (klineModal && klineModal.classList.contains('visible') && !splitLayoutActive) {
            closeAdvancedChartModal();
        } else if (chartModal && chartModal.classList.contains('visible')) {
            closeChartModal();
        }
    }
    // SPACE key — open asset search when no chart/modal is open
    if (e.key === ' ' && !e.ctrlKey && !e.metaKey) {
        var tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        if (klineModal && klineModal.classList.contains('visible')) return;
        if (chartModal && chartModal.classList.contains('visible')) return;
        e.preventDefault();
        showMainAssetSearch();
    }
});

// Event delegation for dynamic elements (XSS-safe)
document.addEventListener('click', function(e) {
    // Guard against non-element targets (text nodes, etc.)
    if (!e.target || !e.target.closest) return;

    // Handle sortable column headers
    var sortTarget = e.target.closest('th.sortable');
    if (sortTarget) {
        var column = sortTarget.getAttribute('data-sort');
        var isWatchlist = sortTarget.getAttribute('data-table') === 'watchlist';
        if (column) {
            handleSort(column, isWatchlist);
            return;
        }
    }

    // Handle click/tap on chart-preview to open full KLineChart modal directly
    var chartTarget = e.target.closest('[data-action="chart-preview"]');
    if (chartTarget) {
        var symbol = chartTarget.getAttribute('data-symbol');
        if (symbol) {
            klineIsFullscreen = true;
            openAdvancedChartModal(symbol);
            return;
        }
    }

    // Handle click on copy icon to copy symbol
    var copyTarget = e.target.closest('[data-action="copy-symbol"]');
    if (copyTarget) {
        var symbol = copyTarget.getAttribute('data-symbol');
        if (symbol) {
            navigator.clipboard.writeText(symbol).then(function() {
                // Show brief feedback with checkmark icon
                var originalIcon = copyTarget.textContent;
                copyTarget.textContent = '✓';
                copyTarget.classList.add('copy-success');
                setTimeout(function() {
                    copyTarget.textContent = originalIcon;
                    copyTarget.classList.remove('copy-success');
                }, 800);
            }).catch(function(err) {
                console.error('Failed to copy symbol:', err);
            });
            return;
        }
    }

    var target = e.target.closest('[data-action]');
    if (!target) return;

    var action = target.getAttribute('data-action');
    if (action === 'toggle-section') {
        var type = target.getAttribute('data-type');
        if (type) toggleSection(type);
    } else if (action === 'toggle-watchlist') {
        var symbol = target.getAttribute('data-symbol');
        if (symbol) {
            if (isInWatchlist(symbol)) {
                removeFromWatchlist(symbol);
            } else {
                addToWatchlist(symbol);
            }
            renderFilteredTable();
            updateStarIcons();
        }
    } else if (action === 'showAssetGroupEditor') {
        showAssetGroupEditor();
    }
});

// Mobile asset-list search button → opens the full asset search (desktop opens it
// with the Space key, which phones don't have, so any-asset search was unreachable).
(function() {
    var _mlsBtn = document.getElementById('mobile-list-search-btn');
    if (_mlsBtn) _mlsBtn.addEventListener('click', function(e) {
        e.preventDefault();
        if (typeof showMainAssetSearch === 'function') showMainAssetSearch();
    });
})();

// Mobile dashboard nav: the asset list shows no bottom bar (removed per user
// request — the markets list IS the page). The ☰ hamburger (top-right, un-hidden
// on mobile via CSS) is the nav and already holds Journal, Morning Report,
// Settings, Logout, etc. Journal also lives on the chart's own bottom action bar.

// Mobile markets-list scroll fix. The first-paint inline script in index.html and initSplitLayout()
// add `split-active` to <body> + .container whenever window.innerWidth > 768. On a
// phone the viewport can briefly report > 768 during first layout (the same quirk
// that also affects chart open), latching `split-active` — which makes
// .container position:fixed and <body> overflow:hidden (see main.css), so the
// page cannot scroll. initSplitLayout() is gated on > 768 so it never runs on
// mobile to clean it up, and split-layout.js's resize handler only un-sets it when
// splitLayoutActive is true (false here). So force it off at any mobile width —
// the correct mobile state is split OFF. Desktop (> 768) is untouched.
(function() {
    function _dropStaleSplitActive() {
        if (window.innerWidth <= 768) {
            document.body.classList.remove('split-active');
            var c = document.querySelector('.container');
            if (c) c.classList.remove('split-active');
        }
    }
    _dropStaleSplitActive();
    window.addEventListener('load', _dropStaleSplitActive);
    window.addEventListener('resize', _dropStaleSplitActive);
    window.addEventListener('orientationchange', _dropStaleSplitActive);
})();

// Standalone chart page: bootstrap immediately (don't wait for window.onload
// which may have already fired by the time this script finishes executing)
if (window.isStandaloneChart) {
    (function standaloneBootstrap() {
        function boot(data) {
            allAssets = {};
            data.assets.forEach(function(a) { allAssets[a.symbol] = a; });
            initBinanceWebSocket();
            if (typeof window.initTableTickers === 'function') window.initTableTickers();
            klineIsFullscreen = true;
            openAdvancedChartModal(window.standaloneSymbol);
            if (window.standaloneTimeframe && window.standaloneTimeframe !== klineChartTimeframe) {
                switchTimeframe(window.standaloneTimeframe);
            }
            var splash = document.getElementById('chart-splash');
            if (splash) splash.remove();
        }

        // Try cached asset list first (from previous dashboard/chart visit)
        var ASSET_CACHE_TTL = 10 * 60 * 1000; // 10 min
        try {
            var cached = sessionStorage.getItem('assetListCache');
            if (cached) {
                var parsed = JSON.parse(cached);
                if (parsed && parsed.data && (Date.now() - parsed.timestamp < ASSET_CACHE_TTL)) {
                    boot(parsed.data);
                    // Refresh in background for next time
                    // SILENCE-OK: boot() above already used the still-valid cached
                    // list — this is purely a background refresh so the NEXT load
                    // gets fresher data. A miss just means the cache keeps its
                    // current (still-within-TTL) contents; it retries every load
                    // until the TTL expires and the "no cache" path below fetches
                    // for real.
                    fetch("/api/assets?mode=daily", { credentials: 'same-origin' })
                        .then(function(r) { return r.json(); })
                        .then(function(data) {
                            try { sessionStorage.setItem('assetListCache', JSON.stringify({ data: data, timestamp: Date.now() })); } catch(e) {}
                        }).catch(function() {});
                    return;
                }
            }
        } catch (e) {}

        // No cache — fetch and boot
        fetch("/api/assets?mode=daily", { credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                try { sessionStorage.setItem('assetListCache', JSON.stringify({ data: data, timestamp: Date.now() })); } catch(e) {}
                boot(data);
            });
    })();
}

window.onload = function() {
    if (window.isStandaloneChart) return;

    // Migration belt: a PWA session that was still
    // running older code could have body.style.overflow stuck to
    // 'hidden' inline (an imperative lock stranded because the JS
    // closer that should clear it got skipped — see
    // syncChartModalScrollLock()'s comment for the concrete bfcache case).
    // Safe to unconditionally clear the legacy inline style here ONLY
    // because this runs at the very top of a cold plain-navigation load,
    // before ANY modal (chart included) has had a chance to legitimately
    // open and set its own inline/class lock this session — so there is
    // nothing real to clobber. This makes an already-stranded install heal
    // itself on the very next launch, with no user action required, before
    // the auto-reopen-chart logic further down runs and (correctly) locks
    // it again if a chart is about to auto-open.
    syncChartModalScrollLock(true);

    initTimeframeMode();
    initChartTooltip();
    loadColumnVisibilityPreferences();
    initDisplayOptions();

    // Quick filter — filters watchlist/type-section rows by symbol or name.
    // Starts collapsed behind the magnifying-glass toggle; expands on
    // click, re-collapses on Esc or on losing focus while empty.
    var _qfEl = document.getElementById('panel-quick-filter');
    var _qfBtn = document.getElementById('panel-filter-toggle');
    if (_qfEl) {
        var _qfTimer = null;
        _qfEl.addEventListener('input', function() {
            if (_qfTimer) clearTimeout(_qfTimer);
            var v = _qfEl.value;
            if (_qfBtn) _qfBtn.classList.toggle('filter-active', !!v.trim());
            _qfTimer = setTimeout(function() {
                panelQuickFilter = (v || '').trim().toUpperCase();
                renderFilteredTable();
            }, 150);
        });
        _qfEl.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                _qfEl.value = ''; panelQuickFilter = ''; renderFilteredTable(); _qfEl.blur();
                _qfEl.classList.add('collapsed');
                if (_qfBtn) { _qfBtn.classList.remove('filter-active'); _qfBtn.setAttribute('aria-expanded', 'false'); }
            }
            e.stopPropagation(); // don't trigger global shortcuts while typing
        });
        _qfEl.addEventListener('blur', function() {
            if (!_qfEl.value.trim()) {
                _qfEl.classList.add('collapsed');
                if (_qfBtn) _qfBtn.setAttribute('aria-expanded', 'false');
            }
        });
        if (_qfBtn) {
            // preventDefault on mousedown so the input doesn't blur-collapse
            // right before the click toggles it back open (double-toggle).
            _qfBtn.addEventListener('mousedown', function(e) { e.preventDefault(); });
            _qfBtn.addEventListener('click', function() {
                var nowCollapsed = _qfEl.classList.toggle('collapsed');
                _qfBtn.setAttribute('aria-expanded', String(!nowCollapsed));
                if (nowCollapsed) _qfEl.blur(); else _qfEl.focus();
            });
        }
    }

    // Restore the persisted sort's arrow indicator — currentSort
    // itself was already restored from localStorage at declaration time.
    updateSortIndicators(false);

    requestNotificationPermission();
    if (typeof initTickerBar === 'function') initTickerBar();
    else if (typeof initSentimentRefresh === 'function') initSentimentRefresh();
    loadData(false);
    initBinanceWebSocket();
    if (typeof window.initTableTickers === 'function') window.initTableTickers();

    // Initialize split layout (desktop only). Guarded: a defer'd
    // sibling script can fail to load (network blip, ad blocker, CDN hiccup —
    // same family as the kline-drawings.js/carrac-indicator-pack.js lazy-load
    // failures fixed elsewhere) without blocking the OTHER defer'd scripts,
    // including this one. An unguarded call here threw ReferenceError and
    // killed every statement below it in this window.onload — the details
    // drawer, onboarding, and the auto-reopen-chart restore never ran either.
    if (typeof initSplitLayout === 'function') {
        initSplitLayout();
    } else {
        if (typeof window.carracLog === 'function') {
            window.carracLog('assets', 'initSplitLayout missing at onload — split-layout.js failed to load or execute');
        }
        if (typeof Sentry !== 'undefined' && Sentry.captureMessage) {
            Sentry.captureMessage('initSplitLayout not defined at window.onload', {
                level: 'error',
                tags: { kind: 'onload_guard', missing: 'initSplitLayout' }
            });
        }
    }

    // Details drawer — flag-gated
    if (window._features && window._features.detailsDrawer && window.CarracDetails) {
        document.body.classList.add('cd-restyle');
        CarracDetails.init();
    }

    // Details drawer row-click delegated handler
    (function(){
        var tb = document.getElementById('table-body');
        if (!tb) return;
        tb.addEventListener('click', function(e){
            if (!(window._features && window._features.detailsDrawer && window.CarracDetails)) return;
            if (e.target.closest('[data-action="chart-preview"]')) return;
            if (e.target.closest('[data-action="toggle-watchlist"]')) return;
            var tr = e.target.closest('tr[data-symbol]');
            if (!tr) return;
            var sym = tr.getAttribute('data-symbol');
            var sc = tr.querySelector('.signals-col');
            if (sc && window.allAssets && allAssets[sym]) allAssets[sym]._signalsHtml = sc.innerHTML;
            CarracDetails.open(sym);
        });
    })();

    // A11y: make onclick divs keyboard-accessible
    if (window.a11y) {
        var wlHeader = document.querySelector('.watchlist-header[data-action]');
        if (wlHeader) a11y.makeClickable(wlHeader);
    }

    // Onboarding for new users (onboarding.js is deferred — retry if not loaded yet)
    if (typeof initOnboarding === 'function') {
        initOnboarding();
    } else if (!window._onboardingCompleted) {
        var _onbRetry = setInterval(function() {
            if (typeof initOnboarding === 'function') { clearInterval(_onbRetry); initOnboarding(); }
        }, 100);
        setTimeout(function() { clearInterval(_onbRetry); }, 5000);
    }

    // Auto-reopen chart from URL param (?chart=BTC) or session restore
    // In split mode, selectAssetInPanel is called by initSplitLayout for session restore
    if (!splitLayoutActive) {
        var urlParams = new URLSearchParams(window.location.search);
        var chartParam = urlParams.get('chart');
        var savedSymbol = chartParam || sessionStorage.getItem('klineOpenSymbol') || localStorage.getItem('lastViewedAsset');
        // PWA standalone: always auto-open chart (use first watchlist item if nothing saved)
        var _isPwaStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
        // net-new desktop users had no saved symbol and fell through to a
        // blank chart area. Fall back to BTC (or the first watchlist item) so the
        // dashboard has content on first paint.
        var _isMobileViewport = window.matchMedia('(max-width: 768px)').matches;
        if (!savedSymbol && (_isPwaStandalone || !_isMobileViewport)) {
            savedSymbol = (watchlistSymbols && watchlistSymbols.length > 0) ? watchlistSymbols[0] : 'BTC';
        }
        if (chartParam) window._chartOpenedFromParam = true;
        if (savedSymbol) {
            setTimeout(function() {
                klineIsFullscreen = true;
                openAdvancedChartModal(savedSymbol);
                // Remove PWA loading screen after chart modal is open
                document.body.classList.add('pwa-ready');
            }, _isPwaStandalone ? 100 : 300);
        } else {
            // No chart to open — show dashboard immediately
            document.body.classList.add('pwa-ready');
        }
    } else {
        // Handle ?chart= param in split mode
        var urlParams = new URLSearchParams(window.location.search);
        var chartParam = urlParams.get('chart');
        if (chartParam) {
            window._chartOpenedFromParam = true;
            setTimeout(function() { selectAssetInPanel(chartParam); }, 400);
        }
        document.body.classList.add('pwa-ready');
    }

    // Lazy-load AI panel on FAB click or AI view-mode button.
    // ai-panel.js is loaded lazily; these listeners fire first and
    // inject the script.  Once loaded, ai-panel.js's DOMContentLoaded handler
    // (which runs synchronously during script eval) attaches real handlers
    // and setAIPanelState becomes available.
    (function() {
        // FAB button — floating action button on chart overlay
        var fab = document.getElementById('ai-fab');
        if (fab) {
            fab.addEventListener('click', function _fabLazy() {
                fab.removeEventListener('click', _fabLazy);
                ensureAIPanel().then(function() {
                    if (typeof setAIPanelState === 'function') setAIPanelState('small');
                });
            }, { once: true });
        }

        // AI view-mode button in split-layout panel strip
        var aiViewBtn = document.querySelector('.view-mode-btn[data-mode="ai"]');
        if (aiViewBtn) {
            aiViewBtn.addEventListener('click', function _aiViewLazy() {
                aiViewBtn.removeEventListener('click', _aiViewLazy);
                ensureAIPanel().then(function() {
                    // setViewMode is defined in split-layout.js; call it again
                    // now that initAIPanel is available
                    if (typeof setViewMode === 'function') setViewMode('ai');
                });
            }, { once: true });
        }
    }());
};

// Contract self-check: the chart app is many classic scripts glued by bare
// globals, often behind typeof guards — so a file that fails to load (or is
// dropped from a template) degrades SILENTLY (e.g. charts lose live ticks
// with zero signal if chart-live-tick.js is not included). Verify the
// contract once per load and beacon any gap. The list holds the globals
// with the most consumers plus one sentinel global per file BOTH templates
// load. Files lazy-loaded via _loadModule (kline-drawings.js) are
// deliberately absent — they are not on window at init and would
// false-alarm every load.
//
// `in window` rather than typeof: a top-level `var` awaiting its first
// assignment (klineChart before the first render) has typeof 'undefined'
// but IS a window property — the contract is the DECLARATION surviving
// extraction, not the value being set yet.
//
// Re-invokable on purpose: `delete window.<name>; _contractSelfCheck()`
// in DevTools (or a gate probe) shows the alarm firing — a guard never
// seen to fail is not a guard.
window._contractSelfCheck = function _contractSelfCheck() {
    try {
        var expected = [
            // resilience stack: chart load, live ticks, stale-chart recovery
            'klineChart', 'klineChartSymbol', 'klineChartTimeframe',
            '_lastRenderedSymbol', '_lastRenderedTimeframe', '_logScaleEnabled',
            'klineDataCache', 'loadKlineChart', 'getTimeframeDurationMs',
            'applyChartTick', '_hideStaleChartBadge',
            // app globals with >=3 consuming files
            'allAssets', 'drawings', 'klineActiveIndicators', 'escapeHtml',
            'showToast', 'convertToKlineData', 'klineSelectedOverlayId',
            'klineModal', 'isCryptoAsset', 'buildTfButtons', 'updateStarIcons',
            'drawerOpen', 'klineDrawingColor', 'klineDrawingWidth',
            'dedupFetch', 'fastSwapChartData', 'openAdvancedChartModal',
            'timeframeMode',
            'initSplitLayout',                              // split-layout.js
            // screener-table.js exports with cross-file consumers
            // (display-options.js hard-calls the cache/load five; the
            // others are called from window.onload + watchlist/split)
            'renderFilteredTable', 'updateWatchlistUI', 'loadData',
            'renderData', 'getCacheTTL', 'getCachedData', 'isCacheStale',
            // one sentinel per file both templates load
            'setupKlineChartEvents',                        // chart-events.js
            'handleKlineKeydown',                           // chart-keyboard.js
            'showSpotBuyDialog',                            // position-tools.js
            'applyKlineIndicators',                         // chart-indicators.js
            'recreateOverlayWithStyles',                    // drawing-editing.js
            'klineToolIcons',                               // draw-tool-palette.js
            'openMobileSheet',                              // mobile-chart.js
            'recoverChart',                                 // stale-tab-recovery.js
            'resetYAxisScale',                              // chart-gestures.js
            'ChartToolbar',                                 // chart-toolbar.js
            'DrawingTools',                                 // drawing-tools.js
            'IndicatorRegistry',                            // indicator-registry.js
            'chartRecoverGate', 'chartViewportDecision',    // chart-live-tick.js
            'shouldReuseChart', 'chartErrorOverlayModel',   // chart-live-tick.js
            'frvpLoad',                                     // frvp.js
            'vpStart',                                      // volume-profile.js
            'klStart',                                      // key-levels.js
            'addToWatchlist',                               // watchlist.js
            'registerCarracIndicatorDefs',                  // chart-indicator-defs.js
            'createPositionFigures',                        // chart-position-overlays.js
            'registerCarracOverlayDefs'                     // chart-overlay-defs.js
        ];
        var missing = [];
        for (var i = 0; i < expected.length; i++) {
            if (!(expected[i] in window)) missing.push(expected[i]);
        }
        if (missing.length) {
            if (typeof window.carracLog === 'function') {
                window.carracLog('init', 'contract MISSING: ' + missing.join(','));
            }
            if (typeof Sentry !== 'undefined' && Sentry.captureMessage) {
                Sentry.captureMessage('frontend contract check failed: ' + missing.join(','), {
                    level: 'error',
                    tags: { kind: 'contract_check' },
                    extra: { missing: missing }
                });
            }
        } else if (typeof window.carracLog === 'function') {
            window.carracLog('init', 'contract ok (' + expected.length + ' globals)');
        }
    } catch (e) {}
};
// Runs at DOMContentLoaded on EVERY page that loads main.js. It must not run
// only inside the dashboard init: that would leave /chart/<sym>/<tf> with no
// contract coverage at all.
//
// readyState is 'interactive' WHILE deferred scripts execute, and deferred
// scripts placed after main.js have not run yet at that moment — checking
// early flagged DrawingTools as missing on a perfectly healthy page. Only
// 'complete' means everything ran; anything else waits for DOMContentLoaded,
// which fires after ALL deferred scripts.
if (document.readyState === 'complete') {
    window._contractSelfCheck();
} else {
    document.addEventListener('DOMContentLoaded', function () { window._contractSelfCheck(); });
}
