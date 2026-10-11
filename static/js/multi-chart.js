/* Multi-Chart Layout Manager for Carrac
 * Manages multiple KLineChart panes in grid layouts (single/dual/tri/quad).
 * Uses ID-swapping so ALL existing code works without modification —
 * the active pane's container always has id="kline-chart-container",
 * and klineChart/klineChartSymbol/klineChartTimeframe always reflect the active pane.
 */

// --- State ---
var _mcLayout = 'single';
var _mcActivePaneIdx = 0;
var _mcPanes = [];      // [{ chart, symbol, timeframe, containerEl, paneEl }]
var _mcGridEl = null;
var _mcInitialized = false;
var _mcTierMaxPanes = 4; // Default; overridden by /api/tier-info on load
var _mcKeydownHandler = null; // stored so duplicate listeners cannot be registered

// --- Constants ---
var _mcTfOrder = ['1m', '5m', '15m', '30m', '1h', '4h', '1D', '3D', '1W', '1M'];

function _mcNextTfUp(tf) {
    var idx = _mcTfOrder.indexOf(tf);
    return (idx < 0 || idx >= _mcTfOrder.length - 1) ? '1W' : _mcTfOrder[idx + 1];
}

// After swapping the globals to a pane whose chart is already
// rendered, re-stamp the chart app's rendered-state globals so chart-live-tick's
// deadlock detector (intended !== rendered) doesn't fire a spurious reload
// ~12s after every pane switch / companion load. ONLY stamp when the chart
// has real data: an instantiated-but-empty chart must stay mismatched so the
// 12s deadlock recovery still auto-heals it (its only recovery path).
function _mcStampRendered() {
    try {
        if (klineChart && klineChart.getDataList && klineChart.getDataList().length > 0 &&
            typeof _lastRenderedSymbol !== 'undefined') {
            _lastRenderedSymbol = klineChartSymbol;
            _lastRenderedTimeframe = klineChartTimeframe;
        }
    } catch (e) {}
}

// Chrome-inset sync: the toolbar is a translucent absolute overlay over the
// pane grid, so anything a pane draws near its top edge (active frame,
// OHLCV legend) must start below the toolbar where they overlap. One
// measured value drives both: per pane, overlap = toolbar bottom − pane top
// (0 for bottom-row panes). Re-run whenever chrome geometry changes:
// layout change, resize, pane switch, companion load, draw-toolbar toggle
// (updateOhlcOffset in kline-drawings.js delegates here in multi mode).
// The draw row opens/closes via a 150ms height transition, so a measurement
// taken at toggle time reads the mid-transition height — apply immediately
// for responsiveness, then re-apply once after the transition settles.
var _mcInsetSettleTimer = null;
function _mcUpdateChromeInsets() {
    _mcApplyChromeInsets();
    if (_mcInsetSettleTimer) clearTimeout(_mcInsetSettleTimer);
    _mcInsetSettleTimer = setTimeout(function () {
        _mcInsetSettleTimer = null;
        _mcApplyChromeInsets();
    }, 220);
}
function _mcApplyChromeInsets() {
    try {
        if (!_mcGridEl) return;
        var toolbar = document.querySelector('.adv-chart-toolbar');
        var tbBottom = toolbar ? toolbar.getBoundingClientRect().bottom : 0;
        for (var i = 0; i < _mcPanes.length; i++) {
            var p = _mcPanes[i];
            if (!p || !p.paneEl) continue;
            var overlap = Math.max(0, Math.round(tbBottom - p.paneEl.getBoundingClientRect().top));
            p.paneEl.style.setProperty('--mc-frame-top', overlap + 'px');
            var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
            if (ch && ch.setStyles) {
                ch.setStyles({ candle: { tooltip: { offsetTop: Math.max(6, overlap + 4) } } });
            }
        }
    } catch (e) {}
}
window.mcUpdateChromeInsets = _mcUpdateChromeInsets;

// --- Layout Selector HTML (called from openAdvancedChartModal toolbar) ---
var _mcIcons = {
    single: '<svg width="16" height="14" viewBox="0 0 16 14"><rect x="1.5" y="1.5" width="13" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
    dual:   '<svg width="16" height="14" viewBox="0 0 16 14"><rect x="1" y="1.5" width="6" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="1.5" width="6" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
    stack:  '<svg width="16" height="14" viewBox="0 0 16 14"><rect x="1.5" y="1.5" width="13" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="1.5" y="7.5" width="13" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
    tri:    '<svg width="16" height="14" viewBox="0 0 16 14"><rect x="1" y="1.5" width="6" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="1.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="7.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
    quad:   '<svg width="16" height="14" viewBox="0 0 16 14"><rect x="1" y="1.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="1.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="1" y="7.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="7.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
    rotate: '<svg width="16" height="14" viewBox="0 0 16 14"><path d="M11 2.5L13 4.5L11 6.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M3 7V5C3 3.5 4.2 2.5 6 2.5H12.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round"/><path d="M5 11.5L3 9.5L5 7.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M13 7V9C13 10.5 11.8 11.5 10 11.5H3.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round"/></svg>',
    spread: '<svg width="16" height="14" viewBox="0 0 16 14"><rect x="1" y="1.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="1.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="1" y="7.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="7.5" width="6" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><line x1="8" y1="3" x2="8" y2="11" stroke="currentColor" stroke-width="1.2" stroke-dasharray="1.5 1.5"/></svg>'
};

function mcLayoutSelectorHTML() {
    return '<div class="mc-layout-selector" id="mc-layout-selector">' +
        '<button class="tool-btn tool-text-btn mc-layout-trigger" id="mc-layout-trigger" title="Chart layout — panes, rotation, TF spread">Panes <i data-lucide="chevron-down" style="width:12px;height:12px"></i></button>' +
        '<div class="mc-layout-dropdown" id="mc-layout-dropdown">' +
            '<div class="mc-layout-opt active" data-mc-layout="single">' + _mcIcons.single + '<span>Single</span><span class="mc-layout-key">Ctrl+1</span></div>' +
            '<div class="mc-layout-opt" data-mc-layout="dual">' + _mcIcons.dual + '<span>Side by side</span><span class="mc-layout-key">Ctrl+2</span></div>' +
            '<div class="mc-layout-opt" data-mc-layout="stack">' + _mcIcons.stack + '<span>Top / bottom</span><span class="mc-layout-key">Ctrl+3</span></div>' +
            '<div class="mc-layout-opt" data-mc-layout="tri">' + _mcIcons.tri + '<span>1+2 stack</span><span class="mc-layout-key">Ctrl+4</span></div>' +
            '<div class="mc-layout-opt" data-mc-layout="quad">' + _mcIcons.quad + '<span>2\u00d72 grid</span><span class="mc-layout-key">Ctrl+5</span></div>' +
            '<div class="mc-layout-sep"></div>' +
            '<div class="mc-layout-opt" id="mc-rotate-btn" data-mc-action="rotate">' + _mcIcons.rotate + '<span>Rotate plays</span><span class="mc-layout-key">Ctrl+6</span></div>' +
            '<div class="mc-layout-opt" id="mc-rotate-fav-btn" data-mc-action="rotate-fav">' + _mcIcons.rotate + '<span>Rotate favourites</span><span class="mc-layout-key">Ctrl+7</span></div>' +
            '<div class="mc-layout-opt" id="mc-spread-btn" data-mc-action="spread">' + _mcIcons.spread + '<span>TF spread</span><span class="mc-layout-key">Ctrl+8</span></div>' +
        '</div>' +
        '</div>';
}

// --- Initialization (called after modal HTML is built, before loadKlineChart) ---
function mcInit() {
    if (_mcInitialized) return;
    var body = document.querySelector('.adv-chart-body');
    var existing = document.getElementById('kline-chart-container');
    if (!body || !existing) return;

    _mcGridEl = document.createElement('div');
    _mcGridEl.className = 'mc-grid mc-single';
    _mcGridEl.id = 'mc-grid';

    // Wrap existing chart container in primary pane
    var pane0 = _mcCreatePaneEl(0);
    var chartArea = pane0.querySelector('.mc-pane-chart');
    existing.parentNode.removeChild(existing);
    chartArea.appendChild(existing);
    _mcGridEl.appendChild(pane0);

    body.innerHTML = '';
    body.appendChild(_mcGridEl);

    _mcPanes = [{
        chart: null,
        symbol: klineChartSymbol,
        timeframe: klineChartTimeframe,
        containerEl: existing,
        paneEl: pane0
    }];
    _mcActivePaneIdx = 0;
    _mcLayout = 'single';
    _mcInitialized = true;

    // Fetch tier info for layout gating
    fetch('/api/tier-info').then(function(r) { return r.json(); }).then(function(d) {
        _mcTierMaxPanes = d.max_chart_panes || 2;
        // Disable layout options that exceed tier
        document.querySelectorAll('.mc-layout-opt[data-mc-layout]').forEach(function(opt) {
            var counts = { single: 1, dual: 2, stack: 2, tri: 3, quad: 4 };
            var need = counts[opt.getAttribute('data-mc-layout')] || 1;
            if (need > _mcTierMaxPanes) {
                opt.classList.add('mc-layout-locked');
                opt.setAttribute('title', 'Upgrade to unlock');
            }
        });
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/tier-info failed — rendered as empty', String((e && e.message) || e)); });

    // Single mode: hide pane label (toolbar already shows asset info)
    var pane0Label = pane0.querySelector('.mc-pane-label');
    if (pane0Label) pane0Label.style.display = 'none';

    // Create rotation overlay
    var _rotateOverlay = document.createElement('div');
    _rotateOverlay.className = 'mc-rotate-overlay';
    _rotateOverlay.id = 'mc-rotate-overlay';
    _rotateOverlay.style.display = 'none';
    // Load saved interval preference
    try { var _savedInt = parseInt(localStorage.getItem('mcRotateInterval')); if (_savedInt >= 30) _mcRotateInterval = _savedInt; } catch(e) {}

    _rotateOverlay.innerHTML =
        '<div class="mc-rotate-progress" id="mc-rotate-progress"></div>' +
        '<span class="mc-rotate-status" id="mc-rotate-status"></span>' +
        '<div style="flex:1"></div>' +
        '<button class="mc-rotate-ctrl" id="mc-rotate-pause" title="Pause/Resume">&#9646;&#9646;</button>' +
        '<button class="mc-rotate-ctrl" id="mc-rotate-prev" title="Previous page">&#9664;</button>' +
        '<span class="mc-rotate-counter" id="mc-rotate-counter"></span>' +
        '<button class="mc-rotate-ctrl" id="mc-rotate-next" title="Next page">&#9654;</button>' +
        '<select class="mc-rotate-interval" id="mc-rotate-interval" title="Rotation interval">' +
            '<option value="30">30s</option>' +
            '<option value="45">45s</option>' +
            '<option value="60">1m</option>' +
            '<option value="90">1.5m</option>' +
            '<option value="120">2m</option>' +
            '<option value="180">3m</option>' +
            '<option value="300">5m</option>' +
        '</select>' +
        '<button class="mc-rotate-ctrl mc-rotate-exit" id="mc-rotate-stop" title="Exit rotation mode">Exit</button>';
    _mcGridEl.appendChild(_rotateOverlay);

    // Set saved value in dropdown
    var intervalSelect = document.getElementById('mc-rotate-interval');
    if (intervalSelect) {
        intervalSelect.value = String(_mcRotateInterval);
        intervalSelect.addEventListener('change', function(e) {
            e.stopPropagation();
            var val = parseInt(this.value);
            if (val > 0) {
                _mcRotateInterval = val;
                try { localStorage.setItem('mcRotateInterval', String(val)); } catch(e) {}
                if (_mcRotateMode) _mcRotateResetTimer();
            }
        });
    }

    document.getElementById('mc-rotate-pause').addEventListener('click', function(e) {
        e.stopPropagation();
        _mcRotateTogglePause();
    });
    document.getElementById('mc-rotate-prev').addEventListener('click', function(e) {
        e.stopPropagation();
        if (!_mcRotateMode) return;
        var prev = (_mcRotatePageIdx - 1 + _mcRotatePages.length) % _mcRotatePages.length;
        _mcRotateLoadPage(prev);
        _mcRotateResetTimer();
    });
    document.getElementById('mc-rotate-next').addEventListener('click', function(e) {
        e.stopPropagation();
        if (!_mcRotateMode) return;
        var next = (_mcRotatePageIdx + 1) % _mcRotatePages.length;
        _mcRotateLoadPage(next);
        _mcRotateResetTimer();
    });
    document.getElementById('mc-rotate-stop').addEventListener('click', function(e) {
        e.stopPropagation();
        mcStopRotation();
    });

    // ESC exits rotation mode (handler stored at module scope so it cannot be double-registered)
    _mcKeydownHandler = function(e) {
        if (e.key === 'Escape' && _mcRotateMode) {
            e.stopImmediatePropagation();
            mcStopRotation();
        }
    };
    document.addEventListener('keydown', _mcKeydownHandler, true);

    // Bind layout dropdown
    var _mcTrigger = document.getElementById('mc-layout-trigger');
    var _mcDropdown = document.getElementById('mc-layout-dropdown');
    if (_mcTrigger && _mcDropdown) {
        _mcTrigger.addEventListener('click', function(e) {
            e.stopPropagation();
            if (typeof window.closeAllChartMenus === 'function') window.closeAllChartMenus('mc-layout-dropdown');
            _mcDropdown.classList.toggle('open');
        });
        document.addEventListener('click', function() { _mcDropdown.classList.remove('open'); });
        _mcDropdown.querySelectorAll('.mc-layout-opt').forEach(function(opt) {
            opt.addEventListener('click', function(e) {
                e.stopPropagation();
                var layout = this.getAttribute('data-mc-layout');
                var action = this.getAttribute('data-mc-action');
                if (action === 'rotate') {
                    mcToggleRotation('plays');
                } else if (action === 'rotate-fav') {
                    mcToggleRotation('favourites');
                } else if (action === 'spread') {
                    mcSpreadTimeframes();
                } else if (layout) {
                    mcSetLayout(layout);
                }
                _mcDropdown.classList.remove('open');
            });
        });
    }

    _mcUpdateChromeInsets();
}

// --- DOM: Create pane element ---
function _mcCreatePaneEl(index) {
    var pane = document.createElement('div');
    pane.className = 'mc-pane' + (index === _mcActivePaneIdx ? ' active' : '');
    pane.setAttribute('data-mc-pane', index);

    var chartArea = document.createElement('div');
    chartArea.className = 'mc-pane-chart';

    // Overlay label inside chart area (replaces header stripe)
    var label = document.createElement('div');
    label.className = 'mc-pane-label';
    label.innerHTML =
        '<span class="mc-pane-sym">\u2014</span>' +
        '<span class="mc-pane-tf"></span>' +
        '<span class="mc-pane-name"></span>' +
        (index > 0 ? '<button class="mc-pane-close" title="Close pane">\u00d7</button>' : '');
    chartArea.appendChild(label);

    pane.appendChild(chartArea);

    // Activate on click / touch
    function _mcPaneActivate(e) {
        if (e.target.closest('.mc-pane-close')) return;
        var idx = parseInt(this.getAttribute('data-mc-pane'));
        if (!isNaN(idx) && idx !== _mcActivePaneIdx) mcSetActivePane(idx);
    }
    pane.addEventListener('mousedown', _mcPaneActivate);
    pane.addEventListener('touchstart', _mcPaneActivate, { passive: true });

    // Close button
    var closeBtn = label.querySelector('.mc-pane-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            e.preventDefault();
            var idx = parseInt(this.closest('.mc-pane').getAttribute('data-mc-pane'));
            if (!isNaN(idx)) mcClosePane(idx);
        });
    }

    return pane;
}

// --- Active Pane Switching ---
function mcSetActivePane(idx) {
    if (idx === _mcActivePaneIdx || idx < 0 || idx >= _mcPanes.length) return;

    // Save current state
    _mcPanes[_mcActivePaneIdx].chart = klineChart;
    _mcPanes[_mcActivePaneIdx].symbol = klineChartSymbol;
    _mcPanes[_mcActivePaneIdx].timeframe = klineChartTimeframe;

    // Visual
    _mcPanes[_mcActivePaneIdx].paneEl.classList.remove('active');
    _mcPanes[idx].paneEl.classList.add('active');

    // Move countdown element from old container before swapping IDs
    var oldContainer = _mcPanes[_mcActivePaneIdx].containerEl;
    var newContainer = _mcPanes[idx].containerEl;
    var mainCountdown = document.getElementById('kline-candle-countdown');
    if (mainCountdown && mainCountdown.parentNode === oldContainer) {
        mainCountdown.style.display = 'none';
        newContainer.appendChild(mainCountdown);
    }
    // Remove any companion countdown from the container becoming active
    var oldCompanion = document.getElementById('mc-countdown-' + idx);
    if (oldCompanion) oldCompanion.remove();

    // Swap container IDs
    _mcPanes[_mcActivePaneIdx].containerEl.id = 'mc-chart-' + _mcActivePaneIdx;
    _mcPanes[idx].containerEl.id = 'kline-chart-container';

    // Restore globals
    _mcActivePaneIdx = idx;
    klineChart = _mcPanes[idx].chart;
    klineChartSymbol = _mcPanes[idx].symbol;
    klineChartTimeframe = _mcPanes[idx].timeframe;
    klineSelectedOverlayId = null;
    _mcStampRendered();

    _mcUpdateToolbar();
    _mcUpdateHeaders();
    _mcSaveState();

    // If chart never finished loading (race condition), reload now
    var _mcNeedsReload = false;
    if (!klineChart && klineChartSymbol) {
        _mcNeedsReload = true;
    } else if (klineChart && klineChartSymbol) {
        // Catch-up: companion panes tick live, but a pane can still be stale
        // (CCXT-routed symbol with the socket parked elsewhere, long-hidden
        // tab). If at least one candle boundary passed since its last REAL
        // bar, refresh — same symbol+TF goes through the shouldReuseChart
        // fast-swap and chartViewportDecision, so no viewport yank.
        try {
            var _dl = klineChart.getDataList();
            if (_dl && _dl.length > 0) {
                var _wi = _dl.length - 1, _nowTs = Date.now();
                while (_wi > 0 && (_dl[_wi].timestamp > _nowTs || _dl[_wi]._phantom)) _wi--;
                var _barMs = (typeof getTimeframeDurationMs === 'function')
                    ? (getTimeframeDurationMs(klineChartTimeframe) || 0) : 0;
                if (_barMs > 0 && _dl[_wi].timestamp + _barMs <= _nowTs) {
                    var _ck = klineChartSymbol + '_' + klineChartTimeframe;
                    if (typeof klineDataCache !== 'undefined') delete klineDataCache[_ck];
                    try { sessionStorage.removeItem('kc_' + _ck); } catch (e) {}
                    _mcNeedsReload = true;
                }
            }
        } catch (e) {}
    }
    if (_mcNeedsReload) {
        // The reload's subscribe block also re-points the CCXT socket if needed.
        loadKlineChart(klineChartSymbol, klineChartTimeframe);
    } else {
        // CCXT socket follows the active pane (single socket page-wide; a
        // companion load may have left it parked on another pane's symbol).
        try {
            var _vm = (window._wsVenueBySymbol || {})[klineChartSymbol];
            if (_vm && _vm.venue === 'ccxt' && _vm.exchange &&
                typeof _ccxtCurrentSymbol !== 'undefined' && _ccxtCurrentSymbol !== klineChartSymbol &&
                typeof subscribeCcxtSymbol === 'function') {
                subscribeCcxtSymbol(klineChartSymbol, _vm.exchange);
            }
        } catch (e) {}
    }

    // Update URL for standalone
    if (window.isStandaloneChart) {
        history.replaceState(null, '', '/chart/' + klineChartSymbol + '/' + klineChartTimeframe);
        document.title = klineChartSymbol + ' ' + klineChartTimeframe + ' \u2014 Carrac';
    }

    _mcUpdateChromeInsets();
}

function _mcUpdateToolbar() {
    var symEl = document.getElementById('adv-chart-symbol');
    var nameEl = document.getElementById('adv-chart-name');
    if (symEl) symEl.textContent = klineChartSymbol || '';
    if (nameEl) {
        var a = (typeof allAssets !== 'undefined' ? allAssets[klineChartSymbol] : null) || {};
        nameEl.textContent = a.name || '';
    }
    var tfContainer = document.getElementById('tf-buttons-container');
    if (tfContainer && typeof buildTfButtons === 'function') {
        tfContainer.innerHTML = buildTfButtons(klineChartSymbol, klineChartTimeframe);
        tfContainer.querySelectorAll('.tf-selector').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var tf = this.getAttribute('data-tf');
                tfContainer.querySelectorAll('.tf-selector').forEach(function(b) { b.classList.remove('active'); });
                this.classList.add('active');
                klineChartTimeframe = tf;
                loadKlineChart(klineChartSymbol, tf);
            });
        });
    }
    if (typeof _updateWatchlistStar === 'function') _updateWatchlistStar(klineChartSymbol);
}

function _mcUpdateHeaders() {
    _mcPanes.forEach(function(p, i) {
        var labelEl = p.paneEl.querySelector('.mc-pane-label');
        if (!labelEl) return;
        var sym = labelEl.querySelector('.mc-pane-sym');
        var tf = labelEl.querySelector('.mc-pane-tf');
        var s = (i === _mcActivePaneIdx) ? klineChartSymbol : p.symbol;
        var t = (i === _mcActivePaneIdx) ? klineChartTimeframe : p.timeframe;
        if (sym) sym.textContent = s || '\u2014';
        if (tf) tf.textContent = t || '';
        // Show label on companion panes in multi mode, hide on pane 0 in single
        labelEl.style.display = (_mcLayout === 'single' && i === 0) ? 'none' : '';
    });
}

// --- Layout Management ---
function mcSetLayout(layout) {
    var counts = { single: 1, dual: 2, stack: 2, tri: 3, quad: 4 };
    if (!counts[layout] || layout === _mcLayout) return;

    // Tier gate: check max panes
    if (counts[layout] > _mcTierMaxPanes) {
        if (typeof showUpgradeModal === 'function') {
            showUpgradeModal('Chart layout with ' + counts[layout] + ' panes requires a subscription.');
        }
        return;
    }

    // Switching layout manually exits spread mode
    if (_mcSpreadMode) { _mcSpreadMode = false; _mcUpdateSpreadUI(); }

    var target = counts[layout];

    // Save current active pane state before modifications
    _mcPanes[_mcActivePaneIdx].chart = klineChart;
    _mcPanes[_mcActivePaneIdx].symbol = klineChartSymbol;
    _mcPanes[_mcActivePaneIdx].timeframe = klineChartTimeframe;

    // Add panes if needed (creates DOM only, no active-pane switching)
    var newPanes = [];
    while (_mcPanes.length < target) {
        _mcAddPane();
        newPanes.push(_mcPanes.length - 1);
    }
    // Remove panes if needed
    while (_mcPanes.length > target) _mcRemoveLastPane();

    _mcGridEl.className = 'mc-grid mc-' + layout;
    _mcLayout = layout;

    _mcPanes.forEach(function(p, i) {
        // Pane 0 (main) never shows label — toolbar already has asset selector
        var show = layout !== 'single' && i > 0;
        var lbl = p.paneEl.querySelector('.mc-pane-label');
        if (lbl) lbl.style.display = show ? '' : 'none';
    });

    _mcUpdateLayoutSelector();
    _mcUpdateHeaders();
    _mcUpdateChromeInsets();

    // Update pane 0's OHLCV tooltip style (compact in multi, full in single)
    _mcUpdateTooltipStyle();

    // Resize charts after DOM reflow, THEN rebuild indicators at compact heights
    requestAnimationFrame(function() {
        _mcPanes.forEach(function(p, i) {
            var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
            if (ch) ch.resize();
        });
        // After resize, rebuild indicators at correct heights for the new layout
        _mcResizeIndicatorPanes();
        // Final resize to settle layout after indicator height changes
        requestAnimationFrame(mcResizeAll);
    });

    // Load new companion panes in background (sequentially, without switching active pane)
    // Skip when called from spread mode — _mcSpreadLoadAll handles all loading
    if (newPanes.length > 0 && !_mcSkipCompanionLoad) {
        var queue = newPanes.slice();
        function _loadNextNewPane() {
            if (queue.length === 0) {
                requestAnimationFrame(mcResizeAll);
                _mcSubscribeCrosshairSync();
                return;
            }
            _mcLoadCompanionPane(queue.shift(), _loadNextNewPane);
        }
        setTimeout(_loadNextNewPane, 50);
    }

    // If rotating, recompute pages for new pane count
    if (_mcRotateMode) {
        _mcRotateComputePages();
        _mcRotatePageIdx = 0;
        _mcRotateLoadPage(0);
        _mcRotateResetTimer();
    }

    _mcSaveState();
}

function _mcAddPane() {
    var idx = _mcPanes.length;
    var primarySymbol = _mcPanes[0].symbol || klineChartSymbol;
    var primaryTf = _mcPanes[0].timeframe || klineChartTimeframe;

    // Pick a TF not already in use
    var usedTfs = {};
    _mcPanes.forEach(function(p) { usedTfs[p.timeframe] = true; });
    var compTf = _mcNextTfUp(primaryTf);
    var maxIter = _mcTfOrder.length;
    while (usedTfs[compTf] && maxIter-- > 0) compTf = _mcNextTfUp(compTf);

    var paneEl = _mcCreatePaneEl(idx);
    var chartArea = paneEl.querySelector('.mc-pane-chart');

    var container = document.createElement('div');
    container.className = 'adv-chart-container';
    container.id = 'mc-chart-' + idx;
    chartArea.appendChild(container);

    _mcGridEl.appendChild(paneEl);

    _mcPanes.push({
        chart: null,
        symbol: primarySymbol,
        timeframe: compTf,
        containerEl: container,
        paneEl: paneEl
    });
    // Note: active pane is NOT switched here. mcSetLayout handles companion loading.
}

function _mcRemoveLastPane() {
    if (_mcPanes.length <= 1) return;
    var idx = _mcPanes.length - 1;
    if (_mcActivePaneIdx === idx) mcSetActivePane(0);

    var pane = _mcPanes[idx];
    if (pane.chart && pane.containerEl) {
        try { klinecharts.dispose(pane.containerEl); } catch(e) {}
    }
    pane.paneEl.remove();
    _mcPanes.pop();
}

function mcClosePane(idx) {
    if (idx <= 0 || idx >= _mcPanes.length) return;

    if (_mcActivePaneIdx === idx) mcSetActivePane(0);
    if (_mcActivePaneIdx > idx) _mcActivePaneIdx--;

    var pane = _mcPanes[idx];
    if (pane.chart && pane.containerEl) {
        try { klinecharts.dispose(pane.containerEl); } catch(e) {}
    }
    pane.paneEl.remove();
    _mcPanes.splice(idx, 1);

    // Re-index
    _mcPanes.forEach(function(p, i) {
        p.paneEl.setAttribute('data-mc-pane', i);
        if (i !== _mcActivePaneIdx) p.containerEl.id = 'mc-chart-' + i;
    });

    var layoutMap = { 1: 'single', 2: (_mcLayout === 'stack' ? 'stack' : 'dual'), 3: 'tri', 4: 'quad' };
    _mcLayout = layoutMap[_mcPanes.length] || 'single';
    _mcGridEl.className = 'mc-grid mc-' + _mcLayout;

    // Pane 0 never shows header; hide all in single mode
    _mcPanes.forEach(function(p, i) {
        var show = _mcLayout !== 'single' && i > 0;
        var lbl = p.paneEl.querySelector('.mc-pane-label');
        if (lbl) lbl.style.display = show ? '' : 'none';
    });

    _mcUpdateLayoutSelector();
    _mcSaveState();

    requestAnimationFrame(function() {
        _mcPanes.forEach(function(p, i) {
            var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
            if (ch) ch.resize();
        });
    });
}

// --- Pane Cycling (Tab) ---
function mcCyclePanes(dir) {
    if (_mcPanes.length <= 1) return;
    var next = (_mcActivePaneIdx + (dir || 1)) % _mcPanes.length;
    if (next < 0) next = _mcPanes.length - 1;
    mcSetActivePane(next);
}

// --- Layout Selector UI ---
function _mcUpdateLayoutSelector() {
    document.querySelectorAll('.mc-layout-opt[data-mc-layout]').forEach(function(opt) {
        opt.classList.toggle('active', opt.getAttribute('data-mc-layout') === _mcLayout);
    });
}

// --- Sync (called at end of renderKlineChart / fastSwapChartData) ---
function mcSyncActiveChart() {
    if (!_mcInitialized || _mcPanes.length === 0) return;
    if (_mcActivePaneIdx < _mcPanes.length) {
        _mcPanes[_mcActivePaneIdx].chart = klineChart;
        _mcPanes[_mcActivePaneIdx].symbol = klineChartSymbol;
        _mcPanes[_mcActivePaneIdx].timeframe = klineChartTimeframe;
    }
    _mcUpdateHeaders();
    _mcSaveState();
}

// --- Persist / Restore multi-chart state ---
function _mcSaveState() {
    if (!_mcInitialized) return;
    var state = {
        layout: _mcLayout,
        active: _mcActivePaneIdx,
        panes: _mcPanes.map(function(p, i) {
            return {
                symbol: (i === _mcActivePaneIdx) ? klineChartSymbol : p.symbol,
                timeframe: (i === _mcActivePaneIdx) ? klineChartTimeframe : p.timeframe
            };
        })
    };
    try { localStorage.setItem('mcState', JSON.stringify(state)); } catch(e) {}
}

function mcRestoreState() {
    if (!_mcInitialized) return false;
    var raw;
    try { raw = JSON.parse(localStorage.getItem('mcState')); } catch(e) { return false; }
    if (!raw || !raw.layout || raw.layout === 'single' || !raw.panes || raw.panes.length < 2) return false;

    // Set primary pane symbol/tf
    if (raw.panes[0]) {
        klineChartSymbol = raw.panes[0].symbol || klineChartSymbol;
        klineChartTimeframe = raw.panes[0].timeframe || klineChartTimeframe;
        _mcPanes[0].symbol = klineChartSymbol;
        _mcPanes[0].timeframe = klineChartTimeframe;
    }

    // Expand to target layout — _mcAddPane creates DOM but we'll override symbols
    var counts = { single: 1, dual: 2, stack: 2, tri: 3, quad: 4 };
    var target = counts[raw.layout] || 1;
    if (target <= 1) return false;

    // Add panes (DOM only, no loading — companion loading handled below)
    while (_mcPanes.length < target) _mcAddPane();

    // Override auto-assigned symbols/tfs with saved state
    for (var i = 1; i < _mcPanes.length && i < raw.panes.length; i++) {
        if (raw.panes[i]) {
            _mcPanes[i].symbol = raw.panes[i].symbol || _mcPanes[i].symbol;
            _mcPanes[i].timeframe = raw.panes[i].timeframe || _mcPanes[i].timeframe;
        }
    }

    _mcGridEl.className = 'mc-grid mc-' + raw.layout;
    _mcLayout = raw.layout;

    _mcPanes.forEach(function(p, i) {
        var show = raw.layout !== 'single' && i > 0;
        var lbl = p.paneEl.querySelector('.mc-pane-label');
        if (lbl) lbl.style.display = show ? '' : 'none';
    });

    // Activate pane 0 first — load its chart
    mcSetActivePane(0);
    klineChartSymbol = _mcPanes[0].symbol;
    klineChartTimeframe = _mcPanes[0].timeframe;
    loadKlineChart(klineChartSymbol, klineChartTimeframe);

    _mcUpdateLayoutSelector();
    _mcUpdateHeaders();

    // Load companion panes sequentially after primary is loaded
    var companionQueue = [];
    for (var j = 1; j < _mcPanes.length; j++) companionQueue.push(j);

    function loadNextCompanion() {
        if (companionQueue.length === 0) {
            // All loaded — activate saved pane
            var activeIdx = raw.active || 0;
            if (activeIdx > 0 && activeIdx < _mcPanes.length) {
                mcSetActivePane(activeIdx);
            }
            requestAnimationFrame(function() { mcResizeAll(); });
            _mcSubscribeCrosshairSync();
            return;
        }
        var idx = companionQueue.shift();
        _mcLoadCompanionPane(idx, loadNextCompanion);
    }

    // Start companion loading after primary chart is fully rendered
    var _primaryPoll = setInterval(function() {
        if (klineChart && klineChart.getDataList && klineChart.getDataList().length > 0) {
            clearInterval(_primaryPoll);
            clearTimeout(_primaryFallback);
            loadNextCompanion();
        }
    }, 100);
    var _primaryFallback = setTimeout(function() {
        clearInterval(_primaryPoll);
        loadNextCompanion();
    }, 8000);

    return true;
}

function _mcLoadCompanionPane(idx, callback) {
    if (idx === _mcActivePaneIdx) { if (callback) callback(); return; }
    var p = _mcPanes[idx];
    if (!p || !p.containerEl || !p.symbol) { if (callback) callback(); return; }

    // Busy flag: while the globals/container-IDs are swapped to the companion,
    // the pane array is inconsistent — the tick fan-out (chart-live-tick.js)
    // and the countdown reload both stand down, and the chart loader (chart-loading.js) must not repoint
    // the single CCXT socket for a background pane. Cleared in _restoreAndContinue.
    window._mcCompanionLoading = true;

    // Save current active state
    var savedChart = klineChart;
    var savedSymbol = klineChartSymbol;
    var savedTf = klineChartTimeframe;
    var savedActiveIdx = _mcActivePaneIdx;

    _mcPanes[savedActiveIdx].chart = savedChart;
    _mcPanes[savedActiveIdx].containerEl.id = 'mc-chart-' + savedActiveIdx;

    // Swap to companion pane
    _mcActivePaneIdx = idx;
    klineChart = p.chart;
    klineChartSymbol = p.symbol;
    klineChartTimeframe = p.timeframe;
    p.containerEl.id = 'kline-chart-container';

    loadKlineChart(p.symbol, p.timeframe);

    // Wait for chart to render, then restore active pane
    var _checkInterval = setInterval(function() {
        // klineChart gets set by renderKlineChart after data arrives
        if (klineChart && klineChart.getDataList && klineChart.getDataList().length > 0) {
            clearInterval(_checkInterval);
            clearTimeout(_checkTimeout);
            _restoreAndContinue();
        }
    }, 100);
    // Safety timeout — don't wait forever
    var _checkTimeout = setTimeout(function() {
        clearInterval(_checkInterval);
        _restoreAndContinue();
    }, 5000);

    function _restoreAndContinue() {
        _mcPanes[idx].chart = klineChart;
        _mcPanes[idx].containerEl.id = 'mc-chart-' + idx;

        _mcActivePaneIdx = savedActiveIdx;
        klineChart = _mcPanes[savedActiveIdx].chart;
        klineChartSymbol = _mcPanes[savedActiveIdx].symbol;
        klineChartTimeframe = _mcPanes[savedActiveIdx].timeframe;
        _mcPanes[savedActiveIdx].containerEl.id = 'kline-chart-container';
        _mcStampRendered();
        _mcUpdateChromeInsets();

        window._mcCompanionLoading = false;
        _mcUpdateHeaders();
        if (callback) callback();
    }
}

// --- Update OHLCV tooltip style on pane 0 when layout changes ---
function _mcUpdateTooltipStyle() {
    var ch = (_mcActivePaneIdx === 0) ? klineChart : _mcPanes[0].chart;
    if (!ch || !ch.setStyles) return;
    var compact = _mcLayout !== 'single';
    ch.setStyles({
        candle: {
            tooltip: {
                text: { size: compact ? 10 : 12 },
                custom: compact ? [
                    {title: 'T: ', value: '{time}'}, {title: 'O: ', value: '{open}'},
                    {title: 'H: ', value: '{high}'}, {title: 'L: ', value: '{low}'},
                    {title: 'C: ', value: '{close}'}, {title: 'V: ', value: '{volume}'}
                ] : [
                    {title: 'time', value: '{time}'}, {title: 'open', value: '{open}'},
                    {title: 'high', value: '{high}'}, {title: 'low', value: '{low}'},
                    {title: 'close', value: '{close}'}, {title: 'volume', value: '{volume}'}
                ]
            }
        }
    });
}

// --- Resize indicator sub-panes for current layout (single vs multi) ---
// Remove+recreate to work around KLineChart v9.8.12 setPaneOptions shrink bug.
// Must be called AFTER chart.resize() so KLineChart knows the container dimensions.
function _mcResizeIndicatorPanes() {
    var isMulti = _mcLayout !== 'single';
    var volH = isMulti ? 55 : 100;
    var rsiH = isMulti ? 55 : 90;
    var macdH = isMulti ? 45 : 70;
    var minH = isMulti ? 25 : 40;

    if (typeof klineActiveIndicators === 'undefined') return;
    var _builtInPanes = (typeof IndicatorRegistry !== 'undefined') ? IndicatorRegistry.getByType('builtin_pane') : null;

    _mcPanes.forEach(function(p, i) {
        var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
        if (!ch || !ch.createIndicator) return;

        if (_builtInPanes) {
            for (var _bk in _builtInPanes) {
                if (!klineActiveIndicators[_bk]) continue;
                var _bi = _builtInPanes[_bk];
                var _bh = _bk === 'VOL' ? volH : (_bk === 'MACD' ? macdH : rsiH);
                try { ch.removeIndicator('candle_pane', _bk); } catch(e) {}
                try { ch.removeIndicator(_bi.paneId); } catch(e) {}
                var createArg = _bi.calcParams ? { name: _bk, calcParams: _bi.calcParams } : _bk;
                try { ch.createIndicator(createArg, false, { id: _bi.paneId, height: _bh, minHeight: minH }); } catch(e) {}
            }
        }
    });
}

// --- Resize all panes ---
function mcResizeAll() {
    if (!_mcInitialized) return;
    _mcPanes.forEach(function(p, i) {
        var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
        if (ch) ch.resize();
    });
    _mcUpdateChromeInsets();
}

// --- Getters ---
function mcIsMultiChart() { return _mcLayout !== 'single'; }
function mcIsRotating() { return _mcRotateMode; }

// Resolve the correct KLineChart instance for a given container element.
// Touch handlers must use this instead of the global klineChart,
// because companion panes have their own chart instances.
function mcGetChartForContainer(containerEl) {
    if (!_mcInitialized || _mcPanes.length <= 1) return klineChart;
    for (var i = 0; i < _mcPanes.length; i++) {
        if (_mcPanes[i].containerEl === containerEl) {
            return (i === _mcActivePaneIdx) ? klineChart : _mcPanes[i].chart;
        }
    }
    return klineChart;
}

// Ensure the pane containing containerEl is active (for touch interactions).
function mcActivatePaneForContainer(containerEl) {
    if (!_mcInitialized || _mcPanes.length <= 1) return;
    for (var i = 0; i < _mcPanes.length; i++) {
        if (_mcPanes[i].containerEl === containerEl && i !== _mcActivePaneIdx) {
            mcSetActivePane(i);
            return;
        }
    }
}

// ==================== Play / Favourite Rotation ====================

var _mcRotateMode = false;
var _mcRotateSource = '';       // 'plays' or 'favourites'
var _mcRotateItems = [];
var _mcRotatePages = [];
var _mcRotatePageIdx = 0;
var _mcRotateInterval = 30;  // must be one of the dropdown options (30/45/60/...) or the select renders blank
var _mcRotateTimer = null;
var _mcRotatePaused = false;
var _mcRotateProgressStart = 0;
var _mcRotateProgressTimer = null;   // unused; kept only because the stop paths still clear it
var _mcRotateProgressRaf = null;     // requestAnimationFrame id of the progress-bar tick
var _mcRotatePreState = null;

function mcToggleRotation(source) {
    source = source || 'plays';
    // Starting rotation exits spread mode
    if (_mcSpreadMode) { _mcSpreadMode = false; _mcUpdateSpreadUI(); }
    if (_mcRotateMode && _mcRotateSource === source) {
        mcStopRotation();
    } else {
        if (_mcRotateMode) mcStopRotation();
        mcStartRotation(source);
    }
}

function mcStartRotation(source) {
    if (_mcRotateMode) return;
    _mcRotateSource = source || 'plays';

    // Save pre-rotation state
    _mcRotatePreState = {
        panes: _mcPanes.map(function(p, i) {
            return {
                symbol: (i === _mcActivePaneIdx) ? klineChartSymbol : p.symbol,
                timeframe: (i === _mcActivePaneIdx) ? klineChartTimeframe : p.timeframe
            };
        })
    };

    // Show loading indicator
    var overlay = document.getElementById('mc-rotate-overlay');
    if (overlay) {
        overlay.style.display = 'flex';
        var statusEl = document.getElementById('mc-rotate-status');
        if (statusEl) statusEl.textContent = 'Loading\u2026';
    }

    // Hide chrome: action-bar, sidebar, drawer, divider
    document.body.classList.add('mc-rotation-active');
    // Keep screen awake
    _mcWakeLockAcquire();

    var fetchFn = _mcRotateSource === 'favourites' ? _mcFetchFavourites : _mcFetchPlays;

    fetchFn().then(function(items) {

        // Bail if nothing to rotate through
        if (!items.length) {
            if (overlay) overlay.style.display = 'none';
            document.body.classList.remove('mc-rotation-active');
            _mcRotatePreState = null;
            if (typeof showToast === 'function') {
                showToast(_mcRotateSource === 'favourites'
                    ? 'Add at least 2 assets to your watchlist to rotate.'
                    : 'No active plays to rotate through.');
            }
            return;
        }

        _mcRotateItems = items;
        _mcRotateMode = true;
        _mcRotatePaused = false;
        _mcRotatePageIdx = 0;

        _mcRotateComputePages();

        // Guard against 1-item rotation — otherwise the timer silently
        // reloads the same asset every interval and nothing visibly changes.
        if (_mcRotatePages.length < 2) {
            _mcRotateMode = false;
            if (overlay) overlay.style.display = 'none';
            document.body.classList.remove('mc-rotation-active');
            _mcRotatePreState = null;
            if (typeof showToast === 'function') {
                showToast(_mcRotateSource === 'favourites'
                    ? 'Need at least 2 watchlist assets to rotate.'
                    : 'Need at least 2 active plays to rotate.');
            }
            return;
        }

        _mcRotateLoadPage(0);
        _mcRotateResetTimer();
        _mcRotateUpdateUI();
        // Resize after DOM reflow from hiding chrome
        requestAnimationFrame(mcResizeAll);
    });
}

function mcStopRotation() {
    if (!_mcRotateMode) return;
    _mcRotateMode = false;

    if (_mcRotateTimer) { clearInterval(_mcRotateTimer); _mcRotateTimer = null; }
    if (_mcRotateProgressTimer) { clearInterval(_mcRotateProgressTimer); _mcRotateProgressTimer = null; }
    if (_mcRotateProgressRaf) { cancelAnimationFrame(_mcRotateProgressRaf); _mcRotateProgressRaf = null; }

    // Restore chrome: action-bar, sidebar, drawer, divider
    document.body.classList.remove('mc-rotation-active');
    _mcWakeLockRelease();

    // Restore original charts
    if (_mcRotatePreState && _mcRotatePreState.panes.length) {
        var p0 = _mcRotatePreState.panes[0];
        if (p0) {
            klineChartSymbol = p0.symbol;
            klineChartTimeframe = p0.timeframe;
            _mcPanes[0].symbol = p0.symbol;
            _mcPanes[0].timeframe = p0.timeframe;
            loadKlineChart(p0.symbol, p0.timeframe);
        }
        var compQueue = [];
        for (var i = 1; i < _mcPanes.length && i < _mcRotatePreState.panes.length; i++) {
            _mcPanes[i].symbol = _mcRotatePreState.panes[i].symbol;
            _mcPanes[i].timeframe = _mcRotatePreState.panes[i].timeframe;
            compQueue.push(i);
        }
        if (compQueue.length) {
            var poll = setInterval(function() {
                if (klineChart && klineChart.getDataList && klineChart.getDataList().length > 0) {
                    clearInterval(poll); clearTimeout(fb);
                    (function loadNext() {
                        if (!compQueue.length) { mcResizeAll(); _mcUpdateHeaders(); return; }
                        _mcLoadCompanionPane(compQueue.shift(), loadNext);
                    })();
                }
            }, 100);
            var fb = setTimeout(function() { clearInterval(poll); _mcUpdateHeaders(); }, 5000);
        }
        _mcRotatePreState = null;
    }

    _mcRotateUpdateUI();
    _mcUpdateHeaders();
    _mcUpdateChromeInsets();
}

function _mcRotateComputePages() {
    var count = Math.max(1, _mcPanes.length);
    _mcRotatePages = [];
    for (var i = 0; i < _mcRotateItems.length; i += count) {
        _mcRotatePages.push(_mcRotateItems.slice(i, i + count));
    }
}

function _mcRotateLoadPage(pageIdx) {
    var page = _mcRotatePages[pageIdx];
    if (!page || !page.length) return;

    _mcRotatePageIdx = pageIdx;
    _mcRotateProgressStart = Date.now();

    // For favourites rotation, sync all items to the current TF
    // (user may have changed TF on the main pane during rotation)
    if (_mcRotateSource === 'favourites') {
        var curTf = klineChartTimeframe || '1D';
        for (var t = 0; t < _mcRotateItems.length; t++) {
            _mcRotateItems[t].timeframe = curTf;
        }
        // Refresh current page reference
        page = _mcRotatePages[pageIdx];
    }

    // Ensure pane 0 is active
    if (_mcActivePaneIdx !== 0) mcSetActivePane(0);

    // Dispose ALL existing charts first — prevents stale poll-checks on companion panes
    _mcPanes.forEach(function(p, i) {
        var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
        if (ch && p.containerEl) {
            try { klinecharts.dispose(p.containerEl); } catch(e) {}
        }
        p.chart = null;
    });
    klineChart = null;

    // Set all pane targets
    for (var i = 0; i < _mcPanes.length && i < page.length; i++) {
        _mcPanes[i].symbol = page[i].asset;
        _mcPanes[i].timeframe = page[i].timeframe;
    }

    // Load pane 0
    klineChartSymbol = page[0].asset;
    klineChartTimeframe = page[0].timeframe;
    loadKlineChart(page[0].asset, page[0].timeframe);

    // Deferred scroll — chart layout may not be settled yet during rotation transitions.
    // Without this, phantom bars cause _scrollToLastReal to miscalculate and show empty chart.
    setTimeout(function() {
        if (klineChart && typeof _scrollToLastReal === 'function') {
            _scrollToLastReal();
            klineChart.resize();
        }
    }, 400);

    // Build companion queue
    var compQueue = [];
    for (var j = 1; j < _mcPanes.length && j < page.length; j++) {
        compQueue.push(j);
    }

    if (compQueue.length) {
        // Start companions after a short delay — don't wait for pane 0 success.
        // Do not wait on pane 0: a 404 there would block all companions.
        setTimeout(function() {
            (function loadNext() {
                if (!compQueue.length) {
                    mcResizeAll();
                    _mcRotateUpdateHeaders();
                    _mcRotatePrefetchNext();
                    _mcSubscribeCrosshairSync();
                    return;
                }
                _mcLoadCompanionPane(compQueue.shift(), loadNext);
            })();
        }, 300);
    } else {
        _mcRotateUpdateHeaders();
        _mcRotatePrefetchNext();
    }
}

function _mcRotatePrefetchNext() {
    if (!_mcRotateMode || _mcRotatePages.length <= 1) return;
    var nextIdx = (_mcRotatePageIdx + 1) % _mcRotatePages.length;
    var nextPage = _mcRotatePages[nextIdx];
    if (!nextPage) return;

    nextPage.forEach(function(item) {
        // Preload chart data into the KLine cache
        if (typeof preloadKlineData === 'function') {
            preloadKlineData(item.asset, item.timeframe);
        }
    });
}

function _mcRotateResetTimer() {
    if (_mcRotateTimer) { clearInterval(_mcRotateTimer); _mcRotateTimer = null; }
    if (_mcRotateProgressTimer) { clearInterval(_mcRotateProgressTimer); _mcRotateProgressTimer = null; }
    if (_mcRotateProgressRaf) { cancelAnimationFrame(_mcRotateProgressRaf); _mcRotateProgressRaf = null; }

    _mcRotateProgressStart = Date.now();

    _mcRotateTimer = setInterval(function() {
        if (!_mcRotatePaused && _mcRotateMode) {
            var next = (_mcRotatePageIdx + 1) % _mcRotatePages.length;
            _mcRotateLoadPage(next);
        }
    }, _mcRotateInterval * 1000);

    // Progress bar is driven by requestAnimationFrame so it aligns to the display
    // refresh rate (60/120Hz); a fixed 50ms interval (20Hz) visibly steps on mobile.
    function _progressTick() {
        if (!_mcRotateMode) { _mcRotateProgressRaf = null; return; }
        if (!_mcRotatePaused) {
            var bar = document.getElementById('mc-rotate-progress');
            if (bar) {
                var elapsed = (Date.now() - _mcRotateProgressStart) / 1000;
                bar.style.width = Math.min(100, (elapsed / _mcRotateInterval) * 100) + '%';
            }
        }
        _mcRotateProgressRaf = requestAnimationFrame(_progressTick);
    }
    _mcRotateProgressRaf = requestAnimationFrame(_progressTick);
}

function _mcRotateTogglePause() {
    _mcRotatePaused = !_mcRotatePaused;
    if (!_mcRotatePaused) _mcRotateProgressStart = Date.now();
    _mcRotateUpdateUI();
}

function _mcRotateUpdateHeaders() {
    var page = _mcRotatePages[_mcRotatePageIdx];
    if (!page) return;

    // Update counter
    var counterEl = document.getElementById('mc-rotate-counter');
    if (counterEl) counterEl.textContent = (_mcRotatePageIdx + 1) + '/' + _mcRotatePages.length;

    // In single mode, show info in overlay status
    var statusEl = document.getElementById('mc-rotate-status');
    if (statusEl && !_mcRotatePaused) {
        if (_mcLayout === 'single' && page[0]) {
            var label = page[0].asset + ' ' + page[0].timeframe;
            if (page[0].name) label += ' \u2014 ' + page[0].name;
            statusEl.textContent = label;
        } else {
            statusEl.textContent = '';
        }
    }

    // Update pane headers with play/asset names
    _mcPanes.forEach(function(p, i) {
        var nameEl = p.paneEl.querySelector('.mc-pane-name');
        if (!nameEl) return;
        if (i < page.length && page[i].name) {
            nameEl.textContent = '\u2014 ' + page[i].name;
            nameEl.style.display = '';
        } else {
            nameEl.style.display = 'none';
        }
    });

    _mcUpdateHeaders();
}

function _mcRotateUpdateUI() {
    var overlay = document.getElementById('mc-rotate-overlay');
    var scenBtn = document.getElementById('mc-rotate-btn');
    var favBtn = document.getElementById('mc-rotate-fav-btn');

    if (overlay) overlay.style.display = _mcRotateMode ? 'flex' : 'none';
    if (scenBtn) scenBtn.classList.toggle('active', _mcRotateMode && _mcRotateSource === 'plays');
    if (favBtn) favBtn.classList.toggle('active', _mcRotateMode && _mcRotateSource === 'favourites');

    var pauseBtn = document.getElementById('mc-rotate-pause');
    if (pauseBtn) pauseBtn.innerHTML = _mcRotatePaused ? '&#9654;' : '&#9646;&#9646;';

    var statusEl = document.getElementById('mc-rotate-status');
    if (statusEl && _mcRotatePaused) statusEl.textContent = 'Paused';
}

function _mcFetchPlays() {
    return fetch('/api/plays/active', { credentials: 'same-origin' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var plays = data.plays || [];
            if (!plays.length) return [];
            return plays.map(function(p) {
                return {
                    asset: p.primary_asset || 'BTC',
                    timeframe: p.timeframe || '1D',
                    name: p.title || 'Untitled',
                    playId: p.id
                };
            });
        })
        .catch(function(err) {
            console.error('mcRotate: fetch error:', err);
            return [];
        });
}

function _mcFetchFavourites() {
    var symbols = (typeof getAllWatchlistSymbols === 'function') ? getAllWatchlistSymbols() : [];
    var tf = klineChartTimeframe || '1D';
    var items = symbols.map(function(sym) {
        return { asset: sym, timeframe: tf, name: '' };
    });
    return Promise.resolve(items);
}

// ==================== Timeframe Spread ====================

var _mcSpreadTfs = ['1h', '4h', '1D', '1W'];
var _mcSpreadMode = false;
var _mcSkipCompanionLoad = false;  // Set by spread mode to prevent mcSetLayout from loading companions

function _mcGetSpreadTfs(sym) {
    var tfs = _mcSpreadTfs.slice();
    if (typeof isCryptoAsset === 'function' && !isCryptoAsset(sym)) {
        tfs = tfs.filter(function(tf) { return ['1m','3m','5m','15m','30m'].indexOf(tf) < 0; });
    }
    if (!window._isPremium) {
        tfs = tfs.filter(function(tf) { return ['1m','3m','5m','15m','30m','1h'].indexOf(tf) < 0; });
    }
    if (tfs.length < 2) tfs = ['4h', '1D', '1W', '1M'];
    return tfs;
}

function _mcSpreadLoadAll(sym) {
    var tfs = _mcGetSpreadTfs(sym);
    var paneCount = _mcPanes.length;

    // Ensure pane 0 is active before we start
    if (_mcActivePaneIdx !== 0) mcSetActivePane(0);

    // Dispose ALL existing charts
    _mcPanes.forEach(function(p, i) {
        var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
        if (ch && p.containerEl) {
            try { klinecharts.dispose(p.containerEl); } catch(e) {}
        }
        p.chart = null;
    });
    klineChart = null;

    // Assign symbol + timeframes to each pane
    for (var i = 0; i < paneCount; i++) {
        _mcPanes[i].symbol = sym;
        _mcPanes[i].timeframe = tfs[i % tfs.length];
    }

    // Update TF button bar to reflect pane 0's timeframe
    klineChartSymbol = sym;
    klineChartTimeframe = tfs[0];
    var tfContainer = document.getElementById('tf-buttons-container');
    if (tfContainer && typeof buildTfButtons === 'function') {
        tfContainer.innerHTML = buildTfButtons(sym, tfs[0]);
        tfContainer.querySelectorAll('.tf-selector').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var tf = this.getAttribute('data-tf');
                tfContainer.querySelectorAll('.tf-selector').forEach(function(b) { b.classList.remove('active'); });
                this.classList.add('active');
                klineChartTimeframe = tf;
                loadKlineChart(klineChartSymbol, tf);
            });
        });
    }

    // Prefetch ALL timeframes in parallel, then render sequentially.
    // This eliminates network wait between panes — only rendering is sequential
    // (because the global klineChart/klineChartSymbol swap requires it).
    var ltfSet = ['1m','3m','5m','15m','30m','1h','4h'];
    var fetchPromises = tfs.slice(0, paneCount).map(function(tf) {
        var cacheKey = sym + '_' + tf;
        var cached = (typeof klineCacheGet === 'function') ? klineCacheGet(cacheKey) : null;
        if (cached && !cached._lite) return Promise.resolve();
        var mode = ltfSet.indexOf(tf) >= 0 ? 'ltf' : 'htf';
        var maxBars = (mode === 'ltf') ? 2500 : 1500;
        var derivParam = (typeof _derivativesParam === 'function') ? _derivativesParam() : '';
        return fetch('/api/chart/' + encodeURIComponent(sym) + '?days=' + maxBars + '&mode=' + mode + '&timeframe=' + encodeURIComponent(tf) + derivParam, {
            credentials: 'same-origin'
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data && data.ohlcv) { klineCacheSave(cacheKey, data); }
        }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/chart/ failed — rendered as empty', String((e && e.message) || e)); });
    });

    Promise.all(fetchPromises).then(function() {
        // All data cached — now render sequentially
        // Pane 0 (active)
        klineChartSymbol = sym;
        klineChartTimeframe = tfs[0];
        loadKlineChart(sym, tfs[0]);

        // Build companion queue
        var compQueue = [];
        for (var j = 1; j < paneCount; j++) compQueue.push(j);

        if (compQueue.length) {
            // Wait for pane 0 to finish rendering
            var _p0Poll = setInterval(function() {
                if (klineChart && klineChart.getDataList && klineChart.getDataList().length > 0) {
                    clearInterval(_p0Poll);
                    clearTimeout(_p0Timeout);
                    _renderNextCompanion();
                }
            }, 50);
            var _p0Timeout = setTimeout(function() {
                clearInterval(_p0Poll);
                _renderNextCompanion();
            }, 5000);

            function _renderNextCompanion() {
                if (!compQueue.length) {
                    mcResizeAll();
                    _mcUpdateHeaders();
                    _mcSubscribeCrosshairSync();
                    return;
                }
                _mcLoadCompanionPane(compQueue.shift(), _renderNextCompanion);
            }
        } else {
            _mcUpdateHeaders();
            _mcUpdateChromeInsets();
        }
    });

    _mcSaveState();
}

function mcSpreadTimeframes() {
    if (!_mcInitialized) return;

    // Toggle mode
    _mcSpreadMode = !_mcSpreadMode;
    _mcUpdateSpreadUI();

    if (!_mcSpreadMode) return;  // Just turned off — done

    var sym = klineChartSymbol;
    if (!sym) return;

    // Stop rotation if active
    if (_mcRotateMode) {
        _mcRotateMode = false;
        if (_mcRotateTimer) { clearInterval(_mcRotateTimer); _mcRotateTimer = null; }
        if (_mcRotateProgressTimer) { clearInterval(_mcRotateProgressTimer); _mcRotateProgressTimer = null; }
        if (_mcRotateProgressRaf) { cancelAnimationFrame(_mcRotateProgressRaf); _mcRotateProgressRaf = null; }
        document.body.classList.remove('mc-rotation-active');
        _mcWakeLockRelease();
        _mcRotateUpdateUI();
    }

    // Ensure we're in quad layout (4 panes)
    if (_mcLayout !== 'quad') {
        // Prevent mcSetLayout from resetting spread mode and loading companions
        _mcSkipCompanionLoad = true;
        mcSetLayout('quad');
        _mcSkipCompanionLoad = false;
        // Restore spread mode (mcSetLayout resets it)
        _mcSpreadMode = true;
        _mcUpdateSpreadUI();
    }

    // Wait for layout to settle, then load all panes
    setTimeout(function() { _mcSpreadLoadAll(sym); }, 100);
}

// Called from asset-search.js / split-layout.js when user switches asset
function mcSpreadOnAssetChange(symbol) {
    if (!_mcSpreadMode || !_mcInitialized || _mcPanes.length <= 1) return false;
    _mcSpreadLoadAll(symbol);
    return true;  // Signal that spread mode handled the load
}

function _mcUpdateSpreadUI() {
    var btn = document.getElementById('mc-spread-btn');
    if (btn) btn.classList.toggle('active', _mcSpreadMode);
}

// ==================== Synchronized Crosshairs ====================

var _mcCrosshairSyncActive = false;
var _mcCrosshairSyncing = false;  // Prevent infinite loops
var _mcCrosshairHandlers = [];    // [{chart, handler}] for clean unsubscribe

function _mcSubscribeCrosshairSync() {
    // Clean up any previous subscriptions first
    _mcUnsubscribeCrosshairSync();

    if (_mcPanes.length <= 1) return;
    _mcCrosshairSyncActive = true;
    _mcCrosshairHandlers = [];

    _mcPanes.forEach(function(p, i) {
        var ch = (i === _mcActivePaneIdx) ? klineChart : p.chart;
        if (!ch || !ch.subscribeAction) return;

        var handler = function(params) {
            if (_mcCrosshairSyncing) return;
            if (!params || !_mcCrosshairSyncActive) return;

            // Get timestamp and price from the crosshair event
            var kLineData = params.kLineData;
            if (!kLineData || !kLineData.timestamp) return;
            var timestamp = kLineData.timestamp;

            // Get the price at the crosshair's y position from source chart
            var srcChart = (i === _mcActivePaneIdx) ? klineChart : p.chart;
            var price = null;
            if (params.y !== undefined && srcChart && srcChart.convertFromPixel) {
                try {
                    var fromPx = srcChart.convertFromPixel({ y: params.y }, { paneId: 'candle_pane' });
                    if (fromPx && fromPx.value !== undefined) price = fromPx.value;
                } catch(e) {}
            }

            _mcCrosshairSyncing = true;
            _mcPanes.forEach(function(op, oi) {
                if (oi === i) return;
                var och = (oi === _mcActivePaneIdx) ? klineChart : op.chart;
                if (!och || !och.executeAction || !och.getDataList) return;
                var targetData = och.getDataList();
                if (!targetData || targetData.length === 0) return;

                // Binary search for nearest timestamp
                var lo = 0, hi = targetData.length - 1, bestIdx = 0;
                while (lo <= hi) {
                    var mid = Math.floor((lo + hi) / 2);
                    if (targetData[mid].timestamp === timestamp) { bestIdx = mid; break; }
                    if (targetData[mid].timestamp < timestamp) { bestIdx = mid; lo = mid + 1; }
                    else { hi = mid - 1; }
                }
                if (bestIdx + 1 < targetData.length) {
                    if (Math.abs(targetData[bestIdx + 1].timestamp - timestamp) < Math.abs(targetData[bestIdx].timestamp - timestamp)) {
                        bestIdx = bestIdx + 1;
                    }
                }

                try {
                    // Get pixel x from data index
                    var pixelX = och.convertToPixel({ dataIndex: bestIdx }, { paneId: 'candle_pane' });
                    if (!pixelX || pixelX.x === undefined) return;

                    var crosshairData = { x: pixelX.x, paneId: 'candle_pane' };

                    // Convert source price to target chart's y pixel
                    if (price !== null) {
                        var pixelY = och.convertToPixel({ value: price }, { paneId: 'candle_pane' });
                        if (pixelY && pixelY.y !== undefined) {
                            crosshairData.y = pixelY.y;
                        }
                    }

                    och.executeAction('onCrosshairChange', crosshairData);

                    // Apply candle color to synced crosshair
                    if (typeof window._setCrosshairColorForChart === 'function') {
                        var candle = targetData[bestIdx];
                        if (candle && price !== null) {
                            var bodyHigh = Math.max(candle.open, candle.close);
                            var bodyLow = Math.min(candle.open, candle.close);
                            if (price >= bodyLow && price <= bodyHigh) {
                                window._setCrosshairColorForChart(och, candle.close >= candle.open ? '#26a69a' : '#ef5350');
                            } else {
                                window._setCrosshairColorForChart(och, '#787b86');
                            }
                        } else {
                            window._setCrosshairColorForChart(och, '#787b86');
                        }
                    }
                } catch(e) {}
            });
            _mcCrosshairSyncing = false;
        };

        ch.subscribeAction('onCrosshairChange', handler);
        _mcCrosshairHandlers.push({ chart: ch, handler: handler });
    });
}

function _mcUnsubscribeCrosshairSync() {
    _mcCrosshairSyncActive = false;
    // Unsubscribe using specific handler references (doesn't remove other crosshair subscribers)
    _mcCrosshairHandlers.forEach(function(entry) {
        if (entry.chart && entry.chart.unsubscribeAction) {
            try { entry.chart.unsubscribeAction('onCrosshairChange', entry.handler); } catch(e) {}
        }
    });
    _mcCrosshairHandlers = [];
}

// --- Cleanup (called before closeAdvancedChartModal disposes primary) ---
function mcDestroy() {
    _mcUnsubscribeCrosshairSync();
    if (!_mcInitialized) return;
    // Stop rotation if active
    if (_mcRotateMode) {
        _mcRotateMode = false;
        if (_mcRotateTimer) { clearInterval(_mcRotateTimer); _mcRotateTimer = null; }
        if (_mcRotateProgressTimer) { clearInterval(_mcRotateProgressTimer); _mcRotateProgressTimer = null; }
        if (_mcRotateProgressRaf) { cancelAnimationFrame(_mcRotateProgressRaf); _mcRotateProgressRaf = null; }
        document.body.classList.remove('mc-rotation-active');
        _mcWakeLockRelease();
    }
    // Save active chart ref
    if (_mcPanes.length > _mcActivePaneIdx) {
        _mcPanes[_mcActivePaneIdx].chart = klineChart;
    }
    // Dispose companions (primary is disposed by closeAdvancedChartModal)
    for (var i = _mcPanes.length - 1; i > 0; i--) {
        if (_mcPanes[i].chart && _mcPanes[i].containerEl) {
            try { klinecharts.dispose(_mcPanes[i].containerEl); } catch(e) {}
        }
        if (_mcPanes[i].paneEl) _mcPanes[i].paneEl.remove();
    }
    // Restore primary as global
    if (_mcPanes.length > 0) {
        klineChart = _mcPanes[0].chart;
        _mcPanes[0].containerEl.id = 'kline-chart-container';
    }
    _mcPanes = [];
    _mcActivePaneIdx = 0;
    _mcLayout = 'single';
    _mcInitialized = false;
    try { localStorage.removeItem('mcState'); } catch(e) {}
}

// ==================== Screen Wake Lock ====================

var _mcWakeLock = null;

function _mcWakeLockAcquire() {
    if (!('wakeLock' in navigator)) return;
    navigator.wakeLock.request('screen').then(function(lock) {
        _mcWakeLock = lock;
        _mcWakeLock.addEventListener('release', function() { _mcWakeLock = null; });
    }).catch(function(err) {
        console.warn('Wake lock failed:', err.message);
    });
}

function _mcWakeLockRelease() {
    if (_mcWakeLock) {
        _mcWakeLock.release();
        _mcWakeLock = null;
    }
}

// Re-acquire wake lock when page becomes visible again (auto-released on tab switch)
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible' && _mcRotateMode && !_mcWakeLock) {
        _mcWakeLockAcquire();
    }
});
