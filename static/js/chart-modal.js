// The chart modal: opening and closing it (openAdvancedChartModal /
// closeAdvancedChartModal), its scroll lock and resize handling, the
// timeframe bar and editor, and the toolbar menus. Needs app-core.js loaded
// first; everything else it calls is only reached at runtime.

function openChartModalFullscreen(symbol) {
    // Open chart modal directly in fullscreen mode
    klineIsFullscreen = true;
    openAdvancedChartModal(symbol);
}

function closeChartModal() {
    exitAddTPMode();
    if (chartModal) {
        chartModal.classList.remove('visible');
        syncChartModalScrollLock();
    }
    if (chartModalInstance) {
        chartModalInstance.remove();
        chartModalInstance = null;
    }
    // If chart was opened via ?chart= param, go back to referring page
    if (window._chartOpenedFromParam) {
        window._chartOpenedFromParam = false;
        sessionStorage.removeItem('klineOpenSymbol');
        history.back();
    }
}

// State-derived body scroll lock for the fullscreen chart modal (klineModal,
// and the legacy `chartModal`). The lock must not be an imperative inline
// style (document.body.style.overflow = 'hidden'/'') written by
// openAdvancedChartModal/closeAdvancedChartModal/closeChartModal: clearing it
// would depend on one of those closers actually running, and any modal exit
// that skips them — e.g. bfcache restoring the page after chart -> Journal ->
// OS-back runs NO app JS at all (see the pageshow(persisted) hook below) —
// would strand the lock: page vertical scroll dead while the list's own
// horizontal overflow still works ("scrolls right, not down").
//
// So the lock is STATE-DERIVED (a body class + a CSS rule,
// `body.chart-modal-open { overflow: hidden }` in main.css) instead of an
// imperative write/clear pair that has to be remembered on every exit path.
// This function is the single source of truth for that class — call it any
// time modal state changes, OR whenever it might have silently changed out
// from under the page (bfcache restore, cold PWA startup), and it leaves
// body in the state that MATCHES REALITY regardless of what ran before.
// Idempotent and cheap; safe to call liberally.
//
// `cleanLegacyInline` additionally clears a raw inline document.body.style.
// overflow left behind by an older imperative lock (PWA sessions that
// had it stranded before this declarative sync existed). This is scoped to an
// opt-in flag — NOT applied on every call — because a blanket clear would
// also strip other, unrelated modals' (help/premium/api-status/custom-
// indicators/settings/feedback) OWN legitimate inline overflow locks if this
// ran while one of them happened to be open (e.g. bfcache-restoring mid-
// Premium-modal after following its real '/premium' link and hitting back).
// It is only passed `true` from the plain-startup call in window.onload
// below, where it's provably safe: that runs before ANY modal — including
// this one — has had a chance to open this session.
function syncChartModalScrollLock(cleanLegacyInline) {
    var modalOpen = !!(klineModal && klineModal.classList.contains('visible')) ||
        !!(chartModal && chartModal.classList.contains('visible'));
    document.body.classList.toggle('chart-modal-open', modalOpen);
    if (cleanLegacyInline && !modalOpen && document.body.style.overflow) {
        document.body.style.overflow = '';
    }
}

// ResizeObserver for chart container — replaces setTimeout resize hacks
var _chartResizeObserver = null;
function _ensureChartResizeObserver() {
    if (_chartResizeObserver || typeof ResizeObserver === 'undefined') return;
    var container = document.getElementById('kline-chart-container');
    if (!container) return;
    var _lastW = 0, _lastH = 0;
    _chartResizeObserver = new ResizeObserver(function(entries) {
        var r = entries[0].contentRect;
        if (Math.abs(r.width - _lastW) > 1 || Math.abs(r.height - _lastH) > 1) {
            _lastW = r.width; _lastH = r.height;
            if (klineChart) klineChart.resize();
        }
    });
    _chartResizeObserver.observe(container);
}

// Force KLineChart to recalculate internal bounding rects by physically nudging
// the container height. resize() alone won't invalidate the cache when the
// container size hasn't changed (e.g. after TF switch with pane removal/recreation).
function _nudgeChartContainer() {
    if (!klineChart) return;
    var c = document.getElementById('kline-chart-container');
    if (!c) return;
    var origH = c.style.height;
    c.style.height = (c.offsetHeight - 1) + 'px';
    klineChart.resize();
    requestAnimationFrame(function() {
        if (!klineChart) return;
        c.style.height = origH || '';
        klineChart.resize();
    });
}

function _getAllTimeframes(symbol) {
    var crypto = isCryptoAsset(symbol);
    var prem = window._isPremium;
    var all = [];
    if (crypto && prem) all = all.concat(['1m','3m','5m','15m','30m']);
    if (prem) all.push('1h');
    all.push('4h');
    all = all.concat(['1D','3D','1W','1M']);
    return all;
}

function _getTfFavourites() {
    try { var s = JSON.parse(localStorage.getItem('tfFavourites')); if (s && s.length) return s; } catch(e) {}
    return null; // null = show all (default)
}

function _saveTfFavourites(favs) {
    try { localStorage.setItem('tfFavourites', JSON.stringify(favs)); } catch(e) {}
    if (window.PreferenceSync) PreferenceSync.syncPreference('tf_favourites', favs);
}

function buildTfButtons(symbol, currentTf) {
    var all = _getAllTimeframes(symbol);
    var favs = _getTfFavourites();
    var show = favs ? all.filter(function(tf) { return favs.indexOf(tf) !== -1; }) : all;
    // Always include current TF so user can see what's selected
    if (show.indexOf(currentTf) === -1) show.push(currentTf);

    var html = '';
    var prevGroup = null;
    show.forEach(function(tf) {
        var group = ['1m','3m','5m','15m','30m'].indexOf(tf) !== -1 ? 'sub' :
                    ['1h','4h'].indexOf(tf) !== -1 ? 'hourly' : 'daily';
        if (prevGroup && prevGroup !== group) html += '<span class="tf-sep"></span>';
        prevGroup = group;
        var tfLabel = tf.match(/m$/) ? tf : tf.toUpperCase();
        html += '<button class="tf-selector' + (currentTf === tf ? ' active' : '') + '" data-tf="' + tf + '">' + tfLabel + '</button>';
    });
    return html;
}

function _openTfEditor(e) {
    e.stopPropagation();
    var existing = document.getElementById('tf-editor-popup');
    if (existing) { existing.remove(); return; }

    var symbol = typeof klineChartSymbol !== 'undefined' ? klineChartSymbol : 'BTC';
    var all = _getAllTimeframes(symbol);
    var favs = _getTfFavourites();

    var popup = document.createElement('div');
    popup.id = 'tf-editor-popup';
    popup.className = 'tf-editor-popup';
    var ltfHint = !window._isPremium ? '<div style="font-size:0.7rem;color:var(--text-tertiary);margin:6px 0 2px;opacity:0.7">1m–1h timeframes available with a subscription</div>' : '';
    popup.innerHTML =
        '<div class="tf-editor-title">Visible Timeframes</div>' +
        ltfHint +
        '<div class="tf-editor-pills">' +
        all.map(function(tf) {
            var active = !favs || favs.indexOf(tf) !== -1;
            var label = tf.match(/m$/) ? tf : tf.toUpperCase();
            return '<button class="tf-editor-pill' + (active ? ' active' : '') + '" data-tf="' + tf + '">' + label + '</button>';
        }).join('') +
        '</div>' +
        '<div class="tf-editor-actions">' +
            '<button class="tf-editor-action" id="tf-editor-all">All</button>' +
            '<button class="tf-editor-action" id="tf-editor-reset">Reset</button>' +
        '</div>';

    popup.addEventListener('click', function(ev) { ev.stopPropagation(); });

    // Position near the edit button
    var container = document.getElementById('tf-buttons-container');
    if (container) {
        container.style.position = 'relative';
        container.appendChild(popup);
    }

    // Toggle individual TFs
    popup.querySelectorAll('.tf-editor-pill').forEach(function(pill) {
        pill.addEventListener('click', function() {
            pill.classList.toggle('active');
            _applyTfEditor(popup, symbol);
        });
    });

    // All button
    popup.querySelector('#tf-editor-all').addEventListener('click', function() {
        popup.querySelectorAll('.tf-editor-pill').forEach(function(p) { p.classList.add('active'); });
        _applyTfEditor(popup, symbol);
    });

    // Reset button
    popup.querySelector('#tf-editor-reset').addEventListener('click', function() {
        localStorage.removeItem('tfFavourites');
        if (window.PreferenceSync) PreferenceSync.syncPreference('tf_favourites', null);
        popup.remove();
        _rebuildTfBar(symbol);
    });

    // Close on outside click
    setTimeout(function() {
        document.addEventListener('click', function handler(ev) {
            if (!popup.contains(ev.target)) {
                popup.remove();
                document.removeEventListener('click', handler);
            }
        });
    }, 10);
}

function _applyTfEditor(popup, symbol) {
    var active = [];
    popup.querySelectorAll('.tf-editor-pill.active').forEach(function(p) { active.push(p.dataset.tf); });
    var all = _getAllTimeframes(symbol);
    // If all selected, store null (show all)
    if (active.length === all.length) {
        localStorage.removeItem('tfFavourites');
    } else if (active.length === 0) {
        // Don't allow empty — keep at least current TF
        return;
    } else {
        _saveTfFavourites(active);
    }
    _rebuildTfBar(symbol);
}

function _rebuildTfBar(symbol) {
    var container = document.getElementById('tf-buttons-container');
    if (!container) return;
    var currentTf = typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '1D';
    // Preserve edit zone and editor popup before replacing
    var editZone = container.querySelector('.tf-edit-zone');
    var editorPopup = document.getElementById('tf-editor-popup');
    if (editZone) editZone.remove();
    if (editorPopup) editorPopup.remove();
    container.innerHTML = buildTfButtons(symbol, currentTf);
    if (editZone) container.appendChild(editZone);
    else _addTfEditZone(container);
    if (editorPopup) container.appendChild(editorPopup);
}

function _addTfEditZone(container) {
    if (container.querySelector('.tf-edit-zone')) return;
    var zone = document.createElement('button');
    zone.className = 'tf-edit-zone';
    zone.title = 'Edit visible timeframes';
    zone.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
    zone.addEventListener('click', _openTfEditor);
    container.appendChild(zone);
}

function openAdvancedChartModal(symbol) {
    // Lazy-load chart modules on first open.
    // After first call both promises are resolved, so this is a no-op (~0 ms).
    if (!_carracPackPromise || !_klineDrawingsPromise) {
        var self = symbol;
        Promise.all([ensureCarracPack(), ensureKLineDrawings()]).then(function() {
            openAdvancedChartModal(self);
        }).catch(function(e) {
            console.error('[lazy] chart module load failed:', e);
            // Still attempt to open — partial functionality beats hard failure
            _openAdvancedChartModalImpl(self);
        });
        return;
    }
    _openAdvancedChartModalImpl(symbol);
}

function _openAdvancedChartModalImpl(symbol) {
    // Desktop split layout: route to inline chart panel.
    // Re-validate the LIVE viewport here — don't trust chartPanelReady alone.
    // initSplitLayout() latches chartPanelReady from window.innerWidth at load,
    // but mobile browsers (notably Firefox/Android and installed PWAs) can briefly
    // report innerWidth > 768 during the first layout pass, latching the flag true
    // on a phone. A stale-true flag routes a 414px tap into the desktop panel
    // (#chart-area-content), which is display:none on mobile, so the chart inits
    // into a 0-size container: silent on Chrome (no chart, just the green
    // active-asset highlight), InvalidStateError "Canvas could not create basic
    // draw target" on Firefox. Gating on the live width sends mobile to the
    // fullscreen #kline-modal (whose container is sized), which is the good path.
    if (splitLayoutActive && chartPanelReady && window.innerWidth > 768) {
        selectAssetInPanel(symbol);
        return;
    }
    // Fullscreen (mobile/standalone) path: never reuse the inline split panel
    // (#chart-area-content) or a detached node as klineModal. If we do, the chart
    // HTML gets injected into a container that's hidden on mobile (its
    // .adv-chart-overlay is display:none) and the user just sees the asset list.
    // This happens after a desktop→mobile resize, where the resize handler clears
    // splitLayoutActive/chartPanelReady but leaves klineModal pointing at the panel.
    // Reuse a live #kline-modal if one exists, else force a fresh one below.
    if (klineModal && (klineModal.id === 'chart-area-content' || !document.body.contains(klineModal))) {
        klineModal = document.getElementById('kline-modal') || null;
    }
    // Close existing modals
    hideChartTooltip();
    closeChartModal();

    klineChartSymbol = symbol;
    // Timeframe is "sticky": it follows you across asset switches and sessions
    // (TradingView-style), most-specific first:
    //  1. Same-session reopen of this exact symbol → its session TF (sessionStorage).
    //  2. Otherwise → the last timeframe you used, on ANY asset (localStorage
    //     survives a new session). Guarded by _getAllTimeframes() so a stale,
    //     premium-gated, or symbol-unavailable TF falls through to the default.
    //  3. No remembered TF (first use) → mode default (4h in LTF mode, else 1D).
    var savedTf = sessionStorage.getItem('klineOpenTf');
    var _lastTf = localStorage.getItem('lastViewedTf');
    var _availTfs = (typeof _getAllTimeframes === 'function') ? _getAllTimeframes(symbol) : null;
    var _lastTfOk = _lastTf && (!_availTfs || _availTfs.indexOf(_lastTf) !== -1);
    klineChartTimeframe =
        (savedTf && sessionStorage.getItem('klineOpenSymbol') === symbol) ? savedTf :
        _lastTfOk ? _lastTf :
        (timeframeMode === 'ltf' ? '4h' : '1D');
    try { sessionStorage.setItem('klineOpenSymbol', symbol); sessionStorage.setItem('klineOpenTf', klineChartTimeframe); localStorage.setItem('lastViewedAsset', symbol); localStorage.setItem('lastViewedTf', klineChartTimeframe); } catch(e) {}
    if (window.PreferenceSync) PreferenceSync.syncPreference('chart_prefs', { lastViewedAsset: symbol });

    // Create modal if doesn't exist
    if (!klineModal) {
        klineModal = document.createElement('div');
        klineModal.id = 'kline-modal';
        klineModal.className = 'advanced-chart-modal';
        klineModal.setAttribute('role', 'dialog');
        klineModal.setAttribute('aria-modal', 'true');
        klineModal.setAttribute('aria-label', 'Chart');
        document.body.appendChild(klineModal);
    }

    var asset = allAssets[symbol] || {};

    // Build modal HTML with enhanced features
    klineModal.innerHTML =
        '<div class="adv-chart-overlay"></div>' +
        '<div class="adv-chart-content" id="adv-chart-content">' +
            '<div class="adv-chart-toolbar">' +
                ChartToolbar.buildNavToolbarHTML(symbol, { showCloseButton: true }) +
                ChartToolbar.buildDrawingToolbarHTML() +
            '</div>' +
            '<div class="asset-search-popup" id="asset-search-popup">' +
                '<input class="asset-search-input" id="asset-search-input" placeholder="Search assets..." autocomplete="off">' +
                '<div class="asset-search-results" id="asset-search-results"></div>' +
            '</div>' +
            '<div class="adv-chart-body">' +
                '<div id="kline-chart-container" class="adv-chart-container"></div>' +
            '</div>' +
            '<div class="ms-stage-rail" id="ms-stage-rail"></div>' +
            ChartToolbar.buildDrawerHTML() +
            '<div class="adv-chart-resize-handle" id="resize-handle"></div>' +
        '</div>';

    var advChartOverlay = klineModal.querySelector('.adv-chart-overlay');
    if (advChartOverlay) advChartOverlay.addEventListener('click', closeAdvancedChartModal);

    klineModal.classList.add('visible');
    // state-derived lock, see syncChartModalScrollLock().
    syncChartModalScrollLock();

    // Pause ticker bar rAF loop while modal is visible — the ticker sits in the header
    // behind the fullscreen modal and its continuous transform writes were forcing
    // the overlay's backdrop-filter blur to recompute every frame, starving mobile pan.
    if (typeof window.pauseTickerHard === 'function') window.pauseTickerHard();

    // Initialize Lucide icons in the modal
    if (typeof lucide !== 'undefined') {
        lucide.createIcons({ attrs: { 'stroke-width': 1.75 } });
    }

    // Add TF edit zone
    var tfCont = document.getElementById('tf-buttons-container');
    if (tfCont) _addTfEditZone(tfCont);

    // Drawer tab click handlers
    document.querySelectorAll('.drawer-tab').forEach(function(tab) {
        tab.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleDrawer(this.getAttribute('data-tab'));
        });
    });
    // Arrow key navigation for drawer tabs
    var drawerHandle = document.getElementById('chart-drawer-handle');
    if (window.a11y && drawerHandle) {
        a11y.initTabStripKeyboard(drawerHandle, { tabSelector: '[role="tab"]' });
    }
    var drawerChevron = document.getElementById('drawer-chevron');
    if (drawerChevron) {
        drawerChevron.addEventListener('click', function(e) {
            e.stopPropagation();
            if (drawerOpen && typeof closeDrawer === 'function') closeDrawer();
            else openDrawer(drawerActiveTab || 'alerts');
        });
    }
    var drawerFsBtn = document.getElementById('drawer-fullscreen');
    if (drawerFsBtn) {
        drawerFsBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleDrawerFullscreen();
        });
    }

    // Mobile tab bar — inject on touch devices (PWA v2)
    var _isTouch = (typeof isTouchDevice === 'function') ? isTouchDevice() : ('ontouchstart' in window || navigator.maxTouchPoints > 0);
    if (_isTouch) {
        var existingBar = document.getElementById('mobile-action-bar');
        if (existingBar) existingBar.remove();
        var actionBar = document.createElement('div');
        actionBar.className = 'mobile-action-bar visible';
        actionBar.id = 'mobile-action-bar';
        // Inline SVGs — lucide.createIcons unreliable on dynamically injected mobile bar
        var _si = function(d) { return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' + d + '</svg>'; };
        // 4 primary actions. Pos+Plays(+Log) → Trade sheet; overflow
        // (Widgets/Report/Split/Rotate/Settings) → top-right ⋮.
        actionBar.innerHTML =
            '<button class="mobile-action-btn" data-tab="chart">' + _si('<rect x="2" y="4" width="4" height="16"/><rect x="10" y="8" width="4" height="12"/><rect x="18" y="2" width="4" height="18"/>') + '<span>Chart</span></button>' +
            '<button class="mobile-action-btn" data-tab="draw">' + _si('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>') + '<span>Draw</span></button>' +
            '<button class="mobile-action-btn" data-tab="alerts">' + _si('<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>') + '<span>Alert</span></button>' +
            '<button class="mobile-action-btn" data-tab="trade">' + _si('<path d="m3 16 4 4 4-4"/><path d="M7 20V4"/><path d="m21 8-4-4-4 4"/><path d="M17 4v16"/>') + '<span>Trade</span></button>' +
            '<button class="mobile-action-btn" data-nav="journal">' + _si('<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>') + '<span>Journal</span></button>';
        document.body.appendChild(actionBar);
        document.body.classList.add('mobile-action-bar-active');

        // Add search + draw icons to top toolbar
        _injectMobileTopBarIcons();

        // Wire tab bar — each tab toggles its sheet
        actionBar.addEventListener('click', function(e) {
            // Journal is a full page (no mobile-native sheet) — navigate to it.
            var navBtn = e.target.closest('[data-nav]');
            if (navBtn) {
                e.preventDefault();
                e.stopPropagation();
                if (navBtn.getAttribute('data-nav') === 'journal') window.location.href = '/journal';
                return;
            }
            var btn = e.target.closest('[data-tab]');
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();
            var tab = btn.getAttribute('data-tab');
            if (tab === 'draw') {
                // Draw is a mode, not a sheet — enter the mobile drawing toolset.
                if (_mobileActiveTab) closeMobileSheet();
                if (typeof enterMobileDrawMode === 'function') enterMobileDrawMode();
                return;
            }
            if (_mobileActiveTab === tab) {
                closeMobileSheet();
            } else {
                openMobileSheet(tab);
            }
        });

        // Remove legacy floating zoom controls if present
        var existingZoom = document.getElementById('mobile-zoom-controls');
        if (existingZoom) existingZoom.remove();
    }

    // Apply fullscreen if was fullscreen before
    var content = document.getElementById('adv-chart-content');
    if (klineIsFullscreen && content) {
        content.classList.add('fullscreen');
    }

    // Set up event handlers
    setupKlineChartEvents();

    // Initialize multi-chart layout grid and restore saved state
    var _mcRestored = false;
    if (typeof mcInit === 'function') {
        mcInit();
        _mcRestored = (typeof mcRestoreState === 'function') && mcRestoreState();
    }

    // Load chart data (skip if multi-chart restore handled it)
    if (!_mcRestored) loadKlineChart(symbol, klineChartTimeframe);
}

// Chart-toolbar dropdown menus that behave as "one at a time": opening any
// one of these should close all the others. They use a MIX of open-state
// classes — 'visible' on most, 'open' on the Panes/mc-layout dropdown wired
// in multi-chart.js — so closeAllChartMenus() strips both, and callers don't
// need to know which class a given menu uses.
var CHART_MENU_IDS = [
    'chart-type-menu',
    'indicator-menu',
    'asset-switcher-menu',
    'lines-group-menu',
    'chan-group-menu',
    'fib-group-menu',
    'shape-group-menu',
    'pos-group-menu',
    'mark-group-menu',
    'tools-group-menu',
    'mc-layout-dropdown',
    'tf-compact-menu'
];

// Closes every chart-toolbar dropdown menu except exceptId (pass the id of
// the menu about to be opened, or omit to close them all). Exposed on
// window so multi-chart.js — a separate file — can call it too.
function closeAllChartMenus(exceptId) {
    CHART_MENU_IDS.forEach(function(id) {
        if (id === exceptId) return;
        var el = document.getElementById(id);
        if (el) el.classList.remove('visible', 'open');
    });
}
window.closeAllChartMenus = closeAllChartMenus;

// Container-width-aware toolbar: viewport media queries can't see the
// toolbar's own box — split/multi-chart panes narrow it while the window stays
// wide — so a ResizeObserver toggles the wrap class ('toolbar-narrow') and the
// compact-TF swap ('toolbar-tf-compact') from the measured container width.
function _updateToolbarWidthClasses(el) {
    var w = el.getBoundingClientRect().width;
    if (!w) return; // hidden/detached — keep current classes
    var mobile = window.innerWidth <= 768; // mobile has its own TF dropdown
    el.classList.toggle('toolbar-narrow', w <= 1200);
    el.classList.toggle('toolbar-tf-compact', !mobile && w <= 640);
}
function _initToolbarResizeObserver() {
    var el = document.querySelector('.adv-chart-toolbar');
    if (!el || el._carracRo) return;
    _updateToolbarWidthClasses(el);
    if (!('ResizeObserver' in window)) return;
    var ro = new ResizeObserver(function(entries) {
        entries.forEach(function(en) { _updateToolbarWidthClasses(en.target); });
    });
    ro.observe(el);
    el._carracRo = ro;
}

// Compact TF dropdown — replaces the TF button row when the toolbar container
// is narrow. A pure proxy over the (hidden) TF row: the menu is built from the
// current .tf-selector buttons at open time and item clicks are forwarded to
// the real buttons, so premium gating, per-asset TF lists, multi-chart and
// spread behavior stay identical to clicking the row itself.
function _refreshTfCompactLabel() {
    var lbl = document.getElementById('tf-compact-current');
    if (!lbl) return;
    var active = document.querySelector('#tf-buttons-container .tf-selector.active');
    lbl.textContent = active ? active.textContent.trim()
        : (typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '');
}
function _initTfCompactDropdown() {
    var btn = document.getElementById('tf-compact-btn');
    var menu = document.getElementById('tf-compact-menu');
    var cont = document.getElementById('tf-buttons-container');
    if (!btn || !menu || btn._wired) return;
    btn._wired = true;
    btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var willOpen = !menu.classList.contains('visible');
        closeAllChartMenus(willOpen ? 'tf-compact-menu' : undefined);
        if (willOpen) {
            var html = '';
            document.querySelectorAll('#tf-buttons-container .tf-selector').forEach(function(b) {
                html += '<div class="indicator-menu-item' + (b.classList.contains('active') ? ' active' : '') +
                    '" data-tf="' + escapeHtml(b.getAttribute('data-tf') || '') + '">' + escapeHtml(b.textContent.trim()) + '</div>';
            });
            menu.innerHTML = html;
            menu.querySelectorAll('.indicator-menu-item').forEach(function(item) {
                item.addEventListener('click', function(ev) {
                    ev.stopPropagation();
                    var tf = item.getAttribute('data-tf');
                    var real = document.querySelector('#tf-buttons-container .tf-selector[data-tf="' + tf + '"]');
                    if (real) real.click();
                    else if (typeof switchTimeframe === 'function') switchTimeframe(tf);
                    menu.classList.remove('visible');
                    _refreshTfCompactLabel();
                });
            });
        }
        menu.classList.toggle('visible', willOpen);
    });
    // Track TF changes from every path (raw row handlers, toolbar rebuilds,
    // keyboard shortcuts) by observing the real TF row instead of patching
    // each of the 7 switch sites.
    if (cont && !cont._tfLabelMo && 'MutationObserver' in window) {
        var mo = new MutationObserver(function() { _refreshTfCompactLabel(); });
        mo.observe(cont, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
        cont._tfLabelMo = mo;
    }
    if (!window._tfCompactDocClose) {
        window._tfCompactDocClose = true;
        document.addEventListener('click', function() {
            var m = document.getElementById('tf-compact-menu');
            if (m) m.classList.remove('visible');
        });
    }
    _refreshTfCompactLabel();
}

function setupKlineResize() {
    var handle = document.getElementById('resize-handle');
    var content = document.getElementById('adv-chart-content');
    if (!handle || !content) return;

    var isResizing = false;
    var startX, startY, startWidth, startHeight;

    handle.addEventListener('mousedown', function(e) {
        if (klineIsFullscreen) return; // No resize in fullscreen

        isResizing = true;
        startX = e.clientX;
        startY = e.clientY;
        startWidth = content.offsetWidth;
        startHeight = content.offsetHeight;

        document.addEventListener('mousemove', handleMouseMove);
        document.addEventListener('mouseup', handleMouseUp);
        e.preventDefault();
    });

    function handleMouseMove(e) {
        if (!isResizing) return;

        var newWidth = startWidth + (e.clientX - startX) * 2; // *2 because centered
        var newHeight = startHeight + (e.clientY - startY) * 2;

        // Apply min/max constraints
        newWidth = Math.max(600, Math.min(window.innerWidth - 40, newWidth));
        newHeight = Math.max(400, Math.min(window.innerHeight - 40, newHeight));

        content.style.width = newWidth + 'px';
        content.style.height = newHeight + 'px';
        content.style.maxWidth = 'none';
        content.style.maxHeight = 'none';

        // Save size for next open
        klineModalSize.width = newWidth;
        klineModalSize.height = newHeight;

        // Resize chart
        if (klineChart) {
            klineChart.resize();
        }
    }

    function handleMouseUp() {
        isResizing = false;
        document.removeEventListener('mousemove', handleMouseMove);
        document.removeEventListener('mouseup', handleMouseUp);
    }
}

// Helper: switch to a specific asset on chart
function switchToAsset(symbol) {
    if (typeof klineChartSymbol !== 'undefined') {
        klineChartSymbol = symbol;
        try { sessionStorage.setItem('klineOpenSymbol', symbol); localStorage.setItem('lastViewedAsset', symbol); } catch(e) {}
        if (window.PreferenceSync) PreferenceSync.syncPreference('chart_prefs', { lastViewedAsset: symbol });
        if (typeof loadKlineChart === 'function') loadKlineChart(symbol, klineChartTimeframe);
        var symEl = document.getElementById('adv-chart-symbol');
        if (symEl) symEl.textContent = symbol;
        var nameEl = document.getElementById('adv-chart-name');
        var a = (typeof allAssets !== 'undefined' && allAssets[symbol]) ? allAssets[symbol] : {};
        if (nameEl) nameEl.textContent = a.name || '';
    }
}

function closeAdvancedChartModal() {
    // In split layout, chart stays visible — don't close.
    // Live-viewport gated (see _openAdvancedChartModalImpl): a stale-true
    // chartPanelReady on a phone must not block mobile modal teardown.
    if (splitLayoutActive && chartPanelReady && window.innerWidth > 768) return;
    // Reset drawer state
    drawerOpen = false;
    drawerActiveTab = 'position';
    if (window._drawerPosTimer) { clearTimeout(window._drawerPosTimer); window._drawerPosTimer = null; }
    // Clean up mobile elements
    if (typeof closeMobileSheet === 'function') closeMobileSheet();
    var mobileBar = document.getElementById('mobile-action-bar');
    if (mobileBar) mobileBar.remove();
    document.body.classList.remove('mobile-action-bar-active');
    document.body.classList.remove('mobile-draw-mode');
    if (typeof exitMobileDrawMode === 'function') exitMobileDrawMode();
    _mobileActiveTab = null;
    var zoomCtrl = document.getElementById('mobile-zoom-controls');
    if (zoomCtrl) zoomCtrl.remove();

    // Standalone chart page: navigate back to dashboard
    if (window.isStandaloneChart) {
        try { sessionStorage.removeItem('klineOpenSymbol'); sessionStorage.removeItem('klineOpenTf'); } catch(e) {}
        window.location.href = '/';
        return;
    }
    // Stop arrow key navigation
    _nav.left = _nav.right = _nav.up = _nav.down = false;
    if (_nav.raf) { cancelAnimationFrame(_nav.raf); _nav.raf = null; }
    _nav.speed = 0; _nav.zoomSpeed = 0;
    if (typeof closeDrawer === 'function') closeDrawer();
    scenarioList = []; scenarioOverlayIds = [];
    _sKeyDown = false; _sArrowUsed = false; clearTimeout(_sKeyTimer);
    // Remove keyup listener
    if (klineKeyupHandler) {
        document.removeEventListener('keyup', klineKeyupHandler, true);
        klineKeyupHandler = null;
    }
    // Clean up countdown timer
    if (window._candleCountdownInterval) {
        clearInterval(window._candleCountdownInterval);
        window._candleCountdownInterval = null;
    }
    // Clean up mobile OHLCV ticker — otherwise its 2s interval keeps firing after
    // close and throws on klineChart.getDataList() (klineChart is nulled below).
    // setupMobileOhlcvDisplay() re-arms it (and clears first) on the next open.
    if (window._mobileOhlcvInterval) {
        clearInterval(window._mobileOhlcvInterval);
        window._mobileOhlcvInterval = null;
    }
    // Clean up Volume Profile
    vpOnChartClose();
    // Clean up Key Levels
    klOnChartClose();
    // Clean up Confluence Zones
    confluenceOnChartClose();
    // Clean up Liquidation Heatmap
    liqHeatmapOnChartClose();
    // Hide the floating favourites rail (if undocked) purely visually — do
    // NOT call _favRailSetDocked()/_favRailSavePersisted() here, since that
    // would overwrite the persisted klineFavRail.docked=false with true and
    // permanently forget the user's undocked position. _favRailInit() (run
    // from setupKlineChartEvents on every chart re-open, see below) reads
    // klineFavRail fresh and re-shows the rail from the untouched state.
    if (_favRailEl) _favRailEl.style.display = 'none';
    if (_favDockHintEl) _favDockHintEl.classList.remove('show');
    // Destroy multi-chart companion panes (restores primary as klineChart)
    if (typeof mcDestroy === 'function') mcDestroy();
    // Dispose chart first before removing modal
    if (klineChart) {
        var container = document.getElementById('kline-chart-container');
        if (container) {
            try {
                klinecharts.dispose(container);
            } catch (e) {
            }
        }
        klineChart = null;
        _crosshairMode = false; _crosshairPos = null;
    }
    if (klineModal) {
        klineModal.classList.remove('visible');
        // state-derived lock, see syncChartModalScrollLock().
        syncChartModalScrollLock();
    }
    klineChartSymbol = null;
    try { sessionStorage.removeItem('klineOpenSymbol'); sessionStorage.removeItem('klineOpenTf'); } catch(e) {}

    // Resume ticker bar rAF loop (paused in openAdvancedChartModal)
    if (typeof window.resumeTickerHard === 'function') window.resumeTickerHard();
}
