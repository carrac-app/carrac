/**
 * Indicator Pack v5
 * JavaScript implementation for KlineChart
 *
 * Includes:
 * - Moving Averages: Daily EMA 21/50/200, Weekly MA 50/100/200, CM Ultimate MA
 * - Channels & Bands: Bollinger Bands, Keltner Tight
 * - Trend Indicators: Williams Alligator, EMA Ribbon
 * - Special Indicators: Pi Cycle Top, TTM Squeeze, AutoFib, CPR
 * - Donchian Channels, Parabolic SAR
 */

// ============================================================================
// Log-scale helper — transforms raw price to ln() space when log scale is active
// ============================================================================
function _logPrice(price) {
    return (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && price > 0) ? Math.log(price) : price;
}

// ============================================================================
// Configuration
// ============================================================================

window.indicatorPackConfig = {
    // Daily EMAs
    dailyEma: {
        enabled: false,
        ema21: { enabled: true, color: '#00FFFF', lineWidth: 1 },
        ema50: { enabled: true, color: '#FF00FF', lineWidth: 1 },
        ema200: { enabled: true, color: '#FFFF00', lineWidth: 2 }
    },

    // Weekly MAs
    weeklyMa: {
        enabled: false,
        ma50: { enabled: true, color: '#00FF00', lineWidth: 2 },
        ma100: { enabled: true, color: '#FF6600', lineWidth: 2 },
        ma200: { enabled: true, color: '#FF0000', lineWidth: 2 }
    },

    // Bull Market Support Band (21W EMA + 20W SMA)
    bullMarketBand: {
        enabled: false,
        ema21w: { color: '#00FFAA', lineWidth: 2 },  // Teal-green
        sma20w: { color: '#FF6B6B', lineWidth: 2 }   // Soft red
    },

    // CM Ultimate MA
    cmUltimate: {
        enabled: false,
        length: 20,
        src: 'close',
        fillColor: '#2196F3',
        upColor: '#00E676',
        downColor: '#FF5252'
    },

    // There is no `bollingerBands` PACK indicator: it has no PARAM_SCHEMAS
    // entry, so there is no UI path to enable or configure it. Keltner/TTM
    // Squeeze use `calculateStdDev` directly, a separate shared primitive. Note: alert-panel.js's
    // `_getBBParams()` still defensively reads
    // `window.indicatorPackConfig.bollingerBands` but already falls back to
    // identical hardcoded defaults (period=20, std_dev=2.0, offset=0) when
    // that's undefined, so removing this block doesn't change its behavior.

    // Keltner Tight (EMA 35 + ATR 14 × 0.5 — tighter channel for mean-reversion)
    keltnerTight: {
        enabled: false,
        emaLength: 35,
        atrLength: 14,
        mult: 0.5,
        basisColor: '#9E9E9E',
        upperColor: '#9E9E9E',
        lowerColor: '#9E9E9E',
        fillOpacity: 0.15
    },

    // Donchian Channels (highest high / lowest low over N periods)
    donchian: {
        enabled: false,
        period: 20,
        upperColor: '#26a69a',     // Green for upper channel
        lowerColor: '#ef5350',     // Red for lower channel
        middleColor: '#9E9E9E',    // Gray for midline
        fillOpacity: 0.05,
        showMiddle: true
    },

    // Parabolic SAR (dot overlay)
    parabolicSar: {
        enabled: false,
        afStart: 0.02,
        afStep: 0.02,
        afMax: 0.20,
        bullColor: '#26a69a',   // Green dots (SAR below price = uptrend)
        bearColor: '#ef5350',   // Red dots (SAR above price = downtrend)
        dotSize: 3
    },

    // Keltner Channels (EMA + ATR envelope)
    keltner: {
        enabled: false,
        emaLength: 20,
        atrLength: 10,
        mult: 1.5,
        basisColor: '#2196F3',
        upperColor: '#FF9800',
        lowerColor: '#FF9800',
        fillOpacity: 0.05
    },

    // Hull Moving Average
    hullMa: {
        enabled: false,
        period: 20,
        color: '#E040FB',     // Magenta
        lineWidth: 2
    },

    // EMA Cross Signals (fast/slow crossover markers)
    emaCross: {
        enabled: false,
        fastPeriod: 9,
        slowPeriod: 21,
        bullColor: '#26a69a',
        bearColor: '#ef5350',
        fastColor: '#00BCD4',    // Cyan fast line
        slowColor: '#FF9800',    // Orange slow line
        showLines: true
    },

    // ALMA (Arnaud Legoux Moving Average)
    alma: {
        enabled: false,
        period: 20,
        offset: 0.85,    // Gaussian offset (0-1), higher = less lag
        sigma: 6,         // Gaussian width
        color: '#00BCD4', // Cyan
        lineWidth: 2
    },

    // Ichimoku Cloud
    ichimoku: {
        enabled: false,
        tenkanLen: 9,
        kijunLen: 26,
        senkouBLen: 52,
        displacement: 26,
        tenkanColor: '#2962FF',     // Blue
        kijunColor: '#B71C1C',      // Dark red
        senkouAColor: '#26A69A',    // Teal/green
        senkouBColor: '#EF5350',    // Red
        chikouColor: '#7B1FA2',     // Purple
        cloudOpacity: 0.12
    },

    // Gaussian Channel (Ehlers Gaussian Filter + filtered true range)
    gaussianChannel: {
        enabled: false,
        poles: 4,
        period: 144,
        mult: 1.414,
        reducedLag: false,
        fastResponse: false,
        bullColor: '#0aff68',
        bearColor: '#ff0a5a',
        neutralColor: '#cccccc',
        fillColor: '#4dd0e1',
        fillOpacity: 0.08
    },

    // KAMA (Kaufman Adaptive Moving Average)
    kama: {
        enabled: false,
        period: 10,
        fastPeriod: 2,
        slowPeriod: 30,
        color: '#FFD600', // Yellow
        lineWidth: 2
    },

    // SuperTrend (ATR-based trend line)
    superTrend: {
        enabled: false,
        period: 10,
        mult: 3.0,
        bullColor: '#26a69a',
        bearColor: '#ef5350',
        lineWidth: 2
    },

    // Williams Alligator
    alligator: {
        enabled: false,
        jawLength: 13, jawOffset: 8, jawColor: '#2196F3',
        teethLength: 8, teethOffset: 5, teethColor: '#FF5252',
        lipsLength: 5, lipsOffset: 3, lipsColor: '#00E676'
    },

    // EMA Ribbon (30/60 EMA with ATR-margin trend coloring)
    emaRibbon: {
        enabled: false,
        fastEmaLen: 30,      // Fast EMA period
        slowEmaLen: 60,      // Slow EMA period (both are EMAs!)
        atrLen: 60,          // ATR period for margin check
        atrMult: 0.30,       // ATR multiplier for bull/bear threshold
        bullColor: '#7cb518',       // Green when bullish
        bearColor: '#fb6107',       // Orange when bearish
        neutralColor: '#f3de2c',    // Yellow when neutral
        fillOpacity: 0.1            // Fill opacity between lines
    },

    // Consensio (SMA 3/7/30 with signal arrows and multi-TF table)
    consensio: {
        enabled: false,
        periodS: 3,
        periodM: 7,
        periodL: 30,
        shortColor: '#00E676',   // Green (short MA)
        mediumColor: '#FFEB3B',  // Yellow (medium MA)
        longColor: '#FF5252',    // Red (long MA)
        bullColor: '#26A69A',    // Signal arrow color
        bearColor: '#EF5350',
        lineWidth: 1,
        showTable: true
    },

    // Consensio Lite (numbers + table only, no SMA lines)
    consensioLite: {
        enabled: false
    },

    // Pi Cycle Top
    piCycle: {
        enabled: false,
        ma111Color: '#00E676',
        ma350x2Color: '#FF5252'
    },

    // TTM Squeeze (Pine Script v4 params: BB(30,2.0), KC(30,2.5), duration >= 3)
    ttmSqueeze: {
        enabled: false,
        bbLength: 30,
        bbMult: 2.0,
        kcLength: 30,
        kcMult: 2.5,           // Pine uses 2.5 for Keltner Channel
        squeezeOnColor: '#FFFF00',  // Yellow when squeeze is ON
        minDuration: 3              // Consecutive bars filter
    },

    // Auto Fibonacci (Pine Script: fib_length = 265)
    autoFib: {
        enabled: false,
        lookback: 265,
        levels: [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1],
        colors: ['#FF5252', '#FF9800', '#FFEB3B', '#00E676', '#2196F3', '#9C27B0', '#FF5252']
    },

    // CPR (Central Pivot Range) - TradingView style
    cpr: {
        enabled: false,
        timeframe: 'monthly',  // daily, weekly, monthly, quarterly, yearly
        // Colors matching TradingView CPR indicator
        pivotColor: '#9C27B0',      // Purple for Pivot/TC/BC
        tcColor: '#9C27B0',         // Purple
        bcColor: '#9C27B0',         // Purple
        fillColor: 'rgba(156, 39, 176, 0.1)',  // Purple fill between TC/BC
        rColor: '#00E676',          // Green for all R levels
        sColor: '#FF5252',          // Red for all S levels
        lineStyle: 'dotted'         // circles style like TradingView
    },

    // CPR History — past months' TC/P/BC lines (bounded per month)
    cprHistory: {
        enabled: false,
        months: 48,
        pivotColor: 'rgba(156, 39, 176, 0.6)',
        lineWidth: 1,
        lineStyle: 'dashed'
    },
    // Weekly CPR
    weeklyCpr: {
        enabled: false,
        pivotColor: '#2196F3', tcColor: '#2196F3', bcColor: '#2196F3',
        fillColor: 'rgba(33, 150, 243, 0.1)',
        rColor: '#00E676', sColor: '#FF5252', lineStyle: 'dotted'
    },
    weeklyCprHistory: {
        enabled: false,
        pivotColor: 'rgba(33, 150, 243, 0.45)', lineWidth: 1, lineStyle: 'dashed'
    },
    // Daily CPR
    dailyCpr: {
        enabled: false,
        pivotColor: '#FF9800', tcColor: '#FF9800', bcColor: '#FF9800',
        fillColor: 'rgba(255, 152, 0, 0.1)',
        rColor: '#00E676', sColor: '#FF5252', lineStyle: 'dotted'
    },
    dailyCprHistory: {
        enabled: false,
        pivotColor: 'rgba(255, 152, 0, 0.45)', lineWidth: 1, lineStyle: 'dashed'
    },

    // Key Levels (IDWM - Intraday, Daily, Weekly, Monthly)
    keyLevels: {
        enabled: false,
        shorthand: true,  // Use short labels (DO, WO, MO) vs full (Daily Open, Weekly Open)
        lineStyle: 'solid', // solid, dashed, dotted
        lineWidth: 1,
        // Daily levels
        daily: {
            open: { enabled: true, color: '#08bcd4' },
            prevHigh: { enabled: false, color: '#08bcd4' },
            prevLow: { enabled: false, color: '#08bcd4' },
            prevMid: { enabled: false, color: '#08bcd4' }
        },
        // Weekly levels
        weekly: {
            open: { enabled: true, color: '#fffcbc' },
            prevHigh: { enabled: true, color: '#fffcbc' },
            prevLow: { enabled: true, color: '#fffcbc' },
            prevMid: { enabled: true, color: '#fffcbc' }
        },
        // Monthly levels
        monthly: {
            open: { enabled: true, color: '#08d48c' },
            prevHigh: { enabled: true, color: '#08d48c' },
            prevLow: { enabled: true, color: '#08d48c' },
            prevMid: { enabled: true, color: '#08d48c' }
        },
        // Quarterly levels
        quarterly: {
            open: { enabled: true, color: '#FF5252' },
            prevMid: { enabled: true, color: '#FF5252' }
        },
        // Yearly levels
        yearly: {
            open: { enabled: true, color: '#FF0000' },
            currentMid: { enabled: true, color: '#FF0000' }
        }
    },

    // Support Resistance Channels (LonesomeTheBlue)
    srChannels: {
        enabled: false,
        pivotPeriod: 10,        // Checks left & right bars for pivots
        source: 'High/Low',     // 'High/Low' or 'Close/Open'
        channelWidth: 5,        // Maximum channel width as % of price range
        minStrength: 1,         // Minimum strength (pivot points in channel)
        maxChannels: 6,         // Maximum number of S/R channels to show
        loopback: 290,          // How far back to check for pivots
        resistanceColor: 'rgba(255, 0, 0, 0.25)',    // Red with transparency
        supportColor: 'rgba(0, 255, 0, 0.25)',       // Green with transparency
        insideColor: 'rgba(128, 128, 128, 0.25)',    // Gray when price inside
        showPivots: false,      // Show H/L pivot labels
        showBroken: false       // Show broken S/R markers
    },

    // Fair Value Gap (SpaceManBTC) — enhanced with auto-threshold + mitigation tracking
    fvg: {
        enabled: false,
        fillByMid: false,           // FVG filled when midpoint is tested
        deleteOnFill: false,        // Keep mitigated FVGs visible (dimmed)
        showMidLine: true,          // Show midpoint line
        showMitigated: true,        // Show mitigated FVGs with reduced opacity
        autoThreshold: true,        // Filter out FVGs smaller than atrThresholdPct of ATR
        atrThresholdPct: 0.25,      // Min FVG size as fraction of ATR (0.25 = 25% of ATR)
        atrLength: 14,              // ATR period for threshold calculation
        maxGaps: 50,                // Maximum number of gaps to display
        bullishColor: 'rgba(0, 255, 0, 0.15)',      // Green for bullish FVG
        bearishColor: 'rgba(255, 0, 0, 0.15)',      // Red for bearish FVG
        mitigatedOpacity: 0.4,      // Opacity multiplier for mitigated FVGs
        midLineColor: 'rgba(255, 255, 255, 0.3)',  // White for midpoint line
        borderColor: 'rgba(255, 255, 255, 0.2)'    // Border color
    },

    // SMA 20/200 overlay
    smaSignal: {
        enabled: false,
        sma20Color: '#00BFFF',    // Deep sky blue for SMA 20
        sma200Color: '#FFD700',   // Gold for SMA 200
        lineWidth: 2
    },

    // RSI Divergence
    rsiDivergence: {
        enabled: false,
        period: 14,
        lookback: 5,              // bars left/right for swing detection (matches TradingView)
        maxBarsBetween: 60,       // max bars between swing points (TradingView rangeUpper)
        bullColor: '#26a69a',     // green - regular bullish
        bearColor: '#ef5350',     // red - regular bearish
        hiddenBullColor: '#26a69a', // green - hidden bullish (dashed)
        hiddenBearColor: '#ef5350', // red - hidden bearish (dashed)
        lineWidth: 2
    },

    cvdDivergence: {
        enabled: false,
        lookback: 5,
        maxBarsBetween: 60,
        bullColor: '#26a69a',
        bearColor: '#ef5350',
        hiddenBullColor: '#26a69a',
        hiddenBearColor: '#ef5350',
        lineWidth: 2
    },

    // BB Width Percentile — flags extreme compression (BB width in bottom 20% of 120-bar history)
    bbwPct: {
        enabled: false,
        bbLength: 20,
        lookback: 120,
        threshold: 20,            // percentile below which = compression
        color: '#00BCD4'          // Cyan
    },

    // ATR Percentile — flags low-volatility bars (ATR in bottom 20% of 100-bar history)
    atrPct: {
        enabled: false,
        atrLength: 14,
        lookback: 100,
        threshold: 20,
        color: '#E040FB'          // Magenta
    },

    // NR7 — Narrowest Range of 7 bars
    nr7: {
        enabled: false,
        lookback: 7,
        color: '#FFB300'          // Amber
    },

    // Market Sessions — trading session overlay at bottom of candle pane
    sessions: {
        enabled: false,
        rectHeight: 5,           // Rectangle height in pixels
        bottomMargin: 6,         // Distance from bottom of candle pane
        labelSize: 10,           // Session label font size
        labelColor: '#9ca3af',   // Label text color
        futureOpacity: 0.35,     // Opacity for projected future sessions
        // Session phase colors (gap + dead = null → transparent)
        colors: {
            asia:   { session: '#6b7280', extension: '#60a5fa' },
            uk:     { session: '#9ca3af', extension: '#60a5fa' },
            us:     { session: '#9ca3af', reversal: '#f59e0b' }
        },
        // Holiday indicator
        holidayColor: '#f59e0b'  // Amber for holidays
    }
};

// ============================================================================
// Utility Functions
// ============================================================================

function calculateEMA(data, period, priceKey = 'close') {
    if (!data || data.length < period) return [];

    const k = 2 / (period + 1);
    const ema = [];

    // Initialize with SMA
    let sum = 0;
    for (let i = 0; i < period; i++) {
        sum += data[i][priceKey];
    }
    ema[period - 1] = sum / period;

    // Calculate EMA
    for (let i = period; i < data.length; i++) {
        ema[i] = data[i][priceKey] * k + ema[i - 1] * (1 - k);
    }

    return ema;
}

function calculateSMA(data, period, priceKey = 'close') {
    if (!data || data.length < period) return [];

    const sma = [];
    let sum = 0;

    for (let i = 0; i < data.length; i++) {
        sum += data[i][priceKey];
        if (i >= period) {
            sum -= data[i - period][priceKey];
        }
        if (i >= period - 1) {
            sma[i] = sum / period;
        }
    }

    return sma;
}

function calculateSMMA(data, period, priceKey = 'close') {
    if (!data || data.length < period) return [];

    const smma = [];
    let sum = 0;

    // First SMMA = SMA
    for (let i = 0; i < period; i++) {
        sum += data[i][priceKey];
    }
    smma[period - 1] = sum / period;

    // Subsequent values
    for (let i = period; i < data.length; i++) {
        smma[i] = (smma[i - 1] * (period - 1) + data[i][priceKey]) / period;
    }

    return smma;
}

function calculateATR(data, period) {
    if (!data || data.length < period + 1) return [];

    const tr = [];
    const atr = [];

    // Calculate True Range
    for (let i = 0; i < data.length; i++) {
        if (i === 0) {
            tr[i] = data[i].high - data[i].low;
        } else {
            const hl = data[i].high - data[i].low;
            const hc = Math.abs(data[i].high - data[i - 1].close);
            const lc = Math.abs(data[i].low - data[i - 1].close);
            tr[i] = Math.max(hl, hc, lc);
        }
    }

    // Calculate ATR using RMA
    let sum = 0;
    for (let i = 0; i < period; i++) {
        sum += tr[i];
    }
    atr[period - 1] = sum / period;

    for (let i = period; i < data.length; i++) {
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period;
    }

    return atr;
}

function calculateStdDev(data, period, priceKey = 'close') {
    if (!data || data.length < period) return [];

    const sma = calculateSMA(data, period, priceKey);
    const stdDev = [];

    for (let i = period - 1; i < data.length; i++) {
        let sumSquares = 0;
        for (let j = 0; j < period; j++) {
            const diff = data[i - j][priceKey] - sma[i];
            sumSquares += diff * diff;
        }
        stdDev[i] = Math.sqrt(sumSquares / period);
    }

    return stdDev;
}

// ============================================================================
// Moving Averages
// ============================================================================

// Infer the loaded chart's bar spacing directly from the data
// (median gap between consecutive bars, robust to weekend/exchange gaps)
// rather than trusting the mutable `klineChartTimeframe` global (which has
// known case-canonicalization gotchas, e.g. '1m' vs '1M'). Used to decide
// whether a "daily"/"weekly" resample is identity, a real resample, or not
// meaningful at all for the loaded granularity.
function _inferBarIntervalMs(data) {
    if (!data || data.length < 2) return 0;
    function toMs(ts) { return ts < 1e12 ? ts * 1000 : ts; }
    var diffs = [];
    for (var i = 1; i < data.length; i++) {
        var d = toMs(data[i].timestamp) - toMs(data[i - 1].timestamp);
        if (d > 0) diffs.push(d);
    }
    if (diffs.length === 0) return 0;
    diffs.sort(function (a, b) { return a - b; });
    return diffs[Math.floor(diffs.length / 2)];
}

// Same Monday-start ISO week-year/week-number algorithm as the local helpers
// in calculateCPR/calculateKeyLevels, reused here (module-level,
// distinct names to avoid shadowing those functions' own nested copies) so
// "weekly" buckets line up with the rest of the chart's weekly period logic.
function _isoWeekNumber(d) {
    const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const dayNum = date.getUTCDay() || 7;
    date.setUTCDate(date.getUTCDate() + 4 - dayNum);
    const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
    return Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
}
function _isoWeekYear(d) {
    const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const dayNum = date.getUTCDay() || 7;
    date.setUTCDate(date.getUTCDate() + 4 - dayNum);
    return date.getUTCFullYear();
}

function calculateDailyEMAs(data) {
    const config = indicatorPackConfig.dailyEma;
    const empty = { ema21: [], ema50: [], ema200: [] };
    if (!data || data.length === 0) return empty;

    var dayMs = 86400000;
    var barMs = _inferBarIntervalMs(data);

    // Already-daily bars: EMA directly on the loaded series — identity
    // resample, same behavior as before.
    if (barMs >= dayMs * 0.75 && barMs <= dayMs * 1.5) {
        return {
            ema21: config.ema21.enabled ? calculateEMA(data, 21) : [],
            ema50: config.ema50.enabled ? calculateEMA(data, 50) : [],
            ema200: config.ema200.enabled ? calculateEMA(data, 200) : []
        };
    }

    // Coarser than daily (3D/1W/1M and beyond) — a "daily EMA" can't be
    // reconstructed from bars that already span multiple days each; skip
    // rendering entirely rather than fabricate a misleading line.
    if (barMs > dayMs * 1.5) {
        return null;
    }

    // Intraday bars (< 1 day): resample into UTC daily buckets — O=first,
    // H=max, L=min, C=last — including the forming (in-progress) last day.
    // This mirrors the server's weekly MAs, which resample via pandas
    // `.resample('1W')` — that includes the current partial bin rather than
    // dropping it, so the in-progress day/week updates live like any other
    // period-anchored indicator on this chart (CPR/key levels do the same).
    function toMs(ts) { return ts < 1e12 ? ts * 1000 : ts; }
    function dayKeyOf(ts) { return Math.floor(toMs(ts) / dayMs); }

    var buckets = [];
    for (var i = 0; i < data.length; i++) {
        var dk = dayKeyOf(data[i].timestamp);
        var last = buckets.length ? buckets[buckets.length - 1] : null;
        if (!last || last.dayKey !== dk) {
            buckets.push({ dayKey: dk, open: data[i].open, high: data[i].high, low: data[i].low, close: data[i].close, startIdx: i, endIdx: i });
        } else {
            if (data[i].high > last.high) last.high = data[i].high;
            if (data[i].low < last.low) last.low = data[i].low;
            last.close = data[i].close;
            last.endIdx = i;
        }
    }
    if (buckets.length === 0) return empty;

    var dailyCloses = buckets.map(function (b) { return { close: b.close }; });
    var out = { ema21: [], ema50: [], ema200: [] };
    var periods = [['ema21', 21], ['ema50', 50], ['ema200', 200]];
    for (var p = 0; p < periods.length; p++) {
        var key = periods[p][0], period = periods[p][1];
        if (!config[key].enabled) continue;
        if (buckets.length < period) continue; // not enough daily buckets yet — omit this line
        var dailyEma = calculateEMA(dailyCloses, period, 'close');
        var mapped = new Array(data.length);
        for (var b = 0; b < buckets.length; b++) {
            var val = dailyEma[b];
            if (val === undefined) continue;
            for (var idx = buckets[b].startIdx; idx <= buckets[b].endIdx; idx++) {
                mapped[idx] = val;
            }
        }
        out[key] = mapped;
    }
    return out;
}

function drawServerWeeklyMA(chart, weeklyMaPoints, color, id, lineWidth, data, groupId) {
    // Draw sparse weekly MA with linear interpolation between weekly points.
    // Values at weekly boundaries stay identical — lines connect them diagonally.
    if (!weeklyMaPoints || weeklyMaPoints.length < 2 || !data || data.length === 0) return;

    var firstBarMs = data[0].timestamp;
    var lastBarMs = data[data.length - 1].timestamp;
    var gid = groupId || 'indicatorPack';

    // Filter to points relevant to chart range (with 1 week buffer)
    var bufferMs = 604800 * 1000;
    var relevant = [];
    for (var i = 0; i < weeklyMaPoints.length; i++) {
        var ptMs = weeklyMaPoints[i].time * 1000;
        if (ptMs >= firstBarMs - bufferMs && ptMs <= lastBarMs + bufferMs) {
            relevant.push({ timestamp: ptMs, value: _logPrice(weeklyMaPoints[i].value) });
        }
    }
    // Also include the last point before chart range for continuity
    for (var j = weeklyMaPoints.length - 1; j >= 0; j--) {
        var tMs = weeklyMaPoints[j].time * 1000;
        if (tMs < firstBarMs - bufferMs) {
            relevant.unshift({ timestamp: tMs, value: _logPrice(weeklyMaPoints[j].value) });
            break;
        }
    }

    if (relevant.length < 1) return;

    // Draw linear segments between consecutive weekly points
    for (var k = 0; k < relevant.length - 1; k++) {
        chart.createOverlay({
            name: 'segment',
            id: 'ind_' + id + '_' + k,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: relevant[k].timestamp, value: relevant[k].value },
                { timestamp: relevant[k + 1].timestamp, value: relevant[k + 1].value }
            ],
            styles: { line: { color: color, size: lineWidth } }
        });
    }

    // Extend last value flat to end of chart data
    if (relevant.length > 0) {
        var last = relevant[relevant.length - 1];
        if (last.timestamp < lastBarMs) {
            chart.createOverlay({
                name: 'segment',
                id: 'ind_' + id + '_ext',
                groupId: gid,
                lock: true,
                points: [
                    { timestamp: last.timestamp, value: last.value },
                    { timestamp: lastBarMs, value: last.value }
                ],
                styles: { line: { color: color, size: lineWidth } }
            });
        }
    }
}

function calculateWeeklyMAs(data) {
    // Fallback (primary path is the server's weekly MAs — see drawSingleIndicator's
    // 'weeklyMa' case): real ISO-week resampling (Monday-start UTC weeks) of the
    // loaded candles, SMA(50/100/200) on weekly closes, step-mapped back to bars.
    const config = indicatorPackConfig.weeklyMa;
    const empty = { ma50: [], ma100: [], ma200: [] };
    if (!data || data.length === 0) return empty;

    // Skip when the loaded TF is coarser than 1W (effectively 1M-scale bars) —
    // a single bar can't be meaningfully assigned to one ISO week.
    var dayMs = 86400000;
    var barMs = _inferBarIntervalMs(data);
    if (barMs > dayMs * 8) return empty;

    function toMs(ts) { return ts < 1e12 ? ts * 1000 : ts; }

    var buckets = [];
    for (var i = 0; i < data.length; i++) {
        var d = new Date(toMs(data[i].timestamp));
        var isoYear = _isoWeekYear(d);
        var week = _isoWeekNumber(d);
        var last = buckets.length ? buckets[buckets.length - 1] : null;
        if (!last || last.isoYear !== isoYear || last.week !== week) {
            buckets.push({ isoYear: isoYear, week: week, close: data[i].close, startIdx: i, endIdx: i });
        } else {
            last.close = data[i].close;
            last.endIdx = i;
        }
    }
    if (buckets.length === 0) return empty;

    var weeklyCloses = buckets.map(function (b) { return { close: b.close }; });
    var out = { ma50: [], ma100: [], ma200: [] };
    var periods = [['ma50', 50], ['ma100', 100], ['ma200', 200]];
    for (var p = 0; p < periods.length; p++) {
        var key = periods[p][0], period = periods[p][1];
        if (!config[key].enabled) continue;
        if (buckets.length < period) continue; // not enough weekly buckets yet — omit this line
        var sma = calculateSMA(weeklyCloses, period, 'close');
        var mapped = new Array(data.length);
        for (var b = 0; b < buckets.length; b++) {
            var val = sma[b];
            if (val === undefined) continue;
            for (var idx = buckets[b].startIdx; idx <= buckets[b].endIdx; idx++) {
                mapped[idx] = val;
            }
        }
        out[key] = mapped;
    }
    return out;
}

function calculateCMUltimateMA(data) {
    const config = indicatorPackConfig.cmUltimate;
    const len = config.length;

    // Calculate VWMA-like weighted average
    const maValues = [];
    const maColors = [];

    for (let i = len - 1; i < data.length; i++) {
        let sumPV = 0;
        let sumV = 0;

        for (let j = 0; j < len; j++) {
            const idx = i - j;
            const vol = data[idx].volume || 1;
            sumPV += data[idx].close * vol;
            sumV += vol;
        }

        const ma = sumPV / sumV;
        maValues[i] = ma;

        // Trend color
        if (i > 0 && maValues[i - 1] !== undefined) {
            maColors[i] = ma > maValues[i - 1] ? config.upColor : config.downColor;
        } else {
            maColors[i] = config.fillColor;
        }
    }

    return { values: maValues, colors: maColors };
}

// ============================================================================
// Channels & Bands
// ============================================================================

// ============================================================================
// Donchian Channels & Parabolic SAR
// ============================================================================

function calculateDonchian(data) {
    var period = indicatorPackConfig.donchian.period;
    var upper = new Array(data.length);
    var lower = new Array(data.length);
    var middle = new Array(data.length);

    for (var i = period - 1; i < data.length; i++) {
        var hh = -Infinity, ll = Infinity;
        for (var j = i - period + 1; j <= i; j++) {
            if (data[j].high > hh) hh = data[j].high;
            if (data[j].low < ll) ll = data[j].low;
        }
        upper[i] = hh;
        lower[i] = ll;
        middle[i] = (hh + ll) / 2;
    }

    return { upper: upper, lower: lower, middle: middle };
}

function calculateParabolicSAR(data) {
    var cfg = indicatorPackConfig.parabolicSar;
    var afStart = cfg.afStart;
    var afStep = cfg.afStep;
    var afMax = cfg.afMax;
    var n = data.length;
    if (n < 2) return { sar: [], direction: [] };

    var sar = new Array(n);
    var direction = new Array(n); // 1 = long (SAR below), -1 = short (SAR above)

    // Initialize: start long if second bar closes higher, else short
    var isLong = data[1].close >= data[0].close;
    var af = afStart;
    var ep = isLong ? data[0].high : data[0].low;
    sar[0] = isLong ? data[0].low : data[0].high;
    direction[0] = isLong ? 1 : -1;

    for (var i = 1; i < n; i++) {
        var newSar = sar[i - 1] + af * (ep - sar[i - 1]);

        if (isLong) {
            // Clamp SAR to not go above prior two bars' lows
            if (i >= 2) newSar = Math.min(newSar, data[i - 1].low, data[i - 2].low);
            else newSar = Math.min(newSar, data[i - 1].low);

            if (newSar > data[i].low) {
                // Flip to short
                isLong = false;
                newSar = ep;
                af = afStart;
                ep = data[i].low;
            } else {
                if (data[i].high > ep) {
                    ep = data[i].high;
                    af = Math.min(af + afStep, afMax);
                }
            }
        } else {
            // Clamp SAR to not go below prior two bars' highs
            if (i >= 2) newSar = Math.max(newSar, data[i - 1].high, data[i - 2].high);
            else newSar = Math.max(newSar, data[i - 1].high);

            if (newSar < data[i].high) {
                // Flip to long
                isLong = true;
                newSar = ep;
                af = afStart;
                ep = data[i].high;
            } else {
                if (data[i].low < ep) {
                    ep = data[i].low;
                    af = Math.min(af + afStep, afMax);
                }
            }
        }

        sar[i] = newSar;
        direction[i] = isLong ? 1 : -1;
    }

    return { sar: sar, direction: direction };
}

function drawParabolicSARDots(chart, data, sarResult, groupId) {
    var cfg = indicatorPackConfig.parabolicSar;
    var sar = sarResult.sar;
    var dir = sarResult.direction;
    var gid = groupId || 'indicatorPack';

    // Collect points with their colors
    var pts = [];
    for (var i = 0; i < data.length; i++) {
        if (sar[i] === undefined) continue;
        pts.push({
            timestamp: data[i].timestamp,
            value: sar[i],
            bull: dir[i] === 1
        });
    }
    if (pts.length === 0) return;

    // Subsample for performance (~300 dots max)
    var step = Math.max(1, Math.floor(pts.length / 300));
    var sampled = [];
    for (var j = 0; j < pts.length; j += step) {
        sampled.push(pts[j]);
    }
    if (sampled[sampled.length - 1] !== pts[pts.length - 1]) {
        sampled.push(pts[pts.length - 1]);
    }

    // Single overlay with onDrawing for batch dot rendering
    chart.createOverlay({
        name: 'simpleAnnotation',
        id: 'ind_psar_dots',
        groupId: gid,
        lock: true,
        visible: true,
        points: [{ timestamp: sampled[0].timestamp, value: sampled[0].value }],
        extendData: { sampled: sampled, dotSize: cfg.dotSize, bullColor: cfg.bullColor, bearColor: cfg.bearColor },
        onDrawing: function(params) {
            var ctx = params.ctx;
            var overlay = params.overlay;
            var dots = overlay.extendData.sampled;
            var size = overlay.extendData.dotSize;
            var bullCol = overlay.extendData.bullColor;
            var bearCol = overlay.extendData.bearColor;

            for (var k = 0; k < dots.length; k++) {
                var px = chart.convertToPixel(
                    { timestamp: dots[k].timestamp, value: dots[k].value },
                    { paneId: 'candle_pane' }
                );
                if (!px || isNaN(px.x) || isNaN(px.y)) continue;

                ctx.beginPath();
                ctx.arc(px.x, px.y, size, 0, Math.PI * 2);
                ctx.fillStyle = dots[k].bull ? bullCol : bearCol;
                ctx.fill();
            }
        }
    });
}

// ============================================================================
// Keltner Channels, Hull MA, EMA Cross, SuperTrend
// ============================================================================

function calculateKeltner(data, configKey) {
    var cfg = indicatorPackConfig[configKey || 'keltner'];
    var ema = calculateEMA(data, cfg.emaLength);
    var atr = calculateATR(data, cfg.atrLength);
    var upper = new Array(data.length);
    var lower = new Array(data.length);

    for (var i = 0; i < data.length; i++) {
        if (ema[i] !== undefined && atr[i] !== undefined) {
            upper[i] = ema[i] + atr[i] * cfg.mult;
            lower[i] = ema[i] - atr[i] * cfg.mult;
        }
    }
    return { basis: ema, upper: upper, lower: lower };
}

function calculateWMA(data, period, priceKey) {
    priceKey = priceKey || 'close';
    if (!data || data.length < period) return [];
    var result = new Array(data.length);
    var denom = period * (period + 1) / 2;

    for (var i = period - 1; i < data.length; i++) {
        var sum = 0;
        for (var j = 0; j < period; j++) {
            sum += data[i - period + 1 + j][priceKey] * (j + 1);
        }
        result[i] = sum / denom;
    }
    return result;
}

function calculateHullMA(data) {
    var period = indicatorPackConfig.hullMa.period;
    var halfPeriod = Math.floor(period / 2);
    var sqrtPeriod = Math.round(Math.sqrt(period));

    // WMA(n/2) and WMA(n)
    var wmaHalf = calculateWMA(data, halfPeriod);
    var wmaFull = calculateWMA(data, period);

    // 2*WMA(n/2) - WMA(n) as intermediate series. Indices before validStart
    // are padded with 0 as a placeholder ONLY — the outer WMA(sqrt(n)) below
    // must never average across the boundary from padded to real values.
    var intermediate = [];
    var validStart = period - 1; // first index where wmaHalf & wmaFull are both real
    for (var i = 0; i < data.length; i++) {
        if (wmaHalf[i] !== undefined && wmaFull[i] !== undefined) {
            intermediate.push({ close: 2 * wmaHalf[i] - wmaFull[i] });
        } else {
            intermediate.push({ close: 0 });
        }
    }

    // WMA(sqrt(n)) of the intermediate
    var hma = calculateWMA(intermediate, sqrtPeriod);

    // Only emit where the outer WMA(sqrt(n)) window is built
    // EXCLUSIVELY from real intermediate values — its earliest index must
    // still be >= validStart. The previous `intermediate[j].close !== 0`
    // sentinel let the first sqrt(n)-1 bars past nominal warmup through even
    // though their window still mixed in zero-padded placeholder entries,
    // deflating those values (and a genuine zero-valued intermediate would
    // have wrongly been dropped by that same sentinel).
    var validStartOuter = validStart + sqrtPeriod - 1;
    var result = new Array(data.length);
    for (var j = 0; j < data.length; j++) {
        if (j >= validStartOuter && hma[j] !== undefined) {
            result[j] = hma[j];
        }
    }
    return result;
}

function calculateEMACross(data) {
    var cfg = indicatorPackConfig.emaCross;
    var fast = calculateEMA(data, cfg.fastPeriod);
    var slow = calculateEMA(data, cfg.slowPeriod);
    var crosses = [];

    for (var i = 1; i < data.length; i++) {
        if (fast[i] === undefined || fast[i - 1] === undefined ||
            slow[i] === undefined || slow[i - 1] === undefined) continue;

        // Bullish cross: fast crosses above slow
        if (fast[i - 1] <= slow[i - 1] && fast[i] > slow[i]) {
            crosses.push({ index: i, type: 'bull' });
        }
        // Bearish cross: fast crosses below slow
        if (fast[i - 1] >= slow[i - 1] && fast[i] < slow[i]) {
            crosses.push({ index: i, type: 'bear' });
        }
    }

    return { fast: fast, slow: slow, crosses: crosses };
}

function drawEMACrossMarkers(chart, data, crossResult, groupId) {
    var cfg = indicatorPackConfig.emaCross;
    var gid = groupId || 'indicatorPack';

    // Draw EMA lines if enabled
    if (cfg.showLines) {
        drawMALine(chart, data, crossResult.fast, cfg.fastColor, 'emaCrossFast', 1, gid);
        drawMALine(chart, data, crossResult.slow, cfg.slowColor, 'emaCrossSlow', 1, gid);
    }

    // Draw cross markers
    crossResult.crosses.forEach(function(cross, idx) {
        var d = data[cross.index];
        var isBull = cross.type === 'bull';
        var color = isBull ? cfg.bullColor : cfg.bearColor;
        var yVal = isBull ? d.low * 0.985 : d.high * 1.015;

        chart.createOverlay({
            name: 'simpleAnnotation',
            id: 'ind_emaCross_' + idx,
            groupId: gid,
            lock: true,
            points: [{ timestamp: d.timestamp, value: yVal }],
            styles: {
                symbol: {
                    type: isBull ? 'triangle' : 'diamond',
                    size: 8,
                    color: color
                }
            }
        });
    });
}

function calculateSuperTrend(data) {
    var cfg = indicatorPackConfig.superTrend;
    var period = cfg.period;
    var mult = cfg.mult;
    var atr = calculateATR(data, period);
    var n = data.length;

    var superTrend = new Array(n);
    var direction = new Array(n); // 1 = up (bull), -1 = down (bear)
    var upperBand = new Array(n);
    var lowerBand = new Array(n);

    for (var i = 0; i < n; i++) {
        if (atr[i] === undefined) continue;
        var hl2 = (data[i].high + data[i].low) / 2;
        upperBand[i] = hl2 + mult * atr[i];
        lowerBand[i] = hl2 - mult * atr[i];
    }

    // Initialize
    var firstValid = period;
    direction[firstValid] = 1;
    superTrend[firstValid] = lowerBand[firstValid];

    for (var i = firstValid + 1; i < n; i++) {
        if (upperBand[i] === undefined) continue;

        // Adjust bands based on previous values
        if (lowerBand[i - 1] !== undefined && lowerBand[i] !== undefined) {
            if (lowerBand[i] < lowerBand[i - 1] && data[i - 1].close > lowerBand[i - 1]) {
                lowerBand[i] = lowerBand[i - 1];
            }
        }
        if (upperBand[i - 1] !== undefined && upperBand[i] !== undefined) {
            if (upperBand[i] > upperBand[i - 1] && data[i - 1].close < upperBand[i - 1]) {
                upperBand[i] = upperBand[i - 1];
            }
        }

        // Determine direction
        if (direction[i - 1] === 1) {
            // Was bullish
            if (data[i].close < lowerBand[i]) {
                direction[i] = -1;
                superTrend[i] = upperBand[i];
            } else {
                direction[i] = 1;
                superTrend[i] = lowerBand[i];
            }
        } else {
            // Was bearish
            if (data[i].close > upperBand[i]) {
                direction[i] = 1;
                superTrend[i] = lowerBand[i];
            } else {
                direction[i] = -1;
                superTrend[i] = upperBand[i];
            }
        }
    }

    return { superTrend: superTrend, direction: direction };
}

function drawSuperTrendLine(chart, data, stResult, groupId) {
    var cfg = indicatorPackConfig.superTrend;
    var gid = groupId || 'indicatorPack';
    var st = stResult.superTrend;
    var dir = stResult.direction;

    // Collect segments grouped by color (direction)
    var segments = [];
    var currentSeg = null;

    for (var i = 0; i < data.length; i++) {
        if (st[i] === undefined || dir[i] === undefined) continue;
        var isBull = dir[i] === 1;
        if (!currentSeg || currentSeg.bull !== isBull) {
            // Start new segment, but connect to last point of previous
            if (currentSeg && currentSeg.points.length > 0) {
                // Add overlap point for continuity
                currentSeg.points.push({ timestamp: data[i].timestamp, value: st[i] });
            }
            currentSeg = { bull: isBull, points: [] };
            segments.push(currentSeg);
        }
        currentSeg.points.push({ timestamp: data[i].timestamp, value: st[i] });
    }

    // Draw each segment as a colored line
    segments.forEach(function(seg, idx) {
        if (seg.points.length < 2) return;
        var color = seg.bull ? cfg.bullColor : cfg.bearColor;

        // Subsample for performance
        var step = Math.max(1, Math.floor(seg.points.length / 100));
        var sampled = [];
        for (var j = 0; j < seg.points.length; j += step) {
            sampled.push(seg.points[j]);
        }
        if (sampled[sampled.length - 1] !== seg.points[seg.points.length - 1]) {
            sampled.push(seg.points[seg.points.length - 1]);
        }

        for (var k = 1; k < sampled.length; k++) {
            chart.createOverlay({
                name: 'segment',
                id: 'ind_st_' + idx + '_' + k,
                groupId: gid,
                lock: true,
                points: [
                    { timestamp: sampled[k - 1].timestamp, value: sampled[k - 1].value },
                    { timestamp: sampled[k].timestamp, value: sampled[k].value }
                ],
                styles: {
                    line: { color: color, size: cfg.lineWidth }
                }
            });
        }
    });
}

// ============================================================================
// ALMA & KAMA
// ============================================================================

function calculateALMA(data) {
    var cfg = indicatorPackConfig.alma;
    var period = cfg.period;
    var offset = cfg.offset;
    var sigma = cfg.sigma;
    var n = data.length;
    var result = new Array(n);

    if (n < period) return result;

    // Pre-compute Gaussian weights. Keep `m` fractional —
    // TradingView's ta.alma defaults to floor=false; flooring here shifts the
    // window center and skews the average versus the reference formula.
    var m = offset * (period - 1);
    var s = period / sigma;
    var weights = new Array(period);
    var wSum = 0;
    for (var j = 0; j < period; j++) {
        weights[j] = Math.exp(-((j - m) * (j - m)) / (2 * s * s));
        wSum += weights[j];
    }
    // Normalize
    for (var j = 0; j < period; j++) {
        weights[j] /= wSum;
    }

    for (var i = period - 1; i < n; i++) {
        var sum = 0;
        for (var j = 0; j < period; j++) {
            sum += data[i - period + 1 + j].close * weights[j];
        }
        result[i] = sum;
    }
    return result;
}

function calculateKAMA(data) {
    var cfg = indicatorPackConfig.kama;
    var period = cfg.period;
    var fastSC = 2 / (cfg.fastPeriod + 1);
    var slowSC = 2 / (cfg.slowPeriod + 1);
    var n = data.length;
    var result = new Array(n);

    if (n < period + 1) return result;

    // First KAMA value = close at period index
    result[period] = data[period].close;

    for (var i = period + 1; i < n; i++) {
        // Efficiency Ratio = direction / volatility
        var direction = Math.abs(data[i].close - data[i - period].close);
        var volatility = 0;
        for (var j = 1; j <= period; j++) {
            volatility += Math.abs(data[i - period + j].close - data[i - period + j - 1].close);
        }
        var er = volatility === 0 ? 0 : direction / volatility;

        // Smoothing constant = (ER * (fast - slow) + slow)^2
        var sc = er * (fastSC - slowSC) + slowSC;
        sc = sc * sc;

        result[i] = result[i - 1] + sc * (data[i].close - result[i - 1]);
    }
    return result;
}

// ============================================================================
// Trend Indicators
// ============================================================================

function calculateAlligator(data) {
    const config = indicatorPackConfig.alligator;

    // Calculate median price (HL2)
    const hl2 = data.map(d => (d.high + d.low) / 2);
    const hl2Data = hl2.map((v, i) => ({ close: v, timestamp: data[i].timestamp }));

    // Calculate SMMA for each line
    const jawRaw = calculateSMMA(hl2Data, config.jawLength);
    const teethRaw = calculateSMMA(hl2Data, config.teethLength);
    const lipsRaw = calculateSMMA(hl2Data, config.lipsLength);

    // Apply offsets
    const jaw = [];
    const teeth = [];
    const lips = [];

    for (let i = 0; i < data.length; i++) {
        const jawIdx = i - config.jawOffset;
        const teethIdx = i - config.teethOffset;
        const lipsIdx = i - config.lipsOffset;

        if (jawIdx >= 0 && jawRaw[jawIdx] !== undefined) jaw[i] = jawRaw[jawIdx];
        if (teethIdx >= 0 && teethRaw[teethIdx] !== undefined) teeth[i] = teethRaw[teethIdx];
        if (lipsIdx >= 0 && lipsRaw[lipsIdx] !== undefined) lips[i] = lipsRaw[lipsIdx];
    }

    return { jaw, teeth, lips };
}

function calculateIchimoku(data) {
    var cfg = indicatorPackConfig.ichimoku;
    var n = data.length;

    // Donchian midpoint helper: (highest high + lowest low) / 2 over `len` bars
    function donchian(arr, len, i) {
        var h = -Infinity, l = Infinity;
        for (var j = i - len + 1; j <= i; j++) {
            if (arr[j].high > h) h = arr[j].high;
            if (arr[j].low < l) l = arr[j].low;
        }
        return (h + l) / 2;
    }

    var tenkan = new Array(n);
    var kijun = new Array(n);
    var chikou = new Array(n);

    // Tenkan-sen (conversion line)
    for (var i = cfg.tenkanLen - 1; i < n; i++) {
        tenkan[i] = donchian(data, cfg.tenkanLen, i);
    }

    // Kijun-sen (base line)
    for (var i = cfg.kijunLen - 1; i < n; i++) {
        kijun[i] = donchian(data, cfg.kijunLen, i);
    }

    // Senkou Span A: (tenkan + kijun) / 2, displaced forward
    // Senkou Span B: donchian(senkouBLen), displaced forward
    // "Displaced forward" = value at bar i appears at bar i + displacement
    // In array terms: senkouA[i + disp] = raw[i], i.e. senkouA[i] = raw[i - disp]
    var disp = cfg.displacement;

    // senkouA/senkouB are sized n + disp so the "leading
    // cloud" can be projected `disp` bars PAST the last real candle — that
    // projection requires arrays longer than the loaded bar range (clamping
    // them would stop the cloud dead at the last candle).
    // Indices 0..n-1 are the normal in-range values (unchanged). Indices
    // n..n+disp-1 are the projection: for future slot k (1..disp), the source
    // is i-disp = (n-1+k)-disp = n-disp-1+k, which ranges n-disp..n-1 — i.e.
    // the most recent `disp` real bars' tenkan/kijun/donchian, already
    // computed above, just plotted at extrapolated future timestamps
    // (lastTs + k*barInterval — see `timestamps` below and the
    // drawSingleIndicator 'ichimoku' case, which builds the matching
    // extended virtual-bar array). klinecharts extrapolates timestamps for
    // overlay x-positioning past the loaded data (see the phantom-bar
    // chart-runway comment in chart-render.js: "Drawings past the runway still position via
    // KLineChart's timestamp extrapolation") — confirmed feasible, not a hack.
    var senkouA = new Array(n + disp);
    var senkouB = new Array(n + disp);
    var timestamps = new Array(n + disp);
    for (var i = 0; i < n; i++) timestamps[i] = data[i].timestamp;

    for (var i = disp; i < n; i++) {
        var src = i - disp;
        if (tenkan[src] !== undefined && kijun[src] !== undefined) {
            senkouA[i] = (tenkan[src] + kijun[src]) / 2;
        }
        if (src >= cfg.senkouBLen - 1) {
            senkouB[i] = donchian(data, cfg.senkouBLen, src);
        }
    }

    if (n > 0 && disp > 0) {
        var lastTs = data[n - 1].timestamp;
        var barIntervalMs = _inferBarIntervalMs(data);
        if (barIntervalMs > 0) {
            for (var k = 1; k <= disp; k++) {
                var futureIdx = n - 1 + k;      // n .. n+disp-1
                var src2 = n - disp - 1 + k;    // n-disp .. n-1 (real, already computed)
                timestamps[futureIdx] = lastTs + barIntervalMs * k;
                if (src2 >= 0 && tenkan[src2] !== undefined && kijun[src2] !== undefined) {
                    senkouA[futureIdx] = (tenkan[src2] + kijun[src2]) / 2;
                }
                if (src2 >= cfg.senkouBLen - 1) {
                    senkouB[futureIdx] = donchian(data, cfg.senkouBLen, src2);
                }
            }
        }
    }

    // Chikou Span: close displaced backward (current close plotted disp bars in the past)
    // chikou[i] = close[i + disp].
    for (var i = 0; i < n - disp; i++) {
        chikou[i] = data[i + disp].close;
    }

    return { tenkan: tenkan, kijun: kijun, senkouA: senkouA, senkouB: senkouB, chikou: chikou, timestamps: timestamps };
}

function calculateGaussianChannel(data) {
    var cfg = indicatorPackConfig.gaussianChannel;
    var n = data.length;
    var N = cfg.poles;
    var per = cfg.period;

    // Alpha/beta from Ehlers' formula
    var beta = (1 - Math.cos(2 * Math.PI / per)) / (Math.pow(Math.SQRT2, 2 / N) - 1);
    var alpha = -beta + Math.sqrt(beta * beta + 2 * beta);

    // Lag for reduced lag mode
    var lag = Math.round((per - 1) / (2 * N));

    // Binomial coefficient C(n,k)
    function binom(n, k) {
        if (k === 0 || k === n) return 1;
        var r = 1;
        for (var i = 0; i < k; i++) r = r * (n - i) / (i + 1);
        return Math.round(r);
    }

    // N-pole Gaussian filter (recursive IIR)
    function gaussFilter(source, poles) {
        var f = new Array(source.length).fill(0);
        var x = 1 - alpha;
        for (var i = 0; i < source.length; i++) {
            f[i] = Math.pow(alpha, poles) * source[i];
            for (var p = 1; p <= poles; p++) {
                var prev = (i - p >= 0) ? f[i - p] : 0;
                var sign = (p % 2 === 1) ? 1 : -1;
                f[i] += sign * binom(poles, p) * Math.pow(x, p) * prev;
            }
        }
        return f;
    }

    // Source (hlc3) and true range
    var src = new Array(n);
    var trData = new Array(n);
    for (var i = 0; i < n; i++) {
        src[i] = (data[i].high + data[i].low + data[i].close) / 3;
        trData[i] = i === 0
            ? data[i].high - data[i].low
            : Math.max(data[i].high - data[i].low, Math.abs(data[i].high - data[i - 1].close), Math.abs(data[i].low - data[i - 1].close));
    }

    // Reduced lag mode: add momentum to source
    if (cfg.reducedLag && lag > 0) {
        var srcOrig = src.slice();
        var trOrig = trData.slice();
        for (var i = lag; i < n; i++) {
            src[i] = srcOrig[i] + (srcOrig[i] - srcOrig[i - lag]);
            trData[i] = trOrig[i] + (trOrig[i] - trOrig[i - lag]);
        }
    }

    // Apply filters
    var filtN = gaussFilter(src, N);
    var filt1 = gaussFilter(src, 1);
    var filtTrN = gaussFilter(trData, N);
    var filtTr1 = gaussFilter(trData, 1);

    // Fast response: average N-pole with 1-pole
    var filt = new Array(n);
    var filtTr = new Array(n);
    for (var i = 0; i < n; i++) {
        filt[i] = cfg.fastResponse ? (filtN[i] + filt1[i]) / 2 : filtN[i];
        filtTr[i] = cfg.fastResponse ? (filtTrN[i] + filtTr1[i]) / 2 : filtTrN[i];
    }

    // Bands
    var upper = new Array(n);
    var lower = new Array(n);
    var colors = new Array(n);
    for (var i = 0; i < n; i++) {
        upper[i] = filt[i] + filtTr[i] * cfg.mult;
        lower[i] = filt[i] - filtTr[i] * cfg.mult;
        if (i > 0 && filt[i] > filt[i - 1]) colors[i] = cfg.bullColor;
        else if (i > 0 && filt[i] < filt[i - 1]) colors[i] = cfg.bearColor;
        else colors[i] = cfg.neutralColor;
    }

    return { filt: filt, upper: upper, lower: lower, colors: colors };
}

function calculateEmaRibbon(data) {
    const config = indicatorPackConfig.emaRibbon;

    // Both are EMAs in TradingView version
    const fastEma = calculateEMA(data, config.fastEmaLen);
    const slowEma = calculateEMA(data, config.slowEmaLen);
    const atr = calculateATR(data, config.atrLen || 60);

    // Calculate trend state for each bar (bull/bear/neutral)
    const trendColors = [];
    for (let i = 0; i < data.length; i++) {
        if (fastEma[i] !== undefined && slowEma[i] !== undefined && atr[i] !== undefined) {
            const emaDiff = fastEma[i] - slowEma[i];
            const atrThreshold = atr[i] * (config.atrMult || 0.30);

            if (emaDiff > atrThreshold) {
                trendColors[i] = 'bull';  // Fast above slow by ATR margin = bullish
            } else if (emaDiff < -atrThreshold) {
                trendColors[i] = 'bear';  // Fast below slow by ATR margin = bearish
            } else {
                trendColors[i] = 'neutral';  // Within ATR margin = neutral
            }
        }
    }

    return { fast: fastEma, slow: slowEma, trendColors };
}

// ============================================================================
// Special Indicators
// ============================================================================

function calculatePiCycle(data) {
    const ma111 = calculateSMA(data, 111);
    const ma350 = calculateSMA(data, 350);

    // 350 MA * 2
    const ma350x2 = ma350.map(v => v !== undefined ? v * 2 : undefined);

    // Detect crossover (Pi Cycle Top signal)
    const signals = [];
    for (let i = 1; i < data.length; i++) {
        if (ma111[i] !== undefined && ma350x2[i] !== undefined &&
            ma111[i - 1] !== undefined && ma350x2[i - 1] !== undefined) {
            // 111 MA crosses above 350*2 MA
            if (ma111[i] >= ma350x2[i] && ma111[i - 1] < ma350x2[i - 1]) {
                signals.push({ index: i, type: 'top' });
            }
        }
    }

    return { ma111, ma350x2, signals };
}

function calculateTTMSqueeze(data) {
    const config = indicatorPackConfig.ttmSqueeze;
    if (!data || data.length < config.bbLength + 1) return { squeeze: [] };

    // Bollinger Bands: SMA basis ± mult * stddev
    const bbBasis = calculateSMA(data, config.bbLength);
    const bbStd = calculateStdDev(data, config.bbLength);

    // Keltner Channel: SMA basis ± mult * ATR (True Range, NOT simple range)
    // Pine: rangemaSQZ = ta.sma(ta.tr, lengthSQZ_KC)  — ta.tr = True Range
    const kcBasis = calculateSMA(data, config.kcLength);
    const trSma = [];
    for (var i = 0; i < data.length; i++) {
        if (i >= config.kcLength) {
            var sum = 0;
            for (var j = i - config.kcLength + 1; j <= i; j++) {
                var hl = data[j].high - data[j].low;
                var hc = Math.abs(data[j].high - data[j - 1].close);
                var lc = Math.abs(data[j].low - data[j - 1].close);
                sum += Math.max(hl, hc, lc);
            }
            trSma[i] = sum / config.kcLength;
        }
    }

    // Squeeze detection: BB inside KC
    var sqzOn = [];
    for (var i = 0; i < data.length; i++) {
        if (bbBasis[i] !== undefined && bbStd[i] !== undefined &&
            kcBasis[i] !== undefined && trSma[i] !== undefined) {
            var bbUpper = bbBasis[i] + bbStd[i] * config.bbMult;
            var bbLower = bbBasis[i] - bbStd[i] * config.bbMult;
            var kcUpper = kcBasis[i] + trSma[i] * config.kcMult;
            var kcLower = kcBasis[i] - trSma[i] * config.kcMult;
            sqzOn[i] = (bbLower > kcLower) && (bbUpper < kcUpper);
        } else {
            sqzOn[i] = false;
        }
    }

    // Duration filter: only mark after minDuration consecutive bars (Pine: >= 3)
    var minDur = config.minDuration || 3;
    var squeeze = [];
    var durCount = 0;
    for (var i = 0; i < data.length; i++) {
        if (sqzOn[i]) {
            durCount++;
        } else {
            durCount = 0;
        }
        squeeze[i] = durCount >= minDur;
    }

    return { squeeze: squeeze };
}

// ============================================================================
// Squeeze / Compression Indicators (BBW%, ATR%, NR7)
// ============================================================================

function calculateBBWPercentile(data) {
    var cfg = indicatorPackConfig.bbwPct;
    // the inner loop already supports an expanding percentile
    // window (10-sample minimum, via `window.length < 10`) — requiring a full
    // bbLength+lookback history up front needlessly blanked short-history
    // assets that already have enough bars for a partial (expanding) result.
    if (!data || data.length < cfg.bbLength + 10) return [];
    var sma = calculateSMA(data, cfg.bbLength);
    var stdDev = calculateStdDev(data, cfg.bbLength);
    var bbw = [];
    for (var i = 0; i < data.length; i++) {
        if (sma[i] !== undefined && stdDev[i] !== undefined && sma[i] > 0) {
            bbw[i] = ((sma[i] + stdDev[i] * 2) - (sma[i] - stdDev[i] * 2)) / sma[i];
        }
    }
    // Rank as percentile of lookback window — return {index, pct} pairs
    var result = [];
    for (var i = 0; i < data.length; i++) {
        if (bbw[i] === undefined) continue;
        var start = Math.max(0, i - cfg.lookback + 1);
        var window = [];
        for (var j = start; j <= i; j++) {
            if (bbw[j] !== undefined) window.push(bbw[j]);
        }
        if (window.length < 10) continue;
        var below = 0;
        for (var j = 0; j < window.length; j++) {
            if (window[j] < bbw[i]) below++;
        }
        var pct = (below / window.length) * 100;
        if (pct < cfg.threshold) result.push({ index: i, pct: pct });
    }
    return result;
}

function calculateATRPercentile(data) {
    var cfg = indicatorPackConfig.atrPct;
    // same expanding-window rationale as calculateBBWPercentile.
    if (!data || data.length < cfg.atrLength + 10) return [];
    var atr = calculateATR(data, cfg.atrLength);
    var result = [];
    for (var i = 0; i < data.length; i++) {
        if (atr[i] === undefined) continue;
        var start = Math.max(0, i - cfg.lookback + 1);
        var window = [];
        for (var j = start; j <= i; j++) {
            if (atr[j] !== undefined) window.push(atr[j]);
        }
        if (window.length < 10) continue;
        var below = 0;
        for (var j = 0; j < window.length; j++) {
            if (window[j] < atr[i]) below++;
        }
        var pct = (below / window.length) * 100;
        if (pct < cfg.threshold) result.push({ index: i, pct: pct });
    }
    return result;
}

function calculateNR7(data) {
    var cfg = indicatorPackConfig.nr7;
    if (!data || data.length < cfg.lookback) return [];
    var result = [];
    for (var i = cfg.lookback - 1; i < data.length; i++) {
        var currentRange = data[i].high - data[i].low;
        var isNarrowest = true;
        for (var j = i - cfg.lookback + 1; j < i; j++) {
            // strict `<` — the current bar only needs to be
            // <= every prior bar in the window (ties flag too). The old `<=`
            // disqualified on a tie, so a run of identical narrowest ranges
            // only ever flagged the first bar instead of every tied bar.
            if ((data[j].high - data[j].low) < currentRange) {
                isNarrowest = false;
                break;
            }
        }
        if (isNarrowest) result.push(i);
    }
    return result;
}

/**
 * Shared function to draw squeeze-style segment overlays below candle lows.
 * @param {object} chart - KlineChart instance
 * @param {array} data - candle data array
 * @param {array} barIndices - array of data indices that qualify
 * @param {string} color - segment color
 * @param {string} idPrefix - unique ID prefix for overlays
 * @param {number} yMult - multiplier for low positioning (e.g. 0.998, 0.996)
 */
function drawSqueezeSegments(chart, data, barIndices, color, idPrefix, yMult, groupId) {
    if (!barIndices || barIndices.length === 0 || !data || data.length < 2) return;
    var barInterval = data.length > 1 ? data[1].timestamp - data[0].timestamp : 86400000;
    var gid = groupId || 'indicatorPack';
    // Group consecutive indices into segments
    var segments = [];
    var segStart = 0;
    for (var i = 1; i <= barIndices.length; i++) {
        var isGap = (i === barIndices.length) ||
            (barIndices[i] - barIndices[i - 1] > 1);
        if (isGap) {
            segments.push({ start: segStart, end: i - 1 });
            segStart = i;
        }
    }
    for (var s = 0; s < segments.length; s++) {
        var seg = segments[s];
        var minLow = Infinity;
        for (var j = seg.start; j <= seg.end; j++) {
            var idx = barIndices[j];
            if (data[idx].low < minLow) minLow = data[idx].low;
        }
        var dotY = minLow * yMult;
        var startTs = data[barIndices[seg.start]].timestamp;
        var endTs = data[barIndices[seg.end]].timestamp;
        if (startTs === endTs) {
            startTs -= barInterval * 0.3;
            endTs += barInterval * 0.3;
        }
        chart.createOverlay({
            name: 'segment',
            id: 'ind_' + idPrefix + '_' + s,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: startTs, value: dotY },
                { timestamp: endTs, value: dotY }
            ],
            styles: { line: { color: color, size: 4, style: 'solid' } }
        });
    }
}

/**
 * Parse hex color to {r,g,b}.
 */
function hexToRgb(hex) {
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    return { r: r, g: g, b: b };
}

/**
 * Draw percentile-intensity segments — brighter color = lower percentile (more extreme).
 * Each bar drawn individually so color can vary per bar.
 * @param {object} chart - KlineChart instance
 * @param {array} data - candle data array
 * @param {array} bars - array of {index, pct} objects
 * @param {string} hexColor - base color in hex (e.g. '#00BCD4')
 * @param {number} threshold - max percentile (e.g. 20)
 * @param {string} idPrefix - unique ID prefix
 * @param {number} yMult - Y position multiplier
 */
function drawIntensitySegments(chart, data, bars, hexColor, threshold, idPrefix, yMult, groupId) {
    if (!bars || bars.length === 0 || !data || data.length < 2) return;
    var barInterval = data.length > 1 ? data[1].timestamp - data[0].timestamp : 86400000;
    var rgb = hexToRgb(hexColor);
    var halfBar = barInterval * 0.4;
    var gid = groupId || 'indicatorPack';

    for (var i = 0; i < bars.length; i++) {
        var idx = bars[i].index;
        var pct = bars[i].pct;
        // Intensity: 0.3 at threshold, 1.0 at pct=0
        var intensity = 0.3 + 0.7 * (1 - pct / threshold);
        var dotY = data[idx].low * yMult;
        var ts = data[idx].timestamp;
        var rgba = 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + intensity.toFixed(2) + ')';
        chart.createOverlay({
            name: 'segment',
            id: 'ind_' + idPrefix + '_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: ts - halfBar, value: dotY },
                { timestamp: ts + halfBar, value: dotY }
            ],
            styles: { line: { color: rgba, size: 4, style: 'solid' } }
        });
    }
}

function calculateAutoFib(data) {
    const config = indicatorPackConfig.autoFib;
    const realLen = data._realLength || data.length;
    // The old guard (`realLen < lookback` where `lookback` is already
    // clamped to `Math.min(config.lookback, realLen)`) can never be true — it's
    // vacuous and let Auto Fib compute off a handful of bars. Enforce a real
    // minimum: the full configured lookback, or 20 bars, whichever is larger.
    const minBars = Math.max(config.lookback || 0, 20);
    if (realLen < minBars) return null;

    const lookback = Math.min(config.lookback, realLen);

    const recentData = data.slice(realLen - lookback, realLen);
    const highest = Math.max(...recentData.map(d => d.high));
    const lowest = Math.min(...recentData.map(d => d.low));
    const range = highest - lowest;

    const highIndex = realLen - lookback + recentData.findIndex(d => d.high === highest);
    const lowIndex = realLen - lookback + recentData.findIndex(d => d.low === lowest);

    // Determine trend direction
    const isUptrend = lowIndex < highIndex;

    // Standard retracement convention — in an uptrend the 0% anchor
    // sits at the swing HIGH (the most recent extreme) and 100% at the swing
    // LOW; in a downtrend it's mirrored (0% at the swing low, 100% at the swing
    // high). The previous code had these swapped.
    const levels = {};
    config.levels.forEach((level, idx) => {
        if (isUptrend) {
            levels[level] = highest - range * level;
        } else {
            levels[level] = lowest + range * level;
        }
    });

    return {
        levels,
        highest,
        lowest,
        highIndex,
        lowIndex,
        isUptrend,
        startIndex: realLen - lookback
    };
}

function calculateCPR(data, config) {
    if (!data || data.length < 10) return null;

    const timeframe = config?.timeframe || 'daily';

    // Helper to get period boundaries
    // Chart timestamps may be kiloseconds×1000 (10-digit, <1e12) — multiply to get ms for Date
    function getDateParts(timestamp) {
        const ms = timestamp < 1e12 ? timestamp * 1000 : timestamp;
        const d = new Date(ms);
        return {
            year: d.getUTCFullYear(),
            month: d.getUTCMonth(),
            quarter: Math.floor(d.getUTCMonth() / 3),
            week: getWeekNumber(d),
            isoYear: getISOWeekYear(d),
            day: d.getUTCDate()
        };
    }

    function getWeekNumber(d) {
        const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
        const dayNum = date.getUTCDay() || 7;
        date.setUTCDate(date.getUTCDate() + 4 - dayNum);
        const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
        return Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    }

    // the ISO week NUMBER can belong to a different ISO
    // week-YEAR than the calendar year (e.g. Dec 29-31 often fall in ISO
    // week 1 of the *next* ISO year). Pairing `week` with `d.getUTCFullYear()`
    // corrupts the same-period check for ~1 week every New Year. Compute the
    // ISO week-year the same way the ISO week number is computed (shift to
    // the Thursday of the week — the calendar year of that Thursday is, by
    // definition, the ISO week-year) and compare (week, isoYear) pairs.
    function getISOWeekYear(d) {
        const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
        const dayNum = date.getUTCDay() || 7;
        date.setUTCDate(date.getUTCDate() + 4 - dayNum);
        return date.getUTCFullYear();
    }

    // Find the previous period's high, low, close
    const lastBar = data[data.length - 1];
    const lastParts = getDateParts(lastBar.timestamp);

    let prevHigh = -Infinity, prevLow = Infinity, prevClose = null;
    let foundCurrentPeriod = false;
    let foundPrevPeriod = false;

    // Helper to check if same period
    function isSamePeriod(parts1, parts2) {
        switch (timeframe) {
            case 'daily':
                return parts1.year === parts2.year && parts1.month === parts2.month && parts1.day === parts2.day;
            case 'weekly':
                return parts1.isoYear === parts2.isoYear && parts1.week === parts2.week;
            case 'monthly':
                return parts1.year === parts2.year && parts1.month === parts2.month;
            case 'quarterly':
                return parts1.year === parts2.year && parts1.quarter === parts2.quarter;
            case 'yearly':
                return parts1.year === parts2.year;
            default:
                return parts1.year === parts2.year && parts1.month === parts2.month && parts1.day === parts2.day;
        }
    }

    // Iterate backwards to find previous period's data
    for (let i = data.length - 1; i >= 0; i--) {
        const bar = data[i];
        const parts = getDateParts(bar.timestamp);

        if (isSamePeriod(parts, lastParts)) {
            foundCurrentPeriod = true;
            continue;
        }

        // We're now in the previous period
        if (foundCurrentPeriod && !foundPrevPeriod) {
            foundPrevPeriod = true;
        }

        if (foundPrevPeriod) {
            // Check if still in previous period
            const prevParts = i > 0 ? getDateParts(data[i - 1].timestamp) : null;

            if (bar.high > prevHigh) prevHigh = bar.high;
            if (bar.low < prevLow) prevLow = bar.low;
            if (prevClose === null) prevClose = bar.close; // First bar of prev period (most recent)

            // If next bar is in a different period, we're done
            if (!prevParts || !isSamePeriod(parts, prevParts)) {
                break;
            }
        }
    }

    if (prevHigh === -Infinity || prevLow === Infinity || prevClose === null) {
        return null;
    }

    // Calculate CPR levels (standard pivot formulas)
    const pivot = (prevHigh + prevLow + prevClose) / 3;
    const bc = (prevHigh + prevLow) / 2;  // Bottom Central
    const tc = pivot * 2 - bc;            // Top Central (same as pivot + (pivot - bc))

    // Support and Resistance levels
    const r1 = pivot * 2 - prevLow;
    const s1 = pivot * 2 - prevHigh;
    const r2 = pivot + (prevHigh - prevLow);
    const s2 = pivot - (prevHigh - prevLow);
    // align with the backend CPRCalculator convention
    // (consensio/indicators/cpr.py) — cumulative-range R3/S3, not classic
    // floor-pivot (R3 = H + 2*(P-L) / S3 = L - 2*(H-P)). H=110/L=90/C=100
    // must yield R3=140, S3=60 to match server-rendered levels.
    const r3 = r2 + (prevHigh - prevLow);
    const s3 = s2 - (prevHigh - prevLow);

    // Find the start of the current period for bounded drawing
    let currentPeriodStart = null;
    for (let i = 0; i < data.length; i++) {
        const parts = getDateParts(data[i].timestamp);
        if (isSamePeriod(parts, lastParts)) {
            currentPeriodStart = data[i].timestamp;
            break;
        }
    }

    // `timeframe` lets drawCPRLevels know which period variant
    // it's rendering without needing extra params threaded through the
    // 'cpr'/'weeklyCpr'/'dailyCpr' call sites.
    return { pivot, bc, tc, s1, s2, s3, r1, r2, r3, prevHigh, prevLow, prevClose, currentPeriodStart, timeframe };
}

// Calculate Key Levels (Daily, Weekly, Monthly, Quarterly, Yearly Opens/Highs/Lows)
function calculateKeyLevels(data) {
    if (!data || data.length < 2) return null;

    const levels = {
        daily: { open: null, prevHigh: null, prevLow: null, prevMid: null },
        weekly: { open: null, prevHigh: null, prevLow: null, prevMid: null },
        monthly: { open: null, prevHigh: null, prevLow: null, prevMid: null },
        quarterly: { open: null, prevHigh: null, prevLow: null },
        yearly: { open: null, currentHigh: null, currentLow: null }
    };

    // Helper: Get day/week/month/quarter/year from timestamp
    // Chart timestamps may be kiloseconds×1000 (10-digit, <1e12) — multiply to get ms for Date
    function getDateParts(timestamp) {
        const ms = timestamp < 1e12 ? timestamp * 1000 : timestamp;
        const d = new Date(ms);
        return {
            year: d.getUTCFullYear(),
            month: d.getUTCMonth(),
            quarter: Math.floor(d.getUTCMonth() / 3),
            week: getWeekNumber(d),
            isoYear: getISOWeekYear(d),
            day: d.getUTCDate(),
            dayOfWeek: d.getUTCDay()
        };
    }

    function getWeekNumber(d) {
        const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
        const dayNum = date.getUTCDay() || 7;
        date.setUTCDate(date.getUTCDate() + 4 - dayNum);
        const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
        return Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    }

    // See calculateCPR for the full rationale: the ISO week
    // NUMBER can belong to a different ISO week-YEAR than the calendar year
    // around New Year. Compare (week, isoYear) pairs, not (week, year).
    function getISOWeekYear(d) {
        const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
        const dayNum = date.getUTCDay() || 7;
        date.setUTCDate(date.getUTCDate() + 4 - dayNum);
        return date.getUTCFullYear();
    }

    const lastBar = data[data.length - 1];
    const lastParts = getDateParts(lastBar.timestamp);

    // Find period boundaries by iterating backwards
    let currentDayOpen = null, prevDayHigh = -Infinity, prevDayLow = Infinity;
    let currentWeekOpen = null, prevWeekHigh = -Infinity, prevWeekLow = Infinity;
    let currentMonthOpen = null, prevMonthHigh = -Infinity, prevMonthLow = Infinity;
    let currentQuarterOpen = null, prevQuarterHigh = -Infinity, prevQuarterLow = Infinity;
    let currentYearOpen = null, currentYearHigh = -Infinity, currentYearLow = Infinity;

    let prevDayFound = false, prevWeekFound = false, prevMonthFound = false, prevQuarterFound = false;

    for (let i = data.length - 1; i >= 0; i--) {
        const bar = data[i];
        const parts = getDateParts(bar.timestamp);

        // Daily
        if (parts.year === lastParts.year && parts.month === lastParts.month && parts.day === lastParts.day) {
            if (currentDayOpen === null) currentDayOpen = bar.open;
        } else if (!prevDayFound) {
            if (bar.high > prevDayHigh) prevDayHigh = bar.high;
            if (bar.low < prevDayLow) prevDayLow = bar.low;
            // Check if we moved to a different day
            const prevParts = i > 0 ? getDateParts(data[i-1].timestamp) : null;
            if (!prevParts || prevParts.day !== parts.day) {
                prevDayFound = true;
            }
        }

        // Weekly (compare isoYear+week, not calendar year+week)
        if (parts.isoYear === lastParts.isoYear && parts.week === lastParts.week) {
            if (parts.dayOfWeek === 1 || currentWeekOpen === null) {
                currentWeekOpen = bar.open;
            }
        } else if (!prevWeekFound) {
            if (bar.high > prevWeekHigh) prevWeekHigh = bar.high;
            if (bar.low < prevWeekLow) prevWeekLow = bar.low;
            const prevParts = i > 0 ? getDateParts(data[i-1].timestamp) : null;
            if (!prevParts || prevParts.week !== parts.week || prevParts.isoYear !== parts.isoYear) {
                prevWeekFound = true;
            }
        }

        // Monthly
        if (parts.year === lastParts.year && parts.month === lastParts.month) {
            if (parts.day === 1 || currentMonthOpen === null) {
                currentMonthOpen = bar.open;
            }
        } else if (!prevMonthFound) {
            if (bar.high > prevMonthHigh) prevMonthHigh = bar.high;
            if (bar.low < prevMonthLow) prevMonthLow = bar.low;
            const prevParts = i > 0 ? getDateParts(data[i-1].timestamp) : null;
            if (!prevParts || prevParts.month !== parts.month) {
                prevMonthFound = true;
            }
        }

        // Quarterly
        if (parts.year === lastParts.year && parts.quarter === lastParts.quarter) {
            if (currentQuarterOpen === null || (parts.month % 3 === 0 && parts.day === 1)) {
                currentQuarterOpen = bar.open;
            }
        } else if (!prevQuarterFound) {
            if (bar.high > prevQuarterHigh) prevQuarterHigh = bar.high;
            if (bar.low < prevQuarterLow) prevQuarterLow = bar.low;
            const prevParts = i > 0 ? getDateParts(data[i-1].timestamp) : null;
            if (!prevParts || prevParts.quarter !== parts.quarter) {
                prevQuarterFound = true;
            }
        }

        // Yearly
        if (parts.year === lastParts.year) {
            if (currentYearOpen === null || (parts.month === 0 && parts.day === 1)) {
                currentYearOpen = bar.open;
            }
            if (bar.high > currentYearHigh) currentYearHigh = bar.high;
            if (bar.low < currentYearLow) currentYearLow = bar.low;
        }

        // Stop if we've gone back far enough
        if (prevDayFound && prevWeekFound && prevMonthFound && prevQuarterFound && i < data.length - 365) {
            break;
        }
    }

    // Assign levels
    levels.daily.open = currentDayOpen;
    levels.daily.prevHigh = prevDayHigh !== -Infinity ? prevDayHigh : null;
    levels.daily.prevLow = prevDayLow !== Infinity ? prevDayLow : null;
    levels.daily.prevMid = levels.daily.prevHigh && levels.daily.prevLow ?
        (levels.daily.prevHigh + levels.daily.prevLow) / 2 : null;

    levels.weekly.open = currentWeekOpen;
    levels.weekly.prevHigh = prevWeekHigh !== -Infinity ? prevWeekHigh : null;
    levels.weekly.prevLow = prevWeekLow !== Infinity ? prevWeekLow : null;
    levels.weekly.prevMid = levels.weekly.prevHigh && levels.weekly.prevLow ?
        (levels.weekly.prevHigh + levels.weekly.prevLow) / 2 : null;

    levels.monthly.open = currentMonthOpen;
    levels.monthly.prevHigh = prevMonthHigh !== -Infinity ? prevMonthHigh : null;
    levels.monthly.prevLow = prevMonthLow !== Infinity ? prevMonthLow : null;
    levels.monthly.prevMid = levels.monthly.prevHigh && levels.monthly.prevLow ?
        (levels.monthly.prevHigh + levels.monthly.prevLow) / 2 : null;

    levels.quarterly.open = currentQuarterOpen;
    levels.quarterly.prevHigh = prevQuarterHigh !== -Infinity ? prevQuarterHigh : null;
    levels.quarterly.prevLow = prevQuarterLow !== Infinity ? prevQuarterLow : null;
    levels.quarterly.prevMid = levels.quarterly.prevHigh && levels.quarterly.prevLow ?
        (levels.quarterly.prevHigh + levels.quarterly.prevLow) / 2 : null;

    levels.yearly.open = currentYearOpen;
    levels.yearly.currentHigh = currentYearHigh !== -Infinity ? currentYearHigh : null;
    levels.yearly.currentLow = currentYearLow !== Infinity ? currentYearLow : null;
    levels.yearly.currentMid = levels.yearly.currentHigh && levels.yearly.currentLow ?
        (levels.yearly.currentHigh + levels.yearly.currentLow) / 2 : null;

    return levels;
}

// Support Resistance Channels calculation (LonesomeTheBlue algorithm)
function calculateSRChannels(data, config) {
    if (!data || data.length < 100) return [];

    const prd = config.pivotPeriod || 10;
    const useHighLow = config.source !== 'Close/Open';
    const channelWidthPct = config.channelWidth || 5;
    const minStrength = config.minStrength || 1;
    const maxChannels = config.maxChannels || 6;
    const loopback = Math.min(config.loopback || 290, data.length - 1);

    // Find pivot highs and lows
    const pivots = [];

    for (let i = prd; i < data.length - prd; i++) {
        const src1 = useHighLow ? data[i].high : Math.max(data[i].close, data[i].open);
        const src2 = useHighLow ? data[i].low : Math.min(data[i].close, data[i].open);

        // Check for pivot high
        let isPivotHigh = true;
        for (let j = 1; j <= prd; j++) {
            const leftSrc = useHighLow ? data[i - j].high : Math.max(data[i - j].close, data[i - j].open);
            const rightSrc = useHighLow ? data[i + j].high : Math.max(data[i + j].close, data[i + j].open);
            if (src1 <= leftSrc || src1 <= rightSrc) {
                isPivotHigh = false;
                break;
            }
        }

        // Check for pivot low
        let isPivotLow = true;
        for (let j = 1; j <= prd; j++) {
            const leftSrc = useHighLow ? data[i - j].low : Math.min(data[i - j].close, data[i - j].open);
            const rightSrc = useHighLow ? data[i + j].low : Math.min(data[i + j].close, data[i + j].open);
            if (src2 >= leftSrc || src2 >= rightSrc) {
                isPivotLow = false;
                break;
            }
        }

        if (isPivotHigh) {
            pivots.push({ value: src1, index: i, type: 'H' });
        }
        if (isPivotLow) {
            pivots.push({ value: src2, index: i, type: 'L' });
        }
    }

    // Filter pivots within loopback period
    const lastIndex = data.length - 1;
    const recentPivots = pivots.filter(p => lastIndex - p.index <= loopback);

    if (recentPivots.length < 2) return [];

    // Calculate channel width based on price range
    let highest = -Infinity, lowest = Infinity;
    const lookbackRange = Math.min(300, data.length);
    for (let i = data.length - lookbackRange; i < data.length; i++) {
        if (data[i].high > highest) highest = data[i].high;
        if (data[i].low < lowest) lowest = data[i].low;
    }
    const cwidth = (highest - lowest) * channelWidthPct / 100;

    // Create SR zones by clustering pivots
    const srZones = [];

    for (let i = 0; i < recentPivots.length; i++) {
        let lo = recentPivots[i].value;
        let hi = lo;
        let strength = 0;
        const includedPivots = [];

        // Find all pivots within channel width
        for (let j = 0; j < recentPivots.length; j++) {
            const cpp = recentPivots[j].value;
            const wdth = cpp <= hi ? hi - cpp : cpp - lo;

            if (wdth <= cwidth) {
                if (cpp <= hi) {
                    lo = Math.min(lo, cpp);
                } else {
                    hi = Math.max(hi, cpp);
                }
                strength += 20; // Each pivot adds 20 to strength
                includedPivots.push(j);
            }
        }

        // Add touch count to strength (how many times price touched this zone)
        for (let k = Math.max(0, data.length - loopback); k < data.length; k++) {
            if ((data[k].high <= hi && data[k].high >= lo) ||
                (data[k].low <= hi && data[k].low >= lo)) {
                strength += 1;
            }
        }

        srZones.push({
            hi: hi,
            lo: lo,
            strength: strength,
            includedPivots: includedPivots
        });
    }

    // Sort by strength and remove overlapping zones
    srZones.sort((a, b) => b.strength - a.strength);

    const finalZones = [];
    const usedPivots = new Set();

    for (const zone of srZones) {
        if (zone.strength < minStrength * 20) continue;

        // Check if any of this zone's pivots are already used
        let hasOverlap = false;
        for (const pivotIdx of zone.includedPivots) {
            if (usedPivots.has(pivotIdx)) {
                hasOverlap = true;
                break;
            }
        }

        if (!hasOverlap) {
            finalZones.push(zone);
            zone.includedPivots.forEach(idx => usedPivots.add(idx));

            if (finalZones.length >= maxChannels) break;
        }
    }

    // Determine zone types (support/resistance/inside) based on current price
    const currentClose = data[data.length - 1].close;

    return finalZones.map(zone => ({
        hi: zone.hi,
        lo: zone.lo,
        strength: zone.strength,
        type: zone.hi < currentClose && zone.lo < currentClose ? 'support' :
              zone.hi > currentClose && zone.lo > currentClose ? 'resistance' : 'inside'
    }));
}

// Fair Value Gap calculation (SpaceManBTC)
// FVG = gap between current candle and 2 candles back (no overlap)
// Bullish FVG: current.low > prevPrev.high (price gapped UP, imbalance below)
// Bearish FVG: current.high < prevPrev.low (price gapped DOWN, imbalance above)
function calculateFVG(data, config) {
    if (!data || data.length < 3) return { bullish: [], bearish: [] };

    const bullishGaps = [];
    const bearishGaps = [];
    const fillByMid = config.fillByMid || false;
    const maxGaps = config.maxGaps || 50;

    // Auto-threshold: compute ATR for filtering out noise FVGs
    var atr = null;
    if (config.autoThreshold) {
        atr = calculateATR(data, config.atrLength || 14);
    }
    var thresholdPct = config.atrThresholdPct || 0.25;

    // Scan for FVGs - start from index 2 (need 2 previous candles)
    for (let i = 2; i < data.length; i++) {
        const current = data[i];
        const prev = data[i - 1];      // Middle candle (impulse candle)
        const prevPrev = data[i - 2];  // 2 candles back

        // Auto-threshold: skip FVGs smaller than threshold % of ATR
        var minGapSize = 0;
        if (atr && atr[i] !== undefined) {
            minGapSize = atr[i] * thresholdPct;
        }

        // Bullish FVG: price gapped UP - current candle's LOW is above prevPrev candle's HIGH
        // This creates an unfilled gap/imbalance below current price (bullish)
        if (current.low > prevPrev.high && (current.low - prevPrev.high) >= minGapSize) {
            const gapTop = current.low;      // Top of the gap (current candle's low)
            const gapBottom = prevPrev.high; // Bottom of the gap (prevPrev candle's high)
            const midPoint = (gapTop + gapBottom) / 2;

            bullishGaps.push({
                startIndex: i,
                timestamp: prev.timestamp,  // Gap formed at middle candle
                top: gapTop,
                bottom: gapBottom,
                mid: midPoint,
                filled: false,
                filledIndex: -1
            });
        }

        // Bearish FVG: price gapped DOWN - current candle's HIGH is below prevPrev candle's LOW
        // This creates an unfilled gap/imbalance above current price (bearish)
        if (current.high < prevPrev.low && (prevPrev.low - current.high) >= minGapSize) {
            const gapTop = prevPrev.low;    // Top of the gap (prevPrev candle's low)
            const gapBottom = current.high; // Bottom of the gap (current candle's high)
            const midPoint = (gapTop + gapBottom) / 2;

            bearishGaps.push({
                startIndex: i,
                timestamp: prev.timestamp,  // Gap formed at middle candle
                top: gapTop,
                bottom: gapBottom,
                mid: midPoint,
                filled: false,
                filledIndex: -1
            });
        }
    }

    // Check for gap fills and update gap boundaries
    // Bullish FVG: filled when price drops DOWN to touch the gap (bearish move into gap)
    // Bearish FVG: filled when price rises UP to touch the gap (bullish move into gap)

    // Process bullish gaps - check if price has come DOWN to fill them
    for (let g = bullishGaps.length - 1; g >= 0; g--) {
        const gap = bullishGaps[g];
        // Fill target: bottom of gap if fillByMid is false, else mid
        let targetLevel = fillByMid ? gap.mid : gap.bottom;

        // Check subsequent candles for fill
        for (let i = gap.startIndex + 1; i < data.length; i++) {
            const candle = data[i];

            // Check if gap is filled (price dropped down to touch target)
            if (candle.low <= targetLevel) {
                gap.filled = true;
                gap.filledIndex = i;
                break;
            }

            // Shrink gap if partially filled (price entered gap but didn't fill completely)
            if (candle.low < gap.top && candle.low > gap.bottom) {
                gap.top = candle.low; // Shrink from top
            }
        }
    }

    // Process bearish gaps - check if price has come UP to fill them
    for (let g = bearishGaps.length - 1; g >= 0; g--) {
        const gap = bearishGaps[g];
        // Fill target: top of gap if fillByMid is false, else mid
        let targetLevel = fillByMid ? gap.mid : gap.top;

        // Check subsequent candles for fill
        for (let i = gap.startIndex + 1; i < data.length; i++) {
            const candle = data[i];

            // Check if gap is filled (price rose up to touch target)
            if (candle.high >= targetLevel) {
                gap.filled = true;
                gap.filledIndex = i;
                break;
            }

            // Shrink gap if partially filled (price entered gap but didn't fill completely)
            if (candle.high > gap.bottom && candle.high < gap.top) {
                gap.bottom = candle.high; // Shrink from bottom
            }
        }
    }

    // Filter: if deleteOnFill, remove filled gaps entirely. If showMitigated, keep them for dimmed display.
    var activeBullish, activeBearish;
    if (config.deleteOnFill && !config.showMitigated) {
        activeBullish = bullishGaps.filter(g => !g.filled).slice(-maxGaps);
        activeBearish = bearishGaps.filter(g => !g.filled).slice(-maxGaps);
    } else {
        // Keep all gaps (filled ones will be drawn with reduced opacity)
        activeBullish = bullishGaps.slice(-maxGaps);
        activeBearish = bearishGaps.slice(-maxGaps);
    }

    return {
        bullish: activeBullish,
        bearish: activeBearish
    };
}

// ============================================================================
// Drawing Functions
// ============================================================================

// Config key → groupId mapping (from registry, with fallback for headless charts)
var INDICATOR_GROUP_IDS = (window.IndicatorRegistry)
    ? IndicatorRegistry.getGroupIds()
    : {
        dailyEma: 'ind_dailyEma', weeklyMa: 'ind_weeklyMa', bullMarketBand: 'ind_bullBand',
        cmUltimate: 'ind_cmUltimate', keltnerTight: 'ind_keltnerTight',
        alligator: 'ind_alligator', emaRibbon: 'ind_emaRibbon', piCycle: 'ind_piCycle',
        autoFib: 'ind_autoFib', cpr: 'ind_cpr', cprHistory: 'ind_cprHistory',
        weeklyCpr: 'ind_weeklyCpr', weeklyCprHistory: 'ind_weeklyCprHistory',
        dailyCpr: 'ind_dailyCpr', dailyCprHistory: 'ind_dailyCprHistory',
        donchian: 'ind_donchian', parabolicSar: 'ind_psar',
        keltner: 'ind_keltner', hullMa: 'ind_hullMa', emaCross: 'ind_emaCross', superTrend: 'ind_superTrend',
        alma: 'ind_alma', kama: 'ind_kama', ichimoku: 'ind_ichimoku', gaussianChannel: 'ind_gaussChan',
        srChannels: 'ind_srChannels', fvg: 'ind_fvg',
        smaSignal: 'ind_smaSignal', rsiDivergence: 'ind_rsiDiv', cvdDivergence: 'ind_cvdDiv', consensio: 'ind_consensio',
        consensioLite: 'ind_consensio', ttmSqueeze: 'ind_ttmSqueeze', bbwPct: 'ind_bbwPct',
        atrPct: 'ind_atrPct', nr7: 'ind_nr7'
    };

// Expose for toggle logic in chart-indicators.js
window.INDICATOR_GROUP_IDS = INDICATOR_GROUP_IDS;

// Draw a single indicator by config key
function drawSingleIndicator(chart, data, serverData, indicatorKey) {
    if (!chart || !data || data.length === 0) return;
    // Exclude phantom future bars
    var _rl = data._realLength;
    if (_rl && _rl < data.length) data = data.slice(0, _rl);
    var config = indicatorPackConfig;
    var gid = INDICATOR_GROUP_IDS[indicatorKey] || 'indicatorPack';

    // Remove existing overlays for this indicator
    try { chart.removeOverlay({ groupId: gid }); } catch (e) {}

    switch (indicatorKey) {
        case 'dailyEma':
            if (!config.dailyEma.enabled) return;
            var emas = calculateDailyEMAs(data);
            // null means the loaded TF is coarser than daily (3D/1W/1M)
            // — a "daily EMA" isn't meaningful there, so render nothing.
            if (!emas) return;
            drawMALine(chart, data, emas.ema21, config.dailyEma.ema21.color, 'ema21', config.dailyEma.ema21.lineWidth, gid);
            drawMALine(chart, data, emas.ema50, config.dailyEma.ema50.color, 'ema50', config.dailyEma.ema50.lineWidth, gid);
            drawMALine(chart, data, emas.ema200, config.dailyEma.ema200.color, 'ema200', config.dailyEma.ema200.lineWidth, gid);
            break;
        case 'weeklyMa':
            if (!config.weeklyMa.enabled) return;
            var serverWMA = serverData && serverData.indicators && serverData.indicators.weeklyMAs;
            if (serverWMA && (serverWMA.wma50 && serverWMA.wma50.length > 1 || serverWMA.wma100 && serverWMA.wma100.length > 1 || serverWMA.wma200 && serverWMA.wma200.length > 1)) {
                if (config.weeklyMa.ma50.enabled) drawServerWeeklyMA(chart, serverWMA.wma50, config.weeklyMa.ma50.color, 'wma50', config.weeklyMa.ma50.lineWidth, data, gid);
                if (config.weeklyMa.ma100.enabled) drawServerWeeklyMA(chart, serverWMA.wma100, config.weeklyMa.ma100.color, 'wma100', config.weeklyMa.ma100.lineWidth, data, gid);
                if (config.weeklyMa.ma200.enabled) drawServerWeeklyMA(chart, serverWMA.wma200, config.weeklyMa.ma200.color, 'wma200', config.weeklyMa.ma200.lineWidth, data, gid);
            } else {
                var mas = calculateWeeklyMAs(data);
                drawMALine(chart, data, mas.ma50, config.weeklyMa.ma50.color, 'wma50', config.weeklyMa.ma50.lineWidth, gid);
                drawMALine(chart, data, mas.ma100, config.weeklyMa.ma100.color, 'wma100', config.weeklyMa.ma100.lineWidth, gid);
                drawMALine(chart, data, mas.ma200, config.weeklyMa.ma200.color, 'wma200', config.weeklyMa.ma200.lineWidth, gid);
            }
            break;
        case 'bullMarketBand':
            if (!config.bullMarketBand || !config.bullMarketBand.enabled) return;
            var serverBMSB = serverData && serverData.indicators && serverData.indicators.bullMarketBand;
            if (serverBMSB) {
                if (serverBMSB.ema21w && serverBMSB.ema21w.length > 1) drawServerWeeklyMA(chart, serverBMSB.ema21w, config.bullMarketBand.ema21w.color, 'bmsb_ema21w', config.bullMarketBand.ema21w.lineWidth, data, gid);
                if (serverBMSB.sma20w && serverBMSB.sma20w.length > 1) drawServerWeeklyMA(chart, serverBMSB.sma20w, config.bullMarketBand.sma20w.color, 'bmsb_sma20w', config.bullMarketBand.sma20w.lineWidth, data, gid);
            }
            break;
        case 'cmUltimate':
            if (!config.cmUltimate.enabled) return;
            var cmData = calculateCMUltimateMA(data);
            drawColoredMALine(chart, data, cmData.values, cmData.colors, 'cmUltimate', gid);
            break;
        case 'keltnerTight':
            if (!config.keltnerTight.enabled) return;
            var kcT = calculateKeltner(data, 'keltnerTight');
            drawMALine(chart, data, kcT.basis, config.keltnerTight.basisColor, 'kcTBasis', 2, gid);
            drawMALine(chart, data, kcT.upper, config.keltnerTight.upperColor, 'kcTUpper', 1, gid);
            drawMALine(chart, data, kcT.lower, config.keltnerTight.lowerColor, 'kcTLower', 1, gid);
            drawBandFill(chart, data, kcT.upper, kcT.lower, 'kcTFill', config.keltnerTight.basisColor, config.keltnerTight.fillOpacity, gid);
            break;
        case 'donchian':
            if (!config.donchian.enabled) return;
            var dc = calculateDonchian(data);
            drawMALine(chart, data, dc.upper, config.donchian.upperColor, 'donchianUpper', 1, gid);
            drawMALine(chart, data, dc.lower, config.donchian.lowerColor, 'donchianLower', 1, gid);
            if (config.donchian.showMiddle) {
                drawMALine(chart, data, dc.middle, config.donchian.middleColor, 'donchianMiddle', 1, gid);
            }
            drawBandFill(chart, data, dc.upper, dc.lower, 'donchianFill', config.donchian.middleColor, config.donchian.fillOpacity, gid);
            break;
        case 'parabolicSar':
            if (!config.parabolicSar.enabled) return;
            var psarResult = calculateParabolicSAR(data);
            drawParabolicSARDots(chart, data, psarResult, gid);
            break;
        case 'keltner':
            if (!config.keltner.enabled) return;
            var kc = calculateKeltner(data);
            drawMALine(chart, data, kc.basis, config.keltner.basisColor, 'kcBasis', 1, gid);
            drawMALine(chart, data, kc.upper, config.keltner.upperColor, 'kcUpper', 1, gid);
            drawMALine(chart, data, kc.lower, config.keltner.lowerColor, 'kcLower', 1, gid);
            drawBandFill(chart, data, kc.upper, kc.lower, 'kcFill', config.keltner.basisColor, config.keltner.fillOpacity, gid);
            break;
        case 'hullMa':
            if (!config.hullMa.enabled) return;
            var hma = calculateHullMA(data);
            drawMALine(chart, data, hma, config.hullMa.color, 'hullMa', config.hullMa.lineWidth, gid);
            break;
        case 'emaCross':
            if (!config.emaCross.enabled) return;
            var crossResult = calculateEMACross(data);
            drawEMACrossMarkers(chart, data, crossResult, gid);
            break;
        case 'superTrend':
            if (!config.superTrend.enabled) return;
            var stResult = calculateSuperTrend(data);
            drawSuperTrendLine(chart, data, stResult, gid);
            break;
        case 'alma':
            if (!config.alma.enabled) return;
            var almaVals = calculateALMA(data);
            drawMALine(chart, data, almaVals, config.alma.color, 'alma', config.alma.lineWidth, gid);
            break;
        case 'kama':
            if (!config.kama.enabled) return;
            var kamaVals = calculateKAMA(data);
            drawMALine(chart, data, kamaVals, config.kama.color, 'kama', config.kama.lineWidth, gid);
            break;
        case 'alligator':
            if (!config.alligator.enabled) return;
            var alligator = calculateAlligator(data);
            drawMALine(chart, data, alligator.jaw, config.alligator.jawColor, 'alligatorJaw', 2, gid);
            drawMALine(chart, data, alligator.teeth, config.alligator.teethColor, 'alligatorTeeth', 2, gid);
            drawMALine(chart, data, alligator.lips, config.alligator.lipsColor, 'alligatorLips', 2, gid);
            break;
        case 'ichimoku':
            if (!config.ichimoku.enabled) return;
            var ichi = calculateIchimoku(data);
            // senkouA/senkouB (and `timestamps`) are extended
            // `displacement` slots past `data` for the leading-cloud
            // projection — build the matching extended virtual-bar array so
            // drawBandFill/drawMALine have a timestamp to plot those slots at.
            var ichiExtData = (ichi.timestamps && ichi.timestamps.length > data.length)
                ? data.concat(ichi.timestamps.slice(data.length).map(function (ts) { return { timestamp: ts }; }))
                : data;
            drawIchimokuCloud(chart, ichiExtData, ichi, config.ichimoku, gid);
            drawMALine(chart, data, ichi.tenkan, config.ichimoku.tenkanColor, 'ichiTenkan', 1, gid);
            drawMALine(chart, data, ichi.kijun, config.ichimoku.kijunColor, 'ichiKijun', 1, gid);
            drawMALine(chart, ichiExtData, ichi.senkouA, config.ichimoku.senkouAColor, 'ichiSenkouA', 1, gid);
            drawMALine(chart, ichiExtData, ichi.senkouB, config.ichimoku.senkouBColor, 'ichiSenkouB', 1, gid);
            drawMALine(chart, data, ichi.chikou, config.ichimoku.chikouColor, 'ichiChikou', 1, gid);
            break;
        case 'gaussianChannel':
            if (!config.gaussianChannel.enabled) return;
            var gc = calculateGaussianChannel(data);
            drawColoredMALine(chart, data, gc.filt, gc.colors, 'gcFilt', gid);
            drawDualColorBandFill(chart, data, gc.upper, gc.lower, gc.colors, config.gaussianChannel, 'gcFill', gid);
            break;
        case 'emaRibbon':
            if (!config.emaRibbon.enabled) return;
            drawEmaRibbon(chart, data, calculateEmaRibbon(data), config.emaRibbon, gid);
            break;
        case 'piCycle':
            if (!config.piCycle.enabled) return;
            var piCycle = calculatePiCycle(data);
            drawMALine(chart, data, piCycle.ma111, config.piCycle.ma111Color, 'piMa111', 2, gid);
            drawMALine(chart, data, piCycle.ma350x2, config.piCycle.ma350x2Color, 'piMa350x2', 2, gid);
            piCycle.signals.forEach(function(signal) { drawSignalMarker(chart, data, signal.index, signal.type, gid); });
            break;
        case 'autoFib':
            if (!config.autoFib.enabled) return;
            var fib = calculateAutoFib(data);
            if (fib) drawFibLevels(chart, data, fib, gid);
            break;
        case 'cpr':
            if (!config.cpr.enabled) return;
            var cpr = null;
            if (serverData && serverData.cpr) {
                cpr = serverData.cpr;
                cpr.currentPeriodStart = cpr.periodStart ? cpr.periodStart * 1000 : data[0].timestamp;
            } else {
                cpr = calculateCPR(data, config.cpr);
            }
            if (cpr) drawCPRLevels(chart, data, cpr, config.cpr, gid);
            break;
        case 'cprHistory':
            if (!config.cprHistory.enabled) return;
            var cprHist = null;
            if (serverData && serverData.cpr && serverData.cpr.history) {
                // Server sends raw prices — transform to log space if needed
                cprHist = serverData.cpr.history.map(function(h) {
                    return { pivot: _logPrice(h.pivot), tc: _logPrice(h.tc), bc: _logPrice(h.bc),
                             periodStart: h.periodStart, periodEnd: h.periodEnd };
                });
            } else {
                cprHist = calculateCPRHistory(data, config.cprHistory.months || 5);
            }
            if (cprHist && cprHist.length > 0) drawCPRHistory(chart, data, cprHist, config.cprHistory, gid);
            break;
        case 'weeklyCpr':
            if (!config.weeklyCpr.enabled) return;
            var wCpr = serverData && serverData.weekly_cpr ? serverData.weekly_cpr : calculateCPR(data, { timeframe: 'weekly' });
            if (wCpr) { wCpr.currentPeriodStart = wCpr.periodStart ? wCpr.periodStart * 1000 : data[0].timestamp; drawCPRLevels(chart, data, wCpr, config.weeklyCpr, gid); }
            break;
        case 'weeklyCprHistory':
            if (!config.weeklyCprHistory.enabled) return;
            var wHist = serverData && serverData.weekly_cpr && serverData.weekly_cpr.history
                ? serverData.weekly_cpr.history.map(function(h) { return { pivot: _logPrice(h.pivot), tc: _logPrice(h.tc), bc: _logPrice(h.bc), periodStart: h.periodStart, periodEnd: h.periodEnd }; })
                : null;
            if (wHist && wHist.length > 0) drawCPRHistory(chart, data, wHist, config.weeklyCprHistory, gid);
            break;
        case 'dailyCpr':
            if (!config.dailyCpr.enabled) return;
            var dCpr = serverData && serverData.daily_cpr ? serverData.daily_cpr : calculateCPR(data, { timeframe: 'daily' });
            if (dCpr) { dCpr.currentPeriodStart = dCpr.periodStart ? dCpr.periodStart * 1000 : data[0].timestamp; drawCPRLevels(chart, data, dCpr, config.dailyCpr, gid); }
            break;
        case 'dailyCprHistory':
            if (!config.dailyCprHistory.enabled) return;
            var dHist = serverData && serverData.daily_cpr && serverData.daily_cpr.history
                ? serverData.daily_cpr.history.map(function(h) { return { pivot: _logPrice(h.pivot), tc: _logPrice(h.tc), bc: _logPrice(h.bc), periodStart: h.periodStart, periodEnd: h.periodEnd }; })
                : null;
            if (dHist && dHist.length > 0) drawCPRHistory(chart, data, dHist, config.dailyCprHistory, gid);
            break;
        case 'keyLevels':
            if (!config.keyLevels.enabled) return;
            var kl = calculateKeyLevels(data);
            if (kl) drawKeyLevels(chart, data, kl, config.keyLevels, gid);
            break;
        case 'srChannels':
            if (!config.srChannels.enabled) return;
            var srZones = calculateSRChannels(data, config.srChannels);
            if (srZones && srZones.length > 0) drawSRChannels(chart, data, srZones, config.srChannels, gid);
            break;
        case 'fvg':
            if (!config.fvg.enabled) return;
            var fvgData = calculateFVG(data, config.fvg);
            if (fvgData && (fvgData.bullish.length > 0 || fvgData.bearish.length > 0)) drawFVG(chart, data, fvgData, config.fvg, gid);
            break;
        case 'smaSignal':
            if (!config.smaSignal.enabled) return;
            drawSmaLines(chart, data, serverData, gid);
            break;
        case 'rsiDivergence':
            if (!config.rsiDivergence.enabled) return;
            drawRSIDivergences(chart, data, config.rsiDivergence, gid);
            break;
        case 'cvdDivergence':
            if (!config.cvdDivergence.enabled) return;
            drawCVDDivergences(chart, data, serverData, config.cvdDivergence, gid);
            break;
        case 'consensio':
        case 'consensioLite':
            removeConsensioTable();
            if (!config.consensio.enabled && !config.consensioLite.enabled) return;
            var consensioResult = calculateConsensio(data);
            drawConsensio(chart, data, consensioResult, config.consensio, serverData, config.consensio.enabled, gid);
            break;
        case 'ttmSqueeze':
            if (!config.ttmSqueeze.enabled) return;
            drawTTMSqueezePane(chart, data, serverData, gid);
            break;
        case 'bbwPct':
            if (!config.bbwPct.enabled) return;
            drawIntensitySegments(chart, data, calculateBBWPercentile(data), config.bbwPct.color, config.bbwPct.threshold, 'bbwpct', 0.996, gid);
            break;
        case 'atrPct':
            if (!config.atrPct.enabled) return;
            drawIntensitySegments(chart, data, calculateATRPercentile(data), config.atrPct.color, config.atrPct.threshold, 'atrpct', 0.994, gid);
            break;
        case 'nr7':
            if (!config.nr7.enabled) return;
            drawSqueezeSegments(chart, data, calculateNR7(data), config.nr7.color, 'nr7', 0.992, gid);
            break;
        case 'sessions':
            if (!config.sessions.enabled) {
                _removeSessionsIndicator(chart);
                return;
            }
            drawMarketSessions(chart, data, serverData, gid);
            break;
    }
}

// ============================================================================
// Market Sessions — Fixed-position bar at bottom of candle pane
// Uses a registered KLineChart indicator with custom draw() for pixel rendering.
// ============================================================================

// Session color lookup — returns null for dead/gap (= don't draw)
function _getSessionColor(session, phase) {
    if (session === 'dead' || phase === 'dead' || phase === 'gap') return null;
    var colors = indicatorPackConfig.sessions.colors;
    var sc = colors[session];
    if (!sc) return null;
    return sc[phase] || sc.session || null;
}

// Register the indicator once (idempotent — skips if already registered)
(function _registerSessionsIndicator() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerIndicator({
            name: 'MARKET_SESSIONS_BAR',
            shortName: '',
            figures: [],
            calc: function(dataList) {
                var lookup = window._sessionBarLookup || {};
                return dataList.map(function(d) {
                    var secTs = Math.floor(d.timestamp / 1000);
                    return lookup[secTs] || null;
                });
            },
            draw: function(params) {
                var ctx = params.ctx;
                var bounding = params.bounding;
                var barSpace = params.barSpace;
                var visibleRange = params.visibleRange;
                var result = params.indicator.result;
                var xAxis = params.xAxis;
                var config = window.indicatorPackConfig && window.indicatorPackConfig.sessions;
                if (!config || !config.enabled) return;
                if (!visibleRange || !xAxis || !barSpace) return;

                var rectH = config.rectHeight;
                var bottomMargin = config.bottomMargin;
                var y = bounding.height - bottomMargin - rectH;
                var labelY = y - 3;  // labels sit just above the bar

                var barW = Math.max(barSpace.bar, 2);
                var intervalMs = window._sessionBarIntervalMs || 0;
                var is4H = intervalMs >= 12600000;

                ctx.save();
                var prevSession = null;
                var sessionGap = Math.max(2, Math.round(barW * 0.3));
                var holidayTextDrawn = {};  // track which holiday sessions already have text
                var labelDrawn = {};  // track session labels drawn per day to avoid duplicates

                for (var i = visibleRange.from; i < visibleRange.to; i++) {
                    var info = result[i];
                    if (!info) { prevSession = null; continue; }

                    var session = info.session;
                    var phase = info.phase;
                    var color = _getSessionColor(session, phase);

                    // Dead zone, gap, and weekends: don't draw
                    if (!color || info.holiday === 'Weekend') {
                        continue;
                    }

                    var x = xAxis.convertToPixel(i);
                    var isNewSession = session !== prevSession;
                    var isHoliday = !!info.holiday;

                    // Holiday sessions: on 4H skip entirely (no room), on sub-4H show label + name
                    if (isHoliday) {
                        if (is4H) {
                            // 4H: skip holiday sessions entirely (text overflows)
                            prevSession = session;
                            continue;
                        }
                        // Dedup key: approximate day from bar index + interval
                        var barsPerDay = Math.max(1, Math.round(86400000 / (intervalMs || 3600000)));
                        var dayKey = session + '_' + info.holiday + '_' + Math.floor(i / barsPerDay);
                        if (!holidayTextDrawn[dayKey]) {
                            holidayTextDrawn[dayKey] = true;
                            var hx = Math.round(x);
                            ctx.globalAlpha = 0.5;
                            ctx.fillStyle = config.labelColor;
                            ctx.font = config.labelSize + 'px JetBrains Mono, monospace';
                            ctx.textAlign = 'left';
                            ctx.textBaseline = 'bottom';
                            ctx.fillText('[' + session.toUpperCase() + ']', hx, labelY);
                            // Holiday name on the next line (bar line)
                            ctx.globalAlpha = 0.6;
                            ctx.fillStyle = config.holidayColor;
                            ctx.font = 'italic ' + (config.labelSize - 1) + 'px JetBrains Mono, monospace';
                            ctx.textBaseline = 'middle';
                            ctx.fillText(info.holiday, hx, y + rectH / 2);
                        }
                        prevSession = session;
                        continue;
                    }

                    // Future opacity
                    ctx.globalAlpha = info.isFuture ? config.futureOpacity : 1;

                    // Per-candle rectangle — add gap at session boundaries
                    var rxLeft = Math.round(x - barW / 2);
                    var rxWidth = Math.round(barW);
                    if (isNewSession && prevSession !== null) {
                        rxLeft += sessionGap;
                        rxWidth -= sessionGap;
                    }
                    ctx.fillStyle = color;
                    ctx.fillRect(rxLeft, y, Math.max(rxWidth, 1), rectH);

                    // Session label on first bar of a new session (not on 4H)
                    // Dedup per session per day to prevent duplicate labels on 1H+
                    if (!is4H && isNewSession && session !== 'dead') {
                        var bpd = Math.max(1, Math.round(86400000 / (intervalMs || 3600000)));
                        var lblKey = session + '_' + Math.floor(i / bpd);
                        if (!labelDrawn[lblKey]) {
                            labelDrawn[lblKey] = true;
                            ctx.globalAlpha = 1;
                            ctx.fillStyle = config.labelColor;
                            ctx.font = config.labelSize + 'px JetBrains Mono, monospace';
                            ctx.textAlign = 'left';
                            ctx.textBaseline = 'bottom';
                            ctx.fillText('[' + session.toUpperCase() + ']', rxLeft, labelY);
                        }
                    }

                    prevSession = session;
                }
                ctx.restore();
            }
        });
    } catch (e) {
        console.warn('[Sessions] indicator register error:', e);
    }
})();

function drawMarketSessions(chart, data, serverData, groupId) {
    var config = indicatorPackConfig.sessions;
    if (!config.enabled || !data || data.length < 2) return;
    if (!serverData || !serverData.sessions || serverData.sessions.length === 0) return;

    var sessions = serverData.sessions;
    var futureSessions = serverData.futureSessions || [];
    var realLen = Math.min(data.length, sessions.length);

    // Store bar interval for 4H detection in draw()
    window._sessionBarIntervalMs = data.length > 1 ? data[1].timestamp - data[0].timestamp : 0;

    // Build timestamp → session lookup (for the indicator's calc function)
    var lookup = {};
    for (var i = 0; i < realLen; i++) {
        var secTs = Math.floor(data[i].timestamp / 1000);
        lookup[secTs] = sessions[i];
    }
    for (var j = 0; j < futureSessions.length; j++) {
        var fs = futureSessions[j];
        lookup[fs.time] = { session: fs.session, phase: fs.phase, holiday: fs.holiday, isFuture: true };
    }
    window._sessionBarLookup = lookup;

    // Remove existing sessions indicator, then re-create on candle pane
    try { chart.removeIndicator('candle_pane', 'MARKET_SESSIONS_BAR'); } catch (e) {}
    chart.createIndicator({ name: 'MARKET_SESSIONS_BAR' }, true, { id: 'candle_pane' });
}

function _removeSessionsIndicator(chart) {
    try { chart.removeIndicator('candle_pane', 'MARKET_SESSIONS_BAR'); } catch (e) {}
    window._sessionBarLookup = null;
}

// Full draw: remove all pack overlays and redraw all enabled indicators
function drawIndicatorPack(chart, data, serverData) {
    if (!chart || !data || data.length === 0) return;

    // Exclude phantom future bars from indicator calculations —
    // they have flat OHLCV at the last close and would make lines extend into the future
    var _rl = data._realLength;
    if (_rl && _rl < data.length) data = data.slice(0, _rl);

    const config = indicatorPackConfig;

    // Remove all indicator pack overlays (per-indicator groupIds)
    for (var key in INDICATOR_GROUP_IDS) {
        try { chart.removeOverlay({ groupId: INDICATOR_GROUP_IDS[key] }); } catch (e) {}
    }

    // Clean up sessions indicator (uses registerIndicator, not overlays)
    _removeSessionsIndicator(chart);

    // Always clean up consensio HTML table (re-drawn if enabled)
    removeConsensioTable();

    // Draw each enabled indicator with error boundary
    var indicatorKeys = [
        'dailyEma', 'weeklyMa', 'bullMarketBand', 'cmUltimate',
        'keltnerTight', 'donchian', 'parabolicSar', 'keltner', 'hullMa', 'emaCross', 'superTrend', 'alma', 'kama',
        'alligator', 'emaRibbon', 'piCycle', 'autoFib',
        'cpr', 'cprHistory', 'weeklyCpr', 'weeklyCprHistory', 'dailyCpr', 'dailyCprHistory',
        'keyLevels', 'srChannels', 'fvg',
        'smaSignal', 'rsiDivergence', 'cvdDivergence', 'consensio', 'ttmSqueeze',
        'bbwPct', 'atrPct', 'nr7', 'sessions'
    ];

    indicatorKeys.forEach(function(key) {
        // Check if this indicator is enabled
        var cfgEntry = config[key];
        if (!cfgEntry || !cfgEntry.enabled) {
            // Special case: consensioLite enables consensio drawing
            if (key === 'consensio' && config.consensioLite && config.consensioLite.enabled) {
                // fall through to draw
            } else {
                return;
            }
        }
        try {
            drawSingleIndicator(chart, data, serverData, key);
        } catch (e) {
            console.error('[indicators] Error drawing ' + key + ':', e);
        }
    });
}

// ============================================================================
// SMA 20/200 Drawing Functions
// ============================================================================

function drawSmaLines(chart, data, serverData, groupId) {
    var smaLines = serverData && serverData.indicators && serverData.indicators.smaLines;
    if (!smaLines) return;
    var config = indicatorPackConfig.smaSignal;
    var gid = groupId || 'indicatorPack';

    // Dense daily SMA points — use direct segment connections (not step interpolation)
    // Subsample to ~200 segments max for performance
    function drawDenseLine(points, color, id) {
        if (!points || points.length < 2) return;
        var step = Math.max(1, Math.floor(points.length / 200));
        var sampled = [];
        for (var i = 0; i < points.length; i += step) {
            sampled.push(points[i]);
        }
        // Always include the last point
        if (sampled[sampled.length - 1] !== points[points.length - 1]) {
            sampled.push(points[points.length - 1]);
        }
        for (var j = 1; j < sampled.length; j++) {
            chart.createOverlay({
                name: 'segment',
                id: 'ind_' + id + '_' + j,
                groupId: gid,
                lock: true,
                points: [
                    { timestamp: sampled[j - 1].time * 1000, value: sampled[j - 1].value },
                    { timestamp: sampled[j].time * 1000, value: sampled[j].value }
                ],
                styles: { line: { color: color, size: config.lineWidth } }
            });
        }
    }

    drawDenseLine(smaLines.sma20, config.sma20Color, 'sma20_signal');
    drawDenseLine(smaLines.sma200, config.sma200Color, 'sma200_signal');
}

function drawMALine(chart, data, values, color, id, lineWidth, groupId) {
    lineWidth = lineWidth || 1;
    // Subsample to ~200 segments max (same approach as drawDenseLine)
    var validPairs = [];
    for (var i = 1; i < data.length; i++) {
        if (values[i] !== undefined && values[i - 1] !== undefined) {
            validPairs.push(i);
        }
    }
    if (validPairs.length === 0) return;

    var step = Math.max(1, Math.floor(validPairs.length / 200));
    var sampled = [];
    for (var j = 0; j < validPairs.length; j += step) {
        sampled.push(validPairs[j]);
    }
    // Always include last valid index
    if (sampled[sampled.length - 1] !== validPairs[validPairs.length - 1]) {
        sampled.push(validPairs[validPairs.length - 1]);
    }

    var gid = groupId || 'indicatorPack';
    for (var k = 0; k < sampled.length; k++) {
        var idx = sampled[k];
        var prevIdx = k > 0 ? sampled[k - 1] : idx - 1;
        if (values[prevIdx] === undefined) prevIdx = idx - 1;
        chart.createOverlay({
            name: 'segment',
            id: 'ind_' + id + '_' + k,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: data[prevIdx].timestamp, value: values[prevIdx] },
                { timestamp: data[idx].timestamp, value: values[idx] }
            ],
            styles: {
                line: {
                    color: color,
                    size: lineWidth
                }
            }
        });
    }
}

function drawColoredMALine(chart, data, values, colors, id, groupId) {
    // Draw as segments with different colors, subsampled to ~300 max
    var validPairs = [];
    for (let i = 1; i < data.length; i++) {
        if (values[i] !== undefined && values[i - 1] !== undefined) {
            validPairs.push(i);
        }
    }
    if (validPairs.length === 0) return;
    var step = Math.max(1, Math.floor(validPairs.length / 300));
    var sampled = [];
    for (var j = 0; j < validPairs.length; j += step) sampled.push(validPairs[j]);
    if (sampled[sampled.length - 1] !== validPairs[validPairs.length - 1]) {
        sampled.push(validPairs[validPairs.length - 1]);
    }
    var gid = groupId || 'indicatorPack';
    for (var k = 0; k < sampled.length; k++) {
        var idx = sampled[k];
        var prevIdx = k > 0 ? sampled[k - 1] : idx - 1;
        if (values[prevIdx] === undefined) prevIdx = idx - 1;
        chart.createOverlay({
            name: 'segment',
            id: 'ind_' + id + '_' + k,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: data[prevIdx].timestamp, value: values[prevIdx] },
                { timestamp: data[idx].timestamp, value: values[idx] }
            ],
            styles: {
                line: {
                    color: colors[idx] || '#2196F3',
                    size: 2
                }
            }
        });
    }
}

function drawBandFill(chart, data, upper, lower, id, color, opacity, groupId) {
    // Collect valid indices
    var valid = [];
    for (var i = 0; i < data.length; i++) {
        if (upper[i] !== undefined && lower[i] !== undefined) valid.push(i);
    }
    if (valid.length < 2) return;

    var gid = groupId || 'indicatorPack';
    var r = parseInt(color.slice(1, 3), 16);
    var g = parseInt(color.slice(3, 5), 16);
    var b = parseInt(color.slice(5, 7), 16);
    var rgba = 'rgba(' + r + ',' + g + ',' + b + ',' + opacity + ')';

    // Split into small polygon chunks (~40 bars each) so fill tracks lines precisely when scrolling
    var CHUNK = 40;
    var chunkIdx = 0;
    for (var c = 0; c < valid.length - 1; c += CHUNK) {
        var end = Math.min(c + CHUNK + 1, valid.length); // +1 overlap for seamless join
        var polyPoints = [];
        // Upper forward
        for (var j = c; j < end; j++) {
            var idx = valid[j];
            polyPoints.push({ timestamp: data[idx].timestamp, value: upper[idx] });
        }
        // Lower backward
        for (var j = end - 1; j >= c; j--) {
            var idx = valid[j];
            polyPoints.push({ timestamp: data[idx].timestamp, value: lower[idx] });
        }
        chart.createOverlay({
            name: 'bandFill',
            id: 'ind_' + id + '_' + chunkIdx,
            groupId: gid,
            lock: true,
            visible: true,
            points: polyPoints,
            extendData: rgba
        });
        chunkIdx++;
    }
}

function drawDualColorBandFill(chart, data, upper, lower, colors, cfg, id, groupId) {
    // Build segments of consecutive same-color bars
    var segments = [];
    var curSeg = null;
    for (var i = 0; i < data.length; i++) {
        if (upper[i] === undefined || lower[i] === undefined) continue;
        var c = colors[i] || cfg.neutralColor;
        if (!curSeg || curSeg.color !== c) {
            // Overlap: include previous point for seamless join
            if (curSeg && curSeg.pts.length >= 2) segments.push(curSeg);
            var startPts = (curSeg && curSeg.pts.length > 0) ? [curSeg.pts[curSeg.pts.length - 1]] : [];
            curSeg = { color: c, pts: startPts };
        }
        curSeg.pts.push({ timestamp: data[i].timestamp, upper: upper[i], lower: lower[i] });
    }
    if (curSeg && curSeg.pts.length >= 2) segments.push(curSeg);
    if (segments.length === 0) return;

    // Encode all segments into a single overlay's points + extendData
    // Points: flat list of all polygon vertices for all segments
    // extendData: segment boundaries + colors for createPointFigures to split
    var allPoints = [];
    var segMeta = []; // { start, count, rgba }
    for (var s = 0; s < segments.length; s++) {
        var seg = segments[s];
        var hex = seg.color;
        var r = parseInt(hex.slice(1, 3), 16);
        var g = parseInt(hex.slice(3, 5), 16);
        var b = parseInt(hex.slice(5, 7), 16);
        var rgba = 'rgba(' + r + ',' + g + ',' + b + ',' + cfg.fillOpacity + ')';
        var start = allPoints.length;
        // Upper forward
        for (var j = 0; j < seg.pts.length; j++) allPoints.push({ timestamp: seg.pts[j].timestamp, value: seg.pts[j].upper });
        // Lower backward
        for (var j = seg.pts.length - 1; j >= 0; j--) allPoints.push({ timestamp: seg.pts[j].timestamp, value: seg.pts[j].lower });
        segMeta.push({ start: start, count: allPoints.length - start, rgba: rgba });
    }

    var gid = groupId || 'indicatorPack';
    chart.createOverlay({
        name: 'dualBandFill',
        id: 'ind_' + id,
        groupId: gid,
        lock: true,
        visible: true,
        points: allPoints,
        extendData: segMeta
    });
}

function drawIchimokuCloud(chart, data, ichi, cfg, groupId) {
    // Split into bull/bear arrays, draw each with drawBandFill for dual-color cloud
    var bullUpper = new Array(data.length);
    var bullLower = new Array(data.length);
    var bearUpper = new Array(data.length);
    var bearLower = new Array(data.length);
    var hasBull = false, hasBear = false;

    for (var i = 0; i < data.length; i++) {
        if (ichi.senkouA[i] !== undefined && ichi.senkouB[i] !== undefined) {
            if (ichi.senkouA[i] >= ichi.senkouB[i]) {
                bullUpper[i] = ichi.senkouA[i];
                bullLower[i] = ichi.senkouB[i];
                hasBull = true;
            } else {
                bearUpper[i] = ichi.senkouB[i];
                bearLower[i] = ichi.senkouA[i];
                hasBear = true;
            }
        }
    }

    var gid = groupId || 'indicatorPack';
    if (hasBull) drawBandFill(chart, data, bullUpper, bullLower, 'ichiCloudBull', cfg.senkouAColor, cfg.cloudOpacity, gid);
    if (hasBear) drawBandFill(chart, data, bearUpper, bearLower, 'ichiCloudBear', cfg.senkouBColor, cfg.cloudOpacity, gid);
}

function drawSignalMarker(chart, data, index, type, groupId) {
    if (index < 0 || index >= data.length) return;

    const color = type === 'top' ? '#FF5252' : '#00E676';
    const label = type === 'top' ? 'PI TOP' : 'PI BOT';
    var gid = groupId || 'indicatorPack';

    chart.createOverlay({
        name: 'simpleAnnotation',
        id: 'ind_piSignal_' + index,
        groupId: gid,
        lock: true,
        points: [{ timestamp: data[index].timestamp, value: data[index].high * 1.02 }],
        styles: {
            symbol: {
                type: 'diamond',
                size: 10,
                color: color
            },
            text: {
                content: label,
                color: color,
                size: 10
            }
        }
    });
}

// Draw EMA Ribbon with dynamic colors and fill between lines
function drawEmaRibbon(chart, data, ribbon, config, groupId) {
    const bullColor = config.bullColor || '#7cb518';
    const bearColor = config.bearColor || '#fb6107';
    const neutralColor = config.neutralColor || '#f3de2c';
    const fillOpacity = config.fillOpacity || 0.1;
    var gid = groupId || 'indicatorPack';

    // Collect points for drawing
    let points = [];
    for (let i = 0; i < data.length; i++) {
        if (ribbon.fast[i] !== undefined && ribbon.slow[i] !== undefined) {
            let lineColor;
            switch (ribbon.trendColors[i]) {
                case 'bull': lineColor = bullColor; break;
                case 'bear': lineColor = bearColor; break;
                default: lineColor = neutralColor;
            }
            points.push({
                timestamp: data[i].timestamp,
                fast: ribbon.fast[i],
                slow: ribbon.slow[i],
                color: lineColor
            });
        }
    }

    if (points.length < 2) return;

    // Subsample to ~300 segments max (same approach as drawColoredMALine) — uncapped,
    // this creates TWO overlays per bar (~5,000 on a full history), every one of which
    // klinecharts re-transforms on every render frame while panning.
    const step = Math.max(1, Math.floor(points.length / 300));
    if (step > 1) {
        const sampled = [];
        for (let s = 0; s < points.length; s += step) sampled.push(points[s]);
        if (sampled[sampled.length - 1] !== points[points.length - 1]) {
            sampled.push(points[points.length - 1]);
        }
        points = sampled;
    }

    // Draw fast and slow EMA lines as segments with dynamic colors
    for (let i = 1; i < points.length; i++) {
        const segColor = points[i].color;

        // Fast EMA segment
        chart.createOverlay({
            name: 'segment',
            id: 'ind_emaRibbon_fast_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: points[i - 1].timestamp, value: points[i - 1].fast },
                { timestamp: points[i].timestamp, value: points[i].fast }
            ],
            styles: { line: { color: segColor, size: 1 } }
        });

        // Slow EMA segment
        chart.createOverlay({
            name: 'segment',
            id: 'ind_emaRibbon_slow_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: points[i - 1].timestamp, value: points[i - 1].slow },
                { timestamp: points[i].timestamp, value: points[i].slow }
            ],
            styles: { line: { color: segColor, size: 1 } }
        });
    }
}

function drawFibLevels(chart, data, fib, groupId) {
    const config = indicatorPackConfig.autoFib;
    var gid = groupId || 'indicatorPack';

    config.levels.forEach((level, idx) => {
        const price = fib.levels[level];
        const color = config.colors[idx] || '#FFFFFF';
        const label = (level * 100).toFixed(1) + '%';

        chart.createOverlay({
            name: 'horizontalRayLine',
            id: 'ind_fib_' + level,
            groupId: gid,
            lock: true,
            points: [{ timestamp: data[fib.startIndex].timestamp, value: price }],
            styles: {
                line: {
                    color: color,
                    size: 1,
                    style: 'dashed'
                },
                text: {
                    content: label + ' (' + price.toFixed(2) + ')',
                    color: color,
                    size: 10
                }
            }
        });
    });
}

function drawCPRLevels(chart, data, cpr, config, groupId) {
    // TradingView style: Pivot/TC/BC in purple, R levels in green, S levels in red
    const pivotColor = config.pivotColor || '#9C27B0';
    const rColor = config.rColor || '#00E676';
    const sColor = config.sColor || '#FF5252';

    // CPR Central Range (TC, Pivot, BC) - Purple dotted lines
    const cprLevels = [
        { value: cpr.tc, color: pivotColor, label: 'TC', id: 'tc' },
        { value: cpr.pivot, color: pivotColor, label: 'P', id: 'pivot' },
        { value: cpr.bc, color: pivotColor, label: 'BC', id: 'bc' }
    ];

    // Resistance levels - Green
    const rLevels = [
        { value: cpr.r1, color: rColor, label: 'R1', id: 'r1' },
        { value: cpr.r2, color: rColor, label: 'R2', id: 'r2' },
        { value: cpr.r3, color: rColor, label: 'R3', id: 'r3' }
    ];

    // Support levels - Red
    const sLevels = [
        { value: cpr.s1, color: sColor, label: 'S1', id: 's1' },
        { value: cpr.s2, color: sColor, label: 'S2', id: 's2' },
        { value: cpr.s3, color: sColor, label: 'S3', id: 's3' }
    ];

    // Bounded segment from period start to end of month
    var leftTs = cpr.currentPeriodStart || data[0].timestamp;
    const refTs = data[0].timestamp;
    // Normalize leftTs to chart data format (server sends seconds, chart data is ms)
    if (leftTs < 1e12 && refTs > 1e12) leftTs = leftTs * 1000;
    if (leftTs > 1e12 && refTs < 1e12) leftTs = Math.floor(leftTs / 1000);

    // Project right endpoint to the period's end using dataIndex extrapolation
    // (KLineChart can position dataIndex beyond actual data range)
    const lastIdx = data.length - 1;
    const lastTs = data[lastIdx].timestamp;
    const leftMs = leftTs > 1e12 ? leftTs : leftTs * 1000;
    const d = new Date(leftMs);
    // The right edge depends on the CPR variant (a fixed end-of-calendar-month
    // would overshoot Weekly/Daily CPR past their real period). `cpr.timeframe` is set by calculateCPR (and, for weekly/daily, by
    // the backend CPRCalculator payload too) — fall back to config.timeframe
    // (used by the generic 'cpr' case) and finally 'monthly' for the legacy
    // server monthly payload, which carries no `timeframe` field.
    const cprVariant = cpr.timeframe || (config && config.timeframe) || 'monthly';
    let periodEnd;
    if (cprVariant === 'daily') {
        // Daily CPR ends at end of the current UTC day.
        periodEnd = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 23, 59, 59));
    } else if (cprVariant === 'weekly') {
        // Weekly CPR ends at end of the current ISO week (Sunday 23:59:59 UTC,
        // weeks start Monday per calculateCPR). Normalize to the Monday of d's
        // week first rather than assuming leftTs always lands exactly on
        // Monday, then add 6 days to reach Sunday.
        const isoDow = d.getUTCDay() || 7; // Mon=1 ... Sun=7
        const mondayDate = d.getUTCDate() - (isoDow - 1);
        periodEnd = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), mondayDate + 6, 23, 59, 59));
    } else {
        // Monthly (and quarterly/yearly, unchanged) — end of calendar month.
        periodEnd = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0, 23, 59, 59));
    }
    const monthEndMs = periodEnd.getTime();
    const lastTsMs = lastTs > 1e12 ? lastTs : lastTs * 1000;
    // Calculate bar interval from last 2 bars
    var barInterval = 86400000; // default 1 day in ms
    if (data.length >= 2) {
        const t1 = data[lastIdx].timestamp;
        const t2 = data[lastIdx - 1].timestamp;
        const diff = (t1 > 1e12 ? t1 : t1 * 1000) - (t2 > 1e12 ? t2 : t2 * 1000);
        if (diff > 0) barInterval = diff;
    }
    const extraBars = Math.max(0, Math.ceil((monthEndMs - lastTsMs) / barInterval));
    const rightDataIndex = lastIdx + extraBars;

    const allLevels = [...cprLevels, ...rLevels, ...sLevels];
    const gid = groupId || 'ind_cpr';

    // Compute visible price range for outlier filtering
    var visHigh = -Infinity, visLow = Infinity;
    for (var vi = Math.max(0, data.length - 200); vi < data.length; vi++) {
        if (data[vi].high > visHigh) visHigh = data[vi].high;
        if (data[vi].low < visLow) visLow = data[vi].low;
    }
    var visRange = visHigh - visLow;

    allLevels.forEach((level) => {
        if (level.value == null || level.value <= 0) return;
        var lv = _logPrice(level.value);
        // Skip levels that are too far from visible range (would squeeze Y-axis)
        if (visRange > 0 && (lv < visLow - visRange * 3 || lv > visHigh + visRange * 3)) return;
        chart.createOverlay({
            name: 'segment',
            id: 'ind_cpr_' + level.id,
            groupId: gid,
            lock: true,
            visible: true,
            points: [
                { timestamp: leftTs, value: lv },
                { dataIndex: rightDataIndex, value: lv }
            ],
            styles: {
                line: {
                    style: 'dashed',
                    dashedValue: [6, 4],
                    color: level.color,
                    size: 2
                }
            }
        });
    });
}

// Calculate CPR for past months from chart data (client-side fallback)
function calculateCPRHistory(data, numMonths) {
    if (!data || data.length < 60) return null;

    function getYM(ts) {
        const ms = ts < 1e12 ? ts * 1000 : ts;
        const d = new Date(ms);
        return { y: d.getUTCFullYear(), m: d.getUTCMonth() };
    }

    // Group bars by month
    const months = {};
    data.forEach(bar => {
        const ym = getYM(bar.timestamp);
        const key = ym.y + '-' + String(ym.m).padStart(2, '0');
        if (!months[key]) months[key] = { y: ym.y, m: ym.m, bars: [] };
        months[key].bars.push(bar);
    });

    const sortedKeys = Object.keys(months).sort();
    if (sortedKeys.length < 3) return null;

    // Current month is last key — skip it (handled by main CPR)
    const history = [];
    // Walk backwards from 2nd-to-last key (that's the month whose CPR is already shown as "current")
    // We want CPR for months BEFORE that
    for (let i = sortedKeys.length - 2; i >= 1 && history.length < numMonths; i--) {
        const applyKey = sortedKeys[i];    // month where CPR lines appear
        const srcKey = sortedKeys[i - 1];  // previous month whose H/L/C generates the CPR
        const srcBars = months[srcKey].bars;
        const applyBars = months[applyKey].bars;
        if (srcBars.length === 0 || applyBars.length === 0) continue;

        const h = Math.max(...srcBars.map(b => b.high));
        const l = Math.min(...srcBars.map(b => b.low));
        const c = srcBars[srcBars.length - 1].close;
        const pivot = (h + l + c) / 3;
        const bc = (h + l) / 2;
        const tc = pivot * 2 - bc;

        history.push({
            pivot: Math.round(pivot * 100) / 100,
            tc: Math.round(tc * 100) / 100,
            bc: Math.round(bc * 100) / 100,
            periodStart: applyBars[0].timestamp / 1000,  // seconds to match server format
            periodEnd: applyBars[applyBars.length - 1].timestamp / 1000
        });
    }

    return history.length > 0 ? history : null;
}

// Draw historical CPR lines — TC/P/BC bounded segment per month
function drawCPRHistory(chart, data, history, config, groupId) {
    const color = config.pivotColor || 'rgba(156, 39, 176, 0.45)';
    const lineWidth = config.lineWidth || 1;
    const gid = groupId || 'ind_cprHistory';
    // Reference timestamp from chart data to normalize formats
    const refTs = data.length > 0 ? data[data.length - 1].timestamp : 0;

    history.forEach((h, idx) => {
        var startTs = (h.periodStart < 1e12) ? h.periodStart * 1000 : h.periodStart;
        var endTs = (h.periodEnd < 1e12) ? h.periodEnd * 1000 : h.periodEnd;
        // Normalize to chart data timestamp format (may be seconds, not ms)
        if (startTs > 1e12 && refTs < 1e12) startTs = Math.floor(startTs / 1000);
        if (endTs > 1e12 && refTs < 1e12) endTs = Math.floor(endTs / 1000);
        const levels = [
            { value: h.tc, id: 'tc' },
            { value: h.pivot, id: 'p' },
            { value: h.bc, id: 'bc' }
        ];

        levels.forEach(lvl => {
            if (lvl.value == null) return;
            chart.createOverlay({
                name: 'segment',
                id: 'ind_cpr_hist_' + idx + '_' + lvl.id,
                groupId: gid,
                lock: true,
                visible: true,
                points: [
                    { timestamp: startTs, value: lvl.value },
                    { timestamp: endTs, value: lvl.value }
                ],
                styles: {
                    line: {
                        style: 'dashed',
                        dashedValue: [4, 4],
                        color: color,
                        size: lineWidth
                    }
                }
            });
        });
    });
}

// Draw Key Levels (IDWM - Daily, Weekly, Monthly, Quarterly, Yearly)
function drawKeyLevels(chart, data, levels, config, groupId) {
    const lastTimestamp = data[data.length - 1].timestamp;
    const sh = config.shorthand;
    let levelIdx = 0;
    var gid = groupId || 'indicatorPack';

    // Helper to draw a level line
    function drawLevel(value, color, label, id) {
        if (value === null || value === undefined) return;
        chart.createOverlay({
            name: 'horizontalStraightLine',
            id: 'ind_keylevel_' + id,
            groupId: gid,
            lock: true,
            points: [{ timestamp: lastTimestamp, value: value }],
            styles: {
                line: {
                    color: color,
                    size: config.lineWidth || 1,
                    style: config.lineStyle || 'solid'
                }
            }
        });
    }

    // Daily levels
    if (config.daily) {
        if (config.daily.open?.enabled) {
            drawLevel(levels.daily.open, config.daily.open.color, sh ? 'DO' : 'Daily Open', 'd_open');
        }
        if (config.daily.prevHigh?.enabled) {
            drawLevel(levels.daily.prevHigh, config.daily.prevHigh.color, sh ? 'PDH' : 'Prev Day High', 'd_ph');
        }
        if (config.daily.prevLow?.enabled) {
            drawLevel(levels.daily.prevLow, config.daily.prevLow.color, sh ? 'PDL' : 'Prev Day Low', 'd_pl');
        }
        if (config.daily.prevMid?.enabled) {
            drawLevel(levels.daily.prevMid, config.daily.prevMid.color, sh ? 'PDM' : 'Prev Day Mid', 'd_pm');
        }
    }

    // Weekly levels
    if (config.weekly) {
        if (config.weekly.open?.enabled) {
            drawLevel(levels.weekly.open, config.weekly.open.color, sh ? 'WO' : 'Weekly Open', 'w_open');
        }
        if (config.weekly.prevHigh?.enabled) {
            drawLevel(levels.weekly.prevHigh, config.weekly.prevHigh.color, sh ? 'PWH' : 'Prev Week High', 'w_ph');
        }
        if (config.weekly.prevLow?.enabled) {
            drawLevel(levels.weekly.prevLow, config.weekly.prevLow.color, sh ? 'PWL' : 'Prev Week Low', 'w_pl');
        }
        if (config.weekly.prevMid?.enabled) {
            drawLevel(levels.weekly.prevMid, config.weekly.prevMid.color, sh ? 'PWM' : 'Prev Week Mid', 'w_pm');
        }
    }

    // Monthly levels
    if (config.monthly) {
        if (config.monthly.open?.enabled) {
            drawLevel(levels.monthly.open, config.monthly.open.color, sh ? 'MO' : 'Monthly Open', 'm_open');
        }
        if (config.monthly.prevHigh?.enabled) {
            drawLevel(levels.monthly.prevHigh, config.monthly.prevHigh.color, sh ? 'PMH' : 'Prev Month High', 'm_ph');
        }
        if (config.monthly.prevLow?.enabled) {
            drawLevel(levels.monthly.prevLow, config.monthly.prevLow.color, sh ? 'PML' : 'Prev Month Low', 'm_pl');
        }
        if (config.monthly.prevMid?.enabled) {
            drawLevel(levels.monthly.prevMid, config.monthly.prevMid.color, sh ? 'PMM' : 'Prev Month Mid', 'm_pm');
        }
    }

    // Quarterly levels
    if (config.quarterly) {
        if (config.quarterly.open?.enabled) {
            drawLevel(levels.quarterly.open, config.quarterly.open.color, sh ? 'QO' : 'Quarterly Open', 'q_open');
        }
        if (config.quarterly.prevMid?.enabled) {
            drawLevel(levels.quarterly.prevMid, config.quarterly.prevMid.color, sh ? 'PQM' : 'Prev Quarter Mid', 'q_pm');
        }
    }

    // Yearly levels
    if (config.yearly) {
        if (config.yearly.open?.enabled) {
            drawLevel(levels.yearly.open, config.yearly.open.color, sh ? 'YO' : 'Yearly Open', 'y_open');
        }
        if (config.yearly.currentMid?.enabled) {
            drawLevel(levels.yearly.currentMid, config.yearly.currentMid.color, sh ? 'CYM' : 'Current Year Mid', 'y_cm');
        }
    }
}

// Draw Support Resistance Channels (LonesomeTheBlue)
function drawSRChannels(chart, data, zones, config, groupId) {
    if (!zones || zones.length === 0) return;

    const firstTimestamp = data[0].timestamp;
    const lastTimestamp = data[data.length - 1].timestamp;
    var gid = groupId || 'indicatorPack';

    zones.forEach((zone, idx) => {
        // Determine color based on zone type
        let fillColor;
        if (zone.type === 'support') {
            fillColor = config.supportColor || 'rgba(0, 255, 0, 0.25)';
        } else if (zone.type === 'resistance') {
            fillColor = config.resistanceColor || 'rgba(255, 0, 0, 0.25)';
        } else {
            fillColor = config.insideColor || 'rgba(128, 128, 128, 0.25)';
        }

        // Draw the SR zone as a rectangle that extends across the visible chart
        chart.createOverlay({
            name: 'rect',
            id: 'ind_sr_zone_' + idx,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: firstTimestamp, value: zone.hi },
                { timestamp: lastTimestamp, value: zone.lo }
            ],
            styles: {
                style: 'fill',
                color: fillColor,
                borderColor: fillColor.replace(/[\d.]+\)$/, '0.5)'), // Slightly more opaque border
                borderSize: 1
            }
        });

        // Add a label for the zone strength
        const midValue = (zone.hi + zone.lo) / 2;
        const typeLabel = zone.type === 'support' ? 'S' : zone.type === 'resistance' ? 'R' : 'SR';

        chart.createOverlay({
            name: 'simpleTag',
            id: 'ind_sr_label_' + idx,
            groupId: gid,
            lock: true,
            points: [{ timestamp: lastTimestamp, value: midValue }],
            styles: {
                backgroundColor: fillColor.replace(/[\d.]+\)$/, '0.7)'),
                text: typeLabel + (idx + 1)
            }
        });
    });
}

// Draw Fair Value Gaps — enhanced with mitigation tracking
function drawFVG(chart, data, fvgData, config, groupId) {
    if (!fvgData) return;

    const lastTimestamp = data[data.length - 1].timestamp;
    var gid = groupId || 'indicatorPack';
    var mitigatedMult = config.mitigatedOpacity || 0.4;

    // Helper: adjust rgba opacity for mitigated gaps
    function dimColor(rgba, mult) {
        return rgba.replace(/([\d.]+)\)$/, function(_, a) {
            return (parseFloat(a) * mult).toFixed(2) + ')';
        });
    }

    function drawGaps(gaps, baseColor, prefix) {
        gaps.forEach(function(gap, idx) {
            var isMitigated = gap.filled;
            var fillColor = isMitigated ? dimColor(baseColor, mitigatedMult) : baseColor;
            var border = isMitigated ? dimColor(config.borderColor, mitigatedMult) : config.borderColor;
            // Mitigated gaps extend only to their fill point, not to chart edge
            var endTime = isMitigated && gap.filledIndex > 0
                ? data[Math.min(gap.filledIndex, data.length - 1)].timestamp
                : lastTimestamp;

            chart.createOverlay({
                name: 'rect',
                id: 'ind_fvg_' + prefix + '_' + idx,
                groupId: gid,
                lock: true,
                points: [
                    { timestamp: gap.timestamp, value: gap.top },
                    { timestamp: endTime, value: gap.bottom }
                ],
                styles: {
                    style: 'fill',
                    color: fillColor,
                    borderSize: 1,
                    borderColor: border
                }
            });

            if (config.showMidLine && !isMitigated) {
                chart.createOverlay({
                    name: 'segment',
                    id: 'ind_fvg_' + prefix + '_mid_' + idx,
                    groupId: gid,
                    lock: true,
                    points: [
                        { timestamp: gap.timestamp, value: gap.mid },
                        { timestamp: endTime, value: gap.mid }
                    ],
                    styles: {
                        style: 'dashed',
                        color: config.midLineColor,
                        size: 1
                    }
                });
            }
        });
    }

    drawGaps(fvgData.bullish, config.bullishColor, 'bull');
    drawGaps(fvgData.bearish, config.bearishColor, 'bear');
}

// ============================================================================
// TTM Squeeze Pane (Sub-indicator)
// ============================================================================

function drawTTMSqueezePane(chart, data, serverData, groupId) {
    var config = indicatorPackConfig.ttmSqueeze;
    if (!config.enabled || !data || data.length < 2) return;
    var gid = groupId || 'indicatorPack';

    // The server precomputes ttmSqueeze with its own fixed params
    // (BB(30, 2.0), KC(30, 2.5), duration >= 3 — carrac/charts/routes.py TTM
    // block). That payload is only valid when the user's config still equals
    // those defaults; if any param was customized, the server data no longer
    // reflects what the user configured, so recompute client-side instead.
    // Default settings keep the server path byte-identical (gold-standard
    // consistency) — only a non-default config diverts to calculateTTMSqueeze.
    var isDefaultTTMConfig = config.bbLength === 30 && config.bbMult === 2.0 &&
        config.kcLength === 30 && config.kcMult === 2.5 && (config.minDuration || 3) === 3;

    // Build set of squeeze-ON timestamps
    var squeezeSet = {};
    if (isDefaultTTMConfig && serverData && serverData.indicators && serverData.indicators.ttmSqueeze &&
        serverData.indicators.ttmSqueeze.length > 0) {
        // Server data: timestamps in seconds, convert to ms
        for (var i = 0; i < serverData.indicators.ttmSqueeze.length; i++) {
            squeezeSet[serverData.indicators.ttmSqueeze[i].time * 1000] = true;
        }
    } else {
        // Client-side — either no server data, or the user customized params.
        var result = calculateTTMSqueeze(data);
        for (var i = 0; i < data.length; i++) {
            if (result.squeeze[i]) squeezeSet[data[i].timestamp] = true;
        }
    }

    // Collect squeeze bar timestamps in chart order
    var squeezeTimes = [];
    for (var i = 0; i < data.length; i++) {
        if (squeezeSet[data[i].timestamp]) {
            squeezeTimes.push({ ts: data[i].timestamp, low: data[i].low });
        }
    }
    if (squeezeTimes.length === 0) return;

    // Bar interval for gap detection
    var barInterval = data.length > 1 ? data[1].timestamp - data[0].timestamp : 86400000;

    // Group consecutive squeeze bars into segments
    var segments = [];
    var segStart = 0;
    for (var i = 1; i <= squeezeTimes.length; i++) {
        var isGap = (i === squeezeTimes.length) ||
            (squeezeTimes[i].ts - squeezeTimes[i - 1].ts > barInterval * 1.5);
        if (isGap) {
            segments.push({ start: segStart, end: i - 1 });
            segStart = i;
        }
    }

    // Draw each segment as a thick yellow line just below candle lows
    for (var s = 0; s < segments.length; s++) {
        var seg = segments[s];
        var minLow = Infinity;
        for (var j = seg.start; j <= seg.end; j++) {
            if (squeezeTimes[j].low < minLow) minLow = squeezeTimes[j].low;
        }
        var dotY = minLow * 0.998;
        var startTs = squeezeTimes[seg.start].ts;
        var endTs = squeezeTimes[seg.end].ts;
        // Single-bar segment: extend slightly so segment is visible
        if (startTs === endTs) {
            startTs -= barInterval * 0.3;
            endTs += barInterval * 0.3;
        }
        chart.createOverlay({
            name: 'segment',
            id: 'ind_squeeze_' + s,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: startTs, value: dotY },
                { timestamp: endTs, value: dotY }
            ],
            styles: {
                line: {
                    color: config.squeezeOnColor,
                    size: 4,
                    style: 'solid'
                }
            }
        });
    }
}

// ============================================================================
// RSI Divergence Detection
// ============================================================================

function calculateRSI(data, period) {
    if (!data || data.length < period + 1) return [];
    var rsi = new Array(data.length);
    var avgGain = 0, avgLoss = 0;
    // First average: simple mean over first 'period' changes
    for (var i = 1; i <= period; i++) {
        var change = data[i].close - data[i - 1].close;
        if (change > 0) avgGain += change; else avgLoss -= change;
    }
    avgGain /= period;
    avgLoss /= period;
    rsi[period] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
    // Wilder's smoothing
    for (var j = period + 1; j < data.length; j++) {
        var ch = data[j].close - data[j - 1].close;
        avgGain = (avgGain * (period - 1) + (ch > 0 ? ch : 0)) / period;
        avgLoss = (avgLoss * (period - 1) + (ch < 0 ? -ch : 0)) / period;
        rsi[j] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
    }
    return rsi;
}

function findSwingLows(values, lookback) {
    var swings = [];
    for (var i = lookback; i < values.length - lookback; i++) {
        if (values[i] === undefined) continue;
        var isLow = true;
        for (var j = 1; j <= lookback; j++) {
            if (values[i - j] === undefined || values[i + j] === undefined ||
                values[i] >= values[i - j] || values[i] >= values[i + j]) {
                isLow = false; break;
            }
        }
        if (isLow) swings.push({ index: i, value: values[i] });
    }
    return swings;
}

function findSwingHighs(values, lookback) {
    var swings = [];
    for (var i = lookback; i < values.length - lookback; i++) {
        if (values[i] === undefined) continue;
        var isHigh = true;
        for (var j = 1; j <= lookback; j++) {
            if (values[i - j] === undefined || values[i + j] === undefined ||
                values[i] <= values[i - j] || values[i] <= values[i + j]) {
                isHigh = false; break;
            }
        }
        if (isHigh) swings.push({ index: i, value: values[i] });
    }
    return swings;
}

function detectDivergences(data, rsi, config) {
    var lb = config.lookback || 5;
    var maxBars = config.maxBarsBetween || 60;
    var minBars = 5;

    // Find pivots on the RSI line (like TradingView), then check price for divergence
    var rsiLowSwings = findSwingLows(rsi, lb);
    var rsiHighSwings = findSwingHighs(rsi, lb);

    var divergences = [];

    // Bullish: RSI makes higher low, but price makes lower low
    for (var i = 1; i < rsiLowSwings.length; i++) {
        var curr = rsiLowSwings[i];
        var prev = rsiLowSwings[i - 1];
        var gap = curr.index - prev.index;
        if (gap > maxBars || gap < minBars) continue;

        var priceCurr = data[curr.index].low;
        var pricePrev = data[prev.index].low;

        // Regular bullish: RSI higher low, price lower low
        if (curr.value > prev.value && priceCurr < pricePrev) {
            divergences.push({
                type: 'bullish',
                priceFrom: { index: prev.index, value: pricePrev },
                priceTo: { index: curr.index, value: priceCurr },
                rsiFrom: { index: prev.index, value: prev.value },
                rsiTo: { index: curr.index, value: curr.value }
            });
        }
        // Hidden bullish: RSI lower low, price higher low
        if (curr.value < prev.value && priceCurr > pricePrev) {
            divergences.push({
                type: 'hidden_bullish',
                priceFrom: { index: prev.index, value: pricePrev },
                priceTo: { index: curr.index, value: priceCurr },
                rsiFrom: { index: prev.index, value: prev.value },
                rsiTo: { index: curr.index, value: curr.value }
            });
        }
    }

    // Bearish divergences from RSI high pivots
    for (var i = 1; i < rsiHighSwings.length; i++) {
        var curr = rsiHighSwings[i];
        var prev = rsiHighSwings[i - 1];
        var gap = curr.index - prev.index;
        if (gap > maxBars || gap < minBars) continue;

        var priceCurr = data[curr.index].high;
        var pricePrev = data[prev.index].high;

        // Regular bearish: RSI lower high, price higher high
        if (curr.value < prev.value && priceCurr > pricePrev) {
            divergences.push({
                type: 'bearish',
                priceFrom: { index: prev.index, value: pricePrev },
                priceTo: { index: curr.index, value: priceCurr },
                rsiFrom: { index: prev.index, value: prev.value },
                rsiTo: { index: curr.index, value: curr.value }
            });
        }
        // Hidden bearish: RSI higher high, price lower high
        if (curr.value > prev.value && priceCurr < pricePrev) {
            divergences.push({
                type: 'hidden_bearish',
                priceFrom: { index: prev.index, value: pricePrev },
                priceTo: { index: curr.index, value: priceCurr },
                rsiFrom: { index: prev.index, value: prev.value },
                rsiTo: { index: curr.index, value: curr.value }
            });
        }
    }

    // Deduplicate overlapping divergences, keep strongest per category
    return _filterOverlapping(
        divergences.filter(function(d) { return d.type === 'bullish' || d.type === 'hidden_bullish'; })
    ).concat(_filterOverlapping(
        divergences.filter(function(d) { return d.type === 'bearish' || d.type === 'hidden_bearish'; })
    ));
}

function _filterOverlapping(divs) {
    if (divs.length <= 1) return divs;
    divs.sort(function(a, b) {
        return Math.abs(b.rsiTo.value - b.rsiFrom.value) - Math.abs(a.rsiTo.value - a.rsiFrom.value);
    });
    var kept = [];
    for (var i = 0; i < divs.length; i++) {
        var dominated = false;
        for (var j = 0; j < kept.length; j++) {
            if (divs[i].rsiFrom.index <= kept[j].rsiTo.index &&
                divs[i].rsiTo.index >= kept[j].rsiFrom.index) {
                dominated = true; break;
            }
        }
        if (!dominated) kept.push(divs[i]);
    }
    return kept;
}

function drawRSIDivergences(chart, data, config, groupId) {
    if (!config.enabled) return;
    if (!klineActiveIndicators.RSI) return;
    var rsi = calculateRSI(data, config.period || 14);
    var divergences = detectDivergences(data, rsi, config);
    var gid = groupId || 'indicatorPack';

    for (var i = 0; i < divergences.length; i++) {
        var div = divergences[i];
        var isBull = div.type === 'bullish' || div.type === 'hidden_bullish';
        var isHidden = div.type === 'hidden_bullish' || div.type === 'hidden_bearish';
        var color = isBull
            ? (isHidden ? config.hiddenBullColor : config.bullColor)
            : (isHidden ? config.hiddenBearColor : config.bearColor);
        var dashValue = isHidden ? [6, 4] : undefined;
        var lw = config.lineWidth || 2;

        // Line on RSI pane (connecting RSI swing points)
        chart.createOverlay({
            name: 'segment',
            id: 'ind_rsidiv_rsi_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: data[div.rsiFrom.index].timestamp, value: div.rsiFrom.value },
                { timestamp: data[div.rsiTo.index].timestamp, value: div.rsiTo.value }
            ],
            styles: { line: { color: color, size: lw, dashedValue: dashValue } }
        }, 'rsi_pane');

        // Label at the right endpoint of the line
        var labelText = isBull ? (isHidden ? 'H.Bull' : 'Bull') : (isHidden ? 'H.Bear' : 'Bear');
        var labelOffset = isBull ? -3 : 3;
        chart.createOverlay({
            name: 'textLabel',
            id: 'ind_rsidiv_lbl_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: data[div.rsiTo.index].timestamp, value: div.rsiTo.value + labelOffset }
            ],
            extendData: labelText,
            styles: { text: { color: color, size: 10 } }
        }, 'rsi_pane');
    }
    if (divergences.length > 0) {
    }
}

// ============================================================================
// CVD Spot Divergence (reuses RSI divergence detection on detrended CVD)
// ============================================================================

function drawCVDDivergences(chart, data, serverData, config, groupId) {
    if (!config.enabled) return;
    if (!klineActiveIndicators.CVD_SPOT) return;

    var cvdRaw = serverData && serverData.cvd_spot;
    if (!cvdRaw || cvdRaw.length < 30) return;

    // Build a CVD array aligned with main data indices (by timestamp)
    var tsMap = {};
    for (var i = 0; i < cvdRaw.length; i++) {
        tsMap[cvdRaw[i].time] = cvdRaw[i].cvd;  // already detrended from server
    }

    var cvdAligned = [];
    for (var i = 0; i < data.length; i++) {
        var ts = Math.floor(data[i].timestamp / 1000);
        cvdAligned[i] = tsMap[ts] !== undefined ? tsMap[ts] : undefined;
    }

    // Reuse the same divergence detection as RSI, but only regular (no hidden)
    var divergences = detectDivergences(data, cvdAligned, config).filter(function(d) {
        return d.type === 'bullish' || d.type === 'bearish';
    });
    var gid = groupId || 'indicatorPack';

    for (var i = 0; i < divergences.length; i++) {
        var div = divergences[i];
        var isBull = div.type === 'bullish' || div.type === 'hidden_bullish';
        var isHidden = div.type === 'hidden_bullish' || div.type === 'hidden_bearish';
        var color = isBull
            ? (isHidden ? config.hiddenBullColor : config.bullColor)
            : (isHidden ? config.hiddenBearColor : config.bearColor);
        var dashValue = isHidden ? [6, 4] : undefined;
        var lw = config.lineWidth || 2;

        // Line on CVD pane (connecting CVD swing points)
        chart.createOverlay({
            name: 'segment',
            id: 'ind_cvddiv_cvd_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: data[div.rsiFrom.index].timestamp, value: div.rsiFrom.value },
                { timestamp: data[div.rsiTo.index].timestamp, value: div.rsiTo.value }
            ],
            styles: { line: { color: color, size: lw, dashedValue: dashValue } }
        }, 'cvd_spot_pane');

        // Label at the right endpoint
        var labelText = isBull ? (isHidden ? 'H.Bull' : 'Bull') : (isHidden ? 'H.Bear' : 'Bear');
        // Scale label offset based on CVD range
        var cvdRange = 0;
        for (var ci = 0; ci < cvdAligned.length; ci++) {
            if (cvdAligned[ci] !== undefined) {
                var absVal = Math.abs(cvdAligned[ci]);
                if (absVal > cvdRange) cvdRange = absVal;
            }
        }
        var labelOffset = (isBull ? -1 : 1) * cvdRange * 0.05;

        chart.createOverlay({
            name: 'textLabel',
            id: 'ind_cvddiv_lbl_' + i,
            groupId: gid,
            lock: true,
            points: [
                { timestamp: data[div.rsiTo.index].timestamp, value: div.rsiTo.value + labelOffset }
            ],
            extendData: labelText,
            styles: { text: { color: color, size: 10 } }
        }, 'cvd_spot_pane');
    }
    if (divergences.length > 0) {
    }
}

// ============================================================================
// Consensio (SMA 3/7/30 with transition signals and multi-TF table)
// ============================================================================

// Register bandFill overlay — filled polygon between upper/lower bands
// Used by: Keltner, Keltner Tight, Donchian, Bollinger, Ichimoku Cloud, EMA Ribbon
(function() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerOverlay({
            name: 'bandFill',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                if (!coords || coords.length < 4) return [];
                var rgba = params.overlay.extendData || 'rgba(33,150,243,0.1)';
                return [{
                    type: 'polygon',
                    attrs: { coordinates: coords },
                    styles: { style: 'fill', color: rgba }
                }];
            }
        });
    } catch (e) {}
})();

// Register dualBandFill — single overlay with multiple colored polygon segments
(function() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerOverlay({
            name: 'dualBandFill',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var coords = params.coordinates;
                var segMeta = params.overlay.extendData;
                if (!coords || !segMeta || segMeta.length === 0) return [];
                var figures = [];
                for (var s = 0; s < segMeta.length; s++) {
                    var meta = segMeta[s];
                    var segCoords = coords.slice(meta.start, meta.start + meta.count);
                    if (segCoords.length < 4) continue;
                    figures.push({
                        type: 'polygon',
                        attrs: { coordinates: segCoords },
                        styles: { style: 'fill', color: meta.rgba }
                    });
                }
                return figures;
            }
        });
    } catch (e) {}
})();

// Register a text-only overlay (no marker, no line, no rectangle)
(function() {
    if (typeof klinecharts === 'undefined') return;
    try {
        klinecharts.registerOverlay({
            name: 'textLabel',
            totalStep: 2,
            needDefaultPointFigure: false,
            needDefaultXAxisFigure: false,
            needDefaultYAxisFigure: false,
            createPointFigures: function(params) {
                var overlay = params.overlay;
                var coords = params.coordinates;
                if (!coords || !coords[0]) return [];
                var label = overlay.extendData || '';
                var styles = overlay.styles || {};
                var textStyles = styles.text || {};
                return [{
                    type: 'text',
                    attrs: {
                        x: coords[0].x,
                        y: coords[0].y,
                        text: label,
                        align: 'center',
                        baseline: 'bottom'
                    },
                    styles: {
                        color: textStyles.color || '#888',
                        size: textStyles.size || 10,
                        family: textStyles.family || 'JetBrains Mono, monospace',
                        weight: textStyles.weight || 'bold'
                    }
                }];
            }
        });
    } catch(e) { console.warn('[Consensio] textLabel register error:', e); }
})();

function calculateConsensio(data) {
    var cfg = indicatorPackConfig.consensio;
    var smaS = calculateSMA(data, cfg.periodS);
    var smaM = calculateSMA(data, cfg.periodM);
    var smaL = calculateSMA(data, cfg.periodL);

    // Determine signal at each bar based on MA ordering only (price is NOT a
    // factor — see consensio_core.py ConsensioCalculator.calculate_signal).
    // truth table mirrors consensio_core.py calculate_signal — keep in sync
    //   S > M > L  ->  +3
    //   M > S > L  ->  +2
    //   S > L > M  ->  +1
    //   M > L > S  ->  -1
    //   L > S > M  ->  -2
    //   L > M > S  ->  -3
    //   (ties / any other ordering) -> 0
    var signals = [];
    var prevSignal = 0;
    var transitions = [];

    for (var i = 0; i < data.length; i++) {
        if (smaS[i] === undefined || smaM[i] === undefined || smaL[i] === undefined) {
            signals.push(0);
            continue;
        }

        var s = smaS[i], m = smaM[i], l = smaL[i];
        var signal = 0;

        if (s > m && m > l) {
            signal = 3;
        } else if (m > s && s > l) {
            signal = 2;
        } else if (s > l && l > m) {
            signal = 1;
        } else if (m > l && l > s) {
            signal = -1;
        } else if (l > s && s > m) {
            signal = -2;
        } else if (l > m && m > s) {
            signal = -3;
        }

        signals.push(signal);

        // Record transitions only
        if (signal !== prevSignal && prevSignal !== 0 && i > cfg.periodL) {
            transitions.push({ index: i, level: signal });
        }
        prevSignal = signal;
    }

    return { short: smaS, medium: smaM, long: smaL, signals: signals, transitions: transitions };
}

function drawConsensio(chart, data, consensio, config, serverData, drawLines, groupId) {
    var gid = groupId || 'indicatorPack';
    // Draw 3 SMA lines (skipped for lite mode)
    if (drawLines !== false) {
        drawMALine(chart, data, consensio.short, config.shortColor, 'consensioS', config.lineWidth, gid);
        drawMALine(chart, data, consensio.medium, config.mediumColor, 'consensioM', config.lineWidth, gid);
        drawMALine(chart, data, consensio.long, config.longColor, 'consensioL', config.lineWidth, gid);
    }

    // Store signals for crosshair-driven MA row inside the table
    window._consensioSignals = drawLines ? consensio.signals : null;
    window._consensioConfig = config;

    // Draw multi-TF table (HTML overlay) — MA row only for full consensio
    if (config.showTable) {
        drawConsensioTable(serverData, drawLines ? chart : null);
    }

    // Show latest bar's signal immediately in the table MA row (full consensio only)
    if (drawLines) {
        var lastSig = 0;
        for (var i = data.length - 1; i >= 0; i--) {
            if (consensio.signals[i] !== 0 && consensio.signals[i] !== undefined) {
                lastSig = consensio.signals[i]; break;
            }
        }
        _updateTableMaValue(lastSig);
    }
}

// Runs on EVERY crosshair-changing mousemove (synchronously, before the canvas
// repaint) — cache the element and skip no-op writes instead of re-querying the DOM.
var _tableMaCache = { el: null, sig: undefined };
function _updateTableMaValue(sig) {
    var el = _tableMaCache.el;
    if (!el || !el.isConnected) {
        el = document.querySelector('[data-consensio-ma]');
        _tableMaCache.el = el;
        _tableMaCache.sig = undefined; // fresh element — force one write
    }
    if (!el) return;
    if (sig === _tableMaCache.sig) return;
    _tableMaCache.sig = sig;
    if (sig === 0 || sig === undefined) {
        el.style.color = '#6b7280';
        el.textContent = '0';
        return;
    }
    var cfg = window._consensioConfig || {};
    el.style.color = sig > 0 ? (cfg.bullColor || '#26A69A') : (cfg.bearColor || '#EF5350');
    el.textContent = (sig > 0 ? '+' : '') + sig;
}

function drawConsensioTable(serverData, chart) {
    // Only remove the table element, not the signal label
    var existingTable = document.querySelector('.consensio-chart-table');
    if (existingTable) existingTable.remove();

    var consensioData = serverData && serverData.indicators && serverData.indicators.consensio;
    if (!consensioData) return;

    var container = document.getElementById('adv-chart-content');
    if (!container) return;

    function tfColor(pos, sig) {
        if (sig > 0) return '#26a69a';
        if (sig < 0) return '#ef5350';
        return '#6b7280';
    }

    var d1 = consensioData['1D'] || { signal: 0, position: 0 };
    var d3 = consensioData['3D'] || { signal: 0, position: 0 };
    var w1 = consensioData['1W'] || { signal: 0, position: 0 };
    var total = consensioData.total || 0;
    var totalColor = total >= 6 ? '#26a69a' : total >= 3 ? '#f59e0b' : '#ef5350';

    // Position below toolbar rows (absolute overlay)
    var _toolbar = document.querySelector('.adv-chart-toolbar');
    var _consTop = _toolbar ? (_toolbar.offsetHeight + 6) : 34;
    var html = '<div class="consensio-chart-table" style="position:absolute;top:' + _consTop + 'px;right:90px;background:rgba(13,17,23,0.85);border:1px solid #30363d;border-radius:4px;padding:6px 10px;font-family:\'JetBrains Mono\',monospace;font-size:11px;z-index:5;pointer-events:none;">';
    html += '<div style="color:#8b949e;margin-bottom:4px;font-weight:600;font-size:10px;letter-spacing:0.5px;text-align:center;">[CONSENSIO]</div>';

    var rows = [
        { label: '1D', sig: d1.signal, pos: d1.position },
        { label: '3D', sig: d3.signal, pos: d3.position },
        { label: '1W', sig: w1.signal, pos: w1.position }
    ];

    for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        var c = tfColor(r.pos, r.sig);
        html += '<div style="display:flex;justify-content:space-between;gap:14px;line-height:1.5;">';
        html += '<span style="color:#6b7280;">' + r.label + '</span>';
        html += '<span style="color:' + c + ';">' + r.pos + '/3</span>';
        html += '</div>';
    }

    html += '<div style="border-top:1px solid #30363d;margin-top:3px;padding-top:3px;display:flex;justify-content:space-between;gap:14px;line-height:1.5;">';
    html += '<span style="color:#e6edf3;font-weight:bold;">Total</span>';
    html += '<span style="color:' + totalColor + ';font-weight:bold;">' + total + '/9</span>';
    html += '</div>';

    // MA crossing value row — full consensio only (chart param is non-null)
    if (chart) {
        html += '<div style="border-top:1px solid #30363d;margin-top:3px;padding-top:3px;display:flex;justify-content:space-between;gap:14px;line-height:1.5;font-size:10px;">';
        html += '<span style="color:#6b7280;">MA</span>';
        html += '<span data-consensio-ma style="color:#6b7280;">0</span>';
        html += '</div>';
    }

    html += '</div>';

    container.insertAdjacentHTML('beforeend', html);

    // Subscribe to crosshair changes to update MA value in table (full consensio only)
    if (chart && !window._consensioCrosshairBound) {
        window._consensioCrosshairBound = true;
        window._consensioCrosshairChart = chart;
        window._consensioCrosshairHandler = function(params) {
            if (!window._consensioSignals) return;
            var signals = window._consensioSignals;
            if (!params || !params.dataIndex) {
                // Crosshair off — show latest
                for (var i = signals.length - 1; i >= 0; i--) {
                    if (signals[i] !== 0 && signals[i] !== undefined) {
                        _updateTableMaValue(signals[i]); return;
                    }
                }
                return;
            }
            var idx = params.dataIndex;
            if (idx >= 0 && idx < signals.length) {
                _updateTableMaValue(signals[idx]);
            }
        };
        try {
            chart.subscribeAction('onCrosshairChange', window._consensioCrosshairHandler);
        } catch(e) {}
    }
}

function _repositionConsensioTable() {
    var table = document.querySelector('.consensio-chart-table');
    if (!table) return;
    var toolbar = document.querySelector('.adv-chart-toolbar');
    table.style.top = (toolbar ? (toolbar.offsetHeight + 6) : 34) + 'px';
}

function removeConsensioTable() {
    var existing = document.querySelector('.consensio-chart-table');
    if (existing) existing.remove();
    window._consensioSignals = null;
    window._consensioConfig = null;
    if (window._consensioCrosshairHandler && window._consensioCrosshairChart) {
        try { window._consensioCrosshairChart.unsubscribeAction('onCrosshairChange', window._consensioCrosshairHandler); } catch(e) {}
        window._consensioCrosshairHandler = null;
        window._consensioCrosshairChart = null;
        window._consensioCrosshairBound = false;
    }
}

// Export for the chart renderer (chart-indicators.js, share-chart-viewer.js)
window.drawIndicatorPack = drawIndicatorPack;
window.drawSingleIndicator = drawSingleIndicator;
window.drawTTMSqueezePane = drawTTMSqueezePane;
window.drawSqueezeSegments = drawSqueezeSegments;
window.indicatorPackConfig = indicatorPackConfig;
window.removeConsensioTable = removeConsensioTable;

