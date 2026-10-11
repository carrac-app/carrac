/* Price Formatting
 * Price display, consensio/signal/RSI classes, trend/structure/CPR/BB formatting.
 * Deps: escapeHtml() (shared-utils.js) */

/* locale-aware number formatting helper */
function _fmtN(n, minDec, maxDec) {
    try {
        return n.toLocaleString(navigator.language, {minimumFractionDigits: minDec, maximumFractionDigits: maxDec !== undefined ? maxDec : minDec});
    } catch(e) {
        return n.toFixed(minDec);
    }
}

function formatPrice(price, symbol, type) {
    if (!price && price !== 0) return "--";

    if (type === "crypto_metric") {
        if (symbol.indexOf(".D") > -1 || symbol === "OTHERS.D") {
            return price.toFixed(2) + "%";
        }
        if (symbol.indexOf("TOTAL") > -1 && symbol.indexOf("BTC") === -1) {
            // $-trillions market caps only — ratio synthetics (TOTAL3BTC etc.)
            // are tiny BTC-denominated values and fall through to the ladder.
            return "$" + price.toFixed(2) + "T";
        }
        if (symbol === "ETHBTC") {
            return price.toFixed(5);
        }
    }

    if (type === "forex") {
        return price.toFixed(2);
    }

    if (price >= 10000) return "$" + price.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (price >= 1000) return "$" + price.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (price >= 10) return "$" + price.toFixed(2);
    if (price >= 1) return "$" + price.toFixed(2);
    if (price >= 0.01) return "$" + price.toFixed(4);
    if (price <= 0) return "$" + price.toFixed(2);
    return "$" + price.toPrecision(4); // sub-cent: keep ~4 sig figs, never "0.0000"
}

// Signed absolute change (Chg $ column) — reuses formatPrice's per-type
// magnitude tiers so decimals match the Price column.
function formatChangeValue(value, symbol, type) {
    if (value === null || value === undefined || isNaN(value)) return '';
    return (value >= 0 ? '+' : '−') + formatPrice(Math.abs(value), symbol, type);
}

function getConsensioClass(pos, max) {
    if (!max || max <= 0) return "consensio-neg";
    var ratio = pos / max;
    if (ratio >= 0.7) return "consensio-high";
    if (ratio >= 0.4) return "consensio-med";
    if (ratio > 0) return "consensio-low";
    return "consensio-neg";
}

function getSignalClass(signal) {
    if (signal > 0) return "signal-pos";
    if (signal < 0) return "signal-neg";
    return "signal-neu";
}

function formatSignal(signal) {
    if (signal > 0) return "+" + signal;
    return signal.toString();
}

function getRsiClass(rsi) {
    if (rsi === null || rsi === undefined) return "rsi-neutral";
    if (rsi > 70) return "rsi-overbought";
    if (rsi < 30) return "rsi-oversold";
    return "rsi-neutral";
}

function formatRsi(rsi) {
    if (rsi === null || rsi === undefined) return "--";
    return _fmtN(rsi, 1);
}


// Format Structure column - shows HH/HL or LH/LL patterns
function formatStructure(structure) {
    if (!structure) {
        return '<span class="structure structure-neutral">--</span>';
    }

    var highType = structure.last_high_type || '--';  // HH or LH
    var lowType = structure.last_low_type || '--';    // HL or LL
    var structureTrend = structure.structure_trend || 'neutral';

    // Color based on structure trend
    var colorClass = 'structure-neutral';
    if (structureTrend === 'bullish') {
        colorClass = 'structure-bullish';
    } else if (structureTrend === 'bearish') {
        colorClass = 'structure-bearish';
    }

    // Build tooltip
    var tooltip = 'Market Structure: ' + highType + '/' + lowType;
    if (structureTrend === 'bullish') {
        tooltip += ' (Uptrend: Higher Highs + Higher Lows)';
    } else if (structureTrend === 'bearish') {
        tooltip += ' (Downtrend: Lower Highs + Lower Lows)';
    } else {
        tooltip += ' (Mixed/Transitioning)';
    }

    return '<span class="structure ' + colorClass + '" title="' + escapeHtml(tooltip) + '">' + escapeHtml(highType) + '/' + escapeHtml(lowType) + '</span>';
}

// Format CPR Level column - shows price position relative to monthly CPR
// Uses TC (Top Central), BC (Bottom Central), CP (Central Pivot) abbreviations
function formatCPRLevel(cpr) {
    if (!cpr || !cpr.price_level) {
        return '<span class="cpr-level cpr-neutral">--</span>';
    }

    var level = cpr.price_level;
    var colorClass = 'cpr-neutral';

    // Color based on level zone (user request: inside CPR = purple, below = red, above = green)
    if (level === 'Below S3' || level === 'S3-S2' || level === 'S2-S1' || level === 'S1-BC') {
        colorClass = 'cpr-below';     // Below CPR - red
    } else if (level === 'BC-CP' || level === 'CP-TC') {
        colorClass = 'cpr-inside';    // Inside CPR zone - purple
    } else if (level === 'TC-R1' || level === 'R1-R2' || level === 'R2-R3' || level === 'Above R3') {
        colorClass = 'cpr-above';     // Above CPR - green
    }

    // Build tooltip with actual price levels (using full names)
    var tooltip = 'Monthly CPR Position: ' + level;
    if (cpr.pivot) {
        tooltip += '\\nTC (Top Central): ' + formatPriceSimple(cpr.tc);
        tooltip += '\\nCP (Central Pivot): ' + formatPriceSimple(cpr.pivot);
        tooltip += '\\nBC (Bottom Central): ' + formatPriceSimple(cpr.bc);
        tooltip += '\\nS1: ' + formatPriceSimple(cpr.s1) + ' | R1: ' + formatPriceSimple(cpr.r1);
        tooltip += '\\nS2: ' + formatPriceSimple(cpr.s2) + ' | R2: ' + formatPriceSimple(cpr.r2);
        tooltip += '\\nS3: ' + formatPriceSimple(cpr.s3) + ' | R3: ' + formatPriceSimple(cpr.r3);
    }

    return '<span class="cpr-level ' + colorClass + '" title="' + escapeHtml(tooltip) + '">' + escapeHtml(level) + '</span>';
}

// Format BB (Bollinger Band) squeeze column
function formatBB(bb) {
    if (!bb || bb.squeeze_pct == null) {
        return '<span class="bb bb-normal">--</span>';
    }

    var pct = Math.round(bb.squeeze_pct);
    var colorClass = 'bb-normal';

    // Color based on squeeze level
    if (pct < 30) {
        colorClass = 'bb-extreme-squeeze';  // Red - extreme squeeze
    } else if (pct < 50) {
        colorClass = 'bb-squeeze';          // Yellow - moderate squeeze
    } else if (pct > 150) {
        colorClass = 'bb-expansion';        // Faded - expansion
    }

    // Build tooltip with BB values
    var tooltip = 'Bollinger Band Width: ' + pct + '% of 60-day average';
    if (bb.upper && bb.lower && bb.middle) {
        tooltip += '\\nUpper: ' + formatPriceSimple(bb.upper);
        tooltip += '\\nMiddle: ' + formatPriceSimple(bb.middle);
        tooltip += '\\nLower: ' + formatPriceSimple(bb.lower);
    }
    if (pct < 50) {
        tooltip += '\\n\u26a0 Squeeze - Breakout may be imminent';
    }

    return '<span class="bb ' + colorClass + '" title="' + tooltip + '">' + pct + '%</span>';
}

function formatMacd(macd) {
    if (!macd || macd.histogram == null) return '<span class="macd-cell">--</span>';
    var h = macd.histogram || 0;
    var cls = h > 0 ? 'macd-bull' : h < 0 ? 'macd-bear' : '';
    var arrow = h > 0 ? '▲' : h < 0 ? '▼' : '—';
    return '<span class="macd-cell ' + cls + '" title="MACD histogram: ' + h.toFixed(2) + '">' + arrow + '</span>';
}

function formatAdx(adx) {
    if (!adx || adx.adx == null) return '<span class="adx-cell">--</span>';
    var val = Math.round(adx.adx);
    var cls = adx.regime === 'strong_trend' ? 'adx-strong' : adx.regime === 'ranging' ? 'adx-weak' : 'adx-forming';
    var dir = adx.trend_direction === 'bullish' ? '↑' : adx.trend_direction === 'bearish' ? '↓' : '';
    return '<span class="adx-cell ' + cls + '" title="ADX ' + val + ' (' + (adx.regime || '') + ')">' + val + dir + '</span>';
}

function formatAtrPct(adx) {
    if (!adx || adx.atr_pct === undefined || adx.atr_pct === null) return '<span class="atr-cell">--</span>';
    var val = adx.atr_pct;
    var cls = val > 5 ? 'atr-high' : val < 1 ? 'atr-low' : '';
    return '<span class="atr-cell ' + cls + '" title="ATR as % of price">' + _fmtN(val, 1) + '%</span>';
}

function formatRelVolume(vol) {
    if (!vol || vol.ratio === undefined || vol.ratio === null) return '<span class="vol-cell">--</span>';
    var r = vol.ratio;
    if (r < 0.01) return '<span class="vol-cell vol-low" title="Volume data unavailable or bar just started">--</span>';
    var cls = r > 1.5 ? 'vol-high' : r < 0.5 ? 'vol-low' : '';
    return '<span class="vol-cell ' + cls + '" title="Volume vs 20-bar average">' + _fmtN(r, 1) + 'x</span>';
}

// Helper to format price for tooltips
function formatPriceSimple(price) {
    if (price === null || price === undefined || isNaN(price)) return '--';
    if (price >= 1000) return price.toLocaleString('en-US', {maximumFractionDigits: 0});
    if (price >= 1) return price.toFixed(2);
    if (price >= 0.01) return price.toFixed(4);
    if (price >= 0.0001) return price.toFixed(6);
    if (price <= 0) return price.toFixed(2);
    return price.toPrecision(4); // micro-prices: ~4 sig figs, not 1
}

// Format EMA Ribbon indicator for dashboard table
function formatEmaRibbon(emaRibbon) {
    if (!emaRibbon) return { html: '--', date: '--' };
    var trend = emaRibbon.trend || 'neutral';
    // Map trend to display labels (Above/Below/Inside)
    var trendLabels = {
        'bull': 'Above',
        'bear': 'Below',
        'neutral': 'Inside'
    };
    var trendLabel = trendLabels[trend] || 'Inside';
    return {
        html: '<span class="ema-ribbon-indicator">' +
              '<span class="ema-ribbon-dot ' + trend + '"></span>' +
              '<span class="ema-ribbon-label ' + trend + '">' + trendLabel + '</span>' +
              '</span>',
        date: emaRibbon.last_change || '--'
    };
}

// ─── Extended indicator column formatters ────────────────────────────
// Each takes the indicator snapshot dict from asset.cockpit_extended.{key}

function formatIchimoku(d) {
    if (!d) return '--';
    var pos = d.cloud_position || '';
    var cls = pos.indexOf('above') !== -1 ? 'ext-bull' : pos.indexOf('below') !== -1 ? 'ext-bear' : 'ext-neutral';
    var label = pos.indexOf('above') !== -1 ? 'Above' : pos.indexOf('below') !== -1 ? 'Below' : 'Inside';
    var tk = d.tk_cross && d.tk_cross !== 'none' ? ' TK' + (d.tk_cross === 'bullish' ? '\u25B2' : '\u25BC') : '';
    return '<span class="' + cls + '">' + label + tk + '</span>';
}

function formatSuperTrend(d) {
    if (!d) return '--';
    var cls = d.direction === 'long' ? 'ext-bull' : 'ext-bear';
    var arrow = d.direction === 'long' ? '\u25B2' : '\u25BC';
    return '<span class="' + cls + '">' + arrow + (d.flip ? '*' : '') + '</span>';
}

function formatPsar(d) {
    if (!d) return '--';
    var cls = d.direction === 'long' ? 'ext-bull' : 'ext-bear';
    var arrow = d.direction === 'long' ? '\u25B2' : '\u25BC';
    return '<span class="' + cls + '">' + arrow + (d.flip ? '*' : '') + '</span>';
}

function formatEmaCross(d) {
    if (!d) return '--';
    var cls = d.state === 'bullish' ? 'ext-bull' : 'ext-bear';
    var arrow = d.state === 'bullish' ? '\u25B2' : '\u25BC';
    var bars = d.bars_since_cross != null ? d.bars_since_cross + 'b' : '';
    return '<span class="' + cls + '">' + arrow + bars + '</span>';
}

function formatAlligator(d) {
    if (!d) return '--';
    if (d.state === 'sleeping') return '<span class="ext-neutral">Sleep</span>';
    var cls = d.state === 'eating_bullish' ? 'ext-bull' : d.state === 'eating_bearish' ? 'ext-bear' : 'ext-neutral';
    var label = d.state === 'eating_bullish' ? 'Eat\u25B2' : d.state === 'eating_bearish' ? 'Eat\u25BC' : 'Mixed';
    return '<span class="' + cls + '">' + label + '</span>';
}

function formatStochRsi(d) {
    if (!d) return '--';
    var cls = d.zone === 'oversold' ? 'ext-bull' : d.zone === 'overbought' ? 'ext-bear' : '';
    var k = d.k != null ? _fmtN(d.k, 0) : '?';
    var dv = d.d != null ? _fmtN(d.d, 0) : '?';
    return '<span class="' + cls + '">' + k + '/' + dv + '</span>';
}

function formatTtmSqueeze(d) {
    if (!d) return '--';
    if (!d.active) return '<span class="ext-neutral">No</span>';
    var cls = d.momentum === 'bullish' || d.momentum === 'bullish_fading' ? 'ext-bull' : 'ext-bear';
    return '<span class="squeeze-active ' + cls + '">\u25CF ' + d.bars + 'b</span>';
}

function formatDonchian(d) {
    if (!d) return '--';
    if (!d.breakout) return '<span class="ext-neutral">Range</span>';
    var cls = d.breakout === 'bullish' ? 'ext-bull' : 'ext-bear';
    var arrow = d.breakout === 'bullish' ? '\u25B2' : '\u25BC';
    return '<span class="' + cls + '">' + arrow + 'Brk</span>';
}

function formatNr7(d) {
    if (!d) return '--';
    return d.active ? '<span class="ext-warn">\u25CF</span>' : '<span class="ext-neutral">--</span>';
}

function formatBbwPct(d) {
    if (!d) return '--';
    var cls = d.compression ? 'ext-warn' : '';
    return '<span class="' + cls + '">' + _fmtN(d.percentile, 0) + '%</span>';
}

function formatAtrPctRank(d) {
    if (!d) return '--';
    var cls = d.low_volatility ? 'ext-warn' : '';
    return '<span class="' + cls + '">' + _fmtN(d.percentile, 0) + '%</span>';
}

function formatObv(d) {
    if (!d || !d.trend) return '--';
    var cls = d.trend === 'rising' ? 'ext-bull' : d.trend === 'falling' ? 'ext-bear' : 'ext-neutral';
    var div = d.divergence && d.divergence !== 'none' ? ' D' : '';
    return '<span class="' + cls + '">' + d.trend.charAt(0).toUpperCase() + d.trend.slice(1) + div + '</span>';
}

function formatFvg(d) {
    if (!d) return '--';
    return '<span>' + (d.bullish_count || 0) + 'B/' + (d.bearish_count || 0) + 'S</span>';
}

function formatVpLevels(vp) {
    if (!vp || !vp.poc) return '--';
    return '<span>POC ' + formatPrice(vp.poc, '', '') + '</span>';
}

// RSI divergence types (verified): bull, bear, hidden_bull, hidden_bear.
var _rsiDivGlyph = { bull: '↑', bear: '↓', hidden_bull: 'h↑', hidden_bear: 'h↓' };
var _rsiDivLabel = { bull: 'bullish', bear: 'bearish', hidden_bull: 'hidden bullish', hidden_bear: 'hidden bearish' };
var _rsiDivClass = { bull: 'ext-bull', bear: 'ext-bear', hidden_bull: 'ext-bull', hidden_bear: 'ext-bear' };

function formatRsiDiv(div) {
    if (!div) return '--';
    // Handle both formats: {daily: 'bull', weekly: 'none'} or {daily: {type: 'bullish'}, weekly: {type: 'none'}}
    var d = typeof div.daily === 'object' ? (div.daily || {}).type || 'none' : (div.daily || 'none');
    var w = typeof div.weekly === 'object' ? (div.weekly || {}).type || 'none' : (div.weekly || 'none');
    var parts = [];
    var titleParts = [];
    if (d && d !== 'none' && _rsiDivGlyph[d]) {
        parts.push('D:<span class="' + _rsiDivClass[d] + '">' + _rsiDivGlyph[d] + '</span>');
        titleParts.push('Daily: ' + _rsiDivLabel[d]);
    }
    if (w && w !== 'none' && _rsiDivGlyph[w]) {
        parts.push('W:<span class="' + _rsiDivClass[w] + '">' + _rsiDivGlyph[w] + '</span>');
        titleParts.push('Weekly: ' + _rsiDivLabel[w]);
    }
    if (!parts.length) return '<span class="ext-neutral">--</span>';
    var title = 'RSI divergence' + (titleParts.length ? ' — ' + titleParts.join(', ') : '');
    return '<span title="' + title + '">' + parts.join(' ') + '</span>';
}
