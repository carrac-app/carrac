/* Display Options — Theme system (4 themes) + timeframe mode.
 * Deps: klineChart (global), timeframeMode (global), loadData and getCacheTTL (screener-table.js) */

var CHART_THEMES = {
    classic_dark: {
        label: 'Classic Dark',
        bg: '#131722',
        candle: { upColor: '#d1d4dc', downColor: '#4a4e59', upBorderColor: '#d1d4dc', downBorderColor: '#4a4e59', upWickColor: '#d1d4dc', downWickColor: '#4a4e59' },
        grid: { horizontal: { color: '#2a2e39' }, vertical: { color: '#2a2e39' } },
        axis: { line: '#2a2e39', tick: '#2a2e39', text: '#787b86' },
        crosshair: { line: '#787b86', bg: '#373b42', border: '#373b42', text: '#c9d1d9' },
        separator: '#2a2e39',
        priceMark: { high: '#787b86', low: '#787b86', noChange: '#787b86' },
        tooltipText: '#8b949e',
        indicatorText: '#ffffff'
    },
    trading_dark: {
        label: 'Trading Dark',
        bg: '#131722',
        candle: { upColor: '#26a69a', downColor: '#ef5350', upBorderColor: '#26a69a', downBorderColor: '#ef5350', upWickColor: '#26a69a', downWickColor: '#ef5350' },
        grid: { horizontal: { color: '#2a2e39' }, vertical: { color: '#2a2e39' } },
        axis: { line: '#2a2e39', tick: '#2a2e39', text: '#787b86' },
        crosshair: { line: '#787b86', bg: '#373b42', border: '#373b42', text: '#c9d1d9' },
        separator: '#2a2e39',
        priceMark: { high: '#787b86', low: '#787b86', noChange: '#787b86' },
        tooltipText: '#8b949e',
        indicatorText: '#ffffff'
    },
    light_trading: {
        label: 'Light Trading',
        bg: '#ffffff',
        candle: { upColor: '#26a69a', downColor: '#ef5350', upBorderColor: '#26a69a', downBorderColor: '#ef5350', upWickColor: '#26a69a', downWickColor: '#ef5350' },
        grid: { horizontal: { color: '#e0e0e0' }, vertical: { color: '#e0e0e0' } },
        axis: { line: '#ccc', tick: '#ccc', text: '#666' },
        crosshair: { line: '#999', bg: '#e0e0e0', border: '#ccc', text: '#333' },
        separator: '#ddd',
        priceMark: { high: '#555', low: '#555', noChange: '#999' },
        tooltipText: '#333',
        indicatorText: '#333'
    },
    light_classic: {
        label: 'Light Classic',
        bg: '#ffffff',
        candle: { upColor: '#333333', downColor: '#aaaaaa', upBorderColor: '#333333', downBorderColor: '#aaaaaa', upWickColor: '#333333', downWickColor: '#aaaaaa' },
        grid: { horizontal: { color: '#e0e0e0' }, vertical: { color: '#e0e0e0' } },
        axis: { line: '#ccc', tick: '#ccc', text: '#666' },
        crosshair: { line: '#999', bg: '#e0e0e0', border: '#ccc', text: '#333' },
        separator: '#ddd',
        priceMark: { high: '#555', low: '#555', noChange: '#999' },
        tooltipText: '#333',
        indicatorText: '#333'
    }
};

function getThemeId() {
    return localStorage.getItem('chartTheme') || 'trading_dark';
}

function applyTheme(themeId) {
    if (!CHART_THEMES[themeId]) themeId = 'trading_dark';
    localStorage.setItem('chartTheme', themeId);

    // Derive legacy keys for any code that still reads them
    var isGreenRed = (themeId === 'trading_dark' || themeId === 'light_trading');
    var isWhiteBg = (themeId === 'light_trading' || themeId === 'light_classic');
    localStorage.setItem('greenRedCandles', isGreenRed ? '1' : '0');
    localStorage.setItem('whiteBackground', isWhiteBg ? '1' : '0');

    if (typeof klineChart !== 'undefined' && klineChart) {
        _applyThemeToChart(themeId);
    }

    if (window.PreferenceSync) PreferenceSync.syncDisplayOptions();
}

function _applyThemeToChart(themeId) {
    var t = CHART_THEMES[themeId];
    if (!t || !klineChart) return;

    klineChart.setStyles({
        candle: {
            bar: t.candle,
            priceMark: {
                high: { color: t.priceMark.high },
                low: { color: t.priceMark.low },
                last: { noChangeColor: t.priceMark.noChange }
            },
            tooltip: { text: { color: t.tooltipText } }
        },
        grid: t.grid,
        indicator: {
            lastValueMark: { text: { color: t.indicatorText } }
        },
        xAxis: {
            axisLine: { color: t.axis.line },
            tickLine: { color: t.axis.tick },
            tickText: { color: t.axis.text }
        },
        yAxis: {
            axisLine: { color: t.axis.line },
            tickLine: { color: t.axis.tick },
            tickText: { color: t.axis.text, marginStart: 2, marginEnd: 4 }
        },
        separator: { color: t.separator },
        crosshair: {
            horizontal: { line: { color: t.crosshair.line }, text: { backgroundColor: t.crosshair.bg, borderColor: t.crosshair.border, color: t.crosshair.text } },
            vertical: { line: { color: t.crosshair.line }, text: { backgroundColor: t.crosshair.bg, borderColor: t.crosshair.border, color: t.crosshair.text } }
        }
    });

    var container = document.getElementById('kline-chart-container');
    if (container) container.style.backgroundColor = t.bg;
}

function initDisplayOptions() {
    // One-time migration from legacy keys to chartTheme
    if (!localStorage.getItem('chartTheme')) {
        var greenRed = localStorage.getItem('greenRedCandles') === '1';
        var whiteBg = localStorage.getItem('whiteBackground') === '1';
        if (whiteBg && greenRed) localStorage.setItem('chartTheme', 'light_trading');
        else if (whiteBg) localStorage.setItem('chartTheme', 'light_classic');
        else if (greenRed) localStorage.setItem('chartTheme', 'trading_dark');
        else localStorage.setItem('chartTheme', 'trading_dark');  // new default
    }

    // Backward-compat: update legacy checkboxes if they exist on the page
    var themeId = getThemeId();
    var isGreenRed = (themeId === 'trading_dark' || themeId === 'light_trading');
    var isWhiteBg = (themeId === 'light_trading' || themeId === 'light_classic');

    var greenRedCb = document.getElementById('view-green-red');
    var whiteBgCb = document.getElementById('view-white-bg');
    if (greenRedCb) greenRedCb.checked = isGreenRed;
    if (whiteBgCb) whiteBgCb.checked = isWhiteBg;
}

// Legacy wrapper — kept for backward compat with code calling toggleGreenRedCandles()
function toggleGreenRedCandles() {
    var el = document.getElementById('view-green-red');
    if (!el) return;
    var checked = el.checked;
    var isWhiteBg = localStorage.getItem('whiteBackground') === '1';
    if (checked) {
        applyTheme(isWhiteBg ? 'light_trading' : 'trading_dark');
    } else {
        applyTheme(isWhiteBg ? 'light_classic' : 'classic_dark');
    }
}

// Legacy wrapper
function toggleWhiteBackground() {
    var el = document.getElementById('view-white-bg');
    if (!el) return;
    var checked = el.checked;
    var isGreenRed = localStorage.getItem('greenRedCandles') === '1';
    if (checked) {
        applyTheme(isGreenRed ? 'light_trading' : 'light_classic');
    } else {
        applyTheme(isGreenRed ? 'trading_dark' : 'classic_dark');
    }
}

// Timeframe mode functions
function setTimeframeMode(mode) {
    if (mode === timeframeMode) return;

    timeframeMode = mode;
    localStorage.setItem('timeframeMode', mode);
    if (window.PreferenceSync) PreferenceSync.syncDisplayOptions();

    // Update button states
    var htfBtn = document.getElementById('htf-btn');
    var ltfBtn = document.getElementById('ltf-btn');
    if (mode === 'htf') {
        if (htfBtn) htfBtn.classList.add('active');
        if (ltfBtn) ltfBtn.classList.remove('active');
    } else {
        if (htfBtn) htfBtn.classList.remove('active');
        if (ltfBtn) ltfBtn.classList.add('active');
    }

    // Update column headers
    updateColumnHeaders();

    // Reset refresh interval
    if (refreshIntervalId) {
        clearInterval(refreshIntervalId);
    }
    refreshIntervalId = setInterval(function() { loadData(false); }, getCacheTTL());

    // Check if we have cached data for the new mode
    var cached = getCachedData();
    if (cached && cached.data && !isCacheStale(cached)) {
        // Use cached data immediately - smooth switch!
        cached.data._cacheTime = cached.timestamp;
        renderData(cached.data, true);
    } else {
        // No cache - show loading and fetch
        var loadingEl = document.getElementById("loading");
        var tableEl = document.getElementById("table-wrapper");
        if (loadingEl) loadingEl.style.display = "block";
        if (tableEl) tableEl.style.display = "none";
        loadData(false);
    }
}

function updateColumnHeaders() {
    var headers = {
        'htf': { tf1: '1D', tf2: '3D', tf3: '1W', rsi: 'W-RSI',
                 tfTitle: 'Per-timeframe Consensio signal (-3 to +3)',
                 rsiTitle: 'Weekly RSI (14)' },
        'ltf': { tf1: '1h', tf2: '2h', tf3: '4h', rsi: '4h-RSI',
                 tfTitle: 'Per-timeframe Consensio signal (-3 to +3) — 1h/2h/4h mode',
                 rsiTitle: '4-hour RSI (14)' }
    };
    var h = headers[timeframeMode];

    // Update main table headers
    var tables = ['data-table', 'watchlist-table'];
    tables.forEach(function(tableId) {
        var table = document.getElementById(tableId);
        if (!table) return;

        var th1d = table.querySelector('th[data-sort="signal_1d"]');
        var th3d = table.querySelector('th[data-sort="signal_3d"]');
        var th1w = table.querySelector('th[data-sort="signal_1w"]');
        var thRsi = table.querySelector('th[data-sort="rsi"]');

        if (th1d) { th1d.textContent = h.tf1; th1d.title = h.tfTitle; }
        if (th3d) { th3d.textContent = h.tf2; th3d.title = h.tfTitle; }
        if (th1w) { th1w.textContent = h.tf3; th1w.title = h.tfTitle; }
        if (thRsi) { thRsi.textContent = h.rsi; thRsi.title = h.rsiTitle; }
    });
}

function initTimeframeMode() {
    // Apply saved mode on load
    var savedMode = localStorage.getItem('timeframeMode') || 'htf';
    timeframeMode = savedMode;

    // Update button states
    var htfBtn = document.getElementById('htf-btn');
    var ltfBtn = document.getElementById('ltf-btn');
    if (savedMode === 'htf') {
        if (htfBtn) htfBtn.classList.add('active');
        if (ltfBtn) ltfBtn.classList.remove('active');
    } else {
        if (htfBtn) htfBtn.classList.remove('active');
        if (ltfBtn) ltfBtn.classList.add('active');
    }

    // Update column headers
    updateColumnHeaders();

    // Set up refresh interval
    refreshIntervalId = setInterval(function() { loadData(false); }, getCacheTTL());
}
