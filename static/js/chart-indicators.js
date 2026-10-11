// Chart indicators: toggling them on and off (toggleKlineIndicator,
// toggleAllIndicators), applying the active set to a chart
// (applyKlineIndicators), and the custom regime, quant-pane and last-price
// indicators. Needs app-core.js and indicator-prefs.js loaded first.

// Enforce volume pane is always the first sub-pane (directly under candle pane)
// Debounced — coalesces rapid calls during group toggles into a single execution
var _volumeEnforceTimer = null;
function _enforceVolumePaneOrder() {
    if (_volumeEnforceTimer) clearTimeout(_volumeEnforceTimer);
    _volumeEnforceTimer = setTimeout(function() {
        _volumeEnforceTimer = null;
        if (!klineChart || !klineActiveIndicators.VOL) return;
        try {
            klineChart.setPaneOptions({ id: 'volume_pane', order: 1 });
            var otherPanes = [
                'rsi_pane', 'macd_pane', 'wr_pane', 'cci_pane', 'stoch_pane', 'stochrsi_pane', 'obv_pane',
                'cvd_spot_pane', 'oi_pane', 'cvd_usdt_pane', 'cvd_coin_pane', 'deriv_combined_pane'
            ];
            for (var i = 0; i < otherPanes.length; i++) {
                try { klineChart.setPaneOptions({ id: otherPanes[i], order: 2 + i }); } catch(e) {}
            }
        } catch(e) {}
    }, 30);
}

function toggleKlineIndicator(indicator) {
    klineActiveIndicators[indicator] = !klineActiveIndicators[indicator];

    // Track custom indicator toggle
    if (indicator.indexOf('CUSTOM_') === 0 && typeof posthog !== 'undefined' && posthog.capture) {
        posthog.capture('custom_indicator_toggled', { enabled: !!klineActiveIndicators[indicator] });
    }

    // Exit pane fullscreen if active (pane layout is about to change)
    if (typeof _fullscreenPaneId !== 'undefined' && _fullscreenPaneId) {
        exitPaneFullscreen();
    }

    // Persist to localStorage
    try { localStorage.setItem('klineActiveIndicators', JSON.stringify(klineActiveIndicators)); } catch(e) {}

    // Save to backend for morning report screenshots
    try {
        fetch('/api/indicator-prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken(), 'X-Sync-Session': (window.DrawingTools && DrawingTools.SYNC_SESSION_ID) || '' },
            credentials: 'same-origin',
            body: JSON.stringify({ indicators: klineActiveIndicators })
        });
    } catch(e) {}

    // Update menu item display
    var menuItem = document.querySelector('.indicator-menu-item[data-ind="' + indicator + '"]');
    if (menuItem) {
        var check = menuItem.querySelector('.ind-checkbox');
        menuItem.classList.toggle('active', klineActiveIndicators[indicator]);
        if (check) {
            check.classList.toggle('active', klineActiveIndicators[indicator]);
        }
    }

    // Update favourite pill active state
    var pill = document.querySelector('.ind-fav-pill[data-ind="' + indicator + '"]');
    if (pill) pill.classList.toggle('active', !!klineActiveIndicators[indicator]);

    // Keep group pills in sync — group active state derives from member indicators
    if (typeof _refreshGroupPillStates === 'function') _refreshGroupPillStates();

    // Volume Profile: managed separately (canvas overlay, not KLineChart indicator)
    if (indicator === 'VP') {
        if (klineActiveIndicators.VP) {
            vpStart();
        } else {
            vpStop();
        }
        return;
    }

    // Key Levels / Monday Range: native canvas indicator (like VP)
    if (indicator === 'KEY_LEVELS' || indicator === 'MON_RANGE') {
        if (klineActiveIndicators.KEY_LEVELS || klineActiveIndicators.MON_RANGE) {
            klStart();
        } else {
            klStop();
        }
        return;
    }

    // Confluence Zones: native canvas indicator (fetches from /api/confluence)
    if (indicator === 'CONFLUENCE') {
        if (klineActiveIndicators.CONFLUENCE) {
            confluenceStart();
        } else {
            confluenceStop();
        }
        return;
    }

    // Liquidation Heatmap: native canvas indicator (fetches from /api/liq-heatmap)
    if (indicator === 'LIQ_HEATMAP') {
        if (klineActiveIndicators.LIQ_HEATMAP) {
            liqHeatmapStart();
        } else {
            liqHeatmapStop();
        }
        return;
    }

    // Re-apply indicators without full chart reload (preserves drawings/positions)
    if (klineChart && klineChartSymbol) {
        var builtInPanes = IndicatorRegistry.getBuiltInPanes();
        var builtInOverlays = IndicatorRegistry.getBuiltInOverlays();
        var cvdGroups = { CVD_SPOT: 'cvdSpotIndicator', CVD_USDT: 'cvdUsdtIndicator', CVD_COIN: 'cvdCoinIndicator', OI: 'oiIndicator' };

        if (indicator in builtInPanes) {
            // Built-in pane indicator: add or remove pane
            if (klineActiveIndicators[indicator]) {
                var indName = indicator === 'VOL' ? 'VOL' : indicator;
                var _oscPanes = { RSI: 1, WILLIAMS_R: 1, CCI: 1, STOCH: 1, STOCH_RSI: 1, OBV: 1 };
                var paneOpts = { id: builtInPanes[indicator], height: indicator === 'VOL' ? 100 : (indicator in _oscPanes ? 90 : 70), minHeight: 40 };
                var _indMeta = IndicatorRegistry.getIndicator(indicator);
                var _cp = _indMeta && _indMeta.calcParams ? _indMeta.calcParams.slice() : null;
                if (indicator === 'RSI') {
                    klineChart.createIndicator({ name: 'RSI', calcParams: _cp || [14] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'rsi_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else if (indicator === 'WILLIAMS_R') {
                    klineChart.createIndicator({ name: 'WILLIAMS_R', calcParams: _cp || [14] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'wr_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else if (indicator === 'CCI') {
                    klineChart.createIndicator({ name: 'CCI', calcParams: _cp || [20] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'cci_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else if (indicator === 'STOCH') {
                    klineChart.createIndicator({ name: 'STOCH', calcParams: _cp || [14, 3, 3] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'stoch_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else if (indicator === 'STOCH_RSI') {
                    klineChart.createIndicator({ name: 'STOCH_RSI', calcParams: _cp || [14, 14, 3, 3] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'stochrsi_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else if (indicator === 'OBV') {
                    klineChart.createIndicator({ name: 'OBV' }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'obv_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else if (indicator === 'VOL') {
                    klineChart.createIndicator({ name: 'VOL', calcParams: _cp || [20] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'volume_pane', gap: { top: 0.02, bottom: 0 } });
                }
                else if (indicator === 'MACD') {
                    klineChart.createIndicator({ name: 'MACD', calcParams: _cp || [12, 26, 9] }, false, paneOpts);
                    klineChart.setPaneOptions({ id: 'macd_pane', gap: { top: 0.05, bottom: 0.05 } });
                }
                else klineChart.createIndicator(indicator, false, paneOpts);
                // Reload sub-pane drawings after pane is created
                var _pId = builtInPanes[indicator];
                setTimeout(function() { if (typeof loadKlineDrawingsForPane === 'function') loadKlineDrawingsForPane(_pId); }, 100);
                // Enforce volume pane always first under candle pane
                _enforceVolumePaneOrder();
            } else {
                try { klineChart.removeIndicator(builtInPanes[indicator]); } catch(e) {}
            }
        } else if (indicator in builtInOverlays) {
            // Built-in overlay indicator (MA/EMA/BOLL on candle pane)
            if (klineActiveIndicators[indicator]) {
                var _ovMeta = IndicatorRegistry.getIndicator(indicator);
                var _ovCp = _ovMeta && _ovMeta.calcParams;
                var _ovCreate = _ovCp ? { name: indicator, calcParams: _ovCp.slice() } : indicator;
                klineChart.createIndicator(_ovCreate, true, { id: 'candle_pane' });
            } else {
                try { klineChart.removeIndicator('candle_pane', indicator); } catch(e) {}
            }
        } else if (indicator in cvdGroups) {
            // CVD/OI: remove overlays + pane, then re-apply from cache
            var cvdPaneMap = { CVD_SPOT: 'cvd_spot_pane', CVD_USDT: 'cvd_usdt_pane', CVD_COIN: 'cvd_coin_pane', OI: 'oi_pane' };
            try { klineChart.removeOverlay({ groupId: cvdGroups[indicator] }); } catch(e) {}
            try { klineChart.removeIndicator(cvdPaneMap[indicator]); } catch(e) {}
            if (klineActiveIndicators[indicator]) {
                var _cvdPId = cvdPaneMap[indicator];
                var cacheKey = klineChartSymbol + '_' + klineChartTimeframe;
                var cached = klineDataCache[cacheKey];
                var derivKeyMap = { OI: 'oi_hist', CVD_USDT: 'cvd_usdt', CVD_COIN: 'cvd_coin', CVD_SPOT: 'cvd_spot' };
                var derivKey = derivKeyMap[indicator] || null;
                if (cached && cached.data && derivKey && !cached.data[derivKey]) {
                    // Data was fetched without this derivative — re-fetch with it
                    var ltfSet3 = ['1m','3m','5m','15m','30m','1h','4h'];
                    var m3 = ltfSet3.indexOf(klineChartTimeframe) >= 0 ? 'ltf' : 'htf';
                    var mb3 = (m3 === 'ltf') ? 2500 : 1500;
                    fetch('/api/chart/' + encodeURIComponent(klineChartSymbol) + '?days=' + mb3 + '&mode=' + m3 + '&timeframe=' + encodeURIComponent(klineChartTimeframe) + _derivativesParam(), {
                        credentials: 'same-origin'
                    }).then(function(r) { return r.json(); }).then(function(full) {
                        if (full && full.ohlcv) {
                            klineCacheSave(cacheKey, full);
                            applyKlineIndicators(full, convertToKlineData(full), true);
                        }
                        // Reload sub-pane drawings after indicators applied
                        setTimeout(function() { if (typeof loadKlineDrawingsForPane === 'function') loadKlineDrawingsForPane(_cvdPId); }, 100);
                    // SILENCE-OK: indicator-toggle refetch — a miss just means this
                    // derivative series doesn't render; toggling the indicator off/on
                    // retries it, and the rest of the chart is unaffected.
                    }).catch(function() {});
                } else if (cached) {
                    applyKlineIndicators(cached.data, convertToKlineData(cached.data), true);
                    // Reload sub-pane drawings after indicators applied
                    setTimeout(function() { if (typeof loadKlineDrawingsForPane === 'function') loadKlineDrawingsForPane(_cvdPId); }, 100);
                }
            }
        } else if (indicator === 'DERIV_COMBINED') {
            // Combined derivatives: remove pane, then re-apply from cache
            try { klineChart.removeIndicator('deriv_combined_pane'); } catch(e) {}
            if (klineActiveIndicators.DERIV_COMBINED) {
                var cacheKeyDC = klineChartSymbol + '_' + klineChartTimeframe;
                var cachedDC = klineDataCache[cacheKeyDC];
                var needsFetch = !cachedDC || !cachedDC.data || (!cachedDC.data.oi_hist && !cachedDC.data.cvd_usdt && !cachedDC.data.cvd_coin);
                if (needsFetch) {
                    var ltfSetDC = ['1m','3m','5m','15m','30m','1h','4h'];
                    var mDC = ltfSetDC.indexOf(klineChartTimeframe) >= 0 ? 'ltf' : 'htf';
                    var mbDC = (mDC === 'ltf') ? 2500 : 1500;
                    fetch('/api/chart/' + encodeURIComponent(klineChartSymbol) + '?days=' + mbDC + '&mode=' + mDC + '&timeframe=' + encodeURIComponent(klineChartTimeframe) + _derivativesParam(), {
                        credentials: 'same-origin'
                    }).then(function(r) { return r.json(); }).then(function(full) {
                        if (full && full.ohlcv) {
                            klineCacheSave(cacheKeyDC, full);
                            applyKlineIndicators(full, convertToKlineData(full), true);
                        }
                        setTimeout(function() { if (typeof loadKlineDrawingsForPane === 'function') loadKlineDrawingsForPane('deriv_combined_pane'); }, 100);
                    // SILENCE-OK: same as the OI/CVD refetch above — retry is a
                    // re-toggle of the indicator, chart itself is unaffected.
                    }).catch(function() {});
                } else if (cachedDC) {
                    applyKlineIndicators(cachedDC.data, convertToKlineData(cachedDC.data), true);
                    setTimeout(function() { if (typeof loadKlineDrawingsForPane === 'function') loadKlineDrawingsForPane('deriv_combined_pane'); }, 100);
                }
            }
        } else if (indicator === 'MKT_STRUCTURE' || indicator === 'ORDER_BLOCKS' || indicator === 'EQ_HL' || indicator === 'VALUE_ZONES') {
            // SMC sub-indicators: managed by smart-money-concepts.js
            if (typeof toggleSMC === 'function') {
                var cacheKeySmc = klineChartSymbol + '_' + klineChartTimeframe;
                var cachedSmc = klineDataCache[cacheKeySmc];
                if (cachedSmc) {
                    var _smcKd = convertToKlineData(cachedSmc.data);
                    var _smcTrunc = _smcKd._realLength ? _smcKd.slice(0, _smcKd._realLength) : _smcKd;
                    try { toggleSMC(klineChart, _smcTrunc); } catch(e) { console.error('[SMC] toggle error:', e); }
                }
            }
        } else if ((IndicatorRegistry.getIndicator(indicator) || {}).type === 'quant_background') {
            // Market Regime ribbon: apply the toggle immediately (other macro/quant
            // indicators still wait for the next data application). Off = remove the
            // ribbon; on = re-apply from cache, or fetch with the quant param when
            // the cached payload lacks the series.
            if (!klineActiveIndicators[indicator]) {
                if (typeof _removeRegimeRibbon === 'function') _removeRegimeRibbon();
            } else {
                var _mqMeta = IndicatorRegistry.getIndicator(indicator);
                var cacheKeyMQ = klineChartSymbol + '_' + klineChartTimeframe;
                var cachedMQ = klineDataCache[cacheKeyMQ];
                var _hasSeries = cachedMQ && cachedMQ.data && cachedMQ.data.quant && cachedMQ.data.quant[_mqMeta.quantSeries];
                if (_hasSeries) {
                    applyKlineIndicators(cachedMQ.data, convertToKlineData(cachedMQ.data), true);
                    // Outside a full chart render the fresh indicator layer doesn't
                    // self-invalidate — nudge a repaint (mirror of _removeRegimeRibbon)
                    try { klineChart.resize(); } catch(e) {}
                } else {
                    var ltfSetMQ = ['1m','3m','5m','15m','30m','1h','4h'];
                    var mMQ = ltfSetMQ.indexOf(klineChartTimeframe) >= 0 ? 'ltf' : 'htf';
                    var mbMQ = (mMQ === 'ltf') ? 2500 : 1500;
                    fetch('/api/chart/' + encodeURIComponent(klineChartSymbol) + '?days=' + mbMQ + '&mode=' + mMQ + '&timeframe=' + encodeURIComponent(klineChartTimeframe) + _quantParam(), {
                        credentials: 'same-origin'
                    }).then(function(r) { return r.json(); }).then(function(full) {
                        if (full && full.ohlcv) {
                            klineCacheSave(cacheKeyMQ, full);
                            applyKlineIndicators(full, convertToKlineData(full), true);
                            try { klineChart.resize(); } catch(e) {}
                        }
                    // SILENCE-OK: regime-ribbon refetch — a miss just means the
                    // ribbon doesn't (re)appear; re-toggling the indicator retries it.
                    }).catch(function() {});
                }
            }
        } else if (['quant_overlay', 'quant_pane'].indexOf((IndicatorRegistry.getIndicator(indicator) || {}).type) !== -1) {
            // Macro/Quant overlay & pane indicators (M2, Net Liquidity, DXY,
            // Fear & Greed, Funding, etc. — the other 13 MACRO_* indicators
            // besides the MACRO_REGIME ribbon above). Same immediate-apply
            // pattern: off = remove this series' own pane/overlay; on =
            // re-apply from cache, or fetch with the quant param when the
            // cached payload lacks the series — reusing the exact
            // applyKlineIndicators()/_quantParam() reload path REGIME uses.
            var _mqiMeta = IndicatorRegistry.getIndicator(indicator);
            if (!klineActiveIndicators[indicator]) {
                // Off: applyKlineIndicators only (re)draws ACTIVE series, so
                // the stale pane/overlay from before the toggle must be torn
                // down explicitly here.
                if (_mqiMeta.type === 'quant_pane') {
                    try { klineChart.removeIndicator('quant_pane_' + indicator); } catch(e) {}
                } else {
                    try { klineChart.removeOverlay({ groupId: 'quant_' + indicator }); } catch(e) {}
                }
                try { klineChart.resize(); } catch(e) {}
            } else {
                var cacheKeyMQ2 = klineChartSymbol + '_' + klineChartTimeframe;
                var cachedMQ2 = klineDataCache[cacheKeyMQ2];
                var _hasSeries2 = cachedMQ2 && cachedMQ2.data && cachedMQ2.data.quant && cachedMQ2.data.quant[_mqiMeta.quantSeries];
                if (_hasSeries2) {
                    applyKlineIndicators(cachedMQ2.data, convertToKlineData(cachedMQ2.data), true);
                    try { klineChart.resize(); } catch(e) {}
                } else {
                    var ltfSetMQ2 = ['1m','3m','5m','15m','30m','1h','4h'];
                    var mMQ2 = ltfSetMQ2.indexOf(klineChartTimeframe) >= 0 ? 'ltf' : 'htf';
                    var mbMQ2 = (mMQ2 === 'ltf') ? 2500 : 1500;
                    fetch('/api/chart/' + encodeURIComponent(klineChartSymbol) + '?days=' + mbMQ2 + '&mode=' + mMQ2 + '&timeframe=' + encodeURIComponent(klineChartTimeframe) + _quantParam(), {
                        credentials: 'same-origin'
                    }).then(function(r) { return r.json(); }).then(function(full) {
                        if (full && full.ohlcv) {
                            klineCacheSave(cacheKeyMQ2, full);
                            applyKlineIndicators(full, convertToKlineData(full), true);
                            try { klineChart.resize(); } catch(e) {}
                        }
                    // SILENCE-OK: macro/quant overlay-pane refetch — same shape
                    // as the regime-ribbon fetch above; retry is a re-toggle.
                    }).catch(function() {});
                }
            }
        } else if (indicator.indexOf('CUSTOM_') === 0 && typeof CustomIndicators !== 'undefined') {
            // Custom formula indicator: add or remove pane
            var _cfId = indicator.replace('CUSTOM_', '');
            if (klineActiveIndicators[indicator]) {
                // Re-draw: get chart data, evaluate, create indicator
                var _cfData = klineChart.getDataList();
                if (_cfData && _cfData.length) {
                    CustomIndicators.drawAll(klineChart, _cfData);
                }
            } else {
                // Remove pane or overlay
                try { klineChart.removeIndicator('formula_pane_' + _cfId, 'CVX_F_' + _cfId); } catch(e) {}
                try { klineChart.removeIndicator('candle_pane', 'CVX_F_' + _cfId); } catch(e) {}
            }
        } else {
            // Indicator pack: toggle single indicator (no full redraw)
            var configMap = IndicatorRegistry.getPackConfigMap();
            var cfgKey = configMap[indicator];
            if (cfgKey && typeof indicatorPackConfig !== 'undefined') {
                // Update the config for this indicator
                if (indicatorPackConfig[cfgKey]) {
                    indicatorPackConfig[cfgKey].enabled = !!klineActiveIndicators[indicator];
                }
                // Remove this indicator's overlays by its groupId
                var groupIds = window.INDICATOR_GROUP_IDS || {};
                var gid = groupIds[cfgKey];
                if (gid) {
                    try { klineChart.removeOverlay({ groupId: gid }); } catch(e) {}
                }
                // Consensio HTML table lives outside KLineChart — remove it explicitly
                if ((cfgKey === 'consensio' || cfgKey === 'consensioLite') && typeof removeConsensioTable === 'function') {
                    removeConsensioTable();
                }
                // If enabling, draw just this indicator
                if (klineActiveIndicators[indicator] && typeof drawSingleIndicator === 'function') {
                    var cacheKey2 = klineChartSymbol + '_' + klineChartTimeframe;
                    var cached2 = klineDataCache[cacheKey2];
                    if (cached2) {
                        var kd = convertToKlineData(cached2.data);
                        try { drawSingleIndicator(klineChart, kd, cached2.data, cfgKey); } catch(e) { console.error('[indicators] toggle error for ' + cfgKey + ':', e); }
                    }
                }
                _enforceVolumePaneOrder();
            }
        }
        // Refresh fullscreen toggle buttons after pane changes
        setTimeout(function() { if (typeof updatePaneFullscreenButtons === 'function') updatePaneFullscreenButtons(); }, 200);
    }
}

function toggleAllIndicators() {
    // Exit pane fullscreen if active (all panes are about to change)
    if (typeof _fullscreenPaneId !== 'undefined' && _fullscreenPaneId) {
        exitPaneFullscreen();
    }
    if (!klineIndicatorsHidden) {
        // Hide all — save current state, set all to false
        klineHiddenIndicatorState = JSON.parse(JSON.stringify(klineActiveIndicators));
        try { localStorage.setItem('klineHiddenIndicatorState', JSON.stringify(klineHiddenIndicatorState)); } catch(e) {}
        for (var k in klineActiveIndicators) klineActiveIndicators[k] = false;
        klineIndicatorsHidden = true;
    } else {
        // Restore saved state
        if (klineHiddenIndicatorState) {
            for (var k in klineHiddenIndicatorState) klineActiveIndicators[k] = klineHiddenIndicatorState[k];
        }
        klineHiddenIndicatorState = null;
        klineIndicatorsHidden = false;
        try { localStorage.removeItem('klineHiddenIndicatorState'); } catch(e) {}
    }
    try { localStorage.setItem('klineActiveIndicators', JSON.stringify(klineActiveIndicators)); } catch(e) {}

    // Update all menu checkboxes
    var items = document.querySelectorAll('.indicator-menu-item[data-ind]');
    items.forEach(function(item) {
        var ind = item.getAttribute('data-ind');
        var check = item.querySelector('.ind-checkbox');
        item.classList.toggle('active', !!klineActiveIndicators[ind]);
        if (check) check.classList.toggle('active', !!klineActiveIndicators[ind]);
    });

    // Update master toggle button
    var masterBtn = document.getElementById('indicator-master-toggle');
    if (masterBtn) {
        masterBtn.innerHTML = klineIndicatorsHidden
            ? '<i data-lucide="eye-off"></i> Show All <span class="shortcut-hint">I</span>'
            : '<i data-lucide="eye"></i> Hide All <span class="shortcut-hint">I</span>';
        if (typeof lucide !== 'undefined') lucide.createIcons({ attrs: { 'stroke-width': 1.75 } });
    }

    // Group + favourite pills track the hide-all/restore state too
    _refreshGroupPillStates();
    document.querySelectorAll('.ind-fav-pill[data-ind]').forEach(function(pill) {
        pill.classList.toggle('active', !!klineActiveIndicators[pill.getAttribute('data-ind')]);
    });

    // Re-apply indicators without reloading chart (preserves scroll/zoom position)
    if (klineChart && klineChartSymbol) {
        klineChart.removeOverlay();
        _alertOverlayIds = [];
        // Consensio HTML elements live outside KLineChart — clean up explicitly
        if (typeof removeConsensioTable === 'function') removeConsensioTable();
        // Remove indicator panes that may need to be toggled
        try { klineChart.removeIndicator('volume_pane'); } catch(e) {}
        try { klineChart.removeIndicator('rsi_pane'); } catch(e) {}
        try { klineChart.removeIndicator('macd_pane'); } catch(e) {}
        try { klineChart.removeIndicator('candle_pane'); } catch(e) {}
        // VP_NATIVE was destroyed by removeIndicator('candle_pane') — reset flag
        _vpIndicatorCreated = false;
        // Re-apply all indicators from cache
        var cacheKey = klineChartSymbol + '_' + klineChartTimeframe;
        var cached = klineDataCache[cacheKey];
        if (cached) {
            applyKlineIndicators(cached.data, convertToKlineData(cached.data));
        }
        // Restore drawings, then alert overlays (order matters to avoid duplicates)
        if (!klineDrawingsHidden) {
            if (typeof loadKlineDrawings === 'function') loadKlineDrawings();
            if (typeof loadDrawingsFromServer === 'function') loadDrawingsFromServer();
        }
        // scenarios.js is not loaded on the standalone chart page
        if (typeof loadPlaysForChart === 'function') loadPlaysForChart(klineChartSymbol, klineChartTimeframe);
        renderAlertOverlays();
    }

    // VP follows indicator master toggle (vpStart re-creates VP_NATIVE if needed)
    if (klineActiveIndicators.VP) vpStart();
    else vpStop();
    // Naked POC follows indicator master toggle too
    if (typeof npocStart === 'function') { if (klineActiveIndicators.NPOC) npocStart(); else npocStop(); }
    // FRVP ranges are independent of VP toggle — always reload
    if (typeof frvpLoad === 'function') frvpLoad();

    // Refresh fullscreen toggle buttons
    setTimeout(function() { if (typeof updatePaneFullscreenButtons === 'function') updatePaneFullscreenButtons(); }, 300);
}

// Normalize columnar RSI {t:[],v:[],ma:[]} to array-of-objects [{time,value,ma}]
function _normalizeRsi(rsi) {
    if (!rsi) return rsi;
    if (rsi.t) {
        var arr = new Array(rsi.t.length);
        for (var i = 0; i < rsi.t.length; i++) {
            var pt = { time: rsi.t[i], value: rsi.v[i] };
            if (rsi.ma && rsi.ma[i] != null) pt.ma = rsi.ma[i];
            arr[i] = pt;
        }
        return arr;
    }
    return rsi;
}

// Regime ribbon — thin pixel-pinned strip at the TOP of the candle pane
// (sessions-style custom indicator), replacing the old full-height background
// tint. Registered on demand: registration must live here (not the lazy-loaded
// indicator pack) because MACRO_REGIME can be active without any pack indicator.
var _regimeBarRegistered = false;
function _ensureRegimeBarIndicator() {
    if (_regimeBarRegistered || typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'QUANT_REGIME_BAR',
            shortName: '',
            figures: [],
            calc: function(dataList) {
                var lookup = window._regimeBarLookup || {};
                return dataList.map(function(d) {
                    var v = lookup[Math.floor(d.timestamp / 1000)];
                    return v === undefined ? null : v;
                });
            },
            draw: function(params) {
                var ctx = params.ctx;
                var bounding = params.bounding;
                var barSpace = params.barSpace;
                var visibleRange = params.visibleRange;
                var xAxis = params.xAxis;
                var result = params.indicator.result;
                if (!visibleRange || !xAxis || !barSpace) return;
                // Pane may be unsized on the very first draw — NaN top would smear
                if (!bounding || !bounding.height) return;
                var RIBBON_H = 10;
                // BOTTOM-anchored: the chart toolbar is position:absolute OVER the
                // canvas top (nav row covers y0-28; +30 with the Draw row open), so
                // no top offset is ever safely visible. Sit above the sessions strip:
                // sessions bar top = h-11 (rectHeight 5 + bottomMargin 6), its labels
                // occupy h-24..h-14 (10px text, baseline 'bottom' at barTop-3) →
                // ribbon bottom = h-26 (2px gap), top = h-36. Without sessions, take
                // the sessions slot itself: h-16.
                var _sessionsOn = window.indicatorPackConfig &&
                                  window.indicatorPackConfig.sessions &&
                                  window.indicatorPackConfig.sessions.enabled;
                var RIBBON_TOP = _sessionsOn ? (bounding.height - 26 - RIBBON_H)
                                             : (bounding.height - 6 - RIBBON_H);
                var barW = Math.max(barSpace.bar, 1);
                ctx.save();
                // Pass 1: per-bar strip + collect visible regime blocks for labels
                var blocks = []; // { state, xStart, xEnd }
                var prevState = null;
                for (var i = visibleRange.from; i < visibleRange.to; i++) {
                    var st = result[i];
                    if (st === null || st === undefined) { prevState = null; continue; }
                    ctx.fillStyle = st >= 0.5 ? 'rgba(38, 166, 91, 0.9)' : 'rgba(234, 57, 67, 0.9)';
                    var x = xAxis.convertToPixel(i);
                    var xLeft = Math.round(x - barW / 2);
                    ctx.fillRect(xLeft, RIBBON_TOP, Math.ceil(barW), RIBBON_H);
                    if (st !== prevState) {
                        blocks.push({ state: st, xStart: xLeft, xEnd: xLeft + Math.ceil(barW) });
                    } else if (blocks.length) {
                        blocks[blocks.length - 1].xEnd = xLeft + Math.ceil(barW);
                    }
                    prevState = st;
                }
                // Pass 2: regime name at the start of each visible block (sessions-style),
                // inside the strip; skipped when the visible slice is too narrow for it.
                // Only canvas calls the sessions draw also uses — notably NO
                // measureText (an unsupported call on the layer ctx would silently
                // kill the labels); width gate uses a fixed 8px-mono estimate.
                ctx.font = '8px JetBrains Mono, monospace';
                ctx.textBaseline = 'middle';
                ctx.textAlign = 'left';
                ctx.globalAlpha = 1;
                for (var b = 0; b < blocks.length; b++) {
                    var blk = blocks[b];
                    var label = blk.state >= 0.5 ? 'MKT RISK-ON' : 'MKT RISK-OFF';
                    if (blk.xEnd - blk.xStart < label.length * 4.8 + 10) continue;
                    ctx.fillStyle = 'rgba(13, 17, 23, 0.95)'; // dark text on the colored strip
                    ctx.fillText(label, blk.xStart + 5, RIBBON_TOP + RIBBON_H / 2 + 0.5);
                }
                ctx.restore();
            }
        });
        _regimeBarRegistered = true;
    } catch (e) {
        console.warn('[Regime] ribbon indicator register error:', e);
    }
}

// Quant sub-pane indicators (Fear & Greed, Funding, VIX, …) render through a
// klinecharts template named QUANT_PANE_<key>. The template must exist BEFORE
// createIndicator is called with that name — an unregistered name silently
// no-ops, which left every quant_pane macro indicator blank. The line reads
// window._quantPaneData[<key>].values (index-aligned with the server ohlcv
// rows; phantom runway bars have no value and draw nothing).
function _ensureQuantPaneIndicator(key, meta) {
    window._quantPaneRegistered = window._quantPaneRegistered || {};
    if (window._quantPaneRegistered[key] || typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'QUANT_PANE_' + key,
            shortName: (meta && meta.shortLabel) || key,
            figures: [{ key: 'qv', title: ((meta && meta.shortLabel) || key) + ': ', type: 'line' }],
            styles: { lines: [{ color: '#64B5F6', size: 1.5, style: 'solid' }] },
            calc: function(dataList) {
                var pd = (window._quantPaneData || {})[key];
                var vals = pd && pd.values;
                if (!vals) return dataList.map(function() { return {}; });
                return dataList.map(function(d, i) {
                    return (vals[i] == null) ? {} : { qv: vals[i] };
                });
            }
        });
        window._quantPaneRegistered[key] = true;
    } catch (e) {
        console.warn('[Quant] pane indicator register error:', e);
    }
}

function _removeRegimeRibbon() {
    try { klineChart.removeIndicator('candle_pane', 'QUANT_REGIME_BAR'); } catch (e) {}
    window._regimeBarLookup = null;
    window._regimeRibbonActive = false;
    // removeIndicator alone doesn't always invalidate the painted layer until the
    // next tick/pan — nudge a repaint so the strip clears instantly on toggle-off.
    try { klineChart.resize(); } catch (e) {}
}

// Custom current-price line. The native candle priceMark.last is
// disabled because it anchors to KLineChart's internal tail slot — which is a
// far-future PHANTOM padding bar, and the tick path overwrites that slot with
// open==close so the native mark collapses to its noChange (gray) colour and
// tracks the phantom, not today's real candle. We draw our own dashed line at
// the real (phantom-skipped) last bar's close instead, coloured by candle
// direction (close vs open — same rule as the Y-axis price badge). draw() reads
// the live dataList directly (no per-bar calc), so it reflects the current tick
// with zero recompute cost.
var _lastPriceIndicatorRegistered = false;
function _ensureLastPriceIndicator() {
    if (_lastPriceIndicatorRegistered || typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'CARRAC_LAST_PRICE',
            shortName: '',
            figures: [],
            calc: function(dataList) { return dataList.map(function() { return {}; }); },
            draw: function(params) {
                var ctx = params.ctx;
                var bounding = params.bounding;
                // v9 passes kLineDataList — bare params.dataList is undefined and made
                // this draw bail silently on every frame (line was never visible).
                var dataList = params.kLineDataList || params.dataList;
                var yAxis = params.yAxis;
                if (!ctx || !yAxis || !dataList || !dataList.length) return false;
                if (!bounding || !bounding.height) return false;
                // Last REAL bar — phantom bars pad the axis with future timestamps
                // and carry no live close, so skip them (same scan as chart-live-tick).
                var now = Date.now();
                var i = dataList.length - 1;
                while (i > 0 && (dataList[i].timestamp > now || dataList[i]._phantom)) i--;
                var real = dataList[i];
                if (!real) return false;
                var price = real.close; // already chart-space (log-transformed when log scale on)
                var openRef = real.open;
                // Once this bar's period has elapsed the tick path
                // stops writing to it (chartTickDecision case B), so real.close
                // freezes. Track the live traded price instead — same source and
                // same staleness rule the Y-axis tag uses, so line and tag can
                // never drift apart.
                // klineChartSymbol/Timeframe describe the ACTIVE pane only, so in
                // split view this must not speak for a companion pane drawing a
                // different symbol. Identity-check the data being drawn against
                // the active chart's list before trusting those globals.
                var _isActivePane = false;
                try { _isActivePane = !!(klineChart && klineChart.getDataList() === dataList); } catch (e) {}
                if (_isActivePane && typeof window.carracAxisPrice === 'function') {
                    var _ax = window.carracAxisPrice(real, klineChartTimeframe, klineChartSymbol);
                    if (_ax && _ax.stale && _ax.price != null && typeof window.carracToChartSpace === 'function') {
                        var _cs = window.carracToChartSpace(_ax.price);
                        if (_cs != null && isFinite(_cs)) price = _cs;
                    }
                }
                if (price == null || !isFinite(price)) return false;
                // Colour by the price actually drawn vs the bar's open — the SAME
                // rule the Y-axis price badge uses (countdown-timer.js isBullish),
                // so line and label always match. Monotonic, correct in log space.
                var color = (price >= openRef) ? '#26a69a' : '#ef5350';
                var y = yAxis.convertToPixel(price);
                // Reposition the DOM y-axis tag from this same
                // repaint with this same y — the 1s countdown timer alone let
                // the tag lag or freeze visibly apart from the line.
                if (_isActivePane && typeof window.carracPositionAxisTag === 'function') {
                    window.carracPositionAxisTag(y);
                }
                if (y == null || !isFinite(y) || y < -1 || y > bounding.height + 1) return false;
                y = Math.round(y) + 0.5;
                ctx.save();
                ctx.strokeStyle = color;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(bounding.width, y);
                ctx.stroke();
                ctx.restore();
                return false;
            }
        });
        _lastPriceIndicatorRegistered = true;
    } catch (e) {
        if (typeof console !== 'undefined') console.warn('[CARRAC_LAST_PRICE] registerIndicator failed:', e);
    }
}

// Apply indicators (KLineChart built-in + indicator pack) to existing chart instance
// skipBuiltIn: true on fast TF swap — built-in indicators auto-recalculate via applyNewData()
function applyKlineIndicators(data, klineData, skipBuiltIn, loadGen) {
    if (!klineChart) return;
    // Always-on custom current-price line: native priceMark.last is
    // disabled (it anchors to the phantom tail); draw our own at the real bar.
    // Idempotent remove+create mirrors the QUANT_REGIME_BAR pattern so it survives
    // chart reuse / TF swaps.
    _ensureLastPriceIndicator();
    try { klineChart.removeIndicator('candle_pane', 'CARRAC_LAST_PRICE'); } catch (e) {}
    try { klineChart.createIndicator({ name: 'CARRAC_LAST_PRICE' }, true, { id: 'candle_pane' }); } catch (e) {}
    // Keep server RSI data in sync when new data arrives
    if (data && data.rsi) window._serverRsiData = _normalizeRsi(data.rsi);
    // Debug: log active indicators to help diagnose split-view rendering issues
    var activeList = [];
    for (var _k in klineActiveIndicators) { if (klineActiveIndicators[_k]) activeList.push(_k); }

    if (!skipBuiltIn) {
        // Smaller sub-panes in multi-chart mode to fit on screen
        var _isMulti = (typeof _mcLayout !== 'undefined' && _mcLayout !== 'single');
        var _volH = _isMulti ? 55 : 100;
        var _rsiH = _isMulti ? 55 : 90;
        var _minH = _isMulti ? 30 : 40;
        // Built-in indicators (only needed on first render, not TF swaps)
        // Built-in pane indicators (from registry)
        var _builtInPaneList = IndicatorRegistry.getByType('builtin_pane');
        for (var _bk in _builtInPaneList) {
            if (!klineActiveIndicators[_bk]) continue;
            var _bi = _builtInPaneList[_bk];
            // single-view height must come from the registry's own
            // paneHeight (same source the toggle re-apply path uses), not a
            // blanket _rsiH (90) — that hardcode shrank MACD (registry: 70) the
            // first time it was toggled off/on. Multi-chart mode keeps the
            // existing compact scaling (_volH === _rsiH === 55 there, so this
            // is a no-op change for that branch).
            var _bh = _isMulti ? (_bk === 'VOL' ? _volH : _rsiH)
                                : (_bi.paneHeight != null ? _bi.paneHeight : (_bk === 'VOL' ? _volH : _rsiH));
            var createArg = _bi.calcParams ? { name: _bk, calcParams: _bi.calcParams } : _bk;
            klineChart.createIndicator(createArg, false, { id: _bi.paneId, height: _bh, minHeight: _minH });
            if (_bk === 'VOL') {
                klineChart.setPaneOptions({ id: _bi.paneId, gap: { top: 0.02, bottom: 0 }, axisOptions: { name: 'volumeAxis' } });
            } else if (_bk === 'MACD') {
                klineChart.setPaneOptions({ id: _bi.paneId, gap: { top: 0.05, bottom: 0.05 } });
            } else {
                klineChart.setPaneOptions({ id: _bi.paneId, gap: { top: 0.05, bottom: 0.05 } });
            }
        }
        // Built-in overlay indicators (from registry). Passing the bare
        // indicator name would drop the registry's calcParams — any user-edited
        // MA/EMA/BOLL params (saved into
        // IndicatorRegistry.INDICATORS.<KEY>.calcParams by indicator-settings.js)
        // would be discarded on every symbol switch / full reload, reverting
        // to the calcParams frozen at registerIndicator time. Mirror the toggle
        // re-apply path: build a {name, calcParams} payload when the
        // registry entry has calcParams, else keep the bare-name behavior.
        var _builtInOverlayList = IndicatorRegistry.getBuiltInOverlays();
        for (var _ok in _builtInOverlayList) {
            if (klineActiveIndicators[_ok]) {
                var _ovInd = IndicatorRegistry.getIndicator(_ok);
                var _ovCreateArg = (_ovInd && _ovInd.calcParams) ? { name: _ok, calcParams: _ovInd.calcParams.slice() } : _ok;
                klineChart.createIndicator(_ovCreateArg, true, { id: 'candle_pane' });
            }
        }
    }

    // Helper: draw a subsampled line-series in its own pane (max ~250 segments)
    // NOTE: createOverlay cannot create new panes in KLineChart v9.8.12.
    // We use createIndicator('DERIV_LINE') to create the pane and provide Y-axis scaling,
    // then add colored segment overlays on top for the actual visual rendering.
    var _derivIndMap = { 'cvd_spot': 'DERIV_CVD_SPOT', 'oi_line': 'DERIV_OI', 'cvd_usdt': 'DERIV_CVD_USDT', 'cvd_coin': 'DERIV_CVD_COIN' };
    function _drawSubsampledPane(seriesData, valueKey, deltaKey, paneId, groupId, idPrefix, label, labelColor, colorFn) {
        if (!seriesData || seriesData.length < 2) return;
        try {
            // Store data globally so the derivative indicator can read it for Y-axis scaling
            window._derivPaneData = window._derivPaneData || {};
            window._derivPaneData[idPrefix] = { data: seriesData, valueKey: valueKey };
            // Create pane via named derivative indicator (shows label + values in pane header)
            var indName = _derivIndMap[idPrefix] || 'DERIV_CVD_SPOT';
            klineChart.createIndicator(
                { name: indName },
                false,
                { id: paneId, height: 70, minHeight: 40 }
            );
            klineChart.setPaneOptions({ id: paneId, gap: { top: 0.05, bottom: 0.05 } });
            // Subsample to ~500 segments max
            var step = Math.max(1, Math.floor(seriesData.length / 500));
            var indices = [];
            for (var i = 1; i < seriesData.length; i += step) indices.push(i);
            if (indices[indices.length - 1] !== seriesData.length - 1) indices.push(seriesData.length - 1);
            for (var j = 0; j < indices.length; j++) {
                var ci = indices[j];
                var pi = j > 0 ? indices[j - 1] : 0;
                klineChart.createOverlay({
                    name: 'segment', id: idPrefix + '_' + j, groupId: groupId, lock: true,
                    points: [
                        { timestamp: seriesData[pi].time * 1000, value: seriesData[pi][valueKey] },
                        { timestamp: seriesData[ci].time * 1000, value: seriesData[ci][valueKey] }
                    ],
                    styles: { line: { color: colorFn(seriesData[pi], seriesData[ci]), size: 1.5 } }
                }, paneId);
            }
        } catch (e) { console.warn(label + ' render error:', e); }
    }

    // CVD Spot pane — indicator draws the colored line via custom draw callback;
    // we only need to create the pane and store data (no segment overlays)
    if (klineActiveIndicators.CVD_SPOT && data && data.cvd_spot && data.cvd_spot.length > 1) {
        try {
            window._derivPaneData = window._derivPaneData || {};
            window._derivPaneData['cvd_spot'] = { data: data.cvd_spot, valueKey: 'cvd' };
            klineChart.createIndicator(
                { name: 'DERIV_CVD_SPOT' },
                false,
                { id: 'cvd_spot_pane', height: 70, minHeight: 40 }
            );
            klineChart.setPaneOptions({ id: 'cvd_spot_pane', gap: { top: 0.05, bottom: 0.05 } });
        } catch (e) { console.warn('CVD Spot render error:', e); }
    }

    // Open Interest historical pane
    if (klineActiveIndicators.OI && data && data.oi_hist && data.oi_hist.length > 1) {
        _drawSubsampledPane(data.oi_hist, 'oi_usd', null, 'oi_pane', 'oiIndicator', 'oi_line', 'Open Interest (USD)', '#FF9800',
            function(prev, curr) { return curr.oi_usd >= prev.oi_usd ? '#FF9800' : '#ab47bc'; });
    }

    // CVD Futures USDT-M pane
    if (klineActiveIndicators.CVD_USDT && data && data.cvd_usdt && data.cvd_usdt.length > 1) {
        _drawSubsampledPane(data.cvd_usdt, 'cvd', 'delta', 'cvd_usdt_pane', 'cvdUsdtIndicator', 'cvd_usdt', 'CVD USDT-M', '#00BCD4',
            function(prev, curr) { return curr.delta >= 0 ? '#26a69a' : '#ef5350'; });
    }

    // CVD Futures COIN-M pane
    if (klineActiveIndicators.CVD_COIN && data && data.cvd_coin && data.cvd_coin.length > 1) {
        _drawSubsampledPane(data.cvd_coin, 'cvd', 'delta', 'cvd_coin_pane', 'cvdCoinIndicator', 'cvd_coin', 'CVD COIN-M', '#E040FB',
            function(prev, curr) { return curr.delta >= 0 ? '#26a69a' : '#ef5350'; });
    }

    // Combined Derivatives pane (OI + CVD USDT + CVD COIN normalized to 0-100)
    if (klineActiveIndicators.DERIV_COMBINED && data) {
        var _dcData = {};
        if (data.oi_hist && data.oi_hist.length > 1) _dcData.oi = { data: data.oi_hist, valueKey: 'oi_usd' };
        if (data.cvd_usdt && data.cvd_usdt.length > 1) _dcData.cvd_usdt = { data: data.cvd_usdt, valueKey: 'cvd' };
        if (data.cvd_coin && data.cvd_coin.length > 1) _dcData.cvd_coin = { data: data.cvd_coin, valueKey: 'cvd' };
        if (Object.keys(_dcData).length > 0) {
            window._derivCombinedData = _dcData;
            try {
                klineChart.createIndicator(
                    { name: 'DERIV_COMBINED' },
                    false,
                    { id: 'deriv_combined_pane', height: 90, minHeight: 60 }
                );
            } catch (e) { console.warn('Combined derivatives render error:', e); }
        }
    }

    // SMC sub-indicators — exclude phantom future bars
    if (typeof drawSMCOverlays === 'function' &&
        (klineActiveIndicators.MKT_STRUCTURE || klineActiveIndicators.ORDER_BLOCKS ||
         klineActiveIndicators.EQ_HL || klineActiveIndicators.VALUE_ZONES)) {
        var _smcData = klineData._realLength ? klineData.slice(0, klineData._realLength) : klineData;
        try { drawSMCOverlays(klineChart, _smcData); } catch (e) { console.error('[SMC] drawSMCOverlays error:', e); }
    }

    // Indicator Pack
    if (typeof indicatorPackConfig !== 'undefined' && typeof drawIndicatorPack === 'function') {
        var _packConfigMap = IndicatorRegistry.getPackConfigMap();
        var anyPackActive = false;
        for (var _pk in _packConfigMap) {
            if (indicatorPackConfig[_packConfigMap[_pk]]) {
                indicatorPackConfig[_packConfigMap[_pk]].enabled = !!klineActiveIndicators[_pk];
                if (klineActiveIndicators[_pk]) anyPackActive = true;
            }
        }

        if (anyPackActive) {
            // Draw synchronously — already deferred by one rAF from fastSwap/render caller.
            // A second rAF created a fragile chain that broke when anything called
            // loadKlineChart between frames (countdown timer, WS update, etc.).
            try {
                drawIndicatorPack(klineChart, klineData, data);
            } catch (e) {
                console.error('[indicators] Pack error:', e);
            }
        }
    }

    // 4b. Enforce volume pane order
    _enforceVolumePaneOrder();

    // 5. Macro & Quant indicators (from server quant bridge data)
    var _regimeRibbonDrawn = false;
    if (data && data.quant) {
        var _quantReg = IndicatorRegistry.getByCategory ? IndicatorRegistry.getByCategory('macro') : {};
        for (var _qk in _quantReg) {
            if (!klineActiveIndicators[_qk]) continue;
            var _qi = _quantReg[_qk];
            var _qs = _qi.quantSeries;
            if (!_qs || !data.quant[_qs]) continue;
            var _qvals = data.quant[_qs].values;
            if (!_qvals || !_qvals.length) continue;

            try {
                if (_qi.type === 'quant_pane') {
                    // Sub-pane indicator (Fear & Greed, Funding, VIX, etc.).
                    // The registered template line is the sole renderer — it also
                    // drives the pane's own y-axis range (overlay segments never
                    // do, so they would draw nothing visible).
                    var _qPaneId = 'quant_pane_' + _qk;
                    window._quantPaneData = window._quantPaneData || {};
                    window._quantPaneData[_qk] = { values: _qvals, label: _qi.label };
                    _ensureQuantPaneIndicator(_qk, _qi);
                    // Recreate so a re-apply (symbol/TF switch) picks up fresh values
                    try { klineChart.removeIndicator(_qPaneId, 'QUANT_PANE_' + _qk); } catch (e) {}
                    klineChart.createIndicator(
                        { name: 'QUANT_PANE_' + _qk },
                        false,
                        { id: _qPaneId, height: _qi.paneHeight || 60, minHeight: 30 }
                    );
                    klineChart.setPaneOptions({ id: _qPaneId, gap: { top: 0.05, bottom: 0.05 } });
                } else if (_qi.type === 'quant_overlay') {
                    // Price-pane overlay (M2, Net Liquidity, DXY, Stablecoins).
                    // The server sends RAW series values (axis:'right'), but there
                    // is no secondary y-axis — raw M2 (~23k) or DXY (~100) plotted
                    // on the price axis lands off-screen. Min-max normalize the
                    // series into the loaded candles' own price range so the line
                    // overlays the price action (shape/divergence comparison, the
                    // indicator's stated purpose). klineData is already
                    // log-transformed when log scale is on, so mapping into its
                    // range is correct in both axis modes. The latest RAW value is
                    // kept visible via a label at the line's right edge.
                    var _qOvPaneId = 'candle_pane';
                    var _step = Math.max(1, Math.floor(_qvals.length / 500));
                    var _colors = { 'M2': '#42A5F5', 'NET_LIQUIDITY': '#26C6DA', 'DXY': '#FFA726', 'STABLECOIN_USDT_SUPPLY': '#66BB6A' };
                    var _color = _colors[_qs] || '#AB47BC';
                    var _sMin = Infinity, _sMax = -Infinity, _pMin = Infinity, _pMax = -Infinity;
                    var _lastIdx = -1;
                    var _qn = Math.min(_qvals.length, klineData.length);
                    for (var _pi = 0; _pi < _qn; _pi++) {
                        var _qv = _qvals[_pi];
                        if (_qv != null) {
                            if (_qv < _sMin) _sMin = _qv;
                            if (_qv > _sMax) _sMax = _qv;
                            _lastIdx = _pi;
                        }
                    }
                    // Price bounds from the VISIBLE window (not the full loaded
                    // history): the full range spans years, so a series at its
                    // own high would map above the default view and render
                    // off-screen (M2 mapped to ~120k on a 58-98k view). The
                    // overlay is static after draw — scrolling far back skews
                    // its scale, but the default/right-aligned view (the use
                    // case) always shows the line and its latest-value label.
                    var _visFrom = 0, _visTo = _qn;
                    try {
                        var _vr = klineChart.getVisibleRange();
                        if (_vr && _vr.to > _vr.from) {
                            _visFrom = Math.max(0, _vr.from);
                            _visTo = Math.min(_qn, _vr.to);
                        }
                    } catch (e) {}
                    for (var _vi = _visFrom; _vi < _visTo; _vi++) {
                        var _kd = klineData[_vi];
                        if (_kd && _kd.low != null && _kd.low < _pMin) _pMin = _kd.low;
                        if (_kd && _kd.high != null && _kd.high > _pMax) _pMax = _kd.high;
                    }
                    if (!isFinite(_pMin) || !isFinite(_pMax)) {
                        for (var _fi = 0; _fi < _qn; _fi++) {
                            var _kf = klineData[_fi];
                            if (_kf && _kf.low != null && _kf.low < _pMin) _pMin = _kf.low;
                            if (_kf && _kf.high != null && _kf.high > _pMax) _pMax = _kf.high;
                        }
                    }
                    if (_lastIdx < 0 || !isFinite(_pMin) || !isFinite(_pMax)) continue;
                    var _sSpan = (_sMax - _sMin) || 1;
                    // 4% pad keeps the line off the exact pane edges
                    var _pPad = (_pMax - _pMin) * 0.04;
                    var _pLo = _pMin + _pPad, _pHi = _pMax - _pPad;
                    var _qScale = function(v) { return _pLo + ((v - _sMin) / _sSpan) * (_pHi - _pLo); };
                    for (var _si = _step; _si < _qn; _si += _step) {
                        if (_qvals[_si] == null || _qvals[_si - _step] == null) continue;
                        klineChart.createOverlay({
                            name: 'segment', id: 'qo_' + _qk + '_' + _si, groupId: 'quant_' + _qk, lock: true,
                            points: [
                                { timestamp: klineData[_si - _step].timestamp, value: _qScale(_qvals[_si - _step]) },
                                { timestamp: klineData[_si].timestamp, value: _qScale(_qvals[_si]) }
                            ],
                            styles: { line: { color: _color, size: 1.5, style: 'dashed' } }
                        }, _qOvPaneId);
                    }
                    // Right-edge label with the latest RAW value (normalization
                    // hides absolute levels; this keeps them one glance away).
                    var _rawLast = _qvals[_lastIdx];
                    var _lblTxt = (_qi.shortLabel || _qk) + ' ' + (Math.abs(_rawLast) >= 1000
                        ? Math.round(_rawLast).toLocaleString('en-US')
                        : _rawLast.toFixed(2));
                    try {
                        klineChart.createOverlay({
                            name: 'simpleAnnotation', id: 'qo_lbl_' + _qk, groupId: 'quant_' + _qk, lock: true,
                            points: [{ timestamp: klineData[_lastIdx].timestamp, value: _qScale(_rawLast) }],
                            styles: { text: { color: '#ffffff', backgroundColor: _color, borderColor: _color, borderRadius: 3, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2, size: 10 } },
                            extendData: _lblTxt
                        }, _qOvPaneId);
                    } catch (e) {}
                } else if (_qi.type === 'quant_background') {
                    // Regime ribbon: thin strip pinned to the top of the candle pane
                    // (sessions-style pixel paint) instead of the old full-height
                    // background tint — same data, non-invasive on a busy chart.
                    _ensureRegimeBarIndicator();
                    var _rlookup = {};
                    var _rn = Math.min(_qvals.length, klineData.length);
                    for (var _ri = 0; _ri < _rn; _ri++) {
                        if (_qvals[_ri] == null) continue;
                        _rlookup[Math.floor(klineData[_ri].timestamp / 1000)] = _qvals[_ri] >= 0.5 ? 1 : 0;
                    }
                    window._regimeBarLookup = _rlookup;
                    try { klineChart.removeIndicator('candle_pane', 'QUANT_REGIME_BAR'); } catch (e) {}
                    klineChart.createIndicator({ name: 'QUANT_REGIME_BAR' }, true, { id: 'candle_pane' });
                    window._regimeRibbonActive = true;
                    _regimeRibbonDrawn = true;
                }
            } catch (e) {
                console.warn('[indicators] Quant render error for ' + _qk + ':', e);
            }
        }
    }

    // 5b. Regime ribbon lifecycle: clear it when the indicator is off or the
    // payload carries no regime data (runs OUTSIDE the data.quant guard so a
    // toggle-off or a quant-less payload removes a stale ribbon).
    if (!_regimeRibbonDrawn && window._regimeRibbonActive) {
        _removeRegimeRibbon();
    }

    // 6. Custom formula indicators
    if (typeof CustomIndicators !== 'undefined' && typeof CustomIndicators.drawAll === 'function') {
        try {
            CustomIndicators.drawAll(klineChart, klineData);
        } catch (e) {
            console.error('[indicators] Custom formula error:', e);
        }
    }
}

// client-side Wilder RSI for the custom RSI indicator's calc().
// The registered calc renders server-computed Wilder RSI(14) via a timestamp
// lookup (fast, and byte-identical to the gold-standard default) but ignored
// calcParams entirely — so the registry's live Period setting was a no-op for
// any value other than 14. This is only used when the user picks a period
// != 14; period 14 keeps using the precomputed server series untouched.
// Wilder smoothing: seed = SMA of the first `period` gains/losses, then
// recursively smooth with weight (period-1)/period — the standard formula
// (same one the server-side precompute uses for period 14).
function _wilderRsiFromDataList(dataList, period) {
    var n = dataList.length;
    var out = new Array(n);
    if (!n || period < 1 || n <= period) return out;
    var gainSum = 0, lossSum = 0;
    for (var i = 1; i <= period; i++) {
        var change = dataList[i].close - dataList[i - 1].close;
        if (change > 0) gainSum += change; else lossSum -= change;
    }
    var avgGain = gainSum / period;
    var avgLoss = lossSum / period;
    out[period] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
    for (var j = period + 1; j < n; j++) {
        var ch = dataList[j].close - dataList[j - 1].close;
        var gain = ch > 0 ? ch : 0;
        var loss = ch < 0 ? -ch : 0;
        avgGain = (avgGain * (period - 1) + gain) / period;
        avgLoss = (avgLoss * (period - 1) + loss) / period;
        out[j] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
    }
    return out;
}
