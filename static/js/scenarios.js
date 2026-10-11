/* Scenario System */

// =============================================================================
// Scenario System
// =============================================================================

// Floating panel functions removed — all UI is now in the bottom drawer

// --- Consolidated draggable resizer for two-column layouts ---
var _resizerRatios = {};
function _loadResizerRatio(key) {
    if (_resizerRatios[key] !== undefined) return _resizerRatios[key];
    try { var v = parseFloat(localStorage.getItem(key)); _resizerRatios[key] = isNaN(v) ? null : v; } catch(e) { _resizerRatios[key] = null; }
    return _resizerRatios[key];
}

function initResizer(container, prefix, minLeft, minRight, storageKey) {
    var resizer = container.querySelector('.' + prefix + '-resizer');
    var layout = container.querySelector('.' + prefix + '-layout');
    if (!resizer || !layout) return;
    var left = layout.querySelector('.' + prefix + '-left') || layout.querySelector('.' + prefix + '-list');
    var right = layout.querySelector('.' + prefix + '-right') || layout.querySelector('.' + prefix + '-form');
    if (!left || !right) return;

    var ratio = _loadResizerRatio(storageKey);
    if (ratio !== null) {
        var layoutW = layout.offsetWidth - resizer.offsetWidth - 12;
        left.style.flex = 'none';
        left.style.width = (ratio * layoutW) + 'px';
        right.style.width = ((1 - ratio) * layoutW) + 'px';
    }

    // Remove previous handler to prevent accumulation on re-init
    if (resizer._onMouseDown) {
        resizer.removeEventListener('mousedown', resizer._onMouseDown);
    }

    var startX, startLeftW, startRightW;
    function onMouseDown(e) {
        e.preventDefault();
        startX = e.clientX;
        startLeftW = left.offsetWidth;
        startRightW = right.offsetWidth;
        left.style.flex = 'none';
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        resizer.classList.add('active');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    }
    function onMouseMove(e) {
        var dx = e.clientX - startX;
        var newLeftW = Math.max(minLeft, startLeftW + dx);
        var newRightW = Math.max(minRight, startRightW - dx);
        if (startLeftW + dx < minLeft) { newLeftW = minLeft; newRightW = startLeftW + startRightW - minLeft; }
        if (startRightW - dx < minRight) { newRightW = minRight; newLeftW = startLeftW + startRightW - minRight; }
        left.style.width = newLeftW + 'px';
        right.style.width = newRightW + 'px';
    }
    function onMouseUp() {
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        resizer.classList.remove('active');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        var total = left.offsetWidth + right.offsetWidth;
        if (total > 0) {
            _resizerRatios[storageKey] = left.offsetWidth / total;
            try { localStorage.setItem(storageKey, _resizerRatios[storageKey]); } catch(e) {}
        }
    }
    resizer._onMouseDown = onMouseDown;
    resizer.addEventListener('mousedown', onMouseDown);
}

function initAlertResizer(container) { initResizer(container, 'drawer-alerts', 150, 150, 'alertResizerRatio'); }

// --- Drawer "Show All" toggle state (per tab) ---
var _drawerShowAll = { alerts: false, scenarios: false, position: false, scanner: false };

// --- Position drawer mode: 'futures' or 'spots' ---
var _positionDrawerMode = 'futures';
try { var _savedMode = localStorage.getItem('positionDrawerMode'); if (_savedMode === 'spots') _positionDrawerMode = 'spots'; } catch(e) {}

function _togglePositionDrawerMode(mode) {
    _positionDrawerMode = mode;
    try { localStorage.setItem('positionDrawerMode', mode); } catch(e) {}
    updateDrawerPosition();
}

// Fetch all drawings from server and merge into localStorage (for All toggle)
var _allDrawingsLoaded = false;
function _loadAllDrawingsFromServer(callback) {
    if (_allDrawingsLoaded) { callback(); return; }
    fetch('/api/drawings', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.success || !data.drawings) { callback(); return; }
        try {
            var store = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            data.drawings.forEach(function(d) {
                var meta = {};
                try { meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : (d.metadata || {}); } catch(e) {}
                if (!meta.kline_name || !meta.points || !meta.local_id) return;
                var sym = d.asset;
                if (!sym) return;
                if (!store[sym]) store[sym] = [];
                // Skip if already exists
                var exists = store[sym].some(function(s) { return s.id === meta.local_id; });
                if (exists) return;
                var drawing = {
                    id: meta.local_id,
                    name: meta.kline_name,
                    points: meta.points,
                    color: d.color || '#3fb950',
                    width: d.line_width || 1,
                    extendData: meta.extend_data || {}
                };
                store[sym].push(drawing);
            });
            localStorage.setItem('klineDrawings', JSON.stringify(store));
            _allDrawingsLoaded = true;
        } catch(e) { console.error('Error merging server drawings:', e); }
        callback();
    })
    .catch(function(e) { console.error('Failed to load all drawings:', e); callback(); });
}

function _drawerToggleShowAll(tab) {
    _drawerShowAll[tab] = !_drawerShowAll[tab];
    if (tab === 'alerts') updateDrawerAlerts();
    else if (tab === 'position') {
        if (_drawerShowAll.position) {
            // Fetch all drawings from server first, then render
            _loadAllDrawingsFromServer(function() { updateDrawerPosition(); });
        } else {
            updateDrawerPosition();
        }
    }
}

function _drawerShowAllBtn(tab) {
    var active = _drawerShowAll[tab];
    return '<div class="drawer-toggle' + (active ? ' active' : '') + '" title="' + (active ? 'Show current asset' : 'Show all assets') + '" data-action="drawer-toggle-show-all" data-tab="' + tab + '" style="cursor:pointer">' +
        '<span class="drawer-toggle-track"><span class="drawer-toggle-thumb"></span></span>' +
        '<span class="drawer-toggle-label">All</span>' +
    '</div>';
}

function _drawerAssetGroupHeader(symbol, countLabel) {
    var a = (typeof allAssets !== 'undefined' ? allAssets[symbol] : null) || {};
    var name = a.name || '';
    var countHtml = countLabel ? '<span class="drawer-asset-group-count">' + escapeHtml(countLabel) + '</span>' : '';
    return '<div class="drawer-asset-group">' + escapeHtml(symbol) + (name ? ' <span class="drawer-asset-group-name">' + escapeHtml(name) + '</span>' : '') + countHtml + '</div>';
}


function loadPlaysForChart(asset, tf) {
    // Fetch plays, filtered by asset.
    // T4.2/T4.3: route through dedupFetch — fastSwapChartData can call
    // loadPlaysForChart more than once per swap, so concurrent calls for the
    // same asset within 2 s collapse to one GET /api/plays?asset=...
    var _playsUrl = '/api/plays?asset=' + encodeURIComponent(asset) + '&include_resolved=false';
    var _playsFetch = (typeof dedupFetch === 'function')
        ? dedupFetch(_playsUrl, { credentials: 'same-origin' }, 2000)
        : fetch(_playsUrl, { credentials: 'same-origin' });
    _playsFetch
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var all = data.plays || [];
        scenarioList = all;
        activeScenarioIndex = scenarioList.length > 0 ? 0 : -1;

        // For plays with linked drawings, fetch drawing data for overlay rendering
        var drawingPromises = scenarioList.filter(function(s) {
            var linked = s.linked_drawing_ids || [];
            return linked.length > 0;
        }).map(function(s) {
            return Promise.all((s.linked_drawing_ids || []).map(function(did) {
                return fetch('/api/drawings/' + did, { credentials: 'same-origin' })
                    .then(function(r) { return r.ok ? r.json() : null; })
                    .catch(function() { return null; });
            })).then(function(drawings) {
                s._children = drawings.filter(function(d) { return d && d.drawing; }).map(function(d) { return d.drawing; });
            });
        });

        // The retired Scenarios drawer, its milestone pins and its
        // stage rail were all called from here. They were already no-ops at
        // runtime — the drawer has no scenarios tab (chart-toolbar.js builds
        // position/alerts/scanner/stream/backtest) — and are now deleted.
        // Only the overlay render is live: plays with linked drawings get
        // chart overlays.
        Promise.all(drawingPromises).then(function() {
            if (scenariosVisible) renderPlayOverlays();
        });
    })
    .catch(function(e) {
        if (typeof window.carracLog === 'function') {
            window.carracLog('plays', 'loadPlaysForChart failed — chart shows no plays',
                             String((e && e.message) || e));
        }
    });
}

function renderPlayOverlays() {
    if (!klineChart) return;
    // Remove existing scenario overlays
    scenarioOverlayIds.forEach(function(id) {
        try { klineChart.removeOverlay(id); } catch(e) {}
    });
    scenarioOverlayIds = [];

    if (!scenariosVisible) return;

    scenarioList.forEach(function(s, idx) {
        var dimmed = (activeScenarioIndex >= 0 && idx !== activeScenarioIndex);

        // v2 scenarios: render linked drawings as overlays
        if (s._children && s._children.length > 0) {
            s._children.forEach(function(child) {
                var childMeta = (typeof child.metadata === 'string' ? JSON.parse(child.metadata) : child.metadata) || {};
                var klineName = childMeta.kline_name;
                var pts = childMeta.points || [];
                if (!klineName || pts.length === 0) return;
                try {
                    var overlayOpts = {
                        name: klineName,
                        lock: true,
                        points: snapPointsToData(pts),
                        styles: {
                            line: {
                                color: dimmed ? 'rgba(150,150,150,0.3)' : (childMeta.color || child.color || '#3fb950'),
                                size: dimmed ? 1 : (childMeta.width || child.line_width || 2)
                            },
                            point: {
                                color: dimmed ? 'rgba(150,150,150,0.3)' : (childMeta.color || child.color || '#3fb950')
                            }
                        }
                    };
                    if (childMeta.extend_data) overlayOpts.extendData = childMeta.extend_data;
                    var oid = klineChart.createOverlay(overlayOpts);
                    if (oid) scenarioOverlayIds.push(oid);
                } catch(e) {}
            });
        }

        // Legacy v1 scenarioPath polyline (metadata.points)
        var meta = s.metadata || {};
        if (meta.points && meta.points.length >= 2) {
            try {
                var oid = klineChart.createOverlay({
                    name: 'scenarioPath',
                    lock: true,
                    points: snapPointsToData(meta.points),
                    extendData: {
                        name: s.label || meta.name || '',
                        direction: s.direction || meta.direction || 'neutral',
                        _dimmed: dimmed
                    }
                });
                if (oid) scenarioOverlayIds.push(oid);
            } catch(e) {}
        }
    });
}


// Scenario drawing flow


// ==================== Scenario TP/SL extraction (shared by the journal and alert actions) ====================


// ==================== Log Scenario to Journal ====================


// ==================== Scenario Alert Creation ====================

function createPriceAlert(symbol, price, note) {
    var currentPrice = allAssets[symbol] ? allAssets[symbol].latest_price : 0;
    var condition = (price >= currentPrice) ? 'above' : 'below';
    return fetch('/api/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        credentials: 'same-origin',
        body: JSON.stringify({ symbol: symbol, condition: condition, price: price, note: note, frequency: 'once' })
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.alert || data.id) {
            showToast('Alert created: ' + note);
            if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts();
            if (typeof renderAlertOverlays === 'function') renderAlertOverlays();
        } else if (data.error) { showToast(data.error); }
    }).catch(function() { showToast('Failed to create alert'); });
}

// ==================== Scenario v2: Drawing Group Edit Mode ====================


// ==================== Scenario Path Drawing Tool (inside edit mode) ====================


// ==================== Elliott Wave Drawing Tool (inside edit mode) ====================


// getCSRFToken provided globally by csrf-utils.js (loaded before this script).

// Bottom bar alerts display — now delegates to drawer

// ======== Bottom Drawer Logic ========
function toggleDrawer(tab) {
    var drawer = document.getElementById('chart-drawer');
    if (!drawer) return;
    if (drawerOpen && drawerActiveTab === tab) {
        closeDrawer();
    } else {
        openDrawer(tab);
    }
}

// Scanner/Stream build hundreds of <tr data-symbol> rows; leaving them in the
// (hidden) drawer after the tab is left permanently enlarges the DOM that every
// WS tick handler's querySelectorAll has to search — the post-Scanner/Radar
// whole-UI stutter. Clear them on leave; they fully rebuild on next open.
function _teardownHeavyDrawerSections(exceptTab) {
    ['scanner', 'stream'].forEach(function(tab) {
        if (tab === exceptTab) return;
        var sec = document.getElementById('drawer-section-' + tab);
        if (sec && sec.firstChild) sec.innerHTML = '';
    });
}

function openDrawer(tab) {
    var drawer = document.getElementById('chart-drawer');
    if (!drawer) return;
    drawerOpen = true;
    drawerActiveTab = tab || 'alerts';
    drawer.classList.add('open');
    _teardownHeavyDrawerSections(drawerActiveTab);
    drawer.querySelectorAll('.drawer-tab').forEach(function(t) {
        var isActive = t.getAttribute('data-tab') === drawerActiveTab;
        t.classList.toggle('active', isActive);
        if (t.hasAttribute('role')) {
            t.setAttribute('aria-selected', String(isActive));
            t.setAttribute('tabindex', isActive ? '0' : '-1');
        }
    });
    document.querySelectorAll('.drawer-section').forEach(function(s) { s.style.display = 'none'; });
    var section = document.getElementById('drawer-section-' + drawerActiveTab);
    if (section) section.style.display = '';
    // Enable click-to-set-price when alerts tab is active
    klineAlertClickMode = (drawerActiveTab === 'alerts');
    if (drawerActiveTab === 'alerts') {
        var priceInput = document.getElementById('alert-price-input');
        if (priceInput) setTimeout(function() { priceInput.focus(); }, 50);
    }
    if (drawerActiveTab === 'scanner') {
        renderScannerTab();
    }
    if (drawerActiveTab === 'stream') {
        renderStreamTab();
    }
    if (drawerActiveTab === 'position') {
        updateDrawerPosition();
    }
    if (drawerActiveTab === 'alerts') {
        updateDrawerAlerts();
    }
    if (drawerActiveTab === 'backtest') {
        if (typeof BacktestUI !== 'undefined') BacktestUI.render();
    }
    if (drawerActiveTab === 'objects') {
        if (typeof renderDrawingsObjectsPanel === 'function') renderDrawingsObjectsPanel();
    }
    if (klineChart && klineChart.resize) {
        setTimeout(function() { if (klineChart && klineChart.resize) klineChart.resize(); }, 220);
    }
}

function closeDrawer() {
    var drawer = document.getElementById('chart-drawer');
    if (!drawer) return;
    drawerOpen = false;
    drawerFullscreen = false;
    drawer.classList.remove('open', 'fullscreen');
    _teardownHeavyDrawerSections();
    drawer.querySelectorAll('.drawer-tab').forEach(function(t) {
        t.classList.remove('active');
        if (t.hasAttribute('role')) {
            t.setAttribute('aria-selected', 'false');
        }
    });
    klineAlertClickMode = false;
    // Reset fullscreen icon
    var fsBtn = document.getElementById('drawer-fullscreen');
    if (fsBtn) {
        fsBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12"><polygon points="6,1.5 10.5,6 6,10.5 1.5,6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="miter"/></svg>';
        fsBtn.title = 'Fullscreen';
    }
    if (klineChart && klineChart.resize) {
        setTimeout(function() { if (klineChart && klineChart.resize) klineChart.resize(); }, 220);
    }
}

var drawerFullscreen = false;
function toggleDrawerFullscreen() {
    var drawer = document.getElementById('chart-drawer');
    if (!drawer) return;
    drawerFullscreen = !drawerFullscreen;
    drawer.classList.toggle('fullscreen', drawerFullscreen);
    // Ensure drawer is open
    if (drawerFullscreen && !drawerOpen) {
        openDrawer(drawerActiveTab || 'alerts');
    }
    // Update icon — single diamond for "fullscreen", two overlapping diamonds for "restore"
    var fsBtn = document.getElementById('drawer-fullscreen');
    if (fsBtn) {
        fsBtn.innerHTML = drawerFullscreen
            ? '<svg width="12" height="12" viewBox="0 0 12 12"><polygon points="7.5,0.5 11.5,4.5 7.5,8.5 3.5,4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="miter"/><polygon points="4.5,3.5 8.5,7.5 4.5,11.5 0.5,7.5" fill="#161b22" stroke="currentColor" stroke-width="1.4" stroke-linejoin="miter"/></svg>'
            : '<svg width="12" height="12" viewBox="0 0 12 12"><polygon points="6,1.5 10.5,6 6,10.5 1.5,6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="miter"/></svg>';
        fsBtn.title = drawerFullscreen ? 'Exit fullscreen' : 'Fullscreen';
    }
    if (klineChart && klineChart.resize) {
        setTimeout(function() { if (klineChart && klineChart.resize) klineChart.resize(); }, 220);
    }
}

// ======== Scanner Drawer Tab ========
var _scannerData = null;
var _scannerVerdictFilter = '';
var _scannerDirFilter = 'both';
var _scannerSetupFilter = '';
var _scannerReadinessFilter = '';
var _scannerSearchFilter = '';
var _scannerTypeFilter = '';
var _scannerExpandedSymbols = {};  // symbol → true when expanded
// detail rows are built lazily (on first expand) instead of for
// the whole ~250+ asset universe on every render. Cache key is "symbol::dir" (dir
// affects which side's verdict/plan/criteria is shown); the cache survives filter/
// search/sort re-renders (same _scannerData reference) and is dropped whenever a new
// /api/scanner fetch replaces that reference.
var _scannerDetailCache = {};
var _scannerDetailCacheDataRef = null;
var _scannerSortCol = 'score';  // score, rr, sl, tp, symbol, setup, price
var _scannerSortAsc = false;    // default descending for score/rr
var _scannerMinScore = '';
var _scannerMinRR = '';
var _scannerRefreshTimer = null;

// verdict display map (internal values stay; only presentation changes)
function _scannerVerdictLabel(v) {
    var map = { STRONG: 'High', GOOD: 'Moderate', NEUTRAL: 'Mixed', WEAK: 'Low', AVOID: 'Conflicting' };
    return map[v] || v || '';
}

// one-time acknowledgement modal
function _showScannerAckIfNeeded() {
    if (localStorage.getItem('carrac_scanner_ack')) return;
    var overlay = document.createElement('div');
    overlay.id = 'scanner-ack-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:26010;display:flex;align-items:center;justify-content:center';
    overlay.innerHTML =
        '<div style="background:#1e222d;border:1px solid #2a2e39;border-radius:8px;padding:20px 24px;max-width:340px;color:#d1d4dc;font-size:13px;line-height:1.55">' +
        '<div style="font-weight:600;margin-bottom:10px;color:#e0e3eb">Scanner results are ideas for your own analysis</div>' +
        '<div style="color:#787b86;margin-bottom:16px">The scanner surfaces observable market data — confluence zones, structure, momentum. It does not give financial advice. Always do your own research before making any decisions.</div>' +
        '<button id="scanner-ack-got-it" style="background:#2962ff;color:#fff;border:none;border-radius:4px;padding:7px 18px;cursor:pointer;font-size:13px;font-weight:500">Got it</button>' +
        '</div>';
    document.body.appendChild(overlay);
    var ackBtn = document.getElementById('scanner-ack-got-it');
    if (ackBtn) ackBtn.onclick = function() {
        localStorage.setItem('carrac_scanner_ack', '1');
        document.getElementById('scanner-ack-overlay').remove();
    };
}

// CSP hardening (nonce-based script-src, no 'unsafe-inline') — every onclick=/
// onchange=/oninput= action in the scanner tab's HTML is
// routed through this single delegated listener, bound EXACTLY ONCE on the section
// (guarded via a dataset flag) so it survives every full re-render of the tab
// (_renderScannerContent reassigns section.innerHTML wholesale — see its comment —
// as well as the lazily-inserted detail rows from _scannerEnsureDetailRow).
function _wireScannerDelegatedEvents(section) {
    if (section.dataset.scannerActionsWired) return;
    section.dataset.scannerActionsWired = '1';

    section.addEventListener('click', function(e) {
        var el = e.target.closest('[data-action]');
        if (!el || !section.contains(el)) return;
        var action = el.getAttribute('data-action');
        var sec = document.getElementById('drawer-section-scanner');
        switch (action) {
            case 'scanner-home':
                renderScannerTab();
                break;
            case 'scanner-clear-search':
                _scannerSearchFilter = '';
                _renderScannerContent(sec, _scannerData);
                break;
            case 'scanner-open-backtest':
                openScannerBacktestModal();
                break;
            case 'scanner-clear-one': {
                var k = el.getAttribute('data-filter-key');
                if (k === 'search') _scannerSearchFilter = '';
                else if (k === 'verdict') _scannerVerdictFilter = '';
                else if (k === 'setup') _scannerSetupFilter = '';
                else if (k === 'readiness') _scannerReadinessFilter = '';
                else if (k === 'dir') _scannerDirFilter = 'both';
                else if (k === 'type') _scannerTypeFilter = '';
                else if (k === 'minScore') _scannerMinScore = '';
                else if (k === 'minRR') _scannerMinRR = '';
                _renderScannerContent(sec, _scannerData);
                break;
            }
            case 'scanner-clear-all':
                _scannerClearAllFilters();
                _renderScannerContent(sec, _scannerData);
                break;
            case 'scanner-set-dir':
                _scannerDirFilter = el.getAttribute('data-val');
                _renderScannerContent(sec, _scannerData);
                break;
            case 'scanner-set-readiness':
                _scannerReadinessFilter = el.getAttribute('data-val');
                _renderScannerContent(sec, _scannerData);
                break;
            case 'scanner-sort': {
                var col = el.getAttribute('data-col');
                if (_scannerSortCol === col) { _scannerSortAsc = !_scannerSortAsc; }
                else { _scannerSortCol = col; _scannerSortAsc = (col === 'symbol' || col === 'setup'); }
                _renderScannerContent(sec, _scannerData);
                break;
            }
            case 'scanner-show-stats':
                _showScannerStats();
                break;
            case 'scanner-copy-agent':
                e.stopPropagation();
                _scannerCopyForAgent(el);
                break;
            case 'scanner-draw-position':
                _drawScannerPosition(el, el.getAttribute('data-log') === '1');
                break;
            case 'scanner-draw-confluence':
                _drawConfluenceZones(el);
                break;
        }
    });

    section.addEventListener('input', function(e) {
        var id = e.target.id;
        if (id === 'scanner-search-input') { _scannerSearchFilter = e.target.value; _scannerScheduleRender(); }
        else if (id === 'scanner-min-score') { _scannerMinScore = e.target.value; _scannerScheduleRender(); }
        else if (id === 'scanner-min-rr') { _scannerMinRR = e.target.value; _scannerScheduleRender(); }
    });

    section.addEventListener('change', function(e) {
        var id = e.target.id;
        var sec = document.getElementById('drawer-section-scanner');
        if (id === 'scanner-verdict-filter') { _scannerVerdictFilter = e.target.value; _renderScannerContent(sec, _scannerData); }
        else if (id === 'scanner-setup-filter') { _scannerSetupFilter = e.target.value; _renderScannerContent(sec, _scannerData); }
        else if (id === 'scanner-type-filter') { _scannerTypeFilter = e.target.value; _renderScannerContent(sec, _scannerData); }
    });
}

// Reset every scanner filter to its default — was duplicated inline across the
// "Clear all" active-filters button and the empty-state "Clear filters" button.
function _scannerClearAllFilters() {
    _scannerSearchFilter = '';
    _scannerVerdictFilter = '';
    _scannerSetupFilter = '';
    _scannerReadinessFilter = '';
    _scannerDirFilter = 'both';
    _scannerTypeFilter = '';
    _scannerMinScore = '';
    _scannerMinRR = '';
}

function renderScannerTab() {
    var section = document.getElementById('drawer-section-scanner');
    if (!section) return;
    _wireScannerDelegatedEvents(section);

    // one-time disclaimer acknowledgement
    _showScannerAckIfNeeded();

    // Show loading on first render (polished state)
    if (!_scannerData) {
        section.innerHTML = '<div class="scanner-state"><div class="scanner-state-spinner"></div><p class="scanner-state-p">Scanning the market…</p></div>';
    } else if (!section.firstChild) {
        // Section was torn down on leave (perf) — repaint cached data instantly
        // instead of showing an empty pane until the refetch lands.
        _renderScannerContent(section, _scannerData);
    }

    fetch('/api/scanner')
        .then(function(r) {
            if (!r.ok) throw new Error('status ' + r.status);
            return r.json();
        })
        .then(function(data) {
            _scannerData = data;
            _renderScannerContent(section, data);
        })
        .catch(function() {
            section.innerHTML = '<div class="scanner-state">' +
                '<div class="scanner-state-ico scanner-state-ico-warn"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg></div>' +
                '<h3 class="scanner-state-h">Scanner data unavailable</h3>' +
                '<p class="scanner-state-p">The feed is briefly offline or the scoring engine is warming up. It refreshes automatically every few minutes.</p>' +
                '<button class="scanner-state-btn" data-action="scanner-home">Retry now</button>' +
            '</div>';
        });

    // Auto-refresh every 60s while visible
    clearInterval(_scannerRefreshTimer);
    _scannerRefreshTimer = setInterval(function() {
        if (drawerOpen && drawerActiveTab === 'scanner') {
            // skip refresh if user is mid-interaction (row expanded or search focused)
            var anyExpanded = Object.keys(_scannerExpandedSymbols).some(function(k) { return _scannerExpandedSymbols[k]; });
            var searchFocused = document.activeElement && document.activeElement.id === 'scanner-search-input';
            if (anyExpanded || searchFocused) return;

            fetch('/api/scanner')
                .then(function(r) {
                    if (!r.ok) throw new Error('status ' + r.status);
                    return r.json();
                })
                .then(function(data) {
                    _scannerData = data;
                    if (data.computed_at) _updateDrawerDataAge(data.computed_at);
                    var s = document.getElementById('drawer-section-scanner');
                    if (s) _renderScannerContent(s, data);
                })
                .catch(function() { /* silent — stale data stays visible */ });
        } else {
            clearInterval(_scannerRefreshTimer);
        }
    }, 60000);
}

// Debounced re-render for the scanner filter inputs — rebuilding the full
// table (1 summary <tr> per asset for the whole universe — detail rows are
// built lazily on expand, see _scannerBuildDetailRowHtml) on every keystroke
// is the scanner-specific typing lag; 200ms of quiet costs nothing perceptible.
function _scannerScheduleRender() {
    if (window._scannerRenderTimer) clearTimeout(window._scannerRenderTimer);
    window._scannerRenderTimer = setTimeout(function() {
        window._scannerRenderTimer = null;
        var sec = document.getElementById('drawer-section-scanner');
        if (sec && _scannerData) _renderScannerContent(sec, _scannerData);
    }, 200);
}

function _renderScannerContent(section, data) {
    var results = data.results || [];
    var macro = data.macro || {};  // used for Copy-for-my-agent prompt

    // A new fetch always hands us a brand-new parsed-JSON `data` object (see
    // renderScannerTab / its 60s auto-refresh), so comparing references is enough to
    // know the lazy detail-row cache below is now stale.
    if (_scannerDetailCacheDataRef !== data) {
        _scannerDetailCache = {};
        _scannerDetailCacheDataRef = data;
    }

    // preserve scroll + expanded state before innerHTML replace
    var _savedScrollTop = section.scrollTop || 0;
    var html = '';

    // Filter bar
    html += '<div class="scanner-header">';
    html += '<div class="scanner-filters">';
    html += '<div class="scanner-search-inline"><input type="text" class="scanner-search-input" id="scanner-search-input" placeholder="Search..." value="' + (_scannerSearchFilter || '') + '">';
    if (_scannerSearchFilter) html += '<button class="scanner-search-clear" data-action="scanner-clear-search">&times;</button>';
    html += '</div>';
    html += '<select class="scanner-select" id="scanner-verdict-filter">';
    html += '<option value=""' + (_scannerVerdictFilter === '' ? ' selected' : '') + '>All verdicts</option>';
    html += '<option value="STRONG"' + (_scannerVerdictFilter === 'STRONG' ? ' selected' : '') + '>High only</option>';
    html += '<option value="STRONG,GOOD"' + (_scannerVerdictFilter === 'STRONG,GOOD' ? ' selected' : '') + '>High + Moderate</option>';
    html += '<option value="STRONG,GOOD,NEUTRAL"' + (_scannerVerdictFilter === 'STRONG,GOOD,NEUTRAL' ? ' selected' : '') + '>Mixed+</option>';
    html += '</select>';
    html += '<select class="scanner-select" id="scanner-setup-filter">';
    html += '<option value=""' + (_scannerSetupFilter === '' ? ' selected' : '') + '>All setups</option>';
    ['trend_continuation', 'pullback', 'breakout', 'reversal', 'mean_reversion', 'range'].forEach(function(t) {
        var label = {trend_continuation:'Trend',pullback:'Pullback',breakout:'Breakout',reversal:'Reversal',mean_reversion:'Mean Rev',range:'Range'}[t] || t;
        html += '<option value="' + t + '"' + (_scannerSetupFilter === t ? ' selected' : '') + '>' + label + '</option>';
    });
    html += '</select>';
    // Scanner v4 — segmented Direction control (was a <select>)
    html += '<div class="scanner-seg" role="group" aria-label="Direction">';
    [['both', 'Both', ''], ['long', 'Long', 'scanner-seg-long'], ['short', 'Short', 'scanner-seg-short']].forEach(function(d) {
        var on = _scannerDirFilter === d[0] ? ' on' : '';
        html += '<button type="button" class="scanner-seg-btn ' + d[2] + on + '" data-action="scanner-set-dir" data-val="' + d[0] + '">' + d[1] + '</button>';
    });
    html += '</div>';
    // Scanner v4 — segmented Readiness control (was a <select>)
    html += '<div class="scanner-seg" role="group" aria-label="Readiness">';
    [['', 'All'], ['ready', 'Ready'], ['watch', 'Watch']].forEach(function(d) {
        var on = _scannerReadinessFilter === d[0] ? ' on' : '';
        html += '<button type="button" class="scanner-seg-btn' + on + '" data-action="scanner-set-readiness" data-val="' + d[0] + '">' + d[1] + '</button>';
    });
    html += '</div>';
    html += '<select class="scanner-select" id="scanner-type-filter">';
    html += '<option value=""' + (_scannerTypeFilter === '' ? ' selected' : '') + '>All types</option>';
    html += '<option value="crypto"' + (_scannerTypeFilter === 'crypto' ? ' selected' : '') + '>Crypto</option>';
    html += '<option value="tradfi"' + (_scannerTypeFilter === 'tradfi' ? ' selected' : '') + '>TradFi</option>';
    html += '<option value="stock"' + (_scannerTypeFilter === 'stock' ? ' selected' : '') + '>Stocks</option>';
    html += '<option value="etf"' + (_scannerTypeFilter === 'etf' ? ' selected' : '') + '>ETFs</option>';
    html += '<option value="forex"' + (_scannerTypeFilter === 'forex' ? ' selected' : '') + '>Forex</option>';
    html += '<option value="commodity"' + (_scannerTypeFilter === 'commodity' ? ' selected' : '') + '>Commodities</option>';
    html += '<option value="index"' + (_scannerTypeFilter === 'index' ? ' selected' : '') + '>Indices</option>';
    html += '</select>';
    html += '<input type="number" class="scanner-min-input" id="scanner-min-score" placeholder="Min score" step="1" value="' + (_scannerMinScore || '') + '">';
    html += '<input type="number" class="scanner-min-input" id="scanner-min-rr" placeholder="Min R:R" step="0.1" value="' + (_scannerMinRR || '') + '">';
    // /api/scanner/backtest enforces limits['backtester'], which is
    // True for member — so gate on _isPremium (member+), not _hasPremiumFeatures.
    if (window._isPremium) {
        html += '<button class="scanner-bt-btn" data-action="scanner-open-backtest" title="Backtest a formula against every asset matching your current verdict/direction filter">Test formula</button>';
    }
    if (data.computed_at) {
        _updateDrawerDataAge(data.computed_at);
    }
    html += '</div>';
    html += '</div>';

    // free-tier banner
    if (data.upgrade_to_see_all && data.total_available && data.returned != null && data.returned < data.total_available) {
        html += '<div class="scanner-free-banner">Showing ' + data.returned + ' of ' + data.total_available + ' assets — <span class="scanner-upgrade-link">'+'This needs a higher plan. An admin can change plans under Admin → Users.'+'</span></div>';
    }

    // Scanner v4 — active-filters strip + clear-all (shows only non-default filters)
    // Each tuple is [chip label, data-filter-key] — the key is dispatched to the
    // matching reset in the "scanner-clear-one" case of _wireScannerDelegatedEvents.
    var _af = [];
    if (_scannerSearchFilter) _af.push(['Search: ' + escapeHtml(_scannerSearchFilter), 'search']);
    if (_scannerVerdictFilter) { var _vm = {'STRONG': 'High only', 'STRONG,GOOD': 'High + Moderate', 'STRONG,GOOD,NEUTRAL': 'Mixed+'}; _af.push([_vm[_scannerVerdictFilter] || _scannerVerdictFilter, 'verdict']); }
    if (_scannerSetupFilter) { var _sm = {trend_continuation: 'Trend', pullback: 'Pullback', breakout: 'Breakout', reversal: 'Reversal', mean_reversion: 'Mean Rev', range: 'Range'}; _af.push([_sm[_scannerSetupFilter] || _scannerSetupFilter, 'setup']); }
    if (_scannerReadinessFilter) _af.push([_scannerReadinessFilter === 'ready' ? 'Ready' : 'Watch', 'readiness']);
    if (_scannerDirFilter && _scannerDirFilter !== 'both') _af.push([_scannerDirFilter.charAt(0).toUpperCase() + _scannerDirFilter.slice(1), 'dir']);
    if (_scannerTypeFilter) { var _tm = {crypto: 'Crypto', tradfi: 'TradFi', stock: 'Stocks', etf: 'ETFs', forex: 'Forex', commodity: 'Commodities', index: 'Indices'}; _af.push([_tm[_scannerTypeFilter] || _scannerTypeFilter, 'type']); }
    if (_scannerMinScore !== '') _af.push(['Score ≥ ' + escapeHtml(String(_scannerMinScore)), 'minScore']);
    if (_scannerMinRR !== '') _af.push(['R:R ≥ ' + escapeHtml(String(_scannerMinRR)), 'minRR']);
    if (_af.length) {
        html += '<div class="scanner-active-filters"><span class="scanner-af-label">Filters</span>';
        _af.forEach(function(f) {
            html += '<span class="scanner-af-chip">' + f[0] + '<button type="button" class="scanner-af-x" aria-label="Remove filter" data-action="scanner-clear-one" data-filter-key="' + f[1] + '">&times;</button></span>';
        });
        html += '<button type="button" class="scanner-af-clear" data-action="scanner-clear-all">Clear all</button>';
        html += '</div>';
    }

    // Apply filters
    var filtered = results;
    if (_scannerSearchFilter) {
        var q = _scannerSearchFilter.toUpperCase();
        filtered = filtered.filter(function(r) { return r.symbol && r.symbol.toUpperCase().indexOf(q) >= 0; });
    }
    if (_scannerTypeFilter) {
        if (_scannerTypeFilter === 'tradfi') {
            filtered = filtered.filter(function(r) { return r.type && r.type !== 'crypto'; });
        } else {
            filtered = filtered.filter(function(r) { return r.type === _scannerTypeFilter; });
        }
    }
    if (_scannerVerdictFilter) {
        var allowed = _scannerVerdictFilter.split(',');
        if (_scannerDirFilter === 'both') {
            filtered = filtered.filter(function(r) { return allowed.indexOf(r.best_verdict) >= 0; });
        } else {
            filtered = filtered.filter(function(r) { return allowed.indexOf((r[_scannerDirFilter] || {}).verdict) >= 0; });
        }
    }
    if (_scannerSetupFilter) {
        filtered = filtered.filter(function(r) {
            var dir = _scannerDirFilter === 'both' ? r.best : _scannerDirFilter;
            var s = ((r[dir] || {}).setup || {}).type;
            return s === _scannerSetupFilter;
        });
    }
    if (_scannerReadinessFilter) {
        filtered = filtered.filter(function(r) {
            var dir = _scannerDirFilter === 'both' ? r.best : _scannerDirFilter;
            var rd = ((r[dir] || {}).setup || {}).readiness;
            return rd === _scannerReadinessFilter;
        });
    }
    if (_scannerMinScore !== '') {
        var minS = parseFloat(_scannerMinScore);
        if (!isNaN(minS)) {
            filtered = filtered.filter(function(r) {
                var dir = _scannerDirFilter === 'both' ? r.best : _scannerDirFilter;
                return ((r[dir] || {}).total || 0) >= minS;
            });
        }
    }
    if (_scannerMinRR !== '') {
        var minRR = parseFloat(_scannerMinRR);
        if (!isNaN(minRR)) {
            filtered = filtered.filter(function(r) {
                var dir = _scannerDirFilter === 'both' ? r.best : _scannerDirFilter;
                var tp = (r[dir] || {}).trade_plan || {};
                return (tp.rr1 || 0) >= minRR;
            });
        }
    }
    // Sort helper — extract sort value for each asset
    function _scannerSortVal(asset, col) {
        var dir = _scannerDirFilter === 'both' ? asset.best : _scannerDirFilter;
        var best = asset[dir] || {};
        var tp = best.trade_plan || {};
        var setup = best.setup || {};
        switch(col) {
            case 'score': return best.total || 0;
            case 'rr': return tp.rr1 || 0;
            case 'sl': return tp.sl_pct || 0;
            case 'tp': return tp.tp1_pct || 0;
            case 'symbol': return asset.symbol || '';
            case 'setup': return setup.type || '';
            case 'price': return asset.price || 0;
            default: return 0;
        }
    }

    // Apply sort
    filtered = filtered.slice().sort(function(a, b) {
        var va = _scannerSortVal(a, _scannerSortCol);
        var vb = _scannerSortVal(b, _scannerSortCol);
        var cmp;
        if (typeof va === 'string') { cmp = va.localeCompare(vb); }
        else { cmp = va - vb; }
        return _scannerSortAsc ? cmp : -cmp;
    });

    // Sort click handler name — logic reproduced in the "scanner-sort" case of
    // _wireScannerDelegatedEvents (asc defaults to true for symbol/setup, false otherwise).
    var _sortClick = function(col) {
        return 'data-action="scanner-sort" data-col="' + col + '"';
    };
    var _sortIcon = function(col) {
        if (_scannerSortCol !== col) return '';
        return _scannerSortAsc ? ' &#9652;' : ' &#9662;';
    };

    // Table header
    html += '<div class="scanner-results">';
    html += '<table class="scanner-table"><thead><tr>';
    html += '<th class="scanner-th scanner-th-score" ' + _sortClick('score') + '>Score' + _sortIcon('score') + '</th>';
    html += '<th class="scanner-th scanner-th-dir">Dir</th>';
    html += '<th class="scanner-th scanner-th-symbol" ' + _sortClick('symbol') + '>Asset' + _sortIcon('symbol') + '</th>';
    html += '<th class="scanner-th scanner-th-setup" ' + _sortClick('setup') + '>Setup' + _sortIcon('setup') + '</th>';
    html += '<th class="scanner-th scanner-th-rr" ' + _sortClick('rr') + '>R:R' + _sortIcon('rr') + '</th>';
    html += '<th class="scanner-th scanner-th-entry">Entry</th>';
    html += '<th class="scanner-th scanner-th-price" ' + _sortClick('price') + '>Price' + _sortIcon('price') + '</th>';
    html += '<th class="scanner-th scanner-th-tp" ' + _sortClick('tp') + '>TP%' + _sortIcon('tp') + '</th>';
    html += '<th class="scanner-th scanner-th-sl" ' + _sortClick('sl') + '>SL%' + _sortIcon('sl') + '</th>';
    html += '<th class="scanner-th scanner-th-actions" aria-hidden="true"></th>';
    html += '</tr></thead><tbody>';

    if (filtered.length === 0) {
        // Polished empty state; offer Clear filters only when something is filtered
        var _emptyBtn = _af.length ? '<button class="scanner-state-btn" data-action="scanner-clear-all">Clear filters</button>' : '';
        html += '<tr><td colspan="10"><div class="scanner-state">' +
            '<div class="scanner-state-ico"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></div>' +
            '<h3 class="scanner-state-h">No assets match your filters</h3>' +
            '<p class="scanner-state-p">Nothing in the universe meets the current criteria. Loosen a filter to widen the scan.</p>' +
            _emptyBtn +
        '</div></td></tr>';
    }
    var _rowAssetIndex = {};  // symbol -> asset; lets the lazy detail-row builder below
                              // reuse the same per-asset data already in scope here,
                              // without refetching or rescanning `filtered`.
    filtered.forEach(function(asset) {
        _rowAssetIndex[asset.symbol] = asset;
        var f = _scannerComputeFields(asset, macro);
        var dir = f.dir, best = f.best, vc = f.vc, setup = f.setup, setupType = f.setupType,
            setupLabel = f.setupLabel, setupReady = f.setupReady, criteria = f.criteria, tp = f.tp,
            critTooltip = f.critTooltip, biasArrow = f.biasArrow, biasWord = f.biasWord,
            verdictDisplay = f.verdictDisplay, _copyData = f.copyData;

        // === Scanner row (collapsed) — real table row ===
        html += '<tr class="scanner-trow" data-symbol="' + asset.symbol + '" data-sym-key="' + asset.symbol + '">';
        html += '<td class="scanner-td-score"><span class="scanner-score ' + vc + '" title="' + critTooltip.trim() + '">' + (best.total || 0) + '</span></td>';
        // bias as observation
        html += '<td class="scanner-td-dir"><span class="scanner-dir scanner-dir-' + dir + '">' + biasArrow + ' ' + biasWord + '</span></td>';
        html += '<td class="scanner-td-symbol"><span class="scanner-symbol" data-sym="' + asset.symbol + '">' + asset.symbol + '</span></td>';
        html += '<td class="scanner-td-setup"><span class="scanner-setup-cell">';
        if (setupLabel) {
            html += '<span class="scanner-setup scanner-setup-' + setupType + '">' + setupLabel + '</span>';
            html += '<span class="scanner-readiness-pill scanner-ready-' + setupReady + '">' + (setupReady === 'ready' ? 'RDY' : 'WCH') + '</span>';
        }
        html += '</span></td>';
        html += '<td class="scanner-td-rr"><span class="' + (tp && tp.rr1 >= 2.5 ? 'scanner-rr-good' : tp && tp.rr1 >= 2.0 ? 'scanner-rr-ok' : '') + '">' + (tp && tp.rr1 != null ? tp.rr1.toFixed(1) : '-') + '</span></td>';
        html += '<td class="scanner-td-entry">' + (tp ? _fmtScannerPrice(tp.entry) : '-') + '</td>';
        html += '<td class="scanner-td-price">' + _fmtScannerPrice(asset.price) + '</td>';
        html += '<td class="scanner-td-tp">' + (tp && tp.tp1_pct != null ? tp.tp1_pct.toFixed(1) + '%' : '-') + '</td>';
        html += '<td class="scanner-td-sl">' + (tp && tp.sl_pct != null ? tp.sl_pct.toFixed(1) + '%' : '-') + '</td>';
        html += '<td class="scanner-td-actions">';
        // row-level Copy button
        html += '<span class="scanner-row-copy" title="Copy for my agent" data-action="scanner-copy-agent" data-cp="' + escapeHtml(_copyData) + '"><svg width="11" height="11" viewBox="0 0 14 14" style="vertical-align:-1px"><rect x="2.5" y="4" width="7.5" height="8" rx="1.3" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5 4 V2.2 H11.5 V10" fill="none" stroke="currentColor" stroke-width="1.3"/></svg></span>';
        html += '<span class="scanner-row-chevron" aria-hidden="true"><svg width="10" height="10" viewBox="0 0 12 12"><polyline points="2,4.5 6,8.5 10,4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square" stroke-linejoin="miter"/></svg></span>';
        html += '</td>';
        html += '</tr>';
        // Detail row is built lazily, not here.
        // See _scannerBuildDetailRowHtml / _scannerEnsureDetailRow below, invoked on
        // first expand (click or re-open-after-render) via _rowAssetIndex.
    });
    html += '</tbody></table></div>'; // close tbody, table, scanner-results

    // Stats & Calibration footer
    html += '<div class="scanner-footer">';
    html += '<button class="scanner-stats-btn" data-action="scanner-show-stats">Stats</button>';
    html += '</div>';

    // capture focused input before DOM replacement
    var _focusedId = document.activeElement && document.activeElement.id;
    section.innerHTML = html;

    // restore scroll position
    section.scrollTop = _savedScrollTop;

    // Restore input focus + cursor position after re-render
    var _restoreIds = ['scanner-search-input', 'scanner-min-score', 'scanner-min-rr'];
    if (_focusedId && _restoreIds.indexOf(_focusedId) >= 0) {
        var _el = document.getElementById(_focusedId);
        if (_el) { _el.focus(); _el.setSelectionRange(_el.value.length, _el.value.length); }
    }

    // Click row → toggle expand. Symbol click → navigate to chart.
    // track expanded state in _scannerExpandedSymbols; re-apply .open after re-render
    // detail rows are built (or reused from cache) lazily via
    // _scannerEnsureDetailRow, only for rows that are actually expanded.
    section.querySelectorAll('.scanner-trow').forEach(function(row) {
        var sym = row.getAttribute('data-sym-key');

        // Re-open rows that were expanded before the render (detail row shows via CSS sibling)
        if (sym && _scannerExpandedSymbols[sym]) {
            row.classList.add('open');
            _scannerEnsureDetailRow(row, _rowAssetIndex[sym], macro);
        }

        row.addEventListener('click', function(e) {
            if (e.target.classList.contains('scanner-symbol')) {
                var s = e.target.getAttribute('data-sym');
                if (s && typeof selectAssetFromSearch === 'function') selectAssetFromSearch(s);
                return;
            }
            // Skip if the click landed on a copy button inside the row
            if (e.target.classList.contains('scanner-row-copy')) return;
            row.classList.toggle('open');
            var isOpen = row.classList.contains('open');
            if (sym) _scannerExpandedSymbols[sym] = isOpen;
            if (isOpen) _scannerEnsureDetailRow(row, _rowAssetIndex[sym], macro);
        });
    });

    // Update scanner tab badge with High (STRONG) count
    var strongCount = 0;
    results.forEach(function(r) {
        if ((r.long || {}).verdict === 'STRONG') strongCount++;
        if ((r.short || {}).verdict === 'STRONG') strongCount++;
    });
    var tabLabel = document.getElementById('drawer-scanner-summary');
    if (tabLabel) tabLabel.textContent = strongCount > 0 ? strongCount + ' High' : 'Scanner';
}

// Derive every per-asset display field the scanner table needs — which side (long/
// short) is "best" under the current direction filter, verdict styling, setup label,
// and the Copy-for-my-agent payload. Shared by the eager summary-row builder above
// and the lazy detail-row builder below so both always agree on what a row currently
// means (extracted so detail rows can be (re)computed later,
// independent of the render pass that built the summary row).
function _scannerComputeFields(asset, macro) {
    var dir = _scannerDirFilter === 'both' ? asset.best : _scannerDirFilter;
    var best = asset[dir] || {};
    var vc = 'scanner-v-' + (best.verdict || 'avoid').toLowerCase();
    var setup = best.setup || {};
    var setupType = setup.type || '';
    var setupLabel = {trend_continuation:'Trend',pullback:'Pullback',breakout:'Breakout',reversal:'Reversal',mean_reversion:'Mean Rev',range:'Range'}[setupType] || '';
    var setupReady = setup.readiness || '';
    var criteria = best.criteria || {};
    var tp = best.trade_plan;

    // Compact criteria scores for tooltip
    var critTooltip = '';
    ['trend','momentum','rsi','structure','level','macro'].forEach(function(name) {
        var c = criteria[name] || {};
        var s = c.score != null ? c.score : '-';
        var abbr = {trend:'T',momentum:'M',rsi:'R',structure:'S',level:'L',macro:'X'}[name];
        critTooltip += abbr + s + ' ';
    });

    // bias as observation (▲ bullish / ▼ bearish), verdict as display label
    var biasArrow = dir === 'long' ? '&#9650;' : '&#9660;';
    var biasWord = dir === 'long' ? 'bullish' : 'bearish';
    var verdictDisplay = _scannerVerdictLabel(best.verdict);

    // build Copy-for-my-agent data attr (safe JSON, escaped for attribute)
    var copyData = JSON.stringify({
        sym: asset.symbol, dir: dir, price: asset.price,
        verdict: best.verdict, score: best.total,
        setup: setupType, readiness: setupReady,
        invalidation: setup.invalidation || null,
        tp: tp ? {entry: tp.entry, sl: tp.sl, tp1: tp.tp1, tp2: tp.tp2, rr1: tp.rr1, sl_pct: tp.sl_pct, tp1_pct: tp.tp1_pct, sl_name: tp.sl_name, tp1_name: tp.tp1_name} : null,
        criteria: criteria,
        conf: best.confluences || null,
        macro: macro
    }).replace(/'/g, '&#39;').replace(/"/g, '&quot;');

    return {
        dir: dir, best: best, vc: vc, setup: setup, setupType: setupType, setupLabel: setupLabel,
        setupReady: setupReady, criteria: criteria, tp: tp, critTooltip: critTooltip,
        biasArrow: biasArrow, biasWord: biasWord, verdictDisplay: verdictDisplay, copyData: copyData
    };
}

// Build a scanner detail row's HTML (trade-plan card, level ladder, criteria grid,
// confluence zone chips, opposite-direction note) — the expensive per-asset work, so it is
// not done for the whole ~250+ asset universe on every render. Invoked lazily, on
// first expand only (see _scannerEnsureDetailRow), then cached.
// Reduce a server-supplied value to a safe CSS class-name fragment.
function _scannerClsToken(s) {
    return String(s == null ? '' : s).toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

function _scannerBuildDetailRowHtml(asset, macro) {
    var f = _scannerComputeFields(asset, macro);
    var dir = f.dir, best = f.best, vc = f.vc, setup = f.setup, setupType = f.setupType,
        criteria = f.criteria, tp = f.tp, biasArrow = f.biasArrow, biasWord = f.biasWord,
        verdictDisplay = f.verdictDisplay, _copyData = f.copyData;
    var _dirCls = _scannerClsToken(dir), _vcCls = _scannerClsToken(vc);

    var html = '';
    // === Expandable detail — second table row, full-width cell ===
    html += '<tr class="scanner-detail-trow"><td colspan="10"><div class="scanner-detail-body">';
    html += '<div class="scanner-detail-columns">';

    // Left: Trade plan card
    html += '<div class="scanner-detail-col scanner-col-card">';
    // Full trade plan — position card style with live price
    if (tp) {
        var _isLong = dir === 'long';
        var _dirClass = _isLong ? 'scanner-plan-long' : 'scanner-plan-short';
        var _currentPrice = asset.price || 0;
        var _risk = tp.sl != null ? Math.abs(tp.entry - tp.sl) : 0;
        var _inProfit = _isLong ? _currentPrice > tp.entry : _currentPrice < tp.entry;
        var _profitClass = _currentPrice > 0 ? (_inProfit ? ' pos-in-profit' : ' pos-in-loss') : '';

        // P&L from entry
        var _plPct = _currentPrice > 0 ? ((_currentPrice - tp.entry) / tp.entry * 100).toFixed(1) : '0.0';
        if (!_isLong) _plPct = (-parseFloat(_plPct)).toFixed(1);
        var _plSign = parseFloat(_plPct) >= 0 ? '+' : '';
        var _plClass = _inProfit ? 'pos-pl-profit' : 'pos-pl-loss';
        var _currentRR = _risk > 0 ? (Math.abs(_currentPrice - tp.entry) / _risk).toFixed(1) : '0.0';

        html += '<div class="scanner-plan-card ' + _dirClass + _profitClass + '">';

        // header: SYMBOL · ▲/▼ bias · verdict display label (score/total)
        html += '<div class="scanner-plan-header">';
        html += '<span class="scanner-dir scanner-dir-' + _dirCls + ' pos-card-dir">' + biasArrow + ' ' + escapeHtml(biasWord) + '</span> ';
        html += '<span class="pos-card-symbol">' + escapeHtml(asset.symbol) + '</span>';
        if (_currentPrice > 0) {
            html += '<span class="pos-card-price">$' + _fmtScannerPrice(_currentPrice) + '</span>';
            html += ' <span class="pos-card-pl ' + _plClass + '">(' + _plSign + _plPct + '%)</span>';
        }
        html += ' <span class="scanner-verdict-badge ' + _vcCls + '">' + escapeHtml(verdictDisplay) + ' (' + (best.total || 0) + '/12)</span>';
        if (tp.entry_type) html += '<span class="scanner-entry-type scanner-entry-' + _scannerClsToken(tp.entry_type) + '" style="margin-left:auto">' + escapeHtml(String(tp.entry_type).toUpperCase()) + '</span>';
        if (tp.price_level) html += '<span class="scanner-tp-position-inline">' + escapeHtml(tp.price_level) + '</span>';
        html += '</div>';

        // reference levels (relabelled from ENTRY/SL/TP)
        html += '<div class="scanner-ref-levels-label">Structure-derived reference levels</div>';
        // Scanner v4 (Claude Design port) — connected LEVEL LADDER replacing the flat level list
        html += '<div class="ladder">';
        var _levels = [];
        _levels.push({label: 'Ref entry', price: tp.entry, cls: 'pos-level-entry', pct: null, rr: null});
        if (tp.sl != null) _levels.push({label: 'Invalidation', price: tp.sl, cls: 'pos-level-sl', pct: tp.sl_pct, rr: null, name: tp.sl_name});
        if (tp.tp1 != null) _levels.push({label: 'Target 1', price: tp.tp1, cls: 'pos-level-tp', pct: tp.tp1_pct, rr: tp.rr1, name: tp.tp1_name});
        if (tp.tp2 != null) _levels.push({label: 'Target 2', price: tp.tp2, cls: 'pos-level-tp', pct: tp.tp2_pct, rr: tp.rr2, name: tp.tp2_name});
        _levels.sort(function(a, b) { return b.price - a.price; });

        // Find where current price sits between levels
        var _priceLineAfter = -1;
        if (_currentPrice > 0) {
            for (var _li = 0; _li < _levels.length - 1; _li++) {
                if (_currentPrice <= _levels[_li].price && _currentPrice >= _levels[_li + 1].price) {
                    _priceLineAfter = _li;
                    break;
                }
            }
            if (_currentPrice > _levels[0].price) _priceLineAfter = -2;
            if (_currentPrice < _levels[_levels.length - 1].price) _priceLineAfter = _levels.length - 1;
        }

        // Live-price node — restored as a real node on the ladder spine at its true position
        var _liveLvlHtml = _currentPrice > 0 ? '<div class="lvl liveprice">' +
            '<div class="node-col"><span class="node"></span></div>' +
            '<div class="lvl-main"><span class="lt">Live price</span></div>' +
            '<div class="lvl-vals"><span class="lp">' + _fmtScannerPrice(_currentPrice) + '</span>' +
            '<div class="lvl-metrics"><span class="dist ' + (_inProfit ? 'pos' : 'neg') + '">' + _plSign + _plPct + '%</span><span class="rmult">1:' + _currentRR + '</span></div>' +
            '</div></div>' : '';

        if (_priceLineAfter === -2) html += _liveLvlHtml;
        _levels.forEach(function(lv, idx) {
            var _lcls = lv.cls === 'pos-level-sl' ? 'invalidation' : (lv.cls === 'pos-level-tp' ? 'target' : 'entry');
            var pctStr = lv.pct != null ? (lv.pct > 0 ? '+' : '') + lv.pct.toFixed(1) + '%' : '';
            var rrStr = lv.rr != null ? '1:' + lv.rr.toFixed(1) : '';
            var chips = '';
            if (lv.name) {
                var _lvf = _expandConfLabel(lv.name).split(', ').filter(Boolean);
                chips = _lvf.slice(0, 3).map(function(p) { return '<span class="src-chip">' + escapeHtml(p) + '</span>'; }).join('');
                if (_lvf.length > 3) chips += '<span class="src-more" title="' + escapeHtml(_lvf.join(', ')) + '">+' + (_lvf.length - 3) + '</span>';
            }
            html += '<div class="lvl ' + _lcls + '">';
            html += '<div class="node-col"><span class="node"></span></div>';
            html += '<div class="lvl-main"><span class="lt">' + escapeHtml(lv.label) + '</span>' + (chips ? '<div class="sources">' + chips + '</div>' : '') + '</div>';
            html += '<div class="lvl-vals"><span class="lp">' + _fmtScannerPrice(lv.price) + '</span>';
            html += '<div class="lvl-metrics">' + (pctStr ? '<span class="dist">' + pctStr + '</span>' : '') + (rrStr ? '<span class="rmult">' + rrStr + '</span>' : '') + '</div>';
            html += '</div>';
            html += '</div>';
            if (idx === _priceLineAfter) html += _liveLvlHtml;
        });
        html += '</div>';

        // Strategy / structure notes
        if (tp.setup_strategy) {
            html += '<div class="scanner-tp-strategy">' + escapeHtml(tp.setup_strategy) + '</div>';
        }

        // disclaimer line
        html += '<div class="scanner-disclaimer">Ideas for your own analysis — not financial advice. DYOR.</div>';

        // Action buttons — Validate removed; Copy-for-my-agent added
        if (tp.entry && tp.sl != null && tp.tp1 != null) {
            var _tpData = escapeHtml(JSON.stringify({sym: asset.symbol, dir: dir, entry: tp.entry, sl: tp.sl, tp1: tp.tp1 || null, tp2: tp.tp2 || null, setupType: setupType, entryType: tp.entry_type || 'market'}));
            html += '<div class="scanner-plan-actions">';
            html += '<button class="scanner-draw-btn" data-tp="' + _tpData + '" data-action="scanner-draw-position" data-log="0">Project</button>';
            html += '<button class="scanner-draw-btn scanner-log-btn" data-tp="' + _tpData + '" data-action="scanner-draw-position" data-log="1">Log</button>';
            html += '<button class="scanner-draw-btn scanner-conf-btn" data-sym="' + escapeHtml(asset.symbol) + '" data-dir="' + escapeHtml(dir) + '" data-action="scanner-draw-confluence">Confluence</button>';
            html += '<button class="scanner-draw-btn scanner-copy-agent-btn" data-cp="' + escapeHtml(_copyData) + '" data-action="scanner-copy-agent"><svg width="11" height="11" viewBox="0 0 14 14" style="vertical-align:-1px"><rect x="2.5" y="4" width="7.5" height="8" rx="1.3" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5 4 V2.2 H11.5 V10" fill="none" stroke="currentColor" stroke-width="1.3"/></svg> Copy for my agent</button>';
            html += '</div>';
        }
        html += '</div>'; // close scanner-plan-card
    }
    html += '</div>'; // close scanner-col-card

    // Right: Criteria grid + analysis details below
    html += '<div class="scanner-detail-col scanner-col-criteria">';
    html += '<div class="scanner-criteria-grid">';
    var _critNames = ['trend', 'momentum', 'rsi', 'structure', 'level', 'macro'];
    _critNames.forEach(function(name) {
        var c = criteria[name] || {};
        var dots = _scoreDots(c.score || 0, 2);
        html += '<div class="scanner-criterion">';
        html += '<span class="scanner-crit-name">' + name + '</span>';
        html += '<span class="scanner-dots">' + dots + '</span>';
        html += '<span class="scanner-evidence">' + escapeHtml(c.evidence || '') + '</span>';
        html += '</div>';
    });
    html += '</div>';
    var vetoes = best.vetoes || [];
    if (vetoes.length > 0) {
        html += '<div class="scanner-vetoes">' + vetoes.map(function(v) { return escapeHtml(v); }).join(' | ') + '</div>';
    }

    // Invalidation
    if (setup.invalidation) {
        html += '<div class="scanner-detail-invalidation">Invalidation: ' + escapeHtml(setup.invalidation) + '</div>';
    }

    // Confluences: prefer label_friendly, fall back to _expandConfLabel
    var conf = best.confluences;
    if (conf && (conf.supports && conf.supports.length || conf.resistances && conf.resistances.length)) {
        html += '<div class="scanner-detail-confluences">';
        var _allZones = (conf.supports || []).concat(conf.resistances || []);
        _allZones.sort(function(a, b) { return b.strength - a.strength; });
        _allZones.slice(0, 6).forEach(function(z) {
            var sideClass = z.side === 'support' ? 'scanner-conf-support' : 'scanner-conf-resistance';
            // Use label_friendly when available, else fall back to expanding terse code label
            var friendlyLabel = z.label_friendly || _expandConfLabel(z.label);
            var _cfp = friendlyLabel.split(/\s*[+,]\s*/).filter(Boolean);
            if (_cfp.length > 3) friendlyLabel = _cfp.slice(0, 3).join(' + ') + ' +' + (_cfp.length - 3) + ' more';
            // Tier badge
            var tierBadge = z.tier ? '<span class="scanner-conf-tier scanner-conf-tier-' + _scannerClsToken(z.tier) + '">' + escapeHtml(String(z.tier)) + '</span> ' : '';
            html += '<span class="scanner-confluence-zone ' + sideClass + '">';
            html += _fmtScannerPrice(z.price) + ' ' + tierBadge + '<small>' + escapeHtml(friendlyLabel) + '</small>';
            html += '</span>';
        });
        html += '</div>';
    }

    // Opposite direction
    var otherDir = dir === 'long' ? 'short' : 'long';
    var otherData = asset[otherDir] || {};
    if (otherData.total) {
        html += '<div class="scanner-detail-alt">Opposite: ' + otherDir.charAt(0).toUpperCase() + otherDir.slice(1) + ' scores ' + (otherData.total || 0) + '/12 — ' + escapeHtml(_scannerVerdictLabel(otherData.verdict || '')) + '</div>';
    }

    html += '</div>'; // close scanner-col-criteria
    html += '</div>'; // close scanner-detail-columns
    html += '</div>'; // close scanner-detail-body
    html += '</td></tr>'; // close detail row
    return html;
}

// Insert (building/caching if needed) a scanner detail row as `row`'s immediate next
// sibling — required by the CSS sibling selector `.scanner-trow.open + tr.scanner-
// detail-trow`. No-ops if a detail row is already resident there: closing a row only
// toggles the `.open` class (CSS hides the sibling), it never removes the node, so
// re-expanding the same row within one render pass is free.
function _scannerEnsureDetailRow(row, asset, macro) {
    if (!row || !asset) return;
    var next = row.nextElementSibling;
    if (next && next.classList.contains('scanner-detail-trow')) return;
    var dirKey = _scannerDirFilter === 'both' ? asset.best : _scannerDirFilter;
    var cacheKey = asset.symbol + '::' + dirKey;
    var detailHtml = _scannerDetailCache[cacheKey];
    if (!detailHtml) {
        detailHtml = _scannerBuildDetailRowHtml(asset, macro);
        _scannerDetailCache[cacheKey] = detailHtml;
    }
    row.insertAdjacentHTML('afterend', detailHtml);
}

function _fmtScannerPrice(p) {
    if (p == null) return '?';
    if (p < 0.01) return p.toFixed(6);
    if (p < 1) return p.toFixed(4);
    if (p < 100) return p.toFixed(2);
    return p.toLocaleString(undefined, {maximumFractionDigits: 2});
}

var _confLabelMap = {
    'S1': 'S1', 'S2': 'S2', 'S3': 'S3', 'R1': 'R1', 'R2': 'R2', 'R3': 'R3',
    'PIVOT': 'Pivot', 'TC': 'CPR Top', 'BC': 'CPR Bottom',
    'PWH': 'Prev Week High', 'PWL': 'Prev Week Low',
    'PMH': 'Prev Month High', 'PML': 'Prev Month Low',
    'PDH': 'Prev Day High', 'PDL': 'Prev Day Low',
    'SW_H': 'Swing High', 'SW_L': 'Swing Low',
    'SW_PH': 'Prev Swing High', 'SW_PL': 'Prev Swing Low',
    'EMA21': 'EMA 21', 'EMA30': 'EMA 30', 'EMA50': 'EMA 50', 'EMA60': 'EMA 60',
    'EMA200': 'EMA 200', 'SMA200': 'SMA 200',
    'BB_U': 'BB Upper', 'BB_M': 'BB Middle', 'BB_L': 'BB Lower',
    'POC': 'POC', 'VAH': 'Value Area High', 'VAL': 'Value Area Low',
    'MH': 'Monthly High', 'ML': 'Monthly Low'
};
function _expandConfLabel(label) {
    if (!label) return '';
    return label.split('+').map(function(k) { return _confLabelMap[k] || k; }).join(', ');
}

function _scoreDots(score, max) {
    var filled = '';
    var empty = '';
    for (var i = 0; i < score; i++) filled += '<span class="scanner-dot-filled">&#9679;</span>';
    for (var i = score; i < max; i++) empty += '<span class="scanner-dot-empty">&#9675;</span>';
    return filled + empty;
}

// Copy-for-my-agent: build a paste-ready prompt from row data
function _scannerCopyForAgent(btn) {
    var raw;
    try { raw = JSON.parse(btn.getAttribute('data-cp')); } catch(e) { return; }

    var sym = raw.sym || '?';
    var dir = raw.dir || '?';
    var price = raw.price != null ? _fmtScannerPrice(raw.price) : '?';
    var verdict = _scannerVerdictLabel(raw.verdict || '');
    var score = raw.score != null ? raw.score : '?';
    var setup = raw.setup || '';
    var invalidation = raw.invalidation || '';
    var tp = raw.tp || {};
    var criteria = raw.criteria || {};
    var conf = raw.conf || {};
    var macro = raw.macro || {};

    var lines = [];
    lines.push('=== Carrac Scanner Idea — ' + sym + ' ===');
    lines.push('Analyze this as a trading idea (not financial advice). Apply your own judgment before acting.');
    lines.push('');
    lines.push('Asset: ' + sym);
    lines.push('Current price: ' + price);
    lines.push('Observed bias: ' + (dir === 'long' ? '▲ bullish' : '▼ bearish'));
    lines.push('Alignment: ' + verdict + ' (' + score + '/12)');
    if (setup) lines.push('Setup type: ' + setup);

    // Macro/regime context
    if (macro && Object.keys(macro).length) {
        lines.push('');
        lines.push('--- Regime / Macro context ---');
        if (macro.regime) lines.push('Regime: ' + macro.regime);
        if (macro.trend) lines.push('Trend: ' + macro.trend);
        if (macro.dxy) lines.push('DXY: ' + macro.dxy);
        if (macro.btc_dominance) lines.push('BTC dominance: ' + macro.btc_dominance);
    }

    // Criteria
    var critKeys = Object.keys(criteria);
    if (critKeys.length) {
        lines.push('');
        lines.push('--- Indicator observations ---');
        critKeys.forEach(function(name) {
            var c = criteria[name] || {};
            var s = c.score != null ? c.score + '/2' : '';
            var ev = c.evidence || '';
            lines.push(name + (s ? ' (' + s + ')' : '') + (ev ? ': ' + ev : ''));
        });
    }

    // Confluence zones
    var allZones = ((conf.supports || []).concat(conf.resistances || [])).sort(function(a, b) { return b.strength - a.strength; });
    if (allZones.length) {
        lines.push('');
        lines.push('--- Confluence zones (structure-derived reference levels) ---');
        allZones.slice(0, 6).forEach(function(z) {
            var friendlyLabel = z.label_friendly || _expandConfLabel(z.label) || z.label || '';
            var tier = z.tier ? ' [' + z.tier + ']' : '';
            lines.push((z.side || '') + ' @ ' + _fmtScannerPrice(z.price) + tier + ' — ' + friendlyLabel);
        });
    }

    // Reference levels
    if (tp.entry != null) {
        lines.push('');
        lines.push('--- Structure-derived reference levels ---');
        lines.push('Ref entry: ' + _fmtScannerPrice(tp.entry));
        if (tp.sl != null) lines.push('Invalidation ref: ' + _fmtScannerPrice(tp.sl) + (tp.sl_pct != null ? ' (' + tp.sl_pct.toFixed(1) + '%)' : '') + (tp.sl_name ? ' [' + tp.sl_name + ']' : ''));
        if (tp.tp1 != null) lines.push('Target 1 ref: ' + _fmtScannerPrice(tp.tp1) + (tp.tp1_pct != null ? ' (' + tp.tp1_pct.toFixed(1) + '%)' : '') + (tp.rr1 != null ? ' R:R ' + tp.rr1.toFixed(1) : '') + (tp.tp1_name ? ' [' + tp.tp1_name + ']' : ''));
        if (tp.tp2 != null) lines.push('Target 2 ref: ' + _fmtScannerPrice(tp.tp2) + (tp.tp2_pct != null ? ' (' + tp.tp2_pct.toFixed(1) + '%)' : '') + (tp.rr2 != null ? ' R:R ' + tp.rr2.toFixed(1) : ''));
    }

    if (invalidation) {
        lines.push('');
        lines.push('Invalidation condition: ' + invalidation);
    }

    lines.push('');
    lines.push('---');
    lines.push('Live data and deeper analysis available via Carrac MCP: scanner_get_screen, scanner_get_confluence_zones, market_get_chart');

    var text = lines.join('\n');

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
            if (typeof showToast === 'function') showToast('Copied for your agent');
        }).catch(function() {
            _scannerCopyFallback(text);
        });
    } else {
        _scannerCopyFallback(text);
    }
}

function _scannerCopyFallback(text) {
    // Non-secure context fallback (e.g., http)
    try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        if (typeof showToast === 'function') showToast('Copied for your agent');
    } catch(e) {
        if (typeof showToast === 'function') showToast('Copy failed — select text manually');
    }
}

function _drawScannerPosition(btn, logTrade) {
    var data;
    try { data = JSON.parse(btn.getAttribute('data-tp')); } catch(e) { return; }
    var sym = data.sym;
    var entry = data.entry;
    var sl = data.sl;
    var tp1 = data.tp1;
    var isLong = data.dir === 'long';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) ? klineChartTimeframe : '1D';
    var now = Date.now();
    var origLabel = btn.textContent;

    // Build position points: entry, SL, final TP
    // The overlay renderer labels ext.tps as TP1,TP2... and points[2] as the final TP.
    // So put the furthest target as points[2] and closer ones as intermediate TPs.
    var mainTP = data.tp2 != null ? data.tp2 : tp1;
    var intermediateTPs = [];
    if (data.tp2 != null && tp1 != null) intermediateTPs.push(tp1);

    // For LIMIT entries, entry line is at limit price (not current price)
    var entryVal = entry;
    var points = [
        { timestamp: now, value: entryVal },
        { timestamp: now, value: sl }
    ];
    if (mainTP != null) points.push({ timestamp: now, value: mainTP });

    var localId = 'scanner_' + sym + '_' + now;
    var extData = { direction: isLong ? 'bullish' : 'bearish' };
    if (intermediateTPs.length > 0) extData.tps = intermediateTPs;

    var payload = {
        asset: sym,
        timeframe: tf,
        drawing_type: 'position',
        label: logTrade ? 'Trade ' + data.dir : 'Scanner ' + data.dir,
        metadata: {
            kline_name: 'position',
            points: points,
            color: isLong ? '#3fb950' : '#f85149',
            width: 2,
            local_id: localId,
            extend_data: extData
        }
    };

    btn.disabled = true;
    btn.textContent = '...';

    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var headers = { 'Content-Type': 'application/json' };
    if (csrfMeta) headers['X-CSRFToken'] = csrfMeta.getAttribute('content');

    fetch('/api/drawings', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(result) {
        if (result.success || result.drawing_id) {
            var drawingId = result.drawing_id || result.id;
            // Save to localStorage so drawing persists across chart loads
            try {
                var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
                if (!allDrawings[sym]) allDrawings[sym] = [];
                allDrawings[sym].push({
                    name: 'position',
                    id: localId,
                    points: points,
                    color: isLong ? '#3fb950' : '#f85149',
                    width: 2,
                    extendData: extData
                });
                window._suppressDrawingSync = true;
                localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
                setTimeout(function() { window._suppressDrawingSync = false; }, 300);
            } catch(e) {}

            if (sym === klineChartSymbol) {
                // Same symbol — create overlay directly, no chart reload needed
                if (typeof klineChart !== 'undefined' && klineChart && typeof snapPointsToData === 'function') {
                    try {
                        var overlayOpts = {
                            name: 'position',
                            id: localId,
                            points: snapPointsToData(points),
                            extendData: extData,
                            styles: {
                                line: { color: isLong ? '#3fb950' : '#f85149', size: 2 },
                                point: { color: 'transparent', borderColor: 'transparent', borderSize: 0, radius: 0, activeColor: 'transparent', activeBorderColor: 'transparent', activeBorderSize: 0, activeRadius: 0 }
                            }
                        };
                        if (typeof getOverlayCallbacks === 'function') {
                            Object.assign(overlayOpts, getOverlayCallbacks());
                        }
                        klineChart.createOverlay(overlayOpts, 'candle_pane');
                        klineChart.scrollToRealTime();
                    } catch(e) {}
                }
            } else if (typeof selectAssetFromSearch === 'function') {
                // Different symbol — close drawer first, then navigate after
                // layout settles (synchronous navigation from fetch callback
                // causes chart to collapse when drawer is open)
                if (typeof closeDrawer === 'function') closeDrawer();
                setTimeout(function() {
                    selectAssetFromSearch(sym);
                }, 300);
            }

            if (!logTrade) {
                btn.textContent = 'Drawn';
                btn.classList.add('drawn');
                return;
            }

            // Build indicators_at_entry from scanner data
            var indicators = { source: 'scanner' };
            if (_scannerData && _scannerData.results) {
                for (var i = 0; i < _scannerData.results.length; i++) {
                    if (_scannerData.results[i].symbol === sym) {
                        var scoreData = _scannerData.results[i][data.dir] || {};
                        indicators.score_total = scoreData.total;
                        indicators.verdict = scoreData.verdict;
                        indicators.criteria = scoreData.criteria;
                        indicators.vetoes = scoreData.vetoes;
                        indicators.setup = scoreData.setup;
                        indicators.trade_plan = scoreData.trade_plan;
                        break;
                    }
                }
            }

            // Create journal entry
            var journalPayload = {
                asset: sym,
                timeframe: tf,
                direction: data.dir,
                entry_price: entry,
                stop_price: sl,
                target_price: tp1,
                setup_type: data.setupType || null,
                drawing_id: drawingId || null,
                indicators_at_entry: indicators,
            };
            return fetch('/api/journal/trades', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify(journalPayload)
            }).then(function(r) { return r.json(); }).then(function(jr) {
                if (jr.id) {
                    btn.textContent = 'Logged';
                    btn.classList.add('drawn');
                } else {
                    btn.textContent = 'Draw OK, log failed';
                    setTimeout(function() { btn.textContent = origLabel; btn.disabled = false; }, 2000);
                }
            });
        } else {
            btn.textContent = 'Error';
            btn.disabled = false;
            setTimeout(function() { btn.textContent = origLabel; }, 2000);
        }
    }).catch(function() {
        btn.textContent = 'Error';
        btn.disabled = false;
        setTimeout(function() { btn.textContent = origLabel; }, 2000);
    });
}

var _confluenceOverlayIds = [];

function _drawConfluenceZones(btn) {
    var sym = btn.getAttribute('data-sym');
    var dir = btn.getAttribute('data-dir');
    if (!sym || !_scannerData || !_scannerData.results) return;

    // Find confluences for this asset
    var conf = null;
    for (var i = 0; i < _scannerData.results.length; i++) {
        if (_scannerData.results[i].symbol === sym) {
            var scoreData = _scannerData.results[i][dir] || {};
            conf = scoreData.confluences;
            break;
        }
    }
    if (!conf) return;

    // Navigate to chart
    if (sym !== klineChartSymbol && typeof selectAssetFromSearch === 'function') {
        selectAssetFromSearch(sym);
    }

    // Clear previous confluence overlays
    if (typeof klineChart !== 'undefined' && klineChart) {
        _confluenceOverlayIds.forEach(function(id) {
            try { klineChart.removeOverlay(id); } catch(e) {}
        });
    }
    _confluenceOverlayIds = [];

    // Draw horizontal lines after chart loads
    setTimeout(function() {
        if (typeof klineChart === 'undefined' || !klineChart) return;

        var allZones = (conf.supports || []).concat(conf.resistances || []);
        allZones.forEach(function(zone) {
            var isSup = zone.side === 'support';
            var strength = zone.strength || 1;
            // Color: support = red shades, resistance = green shades
            var baseR = isSup ? 239 : 63;
            var baseG = isSup ? 83 : 185;
            var baseB = isSup ? 80 : 80;
            var alpha = Math.min(0.3 + strength * 0.1, 0.8);
            var color = 'rgba(' + baseR + ',' + baseG + ',' + baseB + ',' + alpha + ')';
            var lineWidth = Math.min(1 + Math.floor(strength / 2), 3);

            try {
                var id = klineChart.createOverlay({
                    name: 'horizontalStraightLine',
                    points: [{ value: zone.price }],
                    styles: {
                        line: { color: color, size: lineWidth, style: strength >= 2.5 ? 'solid' : 'dashed' },
                        text: { color: color, size: 10 }
                    },
                    extendData: zone.label + ' (' + strength + ')',
                    lock: true,
                });
                if (id) _confluenceOverlayIds.push(id);
            } catch(e) {}
        });

        btn.textContent = 'Drawn';
        btn.classList.add('drawn');
    }, 1200);
}

function _showScannerStats() {
    var section = document.getElementById('drawer-section-scanner');
    if (!section) return;
    section.innerHTML = '<div style="padding:20px;text-align:center;color:#787b86">Loading stats...</div>';

    fetch('/api/scanner/stats').then(function(r) { return r.json(); }).then(function(stats) {
        var html = '<div class="scanner-stats-view">';
        html += '<div class="scanner-stats-header">';
        html += '<span style="font-weight:600">Scanner Stats</span>';
        html += '<button class="scanner-back-btn" data-action="scanner-home">&#8592; Back</button>';
        html += '</div>';

        if (stats.total_trades === 0) {
            html += '<div style="padding:20px;text-align:center;color:#787b86">No completed trades with snapshots yet.</div>';
            html += '</div>';
            section.innerHTML = html;
            return;
        }

        html += '<div class="scanner-stats-summary">';
        html += '<span>' + stats.total_trades + ' trades</span>';
        html += '<span>Win rate: ' + (stats.overall_win_rate * 100).toFixed(1) + '%</span>';
        html += '</div>';

        // Per-criterion win rates
        html += '<div class="scanner-stats-label">Win Rate by Criterion Score</div>';
        var criteria = ['trend', 'momentum', 'rsi', 'structure', 'level', 'macro'];
        html += '<div class="scanner-stats-grid">';
        html += '<div class="scanner-stats-hdr"></div><div class="scanner-stats-hdr">0</div><div class="scanner-stats-hdr">1</div><div class="scanner-stats-hdr">2</div>';
        criteria.forEach(function(c) {
            var bucket = stats.by_criterion[c] || {};
            html += '<div class="scanner-stats-crit">' + c + '</div>';
            [0, 1, 2].forEach(function(s) {
                var b = bucket['score_' + s] || {count: 0, win_rate: 0};
                var wr = (b.win_rate * 100).toFixed(0);
                var barW = Math.max(4, b.win_rate * 100);
                var cls = b.win_rate >= 0.6 ? 'scanner-bar-good' : b.win_rate >= 0.4 ? 'scanner-bar-mid' : 'scanner-bar-low';
                html += '<div class="scanner-stats-cell">';
                html += '<div class="scanner-bar ' + cls + '" style="width:' + barW + '%"></div>';
                html += '<span>' + wr + '% <small>(' + b.count + ')</small></span>';
                html += '</div>';
            });
        });
        html += '</div>';

        // Per-verdict win rates
        html += '<div class="scanner-stats-label" style="margin-top:12px">Win Rate by Verdict</div>';
        html += '<div class="scanner-verdict-bars">';
        ['STRONG', 'GOOD', 'NEUTRAL', 'WEAK', 'AVOID'].forEach(function(v) {
            var b = stats.by_verdict[v] || {count: 0, win_rate: 0};
            if (b.count === 0) return;
            var wr = (b.win_rate * 100).toFixed(0);
            var vc = 'scanner-v-' + v.toLowerCase();
            html += '<div class="scanner-verdict-row">';
            html += '<span class="scanner-verdict-label ' + vc + '">' + v + '</span>';
            html += '<div class="scanner-verdict-bar-track"><div class="scanner-verdict-bar-fill ' + vc + '" style="width:' + wr + '%"></div></div>';
            html += '<span class="scanner-verdict-val">' + wr + '% (' + b.count + ')</span>';
            html += '</div>';
        });
        html += '</div>';

        html += '</div>';
        section.innerHTML = html;
    }).catch(function() {
        section.innerHTML = '<div style="padding:20px;text-align:center;color:#f85149">Failed to load stats</div>';
    });
}


var _alertFormExpanded = false;

function _toggleAlertForm(expand) {
    _alertFormExpanded = (expand !== undefined) ? expand : !_alertFormExpanded;
    updateDrawerAlerts();
}


// CSP hardening — the alerts tab only has two injected inline handlers (the shared
// "show all" toggle + the inline-edit Cancel button); bound EXACTLY ONCE, guarded,
// on the section so they survive every re-render (sectionEl.innerHTML is reassigned
// wholesale on every fetch below).
function _wireAlertsDelegatedEvents(sectionEl) {
    if (sectionEl.dataset.alertActionsWired) return;
    sectionEl.dataset.alertActionsWired = '1';
    sectionEl.addEventListener('click', function(e) {
        var el = e.target.closest('[data-action]');
        if (!el || !sectionEl.contains(el)) return;
        var action = el.getAttribute('data-action');
        if (action === 'drawer-toggle-show-all') { _drawerToggleShowAll(el.getAttribute('data-tab')); }
        else if (action === 'alert-cancel-edit') { cancelEditAlert(); }
    });
}

function updateDrawerAlerts(opts) {
    var summaryEl = document.getElementById('drawer-alerts-summary');
    var sectionEl = document.getElementById('drawer-section-alerts');
    var tabEl = document.getElementById('drawer-tab-alerts');
    if (!sectionEl) return;
    _wireAlertsDelegatedEvents(sectionEl);

    var currentPrice = allAssets[klineChartSymbol] ? allAssets[klineChartSymbol].latest_price : 0;

    // Preserve current form values if form already exists
    var existingPriceInput = document.getElementById('alert-price-input');
    var savedPrice = existingPriceInput ? existingPriceInput.value : '';
    var existingNoteInput = document.getElementById('alert-note-input');
    var savedNote = existingNoteInput ? existingNoteInput.value : '';
    var existingFreqSelect = document.getElementById('alert-freq-select');
    var savedFreq = existingFreqSelect ? existingFreqSelect.value : '';
    var savedEditId = _editingAlertId;

    // T4.3: route through dedupFetch so concurrent callers within 2 s share one
    // GET /api/alerts?active=true. renderAlertOverlays() in alert-panel.js fetches
    // the same URL; collapsing them saves one round-trip per asset switch.
    // opts.fresh bypasses the dedup cache so a post-mutation refresh (right after
    // creating/deleting an alert) never picks up a stale cached list.
    var _fresh = opts && opts.fresh;
    var _alertsFetch = _fresh
        ? fetch('/api/alerts?active=true', { credentials: 'same-origin', cache: 'no-store' })
        : ((typeof dedupFetch === 'function')
            ? dedupFetch('/api/alerts?active=true', { credentials: 'same-origin' }, 2000)
            : fetch('/api/alerts?active=true', { credentials: 'same-origin' }));
    _alertsFetch
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var all = data.alerts || [];
        var showAll = _drawerShowAll.alerts;
        var currentAlerts = all.filter(function(a) { return a.symbol === klineChartSymbol; });
        var alerts;

        // Sort by distance from current price (closest first) within each asset group
        function alertSortKey(a) {
            var cp = allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0;
            if (a.alert_type === 'trendline_break') return 999999; // trendlines at end
            return cp > 0 ? Math.abs(a.price - cp) / cp : 0;
        }
        currentAlerts.sort(function(a, b) { return alertSortKey(a) - alertSortKey(b); });

        if (showAll) {
            var otherAlerts = all.filter(function(a) { return a.symbol !== klineChartSymbol; });
            // Group others by symbol, sort each group by distance
            var bySymbol = {};
            otherAlerts.forEach(function(a) {
                if (!bySymbol[a.symbol]) bySymbol[a.symbol] = [];
                bySymbol[a.symbol].push(a);
            });
            Object.keys(bySymbol).forEach(function(sym) {
                bySymbol[sym].sort(function(a, b) { return alertSortKey(a) - alertSortKey(b); });
            });
            var sortedOthers = [];
            Object.keys(bySymbol).sort().forEach(function(sym) {
                sortedOthers = sortedOthers.concat(bySymbol[sym]);
            });
            alerts = currentAlerts.concat(sortedOthers);
        } else {
            alerts = currentAlerts;
        }

        var countLabel = showAll ? all.length : currentAlerts.length;
        if (summaryEl) summaryEl.textContent = countLabel + ' Alert' + (countLabel !== 1 ? 's' : '');
        if (tabEl) tabEl.classList.toggle('has-alerts', countLabel > 0);

        // Build layout: list (left, always visible) + form (right, collapsible)
        var addBtn = _alertFormExpanded ? '' : '<button class="drawer-add-btn" id="alert-add-btn"><i data-lucide="plus" style="width:11px;height:11px"></i> Add Alert</button>';
        var listHeaderHtml =
            '<div class="drawer-list-header">' +
                '<span class="drawer-scroll-count">' + countLabel + ' alert' + (countLabel !== 1 ? 's' : '') + '</span>' +
                _drawerShowAllBtn('alerts') +
                addBtn +
            '</div>' +
            '<div class="alert-table-header">' +
                '<span class="alert-th alert-th-dir"></span>' +
                '<span class="alert-th alert-th-symbol">Symbol</span>' +
                '<span class="alert-th alert-th-price">Price</span>' +
                '<span class="alert-th alert-th-note">Note</span>' +
                '<span class="alert-th alert-th-freq">Freq</span>' +
                '<span class="alert-th alert-th-actions"></span>' +
            '</div>';

        var formHtml = '';
        if (_alertFormExpanded) {
            formHtml =
                '<div class="drawer-alerts-resizer"></div>' +
                '<div class="drawer-alerts-form">' +
                    '<div class="drawer-form-header">' +
                        '<span class="drawer-form-title">' + escapeHtml(klineChartSymbol) + ' · ' + (currentPrice ? formatPriceSimple(currentPrice) : '--') + '</span>' +
                        '<button class="drawer-form-close" id="alert-form-close" title="Close form"><i data-lucide="x" style="width:12px;height:12px"></i></button>' +
                    '</div>' +
                    '<div class="alert-type-tabs" id="alert-type-tabs">' +
                        '<button class="alert-type-tab active" data-alert-type="price">Price</button>' +
                        '<button class="alert-type-tab" data-alert-type="indicator">Indicator</button>' +
                        '<button class="alert-type-tab" data-alert-type="formula">Formula</button>' +
                    '</div>' +
                    '<div class="alert-price-fields" id="alert-price-fields">' +
                        '<input type="number" class="alert-price-input" id="alert-price-input" placeholder="Alert price" step="any">' +
                    '</div>' +
                    '<div class="alert-formula-fields" id="alert-formula-fields" style="display:none">' +
                        '<textarea class="alert-formula-input" id="alert-formula-input" rows="2" placeholder="RSI(14) < 30 AND SQUEEZE_ON()"></textarea>' +
                        '<select class="alert-formula-tf" id="alert-formula-tf">' +
                            '<option value="1D">Daily</option>' +
                            '<option value="4h">4H</option>' +
                            '<option value="1h">1H</option>' +
                            '<option value="1W">Weekly</option>' +
                        '</select>' +
                    '</div>' +
                    '<div class="alert-indicator-fields" id="alert-indicator-fields" style="display:none">' +
                        '<select class="alert-indicator-select" id="alert-indicator-select">' +
                            '<option value="">Select indicator...</option>' +
                            (typeof _indicatorTargetStates !== 'undefined' ? Object.keys(_indicatorTargetStates).map(function(k) {
                                var label = (typeof _indicatorLabels !== 'undefined' && _indicatorLabels[k])
                                    ? _indicatorLabels[k]
                                    : k.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
                                return { k: k, label: label };
                            }).sort(function(a, b) { return a.label.localeCompare(b.label); }).map(function(item) {
                                return '<option value="' + item.k + '">' + item.label + '</option>';
                            }).join('') : '') +
                        '</select>' +
                        '<select class="alert-target-select" id="alert-target-select">' +
                            '<option value="">Select target state...</option>' +
                        '</select>' +
                    '</div>' +
                    '<textarea class="alert-note-input" id="alert-note-input" placeholder="Note (optional)..." maxlength="200" rows="2"></textarea>' +
                    '<div class="drawer-alert-form-bottom">' +
                        '<select class="alert-freq-select" id="alert-freq-select">' +
                            '<option value="once_per_bar" selected>per bar</option>' +
                            '<option value="once">once</option>' +
                        '</select>' +
                        '<button class="alert-create-btn" id="alert-create-btn">Add Alert</button>' +
                    '</div>' +
                    '<div class="drawer-alert-edit-row" id="alert-edit-row">' +
                        '<button class="alert-cancel-btn" id="alert-cancel-btn" data-action="alert-cancel-edit">Cancel</button>' +
                        '<button class="alert-create-btn alert-update-btn" id="alert-update-btn">Update</button>' +
                    '</div>' +
                    '<div class="alert-hint">Click chart to set price · Esc to cancel edit</div>' +
                '</div>';
        }

        var layoutStart = '<div class="drawer-alerts-layout">' +
            '<div class="drawer-alerts-list">' + listHeaderHtml;

        // Build alert list with asset group headers in "show all" mode
        var _alertCountBySymbol = {};
        if (showAll) {
            alerts.forEach(function(a) { _alertCountBySymbol[a.symbol] = (_alertCountBySymbol[a.symbol] || 0) + 1; });
        }
        var listHtml = '';
        if (alerts.length > 0) {
            var _lastAlertSymbol = null;
            listHtml = alerts.map(function(a) {
                var groupHdr = '';
                if (showAll && a.symbol !== _lastAlertSymbol) {
                    _lastAlertSymbol = a.symbol;
                    var cnt = _alertCountBySymbol[a.symbol] || 0;
                    groupHdr = _drawerAssetGroupHeader(a.symbol, cnt + ' alert' + (cnt !== 1 ? 's' : ''));
                }
                var isAbove = a.price > (allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0);
                var dirClass = isAbove ? 'alert-above' : 'alert-below';
                var dirIcon = isAbove ? '&#9650;' : '&#9660;';
                var freqLabel = a.frequency === 'once_per_bar' ? 'per bar' : 'once';
                var noteSpan = a.note ? '<span class="alert-item-note">' + escapeHtml(a.note) + '</span>' : '';
                var symbolLabel = '<span class="alert-item-symbol">' + escapeHtml(a.symbol) + '</span>';
                var isTriggered = a.triggered === true;
                var triggeredClass = isTriggered ? ' alert-triggered' : '';

                var editingClass = (savedEditId === a.id) ? ' alert-editing' : '';
                var editBtn = '<button class="alert-item-edit" data-alert-id="' + a.id + '" title="Edit"><i data-lucide="pencil"></i></button>';
                var reactivateBtn = isTriggered ? '<button class="alert-item-reactivate" data-alert-id="' + a.id + '">Reactivate</button>' : '';

                if (a.alert_type === 'formula') {
                    var expr = (a.expression || '').substring(0, 30);
                    if ((a.expression || '').length > 30) expr += '...';
                    return groupHdr + '<div class="alert-list-item' + editingClass + triggeredClass + '" data-alert-id="' + a.id + '">' +
                        '<div class="alert-item-main">' +
                            '<span class="alert-item-dir" style="font-size:9px;font-family:monospace">f(x)</span>' +
                            symbolLabel +
                            '<span class="alert-item-price alert-item-indicator" style="font-family:monospace;font-size:10px" title="' + escapeHtml(a.expression || '') + '">' + escapeHtml(expr) + ' <span style="opacity:0.5">' + (a.timeframe || '1D') + '</span></span>' +
                            noteSpan +
                            (isTriggered ? '<span class="alert-item-fired">fired</span>' : '<span class="alert-item-freq">' + freqLabel + '</span>') +
                            '<span class="alert-item-actions">' +
                            reactivateBtn +
                            '<button class="alert-item-delete" data-alert-id="' + a.id + '" title="Delete"><i data-lucide="x"></i></button>' +
                            '</span>' +
                        '</div>' +
                    '</div>';
                }

                if (a.alert_type === 'indicator') {
                    var indLabel = (typeof _indicatorLabels !== 'undefined' && _indicatorLabels[a.indicator]) || a.indicator;
                    var targetLabel = (typeof _formatStateLabel === 'function')
                        ? _formatStateLabel(String(a.target_state))
                        : String(a.target_state).replace(/_/g, ' ');
                    return groupHdr + '<div class="alert-list-item' + editingClass + triggeredClass + '" data-alert-id="' + a.id + '">' +
                        '<div class="alert-item-main">' +
                            '<span class="alert-item-dir">⚡</span>' +
                            symbolLabel +
                            '<span class="alert-item-price alert-item-indicator">' + indLabel + ' → ' + targetLabel + '</span>' +
                            noteSpan +
                            (isTriggered ? '<span class="alert-item-fired">fired</span>' : '<span class="alert-item-freq">' + freqLabel + '</span>') +
                            '<span class="alert-item-actions">' +
                            reactivateBtn +
                            '<button class="alert-item-delete" data-alert-id="' + a.id + '" title="Delete"><i data-lucide="x"></i></button>' +
                            '</span>' +
                        '</div>' +
                    '</div>';
                }

                if (a.alert_type === 'trendline_break') {
                    var slopeDir = '↗';
                    var tlValStr = '';
                    if (a.p1_ts && a.p2_ts && a.p1_ts !== a.p2_ts) {
                        slopeDir = a.p2_val > a.p1_val ? '↗' : '↘';
                        var slopePerMs = (a.p2_val - a.p1_val) / (a.p2_ts - a.p1_ts);
                        var nowVal = a.p1_val + slopePerMs * (Date.now() - a.p1_ts);
                        if (nowVal > 0) tlValStr = ' $' + formatPriceSimple(nowVal);
                    }
                    var tlDirClass = a.initial_side === 'below' ? 'alert-above' : 'alert-below';
                    return groupHdr + '<div class="alert-list-item ' + tlDirClass + editingClass + triggeredClass + '" data-alert-id="' + a.id + '">' +
                        '<div class="alert-item-main">' +
                            '<span class="alert-item-dir">' + slopeDir + '</span>' +
                            symbolLabel +
                            '<span class="alert-item-price alert-item-trendline">Trendline' + tlValStr + '</span>' +
                            noteSpan +
                            (isTriggered ? '<span class="alert-item-fired">fired</span>' : '<span class="alert-item-freq">' + freqLabel + '</span>') +
                            '<span class="alert-item-actions">' +
                            reactivateBtn +
                            editBtn +
                            '<button class="alert-item-delete" data-alert-id="' + a.id + '" title="Delete"><i data-lucide="x"></i></button>' +
                            '</span>' +
                        '</div>' +
                    '</div>';
                }

                return groupHdr + '<div class="alert-list-item ' + dirClass + editingClass + triggeredClass + '" data-alert-id="' + a.id + '">' +
                    '<div class="alert-item-main">' +
                        '<span class="alert-item-dir">' + dirIcon + '</span>' +
                        symbolLabel +
                        '<span class="alert-item-price">' + formatPriceSimple(a.price) + '</span>' +
                        noteSpan +
                        (isTriggered ? '<span class="alert-item-fired">fired</span>' : '<span class="alert-item-freq">' + freqLabel + '</span>') +
                        '<span class="alert-item-actions">' +
                        reactivateBtn +
                        editBtn +
                        '<button class="alert-item-delete" data-alert-id="' + a.id + '" title="Delete"><i data-lucide="x"></i></button>' +
                        '</span>' +
                    '</div>' +
                '</div>';
            }).join('');
        } else {
            listHtml = '<div class="drawer-empty">No ' + (showAll ? 'active alerts' : 'alerts for ' + escapeHtml(klineChartSymbol)) + '</div>';
        }

        // Assemble: list (left) + form (right, if expanded)
        sectionEl.innerHTML = layoutStart + listHtml + '</div>' + formHtml + '</div>';
        if (_alertFormExpanded) initAlertResizer(sectionEl);

        // Wire add/close buttons
        var addBtnEl = document.getElementById('alert-add-btn');
        if (addBtnEl) addBtnEl.onclick = function() { _toggleAlertForm(true); };
        var closeBtnEl = document.getElementById('alert-form-close');
        if (closeBtnEl) closeBtnEl.onclick = function() { _toggleAlertForm(false); };

        // Restore form values
        var priceInput = document.getElementById('alert-price-input');
        var noteInput = document.getElementById('alert-note-input');
        var freqSelect = document.getElementById('alert-freq-select');
        if (priceInput && savedPrice) priceInput.value = savedPrice;
        if (noteInput && savedNote) noteInput.value = savedNote;
        if (freqSelect && savedFreq) freqSelect.value = savedFreq;

        // Restore edit mode
        var createBtn = document.getElementById('alert-create-btn');
        var editRow = document.getElementById('alert-edit-row');
        var updateBtn = document.getElementById('alert-update-btn');
        if (savedEditId) {
            _editingAlertId = savedEditId;
            if (createBtn) createBtn.style.display = 'none';
            if (editRow) editRow.classList.add('visible');
        }

        // Wire alert type tabs (Price / Indicator toggle)
        if (typeof initAlertTypeTabs === 'function') initAlertTypeTabs();

        // Wire create button (add mode)
        if (createBtn) createBtn.onclick = saveOrCreateAlert;
        // Wire update button (edit mode)
        if (updateBtn) updateBtn.onclick = saveOrCreateAlert;

        // Enter on price input creates/updates alert, Escape cancels edit
        if (priceInput) priceInput.onkeydown = function(ev) {
            if (ev.key === 'Enter') { ev.preventDefault(); saveOrCreateAlert(); }
            if (ev.key === 'Escape' && _editingAlertId) { ev.preventDefault(); cancelEditAlert(); }
        };

        // Wire edit buttons
        sectionEl.querySelectorAll('.alert-item-edit').forEach(function(btn) {
            btn.onclick = function() {
                var aid = parseInt(this.getAttribute('data-alert-id'));
                var alertData = alerts.find(function(a) { return a.id === aid; });
                if (alertData) editServerAlert(alertData);
            };
        });

        // Wire delete buttons
        sectionEl.querySelectorAll('.alert-item-delete').forEach(function(btn) {
            btn.onclick = function() {
                var aid = this.getAttribute('data-alert-id');
                // If deleting the alert being edited, cancel edit mode
                if (_editingAlertId && String(_editingAlertId) === String(aid)) cancelEditAlert();
                deleteServerAlert(aid);
            };
        });

        // Wire reactivate buttons
        sectionEl.querySelectorAll('.alert-item-reactivate').forEach(function(btn) {
            btn.onclick = function() {
                var aid = this.getAttribute('data-alert-id');
                reactivateServerAlert(aid);
            };
        });

        // Re-init lucide for icons
        if (typeof lucide !== 'undefined') lucide.createIcons({ attrs: { 'stroke-width': 1.75 } });
    })
    .catch(function(e) {
        // A failed /api/alerts read leaves the drawer's alert list
        // empty, indistinguishable from "no alerts" — breadcrumb it.
        if (typeof window.carracLog === 'function') window.carracLog('alert', 'drawer alerts list failed — rendered as empty', String((e && e.message) || e));
    });
}


// Legacy v1 render (kept for backward compatibility with drawing-based scenarios)

// ── Scenarios v2 rendering ──


function _fmtNum(n) {
    if (n == null) return '?';
    return n >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// ==================== Milestone Playbook v2 ====================


// ── Template definitions ──


// ── Stage status computation ──


// ── Playbook rendering (pipeline stepper) ──


// ── Milestone card rendering ──


// ── Card interactions ──


// ── Milestone actions ──


// ── Template application ──


// ── Wizard modal ──


// ── Edit milestone (opens wizard with data) ──


// ── Help popover (compact) ──


// ==================== Chart-First Milestone Creation ====================


// ==================== Milestone Chart Pins ====================


// ==================== Scenario Workshop ====================


// ── Left Panel: Plan Flow ──


// ── Right Panel: Inspector ──


// ── Scenario Inspector ──


// ── Milestone Inspector ──


// ==================== Stage Rail ====================


// ── Shared event wiring for scenario drawer ──


// ── v2 scenario form builder ──


// ── v2 CRUD actions ──


function _renderSinglePosition(posDrawing, symbol) {
    var pts = posDrawing.points.slice();
    if (posDrawing.name === 'longPosition' || posDrawing.name === 'shortPosition') {
        var tmp = pts[1]; pts[1] = pts[2]; pts[2] = tmp;
    }

    var entry = pts[0].value;
    var sl = pts[1].value;
    var tp = pts[2].value;
    var isLong = sl < entry;
    var currentPrice = allAssets[symbol] ? allAssets[symbol].latest_price : 0;

    var dirLabel = isLong ? 'LONG' : 'SHORT';
    var dirClass = isLong ? 'pos-long' : 'pos-short';

    function pctFrom(target, base) {
        if (!base) return '0.0';
        return ((target - base) / base * 100).toFixed(1);
    }

    var risk = Math.abs(entry - sl);
    var reward = Math.abs(tp - entry);
    var rr = risk > 0 ? (reward / risk).toFixed(1) : '0.0';

    var ext = posDrawing.extendData || {};
    var intermediateTPs = ext.tps || [];

    // Build level list
    var levels = [];
    levels.push({ type: 'sl', label: 'SL', price: sl, cls: 'pos-level-sl' });
    levels.push({ type: 'entry', label: 'Entry', price: entry, cls: 'pos-level-entry' });
    // Collect all TPs then sort by distance from entry (closest = TP1)
    var allTPs = [tp].concat(intermediateTPs);
    allTPs.sort(function(a, b) {
        return Math.abs(a - entry) - Math.abs(b - entry);
    });
    allTPs.forEach(function(tpPrice, i) {
        levels.push({ type: 'tp', label: allTPs.length > 1 ? 'TP' + (i + 1) : 'TP', price: tpPrice, cls: 'pos-level-tp' });
    });

    // Sort by price descending (highest at top)
    levels.sort(function(a, b) { return b.price - a.price; });

    // Determine position state
    var stoppedOut = currentPrice > 0 && (isLong ? currentPrice <= sl : currentPrice >= sl);
    var inProfit = isLong ? currentPrice > entry : currentPrice < entry;

    // Find where current price sits between level rows (levels sorted price descending)
    // priceLineAfter = index of the row AFTER which we insert the line
    var priceLineAfter = -1;
    if (currentPrice > 0) {
        // levels are sorted high to low; find where price slots in
        for (var li = 0; li < levels.length - 1; li++) {
            if (currentPrice <= levels[li].price && currentPrice >= levels[li + 1].price) {
                priceLineAfter = li;
                break;
            }
        }
        // Price above all levels
        if (currentPrice > levels[0].price) priceLineAfter = -2;
        // Price below all levels
        if (currentPrice < levels[levels.length - 1].price) priceLineAfter = levels.length - 1;
    }

    var lineColor = stoppedOut ? '#4a4e59' : (inProfit ? '#26a69a' : '#ef5350');
    var plPctFromEntry = pctFrom(currentPrice, entry);
    if (!isLong) plPctFromEntry = (-parseFloat(plPctFromEntry)).toFixed(1);
    var plPctSign = parseFloat(plPctFromEntry) >= 0 ? '+' : '';
    var currentRR = risk > 0 ? (Math.abs(currentPrice - entry) / risk).toFixed(1) : '0.0';
    var priceLineHtml = '<div class="pos-price-line" style="border-color:' + lineColor + ';color:' + lineColor + '">' +
        '<span class="pos-level-label"></span>' +
        '<span class="pos-price-line-val"><span class="pos-line-bg">[' + formatPriceSimple(currentPrice) + ']</span></span>' +
        '<span class="pos-price-line-pct"><span class="pos-line-bg">[' + plPctSign + plPctFromEntry + '%]</span></span>' +
        '<span class="pos-level-rr"><span class="pos-line-bg">[1:' + currentRR + ']</span></span>' +
    '</div>';

    var levelsHtml = '';
    // Price above all levels
    if (priceLineAfter === -2) {
        levelsHtml += priceLineHtml;
    }
    levels.forEach(function(lv, idx) {
        var pct = pctFrom(lv.price, currentPrice);
        var pctSign = pct >= 0 ? '+' : '';
        var rrCol = '<span class="pos-level-rr"></span>';
        if (lv.type === 'tp' && risk > 0) {
            var tpReward = Math.abs(lv.price - entry);
            rrCol = '<span class="pos-level-rr">1:' + (tpReward / risk).toFixed(1) + '</span>';
        }
        levelsHtml += '<div class="pos-level-row ' + lv.cls + '">' +
            '<span class="pos-level-label">' + lv.label + '</span>' +
            '<span class="pos-level-price">' + formatPriceSimple(lv.price) + '</span>' +
            '<span class="pos-level-pct">' + pctSign + pct + '%</span>' +
            rrCol +
        '</div>';
        if (idx === priceLineAfter) {
            levelsHtml += priceLineHtml;
        }
    });

    var stoppedClass = stoppedOut ? ' pos-card-stopped' : '';
    var profitClass = !stoppedOut && currentPrice > 0 ? (inProfit ? ' pos-in-profit' : ' pos-in-loss') : '';
    var stoppedLabel = stoppedOut ? '<div class="pos-card-stopped-label">Stopped Out</div>' : '';

    // P&L percentage from entry
    var plPct = currentPrice > 0 ? pctFrom(currentPrice, entry) : '0.0';
    if (!isLong) plPct = (-parseFloat(plPct)).toFixed(1);
    var plSign = parseFloat(plPct) >= 0 ? '+' : '';
    var plClass = inProfit ? 'pos-pl-profit' : 'pos-pl-loss';
    if (stoppedOut) plClass = 'pos-pl-stopped';

    var priceLabel = currentPrice > 0 ? '<span class="pos-card-price">$' + formatPriceSimple(currentPrice) + '</span>' : '';

    var drawingId = posDrawing.id || '';
    var escapedSym = escapeHtml(symbol);
    var escapedId = escapeHtml(drawingId);

    // symbol/drawingId are read back from the enclosing .pos-card's own
    // data-symbol/data-drawing-id (set just below) by the delegated position handlers.
    var actionsHtml = '<div class="pos-card-actions">' +
        '<button class="scenario-action-btn pos-eval-btn" data-action="pos-validate" title="AI validation of this position">Validate</button>' +
        '<button class="scenario-action-btn scenario-log-btn" data-action="pos-close-log" title="Close position and log to journal">Close &amp; Log</button>' +
        '<button class="scenario-action-btn" data-action="pos-clear" title="Clear position drawing from chart">Clear</button>' +
        '<button class="scenario-action-btn scenario-danger" data-action="pos-delete" title="Delete position from chart and journal">Delete</button>' +
    '</div>';

    return {
        isLong: isLong,
        dirLabel: dirLabel,
        entry: entry,
        html: '<div class="pos-card ' + dirClass + stoppedClass + profitClass + '" data-symbol="' + escapedSym + '" data-drawing-id="' + escapedId + '" style="cursor:pointer">' +
            stoppedLabel +
            '<div class="pos-card-header">' +
                '<span class="pos-card-dir">[' + dirLabel + ']</span> ' +
                '<span class="pos-card-symbol">' + escapedSym + ':</span> ' +
                priceLabel +
                ' <span class="pos-card-pl ' + plClass + '">(' + plSign + plPct + '%)</span>' +
            '</div>' +
            '<div class="pos-card-levels">' + levelsHtml + '</div>' +
            actionsHtml +
        '</div>'
    };
}


function _findAllPositionDrawings(drawings) {
    var results = [];
    for (var i = 0; i < drawings.length; i++) {
        var d = drawings[i];
        if ((d.name === 'position' || d.name === 'longPosition' || d.name === 'shortPosition') && d.points && d.points.length >= 3) {
            results.push(d);
        }
    }
    return results;
}

function _findAllSpotDrawings(drawings) {
    var results = [];
    for (var i = 0; i < drawings.length; i++) {
        var d = drawings[i];
        if (d.name === 'spot' && d.points && d.points.length >= 1 && !(d.extendData && d.extendData.sold)) {
            results.push(d);
        }
    }
    return results;
}

// --- Spot tree view ---
var _spotTreeCollapsed = {};
function _toggleSpotTree(key) { _spotTreeCollapsed[key] = !_spotTreeCollapsed[key]; updateDrawerPosition(); }

function _spotPnl(entry, qty, symbol) {
    var cp = allAssets[symbol] ? allAssets[symbol].latest_price : 0;
    var pct = cp > 0 && entry > 0 ? ((cp - entry) / entry * 100) : 0;
    var usd = qty > 0 && cp > 0 ? ((cp - entry) * qty) : 0;
    var color = Math.abs(pct) < 0.05 ? '#787b86' : (pct >= 0 ? '#26a69a' : '#ef5350');
    return { pct: pct, usd: usd, color: color, sign: pct >= 0 ? '+' : '', cp: cp };
}

function _fmtSpotUsd(n) {
    var abs = Math.abs(n);
    if (abs >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (abs >= 10000) return (n / 1000).toFixed(1) + 'K';
    return n.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function _fmtSpotQty(q) {
    if (q >= 1000) return q.toFixed(1);
    if (q >= 1) return q.toFixed(3);
    if (q >= 0.01) return q.toFixed(4);
    return q.toPrecision(4);
}

// --- Spot buy -> journal promotion (trade_journal is the
// one ledger — a spot buy that never becomes a trade is invisible to
// /portfolio) ---

// A spot buy is a long position in the one ledger. Returns null when the
// buy cannot be represented as a trade — the caller then leaves the drawing
// unpromoted, and the portfolio page reports it rather than under-counting.
function buildSpotTradePayload(symbol, price, qty, drawingId) {
    if (!symbol || !price || price <= 0) return null;
    if (!qty || qty <= 0) return null;
    return {
        asset: symbol,
        direction: 'long',
        entry_price: price,
        size: qty,
        account_tag: 'spot',
        drawing_id: drawingId,
        timeframe: '1D'
    };
}

// POST the promotion. Failure is loud, never swallowed: an unpromoted buy
// is invisible to /portfolio, so a silent `catch` would under-report the
// book. window.carracLog survives production minification (terser strips
// console.* there) — a console.error would be worthless in production.
function _promoteSpotToJournal(symbol, price, qty, drawingId) {
    var payload = buildSpotTradePayload(symbol, price, qty, drawingId);
    if (!payload) return;
    fetch('/api/journal/trades', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken()},
        body: JSON.stringify(payload)
    }).then(function(r) {
        if (!r.ok) throw new Error('promotion failed: ' + r.status);
    }).catch(function(e) {
        if (typeof window.carracLog === 'function') {
            window.carracLog('portfolio', 'spot promotion failed',
                             {symbol: symbol, drawingId: drawingId, err: String(e)});
        }
    });
}

// Pure selector — kept separate from the fetch so it is unit-testable.
//
// Compares as STRINGS on both sides, not `===`. Prod Postgres declares
// trade_journal.drawing_id as `text` (tests/schema/carrac_pg_schema.sql)
// — psycopg2 returns it as a Python str, so /api/positions/live JSON-encodes
// it as a string (e.g. "1754332800000"), while the browser's drawingId is
// always a number (Date.now()). `===` never matches across that type gap,
// so on real Postgres this NEVER resolves — SQLite's INTEGER affinity
// round-trips a Python int, so tests that ran against SQLite never
// showed the mismatch. Explicit null/undefined guard first: a not-yet-promoted position's
// drawing_id is null, and String(null) must never be allowed to match a
// drawingId of 0 (String(0) === '0', not 'null', but the guard makes the
// "unpromoted never matches" intent explicit rather than relying on that
// string spelling never colliding with a real id).
function selectPromotedTradeId(positions, drawingId) {
    if (!positions || !positions.length) return null;
    for (var i = 0; i < positions.length; i++) {
        var p = positions[i];
        if (p.drawing_id === null || p.drawing_id === undefined) continue;
        if (String(p.drawing_id) === String(drawingId)) return p.id;
    }
    return null;
}

// Resolve the journal trade a spot drawing was promoted into, so a sell can
// close THAT trade instead of logging a fresh, unrelated one (which would
// leave the promoted trade open forever and double-count the book).
function _findPromotedTradeId(drawingId, cb) {
    fetch('/api/positions/live', {credentials: 'same-origin'})
        .then(function(r) { return r.json(); })
        .then(function(d) { cb(selectPromotedTradeId(d.positions, drawingId)); })
        .catch(function() { cb(null); });
}

// A hard delete of a promoted spot buy must also remove its journal trade —
// otherwise it stays open forever and /portfolio shows a position the user
// explicitly erased (the same overstated-book bug the promotion path exists
// to fix, reached through Delete instead of Sell). DELETE, not close: the
// user is saying "this was never a real position," not recording an exit —
// closing would fabricate an exit price and P&L for a sale that never
// happened. Mirrors the existing deletePosition() convention, which DELETEs
// (not closes) a 'position' drawing's linked _journalId on hard delete.
//
// `ext` (the drawing's extendData) must be read by the CALLER before the
// drawing is removed from storage — this runs async, and by the time it
// resolves the drawing may already be gone.
function _deletePromotedSpotTrade(symbol, drawingId, ext) {
    var journalDrawingId = ext && ext._journalDrawingId;
    if (!journalDrawingId) return;
    if (typeof _findPromotedTradeId !== 'function') return;
    function logFail(reason, extra) {
        if (typeof window.carracLog !== 'function') return;
        var data = {symbol: symbol, drawingId: drawingId, journalDrawingId: journalDrawingId};
        if (extra) { for (var k in extra) data[k] = extra[k]; }
        window.carracLog('portfolio', reason, data);
    }
    _findPromotedTradeId(journalDrawingId, function(tradeId) {
        if (!tradeId) {
            // Promoted at buy time but no matching open position now — either
            // the /api/positions/live lookup failed, or the trade is already
            // gone some other way. The drawing is about to disappear either
            // way, so surface it rather than risk a silent orphan.
            logFail('promoted spot trade not found for delete');
            return;
        }
        fetch('/api/journal/trades/' + tradeId, {
            method: 'DELETE',
            headers: { 'X-CSRFToken': getCSRFToken() },
            credentials: 'same-origin'
        }).then(function(r) {
            if (!r.ok) throw new Error('delete failed: ' + r.status);
        }).catch(function(e) {
            logFail('promoted spot trade delete failed', {tradeId: tradeId, err: String(e)});
        });
    });
}

// "Clear all drawings for asset" (Shift+Delete / Shift+D) wipes an entire
// symbol's drawings in one shot — without this, every promoted spot trade
// for that symbol orphans at once, silently. Called before the bucket is
// wiped from storage.
function _cleanupPromotedSpotTradesForSymbol(symbol) {
    if (typeof _deletePromotedSpotTrade !== 'function') return;
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        (all[symbol] || []).forEach(function(d) {
            if (d.extendData && d.extendData._journalDrawingId) {
                _deletePromotedSpotTrade(symbol, d.id, d.extendData);
            }
        });
    } catch (e) {}
}

function _renderSpotTree(spotItems, symbolOrder, spotsBySymbol) {
    var multiAsset = symbolOrder.length >= 2;
    var T = 50; // tree column (absorbs indentation)
    var charW = 7.8; // monospace char width at 0.85em
    var colPad = 18; // padding per column

    // --- Pass 1: Pre-compute all cell text, find max widths ---
    var texts = { date: ['DATE'], price: ['PRICE'], cost: ['COST'], qty: ['QTY'], pct: ['P&L %'], usd: ['P&L $'], note: ['NOTE'] };

    symbolOrder.forEach(function(sym) {
        var group = spotsBySymbol[sym];
        var tQ = 0, tU = 0;
        group.forEach(function(i) { var e = i.drawing.extendData || {}; tQ += e.quantity || 0; tU += e.usdAmount || 0; });
        var aAvg = tQ > 0 ? tU / tQ : group[0].drawing.points[0].value;
        var aP = _spotPnl(aAvg, tQ, sym);
        var uS = aP.usd >= 0 ? '+' : '\u2212';
        // Asset row uses columns: date=sym, price=avg, cost=invested, qty=total
        texts.date.push(sym);
        texts.price.push('Avg $' + formatPriceSimple(aAvg));
        texts.cost.push('$' + _fmtSpotUsd(tU));
        texts.qty.push(_fmtSpotQty(tQ) + ' ' + sym);
        texts.pct.push(aP.sign + aP.pct.toFixed(1) + '%');
        texts.usd.push(uS + '$' + _fmtSpotUsd(Math.abs(aP.usd)));
        texts.note.push(group.length + ' buy' + (group.length !== 1 ? 's' : ''));

        group.forEach(function(item) {
            var d = item.drawing, entry = d.points[0].value, ext = d.extendData || {};
            var usd = ext.usdAmount || 0, qty = ext.quantity || 0, label = ext.label || '';
            var p = _spotPnl(entry, qty, sym);
            var pS = p.pct >= 0 ? '+' : '', uuS = p.usd >= 0 ? '+' : '\u2212';
            var dateStr = '';
            if (d.points[0].timestamp) { var dt = new Date(d.points[0].timestamp); dateStr = dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }); }
            texts.date.push(dateStr);
            texts.price.push('$' + formatPriceSimple(entry));
            texts.cost.push(usd > 0 ? '$' + _fmtSpotUsd(usd) : '');
            texts.qty.push(qty > 0 ? _fmtSpotQty(qty) + ' ' + sym : '');
            texts.pct.push(pS + p.pct.toFixed(1) + '%');
            texts.usd.push(Math.abs(p.usd) > 0.5 ? uuS + '$' + _fmtSpotUsd(Math.abs(p.usd)) : '');
            texts.note.push(label);
        });
    });

    // Grand total column values
    if (multiAsset) {
        var gU = 0, gV = 0;
        spotItems.forEach(function(i) { var e = i.drawing.extendData || {}; gU += e.usdAmount || 0; var c = allAssets[i.symbol] ? allAssets[i.symbol].latest_price : 0; gV += (e.quantity || 0) * c; });
        var gP = gU > 0 ? ((gV - gU) / gU * 100) : 0, gD = gV - gU;
        var gs = gP >= 0 ? '+' : '';
        texts.date.push('PORTFOLIO');
        texts.cost.push('$' + _fmtSpotUsd(gU));
        texts.qty.push('$' + _fmtSpotUsd(gV) + ' val');
        texts.pct.push(gs + gP.toFixed(1) + '%');
        texts.usd.push(gs + '$' + _fmtSpotUsd(Math.abs(gD)));
    }

    function maxW(arr, minW) { var m = 0; arr.forEach(function(t) { if (t.length > m) m = t.length; }); return Math.max(Math.ceil(m * charW + colPad), minW || 40); }
    var C = {
        date: maxW(texts.date, 50),
        price: maxW(texts.price, 50),
        cost: maxW(texts.cost, 50),
        qty: maxW(texts.qty, 50),
        pct: maxW(texts.pct, 50),
        usd: maxW(texts.usd, 50),
        note: maxW(texts.note, 40),
        act: 72
    };

    // --- Pass 2: Render ---
    function col(w, color, weight, align, text) { return '<span style="width:' + w + 'px;flex-shrink:0;color:' + color + (weight ? ';font-weight:' + weight : '') + (align ? ';text-align:' + align : '') + '">' + text + '</span>'; }
    function treecol(indent, color, text) { return '<span style="width:' + T + 'px;flex-shrink:0;padding-left:' + indent + 'px;color:' + color + ';font-size:1.1em">' + text + '</span>'; }
    function rightCols(pctColor, pctW, pctText, usdColor, usdText, noteText, actHtml) {
        return col(C.pct, pctColor, pctW, 'right', pctText) +
            col(C.usd, usdColor, '', 'right', usdText) +
            '<span style="width:' + C.note + 'px;flex-shrink:0;color:#9198a1;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"' + (noteText ? ' title="' + escapeHtml(noteText) + '"' : '') + '>' + escapeHtml(noteText || '') + '</span>' +
            '<span style="width:' + C.act + 'px;flex-shrink:0;display:flex;gap:6px;justify-content:flex-end;align-items:center">' + (actHtml || '') + '</span>';
    }

    var html = '';

    // Header
    html += '<div class="scanner-row" style="padding-top:4px;padding-bottom:4px;font-size:0.75em;border-bottom:1px solid rgba(255,255,255,0.06)">' +
        '<span style="width:' + T + 'px;flex-shrink:0"></span>' +
        col(C.date, '#4a4e59', '', '', 'DATE') +
        col(C.price, '#4a4e59', '', 'right', 'PRICE') +
        col(C.cost, '#4a4e59', '', 'right', 'COST') +
        col(C.qty, '#4a4e59', '', 'right', 'QTY') +
        col(C.pct, '#4a4e59', '', 'right', 'P&L %') +
        col(C.usd, '#4a4e59', '', 'right', 'P&L $') +
        col(C.note, '#4a4e59', '', 'right', 'NOTE') +
        '<span style="width:' + C.act + 'px;flex-shrink:0"></span>' +
    '</div>';

    // Grand total — values in columns
    if (multiAsset) {
        var gU2 = 0, gV2 = 0;
        spotItems.forEach(function(i) { var e = i.drawing.extendData || {}; gU2 += e.usdAmount || 0; var c = allAssets[i.symbol] ? allAssets[i.symbol].latest_price : 0; gV2 += (e.quantity || 0) * c; });
        var gP2 = gU2 > 0 ? ((gV2 - gU2) / gU2 * 100) : 0, gD2 = gV2 - gU2;
        var gc2 = Math.abs(gP2) < 0.05 ? '#787b86' : (gP2 >= 0 ? '#26a69a' : '#ef5350');
        var gs2 = gP2 >= 0 ? '+' : '';
        var gCol = _spotTreeCollapsed['grand'];
        html += '<div class="scanner-row" style="cursor:pointer;background:rgba(77,182,172,0.04)" data-action="spot-toggle-tree" data-tree-key="grand">' +
            treecol(0, '#4db6ac', gCol ? '\u25B8' : '\u25BE') +
            col(C.date, '#4db6ac', '700', '', 'PORTFOLIO') +
            col(C.price, '#9198a1', '', 'right', '') +
            col(C.cost, '#9198a1', '', 'right', '$' + _fmtSpotUsd(gU2)) +
            col(C.qty, '#9198a1', '', 'right', '$' + _fmtSpotUsd(gV2) + ' val') +
                rightCols(gc2, '600', gs2 + gP2.toFixed(1) + '%', gc2, gs2 + '$' + _fmtSpotUsd(Math.abs(gD2)), '', '') +
        '</div>';
    }

    // Per-asset groups
    var aTreeIndent = multiAsset ? 16 : 0;
    symbolOrder.forEach(function(sym) {
        var group = spotsBySymbol[sym];
        if (multiAsset && _spotTreeCollapsed['grand']) return;
        var tQ = 0, tU = 0;
        group.forEach(function(i) { var e = i.drawing.extendData || {}; tQ += e.quantity || 0; tU += e.usdAmount || 0; });
        var aAvg = tQ > 0 ? tU / tQ : group[0].drawing.points[0].value;
        var aP = _spotPnl(aAvg, tQ, sym);
        var aCol = _spotTreeCollapsed['asset_' + sym];
        var aVal = tQ * aP.cp;
        var uS = aP.usd >= 0 ? '+' : '\u2212';

        html += '<div class="scanner-row" style="cursor:pointer" data-action="spot-toggle-tree" data-tree-key="asset_' + escapeHtml(sym) + '">' +
            treecol(aTreeIndent, aP.color, aCol ? '\u25B8' : '\u25BE') +
            col(C.date, '#f0f6fc', '600', '', escapeHtml(sym)) +
            col(C.price, '#9198a1', '', 'right', 'Avg $' + formatPriceSimple(aAvg)) +
            col(C.cost, '#9198a1', '', 'right', '$' + _fmtSpotUsd(tU)) +
            col(C.qty, '#d1d4dc', '600', 'right', _fmtSpotQty(tQ) + ' ' + escapeHtml(sym)) +
                rightCols(aP.color, '600', aP.sign + aP.pct.toFixed(1) + '%', aP.color, uS + '$' + _fmtSpotUsd(Math.abs(aP.usd)), group.length + ' buy' + (group.length !== 1 ? 's' : ''), '') +
        '</div>';

        if (!aCol) {
            var lTreeIndent = multiAsset ? 32 : 16;
            group.forEach(function(item, idx) {
                var d = item.drawing, entry = d.points[0].value, ext = d.extendData || {};
                var usd = ext.usdAmount || 0, qty = ext.quantity || 0, label = ext.label || '';
                var isLast = idx === group.length - 1;
                var p = _spotPnl(entry, qty, sym);
                var conn = isLast ? '\u2514' : '\u251C';
                var pS = p.pct >= 0 ? '+' : '', uuS = p.usd >= 0 ? '+' : '\u2212';
                var dateStr = '';
                if (d.points[0].timestamp) { var dt = new Date(d.points[0].timestamp); dateStr = dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }); }
                var eSym = escapeHtml(sym), eId = escapeHtml(d.id || '');
                var bg = p.pct >= 0 ? 'rgba(38,166,154,0.05)' : 'rgba(239,83,80,0.05)';
                var qtyLabel = qty > 0 ? _fmtSpotQty(qty) + ' ' + escapeHtml(sym) : '';
                // symbol/drawingId are read back from the enclosing .scanner-row's own
                // data-symbol/data-drawing-id (set on that div below) by the delegated
                // spot-sell/spot-edit/spot-delete handlers.
                var actBtns = '<span data-action="spot-sell" style="cursor:pointer;color:#ef5350;font-size:10px;font-weight:600;padding:1px 4px;border:1px solid #ef5350;border-radius:2px" title="Sell">SELL</span> ' +
                    '<span data-action="spot-edit" style="cursor:pointer;color:#4a4e59" title="Edit">\u270E</span>' +
                    '<span data-action="spot-delete" style="cursor:pointer;color:#4a4e59" title="Remove">\u00d7</span>';

                html += '<div class="scanner-row" style="background:' + bg + '" data-symbol="' + eSym + '" data-drawing-id="' + eId + '">' +
                    treecol(lTreeIndent, '#4a4e59', conn) +
                    col(C.date, '#9198a1', '', '', dateStr) +
                    col(C.price, '#d1d4dc', '', 'right', '$' + formatPriceSimple(entry)) +
                    col(C.cost, '#787b86', '', 'right', usd > 0 ? '$' + _fmtSpotUsd(usd) : '') +
                    col(C.qty, '#9198a1', '', 'right', qtyLabel) +
                    rightCols(p.color, '600', pS + p.pct.toFixed(1) + '%', p.color, Math.abs(p.usd) > 0.5 ? uuS + '$' + _fmtSpotUsd(Math.abs(p.usd)) : '', label, actBtns) +
                '</div>';
            });
        }
    });

    return html;
}

function sellSpotFromDrawer(symbol, drawingId) {
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawing = (allDrawings[symbol] || []).find(function(d) { return d.id === drawingId; });
        if (!drawing) return;
        if (typeof showSpotSellDialog === 'function') {
            showSpotSellDialog(drawingId, drawing, function() {
                updateDrawerPosition();
            });
        }
    } catch(e) {}
}

function editSpotBuy(symbol, drawingId) {
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawing = (allDrawings[symbol] || []).find(function(d) { return d.id === drawingId; });
        if (!drawing) return;
        var ext = drawing.extendData || {};
        var entryPrice = drawing.points[0].value;

        showSpotBuyDialog(entryPrice, function(price, usd, qty, label) {
            if (price === null) return;
            var newExt = { usdAmount: usd, quantity: qty, label: label };
            for (var k in ext) { if (!(k in newExt)) newExt[k] = ext[k]; }
            drawing.extendData = newExt;
            localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
            if (symbol === klineChartSymbol && typeof klineChart !== 'undefined' && klineChart) {
                try { klineChart.overrideOverlay({ id: drawingId, extendData: newExt }); } catch(e) {}
            }
            updateDrawerPosition();
            if (typeof scheduleSyncToBackend === 'function') scheduleSyncToBackend();
        }, ext);
    } catch(e) {}
}

function deleteSpot(symbol, drawingId) {
    var currentPrice = allAssets[symbol] ? allAssets[symbol].latest_price : 0;
    var existing = document.getElementById('spot-delete-popup');
    if (existing) existing.remove();

    var popup = document.createElement('div');
    popup.id = 'spot-delete-popup';
    popup.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:280px;background:#1e222d;border:1px solid #4db6ac;border-radius:8px;z-index:26003;padding:16px;box-shadow:0 0 20px rgba(0,0,0,0.6);font-family:JetBrains Mono,monospace;text-align:center';
    popup.innerHTML =
        '<div style="color:#d1d4dc;font-size:13px;margin-bottom:14px">Remove spot buy?</div>' +
        '<div style="display:flex;gap:8px;justify-content:center">' +
            '<button id="spot-delete-hard" style="background:#ef5350;border:none;border-radius:4px;padding:6px 12px;color:#fff;cursor:pointer;font-size:11px;font-family:inherit">Delete</button>' +
            '<button id="spot-mark-sold" style="background:#4db6ac;border:none;border-radius:4px;padding:6px 12px;color:#131722;cursor:pointer;font-weight:600;font-size:11px;font-family:inherit">Mark as Sold' + (currentPrice > 0 ? ' @ $' + formatPriceSimple(currentPrice) : '') + '</button>' +
            '<button id="spot-delete-cancel" style="background:#2a2e39;border:1px solid #363a45;border-radius:4px;padding:6px 12px;color:#787b86;cursor:pointer;font-size:11px;font-family:inherit">Cancel</button>' +
        '</div>';
    document.body.appendChild(popup);

    document.getElementById('spot-delete-hard').addEventListener('click', function() {
        popup.remove();
        // Read extendData BEFORE the drawing is removed — the trade lookup
        // below is async, so by the time it resolves the drawing is gone.
        var _ext = null;
        try {
            var _all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            var _d = (_all[symbol] || []).find(function(d) { return d.id === drawingId; });
            _ext = _d && _d.extendData;
        } catch(e) {}
        if (typeof _deletePromotedSpotTrade === 'function') _deletePromotedSpotTrade(symbol, drawingId, _ext);
        _removePositionDrawing(symbol, drawingId);
        updateDrawerPosition();
    });

    document.getElementById('spot-mark-sold').addEventListener('click', function() {
        popup.remove();
        // Use sell dialog if available (with journal integration + P&L)
        if (typeof showSpotSellDialog === 'function') {
            try {
                var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
                var drawings = allDrawings[symbol] || [];
                var drawing = drawings.find(function(d) { return d.id === drawingId; });
                if (drawing) {
                    // Temporarily switch to this symbol's chart context if needed
                    showSpotSellDialog(drawingId, drawing, function() {
                        updateDrawerPosition();
                    });
                    return;
                }
            } catch(e) {}
        }
        // Fallback: instant mark (if dialog unavailable)
        try {
            var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            var drawings = allDrawings[symbol] || [];
            var drawing = drawings.find(function(d) { return d.id === drawingId; });
            if (drawing) {
                if (!drawing.extendData) drawing.extendData = {};
                drawing.extendData.sold = true;
                drawing.extendData.soldPrice = currentPrice;
                drawing.extendData.soldAt = Date.now();
                localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
            }
        } catch(e) {}
        if (symbol === klineChartSymbol && klineChart) {
            try { klineChart.removeOverlay(drawingId); } catch(e) {}
        }
        updateDrawerPosition();
        if (typeof scheduleSyncToBackend === 'function') scheduleSyncToBackend();
    });

    document.getElementById('spot-delete-cancel').addEventListener('click', function() { popup.remove(); });
    popup.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') { popup.remove(); }
        e.stopPropagation();
    });
}

// CSP hardening — every onclick= action in the position tab (mode
// toggle, spot-tree rows, spot sell/edit/delete, position-card actions) is routed
// through this one delegated listener, bound EXACTLY ONCE on the section (guarded
// via a dataset flag) so it survives every full re-render (sectionEl.innerHTML is
// reassigned wholesale by updateDrawerPosition below).
function _wirePositionDelegatedEvents(sectionEl) {
    if (sectionEl.dataset.positionActionsWired) return;
    sectionEl.dataset.positionActionsWired = '1';
    sectionEl.addEventListener('click', function(e) {
        var el = e.target.closest('[data-action]');
        if (!el || !sectionEl.contains(el)) return;
        var action = el.getAttribute('data-action');
        switch (action) {
            case 'drawer-toggle-show-all':
                _drawerToggleShowAll(el.getAttribute('data-tab'));
                break;
            case 'pos-set-mode':
                _togglePositionDrawerMode(el.getAttribute('data-mode'));
                break;
            case 'spot-toggle-tree':
                _toggleSpotTree(el.getAttribute('data-tree-key'));
                break;
            case 'spot-sell': {
                e.stopPropagation();
                var r1 = el.closest('.scanner-row');
                if (r1) sellSpotFromDrawer(r1.getAttribute('data-symbol'), r1.getAttribute('data-drawing-id'));
                break;
            }
            case 'spot-edit': {
                e.stopPropagation();
                var r2 = el.closest('.scanner-row');
                if (r2) editSpotBuy(r2.getAttribute('data-symbol'), r2.getAttribute('data-drawing-id'));
                break;
            }
            case 'spot-delete': {
                e.stopPropagation();
                var r3 = el.closest('.scanner-row');
                if (r3) deleteSpot(r3.getAttribute('data-symbol'), r3.getAttribute('data-drawing-id'));
                break;
            }
            case 'pos-validate': {
                e.stopPropagation();
                var c1 = el.closest('.pos-card');
                if (c1) evaluatePosition(c1.getAttribute('data-symbol'), c1.getAttribute('data-drawing-id'));
                break;
            }
            case 'pos-close-log': {
                e.stopPropagation();
                var c2 = el.closest('.pos-card');
                if (c2) closePositionAndLog(c2.getAttribute('data-symbol'), c2.getAttribute('data-drawing-id'));
                break;
            }
            case 'pos-clear': {
                e.stopPropagation();
                var c3 = el.closest('.pos-card');
                if (c3) closePositionQuiet(c3.getAttribute('data-symbol'), c3.getAttribute('data-drawing-id'));
                break;
            }
            case 'pos-delete': {
                e.stopPropagation();
                var c4 = el.closest('.pos-card');
                if (c4) deletePosition(c4.getAttribute('data-symbol'), c4.getAttribute('data-drawing-id'));
                break;
            }
        }
    });
}

function updateDrawerPosition() {
    var summaryEl = document.getElementById('drawer-position-summary');
    var sectionEl = document.getElementById('drawer-section-position');
    var tabEl = document.getElementById('drawer-tab-position');
    if (!sectionEl) return;
    _wirePositionDelegatedEvents(sectionEl);

    var showAll = _drawerShowAll.position;
    var allDrawingsStore = {};
    try { allDrawingsStore = JSON.parse(localStorage.getItem('klineDrawings') || '{}'); } catch(e) {}

    // Collect all positions and spots
    var posItems = [];
    var spotItems = [];
    if (showAll) {
        var symbols = Object.keys(allDrawingsStore);
        symbols.forEach(function(sym) {
            _findAllPositionDrawings(allDrawingsStore[sym] || []).forEach(function(pos) {
                posItems.push({ symbol: sym, drawing: pos });
            });
            _findAllSpotDrawings(allDrawingsStore[sym] || []).forEach(function(spot) {
                spotItems.push({ symbol: sym, drawing: spot });
            });
        });
    } else {
        _findAllPositionDrawings(allDrawingsStore[klineChartSymbol] || []).forEach(function(pos) {
            posItems.push({ symbol: klineChartSymbol, drawing: pos });
        });
        _findAllSpotDrawings(allDrawingsStore[klineChartSymbol] || []).forEach(function(spot) {
            spotItems.push({ symbol: klineChartSymbol, drawing: spot });
        });
    }

    function sortItems(a, b) {
        if (a.symbol === klineChartSymbol && b.symbol !== klineChartSymbol) return -1;
        if (b.symbol === klineChartSymbol && a.symbol !== klineChartSymbol) return 1;
        return a.symbol.localeCompare(b.symbol);
    }
    posItems.sort(sortItems);
    // Sort spots by current value (quantity * price) descending
    spotItems.sort(function(a, b) {
        var aExt = a.drawing.extendData || {};
        var bExt = b.drawing.extendData || {};
        var aPrice = allAssets[a.symbol] ? allAssets[a.symbol].latest_price : 0;
        var bPrice = allAssets[b.symbol] ? allAssets[b.symbol].latest_price : 0;
        var aVal = (aExt.quantity || 0) * aPrice;
        var bVal = (bExt.quantity || 0) * bPrice;
        return bVal - aVal;
    });

    var posCount = posItems.length;
    var spotCount = spotItems.length;
    var mode = _positionDrawerMode;

    // Update tab label — color-coded with arrows based on P&L
    // Compute overall spot P&L
    var spotTotalUsd = 0, spotTotalVal = 0;
    spotItems.forEach(function(i) { var e = i.drawing.extendData || {}; spotTotalUsd += e.usdAmount || 0; var c = allAssets[i.symbol] ? allAssets[i.symbol].latest_price : 0; spotTotalVal += (e.quantity || 0) * c; });
    var spotUp = spotTotalVal >= spotTotalUsd;
    // Compute overall futures P&L
    var futuresUp = true;
    if (posCount > 0) {
        var fPnl = 0;
        posItems.forEach(function(p) {
            var pts = p.drawing.points; var entry = pts[0].value; var sl = pts[1].value;
            var isLong = sl < entry; var cp = allAssets[p.symbol] ? allAssets[p.symbol].latest_price : 0;
            if (cp > 0) fPnl += isLong ? (cp - entry) : (entry - cp);
        });
        futuresUp = fPnl >= 0;
    }
    if (summaryEl) {
        if (mode === 'both') {
            var fArrow = futuresUp ? '\u25B2' : '\u25BC'; var fColor = futuresUp ? '#26a69a' : '#ef5350';
            var sArrow = spotUp ? '\u25B2' : '\u25BC'; var sColor = spotUp ? '#26a69a' : '#ef5350';
            summaryEl.innerHTML = '<span style="color:' + fColor + '">' + fArrow + '</span>' + posCount + 'F \u00b7 <span style="color:' + sColor + '">' + sArrow + '</span>' + spotCount + 'S';
        } else if (mode === 'spots') {
            var sa = spotUp ? '\u25B2' : '\u25BC'; var sc = spotUp ? '#26a69a' : '#ef5350';
            summaryEl.innerHTML = spotCount > 0 ? '<span style="color:' + sc + '">' + sa + '</span> ' + spotCount + ' Spot' + (spotCount !== 1 ? 's' : '') : 'Spots';
        } else {
            var fa = futuresUp ? '\u25B2' : '\u25BC'; var fc = futuresUp ? '#26a69a' : '#ef5350';
            summaryEl.innerHTML = posCount > 0 ? '<span style="color:' + fc + '">' + fa + '</span> ' + posCount + ' Position' + (posCount !== 1 ? 's' : '') : 'Positions';
        }
    }
    if (tabEl) {
        var hasLong = posItems.some(function(p) { return p.drawing.points[1].value < p.drawing.points[0].value; });
        var hasShort = posItems.some(function(p) { return p.drawing.points[1].value >= p.drawing.points[0].value; });
        tabEl.classList.toggle('has-long', mode === 'futures' && hasLong);
        tabEl.classList.toggle('has-short', mode === 'futures' && hasShort);
        tabEl.classList.toggle('has-spot', mode === 'spots' && spotCount > 0);
    }

    // Mode toggle buttons
    function _modeBtn(m, label, count) {
        var active = mode === m;
        var c = m === 'spots' ? '#4db6ac' : '#d1d4dc';
        return '<button data-action="pos-set-mode" data-mode="' + m + '" style="border:none;cursor:pointer;padding:3px 8px;' +
            (active ? 'background:#2a2e39;color:' + c + ';font-weight:600' : 'background:transparent;color:#4a4e59') + '">' + label + ' (' + count + ')</button>';
    }
    var modeToggleHtml = '<div style="display:inline-flex;gap:2px;border:1px solid #2a2e39;border-radius:4px;overflow:hidden;font-size:10px;font-family:\'JetBrains Mono\',monospace;margin-right:auto">' +
        _modeBtn('futures', 'Futures', posCount) +
        _modeBtn('spots', 'Spots', spotCount) +
        _modeBtn('both', 'Both', posCount + spotCount) +
    '</div>';

    var headerHtml = '<div class="drawer-list-header">' + modeToggleHtml + _drawerShowAllBtn('position') + '</div>';
    var listHtml = '';

    // Helper: build spots HTML
    function _buildSpotsHtml() {
        if (spotCount === 0) return '<div class="drawer-empty">No spot buys' + (showAll ? ' across assets' : ' for ' + escapeHtml(klineChartSymbol)) + '</div>';
        var spotsBySymbol = {};
        spotItems.forEach(function(item) {
            if (!spotsBySymbol[item.symbol]) spotsBySymbol[item.symbol] = [];
            spotsBySymbol[item.symbol].push(item);
        });
        // Sort each group's buys by value descending
        Object.keys(spotsBySymbol).forEach(function(sym) {
            var cp = allAssets[sym] ? allAssets[sym].latest_price : 0;
            spotsBySymbol[sym].sort(function(a, b) {
                return ((b.drawing.extendData || {}).quantity || 0) * cp - ((a.drawing.extendData || {}).quantity || 0) * cp;
            });
        });
        // Sort symbol groups by total current value descending
        var symbolOrder = Object.keys(spotsBySymbol);
        symbolOrder.sort(function(a, b) {
            var aP = allAssets[a] ? allAssets[a].latest_price : 0;
            var bP = allAssets[b] ? allAssets[b].latest_price : 0;
            var aVal = spotsBySymbol[a].reduce(function(s, i) { return s + ((i.drawing.extendData || {}).quantity || 0) * aP; }, 0);
            var bVal = spotsBySymbol[b].reduce(function(s, i) { return s + ((i.drawing.extendData || {}).quantity || 0) * bP; }, 0);
            return bVal - aVal;
        });
        return _renderSpotTree(spotItems, symbolOrder, spotsBySymbol);
    }
    // Helper: build futures HTML
    function _buildFuturesHtml() {
        if (posCount === 0) return '<div class="drawer-empty">No position' + (showAll ? 's across assets' : ' for ' + escapeHtml(klineChartSymbol)) + '</div>';
        var h = '';
        posItems.forEach(function(item) {
            try { h += _renderSinglePosition(item.drawing, item.symbol).html; } catch(e) { console.error('Error rendering position:', item.symbol, e); }
        });
        return h;
    }

    if (mode === 'futures') {
        listHtml = _buildFuturesHtml();
    } else if (mode === 'spots') {
        listHtml = _buildSpotsHtml();
    } else {
        // Both: side-by-side, responsive
        var spotsHtml = _buildSpotsHtml();
        var futuresHtml = _buildFuturesHtml();
        var fFlex = posCount <= 2 ? 'flex:0 0 auto;min-width:280px;max-width:40%' : 'flex:1;min-width:340px';
        listHtml = '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">' +
            '<div style="display:flex;gap:12px;min-width:700px">' +
                '<div style="flex:2;min-width:400px;overflow-x:auto">' +
                    '<div style="color:#4db6ac;font-weight:600;font-size:0.75em;padding:4px 12px;border-bottom:1px solid rgba(255,255,255,0.06);font-family:\'JetBrains Mono\',monospace">SPOTS</div>' +
                    spotsHtml +
                '</div>' +
                '<div style="' + fFlex + '">' +
                    '<div style="color:#d1d4dc;font-weight:600;font-size:0.75em;padding:4px 12px;border-bottom:1px solid rgba(255,255,255,0.06);font-family:\'JetBrains Mono\',monospace">FUTURES</div>' +
                    '<div class="pos-card-grid" style="grid-template-columns:1fr">' + futuresHtml + '</div>' +
                '</div>' +
            '</div>' +
        '</div>';
    }

    var useGrid = mode === 'futures';
    sectionEl.innerHTML = '<div class="drawer-position-scroll">' + headerHtml + (useGrid ? '<div class="pos-card-grid">' + listHtml + '</div>' : listHtml) + '</div>';

    // Click-to-navigate on position cards (all grids, including "both" mode).
    // Must ignore clicks inside .pos-card-actions: this listener sits closer to the
    // click target than the section-level delegated listener above, so it fires
    // FIRST during bubbling — the action buttons' own e.stopPropagation() can't
    // reach back down to stop it. This guard restores the exclusion inline onclick
    // attributes got for free, now that the buttons are delegated.
    sectionEl.querySelectorAll('.pos-card-grid').forEach(function(grid) {
        grid.addEventListener('click', function(e) {
            if (e.target.closest('.pos-card-actions')) return;
            var card = e.target.closest('.pos-card');
            if (!card) return;
            var sym = card.getAttribute('data-symbol');
            var drawingId = card.getAttribute('data-drawing-id');
            if (sym) navigateToPosition(sym, drawingId);
        });
    });
}

// Remove a position drawing from any symbol's storage
function _removePositionDrawing(symbol, drawingId) {
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        if (allDrawings[symbol]) {
            allDrawings[symbol] = allDrawings[symbol].filter(function(d) { return d.id !== drawingId; });
            localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
        }
        // Delete from server too
        fetch('/api/drawings/by-local-id', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            credentials: 'same-origin',
            body: JSON.stringify({ asset: symbol, local_id: drawingId })
        }).then(function(r) {
            if (!r.ok) console.warn('[Position] Server delete failed:', r.status);
        }).catch(function(e) { console.warn('[Position] Server delete error:', e); });
        // Remove overlay from chart if on current symbol
        if (symbol === klineChartSymbol && klineChart) {
            try { klineChart.removeOverlay(drawingId); } catch(e) {}
        }
    } catch(e) {}
}

function closePositionAndLog(symbol, drawingId) {
    // Find the drawing
    var all, drawing;
    try {
        all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        drawing = (all[symbol] || []).find(function(d) { return d.id === drawingId; });
    } catch(e) {}
    if (!drawing || !drawing.points || drawing.points.length < 3) {
        showToast('Position drawing not found');
        return;
    }

    // If no journal entry, auto-capture first then open modal
    if (!drawing.extendData || !drawing.extendData._journalId) {
        if (typeof _autoCaptureTrade === 'function') {
            _autoCaptureTrade(drawing, symbol);
            // Wait for auto-capture to complete and store journal ID
            setTimeout(function() {
                // Re-read drawing to get updated _journalId
                try {
                    all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
                    drawing = (all[symbol] || []).find(function(d) { return d.id === drawingId; });
                } catch(e2) {}
                _openInlineCloseModal(symbol, drawingId, drawing);
            }, 500);
        } else {
            _openInlineCloseModal(symbol, drawingId, drawing);
        }
        return;
    }

    _openInlineCloseModal(symbol, drawingId, drawing);
}

function _openInlineCloseModal(symbol, drawingId, drawing) {
    var pts = drawing.points;
    var entry = pts[0].value;
    var sl = pts[1].value;
    var tp = pts[2].value;
    var isLong = (drawing.name === 'longPosition') || (drawing.name !== 'shortPosition' && sl < entry);
    var currentPrice = allAssets[symbol] ? allAssets[symbol].latest_price : 0;
    var journalId = (drawing.extendData && drawing.extendData._journalId) || null;

    var dirLabel = isLong ? 'LONG' : 'SHORT';
    var dirClass = isLong ? 'long' : 'short';

    // Remove existing modal if any
    var existing = document.getElementById('pos-close-modal');
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = 'pos-close-modal';
    overlay.className = 'pos-close-overlay';
    overlay.innerHTML =
        '<div class="pos-close-dialog">' +
            '<h2>Close Trade</h2>' +
            '<div class="pos-close-context">' +
                '<span class="pos-close-dir ' + dirClass + '">' + dirLabel + '</span> ' +
                escapeHtml(symbol) + ' @ ' + formatPriceSimple(entry) +
                ' · SL ' + formatPriceSimple(sl) +
                ' · TP ' + formatPriceSimple(tp) +
            '</div>' +
            '<div class="pos-close-row">' +
                '<div class="pos-close-field">' +
                    '<label class="pos-close-label">Exit Price</label>' +
                    '<input type="number" id="pos-close-exit" class="pos-close-input" step="any" min="0" value="' + (currentPrice || '') + '">' +
                '</div>' +
                '<div class="pos-close-field">' +
                    '<label class="pos-close-label">Exit Reason</label>' +
                    '<select id="pos-close-reason" class="pos-close-input">' +
                        '<option value="manual">Manual</option>' +
                        '<option value="tp_hit">TP Hit</option>' +
                        '<option value="sl_hit">SL Hit</option>' +
                        '<option value="trailing_stop">Trailing Stop</option>' +
                        '<option value="time_based">Time-Based</option>' +
                        '<option value="breakeven">Breakeven</option>' +
                    '</select>' +
                '</div>' +
            '</div>' +
            '<div class="pos-close-field">' +
                '<label class="pos-close-label">Notes</label>' +
                '<textarea id="pos-close-notes" class="pos-close-input" placeholder="Post-trade notes..." maxlength="5000"></textarea>' +
            '</div>' +
            '<div class="pos-close-actions">' +
                '<button class="pos-close-btn pos-close-btn-cancel" id="pos-close-cancel">Cancel</button>' +
                '<button class="pos-close-btn pos-close-btn-submit" id="pos-close-submit">Close Trade</button>' +
            '</div>' +
        '</div>';

    document.body.appendChild(overlay);
    // Show with animation
    requestAnimationFrame(function() { overlay.classList.add('active'); });

    // Focus exit price
    setTimeout(function() { document.getElementById('pos-close-exit').select(); }, 100);

    // Cancel
    document.getElementById('pos-close-cancel').addEventListener('click', function() {
        overlay.classList.remove('active');
        setTimeout(function() { overlay.remove(); }, 200);
    });
    // Click outside to cancel
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) {
            overlay.classList.remove('active');
            setTimeout(function() { overlay.remove(); }, 200);
        }
    });
    // Escape to cancel
    var escHandler = function(e) {
        if (e.key === 'Escape') {
            overlay.classList.remove('active');
            setTimeout(function() { overlay.remove(); }, 200);
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);

    // Submit
    document.getElementById('pos-close-submit').addEventListener('click', function() {
        var exitPrice = parseFloat(document.getElementById('pos-close-exit').value);
        if (!exitPrice || exitPrice <= 0) {
            showToast('Enter a valid exit price');
            return;
        }
        var reason = document.getElementById('pos-close-reason').value;
        var notes = document.getElementById('pos-close-notes').value.trim() || null;

        var submitBtn = document.getElementById('pos-close-submit');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Closing...';

        if (journalId) {
            // Close existing journal entry
            var csrf = document.querySelector('meta[name="csrf-token"]');
            var headers = { 'Content-Type': 'application/json' };
            if (csrf) headers['X-CSRFToken'] = csrf.content;

            fetch('/api/journal/trades/' + journalId + '/close', {
                method: 'POST',
                headers: headers,
                credentials: 'same-origin',
                body: JSON.stringify({ exit_price: exitPrice, exit_reason: reason, notes: notes })
            }).then(function(r) {
                if (!r.ok) return r.json().then(function(d) { throw new Error(d.error || 'Failed'); });
                return r.json();
            }).then(function() {
                // Remove position drawing
                _removePositionDrawing(symbol, drawingId);
                updateDrawerPosition();
                overlay.classList.remove('active');
                setTimeout(function() { overlay.remove(); }, 200);
                showToast('Trade closed and logged');
            }).catch(function(err) {
                showToast('Error: ' + err.message);
                submitBtn.disabled = false;
                submitBtn.textContent = 'Close Trade';
            });
        } else {
            // No journal entry — just remove drawing
            _removePositionDrawing(symbol, drawingId);
            updateDrawerPosition();
            overlay.classList.remove('active');
            setTimeout(function() { overlay.remove(); }, 200);
            showToast('Position closed');
        }
    });
}

function closePositionQuiet(symbol, drawingId) {
    _removePositionDrawing(symbol, drawingId);
    updateDrawerPosition();
    if (typeof showToast === 'function') showToast('Position cleared');
}

function deletePosition(symbol, drawingId) {
    if (!confirm('This will delete the position from the chart and from the Journal. Continue?')) return;
    // Delete journal entry if linked
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawing = (all[symbol] || []).find(function(d) { return d.id === drawingId; });
        if (drawing && drawing.extendData && drawing.extendData._journalId) {
            fetch('/api/journal/trades/' + drawing.extendData._journalId, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': getCSRFToken() },
                credentials: 'same-origin'
            }).catch(function(e) { console.warn('[Position] Journal delete error:', e); });
        }
    } catch(e) {}
    _removePositionDrawing(symbol, drawingId);
    updateDrawerPosition();
    if (typeof showToast === 'function') showToast('Position deleted');
}

function evaluatePosition(symbol, drawingId) {
    // Find the drawing
    var drawing;
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        drawing = (all[symbol] || []).find(function(d) { return d.id === drawingId; });
    } catch(e) {}
    if (!drawing || !drawing.points || drawing.points.length < 3) {
        showToast('Position not found');
        return;
    }

    var prompt = 'Validate my current trade setup.';

    // Close drawer, switch to AI panel, send prompt
    if (drawerFullscreen) toggleDrawerFullscreen();
    closeDrawer();

    // Switch to AI view mode
    if (typeof toggleAIPanel === 'function') toggleAIPanel(true);

    // Navigate to the position's symbol if needed
    if (symbol !== klineChartSymbol) {
        selectAssetFromSearch(symbol);
    }

    // Send after panel is ready
    setTimeout(function() {
        if (typeof _aiPanelDoSend === 'function') {
            _aiPanelDoSend(prompt, 'validate');
        } else {
            // Fallback: populate textarea
            var ta = document.getElementById('ai-panel-textarea');
            if (ta) { ta.value = prompt; ta.focus(); }
        }
    }, symbol !== klineChartSymbol ? 600 : 200);
}

function navigateToPosition(symbol, drawingId) {
    // Close fullscreen drawer so the chart is visible
    if (drawerFullscreen) toggleDrawerFullscreen();
    // Close drawer entirely for clean view
    closeDrawer();

    function scrollToDrawing() {
        if (!klineChart) return;
        try {
            var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            var drawings = all[symbol] || [];
            var drawing = null;
            for (var i = 0; i < drawings.length; i++) {
                if (drawingId && drawings[i].id === drawingId) { drawing = drawings[i]; break; }
                // Fallback: find first position drawing
                var n = drawings[i].name;
                if (!drawingId && (n === 'position' || n === 'longPosition' || n === 'shortPosition')) { drawing = drawings[i]; break; }
            }
            if (!drawing || !drawing.points || !drawing.points.length) return;

            var entryTs = drawing.points[0].timestamp;
            var dataList = klineChart.getDataList();
            if (!dataList || !dataList.length) return;

            // Find closest data index to entry timestamp
            var bestIdx = 0, bestDist = Infinity;
            for (var j = 0; j < dataList.length; j++) {
                var dist = Math.abs(dataList[j].timestamp - entryTs);
                if (dist < bestDist) { bestDist = dist; bestIdx = j; }
            }

            // scrollToDataIndex puts index at right edge, then shift left to center
            klineChart.scrollToDataIndex(bestIdx);
            var chartEl = document.getElementById('kline-chart-container');
            var shiftWidth = chartEl ? Math.floor(chartEl.clientWidth * 0.6) : 400;
            klineChart.scrollByDistance(-shiftWidth);
        } catch (e) {
        }
    }

    if (symbol !== klineChartSymbol) {
        // Navigate to different asset, then scroll after chart loads
        selectAssetFromSearch(symbol);
        // Chart load is async — wait for it to finish
        setTimeout(scrollToDrawing, 800);
    } else {
        scrollToDrawing();
    }
}

function updateDrawerAll() {
    updateDrawerAlerts();
    updateDrawerPosition();
}

// ======== Drawer Data Age Badge ========
var _drawerDataAgeISO = null;
var _drawerDataAgeTimer = null;

function _updateDrawerDataAge(isoTimestamp) {
    if (!isoTimestamp) { _renderDrawerDataAge(); return; }
    // Only accept if newer than current (don't let stale scanner overwrite fresh asset data)
    if (!_drawerDataAgeISO || new Date(isoTimestamp) > new Date(_drawerDataAgeISO)) {
        _drawerDataAgeISO = isoTimestamp;
    }
    _renderDrawerDataAge();
}

// Accept cache age in seconds (from /api/assets _cache_age)
function _updateDrawerDataAgeFromCacheAge(ageSeconds) {
    var ts = new Date(Date.now() - ageSeconds * 1000).toISOString();
    if (!_drawerDataAgeISO || new Date(ts) > new Date(_drawerDataAgeISO)) {
        _drawerDataAgeISO = ts;
    }
    _renderDrawerDataAge();
}

function _renderDrawerDataAge() {
    var el = document.getElementById('drawer-data-age');
    if (!_drawerDataAgeISO) return;
    if (!el) {
        // Element not in DOM yet — retry once after DOM settles
        setTimeout(_renderDrawerDataAge, 2000);
        return;
    }
    var ago = Math.round((Date.now() - new Date(_drawerDataAgeISO).getTime()) / 60000);
    var label = ago < 1 ? 'just now' : ago + 'm ago';
    el.textContent = 'data: ' + label;
    // Color: green <5m, yellow <15m, red >15m
    el.classList.remove('drawer-age-fresh', 'drawer-age-warn', 'drawer-age-stale');
    el.classList.add(ago < 5 ? 'drawer-age-fresh' : (ago < 15 ? 'drawer-age-warn' : 'drawer-age-stale'));
    // Keep ticking every 30s
    clearInterval(_drawerDataAgeTimer);
    _drawerDataAgeTimer = setInterval(function() {
        _renderDrawerDataAge();
    }, 30000);
}

// ======== Stream Drawer Tab ========
var _streamData = null;
var _streamEventLog = {};  // key: "SYMBOL:zonePrice" → {first_seen, status}
var _streamRefreshTimer = null;
var _streamZoneTimer = null;
var _streamCountdownTimer = null;
var _streamNextRefresh = 0;
var _streamLastFetch = 0;
var _streamFailCount = 0;       // consecutive fetch failures
var _streamRetryTimer = null;   // pending fast-retry after a transient failure
var _streamPrevPrices = {};  // symbol → price 15s ago for momentum
var _streamFilters = {
    search: '',
    tf: { '1h': true, '4h': true, d: true, '3d': true, w: true, m: true },
    sources: [],   // empty = all; ['cpr','bb',...] = show rows containing ANY of these
    status: '',    // '' = all, 'approaching', 'touching', 'passed_through'
    side: '',      // '' = all, 'support', 'resistance'
    bb: '',        // '' = all, 'tight', 'mid', 'wide'
    row_type: '',  // '' = all, 'zone', 'signal', 'transition'
    activeTab: (function() { try { return localStorage.getItem('streamActiveTab') || 'signal'; } catch (e) { return 'signal'; } })(),
    direction: '',         // signals only: '' = all, 'bullish', 'bearish'
    significance: 0,       // transitions only: 0 = all, 1/2/3 = min stars
};

// Tolerant stream fetch — keeps showing cached data on transient failures
// and only surfaces an "unavailable" message after several consecutive misses.
function _fetchStreamData(section) {
    return fetch('/api/stream', { credentials: 'same-origin' })
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function(data) {
            _streamFailCount = 0;
            if (_streamRetryTimer) { clearTimeout(_streamRetryTimer); _streamRetryTimer = null; }
            _streamData = data;
            _streamLastFetch = Date.now();
            if (data.computed_at) _updateDrawerDataAge(data.computed_at);
            if (section) _computeAndRenderStream(section);
        })
        .catch(function() {
            _streamFailCount++;
            // If we have cached data, NEVER blow away the UI on a transient failure —
            // the periodic refresh will recover, and the user keeps seeing real data.
            if (_streamData) {
                // Soft retry once after 3s in case it was a single network blip
                if (_streamFailCount === 1 && !_streamRetryTimer) {
                    _streamRetryTimer = setTimeout(function() {
                        _streamRetryTimer = null;
                        if (drawerOpen && drawerActiveTab === 'stream') {
                            _fetchStreamData(document.getElementById('drawer-section-stream'));
                        }
                    }, 3000);
                }
                return;
            }
            // No cached data and only one failure so far — keep "Loading…" visible
            // and try again quickly. Show the error only after 3 consecutive misses.
            if (_streamFailCount < 3) {
                if (!_streamRetryTimer) {
                    _streamRetryTimer = setTimeout(function() {
                        _streamRetryTimer = null;
                        if (drawerOpen && drawerActiveTab === 'stream') {
                            _fetchStreamData(document.getElementById('drawer-section-stream'));
                        }
                    }, 2000 * _streamFailCount); // 2s, 4s
                }
                return;
            }
            // Give up — show unavailable. Refresh interval will keep trying every 30s.
            if (section) {
                section.innerHTML = '<div style="padding:20px;text-align:center;color:#f85149">Stream temporarily unavailable<br><span style="font-size:0.8em;color:#787b86">Retrying every 30s\u2026</span></div>';
            }
        });
}

// CSP hardening — the Filter stream ticks on a 15/30s timer and can be patched via
// _diffStreamRows (keyed diff, in-place field updates) instead of a full innerHTML
// rebuild — see _renderStreamContent's early-return into _diffStreamRows. A rebind-
// after-render approach would miss rows the diff path never revisits, so the single
// interactive element in this tab (the asset-symbol click) is delegated, bound
// EXACTLY ONCE on the section (guarded), which survives both render paths since
// section itself is never replaced, only patched or fully rebuilt as a child tree.
function _wireStreamDelegatedEvents(section) {
    if (section.dataset.streamActionsWired) return;
    section.dataset.streamActionsWired = '1';
    section.addEventListener('click', function(e) {
        var el = e.target.closest('[data-action]');
        if (!el || !section.contains(el)) return;
        if (el.getAttribute('data-action') === 'stream-select-asset') {
            var sym = el.getAttribute('data-symbol');
            if (sym && typeof selectAssetFromSearch === 'function') selectAssetFromSearch(sym);
        }
    });
}

function renderStreamTab() {
    var section = document.getElementById('drawer-section-stream');
    if (!section) return;
    _wireStreamDelegatedEvents(section);

    if (!_streamData) {
        section.innerHTML = '<div class="stream-loading"><div class="stream-loading-spinner"></div><div class="stream-loading-text">Loading stream\u2026</div></div>';
    } else if (!section.firstChild) {
        // Section was torn down on leave (perf) \u2014 repaint cached data instantly
        // instead of showing an empty pane until the refetch lands.
        _computeAndRenderStream(section);
    }

    // Fetch stream data + scenario events in parallel
    _fetchPlayStreamEvents();
    _fetchStreamData(section);

    // Zone refresh: re-fetch every 30s with visible countdown
    clearInterval(_streamRefreshTimer);
    clearInterval(_streamCountdownTimer);
    _streamNextRefresh = Date.now() + 30000;
    _streamRefreshTimer = setInterval(function() {
        if (document.hidden) return;
        if (drawerOpen && drawerActiveTab === 'stream') {
            if (window.__radarPaused) return;  // Radar pause freezes the auto-refresh
            _streamNextRefresh = Date.now() + 30000;
            // Flash animation to show refresh is happening
            var s = document.getElementById('drawer-section-stream');
            if (s) { s.style.opacity = '0.6'; setTimeout(function() { s.style.opacity = '1'; }, 300); }
            _fetchStreamData(s);
        } else {
            clearInterval(_streamRefreshTimer);
            clearInterval(_streamZoneTimer);
            clearInterval(_streamCountdownTimer);
            if (_streamRetryTimer) { clearTimeout(_streamRetryTimer); _streamRetryTimer = null; }
            if (window.Radar) window.Radar.teardown();
        }
    }, 30000);
    // Countdown ticker (1s) — shows both countdown and last-fetch age
    if (!window.Radar) {
        _streamCountdownTimer = setInterval(function() {
            if (document.hidden) return;
            var el = document.getElementById('stream-countdown');
            if (!el) return;
            var left = Math.max(0, Math.round((_streamNextRefresh - Date.now()) / 1000));
            var fetchAge = _streamLastFetch ? Math.round((Date.now() - _streamLastFetch) / 1000) : '?';
            el.textContent = left + 's';
            var ageEl = document.getElementById('stream-last-fetch');
            if (ageEl) ageEl.textContent = fetchAge + 's ago';
        }, 1000);
    }

    // Proximity re-compute every 15s using live prices
    clearInterval(_streamZoneTimer);
    _streamZoneTimer = setInterval(function() {
        if (document.hidden) return;
        if (drawerOpen && drawerActiveTab === 'stream' && _streamData && !window.__radarPaused) {
            var s = document.getElementById('drawer-section-stream');
            if (s) _computeAndRenderStream(s);
        } else if (!(drawerOpen && drawerActiveTab === 'stream')) {
            clearInterval(_streamZoneTimer);
            if (window.Radar) window.Radar.teardown();
        }
    }, 15000);
}

// Compact $ formatter for liquidation notional — "$4.2M" / "$350K".
// A tiny dedicated formatter rather than reusing _fmtSpotUsd: the spot-tree
// formatter renders sub-10K values as bare '5,000' with no K/M suffix and
// shows one decimal on K-range values ('300.0K'), both a poor fit for the
// liquidation floor (LIQ_SPIKE_MIN_USD defaults to 250000, so values here
// are almost always in the K/M band and read better with no decimal on K).
function _fmtLiqUsd(n) {
    var abs = Math.abs(n || 0);
    if (abs >= 1000000) return '$' + (n / 1000000).toFixed(1) + 'M';
    if (abs >= 1000) return '$' + Math.round(n / 1000) + 'K';
    return '$' + Math.round(n || 0);
}

// Compute flat rows: each row = one symbol+zone pair, sorted globally
function _computeStreamRows(data) {
    var entries = data.entries || [];
    var rows = [];
    var now = Date.now();
    var currentPrices = {};

    for (var i = 0; i < entries.length; i++) {
        var entry = entries[i];
        var sym = entry.symbol;
        var livePrice = entry.price;
        if (allAssets[sym] && allAssets[sym].latest_price) livePrice = allAssets[sym].latest_price;
        if (allAssets[sym] && allAssets[sym].realtime && allAssets[sym].realtime.price) livePrice = allAssets[sym].realtime.price;
        currentPrices[sym] = livePrice;

        if (!livePrice || !entry.zones || entry.zones.length === 0) continue;

        var pdh = entry.pdh, pdl = entry.pdl;

        for (var j = 0; j < entry.zones.length; j++) {
            var zone = entry.zones[j];
            var zp = zone.price;
            if (!zp) continue;

            var band = zone.band || null;
            var zoneSide = zone.side;
            var proximity, status;
            if (band && band.lower != null && band.upper != null) {
                // User-drawn rectangle: proximity to the NEAREST edge; price
                // can be above / inside / below the band.
                if (livePrice > band.upper) {
                    proximity = (livePrice - band.upper) / livePrice * 100;
                    zoneSide = 'support';
                } else if (livePrice < band.lower) {
                    proximity = (band.lower - livePrice) / livePrice * 100;
                    zoneSide = 'resistance';
                } else {
                    proximity = 0;
                    zoneSide = 'inside';
                }
                if (proximity > 5) continue;
                status = zoneSide === 'inside' ? 'inside'
                    : (proximity <= 0.2 ? 'touching' : 'approaching');
                // Momentum tracks approach to the nearest edge.
                if (zoneSide === 'support') zp = band.upper;
                else if (zoneSide === 'resistance') zp = band.lower;
            } else {
                proximity = Math.abs(livePrice - zp) / livePrice * 100;
                if (proximity > 5) continue;

                status = 'approaching';
                if (proximity <= 0.2) {
                    status = 'touching';
                } else if (pdh != null && pdl != null && zp <= pdh && zp >= pdl) {
                    if ((livePrice > zp && zone.side === 'support') || (livePrice < zp && zone.side === 'resistance')) {
                        status = 'passed_through';
                    }
                }
            }

            var momentum = 'flat';
            var prevPrice = _streamPrevPrices[sym];
            if (prevPrice && zoneSide !== 'inside') {
                var diff = livePrice - prevPrice;
                var towardZone = (zp > livePrice && diff > 0) || (zp < livePrice && diff < 0);
                if (Math.abs(diff / livePrice) > 0.0001) momentum = towardZone ? 'toward' : 'away';
            }

            var key = sym + ':' + zp.toFixed(2);
            if (!_streamEventLog[key]) _streamEventLog[key] = { first_seen: now, status: status };
            else _streamEventLog[key].status = status;

            rows.push({
                key: key,
                symbol: sym,
                row_type: 'zone',
                proximity: proximity,
                status: status,
                momentum: momentum,
                tf: zone.tf,
                label: zone.label,
                sources: zone.sources || [],
                strength: zone.strength,
                side: zoneSide,
                band: band,
                bb_squeeze_pct: entry.bb_squeeze_pct,
                price: zp,
            });
        }
    }

    _streamPrevPrices = currentPrices;

    // Trim old event log
    var cutoff = now - 86400000;
    for (var k in _streamEventLog) {
        if (_streamEventLog[k].first_seen < cutoff) delete _streamEventLog[k];
    }

    // Add signal rows (engulfing, MACD cross, EMA Ribbon flip, Alligator)
    var _signalLabels = {
        engulfing: function(d) { return (d === 'bullish' ? 'Bull' : 'Bear') + ' Engulf'; },
        macd_cross: function(d) { return 'MACD ' + (d === 'bullish' ? '+' : '\u2212'); },
        ema_ribbon_flip: function(d) { return 'EMA Ribbon ' + (d === 'bullish' ? 'Bull' : 'Bear'); },
        alligator: function(d) { return 'Alligator ' + (d === 'bullish' ? 'Bull' : 'Bear'); },
    };
    for (var i = 0; i < entries.length; i++) {
        var entry = entries[i];
        var sym = entry.symbol;
        var signals = entry.signals || [];
        for (var si = 0; si < signals.length; si++) {
            var sig = signals[si];
            var sigType = sig.type || 'engulfing';
            var labelFn = _signalLabels[sigType] || _signalLabels.engulfing;
            rows.push({
                key: sym + ':' + sigType + ':' + sig.tf + ':' + sig.direction + ':' + sig.candle_pos,
                symbol: sym,
                row_type: 'signal',
                signal_type: sigType,
                proximity: null,
                status: sig.candle_pos,
                momentum: 'flat',
                tf: sig.tf,
                label: labelFn(sig.direction),
                sources: [],
                strength: sig.volume_ratio || 0,
                detail: sig.detail || '',
                side: sig.direction === 'bullish' ? 'support' : 'resistance',
                signal_direction: sig.direction,
                bb_squeeze_pct: entry.bb_squeeze_pct,
            });
        }
    }

    // Add indicator transition rows
    var _transLabels = {
        ema_ribbon: 'EMA Ribbon',
        rsi_zone: 'RSI Zone',
        bb_squeeze: 'BB Squeeze',
        bb_position: 'BB Position',
        consensio: 'Consensio',
        structure: 'Structure',
        macd: 'MACD',
        adx_regime: 'ADX Regime',
        cpr_position: 'CPR Position',
        weekly_cpr_position: 'Weekly CPR',
        daily_cpr_position: 'Daily CPR',
        super_trend: 'SuperTrend',
        psar: 'PSAR',
        ichimoku: 'Ichimoku Cloud',
        ichimoku_tk: 'Ichimoku TK Cross',
        ttm_squeeze: 'TTM Squeeze',
        alligator: 'Alligator',
        ema_cross: 'EMA Cross',
        donchian: 'Donchian Channel',
        stoch_rsi: 'Stoch RSI',
        // Sidebar signal vocabulary as tracked states
        ema_gap_squeeze: 'EMA Gap Squeeze',
        v_cpr: 'CPR Vacuum',
        cpr_extreme: 'CPR Extreme',
        structure_disagreement: 'Structure Disagreement',
        tf_divergence: 'Timeframe Divergence',
        rsi_divergence: 'RSI Divergence',
        perp_basis: 'Perp Basis',
    };
    for (var i = 0; i < entries.length; i++) {
        var entry = entries[i];
        var sym = entry.symbol;
        var trans = entry.transitions || [];
        for (var ti = 0; ti < trans.length; ti++) {
            var t = trans[ti];
            rows.push({
                key: sym + ':trans:' + t.indicator + ':' + t.ts,
                symbol: sym,
                row_type: 'transition',
                proximity: null,
                status: 'transition',
                momentum: 'flat',
                tf: '',
                label: (_transLabels[t.indicator] || t.indicator) + ': ' + t.from + ' → ' + t.to,
                sources: [],
                strength: t.significance || 1,
                side: '',
                ts: t.ts,
                significance: t.significance || 1,
            });
        }
    }

    // Add position rows — the user's own open positions,
    // an implicit watchlist entry regardless of TF/lens. Factual wording only
    // (non-advice): never "consider closing", always a measured distance/state.
    var positions = data.positions || [];
    for (var pi = 0; pi < positions.length; pi++) {
        var pos = positions[pi];
        var psym = pos.symbol;
        var pdir = pos.direction === 'short' ? 'short' : 'long';
        var entryTxt = (pos.entry_price != null) ? ' from ' + formatPrice(pos.entry_price, psym) : '';
        var plabel = (pdir === 'long' ? 'LONG' : 'SHORT') + entryTxt;

        var pdetail;
        if (pos.risk_status === 'stopped_out') {
            pdetail = 'stopped out';
        } else if (pos.unrealized_pnl_pct != null && pos.unrealized_pnl_pct > 0) {
            pdetail = '+' + pos.unrealized_pnl_pct.toFixed(1) + '% unrealized' +
                (pos.current_rr != null ? ', ' + pos.current_rr.toFixed(1) + 'R' : '');
        } else if (pos.stop_distance_pct != null) {
            pdetail = Math.abs(pos.stop_distance_pct).toFixed(1) + '% from stop';
        } else if (pos.unrealized_pnl_pct != null) {
            pdetail = pos.unrealized_pnl_pct.toFixed(1) + '% unrealized';
        } else {
            pdetail = '';
        }

        // Magnitude cell: R-multiple when available (the sharper risk signal),
        // else a pnl-derived proxy scaled roughly onto the same 0-5ish range
        // the strength bar expects.
        var pstrength = (pos.current_rr != null) ? Math.abs(pos.current_rr) :
            (pos.unrealized_pnl_pct != null ? Math.abs(pos.unrealized_pnl_pct) / 5 : 0);

        rows.push({
            key: 'position:' + psym,
            symbol: psym,
            row_type: 'position',
            direction: pdir,
            proximity: null,
            status: pos.risk_status || '',
            momentum: 'flat',
            tf: '',
            label: plabel,
            detail: pdetail,
            sources: [],
            strength: pstrength,
            side: '',
            risk_status: pos.risk_status,
            unrealized_pnl_pct: pos.unrealized_pnl_pct,
            current_rr: pos.current_rr,
            stop_distance_pct: pos.stop_distance_pct,
            entry_price: pos.entry_price,
            age: pos.age,
        });
    }

    // Add liquidation-spike rows — backend already applies the
    // spike rule + free-tier truncation (via the entries list), so every
    // entry.liquidation here is a real, already-gated spike. At most one row
    // per symbol.
    for (var li = 0; li < entries.length; li++) {
        var lentry = entries[li];
        var liq = lentry.liquidation;
        if (!liq) continue;
        var lsym = lentry.symbol;
        var dominant = liq.dominant === 'short' ? 'short' : 'long';
        var longUsd = liq.m15_long_usd || 0;
        var shortUsd = liq.m15_short_usd || 0;
        var totalUsd = liq.total_usd != null ? liq.total_usd : (longUsd + shortUsd);
        var dominantUsd = dominant === 'long' ? longUsd : shortUsd;

        rows.push({
            key: 'liq:' + lsym,
            symbol: lsym,
            row_type: 'liquidation',
            dominant: dominant,
            // Long liquidations accompany downside (forced selling) and vice versa.
            direction: dominant === 'long' ? 'bearish' : 'bullish',
            proximity: null,
            status: 'live',
            momentum: 'flat',
            tf: '',
            label: _fmtLiqUsd(dominantUsd) + ' ' + dominant + 's liquidated',
            detail: '15m · ' + _fmtLiqUsd(longUsd) + ' longs / ' + _fmtLiqUsd(shortUsd) + ' shorts',
            sources: [],
            strength: Math.min(3, totalUsd / 1000000),
            side: '',
            total_usd: totalUsd,
            m15_long_usd: longUsd,
            m15_short_usd: shortUsd,
        });
    }

    // Global sort: transitions first (newest), then signals, then zones
    var _sr = { touching: 0, passed_through: 1, approaching: 2, current: 0, previous: 1 };
    rows.sort(function(a, b) {
        // Transitions > Signals > Zones
        var typeOrder = { transition: 0, signal: 1, zone: 2 };
        var ta = typeOrder[a.row_type] || 2;
        var tb = typeOrder[b.row_type] || 2;
        if (ta !== tb) return ta - tb;
        // Within transitions: newest first (by ts)
        if (a.row_type === 'transition' && b.row_type === 'transition') {
            return (b.ts || '').localeCompare(a.ts || '');
        }
        var ra = _sr[a.status] || 2, rb = _sr[b.status] || 2;
        if (ra !== rb) return ra - rb;
        if (a.proximity != null && b.proximity != null) return a.proximity - b.proximity;
        return 0;
    });

    // Imminent macro event — ONE global row, unshifted
    // to the front AFTER the sort above so nothing in that comparator (which
    // has no 'event' case and would fall into the shared default bucket) can
    // reorder it away from the top. No real symbol (MACRO placeholder) — the
    // by-asset view and jumpChart both special-case row_type 'event' to
    // exclude/no-op on it (see radar.js).
    if (data.stream_event && data.stream_event.title) {
        var ev = data.stream_event;
        var hoursUntil = ev.hours_until != null ? ev.hours_until : 0;
        var whenTxt = hoursUntil <= 0 ? 'today' : 'in ~' + Math.round(hoursUntil) + 'h';
        rows.unshift({
            key: 'event:' + ev.title,
            symbol: 'MACRO',
            row_type: 'event',
            proximity: null,
            status: 'live',
            momentum: 'flat',
            tf: '',
            label: ev.title,
            detail: (ev.impact || 'high') + ' impact · ' + whenTxt,
            sources: [],
            strength: 0,
            side: '',
            hours_until: hoursUntil,
            impact: ev.impact || 'high',
        });
    }

    return rows;
}

var _streamPrevState = {};
var _streamLastCompute = 0;

function _computeAndRenderStream(section) {
    if (!_streamData) return;
    _streamLastCompute = Date.now();
    var rows = _computeStreamRows(_streamData);

    // Merge scenario events into stream
    if (window._playStreamEvents) {
        rows = rows.concat(window._playStreamEvents);
    }

    // Radar (unified Filter UI) owns rendering when present — it does its own
    // lensing + filtering from the full, unfiltered row set.
    if (window.Radar) {
        // Per-symbol composite bias map (from the scanner verdict surfaced on
        // each /api/stream entry) — powers the By-Asset lean. May be null per
        // symbol (free tier / not computed); Radar falls back to a client tally.
        var biasBySymbol = {};
        if (_streamData.entries) {
            _streamData.entries.forEach(function(e) {
                if (e && e.symbol) {
                    biasBySymbol[e.symbol] = {
                        bias: e.bias || null,
                        score: (typeof e.bias_score === 'number') ? e.bias_score : null
                    };
                }
            });
        }
        window.Radar.render(section, rows, {
            computedAt: _streamData.computed_at,
            nextRefreshTs: _streamNextRefresh,
            lastFetchTs: _streamLastFetch,
            preview: !!(_streamData && _streamData.preview_mode),
            tier: (_streamData && _streamData.preview_mode) ? 'free' : 'member',
            biasBySymbol: biasBySymbol
        });
        return;
    }

    rows = _applyStreamFilters(rows);
    _renderStreamContent(section, rows);
}

// Fetch active plays for stream tab (called on stream tab open / refresh)
function _fetchPlayStreamEvents() {
    fetch('/api/plays/active', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var statusColors = { watching: '#42a5f5', acting: '#f0b90b', expired: '#ef5350', completed: '#555', invalidated: '#555' };
        window._playStreamEvents = (data.plays || []).map(function(p) {
            var conds = p.conditions || {};
            var rules = conds.rules || [];
            var met = rules.filter(function(r) { return r.met; }).length;
            return {
                row_type: 'scenario',
                symbol: p.primary_asset || '?',
                label: p.title + ' \u2014 ' + (p.status || '?').toUpperCase(),
                detail: met + '/' + rules.length + ' conditions',
                color: statusColors[p.status] || '#787b86',
                time: p.updated_at,
                sort_key: new Date(p.updated_at).getTime() || 0,
            };
        });
    })
    .catch(function() { window._playStreamEvents = []; });
}

function _applyStreamFilters(rows) {
    var f = _streamFilters;
    return rows.filter(function(r) {
        // Search: match symbol or label (all tabs)
        if (f.search) {
            var q = f.search.toLowerCase();
            if (r.symbol.toLowerCase().indexOf(q) === -1 && r.label.toLowerCase().indexOf(q) === -1) return false;
        }
        // Timeframe (signals + zones)
        if (r.row_type !== 'transition' && r.tf && !f.tf[r.tf.toLowerCase()]) return false;
        // Direction (signals only)
        if (f.direction && r.row_type === 'signal' && r.signal_direction !== f.direction) return false;
        // Significance (transitions only)
        if (f.significance > 0 && r.row_type === 'transition' && (r.significance || 0) < f.significance) return false;
        // Zone sources (zones only)
        if (f.sources.length > 0 && r.row_type === 'zone') {
            var match = false;
            for (var i = 0; i < f.sources.length; i++) {
                if (r.sources.indexOf(f.sources[i]) !== -1) { match = true; break; }
            }
            if (!match) return false;
        }
        // Status (zones only)
        if (f.status && r.row_type === 'zone' && r.status !== f.status) return false;
        // Side (zones only)
        if (f.side && r.row_type === 'zone' && r.side !== f.side) return false;
        // BB squeeze (signals + zones)
        if (f.bb && r.row_type !== 'transition') {
            var pct = r.bb_squeeze_pct;
            if (pct == null) return false;
            if (f.bb === 'tight' && pct >= 30) return false;
            if (f.bb === 'mid' && (pct < 30 || pct >= 60)) return false;
            if (f.bb === 'wide' && pct < 60) return false;
        }
        return true;
    });
}

// Tab configuration: order matters (signals leftmost, then transitions, then zones)
var _STREAM_TABS = [
    { key: 'signal',     label: 'Active Signals' },
    { key: 'transition', label: 'Transitions' },
    { key: 'zone',       label: 'Nearby Zones' },
];

function _partitionStreamRows(rows) {
    var groups = { signal: [], transition: [], zone: [] };
    for (var i = 0; i < rows.length; i++) {
        // Only known row_types get bucketed. Anything else (e.g. 'scenario'
        // rows from _fetchPlayStreamEvents) is dropped here — those rows
        // lack tf/proximity/strength so feeding them to _buildStreamRow as
        // zones throws and aborts the whole render.
        var rt = rows[i].row_type;
        if (rt === 'signal' || rt === 'transition' || rt === 'zone') {
            groups[rt].push(rows[i]);
        }
    }
    return groups;
}

function _buildStreamTabs(counts) {
    var active = _streamFilters.activeTab;
    var html = '<div class="stream-tabs">';
    for (var i = 0; i < _STREAM_TABS.length; i++) {
        var t = _STREAM_TABS[i];
        var c = counts[t.key] || 0;
        var cls = 'stream-tab' + (active === t.key ? ' stream-tab-active' : '');
        html += '<button class="' + cls + '" data-stream-tab="' + t.key + '">' +
            '<span class="stream-tab-label">' + t.label + '</span>' +
            '<span class="stream-tab-count">' + c + '</span>' +
        '</button>';
    }
    html += '</div>';
    return html;
}

// Per-tab column headers — each tab gets a layout tailored to its data type
function _buildTabTableHeader(tabKey) {
    if (tabKey === 'signal') {
        return '<div class="stream-table-header stream-th-signal">' +
            '<span class="stream-th stream-th-symbol">Asset</span>' +
            '<span class="stream-th stream-th-tf" title="Timeframe">TF</span>' +
            '<span class="stream-th stream-th-zone">Pattern</span>' +
            '<span class="stream-th stream-th-str">Detail</span>' +
            '<span class="stream-th stream-th-state">When</span>' +
            '<span class="stream-th stream-th-squeeze" title="Bollinger Band width percentile">BBW</span>' +
        '</div>';
    }
    if (tabKey === 'transition') {
        return '<div class="stream-table-header stream-th-transition">' +
            '<span class="stream-th stream-th-symbol">Asset</span>' +
            '<span class="stream-th stream-th-zone-wide">Indicator change</span>' +
            '<span class="stream-th stream-th-str">Significance</span>' +
            '<span class="stream-th stream-th-state">State</span>' +
        '</div>';
    }
    // zone
    return '<div class="stream-table-header stream-th-zone-tab">' +
        '<span class="stream-th stream-th-symbol">Asset</span>' +
        '<span class="stream-th stream-th-tf" title="Timeframe">TF</span>' +
        '<span class="stream-th stream-th-prox" title="Distance from current price; arrow shows momentum">Dist</span>' +
        '<span class="stream-th stream-th-zone">Level</span>' +
        '<span class="stream-th stream-th-str" title="Confluence strength = sum of indicator weights at this level">Str</span>' +
        '<span class="stream-th stream-th-state">Side &amp; Status</span>' +
        '<span class="stream-th stream-th-squeeze" title="Bollinger Band width percentile">BBW</span>' +
    '</div>';
}

function _streamUpsellCard(variant) {
    // variant: 'transition' (zero-preview locked card) or 'footer' (small banner below preview rows)
    var reason = (_streamData && _streamData.preview_reason) || 'This needs a higher plan. An admin can change plans under Admin → Users.';
    if (variant === 'transition') {
        return '<div class="stream-upsell stream-upsell-lock">' +
            '<div class="stream-upsell-icon">\uD83D\uDD12</div>' +
            '<div class="stream-upsell-title">Indicator Transitions</div>' +
            '<div class="stream-upsell-body">Real-time state changes across RSI, MACD, EMA ribbon, and more &mdash; a Member exclusive.</div>' +
            '<span class="stream-upsell-cta">This needs a higher plan. An admin can change plans under Admin → Users.</span>' +
            '</div>';
    }
    return '<div class="stream-upsell stream-upsell-footer">' +
        '<div class="stream-upsell-body">' + reason + '</div>' +
        '<span class="stream-upsell-cta">This needs a higher plan. An admin can change plans under Admin → Users.</span>' +
        '</div>';
}

function _renderStreamContent(section, rows) {
    var previewMode = !!(_streamData && _streamData.preview_mode);

    var existingRows = section.querySelectorAll('.stream-row[data-key]');
    // Never diff-render in preview mode — the upsell card must be reattached on each render.
    if (!previewMode && existingRows.length > 0 && rows.length > 0) {
        _diffStreamRows(section, rows);
        return;
    }

    // Compute counts up front (used by tabs and meta line)
    var groups = _partitionStreamRows(rows);
    var counts = { signal: groups.signal.length, transition: groups.transition.length, zone: groups.zone.length };

    var html = _buildStreamHeader(rows, counts);
    html += _buildStreamTabs(counts);

    var active = _streamFilters.activeTab;
    if (!_STREAM_TABS.some(function(t) { return t.key === active; })) active = 'signal';

    // Preview-mode: transitions tab is fully locked with zero rows.
    if (previewMode && active === 'transition') {
        html += _streamUpsellCard('transition');
        section.innerHTML = html;
        _wireStreamFilters(section);
        _wireStreamTabs(section);
        _streamPrevState = {};
        return;
    }

    var activeRows = groups[active];

    if (activeRows.length === 0) {
        html += '<div class="stream-empty">No ' + active + 's right now \u2014 check back soon</div>';
        if (previewMode) html += _streamUpsellCard('footer');
        section.innerHTML = html;
        _wireStreamFilters(section);
        _wireStreamTabs(section);
        _streamPrevState = {};
        return;
    }

    html += _buildTabTableHeader(active);
    for (var i = 0; i < activeRows.length; i++) {
        try {
            html += _buildStreamRow(activeRows[i]);
        } catch (err) {
            // One malformed row must not blank out the whole tab.
            if (typeof console !== 'undefined') console.warn('[stream] row render failed', err, activeRows[i]);
        }
    }

    if (previewMode) html += _streamUpsellCard('footer');

    section.innerHTML = html;
    _wireStreamFilters(section);
    _wireStreamTabs(section);
    _snapshotStreamState(activeRows);
}

function _wireStreamTabs(section) {
    var tabs = section.querySelectorAll('[data-stream-tab]');
    for (var i = 0; i < tabs.length; i++) {
        (function(el) {
            el.onclick = function() {
                _streamFilters.activeTab = el.getAttribute('data-stream-tab');
                try { localStorage.setItem('streamActiveTab', _streamFilters.activeTab); } catch (e) {}
                _forceStreamFullRender(section);
            };
        })(tabs[i]);
    }
}

function _buildStreamHeader(rows, counts) {
    var f = _streamFilters;
    rows = rows || [];

    // Zone multi-select: build trigger label
    var zoneSources = [
        { key: 'cpr', label: 'CPR' },
        { key: 'bb', label: 'Bollinger' },
        { key: 'ema_ribbon', label: 'EMA Ribbon' },
        { key: 'ma_levels', label: 'Moving Avg' },
        { key: 'structure', label: 'Structure' },
        { key: 'vp', label: 'Vol Profile' },
        { key: 'key_levels', label: 'Key Levels' },
    ];
    var zoneLabel = 'Zone: All';
    if (f.sources.length > 0 && f.sources.length < zoneSources.length) {
        var names = [];
        for (var zi = 0; zi < zoneSources.length; zi++) {
            if (f.sources.indexOf(zoneSources[zi].key) > -1) names.push(zoneSources[zi].label);
        }
        zoneLabel = names.length <= 2 ? names.join(', ') : names.length + ' zones';
    }
    var zoneOpts = '';
    for (var zi = 0; zi < zoneSources.length; zi++) {
        var zs = zoneSources[zi];
        var checked = f.sources.length === 0 || f.sources.indexOf(zs.key) > -1 ? ' checked' : '';
        zoneOpts += '<label class="stream-zone-opt"><input type="checkbox" value="' + zs.key + '"' + checked + '>' + zs.label + '</label>';
    }

    // TF dropdown value — single-select among all TFs
    var _allTfKeys = ['1h', '4h', 'd', '3d', 'w', 'm'];
    var tfVal = '';
    var tfOnCount = 0;
    for (var ti = 0; ti < _allTfKeys.length; ti++) { if (f.tf[_allTfKeys[ti]]) tfOnCount++; }
    if (tfOnCount === _allTfKeys.length) tfVal = '';
    else {
        for (var ti = 0; ti < _allTfKeys.length; ti++) {
            if (f.tf[_allTfKeys[ti]] && tfOnCount === 1) { tfVal = _allTfKeys[ti]; break; }
        }
    }

    var tab = _streamFilters.activeTab || 'signal';

    // Search box (all tabs)
    var searchHTML =
        '<div class="stream-search-inline">' +
            '<input type="text" class="stream-search-input" id="stream-search" placeholder="Search\u2026" value="' + (f.search || '') + '">' +
            (f.search ? '<button class="stream-search-clear" id="stream-search-clear">&times;</button>' : '') +
        '</div>';

    // Timeframe (signals + zones)
    var tfHTML =
        '<select class="stream-select" id="stream-tf-filter">' +
            '<option value=""' + (tfVal === '' ? ' selected' : '') + '>Timeframe: All</option>' +
            '<option value="1h"' + (tfVal === '1h' ? ' selected' : '') + '>1 Hour</option>' +
            '<option value="4h"' + (tfVal === '4h' ? ' selected' : '') + '>4 Hour</option>' +
            '<option value="d"' + (tfVal === 'd' ? ' selected' : '') + '>Daily</option>' +
            '<option value="3d"' + (tfVal === '3d' ? ' selected' : '') + '>3 Day</option>' +
            '<option value="w"' + (tfVal === 'w' ? ' selected' : '') + '>Weekly</option>' +
            '<option value="m"' + (tfVal === 'm' ? ' selected' : '') + '>Monthly</option>' +
        '</select>';

    // Squeeze (signals + zones)
    var bbHTML =
        '<select class="stream-select" id="stream-bb-filter">' +
            '<option value=""' + (!f.bb ? ' selected' : '') + '>Squeeze: All</option>' +
            '<option value="tight"' + (f.bb === 'tight' ? ' selected' : '') + '>Tight &lt;30%</option>' +
            '<option value="mid"' + (f.bb === 'mid' ? ' selected' : '') + '>Mid 30-60%</option>' +
            '<option value="wide"' + (f.bb === 'wide' ? ' selected' : '') + '>Wide &gt;60%</option>' +
        '</select>';

    // Direction (signals only)
    var dirHTML =
        '<select class="stream-select" id="stream-direction-filter">' +
            '<option value=""' + (!f.direction ? ' selected' : '') + '>Direction: All</option>' +
            '<option value="bullish"' + (f.direction === 'bullish' ? ' selected' : '') + '>Bullish only</option>' +
            '<option value="bearish"' + (f.direction === 'bearish' ? ' selected' : '') + '>Bearish only</option>' +
        '</select>';

    // Significance (transitions only)
    var sigHTML =
        '<select class="stream-select" id="stream-significance-filter">' +
            '<option value="0"' + (!f.significance ? ' selected' : '') + '>Significance: All</option>' +
            '<option value="1"' + (f.significance === 1 ? ' selected' : '') + '>\u2605 or higher</option>' +
            '<option value="2"' + (f.significance === 2 ? ' selected' : '') + '>\u2605\u2605 or higher</option>' +
            '<option value="3"' + (f.significance === 3 ? ' selected' : '') + '>\u2605\u2605\u2605 only</option>' +
        '</select>';

    // Zone source (zones only)
    var zoneSourceHTML =
        '<div class="stream-zone-multi">' +
            '<button class="stream-zone-trigger" id="stream-zone-trigger">' + zoneLabel + ' <span class="stream-zone-arrow">&#9662;</span></button>' +
            '<div class="stream-zone-panel" id="stream-zone-panel">' + zoneOpts + '</div>' +
        '</div>';

    // Side (zones only)
    var sideHTML =
        '<select class="stream-select" id="stream-side-filter">' +
            '<option value=""' + (!f.side ? ' selected' : '') + '>Side: All</option>' +
            '<option value="support"' + (f.side === 'support' ? ' selected' : '') + '>Support</option>' +
            '<option value="resistance"' + (f.side === 'resistance' ? ' selected' : '') + '>Resistance</option>' +
        '</select>';

    // Status (zones only)
    var statusHTML =
        '<select class="stream-select" id="stream-status-filter">' +
            '<option value=""' + (!f.status ? ' selected' : '') + '>Status: All</option>' +
            '<option value="approaching"' + (f.status === 'approaching' ? ' selected' : '') + '>Approaching</option>' +
            '<option value="touching"' + (f.status === 'touching' ? ' selected' : '') + '>Touching</option>' +
            '<option value="passed_through"' + (f.status === 'passed_through' ? ' selected' : '') + '>Crossed</option>' +
        '</select>';

    // Compose per active tab
    var filterHTML;
    if (tab === 'signal') {
        filterHTML = searchHTML + tfHTML + dirHTML + bbHTML;
    } else if (tab === 'transition') {
        filterHTML = searchHTML + sigHTML;
    } else {
        filterHTML = searchHTML + tfHTML + zoneSourceHTML + sideHTML + statusHTML + bbHTML;
    }

    var html = '<div class="stream-header">' +
        '<div class="stream-filters stream-filters-tab">' +
            filterHTML +
            '<span class="stream-meta-times">' +
                'Last <span id="stream-last-fetch">\u2014</span> \u00B7 next <span id="stream-countdown">30s</span>' +
            '</span>' +
        '</div>' +
    '</div>';
    return html;
}

function _wireStreamFilters(section) {
    var search = section.querySelector('#stream-search');
    if (search) {
        search.oninput = function() {
            _streamFilters.search = this.value;
            _forceStreamFullRender(section);
        };
    }
    var clearBtn = section.querySelector('#stream-search-clear');
    if (clearBtn) {
        clearBtn.onclick = function() {
            _streamFilters.search = '';
            _forceStreamFullRender(section);
        };
    }
    // Direction filter (signals tab)
    var dirSel = section.querySelector('#stream-direction-filter');
    if (dirSel) {
        dirSel.onchange = function() {
            _streamFilters.direction = this.value;
            _forceStreamFullRender(section);
        };
    }
    // Significance filter (transitions tab)
    var sigSel = section.querySelector('#stream-significance-filter');
    if (sigSel) {
        sigSel.onchange = function() {
            _streamFilters.significance = parseInt(this.value, 10) || 0;
            _forceStreamFullRender(section);
        };
    }
    // TF dropdown
    var tfSel = section.querySelector('#stream-tf-filter');
    if (tfSel) {
        tfSel.onchange = function() {
            var v = this.value;
            var allOn = { '1h': true, '4h': true, d: true, '3d': true, w: true, m: true };
            if (!v) {
                _streamFilters.tf = allOn;
            } else {
                var tf = { '1h': false, '4h': false, d: false, '3d': false, w: false, m: false };
                tf[v] = true;
                _streamFilters.tf = tf;
            }
            _forceStreamFullRender(section);
        };
    }
    // Zone multi-select
    var zoneTrigger = section.querySelector('#stream-zone-trigger');
    var zonePanel = section.querySelector('#stream-zone-panel');
    if (zoneTrigger && zonePanel) {
        zoneTrigger.onclick = function(e) {
            e.stopPropagation();
            zonePanel.classList.toggle('open');
        };
        // Close panel when clicking outside
        document.addEventListener('click', function _closeZone(e) {
            if (!zonePanel.contains(e.target) && e.target !== zoneTrigger) {
                zonePanel.classList.remove('open');
            }
        });
        zonePanel.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {
            cb.onchange = function() {
                var checked = [];
                zonePanel.querySelectorAll('input[type="checkbox"]:checked').forEach(function(c) {
                    checked.push(c.value);
                });
                // If all checked or none checked, treat as "All"
                _streamFilters.sources = checked.length === 6 ? [] : checked;
                _forceStreamFullRender(section);
            };
        });
    }
    // Type dropdown
    // Status dropdown
    var statusSel = section.querySelector('#stream-status-filter');
    if (statusSel) {
        statusSel.onchange = function() {
            _streamFilters.status = this.value;
            _forceStreamFullRender(section);
        };
    }
    // Side dropdown
    var sideSel = section.querySelector('#stream-side-filter');
    if (sideSel) {
        sideSel.onchange = function() {
            _streamFilters.side = this.value;
            _forceStreamFullRender(section);
        };
    }
    // BB dropdown
    var bbSel = section.querySelector('#stream-bb-filter');
    if (bbSel) {
        bbSel.onchange = function() {
            _streamFilters.bb = this.value;
            _forceStreamFullRender(section);
        };
    }
}

function _forceStreamFullRender(section) {
    // Clear existing rows so _renderStreamContent does a full render (not diff)
    section.querySelectorAll('.stream-row[data-key]').forEach(function(el) { el.remove(); });
    _streamPrevState = {};
    _computeAndRenderStream(section);
    _wireStreamFilters(section);
}


// Friendly text for signal detail column
var _signalDetailText = {
    engulfing: function(r) { return 'Volume ' + (r.strength || 0).toFixed(1) + '\u00d7'; },
    macd_cross: function(r) { return r.detail || 'MACD cross'; },
    ema_ribbon_flip: function(r) { return r.detail ? r.detail.replace(/^from\s+/i, 'From ') : 'Trend flip'; },
    alligator: function(r) { return 'Fully aligned'; },
};

function _buildStreamRow(r) {
    var isSignal = r.row_type === 'signal';
    var isTransition = r.row_type === 'transition';
    var isZone = !isSignal && !isTransition;

    var rowClass = 'stream-row';
    if (isSignal) rowClass += ' stream-row-signal';
    else if (isTransition) rowClass += ' stream-row-transition';
    else rowClass += ' stream-status-' + r.status;

    var html = '<div class="' + rowClass + '" data-key="' + r.key + '" data-section="' + (isSignal ? 'signal' : isTransition ? 'transition' : 'zone') + '">';

    // ASSET (all tabs)
    html += '<span class="stream-symbol" data-action="stream-select-asset" data-symbol="' + escapeHtml(r.symbol) + '" title="' + r.symbol + '">' + r.symbol + '</span>';

    // TIMEFRAME (signals + zones only)
    if (!isTransition) {
        html += '<span class="stream-tf stream-tf-' + r.tf.toLowerCase() + '">' + r.tf + '</span>';
    }

    // DISTANCE (zones only)
    if (isZone) {
        var proxClass = r.proximity <= 0.5 ? ' stream-prox-hot' : (r.proximity <= 2 ? ' stream-prox-warm' : '');
        var arrow = r.momentum === 'toward' ? ' <span class="stream-prox-arrow stream-mom-toward">\u2192</span>'
                  : r.momentum === 'away' ? ' <span class="stream-prox-arrow stream-mom-away">\u2190</span>'
                  : '';
        html += '<span class="stream-proximity' + proxClass + '" data-field="prox">' + r.proximity.toFixed(1) + '%' + arrow + '</span>';
    }

    // LEVEL / PATTERN / INDICATOR CHANGE
    if (isSignal) {
        var sigIcon = r.signal_direction === 'bullish' ? '\u25B2 ' : '\u25BC ';
        var sigClass = r.signal_direction === 'bullish' ? 'stream-signal-bull' : 'stream-signal-bear';
        html += '<span class="stream-zone-label ' + sigClass + '" title="' + escapeHtml(r.label) + '">' + sigIcon + escapeHtml(r.label) + '</span>';
    } else if (isTransition) {
        // Transitions get a wider label column since they have fewer total columns
        html += '<span class="stream-zone-label stream-zone-label-wide" title="' + escapeHtml(r.label) + '">' + escapeHtml(r.label) + '</span>';
    } else {
        var label = r.label.length > 24 ? r.label.substring(0, 23) + '\u2026' : r.label;
        html += '<span class="stream-zone-label" title="' + escapeHtml(_expandConfLabel(r.label)) + '">' + escapeHtml(label) + '</span>';
    }

    // DETAIL / SIGNIFICANCE / STRENGTH
    if (isSignal) {
        var detailFn = _signalDetailText[r.signal_type] || _signalDetailText.engulfing;
        var strText = detailFn(r);
        html += '<span class="stream-strength stream-signal-detail" title="' + (r.detail || '') + '">' + strText + '</span>';
    } else if (isTransition) {
        html += '<span class="stream-strength stream-trans-stars" title="Significance level (' + r.significance + '/3)">' + '\u2605'.repeat(r.significance) + '</span>';
    } else {
        var strWidth = Math.min(r.strength / 5 * 100, 100);
        html += '<span class="stream-strength" title="Confluence strength: ' + r.strength.toFixed(1) + ' (sum of indicator weights at this level)"><span class="stream-strength-bar" style="width:' + strWidth + '%"></span><span class="stream-strength-num">' + r.strength.toFixed(1) + '</span></span>';
    }

    // STATE (when fired / state changed / side & status)
    if (isSignal) {
        var sigStatusClass = r.status === 'current' ? 'stream-st-fired' : 'stream-st-prior';
        var sigStatusText = r.status === 'current' ? '\u26A1 Just fired' : 'Last bar';
        html += '<span class="stream-state ' + sigStatusClass + '" data-field="status">' + sigStatusText + '</span>';
    } else if (isTransition) {
        html += '<span class="stream-state stream-st-changed" data-field="status">Changed</span>';
    } else {
        var sideText = r.side === 'support' ? 'Support' : 'Resistance';
        var sideColorClass = r.side === 'support' ? 'stream-side-support' : 'stream-side-resistance';
        var statusText = r.status === 'passed_through' ? 'Crossed' : (r.status.charAt(0).toUpperCase() + r.status.slice(1));
        html += '<span class="stream-state stream-st-' + r.status + '" data-field="status"><span class="' + sideColorClass + '">' + sideText + '</span> \u00B7 ' + statusText + '</span>';
    }

    // SQUEEZE (signals + zones only)
    if (!isTransition) {
        if (r.bb_squeeze_pct != null) {
            var sqPct = Math.round(r.bb_squeeze_pct);
            var sqClass = sqPct < 30 ? ' stream-squeeze-tight' : (sqPct < 60 ? ' stream-squeeze-mid' : ' stream-squeeze-wide');
            html += '<span class="stream-squeeze' + sqClass + '" title="Bollinger Band width percentile (lower = tighter)">' + sqPct + '%</span>';
        } else {
            html += '<span class="stream-squeeze"></span>';
        }
    }

    return html + '</div>';
}

function _diffStreamRows(section, rows) {

    var newByKey = {};
    var newOrder = [];
    for (var i = 0; i < rows.length; i++) {
        newByKey[rows[i].key] = rows[i];
        newOrder.push(rows[i].key);
    }

    // If the set of keys changed at all (added/removed/reordered), do a full rebuild.
    // This preserves correct section grouping and counts. The common 15s tick where only
    // proximity values change (same key set) still hits the fast path below.
    var existingRows = section.querySelectorAll('.stream-row[data-key]');
    var existingKeys = {};
    var existingOrderKeys = [];
    for (var i = 0; i < existingRows.length; i++) {
        var k = existingRows[i].getAttribute('data-key');
        existingKeys[k] = existingRows[i];
        existingOrderKeys.push(k);
    }

    var sameSet = existingOrderKeys.length === newOrder.length;
    if (sameSet) {
        for (var i = 0; i < newOrder.length; i++) {
            if (newOrder[i] !== existingOrderKeys[i]) { sameSet = false; break; }
        }
    }
    if (!sameSet) {
        // Composition changed — full rebuild to keep sections + counts correct
        section.innerHTML = '';
        _renderStreamContent(section, rows);
        return;
    }

    // Fast path: same row keys, just update proximity/momentum/status fields in place
    for (var i = 0; i < existingRows.length; i++) {
        var el = existingRows[i];
        var key = el.getAttribute('data-key');
        var r = newByKey[key];
        if (!r) continue;

        var isZone = el.getAttribute('data-section') === 'zone';
        var changed = false;

        var proxEl = el.querySelector('[data-field="prox"]');
        if (proxEl && isZone) {
            var arrow = r.momentum === 'toward' ? ' <span class="stream-prox-arrow stream-mom-toward">\u2192</span>'
                      : r.momentum === 'away' ? ' <span class="stream-prox-arrow stream-mom-away">\u2190</span>'
                      : '';
            var newProxHTML = r.proximity.toFixed(1) + '%' + arrow;
            if (proxEl.innerHTML !== newProxHTML) {
                proxEl.innerHTML = newProxHTML;
                proxEl.className = 'stream-proximity' + (r.proximity <= 0.5 ? ' stream-prox-hot' : (r.proximity <= 2 ? ' stream-prox-warm' : ''));
                changed = true;
            }
        }

        var statusEl = el.querySelector('[data-field="status"]');
        if (statusEl && isZone) {
            var sideText = r.side === 'support' ? 'Support' : 'Resistance';
            var sideColorClass = r.side === 'support' ? 'stream-side-support' : 'stream-side-resistance';
            var statusText = r.status === 'passed_through' ? 'Crossed' : (r.status.charAt(0).toUpperCase() + r.status.slice(1));
            var newStateHTML = '<span class="' + sideColorClass + '">' + sideText + '</span> \u00B7 ' + statusText;
            if (statusEl.innerHTML !== newStateHTML) {
                statusEl.innerHTML = newStateHTML;
                statusEl.className = 'stream-state stream-st-' + r.status;
                changed = true;
            }
        }

        if (changed) {
            el.classList.remove('stream-flash');
            void el.offsetWidth;
            el.classList.add('stream-flash');
        }
    }

    _snapshotStreamState(rows);
}

function _snapshotStreamState(rows) {
    _streamPrevState = {};
    for (var i = 0; i < rows.length; i++) {
        _streamPrevState[rows[i].key] = { proximity: rows[i].proximity, status: rows[i].status, momentum: rows[i].momentum };
    }
}


// ==================== Scanner → Backtester Modal ====================

function openScannerBacktestModal() {
    if (document.getElementById('scanner-bt-modal')) return;

    // Derive defaults from current scanner filters
    var verdict = _scannerVerdictFilter || 'STRONG,GOOD';
    var direction = _scannerDirFilter || 'long';
    if (direction === 'both') direction = 'long';  // MCP tool wants a concrete side

    var overlay = document.createElement('div');
    overlay.id = 'scanner-bt-modal';
    overlay.className = 'modal-overlay';
    overlay.style.display = 'flex';
    overlay.innerHTML =
        '<div class="modal-content" style="max-width:640px;width:90%;max-height:85vh;overflow:auto;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:16px">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">' +
                '<h3 style="margin:0;font-size:14px;color:#e6edf3">Test Formula Across Scanner Universe</h3>' +
                '<button class="sb-close" style="background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;padding:0 4px">&times;</button>' +
            '</div>' +
            '<div style="font-size:11px;color:#8b949e;margin-bottom:10px">Hold-N flip-signal backtest &mdash; for each matching asset, fire when the formula flips True, hold N bars, record forward return. Ranked by edge (hit rate &times; mean return).</div>' +
            '<div style="margin-bottom:8px">' +
                '<label style="font-size:11px;color:#8b949e;display:block;margin-bottom:2px">Entry formula</label>' +
                '<textarea id="sb-expr" rows="2" spellcheck="false" placeholder="CROSSOVER(EMA(9), EMA(21))" style="width:100%;font-family:monospace;font-size:12px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:3px;padding:6px;resize:vertical"></textarea>' +
                '<div id="sb-expr-status" style="font-size:10px;margin-top:2px;min-height:12px"></div>' +
            '</div>' +
            '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px">' +
                '<div><label style="font-size:10px;color:#8b949e">Verdict filter</label>' +
                    '<select id="sb-verdict" class="bt-select" style="width:100%">' +
                        '<option value="STRONG"' + (verdict === 'STRONG' ? ' selected' : '') + '>STRONG only</option>' +
                        '<option value="STRONG,GOOD"' + (verdict === 'STRONG,GOOD' ? ' selected' : '') + '>STRONG + GOOD</option>' +
                        '<option value="STRONG,GOOD,NEUTRAL"' + (verdict === 'STRONG,GOOD,NEUTRAL' ? ' selected' : '') + '>NEUTRAL+</option>' +
                    '</select></div>' +
                '<div><label style="font-size:10px;color:#8b949e">Direction</label>' +
                    '<select id="sb-direction" class="bt-select" style="width:100%">' +
                        '<option value="long"' + (direction === 'long' ? ' selected' : '') + '>Long</option>' +
                        '<option value="short"' + (direction === 'short' ? ' selected' : '') + '>Short</option>' +
                    '</select></div>' +
                '<div><label style="font-size:10px;color:#8b949e">Timeframe</label>' +
                    '<select id="sb-timeframe" class="bt-select" style="width:100%">' +
                        '<option value="1D" selected>1D</option>' +
                        '<option value="4h">4h</option>' +
                        '<option value="1h">1h</option>' +
                        '<option value="1W">1W</option>' +
                    '</select></div>' +
                '<div><label style="font-size:10px;color:#8b949e">Lookback (bars)</label>' +
                    '<input type="number" id="sb-lookback" class="bt-input" value="365" min="50" max="2000" step="10" style="width:100%"></div>' +
                '<div><label style="font-size:10px;color:#8b949e">Hold (bars)</label>' +
                    '<input type="number" id="sb-hold" class="bt-input" value="5" min="1" max="60" style="width:100%"></div>' +
                '<div><label style="font-size:10px;color:#8b949e">Max assets</label>' +
                    '<input type="number" id="sb-max" class="bt-input" value="10" min="1" max="25" style="width:100%"></div>' +
            '</div>' +
            '<div style="display:flex;gap:8px;margin-bottom:10px">' +
                '<button class="bt-run-btn" id="sb-run">Run Backtest</button>' +
                '<button class="bt-secondary-btn">Cancel</button>' +
            '</div>' +
            '<div id="sb-results" style="min-height:40px"></div>' +
        '</div>';

    // Click outside to close
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closeScannerBacktestModal();
    });

    document.body.appendChild(overlay);

    // One-shot modal (guarded above by the #scanner-bt-modal existence check) —
    // direct .onclick binds, matching the sb-expr input binding just below.
    var sbClose = overlay.querySelector('.sb-close');
    if (sbClose) sbClose.onclick = closeScannerBacktestModal;
    var sbCancel = overlay.querySelector('.bt-secondary-btn');
    if (sbCancel) sbCancel.onclick = closeScannerBacktestModal;
    var sbRun = document.getElementById('sb-run');
    if (sbRun) sbRun.onclick = runScannerBacktest;

    // Live formula validation
    var ta = document.getElementById('sb-expr');
    var status = document.getElementById('sb-expr-status');
    if (ta && window.FormulaParser) {
        ta.addEventListener('input', function() {
            var v = ta.value.trim();
            if (!v) { status.textContent = ''; status.style.color = ''; return; }
            var res = window.FormulaParser.parse(v);
            if (res.error) {
                status.textContent = res.error;
                status.style.color = '#ef5350';
            } else {
                status.textContent = 'Valid';
                status.style.color = '#26a69a';
            }
        });
    }
    setTimeout(function() { if (ta) ta.focus(); }, 20);
}

function closeScannerBacktestModal() {
    var el = document.getElementById('scanner-bt-modal');
    if (el) el.remove();
}

function runScannerBacktest() {
    var expr = (document.getElementById('sb-expr').value || '').trim();
    if (!expr) {
        if (typeof showToast === 'function') showToast('Enter a formula first');
        return;
    }
    if (window.FormulaParser) {
        var parse = window.FormulaParser.parse(expr);
        if (parse.error) {
            if (typeof showToast === 'function') showToast('Invalid formula: ' + parse.error);
            return;
        }
    }

    var body = {
        expression: expr,
        verdict: document.getElementById('sb-verdict').value,
        direction: document.getElementById('sb-direction').value,
        timeframe: document.getElementById('sb-timeframe').value,
        lookback_bars: parseInt(document.getElementById('sb-lookback').value) || 365,
        hold_bars: parseInt(document.getElementById('sb-hold').value) || 5,
        max_assets: parseInt(document.getElementById('sb-max').value) || 10,
    };

    var resultsEl = document.getElementById('sb-results');
    var runBtn = document.getElementById('sb-run');
    if (resultsEl) resultsEl.innerHTML = '<div style="padding:20px;text-align:center;color:#8b949e">Running backtest across ' + body.max_assets + ' assets\u2026 this can take 10-30 seconds on a cold cache.</div>';
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = 'Running\u2026'; }

    var csrfEl = document.querySelector('meta[name="csrf-token"]');
    var csrf = csrfEl ? csrfEl.getAttribute('content') : '';

    fetch('/api/scanner/backtest', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(resp) {
        if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Backtest'; }
        if (!resp.ok) {
            resultsEl.innerHTML = '<div style="padding:12px;color:#ef5350;font-size:12px">' + (resp.data.error || 'Request failed') + '</div>';
            return;
        }
        _renderScannerBacktestResults(resp.data, expr);
    })
    .catch(function(err) {
        if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Backtest'; }
        resultsEl.innerHTML = '<div style="padding:12px;color:#ef5350;font-size:12px">Network error</div>';
    });
}

function _renderScannerBacktestResults(data, formula) {
    var el = document.getElementById('sb-results');
    if (!el) return;
    var results = data.results || [];
    var errors = data.errors || [];

    if (results.length === 0) {
        el.innerHTML = '<div style="padding:12px;color:#8b949e;font-size:12px">' +
            (data.message || 'No assets matched, or no valid trades produced.') + '</div>';
        return;
    }

    var html = '<div style="font-size:11px;color:#8b949e;margin-bottom:6px">' +
        results.length + ' asset(s) tested' +
        (errors.length ? ' · ' + errors.length + ' errored' : '') +
        ' · ' + data.verdict + ' / ' + data.direction + ' / ' + data.timeframe +
        '</div>';
    html += '<table style="width:100%;font-size:11px;border-collapse:collapse">';
    html += '<thead><tr style="border-bottom:1px solid #30363d;color:#8b949e;text-align:left">' +
        '<th style="padding:4px 6px">Symbol</th>' +
        '<th style="padding:4px 6px">Trades</th>' +
        '<th style="padding:4px 6px">Hit</th>' +
        '<th style="padding:4px 6px">Mean</th>' +
        '<th style="padding:4px 6px">Median</th>' +
        '<th style="padding:4px 6px">Sharpe-like</th>' +
        '<th style="padding:4px 6px">Max DD</th>' +
        '<th style="padding:4px 6px"></th>' +
    '</tr></thead><tbody>';
    results.forEach(function(r) {
        var hit = r.hit_rate != null ? (r.hit_rate * 100).toFixed(1) + '%' : '\u2014';
        var hitCls = (r.hit_rate != null && r.hit_rate >= 0.5) ? 'color:#26a69a' : (r.hit_rate != null ? 'color:#ef5350' : 'color:#8b949e');
        var mean = r.mean_return != null ? ((r.mean_return * 100 >= 0 ? '+' : '') + (r.mean_return * 100).toFixed(2) + '%') : '\u2014';
        var median = r.median_return != null ? ((r.median_return * 100 >= 0 ? '+' : '') + (r.median_return * 100).toFixed(2) + '%') : '\u2014';
        var sharpe = r.sharpe_like != null ? r.sharpe_like.toFixed(2) : '\u2014';
        var dd = r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(1) + '%' : '\u2014';
        html += '<tr class="sb-result-row" style="border-bottom:1px solid #21262d">' +
            '<td style="padding:4px 6px;font-weight:600;color:#e6edf3">' + r.symbol + '</td>' +
            '<td style="padding:4px 6px;color:#c9d1d9">' + (r.trades || 0) + '</td>' +
            '<td style="padding:4px 6px;' + hitCls + ';font-weight:600">' + hit + '</td>' +
            '<td style="padding:4px 6px;color:#c9d1d9">' + mean + '</td>' +
            '<td style="padding:4px 6px;color:#c9d1d9">' + median + '</td>' +
            '<td style="padding:4px 6px;color:#c9d1d9">' + sharpe + '</td>' +
            '<td style="padding:4px 6px;color:#c9d1d9">' + dd + '</td>' +
            '<td style="padding:4px 6px"><button class="sb-open-btn" data-symbol="' + r.symbol + '" data-tf="' + data.timeframe + '" style="font-size:10px;padding:2px 6px;background:#1f6feb;color:#fff;border:none;border-radius:3px;cursor:pointer">Open</button></td>' +
        '</tr>';
    });
    html += '</tbody></table>';

    if (errors.length) {
        html += '<div style="margin-top:8px;font-size:10px;color:#8b949e">Errored: ' +
            errors.map(function(e) { return e.symbol + ' (' + (e.error || 'unknown') + ')'; }).join(', ') +
            '</div>';
    }

    el.innerHTML = html;

    // Wire row hover highlight (was inline onmouseover/onmouseout)
    el.querySelectorAll('.sb-result-row').forEach(function(row) {
        row.addEventListener('mouseover', function() { row.style.background = 'rgba(255,255,255,0.03)'; });
        row.addEventListener('mouseout', function() { row.style.background = ''; });
    });

    // Wire "Open" buttons — switch chart + open backtester + prefill
    el.querySelectorAll('.sb-open-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var symbol = btn.getAttribute('data-symbol');
            var tf = btn.getAttribute('data-tf') || '1D';
            closeScannerBacktestModal();
            try {
                if (typeof loadKlineChart === 'function') loadKlineChart(symbol, tf);
                if (typeof openDrawer === 'function') openDrawer('backtest');
            } catch (e) {}
            setTimeout(function() {
                var entry = document.getElementById('bt-entry');
                var exit = document.getElementById('bt-exit');
                if (entry) {
                    entry.value = formula;
                    entry.dispatchEvent(new Event('input', { bubbles: true }));
                }
                if (exit && !exit.value) {
                    exit.value = 'NOT (' + formula + ')';
                    exit.dispatchEvent(new Event('input', { bubbles: true }));
                }
                if (typeof showToast === 'function') showToast('Pre-filled from scanner — review exit formula before running');
            }, 150);
        });
    });
}
