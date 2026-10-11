/* Alert Panel — now integrated into bottom drawer */
// =============================================================================
// Alert System (server-side alerts via /api/alerts)
// =============================================================================
var klineAlertClickMode = false;
var _editingAlertId = null; // Track which alert is being edited
var _alertFormType = 'price'; // 'price' or 'indicator'

// Indicator target states — must mirror INDICATOR_ALERT_CONDITIONS in
// carrac/indicator_state.py. Only indicators in TRACKED_STATES can fire.
var _CPR_ZONES = [
    'below_s3', 's2_s3', 's1_s2', 'bc_s1',
    'below_pivot', 'above_pivot',
    'tc_r1', 'r1_r2', 'r2_r3', 'above_r3',
];
var _indicatorTargetStates = {
    ema_ribbon: ['bullish', 'bearish', 'neutral'],
    rsi_zone: ['oversold', 'overbought', 'neutral'],
    bb_squeeze: ['squeezed', 'normal', 'expanded'],
    bb_position: ['inside', 'above_upper', 'below_lower'],
    consensio: ['full_bull', 'strong_bull', 'bull', 'neutral', 'bear', 'strong_bear'],
    structure: ['bullish', 'bearish', 'neutral'],
    macd: ['bullish', 'bearish', 'flat'],
    adx_regime: ['ranging', 'trend_forming', 'strong_trend'],
    cpr_position: _CPR_ZONES,
    weekly_cpr_position: _CPR_ZONES,
    daily_cpr_position: _CPR_ZONES,
    super_trend: ['bullish', 'bearish'],
    psar: ['bullish', 'bearish'],
    // Tier 2 — extended indicators (cockpit_extended)
    ichimoku: ['above_cloud', 'inside_cloud', 'below_cloud'],
    ichimoku_tk: ['bullish', 'bearish', 'none'],
    ttm_squeeze: ['squeeze_on', 'squeeze_off', 'firing_up', 'firing_down'],
    alligator: ['eating_bullish', 'eating_bearish', 'sleeping', 'mixed'],
    ema_cross: ['bullish', 'bearish'],
    donchian: ['at_high', 'at_low', 'inside'],
    stoch_rsi: ['oversold', 'overbought', 'neutral'],
};

// Display labels for indicators (used in dropdowns) and individual states.
var _indicatorLabels = {
    ema_ribbon: 'EMA Ribbon',
    rsi_zone: 'Weekly RSI',
    bb_squeeze: 'BB Squeeze',
    bb_position: 'BB Position',
    consensio: 'Consensio',
    structure: 'Market Structure',
    macd: 'MACD',
    adx_regime: 'ADX Regime',
    cpr_position: 'Monthly CPR',
    weekly_cpr_position: 'Weekly CPR',
    daily_cpr_position: 'Daily CPR',
    super_trend: 'SuperTrend',
    psar: 'Parabolic SAR',
    ichimoku: 'Ichimoku Cloud',
    ichimoku_tk: 'Ichimoku TK Cross',
    ttm_squeeze: 'TTM Squeeze',
    alligator: 'Alligator',
    ema_cross: 'EMA Cross (9/21)',
    donchian: 'Donchian Channel',
    stoch_rsi: 'Stoch RSI',
};

// Pretty labels for individual state values. Falls back to title-cased
// snake_case for anything not listed here.
var _stateLabels = {
    // CPR zones
    below_s3: 'Below S3', s2_s3: 'S2 – S3', s1_s2: 'S1 – S2', bc_s1: 'BC – S1',
    below_pivot: 'Below Pivot', above_pivot: 'Above Pivot',
    tc_r1: 'TC – R1', r1_r2: 'R1 – R2', r2_r3: 'R2 – R3', above_r3: 'Above R3',
    // BB Position
    above_upper: 'Above Upper Band', below_lower: 'Below Lower Band', inside: 'Inside Bands',
    // Common
    above_cloud: 'Above Cloud', inside_cloud: 'Inside Cloud', below_cloud: 'Below Cloud',
    squeeze_on: 'Squeeze On', squeeze_off: 'Squeeze Off',
    firing_up: 'Firing Up', firing_down: 'Firing Down',
    eating_bullish: 'Eating Bullish', eating_bearish: 'Eating Bearish',
    at_high: 'At High', at_low: 'At Low',
    trend_forming: 'Trend Forming', strong_trend: 'Strong Trend',
    above_zero: 'Above Zero', below_zero: 'Below Zero',
};

function _formatStateLabel(s) {
    // Numeric states (e.g. consensio 0..9) → "n / 9"
    if (typeof s === 'number') return s + ' / 9';
    if (typeof s === 'string' && /^\d+$/.test(s)) return s + ' / 9';
    if (_stateLabels[s]) return _stateLabels[s];
    // Title-case snake_case fallback
    return String(s).split('_').map(function(w) {
        return w.charAt(0).toUpperCase() + w.slice(1);
    }).join(' ');
}

function initAlertTypeTabs() {
    var tabs = document.querySelectorAll('#alert-type-tabs .alert-type-tab');
    tabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
            _alertFormType = this.dataset.alertType;
            tabs.forEach(function(t) { t.classList.toggle('active', t.dataset.alertType === _alertFormType); });
            var priceFields = document.getElementById('alert-price-fields');
            var indFields = document.getElementById('alert-indicator-fields');
            var formulaFields = document.getElementById('alert-formula-fields');
            if (priceFields) priceFields.style.display = _alertFormType === 'price' ? '' : 'none';
            if (indFields) indFields.style.display = _alertFormType === 'indicator' ? '' : 'none';
            if (formulaFields) formulaFields.style.display = _alertFormType === 'formula' ? '' : 'none';
        });
    });

    // Wire indicator select → populate target states
    var indSelect = document.getElementById('alert-indicator-select');
    if (indSelect) {
        indSelect.addEventListener('change', function() {
            var targetSelect = document.getElementById('alert-target-select');
            if (!targetSelect) return;
            var states = _indicatorTargetStates[this.value] || [];
            targetSelect.innerHTML = '<option value="">Select target state...</option>';
            states.forEach(function(s) {
                targetSelect.innerHTML += '<option value="' + s + '">' + _formatStateLabel(s) + '</option>';
            });
        });
    }
}

// ── Pre-fill alert from play condition ──

var _pendingAlertPrefill = null;

// Map Plays condition indicator keys → alert indicator keys.
// Plays uses uppercase descriptive names; alerts use the snake_case
// keys from INDICATOR_ALERT_CONDITIONS.
var _playsToAlertIndicator = {
    EMA_RIBBON: 'ema_ribbon',
    RSI: 'rsi_zone',
    BB_SQUEEZE: 'bb_squeeze',
    CONSENSIO: 'consensio',
    STRUCTURE: 'structure',
    MACD: 'macd',
    ADX: 'adx_regime',
    CPR: 'cpr_position',
    WEEKLY_CPR: 'weekly_cpr_position',
    DAILY_CPR: 'daily_cpr_position',
    SUPER_TREND: 'super_trend',
    PSAR: 'psar',
    ICHIMOKU: 'ichimoku',
    TTM_SQUEEZE: 'ttm_squeeze',
    ALLIGATOR: 'alligator',
    EMA_CROSS: 'ema_cross',
    DONCHIAN: 'donchian',
};

function prefillAlertFromCondition(conditionRule, symbol, playTitle) {
    var t = conditionRule.type;
    var p = conditionRule.params || conditionRule;
    var prefill = { note: 'From play: ' + (playTitle || ''), symbol: symbol };

    if (t === 'price_level' || t === 'candle_close') {
        prefill.type = 'price';
        prefill.price = p.level;
        prefill.condition = p.relation || 'above';
    } else if (t === 'price_zone') {
        // Just use the high price for alert
        prefill.type = 'price';
        prefill.price = p.high || p.low;
        prefill.condition = 'above';
    } else if (t === 'indicator_state') {
        prefill.type = 'indicator';
        var rawInd = (p.indicator || '').toUpperCase();
        prefill.indicator = _playsToAlertIndicator[rawInd] || rawInd.toLowerCase();
        prefill.target_state = p.state;  // user picks if mismatched
    } else if (t === 'indicator_crosses') {
        prefill.type = 'indicator';
        var crossInd = (p.indicator || '').toUpperCase();
        prefill.indicator = _playsToAlertIndicator[crossInd] || crossInd.toLowerCase();
        prefill.target_state = p.direction === 'above' ? 'bullish' : 'bearish';
    } else if (t === 'consensio_threshold') {
        // Consensio target is the position count 0..9. If the play uses a
        // non-integer threshold we leave it blank for the user to pick.
        prefill.type = 'indicator';
        prefill.indicator = 'consensio';
        var thresh = p.min_value;
        prefill.target_state = (typeof thresh === 'number' && thresh >= 0 && thresh <= 9 && Number.isInteger(thresh))
            ? thresh
            : '';
    } else if (t === 'formula_expression') {
        prefill.type = 'formula';
        prefill.expression = p.expression || '';
        prefill.timeframe = p.timeframe || '1D';
    } else if (t === 'cross_asset') {
        prefill.type = 'price';
        prefill.symbol = (p.asset || '').toUpperCase();
        prefill.price = p.level || 0;
        prefill.condition = p.relation || 'above';
    } else {
        return; // not alertable
    }

    _pendingAlertPrefill = prefill;

    // Open alert drawer and switch to correct tab
    if (typeof switchDrawerTab === 'function') switchDrawerTab('alerts');
    setTimeout(function() { _applyAlertPrefill(); }, 300);
}

function _applyAlertPrefill() {
    if (!_pendingAlertPrefill) return;
    var pf = _pendingAlertPrefill;
    _pendingAlertPrefill = null;

    // Switch to correct tab
    _alertFormType = pf.type || 'price';
    var tabs = document.querySelectorAll('#alert-type-tabs .alert-type-tab');
    tabs.forEach(function(t) { t.classList.toggle('active', t.dataset.alertType === _alertFormType); });
    var priceFields = document.getElementById('alert-price-fields');
    var indFields = document.getElementById('alert-indicator-fields');
    var formulaFields = document.getElementById('alert-formula-fields');
    if (priceFields) priceFields.style.display = _alertFormType === 'price' ? '' : 'none';
    if (indFields) indFields.style.display = _alertFormType === 'indicator' ? '' : 'none';
    if (formulaFields) formulaFields.style.display = _alertFormType === 'formula' ? '' : 'none';

    if (_alertFormType === 'price') {
        var priceInput = document.getElementById('alert-price-input');
        if (priceInput && pf.price) priceInput.value = pf.price;
    } else if (_alertFormType === 'indicator') {
        var indSel = document.getElementById('alert-indicator-select');
        if (indSel && pf.indicator) {
            indSel.value = pf.indicator;
            indSel.dispatchEvent(new Event('change'));
            setTimeout(function() {
                var tgtSel = document.getElementById('alert-target-select');
                if (tgtSel && pf.target_state) tgtSel.value = pf.target_state;
            }, 50);
        }
    } else if (_alertFormType === 'formula') {
        var exprInput = document.getElementById('alert-formula-input');
        if (exprInput && pf.expression) exprInput.value = pf.expression;
        var tfSel = document.getElementById('alert-formula-tf');
        if (tfSel && pf.timeframe) tfSel.value = pf.timeframe;
    }

    var noteInput = document.getElementById('alert-note-input');
    if (noteInput && pf.note) noteInput.value = pf.note;

    // Expand form if collapsed
    var form = document.querySelector('.alert-form-inline');
    if (form) form.style.display = '';
    if (typeof showToast === 'function') showToast('Alert pre-filled \u2014 review and save');
}

function createServerAlert() {
    if (_alertFormType === 'indicator') {
        _createIndicatorAlert();
        return;
    }
    if (_alertFormType === 'formula') {
        _createFormulaAlert();
        return;
    }

    var priceInput = document.getElementById('alert-price-input');
    var noteInput = document.getElementById('alert-note-input');
    var freqSelect = document.getElementById('alert-freq-select');
    if (!priceInput || !priceInput.value) return;
    var price = parseFloat(priceInput.value);
    if (isNaN(price) || price <= 0) return;

    // Auto-detect direction: if alert price > current, wait for price to rise (above); else below
    var currentPrice = allAssets[klineChartSymbol] ? allAssets[klineChartSymbol].latest_price : 0;
    var autoDir = (price >= currentPrice) ? 'above' : 'below';
    var body = {
        symbol: klineChartSymbol,
        condition: autoDir,
        price: price,
        note: noteInput ? noteInput.value.trim() : '',
        frequency: freqSelect ? freqSelect.value : 'once_per_bar'
    };

    fetch('/api/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        credentials: 'same-origin',
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
        if (res.data.no_channel) {
            if (confirm('You need to set up at least one notification channel before creating alerts.\n\nGo to notification settings now?')) {
                window.location.href = '/settings#notifications';
            }
            return;
        }
        // 403 with limit_type → upgrade modal instead of a bare alert().
        if (res.status === 403 && typeof handleLimitHitResponse === 'function' && handleLimitHitResponse(res.data, 'alerts_limit')) return;
        if (!res.ok && res.data.error) {
            alert(res.data.error);
            return;
        }
        if (res.data.alert || res.data.id) {
            priceInput.value = '';
            if (noteInput) noteInput.value = '';
            setTimeout(function() {
                updateDrawerAlerts();
                renderAlertOverlays();
            }, 200);
        }
    })
    .catch(function(e) {
        console.error('Failed to create alert:', e);
        if (typeof showToast === 'function') showToast('Could not create alert — check your connection and try again.');
    });
}

function _getBBParams() {
    // Snapshot the current BOLL params from the chart config.
    // Falls back to defaults (period=20, std_dev=2.0, offset=0) if not available.
    var defaults = { period: 20, std_dev: 2.0, offset: 0 };
    try {
        var cfg = window.indicatorPackConfig && window.indicatorPackConfig.bollingerBands;
        if (!cfg) return defaults;
        return {
            period: cfg.length || 20,
            std_dev: cfg.mult || 2.0,
            // optional: older saved configs have no offset
            offset: cfg.offset || 0,
        };
    } catch (e) {
        return defaults;
    }
}

function _createIndicatorAlert() {
    var indSelect = document.getElementById('alert-indicator-select');
    var targetSelect = document.getElementById('alert-target-select');
    var noteInput = document.getElementById('alert-note-input');
    var freqSelect = document.getElementById('alert-freq-select');

    var indicator = indSelect ? indSelect.value : '';
    var targetState = targetSelect ? targetSelect.value : '';
    if (!indicator || !targetState) return;

    var ts = targetState;

    var body = {
        symbol: klineChartSymbol,
        indicator: indicator,
        target_state: ts,
        note: noteInput ? noteInput.value.trim() : '',
        frequency: freqSelect ? freqSelect.value : 'once',
    };

    // Snapshot BOLL params at creation time for bb_position alerts.
    if (indicator === 'bb_position') {
        body.bb_params = _getBBParams();
    }

    fetch('/api/alerts/indicator', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        credentials: 'same-origin',
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
        if (res.data.no_channel) {
            if (confirm('You need to set up at least one notification channel before creating alerts.\n\nGo to notification settings now?')) {
                window.location.href = '/settings#notifications';
            }
            return;
        }
        // 403 with limit_type → upgrade modal.
        if (res.status === 403 && typeof handleLimitHitResponse === 'function' && handleLimitHitResponse(res.data, 'alerts_indicator_limit')) return;
        if (!res.ok && res.data.error) {
            alert(res.data.error);
            return;
        }
        if (res.data.alert || res.data.id) {
            if (indSelect) indSelect.value = '';
            if (targetSelect) targetSelect.innerHTML = '<option value="">Select target state...</option>';
            if (noteInput) noteInput.value = '';
            setTimeout(function() { updateDrawerAlerts(); }, 200);
        }
    })
    .catch(function(e) {
        console.error('Failed to create indicator alert:', e);
        if (typeof showToast === 'function') showToast('Could not create alert — check your connection and try again.');
    });
}

function _createFormulaAlert() {
    var exprInput = document.getElementById('alert-formula-input');
    var tfSelect = document.getElementById('alert-formula-tf');
    var noteInput = document.getElementById('alert-note-input');
    var freqSelect = document.getElementById('alert-freq-select');

    var expression = exprInput ? exprInput.value.trim() : '';
    if (!expression) return;

    var body = {
        symbol: klineChartSymbol,
        expression: expression,
        timeframe: tfSelect ? tfSelect.value : '1D',
        note: noteInput ? noteInput.value.trim() : '',
        frequency: freqSelect ? freqSelect.value : 'once',
    };

    fetch('/api/alerts/formula', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        credentials: 'same-origin',
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
        if (res.data.no_channel) {
            if (confirm('You need to set up at least one notification channel before creating alerts.\n\nGo to notification settings now?')) {
                window.location.href = '/settings#notifications';
            }
            return;
        }
        // 403 with limit_type → upgrade modal.
        if (res.status === 403 && typeof handleLimitHitResponse === 'function' && handleLimitHitResponse(res.data, 'alerts_formula_limit')) return;
        if (!res.ok && res.data.error) {
            alert(res.data.error);
            return;
        }
        if (res.data.alert || res.data.id) {
            if (exprInput) exprInput.value = '';
            if (noteInput) noteInput.value = '';
            setTimeout(function() { updateDrawerAlerts(); }, 200);
        }
    })
    .catch(function(e) {
        console.error('Failed to create formula alert:', e);
        if (typeof showToast === 'function') showToast('Could not create alert — check your connection and try again.');
    });
}

function editServerAlert(alert) {
    // Auto-expand form if collapsed
    if (!_alertFormExpanded) {
        _editingAlertId = alert.id;
        _alertFormExpanded = true;
        updateDrawerAlerts();
        return; // updateDrawerAlerts will restore edit state via savedEditId
    }
    // Populate form with alert's current values
    var priceInput = document.getElementById('alert-price-input');
    var noteInput = document.getElementById('alert-note-input');
    var freqSelect = document.getElementById('alert-freq-select');
    var createBtn = document.getElementById('alert-create-btn');
    var editRow = document.getElementById('alert-edit-row');
    if (priceInput) priceInput.value = alert.price;
    if (noteInput) noteInput.value = alert.note || '';
    if (freqSelect) freqSelect.value = alert.frequency || 'once_per_bar';
    _editingAlertId = alert.id;
    if (createBtn) createBtn.style.display = 'none';
    if (editRow) editRow.classList.add('visible');
    // Highlight the item being edited
    document.querySelectorAll('.alert-list-item').forEach(function(el) {
        el.classList.remove('alert-editing');
    });
    var editItem = document.querySelector('.alert-list-item[data-alert-id="' + alert.id + '"]');
    if (editItem) editItem.classList.add('alert-editing');
    if (priceInput) priceInput.focus();
}

function cancelEditAlert() {
    _editingAlertId = null;
    var priceInput = document.getElementById('alert-price-input');
    var noteInput = document.getElementById('alert-note-input');
    var freqSelect = document.getElementById('alert-freq-select');
    var createBtn = document.getElementById('alert-create-btn');
    var editRow = document.getElementById('alert-edit-row');
    if (priceInput) priceInput.value = '';
    if (noteInput) noteInput.value = '';
    if (freqSelect) freqSelect.value = 'once_per_bar';
    if (createBtn) createBtn.style.display = '';
    if (editRow) editRow.classList.remove('visible');
    document.querySelectorAll('.alert-list-item').forEach(function(el) {
        el.classList.remove('alert-editing');
    });
}

function saveOrCreateAlert() {
    if (_editingAlertId) {
        var priceInput = document.getElementById('alert-price-input');
        var noteInput = document.getElementById('alert-note-input');
        var freqSelect = document.getElementById('alert-freq-select');
        var price = parseFloat(priceInput ? priceInput.value : '');
        if (isNaN(price) || price <= 0) return;
        var currentPrice = allAssets[klineChartSymbol] ? allAssets[klineChartSymbol].latest_price : 0;
        var body = {
            price: price,
            condition: (price >= currentPrice) ? 'above' : 'below',
            note: noteInput ? noteInput.value.trim() : '',
            frequency: freqSelect ? freqSelect.value : 'once_per_bar'
        };
        var editId = _editingAlertId;
        fetch('/api/alerts/' + editId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            credentials: 'same-origin',
            body: JSON.stringify(body)
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.alert) {
                // Also reactivate if it was triggered
                if (data.alert.triggered) {
                    return fetch('/api/alerts/' + editId + '/reactivate', {
                        method: 'POST',
                        headers: { 'X-CSRFToken': getCSRFToken() },
                        credentials: 'same-origin'
                    }).then(function() { return data; });
                }
                return data;
            }
        })
        .then(function() {
            _editingAlertId = null;
            if (priceInput) priceInput.value = '';
            if (noteInput) noteInput.value = '';
            if (freqSelect) freqSelect.value = 'once_per_bar';
            setTimeout(function() {
                updateDrawerAlerts();
                renderAlertOverlays();
            }, 200);
        })
        .catch(function(e) {
            console.error('Failed to update alert:', e);
            if (typeof showToast === 'function') showToast('Could not update alert — check your connection and try again.');
        });
    } else {
        createServerAlert();
    }
}

function deleteServerAlert(alertId) {
    fetch('/api/alerts/' + alertId, {
        method: 'DELETE',
        headers: { 'X-CSRFToken': getCSRFToken() },
        credentials: 'same-origin'
    })
    .then(function(r) { return r.json(); })
    .then(function() {
        updateDrawerAlerts();
        renderAlertOverlays();
    })
    .catch(function(e) {
        console.error('Failed to delete alert:', e);
        if (typeof showToast === 'function') showToast('Could not delete alert — check your connection and try again.');
    });
}

function reactivateServerAlert(alertId) {
    fetch('/api/alerts/' + alertId + '/reactivate', {
        method: 'POST',
        headers: { 'X-CSRFToken': getCSRFToken() },
        credentials: 'same-origin'
    })
    .then(function(r) { return r.json(); })
    .then(function() {
        updateDrawerAlerts();
        renderAlertOverlays();
    })
    .catch(function(e) {
        console.error('Failed to reactivate alert:', e);
        if (typeof showToast === 'function') showToast('Could not reactivate alert — check your connection and try again.');
    });
}

function updateAlertBadge(count) {
    // Badge is now the drawer tab label — no-op, handled by updateDrawerAlerts
}

var _alertOverlayIds = [];
function renderAlertOverlays() {
    if (!klineChart) return;
    // Remove previously auto-rendered alert overlays (each has a unique ID)
    _alertOverlayIds.forEach(function(id) {
        try { klineChart.removeOverlay(id); } catch(e) {}
    });
    _alertOverlayIds = [];
    // T4.3: route through dedupFetch so this call and the concurrent one in
    // updateDrawerAlerts (scenarios.js) share a single GET /api/alerts?active=true
    // per asset-switch window. dedupFetch (chart-loading.js) is a global.
    var _alertsFetch = (typeof dedupFetch === 'function')
        ? dedupFetch('/api/alerts?active=true', { credentials: 'same-origin' }, 2000)
        : fetch('/api/alerts?active=true', { credentials: 'same-origin' });
    _alertsFetch
    .then(function(r) { return r.json(); })
    .then(function(data) {
        // Build set of prices that already have user-drawn alertLine overlays in localStorage
        var drawnPrices = {};
        try {
            var ld = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            (ld[klineChartSymbol] || []).forEach(function(d) {
                if (d.name === 'alertLine' && d.points && d.points.length > 0) {
                    // Round to 6 decimal places to avoid float comparison issues
                    drawnPrices[Math.round(d.points[0].value * 1e6)] = true;
                }
            });
        } catch(e) {}
        // Skip alerts that have a user-drawn alertLine — check both [drawn] note AND localStorage match
        var alerts = (data.alerts || []).filter(function(a) {
            if (a.symbol !== klineChartSymbol || a.triggered) return false;
            if ((a.note || '').indexOf('[drawn]') !== -1) return false;
            // Also skip if an alertLine drawing exists at this price in localStorage
            if (drawnPrices[Math.round(a.price * 1e6)]) return false;
            return true;
        });
        var allData = klineChart ? klineChart.getDataList() : [];
        if (!allData || allData.length === 0) return;
        // Span the right ~20% of chart data for the alert line
        var len = allData.length;
        var startIdx = Math.max(0, Math.floor(len * 0.8));
        var startTs = allData[startIdx].timestamp;
        var endTs = allData[len - 1].timestamp;
        alerts.forEach(function(a) {
            var color = '#ff9800';
            var overlayId = 'alert_ov_' + a.id;
            klineChart.createOverlay({
                id: overlayId,
                name: 'alertLine',
                lock: true,
                extendData: { _color: color },
                // Overlay points live in CHART space (ln under log scale) —
                // a.price is linear; without the conversion server-rendered
                // alert lines sat at the wrong height on log charts.
                points: [
                    { timestamp: startTs, value: (typeof carracToChartSpace === 'function') ? carracToChartSpace(a.price) : a.price },
                    { timestamp: endTs, value: (typeof carracToChartSpace === 'function') ? carracToChartSpace(a.price) : a.price }
                ],
                styles: {
                    line: { color: color, size: 1.5 },
                    point: { color: color, borderColor: color, borderSize: 1.5 }
                }
            });
            _alertOverlayIds.push(overlayId);
        });
    })
    // SILENCE-OK: re-fetched on every asset switch / alerts-drawer update via
    // the shared dedupFetch (2s window) above — a miss just means the alert
    // lines don't redraw this cycle; the next switch/update retries it, and
    // the alert itself is still armed server-side regardless.
    .catch(function() {});
}
