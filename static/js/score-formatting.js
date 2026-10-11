/* Score Formatting
 * Pattern highlight classes, screener filter presets, section labels/order.
 * Deps: none external. */

// Pattern highlight classes for inverted color styling
var patternHighlights = {
    'divergence': 'highlight-divergence',
    'direction_change': 'highlight-direction-change',
    'rsi_oversold': 'highlight-rsi-bull',
    'rsi_overbought': 'highlight-rsi-bear',
    'rsi_bull_div': 'highlight-rsi-bull',
    'rsi_bear_div': 'highlight-rsi-bear',
    'rsi_hidden_bull_div': 'highlight-hidden-div',
    'rsi_hidden_bear_div': 'highlight-hidden-div',
    'fresh_flip': 'highlight-fresh-flip'
};

// Filter presets
var filterPresets = {
    'all': function(a) { return true; },
    'any_signal': function(a) {
        // Use new opportunity patterns if available, fallback to trend_analysis
        var opp = a.opportunity || {};
        var patterns = opp.patterns || (a.trend_analysis || {}).patterns || [];
        return patterns.length > 0;
    },
    'emerging_bull': function(a) {
        // Use new opportunity score and direction
        var opp = a.opportunity || {};
        return opp.direction === 'bullish' && opp.score >= 40;
    },
    'emerging_bear': function(a) {
        var opp = a.opportunity || {};
        return opp.direction === 'bearish' && opp.score >= 40;
    },
    'trade_candidates': function(a) {
        var opp = a.opportunity || {};
        return opp.classification === 'trade_candidate';
    },
    'watchlist_items': function(a) {
        var opp = a.opportunity || {};
        return opp.classification === 'watchlist';
    },
    'fresh': function(a) {
        var opp = a.opportunity || {};
        var patterns = opp.patterns || (a.trend_analysis || {}).patterns || [];
        return patterns.indexOf('fresh_flip') !== -1 ||
               patterns.indexOf('direction_change') !== -1;
    },
    'rsi_extreme': function(a) {
        var rsi = a.weekly_rsi;
        return rsi !== null && rsi !== undefined && (rsi < 30 || rsi > 70);
    },
    'tightening': function(a) {
        // EMA Ribbon converging (yellow phase)
        var ema_ribbon = a.ema_ribbon || {};
        return ema_ribbon.is_converging === true;
    },
    'alerts': function(a) {
        // V-CPR, S3/R3 extremes, or EMA squeeze
        var scoreA = a.score_a || {};
        var scoreB = a.score_b || {};
        return scoreA.has_alert || scoreB.has_alert;
    },
    'custom': function(a) {
        // Custom filter - check if asset has any of the selected signals
        if (typeof customFilterSignals === 'undefined' || customFilterSignals.length === 0) return false;

        var scoreB = a.score_b || {};
        var alerts = scoreB.alerts || [];
        var trendAnalysis = a.trend_analysis || {};
        var patterns = trendAnalysis.patterns || [];

        // Check Signals dropdown (customFilterSignals)
        for (var i = 0; i < customFilterSignals.length; i++) {
            var signal = customFilterSignals[i];

            // Check alerts (ema_squeeze, v_cpr, near_s3, near_r3, structure_*)
            if (alerts.indexOf(signal) !== -1) return true;

            // Check patterns (divergence, rsi_*_div)
            if (patterns.indexOf(signal) !== -1) return true;

            // Check tightening condition
            if (signal === 'tightening') {
                var ema_ribbon = a.ema_ribbon || {};
                if (ema_ribbon.is_converging === true) return true;
            }

            // Check RSI extreme conditions
            if (signal === 'rsi_oversold' || signal === 'rsi_overbought') {
                var rsi = a.weekly_rsi;
                if (rsi !== null && rsi !== undefined) {
                    if (signal === 'rsi_oversold' && rsi < 30) return true;
                    if (signal === 'rsi_overbought' && rsi > 70) return true;
                }
            }
        }

        return false;
    }
};

// Spot price multipliers removed — backend now uses futures contracts (GC=F, SI=F)

// Section labels for display
var sectionLabels = {
    "crypto": "Cryptocurrencies",
    "crypto_metric": "Crypto Metrics",
    "stock": "Stocks",
    "etf": "ETFs",
    "index": "Indices",
    "forex": "Forex",
    "commodity": "Commodities"
};

// Section display order (crypto_metric first for market overview)
var sectionOrder = ["crypto_metric", "crypto", "index", "forex", "stock", "etf", "commodity"];
