/**
 * Preference Sync Module — Cross-device sync for display options,
 * indicator settings, and chart preferences.
 *
 * Reuses the drawing sync poll endpoint (/api/sync/poll) which now also
 * returns prefs_changed/prefs_ts. When a remote preference change is
 * detected, fetches /api/preferences/all and applies to localStorage + UI.
 *
 * Deps: DrawingTools (for SYNC_SESSION_ID), klineActiveIndicators (global)
 */

(function() {
    'use strict';

    var API_BASE = '/api';
    var DEBOUNCE_MS = 2000;  // Aggressive debounce for rapid toggles

    // State
    var _prefsSyncTs = 0;
    var _debounceTimer = null;
    var _pendingKeys = {};   // key -> value to batch-save
    var _suppressPrefsSync = false;

    function _getSyncSessionId() {
        return (window.DrawingTools && DrawingTools.SYNC_SESSION_ID) || '';
    }

    function _getCSRF() {
        return (typeof getCSRFToken === 'function') ? getCSRFToken() : '';
    }

    // ==================== Outgoing: Save preferences to server ====================

    /**
     * Queue a preference key for server sync (debounced).
     * Multiple calls within DEBOUNCE_MS are batched.
     */
    function syncPreference(key, value) {
        if (_suppressPrefsSync) return;
        _pendingKeys[key] = value;
        clearTimeout(_debounceTimer);
        _debounceTimer = setTimeout(_flushPending, DEBOUNCE_MS);
    }

    function _flushPending() {
        var keys = Object.keys(_pendingKeys);
        if (keys.length === 0) return;
        // Skip sync for free tier (returns 403, clutters console)
        if (window._userTier === 'free') { _pendingKeys = {}; return; }

        // Send each key as a separate request (they're different pref categories)
        keys.forEach(function(key) {
            var value = _pendingKeys[key];
            fetch(API_BASE + '/preferences/sync', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': _getCSRF(),
                    'X-Sync-Session': _getSyncSessionId()
                },
                credentials: 'same-origin',
                body: JSON.stringify({ key: key, value: value })
            }).catch(function(e) { console.warn('[PrefsSync] Save error:', e); });
        });

        _pendingKeys = {};
    }

    // ==================== Convenience: Sync display options ====================

    function syncDisplayOptions() {
        syncPreference('display_options', {
            chartTheme: localStorage.getItem('chartTheme') || 'trading_dark',
            greenRedCandles: localStorage.getItem('greenRedCandles') || '0',
            whiteBackground: localStorage.getItem('whiteBackground') || '0',
            timeframeMode: localStorage.getItem('timeframeMode') || 'htf'
        });
    }

    // ==================== Convenience: Sync indicator toggles ====================

    function syncIndicatorToggles() {
        if (typeof klineActiveIndicators === 'undefined') return;
        // This goes through /api/indicator-prefs which now publishes sync events
        try {
            fetch(API_BASE + '/indicator-prefs', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': _getCSRF(),
                    'X-Sync-Session': _getSyncSessionId()
                },
                credentials: 'same-origin',
                body: JSON.stringify({ indicators: klineActiveIndicators })
            });
        } catch(e) {}
    }

    // ==================== Incoming: Apply remote preference changes ====================

    function applyRemotePreferences(newTs) {
        fetch(API_BASE + '/preferences/all', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data) return;
            _suppressPrefsSync = true;

            var changed = false;

            // Apply display options
            if (data.display_options) {
                var d = data.display_options;

                // Prefer chartTheme if present (new system)
                if (d.chartTheme && d.chartTheme !== localStorage.getItem('chartTheme')) {
                    if (typeof applyTheme === 'function') {
                        applyTheme(d.chartTheme);
                    } else {
                        localStorage.setItem('chartTheme', d.chartTheme);
                    }
                    changed = true;
                } else {
                    // Legacy fallback: apply from greenRedCandles + whiteBackground
                    if (d.greenRedCandles !== undefined && d.greenRedCandles !== localStorage.getItem('greenRedCandles')) {
                        localStorage.setItem('greenRedCandles', d.greenRedCandles);
                        var greenRedCb = document.getElementById('view-green-red');
                        if (greenRedCb) greenRedCb.checked = d.greenRedCandles === '1';
                        changed = true;
                    }
                    if (d.whiteBackground !== undefined && d.whiteBackground !== localStorage.getItem('whiteBackground')) {
                        localStorage.setItem('whiteBackground', d.whiteBackground);
                        var whiteBgCb = document.getElementById('view-white-bg');
                        if (whiteBgCb) whiteBgCb.checked = d.whiteBackground === '1';
                        changed = true;
                    }
                    // If legacy keys changed, derive and apply theme
                    if (changed && typeof applyTheme === 'function') {
                        var gr = localStorage.getItem('greenRedCandles') === '1';
                        var wb = localStorage.getItem('whiteBackground') === '1';
                        var derived = (wb && gr) ? 'light_trading' : wb ? 'light_classic' : gr ? 'trading_dark' : 'classic_dark';
                        applyTheme(derived);
                    }
                }

                if (d.timeframeMode && d.timeframeMode !== localStorage.getItem('timeframeMode')) {
                    localStorage.setItem('timeframeMode', d.timeframeMode);
                    if (typeof setTimeframeMode === 'function') {
                        setTimeframeMode(d.timeframeMode);
                    }
                    changed = true;
                }
            }

            // Apply indicator preferences
            if (data.indicator_prefs) {
                var ip = data.indicator_prefs;

                // Active indicator toggles
                if (ip.indicators && typeof klineActiveIndicators !== 'undefined') {
                    var indChanged = false;
                    for (var k in ip.indicators) {
                        if (k in klineActiveIndicators && klineActiveIndicators[k] !== ip.indicators[k]) {
                            klineActiveIndicators[k] = ip.indicators[k];
                            indChanged = true;
                        }
                    }
                    if (indChanged) {
                        try { localStorage.setItem('klineActiveIndicators', JSON.stringify(klineActiveIndicators)); } catch(e) {}
                        // Update indicator menu checkboxes
                        document.querySelectorAll('.indicator-menu-item[data-ind]').forEach(function(item) {
                            var ind = item.getAttribute('data-ind');
                            var check = item.querySelector('.ind-checkbox');
                            item.classList.toggle('active', !!klineActiveIndicators[ind]);
                            if (check) check.classList.toggle('active', !!klineActiveIndicators[ind]);
                        });
                        // Re-render indicators on chart
                        if (typeof drawIndicators === 'function' && typeof klineChart !== 'undefined' && klineChart) {
                            drawIndicators();
                        }
                        changed = true;
                    }
                }

                // Favourite indicators (pills)
                if (ip.favourites && typeof klineFavouriteIndicators !== 'undefined') {
                    var serverFavs = JSON.stringify(ip.favourites);
                    var localFavs = JSON.stringify(klineFavouriteIndicators);
                    if (serverFavs !== localFavs) {
                        klineFavouriteIndicators.length = 0;
                        ip.favourites.forEach(function(f) { klineFavouriteIndicators.push(f); });
                        try { localStorage.setItem('klineFavouriteIndicators', JSON.stringify(klineFavouriteIndicators)); } catch(e) {}
                        if (typeof renderFavouritePills === 'function') renderFavouritePills();
                        changed = true;
                    }
                }

                // Indicator groups
                if (ip.groups && typeof klineIndicatorGroups !== 'undefined') {
                    var serverGroups = JSON.stringify(ip.groups);
                    var localGroups = JSON.stringify(klineIndicatorGroups);
                    if (serverGroups !== localGroups) {
                        klineIndicatorGroups.length = 0;
                        ip.groups.forEach(function(g) { klineIndicatorGroups.push(g); });
                        try { localStorage.setItem('klineIndicatorGroups', JSON.stringify(klineIndicatorGroups)); } catch(e) {}
                        if (typeof renderFavouritePills === 'function') renderFavouritePills();
                        changed = true;
                    }
                }

                // Indicator param overrides
                if (ip.params && typeof IndicatorSettings !== 'undefined' && IndicatorSettings.applyOverrides) {
                    var serverParams = JSON.stringify(ip.params);
                    var localParams = localStorage.getItem('indicatorParamOverrides') || '{}';
                    if (serverParams !== localParams) {
                        IndicatorSettings.applyOverrides(ip.params);
                        changed = true;
                    }
                }
            }

            // Apply ticker preferences
            if (data.ticker_prefs) {
                var serverTicker = JSON.stringify(data.ticker_prefs);
                var localTicker = localStorage.getItem('tickerBarConfig') || '';
                if (serverTicker !== localTicker) {
                    try {
                        localStorage.setItem('tickerBarConfig', serverTicker);
                        if (typeof _tickerRebuild === 'function') _tickerRebuild();
                        changed = true;
                    } catch(e) {}
                }
            }

            // Apply news panel preferences
            if (data.news_panel_prefs) {
                var serverNp = JSON.stringify(data.news_panel_prefs);
                var localNp = localStorage.getItem('newsPanelPrefs') || '';
                if (serverNp !== localNp) {
                    try {
                        localStorage.setItem('newsPanelPrefs', serverNp);
                        if (localStorage.getItem('panelViewMode') === 'news' && typeof refreshNewsPanel === 'function') refreshNewsPanel(true);
                        changed = true;
                    } catch(e) {}
                }
            }

            // Apply chart preferences
            if (data.chart_prefs) {
                var cp = data.chart_prefs;
                if (cp.lastViewedAsset && cp.lastViewedAsset !== localStorage.getItem('lastViewedAsset')) {
                    localStorage.setItem('lastViewedAsset', cp.lastViewedAsset);
                    // Don't auto-switch the chart — just save for next load
                }
            }

            // Apply timeframe favourites
            if (data.tf_favourites !== undefined) {
                var serverTf = JSON.stringify(data.tf_favourites);
                var localTf = localStorage.getItem('tfFavourites') || '';
                if (serverTf !== localTf) {
                    if (data.tf_favourites) {
                        localStorage.setItem('tfFavourites', serverTf);
                    } else {
                        localStorage.removeItem('tfFavourites');
                    }
                    // Rebuild the timeframe bar so the new favourites show immediately
                    try {
                        if (typeof _rebuildTfBar === 'function' && typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
                            _rebuildTfBar(klineChartSymbol);
                        }
                    } catch(e) {}
                    changed = true;
                }
            }

            if (newTs && newTs > _prefsSyncTs) {
                _prefsSyncTs = newTs;
            }

            setTimeout(function() { _suppressPrefsSync = false; }, 500);

            if (changed) {
            }
        })
        .catch(function(e) {
            console.warn('[PrefsSync] Apply error:', e);
            _suppressPrefsSync = false;
        });
    }

    // ==================== Poll Integration ====================

    /**
     * Called by drawing-tools.js poll handler when prefs_changed is true.
     * Can also be called standalone.
     */
    function handlePrefsPollResult(data) {
        if (data.prefs_changed) {
            applyRemotePreferences(data.prefs_ts);
        } else if (data.prefs_ts && data.prefs_ts > _prefsSyncTs) {
            _prefsSyncTs = data.prefs_ts;
        }
    }

    function getPrefsSyncTs() {
        return _prefsSyncTs;
    }

    // ==================== One-time migration ====================

    /**
     * On first load, push current localStorage display options to server
     * if the server doesn't have them yet.
     */
    function _migrateOnFirstLoad() {
        fetch(API_BASE + '/preferences/all', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data) return;
            // If server has no display_options, push current localStorage state
            if (!data.display_options) {
                var current = {
                    chartTheme: localStorage.getItem('chartTheme') || 'trading_dark',
                    greenRedCandles: localStorage.getItem('greenRedCandles') || '0',
                    whiteBackground: localStorage.getItem('whiteBackground') || '0',
                    timeframeMode: localStorage.getItem('timeframeMode') || 'htf'
                };
                // Only push if user has non-default values
                if (current.chartTheme !== 'trading_dark' || current.timeframeMode !== 'htf') {
                    syncPreference('display_options', current);
                }
            } else {
                // Server has prefs — apply them (server wins on first load)
                _suppressPrefsSync = true;
                var d = data.display_options;
                if (d.chartTheme) {
                    // Use applyTheme() to set localStorage AND update chart visually
                    if (typeof applyTheme === 'function') {
                        applyTheme(d.chartTheme);
                    } else {
                        localStorage.setItem('chartTheme', d.chartTheme);
                    }
                    // Derive legacy keys
                    var gr = (d.chartTheme === 'trading_dark' || d.chartTheme === 'light_trading');
                    var wb = (d.chartTheme === 'light_trading' || d.chartTheme === 'light_classic');
                    localStorage.setItem('greenRedCandles', gr ? '1' : '0');
                    localStorage.setItem('whiteBackground', wb ? '1' : '0');
                } else {
                    if (d.greenRedCandles !== undefined) localStorage.setItem('greenRedCandles', d.greenRedCandles);
                    if (d.whiteBackground !== undefined) localStorage.setItem('whiteBackground', d.whiteBackground);
                    // Derive theme and apply visually
                    var _gr = localStorage.getItem('greenRedCandles') === '1';
                    var _wb = localStorage.getItem('whiteBackground') === '1';
                    var derived = (_wb && _gr) ? 'light_trading' : _wb ? 'light_classic' : _gr ? 'trading_dark' : 'classic_dark';
                    if (typeof applyTheme === 'function') {
                        applyTheme(derived);
                    }
                }
                if (d.timeframeMode !== undefined) localStorage.setItem('timeframeMode', d.timeframeMode);
                setTimeout(function() { _suppressPrefsSync = false; }, 500);
            }

            // Sync chart_prefs if not on server
            if (!data.chart_prefs) {
                var lastAsset = localStorage.getItem('lastViewedAsset');
                if (lastAsset) {
                    syncPreference('chart_prefs', { lastViewedAsset: lastAsset });
                }
            }

            // Restore ticker config from server, or push current to server
            if (data.ticker_prefs) {
                try {
                    localStorage.setItem('tickerBarConfig', JSON.stringify(data.ticker_prefs));
                    if (typeof _tickerRebuild === 'function') _tickerRebuild();
                } catch(e) {}
            } else {
                var tickerCfg = localStorage.getItem('tickerBarConfig');
                if (tickerCfg) {
                    try { syncPreference('ticker_prefs', JSON.parse(tickerCfg)); } catch(e) {}
                }
            }

            // Restore news panel prefs from server, or push current to server
            if (data.news_panel_prefs) {
                try { localStorage.setItem('newsPanelPrefs', JSON.stringify(data.news_panel_prefs)); } catch(e) {}
            } else {
                var npPrefs = localStorage.getItem('newsPanelPrefs');
                if (npPrefs) {
                    try { syncPreference('news_panel_prefs', JSON.parse(npPrefs)); } catch(e) {}
                }
            }

            // Restore timeframe favourites from server, or push current to server
            if (data.tf_favourites) {
                try { localStorage.setItem('tfFavourites', JSON.stringify(data.tf_favourites)); } catch(e) {}
                // Chart toolbar was built before this hydrate — rebuild with server favs
                try {
                    if (typeof _rebuildTfBar === 'function' && typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
                        _rebuildTfBar(klineChartSymbol);
                    }
                } catch(e) {}
            } else {
                var tfFavs = localStorage.getItem('tfFavourites');
                if (tfFavs) {
                    try { syncPreference('tf_favourites', JSON.parse(tfFavs)); } catch(e) {}
                }
            }

        })
        // SILENCE-OK: every branch in this .then() already falls back to (or
        // pushes) the localStorage copy when the server has nothing — see the
        // tf_favourites else-branch above for the pattern repeated per-pref.
        // A failed hydrate just means this device keeps using its local prefs.
        .catch(function() {});
    }

    // ==================== Initialization ====================

    function init() {
        // Run migration check quickly (all modules init synchronously before this)
        setTimeout(_migrateOnFirstLoad, 200);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose public API
    window.PreferenceSync = {
        syncPreference: syncPreference,
        syncDisplayOptions: syncDisplayOptions,
        syncIndicatorToggles: syncIndicatorToggles,
        handlePrefsPollResult: handlePrefsPollResult,
        getPrefsSyncTs: getPrefsSyncTs,
        applyRemotePreferences: applyRemotePreferences
    };

})();
