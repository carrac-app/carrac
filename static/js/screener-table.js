/* Screener / main asset table engine: quick filter, loading-progress UX,
   filter/sort/render/cache/load and their delegated document listeners.
   Classic script, global scope. Reads allAssets, watchlistSymbols,
   timeframeMode, escapeHtml (app-core.js, shared-utils.js) and the
   watchlist.js / score-formatting.js helpers at call time; main.js's
   DOMContentLoaded init and the asset-group editor call in the other
   direction (renderFilteredTable, loadData, handleSort, ...), so this
   file must stay an eager script tag on both templates. */

// ── Screener state (used only by this engine) ──────────────
var collapsedSections = {};
try { collapsedSections = JSON.parse(localStorage.getItem('panelCollapsedSections') || '{}') || {}; } catch (e) { collapsedSections = {}; }
var allCollapsed = false;
var currentFilter = 'all';
var panelTypeFilter = 'all'; // 'all', 'crypto', 'misc' — set by panel tabs
var customFilterSignals = [];
// patternFilterSignals removed - v7.1 signal simplification

// Quick filter
var panelQuickFilter = '';

function _passesQuickFilter(asset, symbol) {
    if (!panelQuickFilter) return true;
    var q = panelQuickFilter;
    var sym = (symbol || asset.symbol || '').toUpperCase();
    if (sym.indexOf(q) !== -1) return true;
    var name = (asset && asset.name ? String(asset.name) : '').toUpperCase();
    return name.indexOf(q) !== -1;
}

// Absolute 24h change for an asset — prefers the backend-computed value,
// falls back to deriving it from change_pct so the column also works while
// the precompute cache predates the change_value field.
function _assetChangeValue(asset) {
    var rt = asset && asset.realtime;
    if (!rt) return null;
    if (rt.change_value !== null && rt.change_value !== undefined && !isNaN(rt.change_value)) return rt.change_value;
    var pct = rt.change_pct;
    var price = (rt.price !== undefined && rt.price !== null) ? rt.price : asset.latest_price;
    if (pct === null || pct === undefined || isNaN(pct) || !price) return null;
    var denom = 1 + pct / 100;
    if (!denom) return null;
    return price - price / denom;
}

// Sort state
var currentSort = { column: null, direction: 'asc' };
try {
    var _ps = JSON.parse(localStorage.getItem('panelSort') || 'null');
    if (_ps && _ps.column) currentSort = _ps;
} catch (e) {}
var watchlistSort = { column: null, direction: 'asc' };


// Dynamic quote cache for non-curated watchlist symbols
var _dynamicQuoteCache = {};

function _fetchDynamicQuote(symbol) {
    fetch('/api/quote/' + encodeURIComponent(symbol))
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data || data.error) return;
            _dynamicQuoteCache[symbol] = data;
            // Re-render if still on watchlist view
            if (panelTypeFilter === 'watchlist' || watchlistSymbols.indexOf(symbol) !== -1) {
                renderFilteredTable();
            }
        })
        .catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/quote/' + symbol + ' failed — row shows stale/no quote', String((e && e.message) || e)); });
}


// Loading status helpers
function startWizardMessages() {
    var statusEl = document.getElementById('loading-status');
    if (statusEl) statusEl.textContent = 'Loading market data...';
}

function stopWizardMessages(finalMessage) {
    var statusEl = document.getElementById('loading-status');
    if (statusEl) statusEl.textContent = finalMessage || '';
}

function startRefreshWizardMessages() {
    var statusEl = document.getElementById('refresh-loading-status');
    if (statusEl) statusEl.textContent = 'Refreshing...';
}

function stopRefreshWizardMessages(finalMessage) {
    var statusEl = document.getElementById('refresh-loading-status');
    if (statusEl) statusEl.textContent = finalMessage || '';
}

// Progress bar helpers for loading phases
var progressAnimationId = null;
var refreshProgressAnimationId = null;

// Phase definitions - two sets for instant vs computing responses
var loadingPhasesInstant = {
    connecting: { target: 30, duration: 200 },
    downloading: { target: 70, duration: 300 },
    processing: { target: 95, duration: 200 },
    complete: { target: 100, duration: 150 }
};

var loadingPhasesComputing = {
    connecting: { target: 15, duration: 1500 },
    computing: { target: 50, duration: 30000 },  // Server is computing data
    downloading: { target: 75, duration: 5000 },
    processing: { target: 90, duration: 2000 },
    complete: { target: 100, duration: 300 }
};

// Current mode - starts as instant, switches to computing if needed
var currentLoadingMode = 'instant';

function setLoadingProgress(barId, phaseId, phase, forceMode) {
    var bar = document.getElementById(barId);
    var phaseEl = document.getElementById(phaseId);
    if (!bar) return;

    var phases = (forceMode || currentLoadingMode) === 'computing'
        ? loadingPhasesComputing
        : loadingPhasesInstant;
    var config = phases[phase];
    if (!config) return;

    var phaseTexts = {
        connecting: 'Connecting...',
        computing: 'Computing...',
        downloading: 'Downloading...',
        processing: 'Processing...',
        complete: 'Done'
    };
    if (phaseEl) {
        phaseEl.textContent = phaseTexts[phase] || '';
    }

    // Animate progress bar
    bar.style.transition = 'width ' + (config.duration / 1000) + 's ease-out';
    bar.style.width = config.target + '%';
}

function setLoadingMode(mode) {
    currentLoadingMode = mode;
}

function startLoadingProgress() {
    // Reset progress
    var bar = document.getElementById('loading-progress-bar');
    var phaseEl = document.getElementById('loading-progress-phase');
    if (bar) {
        bar.style.transition = 'none';
        bar.style.width = '0%';
        // Force reflow
        bar.offsetHeight;
    }
    if (phaseEl) phaseEl.textContent = 'Connecting...';

    // Start connecting phase
    setTimeout(function() {
        setLoadingProgress('loading-progress-bar', 'loading-progress-phase', 'connecting');
    }, 50);
}

function updateLoadingPhase(phase) {
    setLoadingProgress('loading-progress-bar', 'loading-progress-phase', phase);
}

function startRefreshProgress() {
    // Reset progress
    var bar = document.getElementById('refresh-progress-bar');
    var phaseEl = document.getElementById('refresh-progress-phase');
    if (bar) {
        bar.style.transition = 'none';
        bar.style.width = '0%';
        // Force reflow
        bar.offsetHeight;
    }
    if (phaseEl) phaseEl.textContent = 'Connecting...';

    // Start connecting phase
    setTimeout(function() {
        setLoadingProgress('refresh-progress-bar', 'refresh-progress-phase', 'connecting');
    }, 50);
}

function updateRefreshPhase(phase) {
    setLoadingProgress('refresh-progress-bar', 'refresh-progress-phase', phase);
}

function updateStarIcons() {
    // Only stars that name a symbol. The details drawer renders its
    // own `.star` (#drStar) with no data-symbol and keeps it in sync itself —
    // its CSS is `#cd-drawer .dh-top .star.on`, not the `.active` this sets, so
    // this loop never maintained it, it only crashed on it.
    document.querySelectorAll('.star[data-symbol]').forEach(function(star) {
        var symbol = star.getAttribute('data-symbol');
        if (isInWatchlist(symbol)) {
            star.classList.add('active');
            star.innerHTML = '&#9733;';
        } else {
            star.classList.remove('active');
            star.innerHTML = '&#9734;';
        }
    });
}

// Watchlist rows are now rendered inside #data-table by renderFilteredTable()
function updateWatchlistUI() {
    renderFilteredTable();
    updateStarIcons();
}

// Price formatting → price-formatting.js

function setFilter(filter) {
    currentFilter = filter;

    // Update button states
    document.querySelectorAll('.filter-btn').forEach(function(btn) {
        if (btn.getAttribute('data-filter') === filter) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    // Handle custom filter button state separately
    var customBtn = document.getElementById('custom-filter-btn');
    if (customBtn) {
        if (filter === 'custom') {
            customBtn.classList.add('active');
        } else {
            customBtn.classList.remove('active');
        }
    }

    // Re-render the table with filter applied
    renderFilteredTable();
    // Also update watchlist with filter
    updateWatchlistUI();
}

// Filter settings dropdown toggle
function toggleFilterSettings() {
    var panel = document.getElementById('filter-settings-panel');
    if (!panel) return;
    var isOpen = panel.style.display !== 'none';
    panel.style.display = isOpen ? 'none' : '';
    if (!isOpen) {
        // Position fixed dropdown below the gear button
        var btn = document.getElementById('filter-settings-btn');
        if (btn) {
            var rect = btn.getBoundingClientRect();
            panel.style.top = rect.bottom + 'px';
            panel.style.right = (window.innerWidth - rect.right) + 'px';
            panel.style.left = 'auto';
        }
        // Close on outside click
        setTimeout(function() {
            document.addEventListener('click', function closeSettings(e) {
                if (!panel.contains(e.target) && e.target.id !== 'filter-settings-btn') {
                    panel.style.display = 'none';
                    document.removeEventListener('click', closeSettings);
                }
            });
        }, 0);
    }
}

// Custom filter dropdown functions
function toggleCustomFilter() {
    var dropdown = document.getElementById('custom-filter-dropdown');
    var btn = document.getElementById('custom-filter-btn');
    if (dropdown && btn) {
        dropdown.classList.toggle('open');
        btn.classList.toggle('dropdown-open');
    }
}

function updateCustomFilter() {
    var checkboxes = document.querySelectorAll('#custom-filter-dropdown input[type="checkbox"]:checked');
    customFilterSignals = [];
    checkboxes.forEach(function(cb) {
        customFilterSignals.push(cb.value);
    });

    // Update the count badge
    var countEl = document.getElementById('custom-filter-count');
    if (countEl) {
        countEl.textContent = customFilterSignals.length > 0 ? customFilterSignals.length : '';
    }

    // If signals are selected, automatically set custom filter
    if (customFilterSignals.length > 0) {
        setFilter('custom');
    }
}

function clearCustomFilter() {
    // Uncheck all checkboxes
    var checkboxes = document.querySelectorAll('#custom-filter-dropdown input[type="checkbox"]');
    checkboxes.forEach(function(cb) {
        cb.checked = false;
    });
    customFilterSignals = [];

    // Update the count badge
    var countEl = document.getElementById('custom-filter-count');
    if (countEl) {
        countEl.textContent = '';
        countEl.classList.remove('visible');
    }

    // Close dropdown and remove active state
    var dropdown = document.getElementById('custom-filter-dropdown');
    var btn = document.getElementById('custom-filter-btn');
    if (dropdown) dropdown.classList.remove('open');
    if (btn) btn.classList.remove('dropdown-open', 'active');

    // Switch to 'all' filter when custom filter is cleared
    // (pattern filter removed - v7.1 signal simplification)
    setFilter('all');
}

function selectAllCustomFilter() {
    // Check all checkboxes
    var checkboxes = document.querySelectorAll('#custom-filter-dropdown input[type="checkbox"]');
    checkboxes.forEach(function(cb) {
        cb.checked = true;
    });
    // Trigger update
    updateCustomFilter();
}

// Pattern filter dropdown functions removed - v7.1 signal simplification
// togglePatternFilter, updatePatternFilter, clearPatternFilter, selectAllPatternFilter removed

// View dropdown functions
function toggleColumnsFilter() {
    var dropdown = document.getElementById('columns-filter-dropdown');
    var btn = document.getElementById('columns-filter-btn');
    if (dropdown && btn) {
        dropdown.classList.toggle('open');
        btn.classList.toggle('dropdown-open');
    }
}

function toggleDisplayFilter() {
    var dropdown = document.getElementById('display-filter-dropdown');
    var btn = document.getElementById('display-filter-btn');
    if (dropdown && btn) {
        dropdown.classList.toggle('open');
        btn.classList.toggle('dropdown-open');
    }
}

// More menu dropdown (Notify, Install, Admin)
function toggleMoreMenu() {
    var dropdown = document.getElementById('more-menu-dropdown');
    var btn = document.getElementById('more-menu-btn');
    if (dropdown && btn) {
        dropdown.classList.toggle('active');
        btn.classList.toggle('active');
        btn.setAttribute('aria-expanded', String(dropdown.classList.contains('active')));
    }
}

function toggleColumnVisibility(column) {
    var checkbox = document.getElementById('view-' + column);
    var isVisible = checkbox ? checkbox.checked : true;
    var tables = document.querySelectorAll('#data-table, .watchlist-table');

    tables.forEach(function(table) {
        if (isVisible) {
            table.classList.remove('hide-' + column);
        } else {
            table.classList.add('hide-' + column);
        }
    });

    // Save preference to localStorage
    localStorage.setItem('view-' + column, isVisible ? 'visible' : 'hidden');

    // WS tick handlers consult this flag so hidden Chg $ cells cost nothing per tick
    if (column === 'changeval') window._changevalVisible = isVisible;

    // Sync column visibility to server
    if (window.PreferenceSync) {
        var allCols = ['wrsi', 'cpr', 'bb', 'trend4h', 'macd', 'adx', 'atr', 'vol', 'signals', 'name',
                       'ichimoku', 'supertrend', 'psar', 'emacross', 'alligator', 'stochrsi',
                       'ttmsqueeze', 'donchian', 'nr7', 'bbwpct', 'atrpctrank', 'obv', 'fvg', 'vplevels', 'rsidiv', 'changeval'];
        var visible = allCols.filter(function(c) { return localStorage.getItem('view-' + c) === 'visible' || (!localStorage.getItem('view-' + c) && c !== 'name' && allCols.indexOf(c) < 10); });
        PreferenceSync.syncPreference('column_prefs', { visible: visible });
    }
}

// Load column visibility preferences on page load
function loadColumnVisibilityPreferences() {
    var columns = ['wrsi', 'cpr', 'bb', 'trend4h', 'macd', 'adx', 'atr', 'vol', 'signals'];
    columns.forEach(function(column) {
        var pref = localStorage.getItem('view-' + column);
        var checkbox = document.getElementById('view-' + column);
        if (checkbox) {
            if (pref === 'hidden') {
                checkbox.checked = false;
                toggleColumnVisibility(column);
            }
        }
    });

    // Name column is hidden by default (opt-in)
    var namePref = localStorage.getItem('view-name');
    var nameCheckbox = document.getElementById('view-name');
    if (nameCheckbox) {
        if (namePref === 'visible') {
            nameCheckbox.checked = true;
        } else {
            nameCheckbox.checked = false;
            toggleColumnVisibility('name');
        }
    }

    // Extended columns — hidden by default (opt-in, like Name)
    var extColumns = ['changeval', 'ichimoku', 'supertrend', 'psar', 'emacross', 'alligator', 'stochrsi',
                      'ttmsqueeze', 'donchian', 'nr7', 'bbwpct', 'atrpctrank', 'obv', 'fvg', 'vplevels', 'rsidiv'];
    extColumns.forEach(function(col) {
        var pref = localStorage.getItem('view-' + col);
        var cb = document.getElementById('view-' + col);
        if (cb) {
            if (pref === 'visible') {
                cb.checked = true;
            } else {
                cb.checked = false;
                toggleColumnVisibility(col);
            }
        }
    });

    window._changevalVisible = localStorage.getItem('view-changeval') === 'visible';
}

// Close all dropdowns when clicking outside (single delegated handler)
document.addEventListener('click', function(e) {
    // More menu
    var moreWrapper = document.querySelector('.more-menu-wrapper');
    var moreDropdown = document.getElementById('more-menu-dropdown');
    if (moreWrapper && moreDropdown && !moreWrapper.contains(e.target)) {
        moreDropdown.classList.remove('active');
        var moreBtn = document.getElementById('more-menu-btn');
        if (moreBtn) moreBtn.classList.remove('active');
    }

    // Custom filter dropdown
    var customWrapper = document.querySelector('#custom-filter-btn');
    if (customWrapper) customWrapper = customWrapper.closest('.custom-filter-wrapper');
    var customDropdown = document.getElementById('custom-filter-dropdown');
    if (customWrapper && customDropdown && !customWrapper.contains(e.target)) {
        customDropdown.classList.remove('open');
        var btn = document.getElementById('custom-filter-btn');
        if (btn) btn.classList.remove('dropdown-open');
    }

    // Columns filter dropdown
    var columnsWrapper = document.querySelector('#columns-filter-btn');
    if (columnsWrapper) columnsWrapper = columnsWrapper.closest('.custom-filter-wrapper');
    var columnsDropdown = document.getElementById('columns-filter-dropdown');
    if (columnsWrapper && columnsDropdown && !columnsWrapper.contains(e.target)) {
        columnsDropdown.classList.remove('open');
        var cbtn = document.getElementById('columns-filter-btn');
        if (cbtn) cbtn.classList.remove('dropdown-open');
    }

    // Display filter dropdown
    var displayWrapper = document.querySelector('#display-filter-btn');
    if (displayWrapper) displayWrapper = displayWrapper.closest('.custom-filter-wrapper');
    var displayDropdown = document.getElementById('display-filter-dropdown');
    if (displayWrapper && displayDropdown && !displayWrapper.contains(e.target)) {
        displayDropdown.classList.remove('open');
        var dbtn = document.getElementById('display-filter-btn');
        if (dbtn) dbtn.classList.remove('dropdown-open');
    }
});

// --- Display Options ---

// Display options + timeframe mode → display-options.js

function passesCheckboxFilters(asset) {
    // Check panel type tab filter
    if (panelTypeFilter === 'crypto') {
        if (asset.type !== 'crypto' && asset.type !== 'crypto_metric') {
            return false;
        }
    } else if (panelTypeFilter === 'misc') {
        if (asset.type !== 'index' && asset.type !== 'forex' && asset.type !== 'stock' && asset.type !== 'etf' && asset.type !== 'commodity') {
            return false;
        }
    }
    return true;
}

// Sorting functions
function getSortValue(asset, column) {
    switch (column) {
        case 'symbol': return asset.symbol || '';
        case 'name': return asset.name || '';
        case 'price': return asset.latest_price || 0;
        case 'consensio': return asset.positions || 0;
        case 'signal_1d': return (asset.consensio && asset.consensio['1D'] && asset.consensio['1D'].signal) || 0;
        case 'signal_3d': return (asset.consensio && asset.consensio['3D'] && asset.consensio['3D'].signal) || 0;
        case 'signal_1w': return (asset.consensio && asset.consensio['1W'] && asset.consensio['1W'].signal) || 0;
        case 'rsi': return asset.weekly_rsi || 0;
        case 'ema_ribbon':
            var trend = asset.ema_ribbon && asset.ema_ribbon.trend;
            if (trend === 'bull') return 2;
            if (trend === 'neutral') return 1;
            if (trend === 'bear') return 0;
            return -1;
        case 'changed':
            // Prefer numeric days_since_flip (converted to an equivalent
            // timestamp so ordering matches the date-based fallback below);
            // fall back to parsing the last_change date string.
            var ribbonForSort = asset.ema_ribbon;
            var daysSinceFlipSort = ribbonForSort && typeof ribbonForSort.days_since_flip === 'number'
                ? ribbonForSort.days_since_flip : null;
            if (daysSinceFlipSort !== null) {
                return Date.now() - daysSinceFlipSort * 86400000;
            }
            var date = ribbonForSort && ribbonForSort.last_change;
            if (!date) return 0;
            // Parse YY-MM-DD format
            var parts = date.split('-');
            if (parts.length === 3) {
                return new Date('20' + parts[0], parts[1] - 1, parts[2]).getTime();
            }
            return 0;
        case 'signals': return getSignalCount(asset);
        // 'patterns' case removed - v7.1 signal simplification
        case 'structure':
            // Sort by structure trend: bullish=2, neutral=1, bearish=0
            var structureTrend = asset.structure && asset.structure.structure_trend;
            if (structureTrend === 'bullish') return 2;
            if (structureTrend === 'neutral') return 1;
            if (structureTrend === 'bearish') return 0;
            return -1;
        case 'cpr':
            // Sort by CPR level order (bullish at bottom: Below S3=1, Above R3=10)
            var cprLevel = asset.cpr && asset.cpr.price_level;
            var cprOrder = {'Below S3': 1, 'S3-S2': 2, 'S2-S1': 3, 'S1-BC': 4, 'BC-CP': 5, 'CP-TC': 6, 'TC-R1': 7, 'R1-R2': 8, 'R2-R3': 9, 'Above R3': 10};
            return cprOrder[cprLevel] || 0;
        case 'bb':
            // Sort by BB squeeze percentage (lower = more squeezed)
            return (asset.bb && asset.bb.squeeze_pct) || 999;
        case 'macd':
            return (asset.macd && asset.macd.histogram) || 0;
        case 'adx':
            return (asset.adx && asset.adx.adx) || 0;
        case 'atr':
            return (asset.adx && asset.adx.atr_pct) || 0;
        case 'vol':
            return (asset.volume_metrics && asset.volume_metrics.ratio) || 0;
        case 'change':
            var chg = asset.realtime && asset.realtime.change_pct;
            return (chg !== null && chg !== undefined && !isNaN(chg)) ? chg : 0;
        default: return 0;
    }
}

// Whole-row click opens the details drawer (flag-gated) — this glyph only
// advertises that, so emit it only when the drawer is live. No data-action:
// it must stay in the row's fallthrough click zone.
function _rowOverviewHintHtml() {
    return (window._features && window._features.detailsDrawer)
        ? '<span class="row-overview-hint" title="Asset overview"></span>' : '';
}

function sortAssets(assets, sortState) {
    if (!sortState.column) return assets;

    return assets.slice().sort(function(a, b) {
        var valA = getSortValue(a, sortState.column);
        var valB = getSortValue(b, sortState.column);

        // String comparison for text columns
        if (typeof valA === 'string' && typeof valB === 'string') {
            valA = valA.toLowerCase();
            valB = valB.toLowerCase();
        }

        var result = 0;
        if (valA < valB) result = -1;
        else if (valA > valB) result = 1;

        return sortState.direction === 'desc' ? -result : result;
    });
}

function handleSort(column, isWatchlist) {
    // The Watchlist TAB (shares #data-table) defaults to the user's manual
    // drag order, so its sort is a 3-state cycle asc → desc → manual
    // (column=null), tracked in watchlistSort — separate from the other
    // tabs' currentSort and deliberately session-only (drag order is the
    // persistent truth; a persisted sort would silently shadow it).
    var isWlTab = !isWatchlist && typeof panelTypeFilter !== 'undefined' && panelTypeFilter === 'watchlist';
    var sortState = (isWatchlist || isWlTab) ? watchlistSort : currentSort;

    // Toggle direction if same column, otherwise reset to asc
    if (sortState.column === column) {
        if (isWlTab && sortState.direction === 'desc') {
            sortState.column = null;
            sortState.direction = 'asc';
            if (typeof showToast === 'function') showToast('Watchlist: back to manual order');
        } else {
            sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
        }
    } else {
        sortState.column = column;
        sortState.direction = 'asc';
    }

    if (!isWatchlist && !isWlTab) {
        try { localStorage.setItem('panelSort', JSON.stringify(currentSort)); } catch (e) {}
    }

    // Update sort indicators
    updateSortIndicators(isWatchlist);

    // Re-render
    if (isWatchlist) {
        updateWatchlistUI();
    } else {
        renderFilteredTable();
    }
}

function updateSortIndicators(isWatchlist) {
    var tableId = isWatchlist ? 'watchlist-table' : 'data-table';
    var sortState = isWatchlist ? watchlistSort : currentSort;
    // Watchlist TAB shares #data-table but sorts via watchlistSort
    if (!isWatchlist && typeof panelTypeFilter !== 'undefined' && panelTypeFilter === 'watchlist') {
        sortState = watchlistSort;
    }
    var table = document.getElementById(tableId);
    if (!table) return;

    // Remove all sort classes
    table.querySelectorAll('th.sortable').forEach(function(th) {
        th.classList.remove('sort-asc', 'sort-desc');
    });

    // Add current sort class
    if (sortState.column) {
        var th = table.querySelector('th[data-sort="' + sortState.column + '"]');
        if (th) {
            th.classList.add('sort-' + sortState.direction);
        }
    }
}

function renderFilteredTable() {
    var tbody = document.getElementById("table-body");
    var html = "";

    // Hide old watchlist section (merged into main table)
    var oldWatchlistSection = document.getElementById('watchlist-section');
    if (oldWatchlistSection) oldWatchlistSection.style.display = 'none';

    var filterFn = filterPresets[currentFilter] || filterPresets['all'];

    // Watchlist-only mode: when panelTypeFilter === 'watchlist'
    if (panelTypeFilter === 'watchlist') {
        // The watchlist tab has no '★ Watchlist' section header, so a collapse
        // state persisted from another tab would hide every row with nothing
        // to click to expand — clear the stale key here.
        if (collapsedSections['watchlist']) {
            collapsedSections['watchlist'] = false;
            try { localStorage.setItem('panelCollapsedSections', JSON.stringify(collapsedSections)); } catch (e) {}
        }
        // Shared per-symbol row builder — emits the same row markup for the flat
        // and grouped lists; `grouped` only adds the
        // data-watchlist-group attribute and picks the per-group section key
        // ('group:<id>' vs the legacy 'watchlist' key) so collapse state and
        // the single-group path stay byte-identical to the pre-groups output.
        var _renderWlRow = function(symbol, gid, idx, grouped) {
            var asset = allAssets[symbol];
            var sectionKey = grouped ? ('group:' + gid) : 'watchlist';
            if (asset) {
                var row = createRow(asset, asset.type, true, grouped ? sectionKey : undefined);
                var attrs = 'data-watchlist-idx="' + idx + '"' + (grouped ? ' data-watchlist-group="' + escapeHtml(gid) + '"' : '');
                row = row.replace('<tr ', '<tr ' + (_wlSortActive ? '' : 'draggable="true" ') + attrs + ' ');
                html += row;
            } else {
                // Dynamic symbol not in allAssets — show row with price cells, fetch async
                var cached = _dynamicQuoteCache[symbol];
                var symEsc = escapeHtml(symbol);
                var pCell = cached ? formatPrice(cached.price, symbol, 'stock') : '<span class="dynamic-quote-loading" data-symbol="' + symEsc + '">…</span>';
                var cCell = '';
                if (cached && cached.change_pct !== null && cached.change_pct !== undefined) {
                    var cClass = cached.change_pct >= 0 ? 'price-change-up' : 'price-change-down';
                    var cSign = cached.change_pct >= 0 ? '+' : '';
                    cCell = '<span class="price-change ' + cClass + '">' + cSign + (typeof _fmtN === 'function' ? _fmtN(cached.change_pct, 2) : cached.change_pct.toFixed(2)) + '%</span>';
                }
                var nCell = cached ? escapeHtml(cached.name || '') : '';
                var cvCell = '';
                if (cached && cached.change_pct !== null && cached.change_pct !== undefined && cached.price) {
                    var _dn = 1 + cached.change_pct / 100;
                    if (_dn) {
                        var _cv = cached.price - cached.price / _dn;
                        var _cvCls = _cv >= 0 ? 'price-change-up' : 'price-change-down';
                        cvCell = '<span class="price-change ' + _cvCls + '">' + (typeof formatChangeValue === 'function' ? formatChangeValue(_cv, symbol, 'stock') : '') + '</span>';
                    }
                }
                var section = escapeHtml(sectionKey);
                var groupAttr = grouped ? (' data-watchlist-group="' + escapeHtml(gid) + '"') : '';
                var hiddenAttr = collapsedSections[sectionKey] ? ' class="hidden"' : '';
                html += '<tr data-section="' + section + '" data-symbol="' + symEsc + '" ' + (_wlSortActive ? '' : 'draggable="true" ') + 'data-watchlist-idx="' + idx + '"' + groupAttr + hiddenAttr + ' style="cursor:pointer;">' +
                    '<td class="symbol"><span class="symbol-chart" data-symbol="' + symEsc + '" data-action="chart-preview">' + _assetLogoImg(symbol, 'stock') + '<span class="symbol-name">' + symEsc + '</span></span>' +
                    '<span class="star active" data-symbol="' + symEsc + '" data-action="toggle-watchlist">&#9733;</span>' + _rowOverviewHintHtml() + '</td>' +
                    '<td class="name-col">' + nCell + '</td>' +
                    '<td class="price">' + pCell + '</td>' +
                    '<td class="change-col">' + cCell + '</td>' +
                    '<td class="change-value-col">' + cvCell + '</td>' +
                    '<td class="consensio-col">—</td><td class="signal-1d-col">—</td><td class="signal-3d-col">—</td><td class="signal-1w-col">—</td>' +
                    '<td class="rsi-col">—</td><td class="cpr-col">—</td><td class="bb-col">—</td><td class="ema-ribbon-col">—</td>' +
                    '<td class="changed-col">—</td><td class="structure-col">—</td><td class="signals-col">—</td></tr>';
                if (!cached) _fetchDynamicQuote(symbol);
            }
        };

        // Watchlist-tab column sort (watchlistSort; column=null = manual drag
        // order). While a sort is active, drag is disabled — a drop from
        // sorted row positions would silently rewrite the manual order.
        var _wlSortActive = !!watchlistSort.column;
        var _wlSortAsset = function(symbol) {
            var a = allAssets[symbol];
            if (a) return a;
            var c = _dynamicQuoteCache[symbol] || {};
            return { symbol: symbol, name: c.name || symbol, latest_price: c.price,
                     realtime: { price: c.price, change_pct: c.change_pct } };
        };
        var _wlSorted = function(symbols) {
            if (!_wlSortActive) return symbols;
            return symbols.slice().sort(function(s1, s2) {
                var v1 = getSortValue(_wlSortAsset(s1), watchlistSort.column);
                var v2 = getSortValue(_wlSortAsset(s2), watchlistSort.column);
                if (v1 < v2) return watchlistSort.direction === 'asc' ? -1 : 1;
                if (v1 > v2) return watchlistSort.direction === 'asc' ? 1 : -1;
                return 0;
            });
        };

        var _wlEmptyMsg = panelQuickFilter
            ? 'No assets match your filter.'
            : 'Your watchlist is empty. Switch to the <b>All</b> tab to find assets.';
        if (typeof watchlistGroups !== 'undefined' && watchlistGroups.length > 1) {
            watchlistGroups.forEach(function(g) {
                var visible = (g.symbols || []).filter(function(s) {
                    return _passesQuickFilter(allAssets[s] || _dynamicQuoteCache[s] || {}, s);
                });
                if (!visible.length && panelQuickFilter) return; // hide empty group while filtering
                var gKey = 'group:' + g.id;
                var isCollapsed = collapsedSections[gKey];
                html += '<tr class="section-header' + (isCollapsed ? ' collapsed' : '') + '" data-type="' + escapeHtml(gKey) + '" data-action="toggle-section">' +
                    '<td colspan="35"><span class="toggle-icon">▼</span>' + escapeHtml(g.name) +
                    '<span class="count">(' + visible.length + ')</span></td></tr>';
                _wlSorted(g.symbols || []).forEach(function(symbol, gi) {
                    if (!_passesQuickFilter(allAssets[symbol] || _dynamicQuoteCache[symbol] || {}, symbol)) return;
                    _renderWlRow(symbol, g.id, gi, true);
                });
            });
            if (!html) {
                html += '<tr><td colspan="35" style="text-align:center;color:#8b949e;padding:24px;">' + _wlEmptyMsg + '</td></tr>';
            }
        } else if (watchlistSymbols.length > 0) {
            var wIdx = 0;
            _wlSorted(watchlistSymbols).forEach(function(symbol) {
                if (_passesQuickFilter(allAssets[symbol] || _dynamicQuoteCache[symbol] || {}, symbol)) {
                    _renderWlRow(symbol, 'default', wIdx, false);
                }
                wIdx++;
            });
            if (!html) {
                html += '<tr><td colspan="35" style="text-align:center;color:#8b949e;padding:24px;">' + _wlEmptyMsg + '</td></tr>';
            }
        } else {
            html += '<tr><td colspan="35" style="text-align:center;color:#8b949e;padding:24px;">Your watchlist is empty. Switch to the <b>All</b> tab to find assets.</td></tr>';
        }
        tbody.innerHTML = html;
        updateStarIcons();
        updateSortIndicators(false);
        return;
    }

    // Render watchlist rows at the top (for non-watchlist tabs)
    if (watchlistSymbols.length > 0 && panelTypeFilter !== 'watchlist') {
        var watchlistAssets = [];
        var wIdx = 0;
        watchlistSymbols.forEach(function(symbol) {
            var asset = allAssets[symbol];
            if (asset && filterFn(asset) && passesCheckboxFilters(asset) && _passesQuickFilter(asset)) {
                watchlistAssets.push({ asset: asset, idx: wIdx });
            }
            wIdx++;
        });
        if (watchlistAssets.length > 0) {
            var isCollapsed = collapsedSections['watchlist'];
            html += '<tr class="section-header' + (isCollapsed ? ' collapsed' : '') + '" data-type="watchlist" data-action="toggle-section">' +
                '<td colspan="35"><span class="toggle-icon">▼</span>&#9733; Watchlist<span class="count">(' + watchlistAssets.length + ')</span></td></tr>';
            watchlistAssets.forEach(function(item) {
                var row = createRow(item.asset, item.asset.type, true);
                // Add draggable and watchlist index
                row = row.replace('<tr ', '<tr draggable="true" data-watchlist-idx="' + item.idx + '" ');
                html += row;
            });
        }
    }

    // Group by type
    var groups = {};
    sectionOrder.forEach(function(type) { groups[type] = []; });

    Object.keys(allAssets).forEach(function(symbol) {
        var a = allAssets[symbol];
        var type = a.type || "stock";
        if (!groups[type]) groups[type] = [];
        if (filterFn(a) && passesCheckboxFilters(a) && _passesQuickFilter(a)) {
            groups[type].push(a);
        }
    });

    // Render each group in order
    sectionOrder.forEach(function(type) {
        if (groups[type] && groups[type].length > 0) {
            html += createSectionHeader(type, groups[type].length);
            var sortedAssets = sortAssets(groups[type], currentSort);
            sortedAssets.forEach(function(asset) {
                html += createRow(asset, type, false);
            });
        }
    });

    tbody.innerHTML = html;
    updateStarIcons();
    updateSortIndicators(false);
}

function updateFilterCounts(assets) {
    var counts = {
        'any_signal': 0,
        // 'any_pattern' removed - v7.1 signal simplification
        'emerging_bull': 0,
        'emerging_bear': 0,
        'fresh': 0,
        'rsi_extreme': 0,
        'tightening': 0,
        'alerts': 0
    };

    assets.forEach(function(a) {
        if (filterPresets['any_signal'](a)) counts['any_signal']++;
        // any_pattern count removed - v7.1 signal simplification
        if (filterPresets['emerging_bull'](a)) counts['emerging_bull']++;
        if (filterPresets['emerging_bear'](a)) counts['emerging_bear']++;
        if (filterPresets['fresh'](a)) counts['fresh']++;
        if (filterPresets['rsi_extreme'](a)) counts['rsi_extreme']++;
        if (filterPresets['tightening'](a)) counts['tightening']++;
        if (filterPresets['alerts'](a)) counts['alerts']++;
    });

    var el;
    el = document.getElementById('count-any-signal');
    if (el) el.textContent = counts['any_signal'] > 0 ? ' (' + counts['any_signal'] + ')' : '';
    // count-any-pattern removed - v7.1 signal simplification
    el = document.getElementById('count-emerging-bull');
    if (el) el.textContent = counts['emerging_bull'] > 0 ? ' (' + counts['emerging_bull'] + ')' : '';
    el = document.getElementById('count-emerging-bear');
    if (el) el.textContent = counts['emerging_bear'] > 0 ? ' (' + counts['emerging_bear'] + ')' : '';
    el = document.getElementById('count-fresh');
    if (el) el.textContent = counts['fresh'] > 0 ? ' (' + counts['fresh'] + ')' : '';
    el = document.getElementById('count-rsi-extreme');
    if (el) el.textContent = counts['rsi_extreme'] > 0 ? ' (' + counts['rsi_extreme'] + ')' : '';
    el = document.getElementById('count-tightening');
    if (el) el.textContent = counts['tightening'] > 0 ? ' (' + counts['tightening'] + ')' : '';
    el = document.getElementById('count-alerts');
    if (el) el.textContent = counts['alerts'] > 0 ? ' (' + counts['alerts'] + ')' : '';
}

function toggleSection(type) {
    collapsedSections[type] = !collapsedSections[type];
    try { localStorage.setItem('panelCollapsedSections', JSON.stringify(collapsedSections)); } catch (e) {}
    updateSectionVisibility(type);
}

function updateSectionVisibility(type) {
    var isCollapsed = collapsedSections[type];
    var header = document.querySelector('.section-header[data-type="' + type + '"]');
    var rows = document.querySelectorAll('tr[data-section="' + type + '"]');

    if (header) {
        if (isCollapsed) {
            header.classList.add('collapsed');
        } else {
            header.classList.remove('collapsed');
        }
    }

    rows.forEach(function(row) {
        if (isCollapsed) {
            row.classList.add('hidden');
        } else {
            row.classList.remove('hidden');
        }
    });
}

function toggleCollapseAll() {
    allCollapsed = !allCollapsed;
    var btn = document.getElementById('collapse-toggle');

    // Include watchlist in collapse-all toggle
    ['watchlist'].concat(sectionOrder).forEach(function(type) {
        collapsedSections[type] = allCollapsed;
        updateSectionVisibility(type);
    });
    try { localStorage.setItem('panelCollapsedSections', JSON.stringify(collapsedSections)); } catch (e) {}

    if (allCollapsed) {
        btn.textContent = '▶';
        btn.classList.add('collapsed');
    } else {
        btn.textContent = '▼';
        btn.classList.remove('collapsed');
    }
}

// formatEmaRibbon → price-formatting.js

function createSectionHeader(type, count) {
    var typeLabel = sectionLabels[type] || type;
    var isCollapsed = collapsedSections[type];
    var safeType = escapeHtml(type);
    return '<tr class="section-header' + (isCollapsed ? ' collapsed' : '') + '" data-type="' + safeType + '" data-action="toggle-section">' +
        '<td colspan="35">' +
        '<span class="toggle-icon">▼</span>' +
        escapeHtml(typeLabel) +
        '<span class="count">(' + count + ')</span>' +
        '</td></tr>';
}

// ── Asset logos (markets list) ───────────────────────────────────────────
// Real per-asset logos with the candlestick glyph as the graceful fallback:
//   crypto    → jsDelivr cryptocurrency-icons (by base symbol)
//   stock/etf → Parqet logos (by ticker)
//   other     → no logo URL; the CSS glyph fallback shows instead.
function _assetLogoUrl(symbol, type) {
    if (!symbol) return null;
    var t = (type || '').toLowerCase();
    if (t === 'crypto') {
        // Symbols are bare tickers here (BTC, ETH, SUI). Strip a trailing fiat/
        // stablecoin quote ONLY if present (pair format like BTCUSDT) — never BTC/ETH
        // themselves, or a bare BTC/ETH would strip to empty. CoinCap covers far more
        // coins than the old cryptocurrency-icons set (incl. SUI/NEAR/RENDER).
        var base = symbol.toUpperCase().replace(/(USDT|USDC|FDUSD|BUSD|TUSD|USD|DAI|EUR|TRY)$/, '') || symbol.toUpperCase();
        return 'https://assets.coincap.io/assets/icons/' + base.toLowerCase() + '@2x.png';
    }
    if (t === 'stock' || t === 'etf') {
        return 'https://assets.parqet.com/logos/symbol/' + encodeURIComponent(symbol.toUpperCase()) + '?format=png';
    }
    return null;
}
function _assetLogoImg(symbol, type) {
    var url = _assetLogoUrl(symbol, type);
    if (!url) return '';
    return '<img class="symbol-logo" loading="lazy" alt="" src="' + url + '">';
}
// A broken logo img removes itself so the CSS glyph fallback
// (.symbol-chart:not(:has(.symbol-logo))::before) shows. `error` doesn't bubble,
// so listen in the capture phase; CSP blocks inline onerror= so it's delegated.
if (!window._symbolLogoErrBound) {
    window._symbolLogoErrBound = true;
    document.addEventListener('error', function(e) {
        var t = e.target;
        if (t && t.tagName === 'IMG' && t.classList && t.classList.contains('symbol-logo')) {
            t.remove();
        }
    }, true);
}

function createRow(asset, type, isWatchlist, sectionKeyOverride) {
    var typeClass = "type-" + asset.type;
    var c = asset.consensio;
    var consClass = getConsensioClass(asset.positions, asset.max_positions);
    var rsiClass = getRsiClass(asset.weekly_rsi);
    var sectionKey = sectionKeyOverride || (isWatchlist ? 'watchlist' : type);
    var hiddenClass = collapsedSections[sectionKey] ? ' hidden' : '';
    var emaRibbon = formatEmaRibbon(asset.ema_ribbon);
    var starred = isInWatchlist(asset.symbol);
    var starIcon = starred ? '&#9733;' : '&#9734;';
    var starClass = starred ? 'star active' : 'star';
    var trendAnalysis = asset.trend_analysis || {};
    var patterns = trendAnalysis.patterns || [];

    // Format RSI - only oversold/overbought colors (divergences moved to Signals column)
    var rsiValue = formatRsi(asset.weekly_rsi);

    // Format Consensio
    var consValue = asset.positions + '/' + asset.max_positions;
    var consensioCell = '<span class="consensio ' + consClass + '">' + consValue + '</span>';

    // Format 1D/3D/1W signals with consensio change background
    var signal1d = formatSignal(c["1D"].signal);
    var prev1d = asset.prev_signal_1d;
    var prev3d = asset.prev_signal_3d;
    var prev1w = asset.prev_signal_1w;

    // Determine per-TF change class: green if improved, red if dropped vs yesterday
    function tfChangeClass(current, prev) {
        if (prev === null || prev === undefined) return '';
        if (current > prev) return ' signal-improved';
        if (current < prev) return ' signal-dropped';
        return '';
    }
    var change1d = tfChangeClass(c["1D"].signal, prev1d);
    var change3d = tfChangeClass(c["3D"].signal, prev3d);
    var change1w = tfChangeClass(c["1W"].signal, prev1w);

    // Format Changed cell: prefer "<N>d" (days since flip) over the bare date,
    // with gradient highlight based on EMA Ribbon transition preserved.
    var changedDate = emaRibbon.date;
    var daysSinceFlip = (asset.ema_ribbon && typeof asset.ema_ribbon.days_since_flip === 'number')
        ? asset.ema_ribbon.days_since_flip : null;
    var changedDisplay = (daysSinceFlip !== null) ? (daysSinceFlip + 'd') : changedDate;
    var changedTitle = 'EMA Ribbon last flipped ' + changedDate +
        (daysSinceFlip !== null ? ' (' + daysSinceFlip + ' days ago)' : '');
    var changedCell = '<span title="' + escapeHtml(changedTitle) + '">' + changedDisplay + '</span>';
    if (patterns.indexOf('fresh_flip') !== -1 && asset.ema_ribbon) {
        var currentTrend = asset.ema_ribbon.trend || 'neutral';
        var gradientClass = 'highlight-flip-to-neutral'; // default
        if (currentTrend === 'bull') {
            gradientClass = 'highlight-flip-to-bull';
        } else if (currentTrend === 'bear') {
            gradientClass = 'highlight-flip-to-bear';
        }
        changedCell = '<span class="signal-highlight ' + gradientClass + '" data-tooltip="Fresh EMA Ribbon Flip" title="' + escapeHtml(changedTitle) + '">' + changedDisplay + '</span>';
    }

    // Format Signals column
    var signalsCell = formatSignals(asset);

    // Patterns column removed - v7.1 signal simplification

    // Format new columns: Structure (HH/HL), CPR Level, BB, MACD, ADX, ATR%, Volume
    var structureCell = formatStructure(asset.structure);
    var cprLevelCell = formatCPRLevel(asset.cpr);
    var bbCell = formatBB(asset.bb);
    var macdCell = formatMacd(asset.macd);
    var adxCell = formatAdx(asset.adx);
    var atrCell = formatAtrPct(asset.adx);
    var volCell = formatRelVolume(asset.volume_metrics);

    // Extended indicator columns (from cockpit_extended)
    var ext = asset.cockpit_extended || {};
    var ichiCell = formatIchimoku(ext.ichimoku);
    var strendCell = formatSuperTrend(ext.supertrend);
    var psarCell = formatPsar(ext.psar);
    var emaCrossCell = formatEmaCross(ext.ema_cross);
    var alligCell = formatAlligator(ext.alligator);
    var stochRsiCell = formatStochRsi(ext.stoch_rsi);
    var ttmSqzCell = formatTtmSqueeze(ext.ttm_squeeze);
    var donchCell = formatDonchian(ext.donchian);
    var nr7Cell = formatNr7(ext.nr7);
    var bbwPctCell = formatBbwPct(ext.bbw_pct);
    var atrPctRankCell = formatAtrPctRank(ext.atr_pct);
    var obvCell = formatObv(ext.obv);
    var fvgCell = formatFvg(ext.fvg);
    var vpCell = formatVpLevels(asset.vp_levels);
    var rsiDivCell = formatRsiDiv(asset.rsi_divergence);

    var safeSymbol = escapeHtml(asset.symbol);
    var safePrice = asset.latest_price || 0;
    // Format price change as separate cell
    var changePct = asset.realtime ? asset.realtime.change_pct : null;
    var changeHtml = '';
    if (changePct !== null && changePct !== undefined) {
        var changeClass = changePct >= 0 ? 'price-change-up' : 'price-change-down';
        var changeSign = changePct >= 0 ? '+' : '';
        changeHtml = '<span class="price-change ' + changeClass + '">' + changeSign + (typeof _fmtN === 'function' ? _fmtN(changePct, 2) : changePct.toFixed(2)) + '%</span>';
    }
    // Absolute change (Chg $, opt-in column)
    var changeVal = _assetChangeValue(asset);
    var changeValHtml = '';
    if (changeVal !== null) {
        var cvClass = changeVal >= 0 ? 'price-change-up' : 'price-change-down';
        changeValHtml = '<span class="price-change ' + cvClass + '">' + formatChangeValue(changeVal, asset.symbol, asset.type) + '</span>';
    }

    var safeSection = escapeHtml(sectionKey);
    var staleChip = asset.data_stale ? '<span class="stale-chip" title="Price feed unavailable — showing last stored data; indicator states may be outdated">&#9888;</span>' : '';
    return '<tr data-section="' + safeSection + '" data-symbol="' + safeSymbol + '" data-price="' + safePrice + '"' + (hiddenClass ? ' class="hidden"' : '') + '>' +
        '<td class="symbol ' + typeClass + '"><span class="symbol-chart" data-symbol="' + safeSymbol + '" data-action="chart-preview">' + _assetLogoImg(asset.symbol, asset.type) + '<span class="symbol-name">' + asset.symbol + '</span></span>' + staleChip + '<span class="' + starClass + '" data-symbol="' + safeSymbol + '" data-action="toggle-watchlist">' + starIcon + '</span>' + _rowOverviewHintHtml() + '</td>' +
        '<td class="name-col"><span data-symbol="' + safeSymbol + '" data-action="chart-preview">' + escapeHtml(asset.name) + '</span></td>' +
        '<td class="price">' + formatPrice(asset.latest_price, asset.symbol, asset.type) + '</td>' +
        '<td class="change-col">' + changeHtml + '</td>' +
        '<td class="change-value-col">' + changeValHtml + '</td>' +
        '<td class="consensio-col">' + consensioCell + '</td>' +
        '<td class="signal signal-1d-col ' + getSignalClass(c["1D"].signal) + change1d + '">' + signal1d + '</td>' +
        '<td class="signal signal-3d-col ' + getSignalClass(c["3D"].signal) + change3d + '">' + formatSignal(c["3D"].signal) + '</td>' +
        '<td class="signal signal-1w-col ' + getSignalClass(c["1W"].signal) + change1w + '">' + formatSignal(c["1W"].signal) + '</td>' +
        '<td class="rsi rsi-col ' + rsiClass + '">' + rsiValue + '</td>' +
        '<td class="cpr-col">' + cprLevelCell + '</td>' +
        '<td class="bb-col">' + bbCell + '</td>' +
        '<td class="ema-ribbon ema-ribbon-col">' + emaRibbon.html + '</td>' +
        '<td class="changed-col ema-ribbon-date">' + changedCell + '</td>' +
        '<td class="structure-col structure-cell">' + structureCell + '</td>' +
        '<td class="macd-col">' + macdCell + '</td>' +
        '<td class="adx-col">' + adxCell + '</td>' +
        '<td class="atr-col">' + atrCell + '</td>' +
        '<td class="vol-col">' + volCell + '</td>' +
        '<td class="signals-col signals-cell">' + signalsCell + '</td>' +
        '<td class="ichimoku-col">' + ichiCell + '</td>' +
        '<td class="supertrend-col">' + strendCell + '</td>' +
        '<td class="psar-col">' + psarCell + '</td>' +
        '<td class="emacross-col">' + emaCrossCell + '</td>' +
        '<td class="alligator-col">' + alligCell + '</td>' +
        '<td class="stochrsi-col">' + stochRsiCell + '</td>' +
        '<td class="ttmsqueeze-col">' + ttmSqzCell + '</td>' +
        '<td class="donchian-col">' + donchCell + '</td>' +
        '<td class="nr7-col">' + nr7Cell + '</td>' +
        '<td class="bbwpct-col">' + bbwPctCell + '</td>' +
        '<td class="atrpctrank-col">' + atrPctRankCell + '</td>' +
        '<td class="obv-col">' + obvCell + '</td>' +
        '<td class="fvg-col">' + fvgCell + '</td>' +
        '<td class="vplevels-col">' + vpCell + '</td>' +
        '<td class="rsidiv-col">' + rsiDivCell + '</td>' +
        '</tr>';
}

// Cache configuration - version suffix invalidates old cache on schema changes
function getCacheKey() {
    return 'market_context_cache_v7.6_' + timeframeMode;  // v7.6: Fixed crypto metric OHLC calculation
}

function getCacheTTL() {
    return timeframeMode === 'ltf' ? LTF_REFRESH_MS : HTF_REFRESH_MS;
}

function getCachedData() {
    try {
        var cached = localStorage.getItem(getCacheKey());
        if (cached) {
            var parsed = JSON.parse(cached);
            return parsed;
        }
    } catch (e) {
        console.warn('Cache read error:', e);
    }
    return null;
}

function setCachedData(data) {
    try {
        var cacheEntry = {
            timestamp: Date.now(),
            data: data
        };
        localStorage.setItem(getCacheKey(), JSON.stringify(cacheEntry));
    } catch (e) {
        console.warn('Cache write error:', e);
    }
}

function isCacheStale(cached) {
    if (!cached || !cached.timestamp) return true;
    return (Date.now() - cached.timestamp) > getCacheTTL();
}

function renderData(data, fromCache) {
    // Store all assets for watchlist lookup
    allAssets = {};
    data.assets.forEach(function(a) {
        allAssets[a.symbol] = a;
    });

    // Update filter counts
    updateFilterCounts(data.assets);

    // Render table with current filter
    renderFilteredTable();

    var timeStr = fromCache ? 'Cached' : 'Updated';
    var timeOpts = { hour: '2-digit', minute: '2-digit' };
    var timestamp = fromCache && data._cacheTime
        ? new Date(data._cacheTime).toLocaleTimeString(undefined, timeOpts)
        : new Date().toLocaleTimeString(undefined, timeOpts);
    var refreshBtn = document.getElementById("refresh-btn");
    if (refreshBtn) refreshBtn.title = timeStr + ": " + timestamp;

    // Update drawer data age badge from asset cache age
    if (typeof _updateDrawerDataAgeFromCacheAge === 'function' && data._cache_age != null) {
        _updateDrawerDataAgeFromCacheAge(data._cache_age);
    }
    document.getElementById("loading").style.display = "none";
    document.getElementById("table-wrapper").style.display = "block";

    // Update watchlist UI
    updateWatchlistUI();

    // Check price alerts
    checkPriceAlerts();

    // allAssets is now populated — poke the ticker bar so it leaves "Loading
    // ticker..." immediately instead of waiting for its next price-interval
    // tick (up to ~4s, or much longer during a cold-start /api/assets retry).
    if (typeof _tickerRebuild === 'function') _tickerRebuild();

    // Server pre-computes both modes, no client prewarm needed
}

function loadData(forceRefresh) {
    var cached = getCachedData();

    // Show cached data immediately if available
    if (cached && cached.data && !forceRefresh) {
        cached.data._cacheTime = cached.timestamp;
        renderData(cached.data, true);

        // If cache is still fresh, don't fetch
        if (!isCacheStale(cached)) {
            return;
        }
        // Otherwise, fetch in background (no loading indicator)
    }

    // Show loading only if no cache
    var showLoading = !cached || !cached.data;
    if (showLoading) {
        document.getElementById("loading").style.display = "block";
        setLoadingMode('instant');  // Start optimistically as instant
        startWizardMessages();
        startLoadingProgress();
    }

    // Fetch with retry logic for 202 "computing" responses
    function doFetch(retryCount) {
        // Watchdog: a single /api/assets attempt has no built-in timeout, so a
        // stalled worker would leave the page-loading spinner stuck forever. Abort
        // after a bounded wait — the rejection lands in .catch below. The 202
        // "computing" retry waits happen between attempts (setTimeout), not within
        // one fetch, so this never trips a legitimately-warming server.
        var _assetsTimedOut = false;
        var _assetsAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        var _assetsTimer = _assetsAbort ? setTimeout(function() {
            _assetsTimedOut = true;
            try { _assetsAbort.abort(); } catch (e) {}
        }, ASSETS_FETCH_TIMEOUT_MS) : null;
        fetch("/api/assets?mode=" + timeframeMode, { credentials: 'same-origin', signal: _assetsAbort ? _assetsAbort.signal : undefined })
            .then(function(r) {
                if (_assetsTimer) { clearTimeout(_assetsTimer); _assetsTimer = null; }
                // Check for auth redirect (session expired)
                if (r.redirected || r.url.includes('/login')) {
                    throw new Error('Session expired - please refresh the page');
                }

                // Handle 202 "computing" status - server is warming up
                if (r.status === 202) {
                    return r.json().then(function(data) {
                        if (data.status === 'computing' && retryCount < 12) {
                            // Switch to computing mode for slower progress
                            if (showLoading && retryCount === 0) {
                                setLoadingMode('computing');
                                updateLoadingPhase('computing');
                            }
                            // Retry after specified delay (default 5s)
                            var retryAfter = (data.retry_after || 5) * 1000;
                            setTimeout(function() {
                                doFetch(retryCount + 1);
                            }, retryAfter);
                            return null;  // Signal retry in progress
                        }
                        throw new Error('Server still computing after max retries');
                    });
                }

                if (!r.ok) {
                    throw new Error('HTTP ' + r.status);
                }

                // Phase: downloading (response headers received)
                if (showLoading) updateLoadingPhase('downloading');
                return r.json();
            })
            .then(function(data) {
                if (data === null) return;  // Retry in progress

                // Phase: processing (JSON parsed)
                if (showLoading) updateLoadingPhase('processing');

                if (data.error) throw new Error(data.error);

                // Phase: complete
                if (showLoading) updateLoadingPhase('complete');

                // Stop wizard messages with summary
                var summary = data.fetch_stats ? data.fetch_stats : "⚡ Instant!";
                stopWizardMessages(summary);

                // Cache the fresh data
                setCachedData(data);
                // Also cache for standalone chart page
                try { sessionStorage.setItem('assetListCache', JSON.stringify({ data: data, timestamp: Date.now() })); } catch(e) {}

                // Render fresh data
                renderData(data, false);
            })
            .catch(function(err) {
                if (_assetsTimer) { clearTimeout(_assetsTimer); _assetsTimer = null; }
                stopWizardMessages("The spell fizzled...");
                // Only show error if we don't have cached data
                if (!cached || !cached.data) {
                    document.getElementById("loading").style.display = "none";
                    var _msg = (_assetsTimedOut || (err && err.name === 'AbortError'))
                        ? 'Loading market data timed out — please refresh the page.'
                        : err.message;
                    document.getElementById("error").textContent = _msg;
                    document.getElementById("error").style.display = "block";
                    window.cvxTrackError && window.cvxTrackError('assets_load', _msg);
                } else {
                    console.warn('Background refresh failed:', err);
                }
            });
    }

    doFetch(0);
}

function manualRefresh() {
    var btn = document.getElementById('refresh-btn');
    var refreshLoadingEl = document.getElementById('refresh-loading');
    var errorEl = document.getElementById('error');

    // Just disable button - no text/style changes
    btn.disabled = true;

    // Hide any error, show refresh loading above watchlist (table stays visible)
    errorEl.style.display = 'none';
    refreshLoadingEl.style.display = 'flex';
    setLoadingMode('instant');  // Start optimistically as instant
    startRefreshWizardMessages();
    startRefreshProgress();

    // Fetch with retry logic for 202 "computing" responses
    function doFetch(retryCount) {
        // Watchdog: bound a single /api/assets attempt so the refresh spinner
        // (and the disabled refresh button) can't hang forever on a stalled
        // worker. 202-retry waits happen between attempts, not within one fetch.
        var _assetsTimedOut = false;
        var _assetsAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        var _assetsTimer = _assetsAbort ? setTimeout(function() {
            _assetsTimedOut = true;
            try { _assetsAbort.abort(); } catch (e) {}
        }, ASSETS_FETCH_TIMEOUT_MS) : null;
        fetch("/api/assets?mode=" + timeframeMode, { credentials: 'same-origin', signal: _assetsAbort ? _assetsAbort.signal : undefined })
            .then(function(r) {
                if (_assetsTimer) { clearTimeout(_assetsTimer); _assetsTimer = null; }
                // Check for auth redirect (session expired)
                if (r.redirected || r.url.includes('/login')) {
                    throw new Error('Session expired - please refresh the page');
                }

                // Handle 202 "computing" status - server is warming up
                if (r.status === 202) {
                    return r.json().then(function(data) {
                        if (data.status === 'computing' && retryCount < 12) {
                            // Switch to computing mode for slower progress
                            if (retryCount === 0) {
                                setLoadingMode('computing');
                                updateRefreshPhase('computing');
                            }
                            // Retry after specified delay (default 5s)
                            var retryAfter = (data.retry_after || 5) * 1000;
                            setTimeout(function() {
                                doFetch(retryCount + 1);
                            }, retryAfter);
                            return null;  // Signal retry in progress
                        }
                        throw new Error('Server still computing after max retries');
                    });
                }

                if (!r.ok) {
                    throw new Error('HTTP ' + r.status);
                }

                // Phase: downloading (response headers received)
                updateRefreshPhase('downloading');
                return r.json();
            })
            .then(function(data) {
                if (data === null) return;  // Retry in progress

                // Phase: processing (JSON parsed)
                updateRefreshPhase('processing');

                if (data.error) throw new Error(data.error);

                // Phase: complete
                updateRefreshPhase('complete');

                // Stop wizard with summary
                var summary = data.fetch_stats ? data.fetch_stats : "⚡ Instant!";
                stopRefreshWizardMessages(summary);

                // Cache and render fresh data
                setCachedData(data);
                renderData(data, false);

                // Hide refresh loading after brief delay to show completion
                setTimeout(function() {
                    refreshLoadingEl.style.display = 'none';
                    btn.disabled = false;
                }, 400);
            })
            .catch(function(err) {
                if (_assetsTimer) { clearTimeout(_assetsTimer); _assetsTimer = null; }
                stopRefreshWizardMessages("The spell fizzled...");
                // Hide refresh loading, show error
                refreshLoadingEl.style.display = 'none';
                errorEl.textContent = (_assetsTimedOut || (err && err.name === 'AbortError'))
                    ? 'Refresh timed out — please try again.'
                    : 'Refresh failed: ' + err.message;
                errorEl.style.display = 'block';
                window.cvxTrackError && window.cvxTrackError('assets_refresh', errorEl.textContent);
                btn.disabled = false;
            });
    }

    doFetch(0);
}
