/* Chart Toolbar — Unified toolbar HTML generation for the chart app + split-layout.js.
 * Loads after indicator-registry.js, before split-layout.js. */
(function() {
    'use strict';

    // Short display label for a chart type value — used by the trigger button's
    // text label and reused by
    // chart-events.js's chart-type click handler to update it after a selection.
    var CHART_TYPE_LABELS = {
        candle_solid: 'Candles',
        heikin_ashi: 'Heikin Ashi',
        candle_up_stroke: 'Hollow',
        ohlc: 'OHLC',
        area: 'Line'
    };
    function chartTypeLabel(type) {
        return CHART_TYPE_LABELS[type] || 'Candles';
    }

    // Small monochrome glyphs previewing each chart type — inherit currentColor
    // so they read correctly in both the resting and hover/active menu-item states.
    var CHART_TYPE_GLYPHS = {
        candle_solid: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">' +
            '<line x1="4" y1="1" x2="4" y2="15" stroke="currentColor" stroke-width="1.3"/>' +
            '<rect x="2" y="5" width="4" height="6" fill="currentColor"/>' +
            '<line x1="11" y1="3" x2="11" y2="13" stroke="currentColor" stroke-width="1.3"/>' +
            '<rect x="9" y="6" width="4" height="4" fill="currentColor"/>' +
        '</svg>',
        heikin_ashi: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">' +
            '<line x1="4" y1="2" x2="4" y2="8" stroke="currentColor" stroke-width="1.3"/>' +
            '<rect x="2" y="4" width="4" height="7" rx="1.2" fill="currentColor"/>' +
            '<line x1="11" y1="5" x2="11" y2="14" stroke="currentColor" stroke-width="1.3"/>' +
            '<rect x="9" y="7" width="4" height="6" rx="1.2" fill="currentColor"/>' +
        '</svg>',
        candle_up_stroke: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">' +
            '<line x1="4" y1="1" x2="4" y2="15" stroke="currentColor" stroke-width="1.3"/>' +
            '<rect x="2" y="5" width="4" height="6" stroke="currentColor" stroke-width="1.3"/>' +
            '<line x1="11" y1="3" x2="11" y2="13" stroke="currentColor" stroke-width="1.3"/>' +
            '<rect x="9" y="6" width="4" height="4" stroke="currentColor" stroke-width="1.3"/>' +
        '</svg>',
        ohlc: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">' +
            '<line x1="4" y1="2" x2="4" y2="14" stroke="currentColor" stroke-width="1.4"/>' +
            '<line x1="1" y1="9" x2="4" y2="9" stroke="currentColor" stroke-width="1.4"/>' +
            '<line x1="11" y1="4" x2="11" y2="13" stroke="currentColor" stroke-width="1.4"/>' +
            '<line x1="11" y1="7" x2="14" y2="7" stroke="currentColor" stroke-width="1.4"/>' +
        '</svg>',
        area: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">' +
            '<path d="M1 11 L5 6 L8 9 L15 2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M1 11 L5 6 L8 9 L15 2 L15 14 L1 14 Z" fill="currentColor" fill-opacity="0.18"/>' +
        '</svg>'
    };
    function chartTypeGlyph(type) {
        return '<span class="ctm-glyph">' + (CHART_TYPE_GLYPHS[type] || CHART_TYPE_GLYPHS.candle_solid) + '</span>';
    }

    // Chart type selector dropdown (reads current type from localStorage)
    function buildChartTypeMenuHTML() {
        var ct = 'candle_solid';
        try { ct = localStorage.getItem('klineChartType') || 'candle_solid'; } catch(e) {}
        return '<div class="indicator-dropdown">' +
            '<button class="indicator-btn" id="chart-type-toggle" aria-label="Chart type"><span id="chart-type-label">' + chartTypeLabel(ct) + '</span> <i data-lucide="chevron-down" aria-hidden="true"></i></button>' +
            '<div class="indicator-menu chart-type-menu" id="chart-type-menu">' +
                '<div class="indicator-menu-item' + (ct === 'candle_solid' ? ' active' : '') + '" data-chart-type="candle_solid">' + chartTypeGlyph('candle_solid') + 'Candles</div>' +
                '<div class="indicator-menu-item' + (ct === 'heikin_ashi' ? ' active' : '') + '" data-chart-type="heikin_ashi">' + chartTypeGlyph('heikin_ashi') + 'Heikin Ashi</div>' +
                '<div class="indicator-menu-item' + (ct === 'candle_up_stroke' ? ' active' : '') + '" data-chart-type="candle_up_stroke">' + chartTypeGlyph('candle_up_stroke') + 'Hollow Candles</div>' +
                '<div class="indicator-menu-item' + (ct === 'ohlc' ? ' active' : '') + '" data-chart-type="ohlc">' + chartTypeGlyph('ohlc') + 'OHLC Bars</div>' +
                '<div class="indicator-menu-item' + (ct === 'area' ? ' active' : '') + '" data-chart-type="area">' + chartTypeGlyph('area') + 'Line / Area</div>' +
            '</div>' +
        '</div>';
    }

    // Navigation toolbar row (Row 1)
    // options: { showCloseButton: bool, showWatchlistStar: bool }
    function buildNavToolbarHTML(symbol, options) {
        var opts = options || {};
        var asset = (typeof allAssets !== 'undefined' && allAssets[symbol]) ? allAssets[symbol] : {};

        var html = '<div class="toolbar-row toolbar-nav">' +
            '<div class="adv-chart-title">' +
                '<div class="asset-switcher-dropdown">' +
                    '<button class="asset-switcher-btn" id="asset-switcher-btn" title="Switch Asset">' +
                        '<span class="adv-chart-symbol" id="adv-chart-symbol">' + escapeHtml(symbol) + '</span>' +
                        '<i data-lucide="chevron-down" class="asset-switcher-arrow"></i>' +
                    '</button>' +
                    '<div class="asset-switcher-menu" id="asset-switcher-menu"></div>' +
                '</div>' +
                '<span class="adv-chart-name" id="adv-chart-name">' + escapeHtml(asset.name || '') + '</span>' +
                '<span class="mobile-ohlcv" id="mobile-ohlcv"></span>';

        if (opts.showWatchlistStar) {
            var starActive = (typeof isInWatchlist === 'function') ? isInWatchlist(symbol) : false;
            html += '<button class="watchlist-star-btn' + (starActive ? ' active' : '') + '" id="watchlist-star-btn" title="Toggle watchlist" aria-label="Toggle watchlist"><i data-lucide="star" aria-hidden="true"></i></button>';
        }

        html += '<button id="chart-search-btn" class="tool-btn" title="Search assets (Space)" aria-label="Search assets"><i data-lucide="search"></i></button>';

        html += '</div>' +
            '<div class="adv-chart-timeframes" id="tf-buttons-container">' +
                buildTfButtons(symbol, klineChartTimeframe) +
            '</div>' +
            '<div class="indicator-dropdown tf-compact-dd" id="tf-compact-dd">' +
                '<button class="indicator-btn" id="tf-compact-btn" title="Timeframe" aria-label="Timeframe" aria-haspopup="true"><span id="tf-compact-current"></span> <i data-lucide="chevron-down"></i></button>' +
                '<div class="indicator-menu" id="tf-compact-menu"></div>' +
            '</div>' +
            buildChartTypeMenuHTML() +
            (typeof mcLayoutSelectorHTML === 'function' ? mcLayoutSelectorHTML() : '') +
            '<div class="indicator-dropdown">' +
                '<button class="indicator-btn" id="indicator-toggle">Indicators <i data-lucide="chevron-down"></i></button>' +
                '<div class="indicator-menu" id="indicator-menu">' +
                    IndicatorRegistry.buildIndicatorMenuHTML() +
                '</div>' +
            '</div>' +
            '<span class="ind-fav-pills" id="ind-fav-pills"></span>' +
            '<div class="draw-toggle-wrapper" id="draw-toggle-wrapper">' +
                '<button class="tool-btn tool-text-btn draw-toggle" id="draw-toggle-btn" title="Drawing Tools (D)">Draw</button>' +
                '<button class="draw-pin-icon-btn" id="draw-pin-btn" title="Pin toolbar (keep it open across sessions)" aria-label="Pin drawing toolbar"><i data-lucide="pin" class="draw-pin-icon" aria-hidden="true"></i></button>' +
            '</div>' +
            '<button class="tool-btn" id="drawings-eye-btn" title="Hide all drawings (H)" aria-label="Hide or show all drawings"><i data-lucide="eye" aria-hidden="true"></i></button>' +
            '<span class="tf-preload-status" id="tf-preload-status"></span>' +
            '<div class="toolbar-spacer"></div>' +
            '<button class="tool-btn" id="shortcuts-btn" title="Keyboard Shortcuts (?)" aria-label="Keyboard Shortcuts"><i data-lucide="keyboard" aria-hidden="true"></i></button>' +
            '<button class="tool-btn" id="screenshot-export-btn" title="Export Chart Image" aria-label="Export Chart Image"><i data-lucide="camera" aria-hidden="true"></i></button>' +
            '<button class="tool-btn" id="chart-share-btn" title="Share Chart (public link)" aria-label="Share Chart"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg></button>';

        if (opts.showCloseButton) {
            html += '<button class="adv-chart-close" aria-label="Close chart"><i data-lucide="x" aria-hidden="true"></i></button>';
        }

        html += '</div>';
        return html;
    }

    // Draw row tool groups (frozen table — data-tool values, codes,
    // shortcuts and ordering must not drift without updating draw-tool-palette.js + main.css in lockstep).
    // First tool in each group's list is that group's default (remembered-tool baseline).
    var DRAW_TOOL_GROUPS = [
        { id: 'lines', label: 'Line tools', tools: [
            { tool: 'segment', code: 'Trend', name: 'Trend Line', kbd: 'Alt+Q' },
            { tool: 'horizontalStraightLine', code: 'H.Line', name: 'Horizontal Line', kbd: 'Alt+H' },
            { tool: 'horizontalRayLine', code: 'H.Ray', name: 'Horizontal Ray', kbd: '' },
            { tool: 'rayLine', code: 'Ray', name: 'Ray', kbd: '' },
            { tool: 'straightLine', code: 'X.Line', name: 'Extended Line', kbd: '' },
            { tool: 'arrow', code: 'Arrow', name: 'Arrow', kbd: '' },
            { tool: 'alertLine', code: 'Alert', name: 'Alert Line', kbd: 'Alt+A' },
            { tool: 'alertLevel', code: 'H.Alert', name: 'Horizontal Alert Line', kbd: 'Alt+L' }
        ] },
        { id: 'chan', label: 'Channel tools', tools: [
            { tool: 'parallelChannel', code: 'P.Channel', name: 'Parallel Channel', kbd: 'Alt+W' },
            { tool: 'pitchfork', code: 'Pitchfork', name: 'Pitchfork', kbd: '' }
        ] },
        { id: 'fib', label: 'Fib & Elliott', tools: [
            { tool: 'fibonacciRetrace', code: 'Fib', name: 'Fib Retracement', kbd: 'Alt+R' },
            { tool: 'elliottImpulse', code: 'EW5', name: 'Elliott Impulse', kbd: '' },
            { tool: 'elliottCorrective', code: 'EW3', name: 'Elliott Corrective', kbd: '' }
        ] },
        { id: 'shape', label: 'Shape tools', tools: [
            { tool: 'rect', code: 'Rect', name: 'Rectangle', kbd: 'Alt+E' },
            { tool: 'path', code: 'Path', name: 'Path', kbd: '' },
            { tool: 'freehand', code: 'Pen', name: 'Freehand', kbd: 'Alt+V' }
        ] },
        { id: 'pos', label: 'Position tools', tools: [
            { tool: 'longPosition', code: 'Long', name: 'Long Position', kbd: 'Alt+B' },
            { tool: 'shortPosition', code: 'Short', name: 'Short Position', kbd: 'Alt+B' },
            { tool: 'spot', code: 'Spot', name: 'Spot Buy', kbd: '' },
            { tool: 'spotSell', code: 'Sell', name: 'Spot Sell', kbd: '' }
        ] },
        { id: 'mark', label: 'Annotation tools', tools: [
            { tool: 'textAnnotation', code: 'Text', name: 'Text', kbd: 'Alt+T' },
            { tool: 'priceLine', code: 'Price', name: 'Price Label', kbd: '' },
            // single-click stamp glyphs (arrow/flag/circle/callout/vline) —
            // fixed v1 set, no emoji picker. Keep in lockstep with klineToolIcons/
            // klineToolNames/klineToolCodes (draw-tool-palette.js) and mobile-draw.js's
            // TOOL_CATEGORIES/SINGLE_POINT_TOOLS/DIALOG_TOOLS.
            { tool: 'arrowUpMarker', code: 'ArrUp', name: 'Arrow Up', kbd: '' },
            { tool: 'arrowDownMarker', code: 'ArrDn', name: 'Arrow Down', kbd: '' },
            { tool: 'flagPin', code: 'Flag', name: 'Flag', kbd: '' },
            { tool: 'circleMarker', code: 'Circle', name: 'Circle', kbd: '' },
            { tool: 'callout', code: 'Callout', name: 'Callout', kbd: '' },
            { tool: 'verticalStraightLine', code: 'V.Line', name: 'Vertical Line', kbd: '' }
        ] },
        { id: 'tools', label: 'Measure & volume', tools: [
            { tool: 'measureTool', code: 'Measure', name: 'Measure', kbd: 'Alt+D' },
            { tool: 'volumeProfileRange', code: 'VP', name: 'Fixed Range VP', kbd: 'Alt+F' },
            { tool: 'anchoredVwap', code: 'VWAP', name: 'Anchored VWAP', kbd: '' }
        ] }
    ];

    // Builds one group's split-button chip + its flyout menu. Chip shows the group's
    // default tool code/title as a no-JS-yet baseline — draw-tool-palette.js overwrites both at
    // runtime from the remembered-tool store.
    function buildToolGroupHTML(group) {
        var def = group.tools[0];
        var title = def.name + (def.kbd ? ' (' + def.kbd + ')' : '');
        var html = '<div class="tool-grp">' +
            '<button class="tool-btn tool-chip" id="' + group.id + '-group-btn" data-group="' + group.id + '" title="' + title + '" aria-haspopup="true" aria-expanded="false">' +
                '<span class="tool-chip-main">' + def.code + '</span><span class="tool-chip-drop" aria-hidden="true">&#9662;</span>' +
            '</button>' +
            '<div class="tool-group-menu" id="' + group.id + '-group-menu">';
        group.tools.forEach(function(t) {
            html += '<div class="tool-menu-item" data-tool="' + t.tool + '"><span class="tmi-code">' + t.code + '</span><span class="tmi-name">' + t.name + '</span>' +
                (t.kbd ? '<kbd class="tmi-kbd">' + t.kbd + '</kbd>' : '') + '</div>';
        });
        html += '</div>' +
            '</div>';
        return html;
    }

    // Drawing tools toolbar row (Row 2) — 7 grouped split-button chips, then the
    // favourites home slot (draw-tool-palette.js populates #fav-tools-bar + creates the floating
    // rail on drag), then Magnet/Undo/Redo/Clear (unchanged below).
    function buildDrawingToolbarHTML() {
        var groupsHtml = '';
        DRAW_TOOL_GROUPS.forEach(function(g) { groupsHtml += buildToolGroupHTML(g); });
        return '<div class="toolbar-row toolbar-draw" id="toolbar-draw">' +
            groupsHtml +
            '<span class="tool-separator"></span>' +
            '<span class="fav-home" id="fav-home">' +
                '<button class="fav-home-grip" id="fav-home-grip" aria-label="Drag favourites out as a floating rail" title="Drag the favourites out to float them"></button>' +
                '<span id="fav-tools-bar"></span>' +
                '<span class="fav-home-ghost" id="fav-home-ghost" hidden>floating &#8212; drop here to snap back</span>' +
            '</span>' +
            '<div class="toolbar-spacer"></div>' +
            '<button class="tool-btn tool-text-btn" id="magnet-btn" data-tool="magnet" title="Magnet: snap to OHLC (off/weak/strong) — hold Ctrl to invert while dragging" aria-label="Magnet snap mode">Magnet</button>' +
            '<span class="tool-separator"></span>' +
            '<button class="tool-btn tool-text-btn" data-tool="undo" title="Undo (Ctrl+Z)">Undo</button>' +
            '<button class="tool-btn tool-text-btn" data-tool="redo" title="Redo (Ctrl+Y)">Redo</button>' +
            '<button class="tool-btn tool-text-btn tool-danger" data-tool="clear" title="Clear All Drawings (hold Shift)">Clear</button>' +
        '</div>';
    }

    // Chart drawer (alerts, position, scanner, stream tabs)
    function buildDrawerHTML() {
        return '<div class="chart-drawer" id="chart-drawer">' +
            '<div class="chart-drawer-handle" id="chart-drawer-handle" role="tablist" aria-label="Chart panels">' +
                '<div class="drawer-tab" data-tab="position" id="drawer-tab-position" role="tab" tabindex="0" aria-selected="false">' +
                    '<i data-lucide="target" style="width:12px;height:12px" aria-hidden="true"></i> ' +
                    '<span class="drawer-tab-label" id="drawer-position-summary">No Position</span>' +
                '</div>' +
                '<div class="drawer-tab" data-tab="alerts" id="drawer-tab-alerts" role="tab" tabindex="-1" aria-selected="false">' +
                    '<i data-lucide="bell" style="width:12px;height:12px" aria-hidden="true"></i> ' +
                    '<span class="drawer-tab-label" id="drawer-alerts-summary">0 Alerts</span>' +
                '</div>' +
                '<div class="drawer-tab" data-tab="scanner" id="drawer-tab-scanner" role="tab" tabindex="-1" aria-selected="false">' +
                    '<i data-lucide="scan" style="width:12px;height:12px" aria-hidden="true"></i> ' +
                    '<span class="drawer-tab-label" id="drawer-scanner-summary">Scanner</span>' +
                '</div>' +
                '<div class="drawer-tab" data-tab="stream" id="drawer-tab-stream" role="tab" tabindex="-1" aria-selected="false">' +
                    '<i data-lucide="filter" style="width:12px;height:12px" aria-hidden="true"></i> ' +
                    '<span class="drawer-tab-label" id="drawer-stream-summary">Radar</span>' +
                '</div>' +
                // tabindex="-1" is NOT a discoverability bug here —
                // this is a roving-tabindex tablist (only drawer-tab-position
                // carries tabindex="0"; a11y.initTabStripKeyboard moves it as
                // you arrow across). Giving Backtest its own tab stop would
                // create a second one and break the pattern. Discoverability is
                // handled where users actually look: the landing page, the Help
                // modal walkthrough, and /guide#backtester — plus this tooltip.
                '<div class="drawer-tab" data-tab="backtest" id="drawer-tab-backtest" role="tab" tabindex="-1" aria-selected="false" ' +
                    'title="Backtest — test an entry/exit rule against this asset\'s history">' +
                    '<i data-lucide="flask-conical" style="width:12px;height:12px" aria-hidden="true"></i> ' +
                    '<span class="drawer-tab-label">Backtest</span>' +
                '</div>' +
                '<div class="drawer-tab" data-tab="objects" id="drawer-tab-objects" role="tab" tabindex="-1" aria-selected="false">' +
                    '<i data-lucide="layers" style="width:12px;height:12px" aria-hidden="true"></i> ' +
                    '<span class="drawer-tab-label" id="drawer-objects-summary">Drawings</span>' +
                '</div>' +
                '<span class="drawer-data-age" id="drawer-data-age" title="How long ago the server recomputed indicator data. Prices update live via WebSocket.">data: --</span>' +
                '<div class="drawer-chevron" id="drawer-chevron" title="Minimize" aria-label="Minimize drawer"><svg width="12" height="12" viewBox="0 0 12 12"><polyline points="2,4.5 6,8.5 10,4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square" stroke-linejoin="miter"/></svg></div>' +
                '<div class="drawer-fullscreen" id="drawer-fullscreen" title="Fullscreen" aria-label="Toggle drawer fullscreen"><svg width="12" height="12" viewBox="0 0 12 12"><polygon points="6,1.5 10.5,6 6,10.5 1.5,6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="miter"/></svg></div>' +
            '</div>' +
            '<div class="chart-drawer-content" id="chart-drawer-content">' +
                '<div class="drawer-section" id="drawer-section-position"></div>' +
                '<div class="drawer-section" id="drawer-section-alerts" style="display:none"></div>' +
                '<div class="drawer-section" id="drawer-section-scanner" style="display:none"></div>' +
                '<div class="drawer-section" id="drawer-section-stream" style="display:none"></div>' +
                '<div class="drawer-section" id="drawer-section-backtest" style="display:none"></div>' +
                '<div class="drawer-section" id="drawer-section-objects" style="display:none"></div>' +
            '</div>' +
        '</div>';
    }

    // Close button — delegated on document since the button is re-created inside HTML
    // strings that callers (the chart app, split-layout.js) assign via innerHTML; bound once
    // at script load, so it keeps working across every fullscreen-chart re-render.
    document.addEventListener('click', function(ev) {
        var btn = ev.target.closest('.adv-chart-close');
        if (btn) closeAdvancedChartModal();
    });

    // Expose as global
    window.ChartToolbar = {
        buildNavToolbarHTML: buildNavToolbarHTML,
        buildChartTypeMenuHTML: buildChartTypeMenuHTML,
        buildDrawingToolbarHTML: buildDrawingToolbarHTML,
        buildDrawerHTML: buildDrawerHTML,
        chartTypeLabel: chartTypeLabel
    };
})();
