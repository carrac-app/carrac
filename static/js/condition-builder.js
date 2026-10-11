/* Condition Builder — UI for adding/editing/displaying conditions.
 *
 * Quick mode: inline pills in cards + "Add Condition" dropdown in form.
 * Power mode: full modal with all 17 types, test button, suggestions.
 * Supports both scenarios and plays via entityType parameter.
 */

// ── Condition type metadata ──

var CONDITION_CATEGORIES = [
    { key: 'price', label: 'Price', types: ['price_level', 'price_zone', 'price_change_pct', 'distance_from'] },
    { key: 'candle', label: 'Candle', types: ['candle_close', 'engulfing_detected', 'bb_breakout_retest_engulfing', 'bb_fade_engulfing', 'bb_squeeze_breakout'] },
    { key: 'indicator', label: 'Indicator', types: ['indicator_value', 'indicator_crosses', 'indicator_state', 'indicator_divergence', 'consensio_threshold', 'scanner_verdict'] },
    // 'trendline_break', 'level_touch', and 'correlation' are hidden:
    // the backend does not accept them (absent from conditions.py's
    // ALL_CONDITION_TYPES / _EVALUATORS; they were stubs that always
    // evaluated to False). The CONDITION_TYPE_META entries below are kept so any
    // previously saved play still renders a readable label.
    // { key: 'drawing', label: 'Drawing', types: ['trendline_break', 'level_touch'] },
    { key: 'cross_asset', label: 'Cross-Asset', types: ['cross_asset'] },
    { key: 'formula', label: 'Formula', types: ['formula_expression'] },
    { key: 'meta', label: 'Other', types: ['time_limit', 'manual'] },
];

var CONDITION_TYPE_META = {
    price_level:        { label: 'Price Level',       desc: 'Price above/below a level',     fields: ['level', 'relation'] },
    price_zone:         { label: 'Price Zone',        desc: 'Price inside/outside a zone',    fields: ['low', 'high', 'relation'] },
    price_change_pct:   { label: 'Price Change %',    desc: 'Price change over period',       fields: ['period', 'direction', 'threshold_pct'] },
    distance_from:      { label: 'Distance From',     desc: 'Price within % of reference',    fields: ['reference', 'threshold_pct'] },
    candle_close:       { label: 'Candle Close',      desc: 'Close above/below level',        fields: ['timeframe', 'level', 'relation'] },
    engulfing_detected: { label: 'Engulfing Candle',  desc: 'Bullish/bearish engulfing (1h or 4h)', fields: ['direction', 'timeframe'] },
    bb_breakout_retest_engulfing: { label: 'BB Breakout-Retest', desc: 'BB breakout → retest middle → engulfing confirmation', fields: ['direction', 'timeframe', 'lookback_bars', 'proximity_pct', 'require_volume'] },
    bb_fade_engulfing:            { label: 'BB Fade (engulfing)',  desc: 'Price extended past band + same-direction engulfing (fade/reversal)', fields: ['direction', 'timeframe'] },
    bb_squeeze_breakout:          { label: 'BB Squeeze Breakout', desc: 'BB squeeze released + directional close past band (TTM Squeeze flavor)', fields: ['direction', 'timeframe', 'lookback_bars', 'min_squeeze_bars', 'require_volume'] },
    bb_breakdown_continuation:    { label: 'BB Breakdown Continuation', desc: 'Band breakout within lookback → engulfing OR retest of the broken band (supports custom BB offset)', fields: ['direction', 'timeframe', 'lookback_bars', 'confirm_mode', 'retest_band', 'proximity_pct', 'bb_offset'] },
    indicator_value:    { label: 'Indicator Value',   desc: 'Indicator above/below threshold', fields: ['indicator', 'timeframe', 'operator', 'value'] },
    indicator_crosses:  { label: 'Indicator Cross',   desc: 'Indicator crosses threshold',    fields: ['indicator', 'timeframe', 'direction', 'value'] },
    indicator_state:    { label: 'Indicator State',   desc: 'Indicator in specific state',    fields: ['indicator', 'timeframe', 'state'] },
    indicator_divergence: { label: 'Divergence',      desc: 'Bullish/bearish divergence',     fields: ['indicator', 'timeframe', 'type'] },
    consensio_threshold: { label: 'Consensio',        desc: 'Consensio positions threshold',  fields: ['min_value'] },
    scanner_verdict:    { label: 'Scanner Verdict',   desc: 'Scanner score threshold',        fields: ['direction', 'min_verdict'] },
    trendline_break:    { label: 'Trendline Break',   desc: 'Price breaks trendline',         fields: ['drawing_id', 'direction'] },
    level_touch:        { label: 'Level Touch',       desc: 'Price touches drawn level',      fields: ['drawing_id'] },
    cross_asset:        { label: 'Cross-Asset',       desc: 'Condition on another asset',     fields: ['asset', 'condition_type'] },
    correlation:        { label: 'Correlation',       desc: 'Asset correlation threshold',    fields: ['asset_a', 'asset_b', 'period', 'threshold'] },
    formula_expression: { label: 'Formula',            desc: 'Custom formula (true when > 0)',  fields: ['expression', 'timeframe'] },
    time_limit:         { label: 'Time Deadline',      desc: 'Evaluate or expire by a date',   fields: ['expires_at', 'time_action'] },
    manual:             { label: 'Manual Check',      desc: 'User manually verifies',         fields: ['label'] },
};

// INDICATOR_LIST is derived from the shared indicator registry at runtime so
// adding an indicator in one place (indicator-registry.js) automatically
// flows into the condition builder.
// _FALLBACK_INDICATOR_LIST is a safety net if the registry script hasn't
// loaded yet — covers the original 39 condition-builder indicators.
var _FALLBACK_INDICATOR_LIST = [
    { value: 'ADX', label: 'ADX' },
    { value: 'ALLIGATOR', label: 'Alligator' },
    { value: 'ALMA', label: 'ALMA' },
    { value: 'ATR_PCT', label: 'ATR %' },
    { value: 'AUTO_FIB', label: 'Auto Fibonacci' },
    { value: 'BB_POSITION', label: 'BB Position' },
    { value: 'BB_SQUEEZE', label: 'Bollinger Squeeze' },
    { value: 'BBW_PCT', label: 'BB Width %' },
    { value: 'BULL_BAND', label: 'Bull Market Band' },
    { value: 'CM_ULTIMATE', label: 'CM Ultimate' },
    { value: 'CONSENSIO', label: 'Consensio' },
    { value: 'CPR', label: 'Monthly CPR' },
    { value: 'CPR_HISTORY', label: 'Monthly CPR History' },
    { value: 'CVD_DIV', label: 'CVD Spot Divergence' },
    { value: 'DAILY_CPR', label: 'Daily CPR' },
    { value: 'DAILY_EMA', label: 'Daily EMAs' },
    { value: 'DONCHIAN', label: 'Donchian Channels' },
    { value: 'EMA_CROSS', label: 'EMA Cross' },
    { value: 'EMA_RIBBON', label: 'EMA Ribbon' },
    { value: 'FVG', label: 'Fair Value Gap' },
    { value: 'GAUSS_CHAN', label: 'Gaussian Channel' },
    { value: 'HULL_MA', label: 'Hull MA' },
    { value: 'ICHIMOKU', label: 'Ichimoku Cloud' },
    { value: 'ICHIMOKU_TK', label: 'Ichimoku TK Cross' },
    { value: 'KAMA', label: 'KAMA' },
    { value: 'KELTNER', label: 'Keltner Channels' },
    { value: 'MACD', label: 'MACD' },
    { value: 'NR7', label: 'NR7' },
    { value: 'OBV', label: 'OBV' },
    { value: 'PI_CYCLE', label: 'Pi Cycle Top' },
    { value: 'PSAR', label: 'Parabolic SAR' },
    { value: 'RSI', label: 'RSI' },
    { value: 'RSI_DIV', label: 'RSI Divergence' },
    { value: 'SMA_SIGNAL', label: 'SMA 20/200' },
    { value: 'SR_CHANNELS', label: 'S/R Channels' },
    { value: 'STOCHASTIC', label: 'Stochastic' },
    { value: 'SUPER_TREND', label: 'SuperTrend' },
    { value: 'TTM_SQUEEZE', label: 'TTM Squeeze' },
    { value: 'VOLUME', label: 'Volume' },
    { value: 'WEEKLY_CPR', label: 'Weekly CPR' },
    { value: 'WEEKLY_MA', label: 'Weekly MAs' },
];

function _getIndicatorList() {
    var reg = (typeof window !== 'undefined' && window.IndicatorRegistry) || null;
    if (!reg || typeof reg.getAllKeys !== 'function' || typeof reg.getIndicator !== 'function') {
        return _FALLBACK_INDICATOR_LIST;
    }
    try {
        var keys = reg.getAllKeys() || [];
        if (!keys.length) return _FALLBACK_INDICATOR_LIST;
        var list = keys.map(function (k) {
            var ind = reg.getIndicator(k) || {};
            return { value: k, label: ind.shortLabel || ind.label || k };
        });
        list.sort(function (a, b) { return a.label.localeCompare(b.label); });
        return list;
    } catch (e) {
        return _FALLBACK_INDICATOR_LIST;
    }
}

// NOTE: deliberately no `var INDICATOR_LIST = ...` — that would trigger
// CreateGlobalVarBinding(configurable:false) at parse time and make this
// defineProperty throw. Downstream code reads INDICATOR_LIST via the global
// scope chain through this getter.
Object.defineProperty(window, 'INDICATOR_LIST', {
    configurable: true,
    get: function () { return _getIndicatorList(); },
});
var TIMEFRAME_LIST = ['1h', '2h', '4h', '1D', '3D', '1W', '1M'];
var RELATION_LIST = ['above', 'below'];
var OPERATOR_LIST = [
    { value: 'gt', label: 'greater than' }, { value: 'lt', label: 'less than' },
    { value: 'gte', label: 'greater or equal' }, { value: 'lte', label: 'less or equal' },
];
var VERDICT_LIST = ['AVOID', 'WEAK', 'NEUTRAL', 'GOOD', 'STRONG'];

// STATE_OPTIONS is derived from the indicator registry at runtime — each
// indicator carries its own `states: [...]` field. A small alias map covers
// legacy condition-builder keys whose saved rows point at keys that differ
// from the canonical registry key (STOCHASTIC→STOCH, STRUCTURE→MKT_STRUCTURE)
// plus a handful of condition-only synthetic keys (ADX, BB_SQUEEZE, VOLUME)
// that aren't toggleable indicators but still want state dropdowns.
var _CONDITION_ALIAS_STATES = {
    // STOCHASTIC: removed. The backend's _get_indicator_state()
    // has no evaluator branch for it — the precompute payload that feeds Plays
    // (asset_data.cockpit_extended) never carries classic Stochastic %K/%D/zone
    // (only Stoch RSI is precomputed there), so any saved condition using this
    // option could never evaluate to anything but "no state data". Verified via
    // carrac/charts/indicators_extended.py: compute_cockpit_snapshot() (the
    // function that actually populates cockpit_extended) omits the `stoch`
    // snapshot key that _stochastic() computes — that richer snapshot is only
    // used by the single-symbol chart-internal API (carrac/charts/service.py),
    // a different data path Plays/Alerts evaluation never sees.
    STRUCTURE: ['HH_HL', 'LH_LL', 'range'],
    // ADX: fixed to the real vocabulary the evaluator emits
    // (asset_data.adx.regime, see consensio/indicators/adx.py) — was
    // ['strong_trend', 'trending', 'weak'], of which only 'strong_trend' is a
    // real value ('trending'/'weak' can never be returned, so those two
    // options created permanently-dead saved conditions).
    ADX: ['ranging', 'trend_forming', 'strong_trend'],
    BB_POSITION: ['inside', 'above_upper', 'below_lower'],
    BB_SQUEEZE: ['squeezed', 'normal', 'expanded'],
    VOLUME: ['high', 'above_avg', 'normal', 'low'],
    // ICHIMOKU_TK is a sub-feature of the Ichimoku indicator (not a standalone registry entry)
    ICHIMOKU_TK: ['bullish', 'bearish', 'none'],
};

// None of the backend's _get_indicator_state() branches (conditions.py)
// read the `timeframe` param at all — each indicator's state is computed from ONE
// fixed upstream timeframe in the precompute payload (see carrac/precompute.py),
// regardless of what's selected here. This map records the ACTUAL timeframe each
// indicator_state-eligible indicator reads, so the picker can show the truth
// instead of an inert dropdown. 'MTF' / 'RT' are synthetic (non-TIMEFRAME_LIST)
// values — safe because CONDITION_SCHEMAS['indicator_state'] has no `timeframes`
// enum constraint, so the backend accepts any non-empty `timeframe` string.
var _INDICATOR_STATE_FIXED_TF = {
    // Weekly-only reads
    RSI: { value: '1W', label: 'Weekly (fixed)' },
    WEEKLY_CPR: { value: '1W', label: 'Weekly (fixed)' },
    // Monthly-only reads
    CPR: { value: '1M', label: 'Monthly (fixed)' },
    // Daily-only reads — core precompute fields and cockpit_extended (ichimoku,
    // alligator, ttm_squeeze, supertrend, psar, donchian, ema_cross) are all
    // computed from tf_data['1D'] regardless of the chart's active timeframe.
    DAILY_CPR: { value: '1D', label: 'Daily (fixed)' },
    EMA_RIBBON: { value: '1D', label: 'Daily (fixed)' },
    MACD: { value: '1D', label: 'Daily (fixed)' },
    STRUCTURE: { value: '1D', label: 'Daily (fixed)' },
    MKT_STRUCTURE: { value: '1D', label: 'Daily (fixed)' },
    BB_SQUEEZE: { value: '1D', label: 'Daily (fixed)' },
    BB_POSITION: { value: '1D', label: 'Daily (fixed)' },
    VOLUME: { value: '1D', label: 'Daily (fixed)' },
    ADX: { value: '1D', label: 'Daily (fixed)' },
    SUPER_TREND: { value: '1D', label: 'Daily (fixed)' },
    TTM_SQUEEZE: { value: '1D', label: 'Daily (fixed)' },
    DONCHIAN: { value: '1D', label: 'Daily (fixed)' },
    ICHIMOKU: { value: '1D', label: 'Daily (fixed)' },
    ICHIMOKU_TK: { value: '1D', label: 'Daily (fixed)' },
    EMA_CROSS: { value: '1D', label: 'Daily (fixed)' },
    ALLIGATOR: { value: '1D', label: 'Daily (fixed)' },
    PSAR: { value: '1D', label: 'Daily (fixed)' },
    // Multi-timeframe reads — no single TF applies
    CONSENSIO: { value: 'MTF', label: '1D+3D+1W combined (fixed)' },
};

function _buildStateOptions() {
    var out = {};
    var reg = (typeof window !== 'undefined' && window.IndicatorRegistry) || null;
    if (reg && typeof reg.getAllKeys === 'function' && typeof reg.getStates === 'function') {
        try {
            (reg.getAllKeys() || []).forEach(function(k) {
                var s = reg.getStates(k);
                if (Array.isArray(s) && s.length) out[k] = s;
            });
        } catch (e) {
            /* fall through to aliases */
        }
    }
    // Merge in condition-builder aliases. Never overwrite a registry-sourced entry.
    Object.keys(_CONDITION_ALIAS_STATES).forEach(function(k) {
        if (!out[k]) out[k] = _CONDITION_ALIAS_STATES[k];
    });
    return out;
}

// NOTE: deliberately no `var STATE_OPTIONS = ...` — same reason as INDICATOR_LIST
// above: the var declaration would create a non-configurable global binding at
// parse time, causing this defineProperty to throw.
Object.defineProperty(window, 'STATE_OPTIONS', {
    configurable: true,
    get: function() { return _buildStateOptions(); },
});

// Build labelled + sorted list of indicators that have state options
var _STATE_INDICATOR_LIST = Object.keys(STATE_OPTIONS).map(function(key) {
    var match = INDICATOR_LIST.find(function(i) { return i.value === key; });
    return { value: key, label: match ? match.label : key };
}).sort(function(a, b) { return a.label.localeCompare(b.label); });

var _conditionCounter = 0;

// ── Pill rendering ──

function renderConditionPills(conditions, editable) {
    var rules = (conditions || {}).rules || [];
    if (!rules.length && !editable) return '';

    var html = '<div class="cond-pills">';
    rules.forEach(function(r) {
        var met = r.met;
        var icon = met ? '&#10003;' : '&#9203;';
        var cls = met ? 'cond-pill-met' : 'cond-pill-pending';
        var summary = _condSummary(r);
        html += '<span class="cond-pill ' + cls + '" title="' + escapeHtml(summary) + '" data-rule-id="' + (r.id || '') + '">' +
            icon + ' ' + escapeHtml(summary);
        if (editable) {
            html += '<button class="cond-pill-remove" data-remove-id="' + (r.id || '') + '">&times;</button>';
        }
        html += '</span>';
    });
    if (editable) {
        html += '<button class="cond-pill-add" id="cond-add-btn">+ Add</button>';
    }
    // Logic indicator
    var logic = (conditions || {}).logic || 'all';
    if (rules.length > 1) {
        html += '<span class="cond-logic-badge">' + logic.toUpperCase() + '</span>';
    }
    html += '</div>';
    return html;
}

var _TF_FRIENDLY = { '1h': '1H', '4h': '4H', 'D': 'Daily', '3D': '3-Day', 'W': 'Weekly' };
function _tfLabel(tf) { return _TF_FRIENDLY[tf] || tf || '?'; }

function _condSummary(rule) {
    var t = rule.type || '?';
    // Support both {params: {field: val}} (from modal) and {field: val} (from quick-add) formats
    var p = rule.params || rule;
    var meta = CONDITION_TYPE_META[t];
    if (!meta) return t;

    if (t === 'price_level') return 'Price ' + (p.relation || '?') + ' $' + _fmtCondNum(p.level);
    if (t === 'price_zone') return 'Price ' + (p.relation || '?') + ' $' + _fmtCondNum(p.low) + '-$' + _fmtCondNum(p.high);
    if (t === 'candle_close') return (p.timeframe || '?') + ' close ' + (p.relation || '?') + ' $' + _fmtCondNum(p.level);
    if (t === 'indicator_value') {
        var opWords = { gt: 'above', lt: 'below', gte: 'at or above', lte: 'at or below', eq: 'at' };
        return (p.indicator || '?').replace(/_/g, ' ') + ' (' + (p.timeframe || '?') + ') ' + (opWords[p.operator] || p.operator || '?') + ' ' + p.value;
    }
    if (t === 'indicator_crosses') return (p.indicator || '?').replace(/_/g, ' ') + ' (' + (p.timeframe || '?') + ') crosses ' + (p.direction || '?') + ' ' + p.value;
    if (t === 'indicator_state') {
        var _bbPosLabels = { above_upper: 'above upper band', below_lower: 'below lower band', inside: 'inside band' };
        var stateLabel = (p.indicator === 'BB_POSITION' && _bbPosLabels[p.state])
            ? _bbPosLabels[p.state]
            : (p.state || '?').replace(/_/g, ' ');
        return (p.indicator || '?').replace(/_/g, ' ') + ' ' + stateLabel + ' (' + _tfLabel(p.timeframe || '?') + ')';
    }
    if (t === 'indicator_divergence') return (p.type || '?') + ' divergence (' + (p.indicator || 'RSI').replace(/_/g, ' ') + ' ' + (p.timeframe || '1D') + ')';
    if (t === 'consensio_threshold') return 'Consensio >= ' + (p.min_value || '?');
    if (t === 'scanner_verdict') return 'Scanner ' + (p.direction || '?') + ' >= ' + (p.min_verdict || '?');
    if (t === 'time_limit') {
        var dateStr = p.expires_at ? p.expires_at.substring(0, 10) : (p.expires_in_days ? p.expires_in_days + ' days' : '?');
        var actionStr = p.time_action === 'skip' ? 'auto-skip' : 'review';
        return 'Deadline: ' + dateStr + ' (' + actionStr + ')';
    }
    if (t === 'manual') return p.label || 'Manual check';
    if (t === 'distance_from') return 'Within ' + (p.threshold_pct || '?') + '% of ' + (p.reference || '?');
    if (t === 'price_change_pct') return 'Price ' + (p.direction || '?') + ' ' + (p.threshold_pct || '?') + '% in ' + (p.period || '?');
    if (t === 'formula_expression') {
        var expr = (p.expression || '?');
        return 'Formula: ' + (expr.length > 40 ? expr.substring(0, 40) + '...' : expr);
    }
    if (t === 'engulfing_detected') return 'Engulfing ' + (p.direction || '?') + ' (' + _tfLabel(p.timeframe) + ')';
    if (t === 'bb_breakout_retest_engulfing') return 'BB breakout-retest ' + (p.direction || '?') + ' (' + _tfLabel(p.timeframe) + ', ' + (p.lookback_bars || '?') + 'b)';
    if (t === 'bb_fade_engulfing') return 'BB fade ' + (p.direction || '?') + ' (' + _tfLabel(p.timeframe) + ')';
    if (t === 'bb_squeeze_breakout') return 'BB squeeze breakout ' + (p.direction || '?') + ' (' + _tfLabel(p.timeframe) + ')';
    if (t === 'bb_breakdown_continuation') return 'BB breakdown ' + (p.direction || '?') + ' (' + _tfLabel(p.timeframe) + ', ' + (p.lookback_bars || '?') + 'b, ' + (p.confirm_mode || 'either') + ((p.bb_params && p.bb_params.offset) ? ', off' + p.bb_params.offset : '') + ')';
    return meta.label;
}

function _fmtCondNum(n) {
    if (n == null) return '?';
    n = parseFloat(n);
    return n >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : n.toString();
}

// ── Add Condition Modal ──

var _condModalCallback = null;
var _condModalEntityType = null;  // 'scenario' or 'play'
var _condModalEntityId = null;
var _condModalRules = []; // Authoritative list of rules in the modal

function openConditionModal(entityType, entityId, existingConditions, onSave) {
    _condModalEntityType = entityType;
    _condModalEntityId = entityId;
    _condModalCallback = onSave;

    var overlay = document.createElement('div');
    overlay.className = 'cond-modal-overlay';
    overlay.id = 'cond-modal-overlay';
    overlay.onclick = function(e) { if (e.target === overlay) closeConditionModal(); };

    var conditions = existingConditions || { rules: [], logic: 'all' };
    var rules = conditions.rules || [];
    _condModalRules = rules.map(function(r) { return Object.assign({}, r); });

    var html = '<div class="cond-modal">' +
        '<div class="cond-modal-header">' +
            '<span class="cond-modal-title">Conditions</span>' +
            '<button class="cond-modal-close">&times;</button>' +
        '</div>' +
        '<div class="cond-modal-body" id="cond-modal-body">' +
            '<div class="cond-existing" id="cond-existing-list">' +
                _renderExistingRules(rules) +
            '</div>' +
            '<div class="cond-add-section">' +
                '<div class="cond-cat-tabs" id="cond-cat-tabs">' +
                    CONDITION_CATEGORIES.map(function(c) {
                        return '<button class="cond-cat-tab" data-cat="' + c.key + '">' + c.label + '</button>';
                    }).join('') +
                '</div>' +
                '<div class="cond-type-form" id="cond-type-form"></div>' +
            '</div>' +
            '<div class="cond-logic-section">' +
                '<label>Logic: </label>' +
                '<select id="cond-logic-select">' +
                    '<option value="all"' + (conditions.logic === 'all' ? ' selected' : '') + '>ALL (every condition must be met)</option>' +
                    '<option value="any"' + (conditions.logic === 'any' ? ' selected' : '') + '>ANY (at least one must be met)</option>' +
                    '<option value="custom"' + (conditions.logic === 'custom' ? ' selected' : '') + '>Custom expression</option>' +
                '</select>' +
                '<input class="cond-expr-input" id="cond-expr-input" placeholder="(c1 AND c2) OR c3" value="' + escapeHtml(conditions.expression || '') + '" style="display:' + (conditions.logic === 'custom' ? 'block' : 'none') + '">' +
            '</div>' +
        '</div>' +
        '<div class="cond-modal-footer">' +
            (entityId ? '<button class="cond-test-btn">Test Against Current Data</button>' : '') +
            '<button class="cond-save-btn">Save Conditions</button>' +
        '</div>' +
        '<div class="cond-test-result" id="cond-test-result" style="display:none"></div>' +
    '</div>';

    overlay.innerHTML = html;
    document.body.appendChild(overlay);

    // Wire modal close / test / save buttons — rendered once per modal open, direct bind
    var closeBtn = overlay.querySelector('.cond-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeConditionModal);
    var testBtn = overlay.querySelector('.cond-test-btn');
    if (testBtn) testBtn.addEventListener('click', _testConditions);
    var saveBtn = overlay.querySelector('.cond-save-btn');
    if (saveBtn) saveBtn.addEventListener('click', _saveConditions);

    // Wire rule-remove buttons — delegated on the stable list container since
    // rows are rebuilt on every re-render (_addCondFromForm / _removeCondRule)
    var existingList = document.getElementById('cond-existing-list');
    if (existingList && !existingList.dataset.bound) {
        existingList.addEventListener('click', function(e) {
            var btn = e.target.closest('.cond-rule-remove');
            if (!btn) return;
            var row = btn.closest('.cond-rule-row');
            var ruleId = row ? row.getAttribute('data-rule-id') : null;
            if (ruleId) _removeCondRule(ruleId);
        });
        existingList.dataset.bound = '1';
    }

    // Wire type-form actions — delegated on the stable form container since its
    // contents (formula presets, add/cancel buttons) are rebuilt on every
    // _showCondTypeForm() / _showCondParamForm() render
    var typeFormEl = document.getElementById('cond-type-form');
    if (typeFormEl && !typeFormEl.dataset.bound) {
        typeFormEl.addEventListener('click', function(e) {
            var presetBtn = e.target.closest('.cond-formula-preset');
            if (presetBtn) {
                var exprEl = document.getElementById('cond-param-expression');
                if (exprEl) exprEl.value = presetBtn.getAttribute('data-preset-expr') || '';
                return;
            }
            var addBtn = e.target.closest('.cond-param-add');
            if (addBtn) {
                _addCondFromForm(addBtn.getAttribute('data-cond-type'));
                return;
            }
            var cancelBtn = e.target.closest('.cond-param-cancel');
            if (cancelBtn) {
                _showCondTypeForm(cancelBtn.getAttribute('data-cat') || '');
                return;
            }
        });
        typeFormEl.dataset.bound = '1';
    }

    // Wire category tabs
    overlay.querySelectorAll('.cond-cat-tab').forEach(function(tab) {
        tab.onclick = function() {
            overlay.querySelectorAll('.cond-cat-tab').forEach(function(t) { t.classList.remove('active'); });
            this.classList.add('active');
            _showCondTypeForm(this.getAttribute('data-cat'));
        };
    });

    // Wire logic select
    var logicSel = document.getElementById('cond-logic-select');
    if (logicSel) logicSel.onchange = function() {
        var exprInput = document.getElementById('cond-expr-input');
        if (exprInput) exprInput.style.display = this.value === 'custom' ? 'block' : 'none';
    };

    // Default to first category
    var firstTab = overlay.querySelector('.cond-cat-tab');
    if (firstTab) firstTab.click();
}

function closeConditionModal() {
    var overlay = document.getElementById('cond-modal-overlay');
    if (overlay) overlay.remove();
    _condModalCallback = null;
    _condModalEntityType = null;
    _condModalEntityId = null;
    _condModalRules = [];
}

function _renderExistingRules(rules) {
    if (!rules.length) return '<div class="cond-empty">No conditions yet. Add one below.</div>';
    return rules.map(function(r) {
        var summary = _condSummary(r);
        var metIcon = r.met ? '<span class="cond-met-icon">&#10003;</span>' : '<span class="cond-pending-icon">&#9203;</span>';
        return '<div class="cond-rule-row" data-rule-id="' + r.id + '">' +
            metIcon + ' <span class="cond-rule-text">' + escapeHtml(summary) + '</span>' +
            '<button class="cond-rule-remove">&times;</button>' +
        '</div>';
    }).join('');
}

function _showCondTypeForm(category) {
    var cat = CONDITION_CATEGORIES.find(function(c) { return c.key === category; });
    if (!cat) return;
    var formEl = document.getElementById('cond-type-form');
    if (!formEl) return;

    var html = cat.types.map(function(t) {
        var meta = CONDITION_TYPE_META[t];
        return '<div class="cond-type-option" data-type="' + t + '">' +
            '<strong>' + meta.label + '</strong> <span class="cond-type-desc">' + meta.desc + '</span>' +
        '</div>';
    }).join('');
    formEl.innerHTML = html;

    formEl.querySelectorAll('.cond-type-option').forEach(function(opt) {
        opt.onclick = function() {
            _showCondParamForm(this.getAttribute('data-type'));
        };
    });
}

function _showCondParamForm(type) {
    var formEl = document.getElementById('cond-type-form');
    if (!formEl) return;
    var meta = CONDITION_TYPE_META[type];
    if (!meta) return;

    var html = '<div class="cond-param-form"><div class="cond-param-title">' + meta.label + '</div>';

    if (type === 'price_level') {
        html += _selectField('relation', 'Relation', RELATION_LIST);
        html += _numField('level', 'Price level');
    } else if (type === 'price_zone') {
        html += _numField('low', 'Zone low');
        html += _numField('high', 'Zone high');
        html += _selectField('relation', 'Relation', ['inside', 'outside', 'breaks_above', 'breaks_below']);
    } else if (type === 'candle_close') {
        html += _selectField('timeframe', 'Timeframe', TIMEFRAME_LIST);
        html += _selectField('relation', 'Relation', RELATION_LIST);
        html += _numField('level', 'Close level');
    } else if (type === 'indicator_value') {
        html += _selectField('indicator', 'Indicator', INDICATOR_LIST);
        html += _selectField('timeframe', 'Timeframe', TIMEFRAME_LIST);
        html += _selectField('operator', 'Operator', OPERATOR_LIST);
        html += _numField('value', 'Value');
    } else if (type === 'indicator_crosses') {
        html += _selectField('indicator', 'Indicator', INDICATOR_LIST);
        html += _selectField('timeframe', 'Timeframe', TIMEFRAME_LIST);
        html += _selectField('direction', 'Direction', ['above', 'below']);
        html += _numField('value', 'Threshold');
    } else if (type === 'indicator_state') {
        html += _selectField('indicator', 'Indicator', _STATE_INDICATOR_LIST);
        html += _selectField('timeframe', 'Timeframe', TIMEFRAME_LIST);
        html += '<select class="cond-field" id="cond-param-state"><option value="">Select state...</option></select>';
    } else if (type === 'indicator_divergence') {
        html += _selectField('indicator', 'Indicator', ['RSI', 'MACD']);
        html += _selectField('timeframe', 'Timeframe', TIMEFRAME_LIST);
        html += _selectField('type', 'Type', ['bullish', 'bearish']);
    } else if (type === 'consensio_threshold') {
        html += _numField('min_value', 'Min positions (0-9)');
    } else if (type === 'scanner_verdict') {
        html += _selectField('direction', 'Direction', ['long', 'short']);
        html += _selectField('min_verdict', 'Min verdict', VERDICT_LIST);
    } else if (type === 'time_limit') {
        html += '<label style="font-size:10px;color:#787b86;display:block;margin-bottom:3px">Deadline date</label>';
        html += '<input class="cond-field" id="cond-param-expires_at" type="date">';
        html += '<label style="font-size:10px;color:#787b86;display:block;margin:8px 0 3px">When deadline passes</label>';
        html += '<select class="cond-field" id="cond-param-time_action">' +
            '<option value="notify">Notify me to review (keep active)</option>' +
            '<option value="skip">Auto-skip this milestone</option>' +
        '</select>';
    } else if (type === 'manual') {
        html += '<input class="cond-field" id="cond-param-label" placeholder="What to check manually...">';
    } else if (type === 'formula_expression') {
        html += '<label style="font-size:10px;color:#787b86;display:block;margin-bottom:3px">Formula expression</label>';
        html += '<textarea class="cond-field" id="cond-param-expression" rows="3" placeholder="RSI(14) < 30 AND CROSSOVER(EMA(9), EMA(21))" style="font-family:monospace;font-size:12px;resize:vertical;width:100%"></textarea>';
        html += _selectField('timeframe', 'Timeframe', TIMEFRAME_LIST);
        html += '<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">';
        var presets = [
            ['RSI Oversold', 'RSI(14) < 30'],
            ['BB Squeeze', 'SQUEEZE_ON()'],
            ['Golden Cross', 'CROSSOVER(SMA(50), SMA(200))'],
            ['MACD Bull Cross', 'CROSSOVER(MACD_LINE(), MACD_SIGNAL())'],
            ['Trend + RSI', 'EMA_RIBBON_TREND() > 0 AND RSI(14) > 50'],
        ];
        presets.forEach(function(p) {
            html += '<button type="button" class="cond-formula-preset" style="font-size:10px;padding:2px 6px;background:#2a2e39;border:1px solid #363a45;border-radius:3px;color:#d1d4dc;cursor:pointer" ' +
                'data-preset-expr="' + escapeHtml(p[1]) + '">' + p[0] + '</button>';
        });
        html += '</div>';
        html += '<a href="/formulas" target="_blank" style="font-size:10px;margin-top:4px;display:block;color:#2962ff">Formula reference &rarr;</a>';
    } else if (type === 'distance_from') {
        html += _selectField('reference', 'Reference', ['entry', 'invalidation', 'target']);
        html += _numField('threshold_pct', 'Threshold %');
    } else if (type === 'price_change_pct') {
        html += _selectField('period', 'Period', ['24h', '7d', '30d']);
        html += _selectField('direction', 'Direction', ['up', 'down']);
        html += _numField('threshold_pct', 'Threshold %');
    } else if (type === 'engulfing_detected') {
        html += _selectField('direction', 'Direction', ['bullish', 'bearish']);
        html += _selectField('timeframe', 'Timeframe', [
            { value: '1h', label: '1H' }, { value: '4h', label: '4H' },
            { value: 'D', label: 'Daily' }, { value: '3D', label: '3-Day' }, { value: 'W', label: 'Weekly' },
        ]);
    } else if (type === 'bb_breakout_retest_engulfing') {
        html += _selectField('direction', 'Direction', ['bullish', 'bearish']);
        html += _selectField('timeframe', 'Timeframe', [
            { value: '1h', label: '1H' }, { value: '4h', label: '4H' },
            { value: 'D', label: 'Daily' }, { value: '3D', label: '3-Day' }, { value: 'W', label: 'Weekly' },
        ]);
        html += '<input class="cond-field" id="cond-param-lookback_bars" type="number" min="5" max="50" step="1" placeholder="Lookback bars (5–50, default 15)">';
        html += '<input class="cond-field" id="cond-param-proximity_pct" type="number" min="0.1" max="2.0" step="0.1" placeholder="Proximity % to middle (0.1–2.0, default 0.5)">';
        html += '<label style="display:flex;align-items:center;gap:6px;color:#d1d4dc;font-size:12px;margin-top:4px">' +
                '<input type="checkbox" id="cond-param-require_volume" checked> Require volume confirmation</label>';
    } else if (type === 'bb_fade_engulfing') {
        html += _selectField('direction', 'Direction', [
            { value: 'bullish', label: 'Bullish (fade off lower band — long reversal)' },
            { value: 'bearish', label: 'Bearish (fade off upper band — short reversal)' },
        ]);
        html += _selectField('timeframe', 'Timeframe', [
            { value: '1h', label: '1H' }, { value: '4h', label: '4H' },
            { value: 'D', label: 'Daily' }, { value: '3D', label: '3-Day' }, { value: 'W', label: 'Weekly' },
        ]);
    } else if (type === 'bb_squeeze_breakout') {
        html += _selectField('direction', 'Direction', ['bullish', 'bearish']);
        html += _selectField('timeframe', 'Timeframe', [
            { value: '1h', label: '1H' }, { value: '4h', label: '4H' },
            { value: 'D', label: 'Daily' }, { value: '3D', label: '3-Day' }, { value: 'W', label: 'Weekly' },
        ]);
        html += '<input class="cond-field" id="cond-param-lookback_bars" type="number" min="5" max="30" step="1" placeholder="Lookback bars (5–30, default 10)">';
        html += '<input class="cond-field" id="cond-param-min_squeeze_bars" type="number" min="1" max="20" step="1" placeholder="Min consecutive squeeze bars (1–20, default 3)">';
        html += '<label style="display:flex;align-items:center;gap:6px;color:#d1d4dc;font-size:12px;margin-top:4px">' +
                '<input type="checkbox" id="cond-param-require_volume" checked> Require volume above 20-SMA on breakout bar</label>';
    } else if (type === 'bb_breakdown_continuation') {
        html += _selectField('direction', 'Direction', [
            { value: 'bearish', label: 'Bearish (breakdown below lower band — short)' },
            { value: 'bullish', label: 'Bullish (breakout above upper band — long)' },
        ]);
        html += _selectField('timeframe', 'Timeframe', [
            { value: '1h', label: '1H' }, { value: '4h', label: '4H' },
            { value: 'D', label: 'Daily' }, { value: '3D', label: '3-Day' }, { value: 'W', label: 'Weekly' },
        ]);
        html += '<input class="cond-field" id="cond-param-lookback_bars" type="number" min="5" max="50" step="1" placeholder="Lookback bars (5–50, default 15)">';
        html += _selectField('confirm_mode', 'Confirm with', [
            { value: 'either', label: 'Either — engulfing OR band retest' },
            { value: 'engulfing', label: 'Engulfing only' },
            { value: 'band_retest', label: 'Band retest only' },
        ]);
        html += _selectField('retest_band', 'Retest band', [
            { value: 'broken', label: 'Broken band (lower for short / upper for long)' },
            { value: 'middle', label: 'Middle band' },
        ]);
        html += '<input class="cond-field" id="cond-param-proximity_pct" type="number" min="0.1" max="2.0" step="0.1" placeholder="Retest proximity % (0.1–2.0, default 0.5)">';
        html += '<input class="cond-field" id="cond-param-bb_offset" type="number" min="-20" max="20" step="1" placeholder="BB offset (−20 to 20, default 0)">';
    } else {
        html += '<div class="cond-type-desc">Configuration coming soon</div>';
    }

    var _backCatKey = (CONDITION_CATEGORIES.find(function(c) { return c.types.indexOf(type) >= 0; }) || {}).key || '';
    html += '<div class="cond-param-actions">' +
        '<button class="cond-param-add" data-cond-type="' + escapeHtml(type) + '">Add Condition</button>' +
        '<button class="cond-param-cancel" data-cat="' + escapeHtml(_backCatKey) + '">Back</button>' +
    '</div></div>';

    formEl.innerHTML = html;

    // Replace indicator <select> with searchable dropdown
    _upgradeToSearchableSelect('cond-param-indicator');

    // Wire indicator_state: indicator select → populate state options + lock
    // timeframe to whatever the backend actually reads (see
    // _INDICATOR_STATE_FIXED_TF above; generalises the old CPR-only lock).
    if (type === 'indicator_state') {
        var indSel = document.getElementById('cond-param-indicator');
        if (indSel) indSel.addEventListener('change', function() {
            var val = indSel.value;
            var s = document.getElementById('cond-param-state');
            var opts = STATE_OPTIONS[val] || [];
            s.innerHTML = opts.map(function(o) { return '<option>' + o + '</option>'; }).join('');
            var tfSel = document.getElementById('cond-param-timeframe');
            if (tfSel) {
                var fixedTf = _INDICATOR_STATE_FIXED_TF[val];
                if (fixedTf) {
                    tfSel.innerHTML = '<option value="' + fixedTf.value + '">' + fixedTf.label + '</option>';
                    tfSel.disabled = true;
                } else {
                    tfSel.innerHTML = TIMEFRAME_LIST.map(function(t) { return '<option value="' + t + '">' + t + '</option>'; }).join('');
                    tfSel.disabled = false;
                }
            }
        });
    }
}

function _selectField(id, label, options) {
    return '<select class="cond-field" id="cond-param-' + id + '">' +
        '<option value="">' + label + '...</option>' +
        options.map(function(o) {
            var val = typeof o === 'object' ? o.value : o;
            var text = typeof o === 'object' ? o.label : o;
            return '<option value="' + val + '">' + text + '</option>';
        }).join('') +
    '</select>';
}

function _numField(id, placeholder) {
    return '<input class="cond-field" id="cond-param-' + id + '" type="number" step="any" placeholder="' + placeholder + '">';
}

function _upgradeToSearchableSelect(selectId) {
    var sel = document.getElementById(selectId);
    if (!sel || sel.options.length <= 10) return;

    // Collect options (skip placeholder)
    var items = [];
    for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value) items.push({ value: sel.options[i].value, label: sel.options[i].textContent });
    }

    // Replace select with a text input + dropdown wrapper
    var wrap = document.createElement('div');
    wrap.className = 'cond-searchable-wrap';
    wrap.style.position = 'relative';

    var input = document.createElement('input');
    input.className = 'cond-field';
    input.id = selectId + '-search';
    input.placeholder = sel.options[0] ? sel.options[0].textContent : 'Select...';
    input.autocomplete = 'off';

    // Hidden input to hold the actual value
    var hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.id = selectId;
    hidden.name = selectId;

    var dd = document.createElement('div');
    dd.className = 'cond-searchable-dd';
    dd.style.display = 'none';

    wrap.appendChild(input);
    wrap.appendChild(hidden);
    wrap.appendChild(dd);
    sel.parentNode.replaceChild(wrap, sel);

    function renderList(q) {
        var filtered = items.filter(function(it) {
            if (!q) return true;
            return it.label.toLowerCase().indexOf(q) >= 0 || it.value.toLowerCase().indexOf(q) >= 0;
        });
        dd.innerHTML = filtered.map(function(it) {
            return '<div class="cond-searchable-item" data-value="' + it.value + '">' + it.label + '</div>';
        }).join('');
        dd.style.display = filtered.length ? 'block' : 'none';
        dd.querySelectorAll('.cond-searchable-item').forEach(function(item) {
            item.addEventListener('mousedown', function(e) {
                e.preventDefault();
                hidden.value = item.getAttribute('data-value');
                input.value = item.textContent;
                dd.style.display = 'none';
                // Trigger change event for downstream handlers (state options, timeframe lock)
                hidden.dispatchEvent(new Event('change', { bubbles: true }));
            });
        });
    }

    input.addEventListener('focus', function() { renderList(input.value.toLowerCase()); });
    input.addEventListener('input', function() { renderList(input.value.toLowerCase()); });
    input.addEventListener('blur', function() { setTimeout(function() { dd.style.display = 'none'; }, 150); });
}

function _addCondFromForm(type) {
    var meta = CONDITION_TYPE_META[type];
    if (!meta) return;

    var params = {};
    meta.fields.forEach(function(f) {
        var el = document.getElementById('cond-param-' + f);
        if (!el) return;
        var val = el.value;
        if (el.type === 'checkbox') { params[f] = el.checked; return; }
        if (el.type === 'number' && val) val = parseFloat(val);
        if (val !== '' && val !== null) params[f] = val;
    });

    // Handle time_limit date conversion
    if (type === 'time_limit' && params.expires_at) {
        params.expires_at = params.expires_at + 'T23:59:59Z';
    }

    // Setup F: assemble nested bb_params from the flat bb_offset field
    if (type === 'bb_breakdown_continuation') {
        if (params.bb_offset !== undefined && params.bb_offset !== '') {
            params.bb_params = { offset: parseInt(params.bb_offset, 10) };
        }
        delete params.bb_offset;
    }

    // Normalize the legacy 'STRUCTURE' alias key to the registry's
    // canonical 'MKT_STRUCTURE' at save time, so new rows are consistent with
    // every other touch point (indicator-registry.js, toggle maps, etc). The
    // backend's _get_indicator_state() still accepts both keys, so plays saved
    // with either one keep evaluating unchanged.
    if (type === 'indicator_state' && params.indicator === 'STRUCTURE') {
        params.indicator = 'MKT_STRUCTURE';
    }

    _conditionCounter++;
    var rule = {
        id: 'c' + _conditionCounter,
        type: type,
        params: params,
        met: false,
        met_at: null,
    };

    // Add to authoritative array and re-render the list
    _condModalRules.push(rule);
    var listEl = document.getElementById('cond-existing-list');
    if (listEl) {
        listEl.innerHTML = _renderExistingRules(_condModalRules);
    }

    // Reset form to type list
    var cat = CONDITION_CATEGORIES.find(function(c) { return c.types.indexOf(type) >= 0; });
    if (cat) _showCondTypeForm(cat.key);
}

function _removeCondRule(ruleId) {
    _condModalRules = _condModalRules.filter(function(r) { return r.id !== ruleId; });
    var listEl = document.getElementById('cond-existing-list');
    if (listEl) {
        listEl.innerHTML = _condModalRules.length
            ? _renderExistingRules(_condModalRules)
            : '<div class="cond-empty">No conditions. Add one below.</div>';
    }
}

function _saveConditions() {
    var logicSel = document.getElementById('cond-logic-select');
    var exprInput = document.getElementById('cond-expr-input');
    var logic = logicSel ? logicSel.value : 'all';
    var expression = (logic === 'custom' && exprInput) ? exprInput.value : undefined;

    var conditions = { rules: _condModalRules.slice(), logic: logic };
    if (expression) conditions.expression = expression;

    if (_condModalCallback) _condModalCallback(conditions);
    closeConditionModal();
}

function _testConditions() {
    if (!_condModalEntityId) return;
    var resultEl = document.getElementById('cond-test-result');
    if (!resultEl) return;

    resultEl.style.display = 'block';
    resultEl.innerHTML = '<span class="cond-test-loading">Testing...</span>';

    // Every caller opens the modal with entityType 'play' (plays-panel.js,
    // plays-creation-form.js); the 'scenario' branch went with the Scenarios
    // feature — its endpoint never existed in either app.
    var url = '/api/plays/' + _condModalEntityId + '/evaluate';

    var csrfToken = typeof getCSRFToken === 'function' ? getCSRFToken() : '';
    fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        // Plays endpoint returns {play, evaluation}, scenarios returns flat
        var eval_ = data.evaluation || data;
        var rules = eval_.rules || (data.play && data.play.conditions ? data.play.conditions.rules : []) || [];
        var met_count = eval_.met_count != null ? eval_.met_count : rules.filter(function(r) { return r.met; }).length;
        var total_count = eval_.total_count != null ? eval_.total_count : rules.length;

        var html = '<div class="cond-test-header">' + met_count + '/' + total_count + ' conditions met</div>';
        rules.forEach(function(r) {
            var icon = r.met ? '&#10003;' : '&#10007;';
            var cls = r.met ? 'cond-test-met' : 'cond-test-unmet';
            html += '<div class="' + cls + '">' + icon + ' ' + escapeHtml(r.evidence || _condSummary(r) || r.type) + '</div>';
        });
        if (eval_.invalidation_triggered || data.invalidation_triggered) {
            html += '<div class="cond-test-warn">&#9888; Invalidation triggered!</div>';
        }
        if (eval_.suggested_status || data.suggested_status) {
            html += '<div class="cond-test-suggest">Suggested: ' + (eval_.suggested_status || data.suggested_status).toUpperCase() + '</div>';
        }
        resultEl.innerHTML = html;
    })
    .catch(function(e) {
        resultEl.innerHTML = '<div class="cond-test-error">Test failed: ' + e.message + '</div>';
    });
}

