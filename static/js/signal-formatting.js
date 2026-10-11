/* Signal Formatting
 * Pattern names, signal abbreviations, hot section logic.
 * Deps: escapeHtml() (shared-utils.js), allAssets (global) */

// Pattern names for tooltips
var patternNames = {
    'divergence': 'Timeframe Divergence',
    'fresh_flip': 'Fresh EMA Ribbon Flip',
    'rsi_oversold': 'RSI Oversold',
    'rsi_overbought': 'RSI Overbought',
    'rsi_bull_div': 'RSI Bullish Divergence',
    'rsi_bear_div': 'RSI Bearish Divergence',
    'rsi_hidden_bull_div': 'Hidden Bullish Divergence',
    'rsi_hidden_bear_div': 'Hidden Bearish Divergence',
    'direction_change': 'Direction Change',
    'momentum': 'Momentum Streak',
    // v2 patterns
    'convergence_plus_flip': 'Converging + Fresh Flip (Best!)',
    'converging': 'EMAs Converging (Tightening)',
    'sandwiched': 'Price Sandwiched Between EMAs',
    'above_emas': 'Price Above Both EMAs',
    'below_emas': 'Price Below Both EMAs',
    'adx_sweet_spot': 'ADX Sweet Spot (20-30)',
    'ranging_penalty': 'Ranging Market Penalty',
    'mature_penalty': 'Mature Trend Penalty',
    // Components
    'ema_convergence': 'EMA Convergence',
    'price_position': 'Price Position',
    'volume': 'Volume',
    'adx_modifier': 'ADX Modifier'
};

// Alert names for tooltip
var alertNames = {
    'ema_squeeze': 'EMA Squeeze - Flip Imminent!',
    'v_cpr': 'V-CPR Vacuum - Price May Pull to Middle',
    'near_s3': 'Near S3 - Extreme Support Zone',
    'near_r3': 'Near R3 - Extreme Resistance Zone'
};

// Signal abbreviations for Signals column
var signalAbbreviations = {
    // Alerts
    'ema_squeeze': { abbr: 'SQZ', class: 'signal-squeeze', tooltip: 'EMA Squeeze - Flip Imminent!' },
    'v_cpr': { abbr: 'V-CPR', class: 'signal-vcpr', tooltip: 'V-CPR Vacuum - Price May Pull to Middle' },
    'near_s3': { abbr: 'S3', class: 'signal-bear', tooltip: 'Near S3 - Extreme Support Zone' },
    'near_r3': { abbr: 'R3', class: 'signal-bull', tooltip: 'Near R3 - Extreme Resistance Zone' },
    // Divergences
    'divergence': { abbr: 'TF-DIV', class: 'signal-divergence', tooltip: 'Timeframe Divergence - Daily disagrees with higher TFs' },
    'rsi_bull_div': { abbr: 'RSI\u2191', class: 'signal-bull', tooltip: 'RSI Bullish Divergence' },
    'rsi_bear_div': { abbr: 'RSI\u2193', class: 'signal-bear', tooltip: 'RSI Bearish Divergence' },
    'rsi_hidden_bull_div': { abbr: 'H-RSI\u2191', class: 'signal-hidden', tooltip: 'Hidden Bullish Divergence (continuation)' },
    'rsi_hidden_bear_div': { abbr: 'H-RSI\u2193', class: 'signal-hidden', tooltip: 'Hidden Bearish Divergence (continuation)' },
    // Structure Divergence (EMA Ribbon vs HH/HL/LH/LL)
    'structure_bearish': { abbr: 'STR\u2193', class: 'signal-structure-bear', tooltip: 'Structure Warning: EMA Ribbon bullish but making LH+LL (bearish structure)' },
    'structure_bullish': { abbr: 'STR\u2191', class: 'signal-structure-bull', tooltip: 'Structure Divergence: EMA Ribbon bearish but making HH+HL (bullish structure)' },
    // Conditions
    'tightening': { abbr: 'TGT', class: 'signal-tightening', tooltip: 'EMA Ribbon Tightening - EMAs converging, flip may be imminent' },
    'rsi_oversold': { abbr: 'RSI<30', class: 'signal-rsi-oversold', tooltip: 'RSI Oversold - Weekly RSI below 30' },
    'rsi_overbought': { abbr: 'RSI>70', class: 'signal-rsi-overbought', tooltip: 'RSI Overbought - Weekly RSI above 70' },
    // Momentum
    'direction_change': { abbr: 'CC', class: 'signal-cc', tooltip: 'Consensio Change - Positions changed since yesterday' }
};

// Format Signals column - collects all signals into abbreviated icons with tooltips
function formatSignals(asset) {
    var signals = [];
    var scoreB = asset.score_b || {};
    var alerts = Array.isArray(scoreB.alerts) ? scoreB.alerts : [];
    var trendAnalysis = asset.trend_analysis || {};
    var patterns = trendAnalysis.patterns || [];

    // Collect alerts from score_b
    alerts.forEach(function(alert) {
        if (signalAbbreviations[alert]) {
            signals.push(signalAbbreviations[alert]);
        }
    });

    // Collect pattern-based signals (divergences)
    var patternSignals = ['divergence', 'rsi_bull_div', 'rsi_bear_div', 'rsi_hidden_bull_div', 'rsi_hidden_bear_div'];
    patternSignals.forEach(function(pattern) {
        if (patterns.indexOf(pattern) !== -1 && signalAbbreviations[pattern]) {
            signals.push(signalAbbreviations[pattern]);
        }
    });

    // Check for RSI extremes
    var rsi = asset.weekly_rsi;
    if (rsi !== null && rsi !== undefined && !isNaN(rsi)) {
        if (rsi < 30) {
            signals.push(signalAbbreviations['rsi_oversold']);
        } else if (rsi > 70) {
            signals.push(signalAbbreviations['rsi_overbought']);
        }
    }

    if (signals.length === 0) {
        return '<span class="signals-empty">--</span>';
    }

    // Build HTML for each signal
    var html = '<span class="signals-container">';
    signals.forEach(function(sig) {
        html += '<span class="signal-badge ' + escapeHtml(sig.class) + '" title="' + escapeHtml(sig.tooltip) + '">' + escapeHtml(sig.abbr) + '</span>';
    });
    html += '</span>';

    return html;
}

// formatPatterns() and getPatternCount() removed - v7.1 signal simplification
// Chart pattern detection disabled (unreliable in crypto markets)

// Get signal count for sorting
function getSignalCount(asset) {
    var count = 0;
    var scoreB = asset.score_b || {};
    var alerts = Array.isArray(scoreB.alerts) ? scoreB.alerts : [];
    var trendAnalysis = asset.trend_analysis || {};
    var patterns = trendAnalysis.patterns || [];

    count += alerts.length;

    var patternSignals = ['divergence', 'rsi_bull_div', 'rsi_bear_div', 'rsi_hidden_bull_div', 'rsi_hidden_bear_div'];
    patternSignals.forEach(function(pattern) {
        if (patterns.indexOf(pattern) !== -1) count++;
    });

    // Note: Chart patterns now have their own column

    return count;
}

// Hot Section — signal highlights
var _hotScoreWeights = {
    'ema_squeeze': 3, 'v_cpr': 2, 'near_s3': 1, 'near_r3': 1,
    'divergence': 2, 'rsi_bull_div': 2, 'rsi_bear_div': 2,
    'direction_change': 2, 'rsi_hidden_bull_div': 1, 'rsi_hidden_bear_div': 1
};

function toggleHotSection() {
    var collapsed = false;
    try { collapsed = localStorage.getItem('hotSectionCollapsed') === '1'; } catch(e) {}
    try { localStorage.setItem('hotSectionCollapsed', collapsed ? '0' : '1'); } catch(e) {}
    var items = document.getElementById('hot-items');
    var btn = document.getElementById('hot-toggle-btn');
    if (items) items.style.display = collapsed ? '' : 'none';
    if (btn) btn.className = 'hot-toggle-btn' + (collapsed ? '' : ' collapsed');
}
