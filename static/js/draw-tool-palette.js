// The drawing-tool palette: tool catalogue (icons, names, mnemonic codes,
// groups), favourite tools and their bar, per-group last-used tool memory
// with active-state sync, and the draggable floating favourites rail.
// Needs app-core.js loaded first.

var klineFavouriteTools = (function() {
    try {
        var favs = JSON.parse(localStorage.getItem('klineFavouriteTools') || '[]');
        // Migrate old overlay names to current tool names
        var changed = false;
        favs = favs.filter(function(t) {
            if (t === 'parallelStraightLine') { changed = true; return false; }
            if (t === 'boundedChannel') { changed = true; return false; }
            return true;
        });
        if (changed) localStorage.setItem('klineFavouriteTools', JSON.stringify(favs));
        return favs;
    } catch(e) { return []; }
})();
var klineToolIcons = {
    measureTool: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="6" y1="22" x2="22" y2="6"/><line x1="6" y1="22" x2="6" y2="17"/><line x1="6" y1="22" x2="11" y2="22"/><line x1="22" y1="6" x2="22" y2="11"/><line x1="22" y1="6" x2="17" y2="6"/><line x1="10" y1="22" x2="10" y2="19" opacity="0.4"/><line x1="14" y1="18" x2="14" y2="15" opacity="0.4"/><line x1="18" y1="14" x2="18" y2="11" opacity="0.4"/></svg>',
    segment: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="6" y1="21" x2="22" y2="7"/><rect x="4" y="19" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/><rect x="20" y="5" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/></svg>',
    rayLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="6" y1="20" x2="25" y2="6"/><rect x="4" y="18" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/><path d="M23,6 L25,6 L25,8" stroke-width="1.2" opacity="0.4"/></svg>',
    straightLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="2" y1="25" x2="26" y2="3"/><rect x="9" y="16" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/><rect x="15" y="8" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/></svg>',
    horizontalStraightLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="3" y1="14" x2="25" y2="14"/><rect x="12" y="12" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/></svg>',
    horizontalRayLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="5" y1="14" x2="25" y2="14"/><rect x="4" y="12" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/><path d="M23,12 L25,14 L23,16" stroke-width="1.2" opacity="0.4"/></svg>',
    parallelChannel: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="4" y1="10" x2="24" y2="6"/><line x1="4" y1="22" x2="24" y2="18"/><line x1="4" y1="16" x2="24" y2="12" stroke-dasharray="3 3" opacity="0.3"/><path d="M4,10 L4,22" stroke-width="0.8" opacity="0.2"/><path d="M24,6 L24,18" stroke-width="0.8" opacity="0.2"/></svg>',
    rect: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="7" width="18" height="14" rx="1"/><rect x="5" y="7" width="18" height="14" rx="1" fill="currentColor" opacity="0.06"/></svg>',
    fibonacciRetrace: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"><line x1="4" y1="5" x2="24" y2="5"/><line x1="4" y1="10" x2="24" y2="10" stroke-dasharray="3 2" opacity="0.5"/><line x1="4" y1="15" x2="24" y2="15" opacity="0.3"/><line x1="4" y1="19" x2="24" y2="19" stroke-dasharray="3 2" opacity="0.5"/><line x1="4" y1="23" x2="24" y2="23"/><text x="25" y="6" font-size="5" fill="currentColor" opacity="0.5" font-family="sans-serif">0</text><text x="25" y="24" font-size="5" fill="currentColor" opacity="0.5" font-family="sans-serif">1</text></svg>',
    fibonacciLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"><line x1="4" y1="5" x2="24" y2="5"/><line x1="4" y1="10" x2="24" y2="10" stroke-dasharray="3 2" opacity="0.5"/><line x1="4" y1="15" x2="24" y2="15" opacity="0.3"/><line x1="4" y1="19" x2="24" y2="19" stroke-dasharray="3 2" opacity="0.5"/><line x1="4" y1="23" x2="24" y2="23"/></svg>',
    fibonacciInfinite: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"><line x1="0" y1="5" x2="28" y2="5"/><line x1="0" y1="10" x2="28" y2="10" stroke-dasharray="3 2" opacity="0.5"/><line x1="0" y1="15" x2="28" y2="15" opacity="0.3"/><line x1="0" y1="19" x2="28" y2="19" stroke-dasharray="3 2" opacity="0.5"/><line x1="0" y1="23" x2="28" y2="23"/></svg>',
    priceLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="4" y1="14" x2="24" y2="14"/><line x1="20" y1="10" x2="20" y2="18" opacity="0.3"/><text x="21" y="13" font-size="6" fill="currentColor" opacity="0.6" font-family="sans-serif">$</text></svg>',
    textAnnotation: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="8" y1="7" x2="20" y2="7"/><line x1="14" y1="7" x2="14" y2="21"/><line x1="10" y1="21" x2="18" y2="21" stroke-width="1.2"/></svg>',
    position: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="6" y="6" width="16" height="16" rx="1" stroke-dasharray="3 2" opacity="0.2"/><line x1="6" y1="14" x2="22" y2="14"/><line x1="6" y1="6" x2="22" y2="6" stroke-width="1.5"/><line x1="6" y1="22" x2="22" y2="22" stroke-dasharray="3 2" opacity="0.5"/><rect x="6" y="6" width="16" height="8" fill="currentColor" opacity="0.06"/><path d="M11,10 L15,10 L13,8 Z" fill="currentColor" opacity="0.5"/></svg>',
    longPosition: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="6" y="6" width="16" height="16" rx="1" stroke-dasharray="3 2" opacity="0.2"/><line x1="6" y1="14" x2="22" y2="14"/><line x1="6" y1="6" x2="22" y2="6" stroke-width="1.5" /><line x1="6" y1="22" x2="22" y2="22" stroke-dasharray="3 2" opacity="0.5"/><rect x="6" y="6" width="16" height="8" fill="currentColor" opacity="0.10"/><path d="M11,10 L15,10 L13,8 Z" fill="currentColor" opacity="0.6" stroke="none"/></svg>',
    shortPosition: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="6" y="6" width="16" height="16" rx="1" stroke-dasharray="3 2" opacity="0.2"/><line x1="6" y1="14" x2="22" y2="14"/><line x1="6" y1="6" x2="22" y2="6" stroke-width="1.5"/><line x1="6" y1="22" x2="22" y2="22" stroke-dasharray="3 2"  opacity="0.7"/><rect x="6" y="14" width="16" height="8" fill="currentColor" opacity="0.10"/><path d="M11,18 L15,18 L13,20 Z" fill="currentColor" opacity="0.6" stroke="none"/></svg>',
    spot: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="4" y1="14" x2="24" y2="14" stroke-dasharray="3 2" opacity="0.4"/><circle cx="14" cy="14" r="3" stroke-width="1.5"/><path d="M14,18 L11,23 L17,23 Z" fill="currentColor" opacity="0.4" stroke="none"/></svg>',
    alertLevel: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="2" y1="14" x2="26" y2="14" stroke-dasharray="4 3"/><path d="M22,6 a4,4 0 0 0 -4,4 c0,3 -1.5,4 -1.5,4 h11 s-1.5,-1 -1.5,-4 a4,4 0 0 0 -4,-4" stroke-width="1.2"/></svg>',
    alertLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="4" y1="14" x2="17" y2="14"/><path d="M22,8 a5,5 0 0 0 -5,5 c0,4 -2,5 -2,5 h14 s-2,-1 -2,-5 a5,5 0 0 0 -5,-5" stroke-width="1.3"/><line x1="22" y1="6" x2="22" y2="8" stroke-width="1"/><circle cx="22" cy="20.5" r="1" fill="currentColor" stroke="none"/></svg>',
    volumeProfileRange: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1"><line x1="6" y1="4" x2="6" y2="24" stroke-dasharray="2 2" opacity="0.4"/><line x1="23" y1="4" x2="23" y2="24" stroke-dasharray="2 2" opacity="0.4"/><rect x="7" y="6" width="12" height="3" rx="0.5" fill="currentColor" opacity="0.25"/><rect x="7" y="10" width="15" height="3" rx="0.5" fill="currentColor" opacity="0.55"/><rect x="7" y="14" width="9" height="3" rx="0.5" fill="currentColor" opacity="0.2"/><rect x="7" y="18" width="14" height="3" rx="0.5" fill="currentColor" opacity="0.45"/><rect x="7" y="22" width="7" height="3" rx="0.5" fill="currentColor" opacity="0.15"/></svg>',
    anchoredVwap: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="5" cy="20" r="2.5" stroke-width="1.2"/><circle cx="5" cy="20" r="1" fill="currentColor" stroke="none"/><path d="M7,19 C10,15 14,13 18,14 C22,15 24,11 26,9" fill="none"/><text x="20" y="8" font-size="6" fill="currentColor" opacity="0.5" font-family="sans-serif">V</text></svg>',
    freehand: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5,20 C7,15 9,10 12,12 C15,14 14,21 17,16 C20,11 22,8 25,10"/></svg>',
    path: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4,20 9,12 15,16 20,8 25,11"/><circle cx="4" cy="20" r="2" stroke-width="1"/><path d="M23,9 L25,11 L23,13" stroke-width="1.2"/></svg>',
    elliottImpulse: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3,20 8,8 12,22 17,4 22,16 26,10"/><text x="7" y="7" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">1</text><text x="12" y="26" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">2</text><text x="17" y="3" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">3</text><text x="22" y="20" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">4</text><text x="26" y="9" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">5</text></svg>',
    elliottCorrective: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="5,8 12,22 19,6 25,18"/><text x="12" y="26" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">A</text><text x="19" y="5" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">B</text><text x="25" y="22" font-size="5.5" fill="currentColor" font-family="sans-serif" text-anchor="middle">C</text></svg>',
    arrow: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="21" x2="21" y2="8"/><path d="M14,7 L22,7 L22,15"/></svg>',
    priceChannelLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="4" y1="9" x2="24" y2="5"/><line x1="4" y1="23" x2="24" y2="19"/></svg>',
    pitchfork: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="5" y1="22" x2="14" y2="13" stroke-width="1.2" opacity="0.5"/><line x1="14" y1="13" x2="24" y2="3" stroke-dasharray="3 2" opacity="0.4"/><line x1="13" y1="3" x2="26" y2="10"/><line x1="9" y1="10" x2="24" y2="18"/></svg>',
    spotSell: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="4" y1="14" x2="24" y2="14" stroke-dasharray="3 2" opacity="0.4"/><circle cx="14" cy="14" r="3"/><path d="M14,10 L11,5 L17,5 Z" fill="currentColor" opacity="0.4" stroke="none"/></svg>',
    // Fixed v1 annotation glyph set (no emoji picker)
    arrowUpMarker: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M14,6 L21,18 L7,18 Z" fill="currentColor" opacity="0.6" stroke="none"/><line x1="14" y1="18" x2="14" y2="23" opacity="0.35"/></svg>',
    arrowDownMarker: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M14,22 L21,10 L7,10 Z" fill="currentColor" opacity="0.6" stroke="none"/><line x1="14" y1="10" x2="14" y2="5" opacity="0.35"/></svg>',
    flagPin: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="8" y1="5" x2="8" y2="23"/><path d="M8,6 L21,10 L8,14 Z" fill="currentColor" opacity="0.55" stroke="none"/></svg>',
    circleMarker: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="14" cy="14" r="7" fill="currentColor" fill-opacity="0.2"/></svg>',
    callout: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="6" cy="20" r="2" fill="currentColor" stroke="none"/><line x1="7.5" y1="18.5" x2="15" y2="10" stroke-dasharray="2 2" opacity="0.6"/><rect x="14" y="4" width="12" height="8" rx="1" fill="currentColor" fill-opacity="0.08"/><line x1="17" y1="8" x2="23" y2="8" stroke-width="1.2" opacity="0.7"/></svg>',
    verticalStraightLine: '<svg viewBox="0 0 28 28" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="14" y1="3" x2="14" y2="25"/><rect x="12" y="12" width="4" height="4" rx="1" fill="currentColor" stroke="none" opacity="0.5"/></svg>'
};
var klineToolNames = {
    horizontalStraightLine: 'Horizontal Line', horizontalRayLine: 'Horizontal Ray',
    segment: 'Trend Line', rayLine: 'Ray', straightLine: 'Extended Line',
    parallelChannel: 'Parallel Channel', priceChannelLine: 'Price Channel', rect: 'Rectangle',
    fibonacciRetrace: 'Fib Retrace', fibonacciLine: 'Fib Retrace', fibonacciInfinite: 'Fib Infinite', priceLine: 'Price Line',
    textAnnotation: 'Text',
    position: 'Position',
    longPosition: 'Long Position',
    shortPosition: 'Short Position',
    spot: 'Spot Buy',
    spotSell: 'Sell Spot',
    measureTool: 'Measure',
    alertLine: 'Alert Line',
    alertLevel: 'Horizontal Alert Line',
    volumeProfileRange: 'VP Range',
    anchoredVwap: 'Anchored VWAP',
    freehand: 'Freehand',
    path: 'Path',
    elliottImpulse: 'Elliott Impulse',
    elliottCorrective: 'Elliott Corrective',
    arrow: 'Arrow',
    pitchfork: 'Pitchfork',
    arrowUpMarker: 'Arrow Up',
    arrowDownMarker: 'Arrow Down',
    flagPin: 'Flag',
    circleMarker: 'Circle',
    callout: 'Callout',
    verticalStraightLine: 'Vertical Line'
};

// 2-4 char mnemonic codes for the group-chip / favourites text
// chips — values copied verbatim from chart-toolbar.js's DRAW_TOOL_GROUPS
// (.tmi-code text), which is the frozen single source of truth for the 30
// draw tools (24 + 6 annotation glyphs). Keep in lockstep if that
// table ever changes.
var klineToolCodes = {
    segment: 'Trend', horizontalStraightLine: 'H.Line', horizontalRayLine: 'H.Ray',
    rayLine: 'Ray', straightLine: 'X.Line', arrow: 'Arrow',
    parallelChannel: 'P.Channel', pitchfork: 'Pitchfork',
    fibonacciRetrace: 'Fib', elliottImpulse: 'EW5', elliottCorrective: 'EW3',
    rect: 'Rect', path: 'Path', freehand: 'Pen',
    longPosition: 'Long', shortPosition: 'Short', spot: 'Spot', spotSell: 'Sell',
    textAnnotation: 'Text', priceLine: 'Price', alertLine: 'Alert', alertLevel: 'H.Alert',
    measureTool: 'Measure', volumeProfileRange: 'VP', anchoredVwap: 'VWAP',
    arrowUpMarker: 'ArrUp', arrowDownMarker: 'ArrDn', flagPin: 'Flag',
    circleMarker: 'Circle', callout: 'Callout', verticalStraightLine: 'V.Line'
};

// The 7 draw-tool group ids (frozen table, must match the
// DRAW_TOOL_GROUPS ids in chart-toolbar.js) + the per-tool group index built
// from the live DOM at setup time (single source of truth — avoids a second
// hand-authored groups table drifting from the markup).
var KLINE_TOOL_GROUP_IDS = ['lines', 'chan', 'fib', 'shape', 'pos', 'mark', 'tools'];
var klineToolGroupOf = {}; // tool -> groupId, populated in setupKlineChartEvents
var _klineGroupToolList = {}; // groupId -> [tool,...] in DOM (default) order

// Remembered last-used tool per group — {groupId: toolName}. Validated
// against the live markup in setupKlineChartEvents (unknown/missing entries
// fall back to the group's default = first menu item).
var klineToolGroupMemory = (function() {
    try {
        var mem = JSON.parse(localStorage.getItem('klineToolGroupMemory') || '{}');
        return (mem && typeof mem === 'object' && !Array.isArray(mem)) ? mem : {};
    } catch(e) { return {}; }
})();
function _klineSaveToolGroupMemory() {
    try { localStorage.setItem('klineToolGroupMemory', JSON.stringify(klineToolGroupMemory)); } catch(e) {}
}

function toggleFavouriteTool(tool, starEl) {
    var idx = klineFavouriteTools.indexOf(tool);
    if (idx >= 0) {
        klineFavouriteTools.splice(idx, 1);
        if (starEl) { starEl.classList.remove('active'); starEl.textContent = '☆'; }
    } else {
        klineFavouriteTools.push(tool);
        if (starEl) { starEl.classList.add('active'); starEl.textContent = '★'; }
    }
    try { localStorage.setItem('klineFavouriteTools', JSON.stringify(klineFavouriteTools)); } catch(e) {}
    rebuildFavToolsBar();
}

// Favourite chips are text (code + full-name/kbd title) and render into
// whichever container is currently live — the docked '#fav-tools-bar'
// strip, or the floating rail's chip slot.
function rebuildFavToolsBar() {
    var docked = !_favRailEl || _favRailEl.style.display === 'none';
    var target = docked ? document.getElementById('fav-tools-bar') : _favRailChips;
    var other = docked ? _favRailChips : document.getElementById('fav-tools-bar');
    if (other) other.innerHTML = '';
    if (!target) return;
    if (klineFavouriteTools.length === 0) {
        target.innerHTML = '';
        // Nothing left to float — dock back home (item 6 rule).
        if (!docked) _favRailDockHome();
        return;
    }
    var html = '';
    klineFavouriteTools.forEach(function(tool) {
        if (!klineToolNames[tool]) return;
        var code = klineToolCodes[tool] || '?';
        var name = klineToolNames[tool];
        var kbd = _klineToolKbd(tool);
        var title = name + (kbd ? ' (' + kbd + ')' : '');
        html += '<button class="tool-btn fav-tool-btn" data-tool="' + escapeHtml(tool) + '" title="' + escapeHtml(title) + '" aria-pressed="false">' + escapeHtml(code) + '</button>';
    });
    target.innerHTML = html;
    // Attach click handlers
    target.querySelectorAll('.fav-tool-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var t = this.getAttribute('data-tool');
            _klineFireTool(t, this, e);
        });
    });
    _klineSyncToolActiveState();
}

// ============= group-chip tool memory + active-state sync =============

// Reads a tool's Alt+ shortcut straight off its (always-present, even if the
// flyout is currently closed) '.tool-menu-item' markup — single source of
// truth, no second kbd map to keep in sync.
function _klineToolKbd(tool) {
    var el = document.querySelector('#toolbar-draw .tool-menu-item[data-tool="' + tool + '"] .tmi-kbd');
    return el ? el.textContent : '';
}

// Updates one group chip's visible code + title from whichever tool is
// currently remembered for that group. Reads name/kbd off the corresponding
// (always-in-DOM) flyout row when possible, falling back to the klineToolCodes/
// klineToolNames maps only if that row can't be found.
function _klineUpdateChipDisplay(group) {
    var tool = klineToolGroupMemory[group];
    var chip = document.getElementById(group + '-group-btn');
    if (!chip || !tool) return;
    var main = chip.querySelector('.tool-chip-main');
    var item = document.querySelector('#' + group + '-group-menu .tool-menu-item[data-tool="' + tool + '"]');
    var codeEl = item ? item.querySelector('.tmi-code') : null;
    var nameEl = item ? item.querySelector('.tmi-name') : null;
    var kbdEl = item ? item.querySelector('.tmi-kbd') : null;
    var code = codeEl ? codeEl.textContent : (klineToolCodes[tool] || '?');
    var name = nameEl ? nameEl.textContent : (klineToolNames[tool] || tool);
    var kbd = kbdEl ? kbdEl.textContent : '';
    if (main) main.textContent = code;
    chip.title = name + (kbd ? ' (' + kbd + ')' : '');
}

// Resets aria-expanded on every group chip except keepGroup (pass null to
// reset all) — used whenever a menu-close action isn't necessarily the one
// specific chip whose own click handler is running (e.g. firing a chip's
// main zone closes every menu via closeAllChartMenus(), which may include a
// DIFFERENT group's flyout that was left open).
function _klineResetChipAriaExpanded(keepGroup) {
    KLINE_TOOL_GROUP_IDS.forEach(function(g) {
        if (g === keepGroup) return;
        var b = document.getElementById(g + '-group-btn');
        if (b) b.setAttribute('aria-expanded', 'false');
    });
}

// Fires `tool` through the existing handleKlineToolClick path, always
// resolving to the tool's real '.tool-menu-item' element (present in the DOM
// even while its flyout is closed) so the right row — and, via the active-
// state observer below, the right chip/favourite — end up highlighted
// regardless of which control (chip main-zone, flyout row, favourite chip,
// or an Alt+ shortcut) actually fired it.
function _klineFireTool(tool, triggerEl, e) {
    var menuItemEl = document.querySelector('#toolbar-draw .tool-menu-item[data-tool="' + tool + '"]') || triggerEl || document.createElement('button');
    handleKlineToolClick(tool, menuItemEl, [], e);
    showDrawingToolbar();
    _klineSyncToolActiveState();
}

// Alt+ shortcut path — also records the tool as its group's new remembered
// tool + relabels the chip (the shortcut has no flyout-click to do that for
// it), then fires it the same way any other trigger would.
function _klineRememberShortcutTool(tool) {
    var group = klineToolGroupOf[tool];
    if (!group) return;
    klineToolGroupMemory[group] = tool;
    _klineSaveToolGroupMemory();
    _klineUpdateChipDisplay(group);
}
function _klineFireToolFromShortcut(tool, e) {
    _klineRememberShortcutTool(tool);
    _klineFireTool(tool, null, e);
}

// Whichever '.tool-menu-item[data-tool]' currently carries '.active' is the
// engaged tool (handleKlineToolClick enforces "only one at a time" via its
// own global sweep). Derived rather than tracked, so it also reflects
// kline-drawings.js's internal onDrawEnd auto-deactivate.
function _klineCurrentEngagedTool() {
    var active = document.querySelector('#toolbar-draw .tool-menu-item.active[data-tool]');
    return active ? active.getAttribute('data-tool') : null;
}

// Syncs '.tool-chip' (active when its group's remembered tool is the engaged
// tool) and '.fav-tool-btn' (active when it IS the engaged tool). Diffs
// before touching classList/attributes so a MutationObserver watching the
// same subtree (see _klineInitToolActiveObserver) can't be re-triggered by
// our own no-op writes — that would otherwise loop forever.
function _klineSyncToolActiveState() {
    var engaged = _klineCurrentEngagedTool();
    document.querySelectorAll('#toolbar-draw .tool-chip[data-group]').forEach(function(chip) {
        var g = chip.getAttribute('data-group');
        var shouldBeActive = !!engaged && klineToolGroupMemory[g] === engaged;
        if (shouldBeActive !== chip.classList.contains('active')) {
            chip.classList.toggle('active', shouldBeActive);
            chip.setAttribute('aria-pressed', shouldBeActive ? 'true' : 'false');
        }
    });
    document.querySelectorAll('.fav-tool-btn').forEach(function(btn) {
        var shouldBeActive = !!engaged && btn.getAttribute('data-tool') === engaged;
        if (shouldBeActive !== btn.classList.contains('active')) {
            btn.classList.toggle('active', shouldBeActive);
            btn.setAttribute('aria-pressed', shouldBeActive ? 'true' : 'false');
        }
    });
}

// Observes '#toolbar-draw' for class changes on any '.tool-menu-item' (the
// only elements handleKlineToolClick actually toggles '.active' on for draw
// tools) and re-derives chip/favourite highlighting from them — a safety net
// for state changes we don't directly trigger (onDrawEnd auto-deactivate).
function _klineInitToolActiveObserver() {
    var row = document.getElementById('toolbar-draw');
    if (!row || row._klineActiveMo || !('MutationObserver' in window)) return;
    var mo = new MutationObserver(function() { _klineSyncToolActiveState(); });
    mo.observe(row, { subtree: true, attributes: true, attributeFilter: ['class'] });
    row._klineActiveMo = mo;
}

// ============= floating favourites rail =============
// Docked by default in '#fav-home' (inside the draw row). Dragging
// '#fav-home-grip' (or, once floating, '.fav-rail-grip') pulls the
// favourites out as a 'position:fixed' rail created lazily on document.body
// — mirrors the approved mockup's specimen C. Geometry is computed against
// the same chart-area element '#drawing-ctx-bar' positioning uses.
var _favRailEl = null;
var _favRailChips = null;
var _favDockHintEl = null;
var _favRailDrag = null; // {dx, dy, rect} while dragging; null otherwise

function _favRailChartRect() {
    var anchor = null;
    try { if (klineChart && klineChart.getDom) anchor = klineChart.getDom(); } catch(e) {}
    if (!anchor) anchor = document.getElementById('kline-chart-container');
    return anchor ? anchor.getBoundingClientRect() : null;
}

function _favRailLoadPersisted() {
    try {
        var d = JSON.parse(localStorage.getItem('klineFavRail') || 'null');
        if (d && typeof d === 'object') return d;
    } catch(e) {}
    return null;
}

function _favRailSavePersisted() {
    var docked = !_favRailEl || _favRailEl.style.display === 'none';
    var data = { docked: docked };
    if (!docked) {
        var rect = _favRailChartRect();
        var fixedLeft = parseInt(_favRailEl.style.left, 10) || 0;
        var fixedTop = parseInt(_favRailEl.style.top, 10) || 0;
        // Store relative to the chart rect so a window resize (which moves
        // the rect) doesn't strand the rail off-screen.
        data.left = rect ? (fixedLeft - rect.left) : fixedLeft;
        data.top = rect ? (fixedTop - rect.top) : fixedTop;
        data.orient = _favRailEl.classList.contains('vert') ? 'vert' : 'horiz';
    }
    try { localStorage.setItem('klineFavRail', JSON.stringify(data)); } catch(e) {}
}

function _favRailSetDocked(docked) {
    var grip = document.getElementById('fav-home-grip');
    var bar = document.getElementById('fav-tools-bar');
    var ghost = document.getElementById('fav-home-ghost');
    if (grip) grip.style.display = docked ? '' : 'none';
    if (bar) bar.style.display = docked ? '' : 'none';
    if (ghost) ghost.hidden = docked;
    if (_favRailEl) _favRailEl.style.display = docked ? 'none' : 'flex';
    _favRailSavePersisted();
}

function _favRailDockHome() {
    _favRailSetDocked(true);
    rebuildFavToolsBar();
}

function _favRailApplyPos(relLeft, relTop) {
    var rect = _favRailChartRect();
    if (!rect || !_favRailEl) return;
    _favRailEl.style.left = Math.round(rect.left + (relLeft || 0)) + 'px';
    _favRailEl.style.top = Math.round(rect.top + (relTop || 0)) + 'px';
    _favRailClamp();
}

function _favRailClamp() {
    if (!_favRailEl) return;
    var rect = _favRailChartRect();
    if (!rect) return;
    var w = _favRailEl.offsetWidth, h = _favRailEl.offsetHeight;
    var left = parseInt(_favRailEl.style.left, 10) || 0;
    var top = parseInt(_favRailEl.style.top, 10) || 0;
    left = Math.max(rect.left + 4, Math.min(left, rect.left + rect.width - w - 4));
    top = Math.max(rect.top + 4, Math.min(top, rect.top + rect.height - h - 4));
    _favRailEl.style.left = Math.round(left) + 'px';
    _favRailEl.style.top = Math.round(top) + 'px';
    _favRailSavePersisted();
}

function _favDockHintPosition(rect) {
    if (!_favDockHintEl) return;
    var homeRow = document.getElementById('toolbar-draw');
    var homeRect = homeRow ? homeRow.getBoundingClientRect() : rect;
    var zoneW = 54;
    var l = _favDockHintEl.querySelector('.fdz-l');
    var r = _favDockHintEl.querySelector('.fdz-r');
    var h = _favDockHintEl.querySelector('.fdz-home');
    var bandTop = Math.round(homeRect.bottom + 4);
    var bandH = Math.max(0, Math.round(rect.top + rect.height - homeRect.bottom - 8));
    if (l) {
        l.style.left = Math.round(rect.left + 4) + 'px';
        l.style.top = bandTop + 'px';
        l.style.width = zoneW + 'px';
        l.style.height = bandH + 'px';
    }
    if (r) {
        r.style.left = Math.round(rect.left + rect.width - zoneW - 4) + 'px';
        r.style.top = bandTop + 'px';
        r.style.width = zoneW + 'px';
        r.style.height = bandH + 'px';
    }
    if (h) {
        h.style.left = Math.round(homeRect.left) + 'px';
        h.style.top = Math.round(homeRect.top) + 'px';
        h.style.width = Math.round(homeRect.width) + 'px';
        h.style.height = Math.round(homeRect.height) + 'px';
    }
}

// Shared zone hit-test — the SAME test drives the live preview, the zone
// highlight, and the final drop, so the glowing hints can never disagree
// with what a release actually does. Judged by POINTER position (a wide
// horizontal rail's center can never reach the edge zones — the move clamp
// stops it ~100px short, so the hint and the drop would disagree).
function _favRailZoneAt(px, py, rect) {
    var homeRow = document.getElementById('toolbar-draw');
    var homeRect = homeRow ? homeRow.getBoundingClientRect() : null;
    if (homeRect && py <= homeRect.bottom + 4) return 'home';
    if (px - rect.left < 70) return 'left';
    if (rect.left + rect.width - px < 70) return 'right';
    return null;
}

// Live dock preview while dragging: entering an edge zone snaps the rail
// into its docked form (vertical, flush) immediately; leaving the zone
// reverts to the free-floating horizontal form. Release confirms whatever
// is on screen. Zone spans get .fdz-active while the pointer is inside.
function _favRailPreviewZone(zone, rect, e) {
    if (_favDockHintEl) {
        var zs = { left: '.fdz-l', right: '.fdz-r', home: '.fdz-home' };
        Object.keys(zs).forEach(function(k) {
            var el = _favDockHintEl.querySelector(zs[k]);
            if (el) el.classList.toggle('fdz-active', zone === k);
        });
    }
    if (!_favRailEl || !_favRailDrag) return;
    if (zone === 'left' || zone === 'right') {
        _favRailEl.classList.add('vert'); _favRailEl.classList.remove('horiz');
        var w = _favRailEl.offsetWidth, h = _favRailEl.offsetHeight;
        var left = zone === 'left' ? rect.left + 8 : rect.left + rect.width - w - 8;
        var top = Math.max(rect.top + 4, Math.min(e.clientY - 12, rect.top + rect.height - h - 4));
        _favRailEl.style.left = Math.round(left) + 'px';
        _favRailEl.style.top = Math.round(top) + 'px';
        // Keep the grip under the pointer for a drag back out of the zone.
        _favRailDrag.dx = 10; _favRailDrag.dy = 10;
    } else if (_favRailDrag.preview === 'left' || _favRailDrag.preview === 'right') {
        _favRailEl.classList.add('horiz'); _favRailEl.classList.remove('vert');
        _favRailDrag.dx = 10; _favRailDrag.dy = 10;
    }
    _favRailDrag.preview = zone;
}

function _favRailStartDrag(e, fromHome) {
    var rect = _favRailChartRect();
    if (!rect || !_favRailEl) return;
    if (fromHome) {
        _favRailSetDocked(false);
        _favRailEl.classList.add('horiz');
        _favRailEl.classList.remove('vert');
        var fx = Math.max(rect.left + 4, e.clientX - 20);
        var fy = Math.max(rect.top + 4, e.clientY - 12);
        _favRailEl.style.left = Math.round(fx) + 'px';
        _favRailEl.style.top = Math.round(fy) + 'px';
        rebuildFavToolsBar();
    }
    var r = _favRailEl.getBoundingClientRect();
    _favRailDrag = { dx: e.clientX - r.left, dy: e.clientY - r.top, rect: rect, px: e.clientX, py: e.clientY, preview: null };
    _favRailEl.classList.add('dragging');
    _favDockHintPosition(rect);
    if (_favDockHintEl) _favDockHintEl.classList.add('show');
    e.preventDefault();
}

function _favRailOnMove(e) {
    if (!_favRailDrag || !_favRailEl) return;
    // If the buttons bitmask reports nothing pressed, the pointerup was
    // missed (released outside the window, or a touch that never fired
    // pointerup) — end the drag defensively instead of gluing the rail to
    // wherever the cursor/finger last reported a move event.
    if (e.buttons === 0) { _favRailEndDrag(); return; }
    var rect = _favRailDrag.rect;
    _favRailDrag.px = e.clientX;
    _favRailDrag.py = e.clientY;
    var zone = _favRailZoneAt(e.clientX, e.clientY, rect);
    _favRailPreviewZone(zone, rect, e);
    if (zone === 'left' || zone === 'right') return; // preview parked it flush
    var x = e.clientX - rect.left - _favRailDrag.dx;
    var y = e.clientY - rect.top - _favRailDrag.dy;
    var w = _favRailEl.offsetWidth, h = _favRailEl.offsetHeight;
    x = Math.max(4, Math.min(x, rect.width - w - 4));
    y = Math.max(4, Math.min(y, rect.height - h - 4));
    _favRailEl.style.left = Math.round(rect.left + x) + 'px';
    _favRailEl.style.top = Math.round(rect.top + y) + 'px';
}

function _favRailEndDrag(e) {
    if (!_favRailDrag || !_favRailEl) return;
    var drag = _favRailDrag;
    var rect = drag.rect;
    _favRailDrag = null;
    _favRailEl.classList.remove('dragging');
    if (_favDockHintEl) {
        _favDockHintEl.classList.remove('show');
        var actives = _favDockHintEl.querySelectorAll('.fdz-active');
        for (var i = 0; i < actives.length; i++) actives[i].classList.remove('fdz-active');
    }
    // Same pointer-based zone test the live preview used — release confirms
    // exactly what the preview showed.
    var px = (e && e.type === 'pointerup' && typeof e.clientX === 'number') ? e.clientX : drag.px;
    var py = (e && e.type === 'pointerup' && typeof e.clientY === 'number') ? e.clientY : drag.py;
    var zone = _favRailZoneAt(px, py, rect);
    if (zone === 'home') {
        _favRailDockHome();
        return;
    }
    if (zone === 'left' || zone === 'right') {
        _favRailEl.classList.add('vert'); _favRailEl.classList.remove('horiz');
        _favRailEl.style.left = Math.round(zone === 'left' ? rect.left + 8 : rect.left + rect.width - _favRailEl.offsetWidth - 8) + 'px';
    }
    _favRailClamp();
}

// Creates the rail + dock-hint DOM lazily (first undock) and wires the
// document-level drag listeners exactly once (guarded by _favRailEl itself
// persisting across chart modal re-opens, since it lives on document.body).
function _favRailBuild() {
    if (_favRailEl) return;
    _favRailEl = document.createElement('div');
    _favRailEl.id = 'fav-rail';
    _favRailEl.className = 'fav-rail horiz';
    _favRailEl.style.display = 'none';
    _favRailEl.innerHTML =
        '<div class="fav-rail-grip" id="fav-rail-grip" title="Drag — edges dock, the row snaps home"></div>' +
        '<span class="fav-rail-chips" id="fav-rail-chips"></span>' +
        '<button class="fav-rail-flip" id="fav-rail-flip" title="Flip orientation">&#8635;</button>';
    document.body.appendChild(_favRailEl);
    _favRailChips = _favRailEl.querySelector('#fav-rail-chips');

    _favDockHintEl = document.createElement('div');
    _favDockHintEl.id = 'fav-dock-hint';
    _favDockHintEl.className = 'fav-dock-hint';
    _favDockHintEl.innerHTML = '<span class="fdz-l"></span><span class="fdz-r"></span><span class="fdz-home">Dock</span>';
    document.body.appendChild(_favDockHintEl);

    _favRailEl.querySelector('#fav-rail-grip').addEventListener('pointerdown', function(e) {
        _favRailStartDrag(e, false);
    });
    _favRailEl.querySelector('#fav-rail-flip').addEventListener('click', function(e) {
        e.stopPropagation();
        _favRailEl.classList.toggle('vert');
        _favRailEl.classList.toggle('horiz');
        _favRailClamp();
    });

    document.addEventListener('pointermove', _favRailOnMove);
    document.addEventListener('pointerup', _favRailEndDrag);
    // pointercancel (touch interrupted by e.g. a system gesture) never fires
    // pointerup — without this the rail would stay glued to the last known
    // pointer position mid-drag.
    document.addEventListener('pointercancel', _favRailEndDrag);
    window.addEventListener('resize', function() {
        if (_favRailEl && _favRailEl.style.display !== 'none') _favRailClamp();
    });
}

// Wires the home-grip (present every toolbar (re)build) + restores a
// persisted floating rail synchronously so there's no docked-bar flash
// before it moves (mirrors the drawPinBtn pinned-state restore pattern).
function _favRailInit() {
    var homeGrip = document.getElementById('fav-home-grip');
    if (homeGrip && !homeGrip._favWired) {
        homeGrip._favWired = true;
        homeGrip.addEventListener('pointerdown', function(e) {
            if (e.button !== undefined && e.button !== 0) return;
            _favRailBuild();
            _favRailStartDrag(e, true);
        });
    }
    var persisted = _favRailLoadPersisted();
    if (persisted && persisted.docked === false) {
        _favRailBuild();
        _favRailEl.classList.toggle('vert', persisted.orient === 'vert');
        _favRailEl.classList.toggle('horiz', persisted.orient !== 'vert');
        _favRailSetDocked(false);
        _favRailApplyPos(persisted.left, persisted.top);
    }
}
