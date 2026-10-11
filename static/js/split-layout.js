/* Split Layout — the desktop right-hand panel.
 *
 * Owns: the chart panel shell, the panel-tabs filter strip (Watchlist / Crypto /
 * TradFi / All), the vertical view-mode strip (Lite / Consensio / Detail / News /
 * Report / Plays / Journal), the draggable divider, per-mode width persistence,
 * and the collapse toggle.
 *
 * Source note: the deployed bundle is produced by minifying this readable file.
 * Terser's default --mangle spares top-level names, so the public API (all
 * top-level function names) is unchanged by minification. Equivalence is
 * checked, not assumed: minifying this file with the deploy pipeline's exact
 * terser flags must reproduce the committed minified output byte for byte.
 * See scripts/check-split-layout-equivalence.sh.
 */

// ============= Split Layout (TradingView-style) =============

function _bindWatchlistStar() {
    var btn = document.getElementById('watchlist-star-btn');
    if (!btn) return;
    btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var sym = klineChartSymbol;
        if (!sym) return;
        if (isInWatchlist(sym)) {
            removeFromWatchlist(sym);
        } else {
            addToWatchlist(sym);
        }
        _updateWatchlistStar(sym);
        if (typeof updateStarIcons === 'function') updateStarIcons();
        if (typeof renderFilteredTable === 'function') renderFilteredTable();
    });
}

function _updateWatchlistStar(symbol) {
    var btn = document.getElementById('watchlist-star-btn');
    if (!btn) return;
    var active = isInWatchlist(symbol);
    btn.classList.toggle('active', active);
    btn.title = active ? 'Remove from watchlist' : 'Add to watchlist';
}

function buildChartPanelHTML(symbol) {
    // Vestigial lookup, kept deliberately: the value is unused, but removing it
    // changes behavior — the read throws if `allAssets` is not defined yet.
    allAssets[symbol]; // eslint-disable-line no-unused-expressions
    // Reuse exact same toolbar + chart body HTML as the modal, minus overlay/close/resize
    return '<div class="adv-chart-content fullscreen" id="adv-chart-content">' +
            '<div class="adv-chart-toolbar">' +
                ChartToolbar.buildNavToolbarHTML(symbol, { showWatchlistStar: true }) +
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
        '</div>';
}

function initSplitLayout() {
    // Only activate on desktop (>768px). NOTE for anyone adding a panel view:
    // below this width the right panel does not exist at all, so a panel-only
    // feature is unreachable on phones.
    if (window.innerWidth <= 768) return;

    splitLayoutActive = true;

    // Mark container and body as split-active for CSS
    var container = document.querySelector('.container');
    if (container) container.classList.add('split-active');
    document.body.classList.add('split-active');

    var chartAreaContent = document.getElementById('chart-area-content');
    if (!chartAreaContent) return;

    // Set klineModal to chart-area-content so all existing querySelector calls work
    klineModal = chartAreaContent;

    initPanelTabs();
    initViewModeButtons();

    // Tab strip keyboard navigation (arrow keys)
    if (window.a11y) {
        a11y.initTabStripKeyboard(document.getElementById('view-mode-strip'), { tabSelector: '[role="tab"]', vertical: true });
        a11y.initTabStripKeyboard(document.getElementById('panel-tabs'), { tabSelector: '[role="tab"]' });
    }

    initWatchlistDragDrop();
    initPanelDivider();

    chartPanelReady = true;

    // Auto-select last asset from session if available
    var savedSymbol = sessionStorage.getItem('klineOpenSymbol') || localStorage.getItem('lastViewedAsset');
    if (savedSymbol) {
        setTimeout(function() { selectAssetInPanel(savedSymbol); }, 400);
    }
}

function selectAssetInPanel(symbol) {
    if (!chartPanelReady) return;
    // Desktop only. Deliberately `!(w > 768)` rather than `w <= 768`: the two are
    // equivalent, but only this form compiles to the exact bytes that were
    // running in production, which is what the equivalence check pins. Don't
    // "tidy" it without re-running scripts/check-split-layout-equivalence.sh.
    if (!(window.innerWidth > 768)) return;

    var chartAreaContent = document.getElementById('chart-area-content');
    if (!chartAreaContent) return;

    var placeholder = document.getElementById('chart-placeholder');
    if (placeholder) placeholder.style.display = 'none';

    // First time, or the chart container is gone and needs a full rebuild
    var needsRebuild = !klineChartSymbol || !document.getElementById('kline-chart-container');

    klineChartSymbol = symbol;
    var savedTf = sessionStorage.getItem('klineOpenTf');
    klineChartTimeframe = (savedTf && sessionStorage.getItem('klineOpenSymbol') === symbol) ? savedTf : (timeframeMode === 'ltf' ? '4h' : '1D');
    try {
        sessionStorage.setItem('klineOpenSymbol', symbol);
        sessionStorage.setItem('klineOpenTf', klineChartTimeframe);
        localStorage.setItem('lastViewedAsset', symbol);
    } catch (e) {}
    if (window.PreferenceSync) PreferenceSync.syncPreference('chart_prefs', { lastViewedAsset: symbol });

    if (needsRebuild) {
        chartAreaContent.innerHTML = buildChartPanelHTML(symbol);
        klineModal = chartAreaContent;
        klineModal.classList.add('visible');
        klineIsFullscreen = true;

        if (typeof lucide !== 'undefined') {
            lucide.createIcons({ attrs: { 'stroke-width': 1.75 } });
        }

        var tfCont = document.getElementById('tf-buttons-container');
        if (tfCont && typeof _addTfEditZone === 'function') _addTfEditZone(tfCont);

        // Wire drawer tab click handlers
        chartAreaContent.querySelectorAll('.drawer-tab').forEach(function(tab) {
            tab.addEventListener('click', function(e) {
                e.stopPropagation();
                toggleDrawer(this.getAttribute('data-tab'));
            });
        });
        var drawerChevron = document.getElementById('drawer-chevron');
        if (drawerChevron) {
            drawerChevron.addEventListener('click', function(e) {
                e.stopPropagation();
                if (drawerOpen) closeDrawer();
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

        setupKlineChartEvents();
        _bindWatchlistStar();

        // Multi-chart layout grid + restore saved state (desktop pointer only)
        var _mcRestored = false;
        if (!('ontouchstart' in window || navigator.maxTouchPoints > 0) && typeof mcInit === 'function') {
            mcInit();
            _mcRestored = (typeof mcRestoreState === 'function') && mcRestoreState();
        }

        // Load chart data (skip if multi-chart restore handled it)
        if (!_mcRestored) loadKlineChart(symbol, klineChartTimeframe);
    } else {
        // Fast swap — chart already exists, just switch asset
        var asset = allAssets[symbol] || {};
        var symEl = document.getElementById('adv-chart-symbol');
        var nameEl = document.getElementById('adv-chart-name');
        if (symEl) symEl.textContent = symbol;
        if (nameEl) nameEl.textContent = asset.name || '';
        _updateWatchlistStar(symbol);

        if (typeof cancelEditAlert === 'function') cancelEditAlert();

        // Reset TF if sub-hourly on non-crypto
        if (!isCryptoAsset(symbol) && ['1m', '3m', '5m', '15m', '30m'].indexOf(klineChartTimeframe) >= 0) {
            klineChartTimeframe = '1D';
        }
        // Free-tier: reset to 4H if on a blocked TF
        if (!window._isPremium && ['1m', '3m', '5m', '15m', '30m', '1h'].indexOf(klineChartTimeframe) >= 0) {
            klineChartTimeframe = '4h';
        }

        var tfContainer = document.getElementById('tf-buttons-container');
        if (tfContainer) {
            tfContainer.innerHTML = buildTfButtons(symbol, klineChartTimeframe);
            if (typeof _addTfEditZone === 'function') _addTfEditZone(tfContainer);
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

        // In spread mode mcSpreadOnAssetChange reloads every pane and returns
        // truthy; otherwise load just the active one.
        if (typeof mcSpreadOnAssetChange !== 'function' || !mcSpreadOnAssetChange(symbol)) {
            loadKlineChart(symbol, klineChartTimeframe);
        }
    }

    highlightActiveAsset(symbol);
}

function highlightActiveAsset(symbol) {
    document.querySelectorAll('tr.active-asset').forEach(function(row) {
        row.classList.remove('active-asset');
    });
    // Highlight matching rows (main table + watchlist)
    document.querySelectorAll('[data-action="chart-preview"][data-symbol="' + symbol + '"]').forEach(function(el) {
        var row = el.closest('tr');
        if (row) row.classList.add('active-asset');
    });
}

function initPanelTabs() {
    document.querySelectorAll('.panel-tab').forEach(function(tab) {
        tab.addEventListener('click', function() {
            switchPanelTab(this.getAttribute('data-tab'));
        });
    });
    // Restore saved tab (default to 'watchlist' for users with a watchlist)
    var savedTab = localStorage.getItem('panelTypeFilter');
    if (savedTab) {
        switchPanelTab(savedTab);
    } else if (watchlistSymbols && watchlistSymbols.length > 0) {
        switchPanelTab('watchlist');
    }
}

function switchPanelTab(tabId) {
    // NOTE: these are FILTERS over one symbol list, not content panes. A new
    // content view belongs in the view-mode strip (setViewMode), not here.
    // 'watchlist' = personal watchlist only, 'screener' = all, 'crypto'/'misc' = type filter
    panelTypeFilter = tabId === 'screener' ? 'all' : tabId;
    document.querySelectorAll('.panel-tab').forEach(function(tab) {
        var tabData = tab.getAttribute('data-tab');
        var isActive = tabData === tabId || (tabData === 'screener' && tabId === 'all');
        tab.classList.toggle('active', isActive);
        if (tab.hasAttribute('role')) {
            tab.setAttribute('aria-selected', String(isActive));
            tab.setAttribute('tabindex', isActive ? '0' : '-1');
        }
    });
    // Group editing only makes sense while looking at the watchlist
    (function() {
        var groupsBtn = document.getElementById('groups-edit-btn');
        if (groupsBtn) groupsBtn.style.display = panelTypeFilter === 'watchlist' ? '' : 'none';
    })();
    renderFilteredTable();
    updateWatchlistUI();
    try { localStorage.setItem('panelTypeFilter', panelTypeFilter); } catch (e) {}
}

// Right panel collapse/expand
var _panelCollapsed = false;
var _panelWidthBeforeCollapse = null;
var _panelCollapseTimer = null;

function toggleRightPanel() {
    if (_panelCollapseTimer) { clearTimeout(_panelCollapseTimer); _panelCollapseTimer = null; }
    var panel = document.getElementById('right-panel');
    if (!panel) return;
    _panelCollapsed = !_panelCollapsed;
    var strip = document.getElementById('view-mode-strip');
    var collapsedWidth = strip ? strip.offsetWidth + 1 : 33; // +1 for border
    if (_panelCollapsed) {
        _panelWidthBeforeCollapse = panel.style.width || (panel.offsetWidth + 'px');
        // Set current width explicitly so the transition has a start value
        panel.style.width = panel.offsetWidth + 'px';
        panel.offsetHeight; // force reflow
        panel.style.width = collapsedWidth + 'px';
        // Apply collapsed class after the transition so content hides at the end
        _panelCollapseTimer = setTimeout(function() {
            panel.classList.add('panel-collapsed');
            _panelCollapseTimer = null;
        }, 150);
    } else {
        // Remove collapsed class first (content visible but clipped)
        panel.classList.remove('panel-collapsed');
        panel.style.width = collapsedWidth + 'px';
        panel.offsetHeight; // force reflow
        panel.style.width = _panelWidthBeforeCollapse;
    }
    var btn = document.getElementById('panel-collapse-btn');
    if (btn) {
        btn.innerHTML = _panelCollapsed
            ? '<i data-lucide="chevrons-left" style="width:14px;height:14px"></i>'
            : '<i data-lucide="chevrons-right" style="width:14px;height:14px"></i>';
        btn.title = _panelCollapsed ? 'Expand panel (E)' : 'Collapse panel (E)';
        if (typeof lucide !== 'undefined') lucide.createIcons({ nodes: [btn], attrs: { 'stroke-width': 1.75 } });
    }
    if (klineChart) setTimeout(function() { klineChart.resize(); }, 170);
}

// View Mode Buttons
var panelReportLoaded = false;

function initViewModeButtons() {
    var strip = document.getElementById('view-mode-strip');
    if (!strip) return;
    strip.querySelectorAll('.view-mode-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            setViewMode(this.getAttribute('data-mode'));
        });
    });
    // Restore saved mode
    setViewMode(localStorage.getItem('panelViewMode') || 'consensio');
}

/* Per-mode panel width. Detail and Journal want a wide panel, Lite wants a
 * narrow one, and remembering a single global width made every mode switch
 * feel wrong. Stored as one {mode: px} object under `panelWidthByMode`. */
function _readPanelWidths() {
    try {
        var raw = localStorage.getItem('panelWidthByMode');
        if (raw) {
            var parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object') return parsed;
        }
    } catch (e) {}
    return {};
}

function _panelWidthForMode(mode, def) {
    var widths = _readPanelWidths(), saved = widths[mode];
    // Anything under the 280px drag floor is treated as junk, not a preference.
    return (typeof saved === 'number' && saved >= 280) ? saved : def;
}

function _savePanelWidthForMode(mode, width) {
    try {
        var widths = _readPanelWidths();
        widths[mode] = Math.round(width);
        localStorage.setItem('panelWidthByMode', JSON.stringify(widths));
    } catch (e) {}
}

/* THE mechanism for adding a right-panel view: a data-mode
 * button in .view-mode-strip, a #panel-<name>-view pane, a default width in
 * `widths` below, and an init-once + refresh branch in the chain at the end. */
function setViewMode(mode) {
    // Declared here, assigned far below where it is used. Keep the split:
    // joining the declaration and assignment changes the compiled output, so
    // re-run scripts/check-split-layout-equivalence.sh if you move it.
    var actionsRow;
    var panel = document.getElementById('right-panel');
    if (!panel) return;

    // Notify the nudge system of the view mode change
    try { document.dispatchEvent(new CustomEvent('carrac:viewmode', { detail: { mode: mode } })); } catch (e) {}

    // Swap the view-* class
    panel.className = panel.className.replace(/\bview-\w+/g, '').trim();
    panel.classList.add('view-' + mode);

    // Default width per mode; the wide modes share one value for smooth cycling.
    var expandedWidth = Math.max(Math.round(window.innerWidth * 0.55), 800);
    var widths = {
        minimal: 340,
        consensio: 400,
        full: expandedWidth,
        report: 400,
        ai: expandedWidth,
        journal: expandedWidth,
        matrix: expandedWidth,
        plays: expandedWidth,
        news: expandedWidth,
        portfolio: expandedWidth
    };
    var targetWidth = widths[mode] || 400;
    // A width the user dragged for this mode wins over the default.
    panel.style.width = _panelWidthForMode(mode, targetWidth) + 'px';

    document.querySelectorAll('.view-mode-btn').forEach(function(btn) {
        var isActive = btn.getAttribute('data-mode') === mode;
        btn.classList.toggle('active', isActive);
        if (btn.hasAttribute('role')) {
            btn.setAttribute('aria-selected', String(isActive));
            btn.setAttribute('tabindex', isActive ? '0' : '-1');
        }
    });

    var screener = document.getElementById('tab-screener');
    var reportView = document.getElementById('panel-report-view');
    var aiView = document.getElementById('panel-ai-view');
    var journalView = document.getElementById('panel-journal-view');
    var matrixView = document.getElementById('panel-matrix-view');
    var playsView = document.getElementById('panel-plays-view');
    var newsView = document.getElementById('panel-news-view');
    var portfolioView = document.getElementById('panel-portfolio-view');
    var tabs = document.getElementById('panel-tabs');

    // Hide every pane first, then show exactly one below.
    if (screener) screener.style.display = 'none';
    if (reportView) reportView.style.display = 'none';
    if (aiView) aiView.style.display = 'none';
    if (journalView) journalView.style.display = 'none';
    if (matrixView) matrixView.style.display = 'none';
    if (playsView) playsView.style.display = 'none';
    if (newsView) newsView.style.display = 'none';
    if (portfolioView) portfolioView.style.display = 'none';
    if (tabs) tabs.style.display = 'none';

    // The actions row belongs to the screener-ish modes only
    actionsRow = document.getElementById('panel-actions-row');
    if (actionsRow) actionsRow.style.display = (mode === 'minimal' || mode === 'consensio' || mode === 'full') ? '' : 'none';

    if (mode === 'matrix') {
        if (matrixView) matrixView.style.display = '';
        if (typeof initScreeningMatrix === 'function' && !matrixView._initialized) {
            initScreeningMatrix();
            matrixView._initialized = true;
        }
        if (typeof refreshMatrix === 'function') refreshMatrix();
    } else if (mode === 'report') {
        if (reportView) reportView.style.display = '';
        if (!panelReportLoaded) loadPanelReport();
    } else if (mode === 'ai') {
        if (aiView) aiView.style.display = '';
        if (typeof initAIPanel === 'function' && !aiView._initialized) {
            initAIPanel();
            aiView._initialized = true;
            refreshAIPanelBalance();
        }
    } else if (mode === 'journal') {
        if (journalView) journalView.style.display = '';
        // The journal panel lives inside the AI bundle; if it has not loaded
        // yet, pull it in and re-enter once it is there.
        if (typeof initJournalPanel !== 'function' && typeof ensureAIPanel === 'function') {
            ensureAIPanel().then(function() { setViewMode('journal'); });
        }
        if (typeof initJournalPanel === 'function' && !journalView._initialized) {
            initJournalPanel();
            if (typeof initPositionWatch === 'function') setTimeout(initPositionWatch, 500);
            journalView._initialized = true;
        }
        if (typeof refreshJournal === 'function') {
            if (typeof _journalLastAsset !== 'undefined') _journalLastAsset = null;
            refreshJournal();
        }
    } else if (mode === 'plays') {
        if (playsView) playsView.style.display = '';
        if (typeof initPlaysPanel === 'function' && !playsView._initialized) {
            initPlaysPanel();
            playsView._initialized = true;
        }
        if (typeof refreshPlays === 'function') refreshPlays();
    } else if (mode === 'news') {
        if (newsView) newsView.style.display = '';
        if (typeof initNewsPanel === 'function' && !newsView._initialized) {
            initNewsPanel();
            newsView._initialized = true;
        }
        if (typeof refreshNewsPanel === 'function') refreshNewsPanel();
    } else if (mode === 'portfolio') {
        if (portfolioView) portfolioView.style.display = '';
        // portfolio.js + portfolio-charts.js already drive the standalone
        // /portfolio page off these same endpoints; lazily pulled
        // in on first visit here, same shape as journal's ensureAIPanel() call.
        if (typeof Portfolio === 'undefined' && typeof ensurePortfolioPanel === 'function') {
            ensurePortfolioPanel().then(function() { setViewMode('portfolio'); });
        }
        // Portfolio.init() binds the sort headers/row clicks and does the
        // first load; every re-entry after that just re-fetches, same as
        // refreshJournal/refreshPlays.
        if (typeof Portfolio !== 'undefined' && typeof Portfolio.init === 'function' && !portfolioView._initialized) {
            Portfolio.init();
            portfolioView._initialized = true;
        } else if (typeof Portfolio !== 'undefined') {
            if (typeof Portfolio.loadSummary === 'function') Portfolio.loadSummary();
            if (typeof Portfolio.loadHistory === 'function') Portfolio.loadHistory();
            if (typeof Portfolio.checkUnpromotedSpots === 'function') Portfolio.checkUnpromotedSpots();
        }
    } else {
        // Screener modes (minimal, consensio, full)
        if (screener) { screener.style.display = ''; screener.classList.add('active'); }
        if (tabs) tabs.style.display = '';
    }

    // Resize chart to fill the remaining space
    if (klineChart) setTimeout(function() { klineChart.resize(); }, 50);

    try { localStorage.setItem('panelViewMode', mode); } catch (e) {}
}

/* Pull the movers out of the report's Market Snapshot table. The report is
 * LLM-authored markdown rendered to HTML, so this parses the rendered table
 * rather than any structured payload. Row 0 is the header. */
function _parsePanelMovers(html) {
    var movers = [];
    if (!html) return movers;
    var rows = html.match(/<tr>([\s\S]*?)<\/tr>/gi) || [];
    rows.forEach(function(row, i) {
        if (i === 0) return;
        var cells = row.match(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi) || [];
        if (cells.length >= 2) {
            var sym = cells[0].replace(/<[^>]+>/g, '').trim();
            var price = cells.length >= 3 ? cells[1].replace(/<[^>]+>/g, '').trim() : '';
            var pctRaw = (cells.length >= 3 ? cells[2] : cells[1]).replace(/<[^>]+>/g, '').trim();
            var pct = parseFloat(pctRaw.replace(/[^0-9.\-+]/g, ''));
            var type = cells.length >= 4 ? cells[3].replace(/<[^>]+>/g, '').trim() : '';
            if (sym && !isNaN(pct)) movers.push({ sym: sym, price: price, pct: pct, type: type });
        }
    });
    // Biggest absolute move first, direction-agnostic
    movers.sort(function(a, b) { return Math.abs(b.pct) - Math.abs(a.pct); });
    return movers;
}

function _fetchPanelRibbon() {
    fetch('/api/market-sentiment', { credentials: 'same-origin' }).then(function(r) {
        return r.ok ? r.json() : null;
    }).then(function(data) {
        if (!data || !data.sentiment) return;
        var s = data.sentiment;
        var moodEl = document.getElementById('pr-rib-mood');
        var whenTagEl = document.getElementById('pr-when-tag');
        var fgEl = document.getElementById('pr-rib-fg');
        if (s.regime && s.regime.regime) {
            if (moodEl) {
                moodEl.textContent = s.regime.regime;
                moodEl.className = 'v neu';
            }
            if (whenTagEl) whenTagEl.textContent = s.regime.regime;
        }
        if (fgEl && s.fear_greed) {
            var value = s.fear_greed.value != null ? Math.round(s.fear_greed.value) : null;
            if (value != null) {
                fgEl.textContent = value;
                fgEl.className = 'v ' + (value <= 25 ? 'bear' : value >= 75 ? 'bull' : 'neu');
            }
        }
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/market-sentiment failed — rendered as empty', String((e && e.message) || e)); });
}

function loadPanelReport() {
    var container = document.getElementById('panel-report-content');
    if (!container) return;
    container.innerHTML = '<div style="padding:40px 20px;text-align:center;color:#979289">Loading report…</div>';
    fetch('/api/report/morning/latest', { credentials: 'same-origin' }).then(function(r) {
        return r.ok ? r.json() : null;
    }).then(function(data) {
        if (!data || !data.content) {
            container.innerHTML = '<div style="padding:40px 20px;text-align:center;color:#979289">No report available. <a href="/report/morning" style="color:#c6a96f">View full report</a></div>';
            panelReportLoaded = true;
            return;
        }
        var sec = typeof ReportCore !== 'undefined' ? ReportCore.parseReportSections(data.content) : {};
        // created_at arrives as a naive string; treat it as UTC unless it already says otherwise.
        var _ts = data.created_at ? data.created_at.trim().replace(' ', 'T') : null;
        if (_ts && !/[zZ]$|[+\-]\d{2}:?\d{2}$/.test(_ts)) _ts += 'Z';
        var dt = _ts ? new Date(_ts) : null;
        var html = '<div class="phead"><div class="types"><a class="on" href="#">Morning</a><a href="/report/afternoon">Afternoon</a><a href="/report/weekly">Weekly</a></div><a class="listen" id="pr-listen" href="#" style="display:none"><span class="dot"></span>Listen</a></div>';
        if (dt) {
            var agoMs = Date.now() - dt.getTime();
            var agoStr = agoMs < 36e5 ? Math.round(agoMs / 6e4) + 'm ago' : Math.round(agoMs / 36e5) + 'h ago';
            var dStr = dt.toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            html += '<div class="when"><span class="tag" id="pr-when-tag">—</span> ' + dStr + ' · ' + agoStr + '</div>';
        }
        html += '<div class="miniribbon"><div class="it"><div class="k">Mood</div><div class="v" id="pr-rib-mood">—</div></div><div class="it"><div class="k">Fear / Greed</div><div class="v" id="pr-rib-fg">—</div></div><div class="it"><div class="k">Mover 7d</div><div class="v" id="pr-rib-mover">—</div></div></div>';
        var headline = data.summary || (data.metadata && data.metadata.summary) || '';
        if (!headline && sec.story) headline = sec.story.replace(/<[^>]+>/g, '').split(/\n/)[0].trim().substring(0, 180);
        html += '<div class="headline serif">' + escapeHtml(headline || 'Morning briefing') + '</div>';

        html += '<div class="eyebrow">The take</div><ul class="take">';
        var takeItems = (sec.overview || '').match(/<li>([\s\S]*?)<\/li>/g) || [];
        if (!takeItems.length) takeItems = (sec.story || '').match(/<li>([\s\S]*?)<\/li>/g) || [];
        takeItems.slice(0, 3).forEach(function(li) {
            html += '<li>' + li.replace(/<\/?li>/g, '') + '</li>';
        });
        if (!takeItems.length) html += '<li style="color:#979289">See full report for details.</li>';
        html += '</ul>';

        html += '<div class="eyebrow">Since yesterday</div><ul class="changes">';
        var chItems = (sec.sinceYesterday || '').match(/<li>([\s\S]*?)<\/li>/g) || [];
        chItems.slice(0, 3).forEach(function(li) {
            var inner = li.replace(/<\/?li>/g, '').trim();
            var ci = inner.indexOf(':');
            // Only split on a colon that reads like a label, not mid-sentence
            if (ci > 0 && ci < 35) html += '<li><span>' + inner.substring(0, ci).trim() + '</span><span class="val">' + inner.substring(ci + 1).trim() + '</span></li>';
            else html += '<li><span>' + inner + '</span></li>';
        });
        if (!chItems.length) html += '<li><span style="color:#979289">No delta available</span></li>';
        html += '</ul>';

        html += '<div class="eyebrow">Today\'s movers</div><div class="movers">';
        var mvrs = _parsePanelMovers(sec.marketSnapshot || '');
        mvrs.slice(0, 3).forEach(function(m) {
            var cls = m.pct >= 0 ? 'bull' : 'bear';
            var sign = m.pct >= 0 ? '+' : '';
            html += '<div class="mv"><span class="nm">' + escapeHtml(m.sym) + '<small>' + escapeHtml(m.type) + '</small></span><span class="pr">' + escapeHtml(m.price) + '</span><span class="ch ' + cls + '">' + sign + m.pct.toFixed(1) + '%</span></div>';
        });
        if (!mvrs.length) html += '<div style="color:#979289;font-size:12px;padding:8px 0">See full report for movers.</div>';
        html += '</div>';
        html += '<a class="readmore" href="/report/morning">Read the full briefing →</a>';
        container.innerHTML = html;

        // Reveal the Listen button only if audio actually exists for this report
        (function() {
            var btn = document.getElementById('pr-listen');
            if (!btn) return;
            fetch('/api/report/morning/latest/audio', { method: 'HEAD', credentials: 'same-origin' }).then(function(r) {
                if (r.ok) {
                    btn.style.display = '';
                    var au = null;
                    btn.addEventListener('click', function(ev) {
                        ev.preventDefault();
                        ev.stopPropagation();
                        if (!au) au = new Audio('/api/report/morning/latest/audio');
                        if (au.paused) {
                            au.play();
                            btn.innerHTML = '<span class="dot"></span>Pause';
                        } else {
                            au.pause();
                            btn.innerHTML = '<span class="dot"></span>Listen';
                        }
                    });
                }
            }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/report/morning/latest/audio failed — rendered as empty', String((e && e.message) || e)); });
        })();

        _fetchPanelRibbon();
        _loadPanelAfternoonDelta(container, dt);
        panelReportLoaded = true;
    }).catch(function() {
        container.innerHTML = '<div style="padding:40px 20px;text-align:center;color:#979289">Failed to load report. <a href="/report/morning" style="color:#c6a96f">View full report</a></div>';
        panelReportLoaded = true;
    });
}

/* Append today's afternoon delta under the morning report, if one exists.
 * Yesterday's afternoon report is deliberately dropped — a stale delta under
 * a fresh morning report reads as current. */
function _loadPanelAfternoonDelta(container, morningDt) {
    fetch('/api/report/afternoon/latest', { credentials: 'same-origin' }).then(function(r) {
        return r.ok ? r.json() : null;
    }).then(function(data) {
        if (!data || !data.content) return;
        var timeStr = '', agoStr = '', aftDt = null;
        if (data.created_at) {
            var created = new Date(data.created_at.trim().replace(' ', 'T').replace(/([^Z])$/, '$1Z'));
            var now = new Date();
            if (created.toISOString().slice(0, 10) !== now.toISOString().slice(0, 10)) return;
            aftDt = created;
            var hours = (now - created) / 36e5;
            agoStr = hours < 1 ? Math.floor(60 * hours) + 'm ago' : Math.floor(hours) + 'h ago';
            timeStr = created.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
        }
        var delta = data.delta || null;
        var wrap = document.createElement('div');
        wrap.className = 'ir-afternoon-delta';
        if (!delta) {
            wrap.innerHTML = '<div class="eyebrow" style="margin:0 0 10px">Afternoon delta' + (timeStr ? '<span class="ir-afternoon-time"> ' + timeStr + (agoStr ? ' · ' + agoStr : '') + '</span>' : '') + ' </div>' +
                '<div class="ir-afternoon-body">' + ReportCore.simpleMarkdownToHtml(data.content) + '</div>';
        } else {
            var mTs = morningDt ? morningDt.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '';
            var html = '<div class="eyebrow" style="margin:0 0 10px">Afternoon delta' + (timeStr ? '<span class="ir-afternoon-time"> ' + timeStr + (agoStr ? ' · ' + agoStr : '') + '</span>' : '') + ' </div>';
            html += '<div class="tl" style="margin-bottom:18px"><div class="node done"><div class="dot"></div><div class="t">' + escapeHtml(mTs || '—') + '</div><div class="l">Morning</div></div><div class="node now"><div class="dot"></div><div class="t">' + escapeHtml(timeStr) + '</div><div class="l">Now</div></div><div class="node future"><div class="dot"></div><div class="t">Asia</div><div class="l">Overnight</div></div></div>';
            var allHeld = !delta.regime.changed &&
                (!delta.fear_greed.dir || delta.fear_greed.dir === 'flat') &&
                (!delta.levels_tested || !delta.levels_tested.length) &&
                (!delta.signal_flips || !delta.signal_flips.length) &&
                (!delta.new_prints || !delta.new_prints.length);
            if (allHeld) {
                html += '<div style="padding:10px 0 16px;color:var(--muted);font-size:12px">No material change since this morning.</div>';
            } else {
                html += '<div class="ledger" style="margin-bottom:16px">';
                var regChg = delta.regime.changed;
                html += '<div class="lrow neu"><div class="what">Regime</div><div class="was">' + escapeHtml(String(delta.regime.was || '—')) + '</div><div class="arr">→</div><div class="nowv">' + escapeHtml(String(delta.regime.now || '—')) + ' <span class="chip ' + (regChg ? 'new' : 'held') + '">' + (regChg ? 'changed' : 'held') + '</span></div></div>';
                var fgd = delta.fear_greed.dir || 'flat';
                var fgc = fgd === 'up' ? 'up' : fgd === 'down' ? 'dn' : 'neu';
                var fga = fgd === 'up' ? ' ▲' : fgd === 'down' ? ' ▼' : '';
                html += '<div class="lrow ' + fgc + '"><div class="what">F&amp;G</div><div class="was">' + escapeHtml(String(delta.fear_greed.was != null ? delta.fear_greed.was : '—')) + '</div><div class="arr">→</div><div class="nowv">' + escapeHtml(String(delta.fear_greed.now != null ? delta.fear_greed.now : '—')) + escapeHtml(fga) + '</div></div>';
                (delta.levels_tested || []).forEach(function(lv) {
                    var oc = String(lv.outcome || '');
                    var lc = /reclaim|hold|held|above/i.test(oc) ? 'up' : /lost|broke|below/i.test(oc) ? 'dn' : 'neu';
                    html += '<div class="lrow ' + lc + '"><div class="what">' + escapeHtml(String(lv.asset || '') + ' ' + String(lv.level || '')) + '</div><div class="was">tested</div><div class="arr">→</div><div class="nowv">' + escapeHtml(oc) + '</div></div>';
                });
                (delta.signal_flips || []).forEach(function(sf) {
                    var tv = String(sf.to || '');
                    var sc = /bull/i.test(tv) ? 'up' : /bear/i.test(tv) ? 'dn' : 'neu';
                    html += '<div class="lrow ' + sc + '"><div class="what">' + escapeHtml(String(sf.asset || '')) + '</div><div class="was">' + escapeHtml(String(sf.from || '—')) + '</div><div class="arr">→</div><div class="nowv">' + escapeHtml(tv) + '</div></div>';
                });
                (delta.new_prints || []).forEach(function(np) {
                    var ex = np.forecast != null ? String(np.forecast) : np.expected != null ? String(np.expected) : '—';
                    var ac = np.actual != null ? String(np.actual) : '—';
                    html += '<div class="lrow neu"><div class="what">' + escapeHtml(String(np.event || np.name || '')) + '</div><div class="was">exp ' + escapeHtml(ex) + '</div><div class="arr">→</div><div class="nowv">' + escapeHtml(ac) + ' <span class="chip new">new</span></div></div>';
                });
                html += '</div>';
            }
            html += '<div class="ir-afternoon-body">' + ReportCore.simpleMarkdownToHtml(data.content) + '</div>';
            wrap.innerHTML = html;
        }
        // Keep the "read the full briefing" link last
        var readmore = container.querySelector('.readmore');
        if (readmore) container.insertBefore(wrap, readmore);
        else container.appendChild(wrap);
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}

function initPanelDivider() {
    var divider = document.getElementById('panel-divider');
    var panel = document.getElementById('right-panel');
    if (!divider || !panel) return;

    // No width restore here — setViewMode applies the per-mode width on entry.

    var startX, startWidth;

    divider.addEventListener('mousedown', function(e) {
        function onMouseMove(e) {
            var dx = startX - e.clientX;
            var newWidth = Math.max(280, Math.min(window.innerWidth * 0.8, startWidth + dx));
            panel.style.width = newWidth + 'px';
            // One resize per frame — mousemove fires far faster than the chart can redraw
            if (klineChart) {
                if (!window.__pdResizeRaf) {
                    window.__pdResizeRaf = requestAnimationFrame(function() {
                        window.__pdResizeRaf = null;
                        klineChart.resize();
                    });
                }
            }
        }
        e.preventDefault();
        startX = e.clientX;
        startWidth = panel.offsetWidth;
        divider.classList.add('dragging');
        panel.style.transition = 'none'; // disable during drag
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', function onMouseUp() {
            divider.classList.remove('dragging');
            panel.style.transition = ''; // re-enable
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
            // Remember this width for the mode it was dragged in
            try {
                _savePanelWidthForMode(localStorage.getItem('panelViewMode') || 'consensio', panel.offsetWidth);
            } catch (e) {}
            if (klineChart) klineChart.resize();
        });
    });
}

// Handle window resize: toggle split layout at the 768px threshold
window.addEventListener('resize', function() {
    if (window.innerWidth <= 768 && splitLayoutActive) {
        // Went below threshold — tear the split layout down
        splitLayoutActive = false;
        chartPanelReady = false;
        var container = document.querySelector('.container');
        if (container) container.classList.remove('split-active');
        document.body.classList.remove('split-active');
        // The AI panel cannot stay a column once the columns are gone
        if (typeof _aiPanelState !== 'undefined' && _aiPanelState === 'column') {
            setAIPanelState('small');
        }
    } else if (window.innerWidth > 768 && !splitLayoutActive && !window.isStandaloneChart) {
        // Went above threshold — re-enable
        initSplitLayout();
    }
    if (klineChart) klineChart.resize();
});

/* The AI panel can dock as a third column, to the right of the right panel.
 * It is moved in the DOM rather than re-created so its state survives. */
function insertAIColumn(panel) {
    var splitLayout = document.getElementById('split-layout');
    var rightPanel = document.getElementById('right-panel');
    if (!splitLayout || !rightPanel || !panel) return;
    var divider = document.getElementById('ai-panel-divider');
    if (!divider) {
        divider = document.createElement('div');
        divider.className = 'panel-divider ai-panel-divider';
        divider.id = 'ai-panel-divider';
    }
    rightPanel.after(divider);
    divider.after(panel);
}

function removeAIColumn(panel) {
    if (!panel) return;
    var divider = document.getElementById('ai-panel-divider');
    if (divider) divider.remove();
    document.body.appendChild(panel);
}

var _aiDividerBound = false;

function initAIPanelDivider() {
    // Bind once — the divider is re-inserted on every dock, the listener is not
    if (_aiDividerBound) return;
    _aiDividerBound = true;
    var divider = document.getElementById('ai-panel-divider');
    var panel = document.getElementById('ai-float');
    if (!divider || !panel) return;
    divider.addEventListener('mousedown', function(evt) {
        evt.preventDefault();
        var startX = evt.clientX;
        var startW = panel.offsetWidth;
        divider.classList.add('dragging');
        panel.style.transition = 'none';
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        function onMove(e) {
            var delta = startX - e.clientX;
            var w = Math.max(300, Math.min(600, startW + delta));
            panel.style.width = w + 'px';
            if (klineChart) {
                if (!window.__aiResizeRaf) {
                    window.__aiResizeRaf = requestAnimationFrame(function() {
                        window.__aiResizeRaf = null;
                        klineChart.resize();
                    });
                }
            }
        }
        function onUp() {
            divider.classList.remove('dragging');
            panel.style.transition = '';
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            try {
                localStorage.setItem('aiColumnWidth', panel.offsetWidth);
            } catch (ex) {}
            if (klineChart) klineChart.resize();
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
}
