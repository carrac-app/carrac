// Backtest UI — drawer tab for strategy backtesting
// Depends on: backtester.js, formula-parser.js, custom-indicators.js (extractOHLCV)

(function() {
    'use strict';

    var _previewTimer = null;
    var _lastResult = null;
    var _lastOhlcv = null;
    var _lastConfig = null;
    var _markerOverlayIds = [];

    // Field readers — module scope so getConfig() and runBacktest
    // read the panel through exactly the same accessors. Keeping them out of
    // runBacktest lets getConfig reach the Realism fields, so a saved setup is complete.
    function _el(id) { return document.getElementById(id); }
    function _num(id, fbk) { var el = _el(id); var v = el ? parseFloat(el.value) : NaN; return isNaN(v) ? fbk : v; }
    function _val(id, fbk) { var el = _el(id); return el ? el.value : fbk; }

    function _csrf() {
        var el = document.querySelector('meta[name="csrf-token"]');
        return el ? el.getAttribute('content') : '';
    }

    // ── Pattern presets ──────────────────────────────────────
    // Inserted as EDITABLE formula text, never applied as a hidden strategy
    // object — the user sees and can change exactly what runs, and every
    // statistic comes from their own Run on their own symbol and window.
    //
    // Every function used here must exist in BOTH engines; enforced by
    // tests/test_backtest_presets.py. Note there is deliberately no "order
    // block" preset: Carrac draws structure-anchored OB zones with mitigation
    // (smart-money-concepts.js), and no candle-sequence formula here is that.
    var BT_PATTERN_PRESETS = [
        { "label": "Gap up (FVG)",        "formula": "GAP_UP()",                          "target": "entry" },
        { "label": "Gap + trend filter",  "formula": "GAP_UP() AND CLOSE > EMA(21)",      "target": "entry" },
        { "label": "Sweep + reclaim",     "formula": "SWEPT_LOW(20) AND CLOSE > OPEN",    "target": "entry" },
        { "label": "Displacement candle", "formula": "DISPLACEMENT(2)",                   "target": "entry" },
        { "label": "Reversal close",      "formula": "REVERSAL_CLOSE_UP(2)",              "target": "entry" },
        { "label": "Sweep of highs",      "formula": "SWEPT_HIGH(20) AND CLOSE < OPEN",   "target": "exit" }
    ];

    function buildPresetPicker(targetId) {
        var opts = '<option value="">Patterns…</option>';
        for (var i = 0; i < BT_PATTERN_PRESETS.length; i++) {
            opts += '<option value="' + i + '">' + BT_PATTERN_PRESETS[i].label + '</option>';
        }
        return '<select class="bt-select bt-preset-picker" data-target="' + targetId +
               '" title="Insert a starting formula — fully editable, and the results you get are for your own asset and dates">' +
               opts + '</select>';
    }

    // ── Render Panel ──────────────────────────────────────────────────────

    function renderBacktestPanel() {
        var section = document.getElementById('drawer-section-backtest');
        if (!section) return;
        if (section.querySelector('.bt-panel')) return; // already rendered

        // Gate on _isPremium (member+), NOT _hasPremiumFeatures
        // (premium/admin/tester only). TIER_LIMITS['member']['backtester'] is
        // True — the backtester is the entry paid tier's differentiator, so
        // _hasPremiumFeatures paywalled a feature members already pay for.
        if (!window._isPremium) {
            section.innerHTML = '<div class="bt-panel" style="padding:24px;text-align:center;color:#888;">' +
                '<p style="font-size:15px;margin-bottom:12px;">Strategy backtesting is available with a subscription.</p>' +
                '<p style="font-size:12px;">Test entry and exit formulas against historical data, with macro regime filtering, funding cost simulation, and cross-asset benchmarks.</p>' +
                (typeof showUpgradeModal === 'function' ?
                    '<button class="bt-run-btn bt-upgrade-btn" style="margin-top:12px;">Upgrade</button>' : '') +
                '</div>';
            var upgradeBtn = section.querySelector('.bt-upgrade-btn');
            if (upgradeBtn) upgradeBtn.addEventListener('click', function() {
                showUpgradeModal('Unlock the backtester with a subscription.');
            });
            return;
        }

        section.innerHTML =
        '<div class="bt-panel">' +
            
            '<div class="bt-layout">' +
                // Left: editor
                '<div class="bt-editor">' +
                    '<div class="bt-field">' +
                        '<label class="bt-label">Entry <span class="bt-hint">when to open</span>' +
                            buildPresetPicker('bt-entry') + '</label>' +
                        '<textarea class="bt-formula" id="bt-entry" rows="2" spellcheck="false" placeholder="CROSSOVER(EMA(9), EMA(21))"></textarea>' +
                        '<div class="bt-validation" id="bt-entry-status"></div>' +
                    '</div>' +
                    '<div class="bt-field">' +
                        '<label class="bt-label">Exit <span class="bt-hint">when to close</span>' +
                            buildPresetPicker('bt-exit') + '</label>' +
                        '<textarea class="bt-formula" id="bt-exit" rows="2" spellcheck="false" placeholder="CROSSUNDER(EMA(9), EMA(21))"></textarea>' +
                        '<div class="bt-validation" id="bt-exit-status"></div>' +
                    '</div>' +
                    '<div class="bt-params">' +
                        '<div class="bt-param">' +
                            '<label>Direction</label>' +
                            '<select id="bt-direction" class="bt-select">' +
                                '<option value="long">Long</option>' +
                                '<option value="short">Short</option>' +
                                '<option value="both">Both (reverse)</option>' +
                            '</select>' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>Capital</label>' +
                            '<input type="number" id="bt-capital" class="bt-input" value="10000" min="1">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>Size %</label>' +
                            '<input type="number" id="bt-size" class="bt-input" value="100" min="1" max="100">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>Commission %</label>' +
                            '<input type="number" id="bt-commission" class="bt-input" value="0.1" min="0" step="0.01">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>Regime</label>' +
                            '<select id="bt-regime" class="bt-select">' +
                                '<option value="all">All</option>' +
                                '<option value="favorable">Favorable</option>' +
                                '<option value="unfavorable">Unfavorable</option>' +
                            '</select>' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>Instrument</label>' +
                            '<select id="bt-instrument" class="bt-select">' +
                                '<option value="spot">Spot</option>' +
                                '<option value="perp">Perpetual</option>' +
                            '</select>' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>Start Date</label>' +
                            '<input type="date" id="bt-start-date" class="bt-input">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label>End Date</label>' +
                            '<input type="date" id="bt-end-date" class="bt-input">' +
                        '</div>' +
                    '</div>' +
                    '<div class="bt-advanced-toggle" id="bt-adv-toggle" style="font-size:11px;color:#8b949e;cursor:pointer;user-select:none;margin:6px 0 4px;">' +
                        '<span id="bt-adv-chevron">\u25B6</span> Realism &amp; Risk Controls' +
                    '</div>' +
                    '<div class="bt-params bt-advanced" id="bt-advanced" style="display:none;">' +
                        '<div class="bt-param">' +
                            '<label title="Price slippage applied on entry + exit (bps = 0.01%)">Slippage (bps)</label>' +
                            '<input type="number" id="bt-slippage" class="bt-input" value="0" min="0" step="1">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label title="Stop hit check: close-only uses exit formula; intrabar checks bar high/low">Stop Type</label>' +
                            '<select id="bt-stop-type" class="bt-select">' +
                                '<option value="close">Close only</option>' +
                                '<option value="intrabar">Intrabar (high/low)</option>' +
                            '</select>' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label title="Stop loss percent from entry (0 = disabled)">Stop Loss %</label>' +
                            '<input type="number" id="bt-stop-loss" class="bt-input" value="0" min="0" max="50" step="0.1">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            '<label title="Take profit percent from entry (0 = disabled)">Take Profit %</label>' +
                            '<input type="number" id="bt-take-profit" class="bt-input" value="0" min="0" step="0.1">' +
                        '</div>' +
                        '<div class="bt-param">' +
                            // next_open MUST stay first (= browser-selected
                            // default). backtester.js defaults to next_open
                            // precisely to avoid look-ahead bias and states that 'close'
                            // "must be explicitly opted into by the caller" — but this
                            // select listed 'close' first and _val() below passed it
                            // explicitly, so every UI backtest silently ran look-ahead
                            // and diverged from the same test over MCP.
                            '<label title="Next open: fill on the next bar\'s open — you cannot act on a close before it happens. Signal-bar close is look-ahead bias; use it for signal study only.">Entry Fill</label>' +
                            '<select id="bt-entry-mode" class="bt-select">' +
                                '<option value="next_open">Next-bar open (realistic)</option>' +
                                '<option value="close">Signal-bar close (look-ahead)</option>' +
                            '</select>' +
                        '</div>' +
                    '</div>' +
                    '<div class="bt-actions">' +
                        '<button class="bt-run-btn" id="bt-run">Run Backtest</button>' +
                        '<button class="bt-secondary-btn" id="bt-clear-markers" title="Remove chart markers">Clear Markers</button>' +
                        '<div class="bt-spacer"></div>' +
                        '<button class="bt-icon-btn" id="bt-load" title="Load strategy">Load</button>' +
                        '<button class="bt-icon-btn" id="bt-history" title="Saved runs — including ones your AI assistant saved">History</button>' +
                        '<button class="bt-icon-btn" id="bt-save-run" disabled title="Keep these exact numbers permanently — a saved result is what you just measured, not the formula, and reopening it will not run it again. Find it later under History.">Save Result</button>' +
                        '<button class="bt-icon-btn" id="bt-csv" title="Export CSV">CSV</button>' +
                        '<a href="/formulas" target="_blank" class="bt-help-link">?</a>' +
                    '</div>' +
                    '<div class="bt-save-row" id="bt-save-row">' +
                        '<input type="text" id="bt-save-name" class="bt-input bt-save-input" placeholder="Strategy name..." maxlength="50">' +
                        '<button class="bt-icon-btn" id="bt-save-confirm">Save</button>' +
                    '</div>' +
                '</div>' +
                // Right: results
                '<div class="bt-results" id="bt-results">' +
                    '<div class="bt-placeholder">Configure entry &amp; exit formulas, then run.</div>' +
                '</div>' +
            '</div>' +
        '</div>';

        // Wire events
        document.getElementById('bt-run').addEventListener('click', runBacktest);
        document.getElementById('bt-entry').addEventListener('input', debounceValidate);
        document.getElementById('bt-exit').addEventListener('input', debounceValidate);

        // Pattern presets — insert editable text, then reuse the existing
        // validation path so the user gets immediate feedback.
        var pickers = document.querySelectorAll('.bt-preset-picker');
        for (var pi = 0; pi < pickers.length; pi++) {
            pickers[pi].addEventListener('change', function (e) {
                var idx = e.target.value;
                if (idx === '') return;
                var field = document.getElementById(e.target.getAttribute('data-target'));
                if (!field) { e.target.value = ''; return; }
                field.value = BT_PATTERN_PRESETS[idx].formula;
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.focus();
                e.target.value = '';   // reset so the same preset can be re-picked
            });
        }
        document.getElementById('bt-clear-markers').addEventListener('click', clearMarkers);
        if (!window._isPremium) {
            var saveRow = document.getElementById('bt-save-row');
            if (saveRow) { saveRow.style.opacity = '0.4'; saveRow.style.pointerEvents = 'none'; saveRow.title = 'Save/load strategies requires a subscription'; }
            var loadBtn = document.getElementById('bt-load');
            if (loadBtn) { loadBtn.style.opacity = '0.4'; loadBtn.onclick = function() { if (typeof showUpgradeModal === 'function') showUpgradeModal('Save and load backtester strategies with a subscription.'); }; }
            var histBtn = document.getElementById('bt-history');
            if (histBtn) { histBtn.style.opacity = '0.4'; histBtn.onclick = function() { if (typeof showUpgradeModal === 'function') showUpgradeModal('Saved backtest runs need a subscription.'); }; }
        } else {
            document.getElementById('bt-save-confirm').addEventListener('click', saveStrategy);
            document.getElementById('bt-save-name').addEventListener('keydown', function(e) {
                if (e.key === 'Enter') saveStrategy();
            });
            document.getElementById('bt-load').addEventListener('click', loadStrategies);
            document.getElementById('bt-history').addEventListener('click', loadRunHistory);
            document.getElementById('bt-save-run').addEventListener('click', saveBacktestResult);
        }
        document.getElementById('bt-csv').addEventListener('click', exportCSV);
        var advToggle = document.getElementById('bt-adv-toggle');
        if (advToggle) {
            advToggle.addEventListener('click', function() {
                var adv = document.getElementById('bt-advanced');
                var chev = document.getElementById('bt-adv-chevron');
                if (!adv) return;
                var hidden = adv.style.display === 'none' || !adv.style.display;
                adv.style.display = hidden ? 'grid' : 'none';
                if (chev) chev.textContent = hidden ? '\u25BC' : '\u25B6';
            });
        }
    }

    // ── Timeframe → seconds ──────────────────────────────────────────────
    // Maps the global klineChartTimeframe to bar-seconds for TF-aware Sharpe.
    function _tfToSeconds(tf) {
        if (!tf) return 86400;
        var t = String(tf).trim().toLowerCase();
        // Common forms: 1m, 5m, 15m, 1h, 4h, 1d, 1w, 1M, or minutes-as-number
        var m = t.match(/^(\d+)\s*(m|h|d|w|mon)?$/);
        if (!m) return 86400;
        var n = parseInt(m[1], 10);
        var unit = m[2] || 'm';
        if (unit === 'm') return n * 60;
        if (unit === 'h') return n * 3600;
        if (unit === 'd') return n * 86400;
        if (unit === 'w') return n * 7 * 86400;
        if (unit === 'mon') return n * 30 * 86400;
        return 86400;
    }

    // ── Validation ────────────────────────────────────────────────────────

    function debounceValidate() {
        if (_previewTimer) clearTimeout(_previewTimer);
        _previewTimer = setTimeout(validateFormulas, 400);
    }

    function validateFormulas() {
        validateOne('bt-entry', 'bt-entry-status');
        validateOne('bt-exit', 'bt-exit-status');
    }

    function validateOne(inputId, statusId) {
        var el = document.getElementById(inputId);
        var st = document.getElementById(statusId);
        if (!el || !st) return;
        var val = el.value.trim();
        if (!val) { st.textContent = ''; st.className = 'bt-validation'; return; }
        var result = window.FormulaParser.parse(val);
        if (result.error) {
            st.textContent = result.error;
            st.className = 'bt-validation bt-error';
        } else {
            st.textContent = 'Valid';
            st.className = 'bt-validation bt-ok';
        }
    }

    // ── Run Backtest ──────────────────────────────────────────────────────

    function runBacktest() {
        var entry = (document.getElementById('bt-entry').value || '').trim();
        var exit = (document.getElementById('bt-exit').value || '').trim();
        if (!entry || !exit) {
            showToast('Enter both entry and exit formulas');
            return;
        }

        if (!window.CustomIndicators || typeof window.CustomIndicators.extractOHLCV !== 'function') {
            showToast('Custom indicators module not loaded');
            renderError('CustomIndicators module not available. Try reloading the page.');
            return;
        }

        var ohlcv = window.CustomIndicators.extractOHLCV();
        if (!ohlcv) {
            showToast('Load a chart first');
            return;
        }

        if (!window.Backtester || typeof window.Backtester.run !== 'function') {
            showToast('Backtester engine not loaded');
            renderError('Backtester module not available. Try reloading the page.');
            return;
        }

        // Check for quant variable dependencies in entry/exit formulas
        var FP = window.FormulaParser;
        if (FP && FP.getQuantDeps && FP.QUANT_VARS) {
            var deps = FP.getQuantDeps(entry).concat(FP.getQuantDeps(exit));
            if (deps.length > 0) {
                // Merge cached quant data into ohlcv (uses same cache as custom indicators)
                var QV = FP.QUANT_VARS;
                var _qc = (window.CustomIndicators && window.CustomIndicators.getQuantCache) ? window.CustomIndicators.getQuantCache() : {};
                for (var ident in QV) {
                    var series = QV[ident];
                    if (_qc[series]) ohlcv[ident] = _qc[series];
                }
            }
        }

        // Apply regime filter if selected
        var regimeFilter = document.getElementById('bt-regime') ? document.getElementById('bt-regime').value : 'all';
        if (regimeFilter === 'favorable') {
            entry = 'AND(REGIME == 1, ' + entry + ')';
        } else if (regimeFilter === 'unfavorable') {
            entry = 'AND(REGIME == 0, ' + entry + ')';
        }
        // If regime filter is active, ensure REGIME data is in quant deps
        if (regimeFilter !== 'all' && FP && FP.getQuantDeps) {
            var regimeDeps = FP.getQuantDeps(entry);
            if (regimeDeps.length > 0) {
                var _qc = (window.CustomIndicators && window.CustomIndicators.getQuantCache) ? window.CustomIndicators.getQuantCache() : {};
                for (var ident in (FP.QUANT_VARS || {})) {
                    var series = FP.QUANT_VARS[ident];
                    if (_qc[series] && !ohlcv[ident]) ohlcv[ident] = _qc[series];
                }
            }
        }

        // Resolve date range to bar indices
        var dataList = (typeof klineChart !== 'undefined' && klineChart) ? klineChart.getDataList() : null;
        var startBar = 0, endBar = ohlcv.close.length - 1;
        var startDateVal = document.getElementById('bt-start-date').value;
        var endDateVal = document.getElementById('bt-end-date').value;
        if (startDateVal && dataList) {
            var startTs = new Date(startDateVal).getTime();
            for (var i = 0; i < dataList.length; i++) {
                if (dataList[i].timestamp >= startTs) { startBar = i; break; }
            }
        }
        if (endDateVal && dataList) {
            var endTs = new Date(endDateVal).getTime() + 86400000; // end of day
            for (var i = dataList.length - 1; i >= 0; i--) {
                if (dataList[i].timestamp <= endTs) { endBar = i; break; }
            }
        }

        var tfGlobal = (typeof klineChartTimeframe !== 'undefined') ? klineChartTimeframe : null;
        var config = {
            entry: entry,
            exit: exit,
            direction: document.getElementById('bt-direction').value,
            instrument: _val('bt-instrument', 'spot'),
            initialCapital: _num('bt-capital', 10000),
            sizePercent: _num('bt-size', 100),
            commissionPercent: _num('bt-commission', 0),
            slippageBps: _num('bt-slippage', 0),
            stopType: _val('bt-stop-type', 'close'),
            stopLossPercent: _num('bt-stop-loss', 0),
            takeProfitPercent: _num('bt-take-profit', 0),
            // Fall back to next_open, matching the engine's own
            // default — not 'close', which is the look-ahead reading.
            entryMode: _val('bt-entry-mode', 'next_open'),
            barSeconds: _tfToSeconds(tfGlobal),
            startBar: startBar,
            endBar: endBar
        };

        // Pre-load REGIME so we can split stats by regime post-run.
        // Graceful: if fetch fails or series empty, regime-split tabs are hidden (falls back to 'all').
        _ensureRegimeLoaded(ohlcv, dataList).then(function(regimeSeries) {
            if (regimeSeries) config.regimeSeries = regimeSeries;

            try {
                var result = window.Backtester.run(config, ohlcv);
                _lastResult = result;
                _lastOhlcv = ohlcv;
                _lastConfig = config;

                if (result.error) {
                    showToast(result.error);
                    renderError(result.error);
                    return;
                }

                renderResults(result, config);
                drawChartMarkers(result.trades);
                _fetchQuantContext(entry, config);

                // A saved RESULT is only offered once there is one to save.
                var saveRunBtn = document.getElementById('bt-save-run');
                if (saveRunBtn) saveRunBtn.disabled = false;

                var s = result.stats;
                showToast(s.totalTrades + ' trades, ' + (s.winRate * 100).toFixed(1) + '% win rate, ' +
                          (s.totalReturnPercent >= 0 ? '+' : '') + s.totalReturnPercent.toFixed(1) + '% return');
            } catch (e) {
                var msg = e && e.message ? e.message : String(e);
                console.error('[Backtester]', e);
                showToast('Backtest error: ' + msg);
                renderError('Error: ' + msg);
            }
        });
    }

    // Ensure REGIME quant series is loaded (or try). Returns Promise<array|null>.
    function _ensureRegimeLoaded(ohlcv, dataList) {
        var cache = (window.CustomIndicators && window.CustomIndicators.getQuantCache)
            ? window.CustomIndicators.getQuantCache() : {};
        if (cache.REGIME && cache.REGIME.length === ohlcv.close.length) {
            return Promise.resolve(cache.REGIME);
        }
        if (!dataList || !dataList.length || !window.CustomIndicators || !window.CustomIndicators.fetchQuantSeries) {
            return Promise.resolve(null);
        }
        var timestamps = [];
        for (var i = 0; i < dataList.length; i++) timestamps.push(dataList[i].timestamp / 1000);
        return window.CustomIndicators.fetchQuantSeries(['REGIME'], timestamps)
            .then(function(res) { return res && res.REGIME ? res.REGIME : null; })
            .catch(function() { return null; });
    }

    // ── Quant Cross-Validation ──────────────────────────────────────────

    // Map known formula quant comparisons to z-score conditions for the conditional engine.
    // Scans the entry formula tokens for patterns like FEAR_GREED < 25, REGIME == 1, etc.
    function _extractQuantConditions(formulaStr) {
        var conditions = [];
        var FP = window.FormulaParser;
        if (!FP || !FP.QUANT_VARS) return conditions;

        // Simple regex scan for "QUANT_VAR op value" patterns
        var QV = FP.QUANT_VARS;
        var varNames = Object.keys(QV).join('|');
        var re = new RegExp('(' + varNames + ')\\s*([<>=!]+)\\s*([\\d.\\-]+)', 'gi');
        var match;
        while ((match = re.exec(formulaStr)) !== null) {
            var ident = match[1].toLowerCase();
            var op = match[2];
            var val = parseFloat(match[3]);
            var series = QV[ident];
            if (!series || isNaN(val)) continue;

            // Map to z-score condition format: METRIC:op:threshold
            // For bounded metrics (Fear & Greed 0-100), rough z-score mapping
            var zOp = op === '<' || op === '<=' ? 'lt' : op === '>' || op === '>=' ? 'gt' : 'eq';
            // Use raw value as approximate z-score threshold (the conditional engine
            // will match against actual z-scores, so extreme values naturally filter)
            conditions.push(series + ':' + zOp + ':' + val);
        }
        return conditions;
    }

    function _fetchQuantContext(entryFormula, config) {
        var conditions = _extractQuantConditions(entryFormula);
        if (conditions.length === 0) return;

        var target = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : 'BTC';
        var url = '/api/backtest/quant-context?conditions=' + encodeURIComponent(conditions.join(',')) +
                  '&target=' + encodeURIComponent(target) + '&horizons=7,30,90';

        fetch(url, { credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error || !data.horizons) return;
                var panel = document.getElementById('bt-quant-context');
                if (!panel) {
                    // Create panel after stats
                    var results = document.getElementById('bt-results');
                    if (!results) return;
                    var div = document.createElement('div');
                    div.id = 'bt-quant-context';
                    div.className = 'bt-quant-context';
                    results.appendChild(div);
                    panel = div;
                }
                var h = data.horizons;
                var instances = 0;
                var rows = '';
                ['7d', '30d', '90d'].forEach(function(key) {
                    var d = h[key];
                    if (!d) return;
                    instances = Math.max(instances, d.instances || 0);
                    var med = d.median_return != null ? (d.median_return >= 0 ? '+' : '') + d.median_return.toFixed(1) + '%' : 'N/A';
                    var p25 = d.p25 != null ? d.p25.toFixed(1) + '%' : '';
                    var p75 = d.p75 != null ? d.p75.toFixed(1) + '%' : '';
                    rows += '<tr><td>' + key + '</td><td>' + med + '</td><td style="color:#888">' + p25 + ' / ' + p75 + '</td></tr>';
                });

                panel.innerHTML =
                    '<div class="bt-section-title" style="margin-top:8px;" title="Conditional engine: how the target asset moved historically when these conditions held. Distinct from full trade sim.">Historical Analog <span style="color:#888;font-weight:normal;font-size:10px;">(' + instances + ' historical matches · forward return distribution)</span></div>' +
                    '<table class="bt-stats-mini" style="font-size:11px;width:100%;">' +
                    '<thead><tr><th>Horizon</th><th>Median Return</th><th>p25 / p75</th></tr></thead>' +
                    '<tbody>' + rows + '</tbody></table>';
            })
            .catch(function() { /* silent — quant context is best-effort */ });
    }

    // ── Chart Markers ──────────────────────────────────────────

    function registerMarkerOverlay() {
        if (typeof klinecharts === 'undefined' || !klinecharts.registerOverlay) return;
        try {
            klinecharts.registerOverlay({
                name: 'btMarker',
                totalStep: 1,
                needDefaultPointFigure: false,
                needDefaultXAxisFigure: false,
                needDefaultYAxisFigure: false,
                createPointFigures: function(params) {
                    var coords = params.coordinates;
                    var ext = (params.overlay || {}).extendData || {};
                    if (!coords || !coords[0]) return [];
                    var c = coords[0];
                    var isEntry = ext.type === 'entry';
                    var color = isEntry ? '#26a69a' : '#ef5350';
                    var symbol = isEntry ? '\u25B2' : '\u25BC';  // ▲ ▼
                    var yOff = isEntry ? 10 : -10;
                    return [{
                        type: 'text',
                        attrs: { x: c.x, y: c.y + yOff, text: symbol, align: 'center', baseline: isEntry ? 'top' : 'bottom' },
                        styles: { color: color, size: 12, family: 'sans-serif' }
                    }];
                }
            });
        } catch (e) { /* already registered or KLineChart not loaded */ }
    }

    function drawChartMarkers(trades) {
        if (typeof klineChart === 'undefined' || !klineChart) return;
        clearMarkers();
        registerMarkerOverlay();

        var dataList = klineChart.getDataList();
        if (!dataList || !dataList.length) return;

        for (var i = 0; i < trades.length; i++) {
            var t = trades[i];
            var entryBar = dataList[t.entryBar];
            var exitBar = dataList[t.exitBar];
            if (!entryBar || !exitBar) continue;

            // Entry marker (below candle for long, above for short)
            var entryVal = t.direction === 'long' ? (entryBar._origLow || entryBar.low) : (entryBar._origHigh || entryBar.high);
            try {
                var oid = klineChart.createOverlay({
                    name: 'btMarker',
                    groupId: 'backtest',
                    lock: true,
                    points: [{ timestamp: entryBar.timestamp, value: entryVal }],
                    extendData: { type: 'entry' }
                });
                if (oid) _markerOverlayIds.push(oid);
            } catch (e) {}

            // Exit marker (above candle for long, below for short)
            var exitVal = t.direction === 'long' ? (exitBar._origHigh || exitBar.high) : (exitBar._origLow || exitBar.low);
            try {
                var oid2 = klineChart.createOverlay({
                    name: 'btMarker',
                    groupId: 'backtest',
                    lock: true,
                    points: [{ timestamp: exitBar.timestamp, value: exitVal }],
                    extendData: { type: 'exit' }
                });
                if (oid2) _markerOverlayIds.push(oid2);
            } catch (e) {}
        }
    }

    function clearMarkers() {
        if (typeof klineChart === 'undefined' || !klineChart) return;
        for (var i = 0; i < _markerOverlayIds.length; i++) {
            try { klineChart.removeOverlay(_markerOverlayIds[i]); } catch (e) {}
        }
        _markerOverlayIds = [];
    }

    function scrollToTrade(tradeIndex) {
        if (!_lastResult || !_lastResult.trades[tradeIndex]) return;
        if (typeof klineChart === 'undefined' || !klineChart) return;
        var t = _lastResult.trades[tradeIndex];
        try { klineChart.scrollToDataIndex(t.entryBar); } catch (e) {}
    }

    // ── Strategy Persistence ───────────────────────────────────

    function getConfig() {
        return {
            entry: (document.getElementById('bt-entry').value || '').trim(),
            exit: (document.getElementById('bt-exit').value || '').trim(),
            direction: document.getElementById('bt-direction').value,
            capital: parseFloat(document.getElementById('bt-capital').value) || 10000,
            size: parseFloat(document.getElementById('bt-size').value) || 100,
            commission: parseFloat(document.getElementById('bt-commission').value) || 0,
            startDate: document.getElementById('bt-start-date').value || '',
            endDate: document.getElementById('bt-end-date').value || '',
            // Realism fields — must be saved with the strategy.
            regime: _val('bt-regime', 'all'),
            instrument: _val('bt-instrument', 'spot'),
            slippageBps: _num('bt-slippage', 0),
            stopType: _val('bt-stop-type', 'close'),
            stopLossPercent: _num('bt-stop-loss', 0),
            takeProfitPercent: _num('bt-take-profit', 0),
            entryMode: _val('bt-entry-mode', 'next_open'),
            timeframe: (typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '')
        };
    }

    function _set(id, value) {
        if (value === undefined || value === null || value === '') return;
        var el = document.getElementById(id);
        if (el) el.value = value;
    }

    function setConfig(cfg) {
        if (cfg.entry) document.getElementById('bt-entry').value = cfg.entry;
        if (cfg.exit) document.getElementById('bt-exit').value = cfg.exit;
        if (cfg.direction) document.getElementById('bt-direction').value = cfg.direction;
        if (cfg.capital) document.getElementById('bt-capital').value = cfg.capital;
        if (cfg.size) document.getElementById('bt-size').value = cfg.size;
        if (cfg.commission != null) document.getElementById('bt-commission').value = cfg.commission;
        _set('bt-start-date', cfg.startDate);
        _set('bt-end-date', cfg.endDate);
        // Restore the rest of the setup. Older saved strategies have none
        // of these, so those fields keep their defaults.
        _set('bt-regime', cfg.regime);
        _set('bt-instrument', cfg.instrument);
        _set('bt-slippage', cfg.slippageBps);
        _set('bt-stop-type', cfg.stopType);
        _set('bt-stop-loss', cfg.stopLossPercent);
        _set('bt-take-profit', cfg.takeProfitPercent);
        _set('bt-entry-mode', cfg.entryMode);
        // Open the Realism panel if anything in it was restored, so a loaded
        // strategy never runs with settings the user cannot see.
        if (cfg.slippageBps || cfg.stopLossPercent || cfg.takeProfitPercent ||
            (cfg.stopType && cfg.stopType !== 'close') ||
            (cfg.entryMode && cfg.entryMode !== 'next_open')) {
            var adv = document.getElementById('bt-advanced');
            var chev = document.getElementById('bt-adv-chevron');
            if (adv && (adv.style.display === 'none' || !adv.style.display)) {
                adv.style.display = 'grid';
                if (chev) chev.textContent = '▼';
            }
        }
        validateFormulas();
    }

    function saveStrategy() {
        var cfg = getConfig();
        if (!cfg.entry || !cfg.exit) {
            showToast('Enter entry and exit formulas first');
            return;
        }
        var nameEl = document.getElementById('bt-save-name');
        var name = (nameEl.value || '').trim();
        if (!name) {
            nameEl.focus();
            showToast('Enter a strategy name');
            return;
        }

        var body = {
            name: name.trim(),
            entry_formula: cfg.entry,
            exit_formula: cfg.exit,
            direction: cfg.direction,
            capital: cfg.capital,
            size_percent: cfg.size,
            commission_percent: cfg.commission,
            // getConfig() captured startDate/endDate all along but
            // this body never sent them, and never captured the seven Realism
            // fields at all. Save now keeps the whole setup.
            timeframe: cfg.timeframe,
            start_date: cfg.startDate,
            end_date: cfg.endDate,
            regime: cfg.regime,
            instrument: cfg.instrument,
            slippage_bps: cfg.slippageBps,
            stop_type: cfg.stopType,
            stop_loss_percent: cfg.stopLossPercent,
            take_profit_percent: cfg.takeProfitPercent,
            entry_mode: cfg.entryMode
        };

        fetch('/api/strategies', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrf() },
            credentials: 'same-origin',
            body: JSON.stringify(body)
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) showToast(data.error);
            else {
                showToast('Strategy "' + name + '" saved');
                document.getElementById('bt-save-name').value = '';
            }
        }).catch(function() { showToast('Failed to save'); });
    }

    function loadStrategies() {
        fetch('/api/strategies')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.strategies || !data.strategies.length) {
                showToast('No saved strategies');
                return;
            }
            showStrategyPicker(data.strategies);
        }).catch(function() { showToast('Failed to load strategies'); });
    }

    function showStrategyPicker(strategies) {
        // Simple dropdown in results area
        var el = document.getElementById('bt-results');
        if (!el) return;
        var html = '<div class="bt-section-title">Saved Strategies</div>';
        for (var i = 0; i < strategies.length; i++) {
            var s = strategies[i];
            html += '<div class="bt-strategy-row" data-idx="' + i + '">' +
                '<span class="bt-strategy-name">' + escapeHtml(s.name) + '</span>' +
                '<span class="bt-strategy-meta">' + s.direction + ' | ' + escapeHtml(s.entry_formula.substring(0, 30)) + '</span>' +
                '<button class="bt-strategy-load" data-idx="' + i + '">Load</button>' +
                '<button class="bt-strategy-del" data-idx="' + i + '" title="Delete">&times;</button>' +
            '</div>';
        }
        el.innerHTML = html;

        // Wire load/delete buttons
        el.querySelectorAll('.bt-strategy-load').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var s = strategies[parseInt(this.getAttribute('data-idx'))];
                var p = s.params || {};  // absent on pre-existing rows
                setConfig({
                    entry: s.entry_formula,
                    exit: s.exit_formula,
                    direction: s.direction,
                    capital: s.capital || 10000,
                    size: s.size_percent || 100,
                    commission: s.commission_percent || 0,
                    startDate: p.start_date,
                    endDate: p.end_date,
                    regime: p.regime,
                    instrument: p.instrument,
                    slippageBps: p.slippage_bps,
                    stopType: p.stop_type,
                    stopLossPercent: p.stop_loss_percent,
                    takeProfitPercent: p.take_profit_percent,
                    entryMode: p.entry_mode
                });
                // Be explicit when a strategy predates the full-setup save,
                // rather than quietly loading half of it.
                if (!s.params) {
                    showToast('Loaded "' + s.name + '" — saved before full settings were kept, so risk controls are at defaults');
                } else if (p.timeframe && typeof klineChartTimeframe !== 'undefined'
                           && p.timeframe !== klineChartTimeframe) {
                    showToast('Loaded "' + s.name + '" — saved on ' + p.timeframe +
                              ', chart is on ' + klineChartTimeframe);
                } else {
                    showToast('Loaded "' + s.name + '"');
                }
            });
        });
        el.querySelectorAll('.bt-strategy-del').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var s = strategies[parseInt(this.getAttribute('data-idx'))];
                if (!confirm('Delete "' + s.name + '"?')) return;
                fetch('/api/strategies/' + s.id, { method: 'DELETE', headers: { 'X-CSRFToken': _csrf() }, credentials: 'same-origin' })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.error) showToast(data.error);
                    else { showToast('Deleted'); loadStrategies(); }
                });
            });
        });
    }

    // ── Run history ───────────────────────────────────────────
    //
    // Saved RUNS, as opposed to the saved STRATEGIES the Load button shows.
    // Same rows an AI assistant sees via backtest_get_runs, so a backtest an
    // agent saved shows up here, and the reverse.

    // An MCP agent can persist a RUN (a result); the browser can also save a
    // STRATEGY (a recipe). This saves a run — it stores the numbers already on screen. The same rule applies here: this
    // must never re-run the backtest, only record what was measured.
    function saveBacktestResult() {
        if (!_lastResult || _lastResult.error || !_lastConfig) {
            showToast('Run a backtest first');
            return;
        }
        var name = prompt('Name this saved result (optional):', '');
        if (name === null) return; // cancelled
        name = name.trim();

        var s = _lastResult.stats || {};
        var cfg = _lastConfig;
        var lookbackBars = (cfg.endBar != null && cfg.startBar != null)
            ? (cfg.endBar - cfg.startBar + 1) : undefined;

        var body = {
            symbol: (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : '',
            timeframe: (typeof klineChartTimeframe !== 'undefined') ? klineChartTimeframe : '',
            expression: cfg.entry,
            name: name || undefined,
            lookback_bars: lookbackBars,
            // The reviewed numbers, verbatim — nothing here is recomputed.
            result: {
                totalTrades: s.totalTrades,
                winRate: s.winRate,
                profitFactor: s.profitFactor,
                totalReturnPercent: s.totalReturnPercent,
                maxDrawdownPercent: s.maxDrawdownPercent,
                sharpeRatio: s.sharpeRatio,
                sortinoRatio: s.sortinoRatio,
                calmarRatio: s.calmarRatio,
                ulcerIndex: s.ulcerIndex,
                avgWin: s.avgWin,
                avgLoss: s.avgLoss,
                avgRR: s.avgRR,
                avgBarsInTrade: s.avgBarsInTrade,
                maxConsecutiveWins: s.maxConsecutiveWins,
                maxConsecutiveLosses: s.maxConsecutiveLosses
            },
            config: {
                exit: cfg.exit,
                direction: cfg.direction,
                instrument: cfg.instrument,
                initialCapital: cfg.initialCapital,
                sizePercent: cfg.sizePercent,
                commissionPercent: cfg.commissionPercent,
                slippageBps: cfg.slippageBps,
                stopType: cfg.stopType,
                stopLossPercent: cfg.stopLossPercent,
                takeProfitPercent: cfg.takeProfitPercent,
                entryMode: cfg.entryMode
            }
        };

        fetch('/api/backtest/runs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrf() },
            credentials: 'same-origin',
            body: JSON.stringify(body)
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) showToast(data.error);
            else showToast('Result saved — find it under History');
        }).catch(function() { showToast('Failed to save result'); });
    }

    function _fmtPct(v) {
        return (v === null || v === undefined || isNaN(v)) ? '—' : (+v).toFixed(1) + '%';
    }

    function _fmtNum(v) {
        return (v === null || v === undefined || isNaN(v)) ? '—' : (+v).toFixed(2);
    }

    function _runHeadline(run) {
        // The two models report different measurements. Label them, never
        // merge them into one column.
        if (run.model === 'Full Simulation') {
            return 'Win ' + _fmtPct(run.winRate === null || run.winRate === undefined
                        ? null : run.winRate * 100) +
                   ' · Return ' + _fmtPct(run.totalReturnPercent) +
                   ' · Sharpe ' + _fmtNum(run.sharpeRatio);
        }
        return 'Hit ' + _fmtPct(run.hit_rate === null || run.hit_rate === undefined
                    ? null : run.hit_rate * 100) +
               ' · Mean ' + _fmtPct(run.mean_return === null || run.mean_return === undefined
                    ? null : run.mean_return * 100);
    }

    function loadRunHistory() {
        fetch('/api/backtest/runs?limit=25', { credentials: 'same-origin' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var el = document.getElementById('bt-results');
            if (!el) return;
            if (!data.runs || !data.runs.length) {
                el.innerHTML = '<div class="bt-section-title">Saved Runs</div>' +
                    '<div class="bt-placeholder">No saved runs yet. Runs you or your AI assistant save appear here.</div>';
                return;
            }
            var html = '<div class="bt-section-title">Saved Runs</div>';
            for (var i = 0; i < data.runs.length; i++) {
                var run = data.runs[i];
                var when = run.created_at ? String(run.created_at).slice(0, 10) : '';
                html += '<div class="bt-strategy-row" data-idx="' + i + '">' +
                    '<span class="bt-strategy-name">' + escapeHtml(run.name || (run.symbol + ' ' + run.timeframe)) + '</span>' +
                    '<span class="bt-strategy-meta">' + escapeHtml(run.model) + ' · ' + escapeHtml(run.symbol) +
                        ' · ' + escapeHtml(when) + ' · ' + escapeHtml(_runHeadline(run)) + '</span>' +
                    '<button class="bt-strategy-load" data-idx="' + i + '">Open</button>' +
                    '<button class="bt-strategy-del" data-idx="' + i + '" title="Delete">&times;</button>' +
                '</div>';
            }
            el.innerHTML = html;

            el.querySelectorAll('.bt-strategy-load').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    _showRunDetail(data.runs[parseInt(this.getAttribute('data-idx'))]);
                });
            });
            el.querySelectorAll('.bt-strategy-del').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var run = data.runs[parseInt(this.getAttribute('data-idx'))];
                    if (!confirm('Delete this saved run?')) return;
                    fetch('/api/backtest/runs/' + run.run_id, {
                        method: 'DELETE',
                        headers: { 'X-CSRFToken': _csrf() },
                        credentials: 'same-origin'
                    }).then(function(r) { return r.json(); })
                      .then(function(d) {
                          if (d.error) showToast(d.error);
                          else { showToast('Deleted'); loadRunHistory(); }
                      });
                });
            });
        }).catch(function() { showToast('Failed to load run history'); });
    }

    function _showRunDetail(summary) {
        fetch('/api/backtest/runs/' + summary.run_id, { credentials: 'same-origin' })
        .then(function(r) { return r.json(); })
        .then(function(run) {
            var el = document.getElementById('bt-results');
            if (!el || run.error) { showToast(run && run.error ? run.error : 'Not found'); return; }
            var cfg = run.config || {};
            var html = '<div class="bt-section-title">' + escapeHtml(run.name || 'Saved run') + '</div>' +
                '<div class="bt-strategy-meta" style="display:block;margin-bottom:8px">' +
                    escapeHtml(run.model) + ' · ' + escapeHtml(run.symbol) + ' ' + escapeHtml(run.timeframe || '') +
                    ' · ' + escapeHtml(String(run.created_at || '').slice(0, 16).replace('T', ' ')) +
                '</div>';
            html += '<div class="bt-strategy-meta" style="display:block;margin-bottom:8px">Entry: <code>' +
                escapeHtml(cfg.entry_expression || run.expression || '') + '</code></div>';
            if (cfg.exit_expression) {
                html += '<div class="bt-strategy-meta" style="display:block;margin-bottom:8px">Exit: <code>' +
                    escapeHtml(cfg.exit_expression) + '</code></div>';
            }
            html += '<table class="bt-stats-mini" style="font-size:11px;width:100%;">';
            var res = run.result || {};
            Object.keys(res).forEach(function(k) {
                var v = res[k];
                if (v === null || typeof v === 'object') return;
                html += '<tr><td>' + escapeHtml(k) + '</td><td style="text-align:right">' +
                    escapeHtml(typeof v === 'number' ? (+v).toFixed(4).replace(/\.?0+$/, '') : String(v)) +
                    '</td></tr>';
            });
            html += '</table>';
            html += '<div class="bt-strategy-meta" style="display:block;margin-top:10px">' +
                'Saved runs are a record of what was measured — reopening one does not re-run it.</div>';
            html += '<button class="bt-strategy-load" id="bt-history-back" style="margin-top:10px">Back to runs</button>';
            el.innerHTML = html;
            var back = document.getElementById('bt-history-back');
            if (back) back.addEventListener('click', loadRunHistory);
        }).catch(function() { showToast('Failed to load run'); });
    }

    // ── CSV Export ─────────────────────────────────────────────────────────

    function _barDate(barIdx) {
        var dl = (typeof klineChart !== 'undefined' && klineChart) ? klineChart.getDataList() : null;
        if (!dl || !dl[barIdx]) return '';
        var d = new Date(dl[barIdx].timestamp);
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }

    function exportCSV() {
        if (!_lastResult || !_lastResult.trades.length) {
            showToast('Run a backtest first');
            return;
        }

        var symbol = typeof klineChartSymbol !== 'undefined' ? klineChartSymbol : 'unknown';
        var tf = typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '';
        var cfg = _lastConfig || {};

        // Header with strategy config
        var lines = [
            'Backtest: ' + symbol + ' ' + tf,
            'Entry: "' + (cfg.entry || '') + '"',
            'Exit: "' + (cfg.exit || '') + '"',
            'Direction: ' + (cfg.direction || 'long') + ', Capital: ' + (cfg.initialCapital || 10000) +
                ', Size: ' + (cfg.sizePercent || 100) + '%, Commission: ' + (cfg.commissionPercent || 0) + '%',
            ''
        ];

        // Trade list with dates
        lines.push('#,Direction,Entry Date,Entry Price,Exit Date,Exit Price,Bars,P&L,P&L %,Exit Reason');
        for (var i = 0; i < _lastResult.trades.length; i++) {
            var t = _lastResult.trades[i];
            lines.push([
                i + 1, t.direction,
                _barDate(t.entryBar), t.entryPrice,
                _barDate(t.exitBar), t.exitPrice,
                t.bars, t.pnl.toFixed(2), t.pnlPercent.toFixed(2),
                t.exitReason || 'signal'
            ].join(','));
        }

        // Summary stats
        var s = _lastResult.stats;
        lines.push('');
        lines.push('Total Trades,' + s.totalTrades);
        lines.push('Winners,' + s.winners);
        lines.push('Losers,' + s.losers);
        lines.push('Win Rate,' + (s.winRate * 100).toFixed(2) + '%');
        lines.push('Profit Factor,' + (s.profitFactor === Infinity ? 'Inf' : s.profitFactor.toFixed(2)));
        lines.push('Total Return,' + s.totalReturnPercent.toFixed(2) + '%');
        lines.push('Buy & Hold Return,' + _benchmarkReturn(cfg));
        lines.push('Max Drawdown,' + s.maxDrawdownPercent.toFixed(2) + '%');
        lines.push('Sharpe Ratio,' + s.sharpeRatio.toFixed(2));
        lines.push('Sortino Ratio,' + (s.sortinoRatio != null ? s.sortinoRatio.toFixed(2) : '0.00'));
        lines.push('Calmar Ratio,' + (s.calmarRatio != null ? s.calmarRatio.toFixed(2) : '0.00'));
        lines.push('Ulcer Index,' + (s.ulcerIndex != null ? s.ulcerIndex.toFixed(2) : '0.00'));
        lines.push('Avg Win,' + s.avgWin.toFixed(2));
        lines.push('Avg Loss,' + s.avgLoss.toFixed(2));
        lines.push('Avg R:R,' + s.avgRR.toFixed(2));
        lines.push('Avg Bars in Trade,' + s.avgBarsInTrade.toFixed(1));
        lines.push('Max Consecutive Wins,' + s.maxConsecutiveWins);
        lines.push('Max Consecutive Losses,' + s.maxConsecutiveLosses);

        var blob = new Blob([lines.join('\n')], { type: 'text/csv' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'backtest_' + symbol + '_' + tf + '.csv';
        a.click();
        URL.revokeObjectURL(a.href);
        showToast('CSV exported');
    }

    // ── Render Results ────────────────────────────────────────────────────

    function renderError(msg) {
        var el = document.getElementById('bt-results');
        if (el) el.innerHTML = '<div class="bt-placeholder bt-error">' + escapeHtml(msg) + '</div>';
    }

    // Build the stats grid for a given scope.
    //   scope = 'all' → portfolio stats (Sharpe/Sortino/Calmar/Ulcer/Max DD included)
    //   scope = 'favorable' | 'unfavorable' | 'unknown' → trade-only subset
    // Portfolio-level cards in non-'all' scopes show '—' (not meaningful for filtered trades).
    function _renderStatsGrid(result, config, scope) {
        scope = scope || 'all';
        var source;
        if (scope === 'all') source = result.stats;
        else if (result.statsRegime) source = result.statsRegime[scope];
        else source = result.stats;

        var s = source || {};
        var tradeOnly = scope !== 'all';

        var sortino = s.sortinoRatio != null ? s.sortinoRatio : 0;
        var calmar = s.calmarRatio != null ? s.calmarRatio : 0;
        var ulcer = s.ulcerIndex != null ? s.ulcerIndex : 0;
        var sharpe = s.sharpeRatio != null ? s.sharpeRatio : 0;
        var maxDDPct = s.maxDrawdownPercent != null ? s.maxDrawdownPercent : 0;

        var portfolioCard = function(label, val, cls, tip) {
            if (tradeOnly) {
                return '<div class="bt-stat" title="' + (tip || 'Portfolio-level metric — not meaningful for a regime-filtered subset') + '"><div class="bt-stat-val" style="color:#484f58">—</div><div class="bt-stat-label">' + label + '</div></div>';
            }
            return statCard(label, val, cls);
        };

        return '<div class="bt-stats-grid">' +
            statCard('Trades', s.totalTrades || 0, '') +
            statCard('Win Rate', ((s.winRate || 0) * 100).toFixed(1) + '%', (s.winRate || 0) >= 0.5 ? 'pos' : 'neg') +
            statCard('Profit Factor', s.profitFactor === Infinity ? 'Inf' : ((s.profitFactor || 0).toFixed(2)), (s.profitFactor || 0) >= 1 ? 'pos' : 'neg') +
            statCard('Return', ((s.totalReturnPercent || 0) >= 0 ? '+' : '') + ((s.totalReturnPercent || 0).toFixed(2)) + '%', (s.totalReturnPercent || 0) >= 0 ? 'pos' : 'neg') +
            portfolioCard('Max DD', '-' + maxDDPct.toFixed(2) + '%', 'neg') +
            portfolioCard('Sharpe', sharpe.toFixed(2), sharpe >= 1 ? 'pos' : sharpe >= 0 ? '' : 'neg') +
            portfolioCard('Sortino', sortino.toFixed(2), sortino >= 1 ? 'pos' : sortino >= 0 ? '' : 'neg') +
            portfolioCard('Calmar', calmar.toFixed(2), calmar >= 1 ? 'pos' : calmar >= 0 ? '' : 'neg') +
            portfolioCard('Ulcer', ulcer.toFixed(2), ulcer <= 5 ? 'pos' : ulcer <= 10 ? '' : 'neg') +
            statCard('Avg Win', fmtMoney(s.avgWin || 0), 'pos') +
            statCard('Avg Loss', fmtMoney(s.avgLoss || 0), 'neg') +
            statCard('Avg R:R', (s.avgRR || 0).toFixed(2), (s.avgRR || 0) >= 1 ? 'pos' : 'neg') +
            statCard('Avg Bars', (s.avgBarsInTrade || 0).toFixed(1), '') +
            statCard('W Streak', s.maxConsecutiveWins || 0, 'pos') +
            statCard('L Streak', s.maxConsecutiveLosses || 0, 'neg') +
            (scope === 'all' ? statCard('B&H', _benchmarkReturn(config) + '%', '') : '') +
            '</div>';
    }

    function renderResults(result, config) {
        var el = document.getElementById('bt-results');
        if (!el) return;

        var html =
            '<div class="bt-section-title" title="Trade-by-trade simulation with entry/exit, sizing, fees, and slippage">Full Simulation ' +
                '<span style="color:#888;font-weight:normal;font-size:10px;">entry/exit · sizing · fees · slippage</span></div>';

        // Regime-split tabs (only when regime data was loaded)
        if (result.statsRegime) {
            var favN = result.statsRegime.favorable.totalTrades;
            var unfN = result.statsRegime.unfavorable.totalTrades;
            var unkN = result.statsRegime.unknown.totalTrades;
            html += '<div class="bt-regime-tabs" id="bt-regime-tabs">' +
                '<button class="bt-regime-tab active" data-scope="all">All <span class="bt-regime-n">' + result.stats.totalTrades + '</span></button>' +
                '<button class="bt-regime-tab" data-scope="favorable" title="Trades entered during REGIME == 1">Favorable <span class="bt-regime-n">' + favN + '</span></button>' +
                '<button class="bt-regime-tab" data-scope="unfavorable" title="Trades entered during REGIME == 0">Unfavorable <span class="bt-regime-n">' + unfN + '</span></button>' +
                (unkN > 0 ? '<button class="bt-regime-tab" data-scope="unknown" title="Trades entered when regime data was missing">Unknown <span class="bt-regime-n">' + unkN + '</span></button>' : '') +
            '</div>';
        }

        html += '<div class="bt-stats-grid-wrap" id="bt-stats-grid-wrap">' + _renderStatsGrid(result, config, 'all') + '</div>';

        // Hold-N Signal Test — compact summary using client-side flip detector
        var holdN = _computeHoldN(config.entry, _lastOhlcv, 5);
        if (holdN) {
            html += '<div class="bt-hold-n" style="margin-top:6px;padding:6px 8px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:4px;">' +
                '<div class="bt-section-title" style="margin:0 0 4px;" title="Signal-flip model: on each False→True transition, hold N bars and measure forward return. Used by Copilot + MCP.">' +
                    'Hold-N Signal Test <span style="color:#888;font-weight:normal;font-size:10px;">flip → hold ' + holdN.hold + ' bars → forward return</span>' +
                '</div>' +
                '<div style="font-size:11px;color:#c9d1d9;">' +
                    '<b>' + holdN.flips + '</b> flips · Hit <b>' + (holdN.hitRate * 100).toFixed(1) + '%</b> · ' +
                    'Median <b>' + (holdN.median >= 0 ? '+' : '') + holdN.median.toFixed(2) + '%</b> · ' +
                    'Mean <b>' + (holdN.mean >= 0 ? '+' : '') + holdN.mean.toFixed(2) + '%</b>' +
                '</div>' +
                '</div>';
        }

        // Equity curve
        html += '<div class="bt-equity-wrap">' +
            '<div class="bt-section-title">Equity Curve <span class="bt-legend"><span class="bt-legend-strat">&#9473; Strategy</span> <span class="bt-legend-bh">&#9476; Buy &amp; Hold</span></span></div>' +
            '<canvas id="bt-equity-canvas" class="bt-equity-canvas" height="120"></canvas>' +
            '<div class="bt-benchmark-checks" style="font-size:11px;color:#999;margin-top:3px;">' +
                '<label style="margin-right:8px;"><input type="checkbox" id="bt-bench-btc" style="vertical-align:-1px;"> <span style="color:#F7931A;">BTC</span></label>' +
                '<label style="margin-right:8px;"><input type="checkbox" id="bt-bench-spx" style="vertical-align:-1px;"> <span style="color:#4CAF50;">SPX</span></label>' +
                '<label><input type="checkbox" id="bt-bench-gold" style="vertical-align:-1px;"> <span style="color:#FFD700;">Gold</span></label>' +
            '</div>' +
        '</div>';

        // Trade list
        if (result.trades.length > 0) {
            html += '<div class="bt-trades-wrap">' +
                '<div class="bt-section-title">Trades (' + result.trades.length + ')</div>' +
                '<div class="bt-trades-scroll">' +
                '<table class="bt-trades-table">' +
                '<thead><tr><th>#</th><th>Dir</th><th>Entry</th><th></th><th>Exit</th><th></th><th>Bars</th><th>P&amp;L</th><th>P&amp;L %</th><th>Reason</th></tr></thead>' +
                '<tbody>';
            for (var i = 0; i < result.trades.length; i++) {
                var t = result.trades[i];
                var pnlClass = t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : '';
                html += '<tr class="bt-trade-row ' + pnlClass + '" data-trade="' + i + '">' +
                    '<td>' + (i + 1) + '</td>' +
                    '<td class="bt-dir-' + t.direction + '">' + (t.direction === 'long' ? 'L' : 'S') + '</td>' +
                    '<td class="bt-date">' + _barDate(t.entryBar) + '</td>' +
                    '<td>' + fmtPrice(t.entryPrice) + '</td>' +
                    '<td class="bt-date">' + _barDate(t.exitBar) + '</td>' +
                    '<td>' + fmtPrice(t.exitPrice) + '</td>' +
                    '<td>' + t.bars + '</td>' +
                    '<td class="' + pnlClass + '">' + fmtMoney(t.pnl) + '</td>' +
                    '<td class="' + pnlClass + '">' + (t.pnlPercent >= 0 ? '+' : '') + t.pnlPercent.toFixed(2) + '%</td>' +
                    '<td style="font-size:10px;color:#8b949e;">' + escapeHtml(t.exitReason || 'signal') + '</td>' +
                '</tr>';
            }
            html += '</tbody></table></div></div>';
        }

        el.innerHTML = html;

        // Wire regime-split tabs (swap stats grid only — trade list & equity stay 'all')
        el.querySelectorAll('.bt-regime-tab').forEach(function(tab) {
            tab.addEventListener('click', function() {
                el.querySelectorAll('.bt-regime-tab').forEach(function(t) { t.classList.remove('active'); });
                tab.classList.add('active');
                var scope = tab.getAttribute('data-scope');
                var wrap = document.getElementById('bt-stats-grid-wrap');
                if (wrap) wrap.innerHTML = _renderStatsGrid(result, config, scope);
            });
        });

        // Wire trade row clicks → scroll chart to that trade
        el.querySelectorAll('.bt-trade-row').forEach(function(row) {
            row.addEventListener('click', function() {
                var idx = parseInt(this.getAttribute('data-trade'));
                scrollToTrade(idx);
            });
        });

        // Draw equity curve + buy-and-hold benchmark + cross-asset benchmarks
        setTimeout(function() {
            _drawEquityWithBenchmarks(result, config);
            // Wire cross-asset benchmark checkboxes
            ['bt-bench-btc', 'bt-bench-spx', 'bt-bench-gold'].forEach(function(id) {
                var cb = document.getElementById(id);
                if (cb) cb.addEventListener('change', function() { _drawEquityWithBenchmarks(result, config); });
            });
        }, 10);
    }

    function _drawEquityWithBenchmarks(result, config) {
        var benchmarks = [_benchmarkEquity(config)]; // index 0 = buy & hold (always)
        var benchColors = ['rgba(255,255,255,0.25)'];
        var _qc = (window.CustomIndicators && window.CustomIndicators.getQuantCache) ? window.CustomIndicators.getQuantCache() : {};
        var _crossAssets = [
            { id: 'bt-bench-btc', key: 'BTC', color: '#F7931A' },
            { id: 'bt-bench-spx', key: 'SPX', color: '#4CAF50' },
            { id: 'bt-bench-gold', key: 'GOLD_F', color: '#FFD700' }
        ];
        _crossAssets.forEach(function(ca) {
            var cb = document.getElementById(ca.id);
            if (cb && cb.checked && _qc[ca.key]) {
                var closes = _qc[ca.key];
                var start = config.startBar || 0;
                var end = config.endBar != null ? config.endBar : closes.length - 1;
                var capital = config.initialCapital || 10000;
                if (start < closes.length && closes[start] && !isNaN(closes[start]) && closes[start] > 0) {
                    var units = capital / closes[start];
                    var eq = new Array(closes.length);
                    for (var i = 0; i < closes.length; i++) {
                        var v = closes[i];
                        if (isNaN(v) || v == null) { eq[i] = i > 0 ? eq[i-1] : capital; continue; }
                        if (i < start) eq[i] = capital;
                        else if (i > end) eq[i] = units * closes[end];
                        else eq[i] = units * v;
                    }
                    benchmarks.push(eq);
                    benchColors.push(ca.color);
                }
            }
        });
        drawEquityCurve(result.equity, benchmarks, benchColors);
    }

    // ── Buy & Hold Benchmark ──────────────────────────────────────────────

    function _benchmarkEquity(config) {
        if (!_lastOhlcv) return null;
        var closes = _lastOhlcv.close;
        var start = config.startBar || 0;
        var end = config.endBar != null ? config.endBar : closes.length - 1;
        var capital = config.initialCapital || 10000;
        if (start >= closes.length || closes[start] <= 0) return null;
        var units = capital / closes[start];
        var eq = new Array(closes.length);
        for (var i = 0; i < closes.length; i++) {
            if (i < start) eq[i] = capital;
            else if (i > end) eq[i] = units * closes[end];
            else eq[i] = units * closes[i];
        }
        return eq;
    }

    function _benchmarkReturn(config) {
        if (!_lastOhlcv) return '0.00';
        var closes = _lastOhlcv.close;
        var start = config.startBar || 0;
        var end = config.endBar != null ? config.endBar : closes.length - 1;
        if (start >= closes.length || closes[start] <= 0) return '0.00';
        var ret = ((closes[end] - closes[start]) / closes[start]) * 100;
        return (ret >= 0 ? '+' : '') + ret.toFixed(2);
    }

    // ── Equity Curve Canvas ───────────────────────────────────────────────

    var _eqChart = null; // stored for crosshair redraws

    function drawEquityCurve(equity, benchmarks, benchColors) {
        // benchmarks: array of equity arrays (first = buy & hold, rest = cross-asset)
        // benchColors: array of color strings matching benchmarks
        if (!benchmarks) benchmarks = [];
        if (!benchColors) benchColors = [];
        var canvas = document.getElementById('bt-equity-canvas');
        if (!canvas) return;
        var w = canvas.parentElement.offsetWidth;
        canvas.width = w;
        var h = canvas.height;
        var pad = { top: 4, bottom: 16, left: 0, right: 0 };
        var plotH = h - pad.top - pad.bottom;

        // Compute min/max across strategy + all benchmarks
        var min = Infinity, max = -Infinity;
        for (var i = 0; i < equity.length; i++) {
            if (!isNaN(equity[i])) {
                if (equity[i] < min) min = equity[i];
                if (equity[i] > max) max = equity[i];
            }
        }
        for (var bi = 0; bi < benchmarks.length; bi++) {
            var bm = benchmarks[bi];
            if (!bm) continue;
            for (var i = 0; i < bm.length; i++) {
                if (!isNaN(bm[i])) {
                    if (bm[i] < min) min = bm[i];
                    if (bm[i] > max) max = bm[i];
                }
            }
        }
        var range = max - min || 1;
        var n = equity.length;
        var startVal = NaN, endVal = NaN;
        for (var i = 0; i < n; i++) { if (!isNaN(equity[i])) { startVal = equity[i]; break; } }
        for (var i = n - 1; i >= 0; i--) { if (!isNaN(equity[i])) { endVal = equity[i]; break; } }
        var lineColor = endVal >= startVal ? '#26a69a' : '#ef5350';

        var dataList = (typeof klineChart !== 'undefined' && klineChart) ? klineChart.getDataList() : null;

        _eqChart = { canvas: canvas, equity: equity, benchmarks: benchmarks, benchColors: benchColors, min: min, max: max, range: range, n: n, w: w, h: h, pad: pad, plotH: plotH, lineColor: lineColor, dataList: dataList };

        // Initial render
        _renderEqCurve();

        // Wire crosshair events (only once)
        if (!canvas._btWired) {
            canvas._btWired = true;
            canvas.addEventListener('mousemove', _eqMouseMove);
            canvas.addEventListener('mouseleave', _eqMouseLeave);
        }
    }

    function _xToBar(x) {
        if (!_eqChart) return 0;
        return Math.round((x / _eqChart.w) * (_eqChart.n - 1));
    }

    function _barToX(bar) {
        if (!_eqChart) return 0;
        return (bar / (_eqChart.n - 1)) * _eqChart.w;
    }

    function _valToY(v) {
        if (!_eqChart) return 0;
        var c = _eqChart;
        return c.pad.top + c.plotH - ((v - c.min) / c.range) * c.plotH;
    }

    function _renderEqCurve(crosshairX) {
        var c = _eqChart;
        if (!c) return;
        var canvas = c.canvas, ctx = canvas.getContext('2d');
        var w = c.w, h = c.h, n = c.n, equity = c.equity;
        var benchmarks = c.benchmarks || [];
        var benchColors = c.benchColors || [];

        ctx.clearRect(0, 0, w, h);

        // Strategy gradient fill
        var grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, c.lineColor + '25');
        grad.addColorStop(1, c.lineColor + '03');
        ctx.beginPath();
        ctx.moveTo(0, h);
        for (var i = 0; i < n; i++) {
            if (isNaN(equity[i])) continue;
            ctx.lineTo(_barToX(i), _valToY(equity[i]));
        }
        ctx.lineTo(w, h);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();

        // Strategy line
        ctx.beginPath();
        var started = false;
        for (var i = 0; i < n; i++) {
            if (isNaN(equity[i])) continue;
            var x = _barToX(i), y = _valToY(equity[i]);
            if (!started) { ctx.moveTo(x, y); started = true; }
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = c.lineColor;
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // Benchmark lines (dashed)
        for (var bi = 0; bi < benchmarks.length; bi++) {
            var bm = benchmarks[bi];
            if (!bm) continue;
            ctx.beginPath();
            ctx.setLineDash([4, 3]);
            started = false;
            for (var i = 0; i < bm.length; i++) {
                if (isNaN(bm[i])) continue;
                var x = _barToX(i), y = _valToY(bm[i]);
                if (!started) { ctx.moveTo(x, y); started = true; }
                else ctx.lineTo(x, y);
            }
            ctx.strokeStyle = benchColors[bi] || '#585b66';
            ctx.lineWidth = 1;
            ctx.stroke();
            ctx.setLineDash([]);
        }

        // Date X-axis labels
        var dl = c.dataList;
        if (dl && dl.length >= n) {
            ctx.font = '9px monospace';
            ctx.fillStyle = '#484f58';
            ctx.textAlign = 'center';
            var labelCount = Math.min(6, Math.floor(w / 90));
            for (var j = 0; j < labelCount; j++) {
                var barIdx = Math.round((j / (labelCount - 1)) * (n - 1));
                var bar = dl[barIdx];
                if (!bar) continue;
                var d = new Date(bar.timestamp);
                var lbl = (d.getMonth() + 1) + '/' + d.getDate() + '/' + (d.getFullYear() % 100);
                ctx.fillText(lbl, _barToX(barIdx), h - 2);
            }
        }

        // Y-axis labels
        ctx.font = '10px monospace';
        ctx.fillStyle = '#8b949e';
        ctx.textAlign = 'left';
        ctx.fillText(fmtMoney(c.max), 4, c.pad.top + 10);
        ctx.fillText(fmtMoney(c.min), 4, c.pad.top + c.plotH - 2);

        // Crosshair
        if (crosshairX != null) {
            var bar = Math.max(0, Math.min(n - 1, _xToBar(crosshairX)));
            var eqVal = equity[bar];
            var bhVal = (benchmarks && benchmarks[0]) ? benchmarks[0][bar] : NaN;

            // Vertical line
            var cx = _barToX(bar);
            ctx.beginPath();
            ctx.moveTo(cx, c.pad.top);
            ctx.lineTo(cx, c.pad.top + c.plotH);
            ctx.strokeStyle = 'rgba(255,255,255,0.2)';
            ctx.lineWidth = 1;
            ctx.stroke();

            // Dots
            if (!isNaN(eqVal)) {
                ctx.beginPath();
                ctx.arc(cx, _valToY(eqVal), 3, 0, Math.PI * 2);
                ctx.fillStyle = c.lineColor;
                ctx.fill();
            }
            if (!isNaN(bhVal)) {
                ctx.beginPath();
                ctx.arc(cx, _valToY(bhVal), 3, 0, Math.PI * 2);
                ctx.fillStyle = '#585b66';
                ctx.fill();
            }

            // Tooltip
            var date = '';
            if (dl && dl[bar]) {
                var d = new Date(dl[bar].timestamp);
                date = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + '  ';
            }
            var tip = date + 'Strategy: ' + (!isNaN(eqVal) ? fmtMoney(eqVal) : '—');
            if (!isNaN(bhVal)) tip += '  B&H: ' + fmtMoney(bhVal);

            ctx.font = '10px monospace';
            var tipW = ctx.measureText(tip).width + 10;
            var tipX = cx + 8;
            if (tipX + tipW > w) tipX = cx - tipW - 4;
            ctx.fillStyle = 'rgba(13,17,23,0.9)';
            ctx.fillRect(tipX, c.pad.top, tipW, 16);
            ctx.fillStyle = '#d1d4dc';
            ctx.textAlign = 'left';
            ctx.fillText(tip, tipX + 5, c.pad.top + 12);
        }
    }

    function _eqMouseMove(e) {
        if (!_eqChart) return;
        var rect = _eqChart.canvas.getBoundingClientRect();
        var x = e.clientX - rect.left;
        _renderEqCurve(x);
    }

    function _eqMouseLeave() {
        _renderEqCurve(); // no crosshair
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    function statCard(label, value, cls) {
        return '<div class="bt-stat ' + cls + '"><div class="bt-stat-val">' + value + '</div><div class="bt-stat-label">' + label + '</div></div>';
    }

    function fmtMoney(v) {
        if (v === 0) return '$0';
        var abs = Math.abs(v);
        var str = abs >= 1000 ? abs.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : abs.toFixed(2);
        return (v < 0 ? '-$' : '$') + str;
    }

    function fmtPrice(v) {
        if (v >= 1000) return v.toFixed(0);
        if (v >= 1) return v.toFixed(2);
        return v.toPrecision(4);
    }


    // Hold-N flip detector (client-side mirror of MCP backtest_indicator_formula):
    // for each False→True transition of `formulaStr`, hold N bars, record forward return.
    // Returns null when formula fails or no flips found.
    function _computeHoldN(formulaStr, ohlcv, hold) {
        if (!ohlcv || !ohlcv.close || !ohlcv.close.length) return null;
        var FP = window.FormulaParser;
        if (!FP) return null;
        var h = Math.max(1, Math.min(60, hold || 5));
        var res = FP.evaluate(formulaStr, ohlcv);
        if (res.error || !res.values) return null;
        var sig = res.values;
        var closes = ohlcv.close;
        var n = closes.length;
        var flips = 0, wins = 0;
        var rets = [];
        var prev = false;
        for (var i = 0; i < n - h; i++) {
            var v = sig[i];
            var cur = v && !isNaN(v) && v !== 0;
            if (cur && !prev) {
                var entryPx = closes[i], exitPx = closes[i + h];
                if (entryPx > 0 && exitPx > 0 && !isNaN(entryPx) && !isNaN(exitPx)) {
                    flips++;
                    var r = (exitPx - entryPx) / entryPx * 100;
                    rets.push(r);
                    if (r > 0) wins++;
                }
            }
            prev = cur;
        }
        if (!flips) return null;
        rets.sort(function(a, b) { return a - b; });
        var median = rets.length % 2 ? rets[(rets.length - 1) >> 1] : (rets[rets.length / 2 - 1] + rets[rets.length / 2]) / 2;
        var mean = rets.reduce(function(a, b) { return a + b; }, 0) / rets.length;
        return { flips: flips, hold: h, hitRate: wins / flips, mean: mean, median: median };
    }

    // ── Public API ────────────────────────────────────────────────────────

    window.BacktestUI = {
        render: renderBacktestPanel,
        run: runBacktest,
        clearMarkers: clearMarkers
    };
})();
