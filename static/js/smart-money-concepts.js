/**
 * Smart Money Concepts (SMC) Indicator for KlineChart
 * Ported from LuxAlgo's PineScript indicator
 *
 * Features:
 * - BOS/CHoCH Structure Detection (Internal and Swing)
 * - Order Blocks (Bullish/Bearish, Swing/Internal)
 * - Fair Value Gaps (FVG)
 * - Equal Highs/Lows
 * - Premium/Discount/Equilibrium Zones
 * - Strong/Weak High/Low
 * - Swing Labels (HH, HL, LH, LL)
 *
 * @version 2.0.0
 */

(function() {
    'use strict';

    // SMC Configuration
    window.smcConfig = {
        // Structure settings
        swingLength: 50,
        internalLength: 5,
        showSwingStructure: true,
        showInternalStructure: true,

        // Order Blocks
        showSwingOrderBlocks: true,
        showInternalOrderBlocks: true,
        orderBlockMitigation: 'close',
        maxOrderBlocks: 5,
        orderBlockExtend: 25,

        // Fair Value Gaps
        // default OFF. This draws its own FVG zones (no ATR
        // filter) which duplicate the standalone FVG indicator's zones (which
        // ARE ATR-filtered) whenever ORDER_BLOCKS is active. Verified via a
        // repo-wide grep that no settings UI currently reads/sets
        // smcConfig.showFVG, so flipping the default doesn't orphan a
        // user-facing toggle — the knob stays functional via
        // updateSMCConfig({showFVG: true}) if one is wired up later.
        showFVG: false,
        fvgMitigation: 'close',
        maxFVG: 5,
        fvgExtend: true,

        // Equal Highs/Lows
        showEqualHL: true,
        eqThreshold: 0.1,        // fraction of ATR(200) — matches LuxAlgo default
        eqLength: 3,             // pivot detection period (short = more pivots)

        // Premium/Discount Zones
        showPremiumDiscount: true,

        // Swing Labels (HH, HL, LH, LL)
        showSwingLabels: true,

        // Strong/Weak High/Low lines
        showStrongWeakHL: true,

        // Colors — matching LuxAlgo TradingView style
        bullColor: '#089981',           // Teal green (TradingView bull)
        bearColor: '#f23645',           // Red (TradingView bear)
        // Swing OB fills
        swingOBBullFill: 'rgba(8, 153, 129, 0.25)',
        swingOBBearFill: 'rgba(242, 54, 69, 0.25)',
        swingOBBullBorder: 'rgba(8, 153, 129, 0.6)',
        swingOBBearBorder: 'rgba(242, 54, 69, 0.6)',
        // Internal OB fills — dark gray
        intOBFill: 'rgba(120, 120, 120, 0.2)',
        intOBBorder: 'rgba(120, 120, 120, 0.5)',
        // FVG
        fvgBullColor: 'rgba(8, 153, 129, 0.15)',
        fvgBearColor: 'rgba(242, 54, 69, 0.15)',
        // EQH/EQL
        eqHighColor: 'rgba(242, 54, 69, 0.5)',
        eqLowColor: 'rgba(8, 153, 129, 0.5)',
        // Premium/Discount/Equilibrium
        premiumColor: '#f23645',
        discountColor: '#089981',
        eqLineColor: 'rgba(255, 255, 255, 0.35)',
        // Strong/Weak
        strongColor: '#f23645',
        weakColor: '#f23645'
    };

    // SMC State
    window.smcState = {
        swingHighs: [],
        swingLows: [],
        internalHighs: [],
        internalLows: [],
        orderBlocks: [],
        fvgZones: [],
        equalHighs: [],
        equalLows: [],
        trend: 1,
        internalTrend: 1,
        lastBOS: null,
        lastCHoCH: null
    };

    // dataset identity tracking so a symbol/timeframe switch
    // never leaves the PREVIOUS dataset's structures on screen. `smcState` is
    // module-global, so it must be reset on a dataset change — otherwise the
    // early-return path below (insufficient bars for swingLength*2) would hand
    // back whatever the last successful calc produced, i.e. the old symbol's
    // levels rendered on top of the new (short-history) chart.
    //
    // Identity key: prefer the chart's own current symbol+timeframe globals.
    // klineChartSymbol/klineChartTimeframe are plain top-level `var`s in
    // app-core.js (not inside an IIFE/module), so — same as this file already
    // does for `klineActiveIndicators` above — they are reachable here as
    // globals. That pair is stable across live-candle updates (a live tick
    // only mutates the forming last bar's OHLC, it doesn't change the symbol
    // or timeframe) and changes exactly when the user switches symbol/TF.
    // Fall back to a signature derived from the data itself only if those
    // globals are ever unreachable (e.g. some future embedded context).
    var _smcDatasetKey = null;

    function _smcComputeKey(data) {
        var symTf = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol)
            ? (klineChartSymbol + '|' + (typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : ''))
            : null;
        if (symTf) return symTf;
        if (data && data.length > 0) {
            var barSpan = data.length > 1 ? (data[1].timestamp - data[0].timestamp) : 0;
            return 'data:' + data[0].timestamp + '_' + barSpan + '_' + data.length;
        }
        return null;
    }

    // Fresh, fully-empty state — covers every field calculateSMC ever sets
    // (not just the ones in the initializer above), so a reset can't leave
    // stale swingStructures/premiumDiscount/strongLevel/etc. behind.
    function _smcEmptyState() {
        return {
            swingHighs: [], swingLows: [], internalHighs: [], internalLows: [],
            swingStructures: [], internalStructures: [],
            orderBlocks: [], fvgZones: [],
            equalHighs: [], equalLows: [],
            swingLabels: [],
            trend: 1, internalTrend: 1,
            lastBOS: null, lastCHoCH: null,
            premiumDiscount: null, strongLevel: null, weakLevel: null
        };
    }

    function _smcResetState() {
        Object.assign(smcState, _smcEmptyState());
    }

    /**
     * Find pivot highs in data
     */
    function findPivotHigh(data, index, leftBars, rightBars) {
        if (index < leftBars || index > data.length - rightBars - 1) return null;
        var pivotHigh = data[index].high;
        for (var i = 1; i <= leftBars; i++) {
            if (data[index - i].high >= pivotHigh) return null;
        }
        for (var j = 1; j <= rightBars; j++) {
            if (data[index + j].high > pivotHigh) return null;
        }
        return { index: index, price: pivotHigh, time: data[index].timestamp };
    }

    /**
     * Find pivot lows in data
     */
    function findPivotLow(data, index, leftBars, rightBars) {
        if (index < leftBars || index > data.length - rightBars - 1) return null;
        var pivotLow = data[index].low;
        for (var i = 1; i <= leftBars; i++) {
            if (data[index - i].low <= pivotLow) return null;
        }
        for (var j = 1; j <= rightBars; j++) {
            if (data[index + j].low < pivotLow) return null;
        }
        return { index: index, price: pivotLow, time: data[index].timestamp };
    }

    /**
     * Detect market structure (BOS/CHoCH)
     */
    function detectStructure(data, pivots, type, isSwing) {
        var structures = [];
        var trend = 1;
        var lastHigh = null;
        var lastLow = null;

        pivots.forEach(function(pivot) {
            if (pivot.type === 'high') {
                if (lastHigh && pivot.price > lastHigh.price && trend === -1) {
                    structures.push({ type: 'CHoCH', direction: 'bull', start: lastHigh, end: pivot, isSwing: isSwing });
                    trend = 1;
                } else if (lastHigh && pivot.price > lastHigh.price && trend === 1) {
                    structures.push({ type: 'BOS', direction: 'bull', start: lastHigh, end: pivot, isSwing: isSwing });
                }
                lastHigh = pivot;
            } else if (pivot.type === 'low') {
                if (lastLow && pivot.price < lastLow.price && trend === 1) {
                    structures.push({ type: 'CHoCH', direction: 'bear', start: lastLow, end: pivot, isSwing: isSwing });
                    trend = -1;
                } else if (lastLow && pivot.price < lastLow.price && trend === -1) {
                    structures.push({ type: 'BOS', direction: 'bear', start: lastLow, end: pivot, isSwing: isSwing });
                }
                lastLow = pivot;
            }
        });

        return { structures: structures, trend: trend };
    }

    /**
     * Detect Order Blocks
     */
    function detectOrderBlocks(data, structures) {
        var orderBlocks = [];

        structures.forEach(function(structure) {
            if (structure.type === 'BOS' || structure.type === 'CHoCH') {
                var startIdx = structure.start.index;
                var endIdx = structure.end.index;

                for (var i = endIdx - 1; i >= Math.max(0, startIdx - 10); i--) {
                    var candle = data[i];
                    var isBullCandle = candle.close > candle.open;
                    var isBearCandle = candle.close < candle.open;

                    if (structure.direction === 'bull' && isBearCandle) {
                        orderBlocks.push({
                            type: 'bull', top: candle.open, bottom: candle.close,
                            startTime: candle.timestamp, startIndex: i,
                            isSwing: structure.isSwing, mitigated: false
                        });
                        break;
                    } else if (structure.direction === 'bear' && isBullCandle) {
                        orderBlocks.push({
                            type: 'bear', top: candle.close, bottom: candle.open,
                            startTime: candle.timestamp, startIndex: i,
                            isSwing: structure.isSwing, mitigated: false
                        });
                        break;
                    }
                }
            }
        });

        // Check for mitigation
        if (data.length > 0) {
            orderBlocks.forEach(function(ob) {
                for (var i = ob.startIndex + 1; i < data.length; i++) {
                    var candle = data[i];
                    var checkPrice = smcConfig.orderBlockMitigation === 'close' ? candle.close :
                                     (ob.type === 'bull' ? candle.low : candle.high);
                    if (ob.type === 'bull' && checkPrice < ob.bottom) {
                        ob.mitigated = true; ob.mitigatedIndex = i; break;
                    } else if (ob.type === 'bear' && checkPrice > ob.top) {
                        ob.mitigated = true; ob.mitigatedIndex = i; break;
                    }
                }
            });
        }

        // cap PER TYPE, not on the concatenated array. The
        // caller builds `structures` as [all swing structures] followed by
        // [all internal structures] (see allStructures below), and internal
        // pivots (internalLength=5) vastly outnumber swing pivots
        // (swingLength=50) — so a single `.slice(-maxOrderBlocks)` on the
        // combined, unmitigated list almost always lands entirely inside the
        // internal tail, starving swing OBs out of the render. Give each type
        // its own budget using the same maxOrderBlocks config instead of
        // merging chronologically with a guaranteed quota (smaller diff, same
        // effect: up to 5 swing + up to 5 internal).
        var unmitigated = orderBlocks.filter(function(ob) { return !ob.mitigated; });
        var swingOBs = unmitigated.filter(function(ob) { return ob.isSwing; }).slice(-smcConfig.maxOrderBlocks);
        var internalOBs = unmitigated.filter(function(ob) { return !ob.isSwing; }).slice(-smcConfig.maxOrderBlocks);
        return swingOBs.concat(internalOBs);
    }

    /**
     * Detect Fair Value Gaps
     */
    function detectFVG(data) {
        var fvgZones = [];

        for (var i = 2; i < data.length; i++) {
            var prev = data[i - 2];
            var next = data[i];

            if (prev.high < next.low) {
                fvgZones.push({
                    type: 'bull', top: next.low, bottom: prev.high,
                    startTime: data[i - 1].timestamp, startIndex: i - 1, mitigated: false
                });
            }
            if (prev.low > next.high) {
                fvgZones.push({
                    type: 'bear', top: prev.low, bottom: next.high,
                    startTime: data[i - 1].timestamp, startIndex: i - 1, mitigated: false
                });
            }
        }

        fvgZones.forEach(function(fvg) {
            for (var i = fvg.startIndex + 1; i < data.length; i++) {
                var candle = data[i];
                var checkPrice = smcConfig.fvgMitigation === 'close' ? candle.close :
                                 (fvg.type === 'bull' ? candle.low : candle.high);
                if (fvg.type === 'bull' && checkPrice < fvg.bottom) { fvg.mitigated = true; break; }
                else if (fvg.type === 'bear' && checkPrice > fvg.top) { fvg.mitigated = true; break; }
            }
        });

        return fvgZones.filter(function(fvg) { return !fvg.mitigated; })
                       .slice(-smcConfig.maxFVG);
    }

    /**
     * Detect Equal Highs/Lows using dedicated short-period pivots + ATR threshold
     * Matches LuxAlgo: separate pivot scan (eqLength=3), threshold = eqThreshold * ATR(200)
     */
    function detectEqualHL(data) {
        var equalHighs = [];
        var equalLows = [];
        var eqLen = smcConfig.eqLength || 3;
        var threshold = smcConfig.eqThreshold || 0.1;

        // Compute ATR(200) on ORIGINAL prices (not log-transformed)
        // Log scale stores originals in _origHigh/_origLow/_origClose
        var atrPeriod = Math.min(200, data.length - 1);
        var atr200 = 0;
        if (atrPeriod > 0) {
            var trSum = 0;
            for (var a = data.length - atrPeriod; a < data.length; a++) {
                var hi = data[a]._origHigh || data[a].high;
                var lo = data[a]._origLow || data[a].low;
                var prevC = a > 0 ? (data[a - 1]._origClose || data[a - 1].close) : lo;
                trSum += Math.max(hi - lo, Math.abs(hi - prevC), Math.abs(lo - prevC));
            }
            atr200 = trSum / atrPeriod;
        }
        var absThreshold = threshold * atr200;

        // Detect pivots with short period (eqLength) — many more than swing pivots
        // Compare in original price space (handles log scale)
        var lastHigh = null;
        var lastLow = null;

        for (var i = eqLen; i < data.length - eqLen; i++) {
            var ph = findPivotHigh(data, i, eqLen, eqLen);
            var pl = findPivotLow(data, i, eqLen, eqLen);

            if (ph) {
                // Use original price for comparison (log scale stores originals)
                var phOrigPrice = data[ph.index]._origHigh || ph.price;
                if (lastHigh) {
                    var lhOrigPrice = data[lastHigh.index]._origHigh || lastHigh.price;
                    if (Math.abs(phOrigPrice - lhOrigPrice) < absThreshold) {
                        equalHighs.push({
                            price: (ph.price + lastHigh.price) / 2,
                            time1: lastHigh.time, time2: ph.time,
                            index1: lastHigh.index, index2: ph.index
                        });
                    }
                }
                lastHigh = ph;
            }
            if (pl) {
                var plOrigPrice = data[pl.index]._origLow || pl.price;
                if (lastLow) {
                    var llOrigPrice = data[lastLow.index]._origLow || lastLow.price;
                    if (Math.abs(plOrigPrice - llOrigPrice) < absThreshold) {
                        equalLows.push({
                            price: (pl.price + lastLow.price) / 2,
                            time1: lastLow.time, time2: pl.time,
                            index1: lastLow.index, index2: pl.index
                        });
                    }
                }
                lastLow = pl;
            }
        }
        return { equalHighs: equalHighs, equalLows: equalLows };
    }

    /**
     * Label swing points as HH, HL, LH, LL
     */
    function labelSwingPoints(swingPivots) {
        var labels = [];
        var lastHigh = null;
        var lastLow = null;

        swingPivots.forEach(function(pivot) {
            if (pivot.type === 'high') {
                if (lastHigh) {
                    labels.push({ index: pivot.index, price: pivot.price, time: pivot.time,
                        label: pivot.price > lastHigh.price ? 'HH' : 'LH', type: 'high' });
                }
                lastHigh = pivot;
            } else if (pivot.type === 'low') {
                if (lastLow) {
                    labels.push({ index: pivot.index, price: pivot.price, time: pivot.time,
                        label: pivot.price > lastLow.price ? 'HL' : 'LL', type: 'low' });
                }
                lastLow = pivot;
            }
        });
        return labels;
    }

    /**
     * Main SMC calculation function
     */
    window.calculateSMC = function(data) {
        var _key = _smcComputeKey(data);
        if (_key !== _smcDatasetKey) {
            // Symbol/timeframe changed since the last calc — wipe everything
            // before deciding anything else about this call.
            _smcResetState();
            _smcDatasetKey = _key;
        }

        if (!data || data.length < smcConfig.swingLength * 2) {
            // Not enough bars to calculate structure for THIS dataset — never
            // hand back a previous (possibly still-populated) state. Render
            // nothing rather than the wrong symbol's levels.
            _smcResetState();
            return smcState;
        }

        var swingPivots = [];
        var internalPivots = [];

        for (var i = smcConfig.swingLength; i < data.length - smcConfig.swingLength; i++) {
            var sh = findPivotHigh(data, i, smcConfig.swingLength, smcConfig.swingLength);
            var sl = findPivotLow(data, i, smcConfig.swingLength, smcConfig.swingLength);
            if (sh) swingPivots.push({ ...sh, type: 'high' });
            if (sl) swingPivots.push({ ...sl, type: 'low' });
        }

        for (var j = smcConfig.internalLength; j < data.length - smcConfig.internalLength; j++) {
            var ih = findPivotHigh(data, j, smcConfig.internalLength, smcConfig.internalLength);
            var il = findPivotLow(data, j, smcConfig.internalLength, smcConfig.internalLength);
            if (ih) internalPivots.push({ ...ih, type: 'high' });
            if (il) internalPivots.push({ ...il, type: 'low' });
        }

        swingPivots.sort(function(a, b) { return a.index - b.index; });
        internalPivots.sort(function(a, b) { return a.index - b.index; });

        var swingStructure = detectStructure(data, swingPivots, 'swing', true);
        var internalStructure = detectStructure(data, internalPivots, 'internal', false);

        var allStructures = swingStructure.structures.concat(internalStructure.structures);
        var orderBlocks = detectOrderBlocks(data, allStructures);
        var fvgZones = detectFVG(data);
        var equalHL = detectEqualHL(data);

        smcState.swingHighs = swingPivots.filter(function(p) { return p.type === 'high'; });
        smcState.swingLows = swingPivots.filter(function(p) { return p.type === 'low'; });
        smcState.internalHighs = internalPivots.filter(function(p) { return p.type === 'high'; });
        smcState.internalLows = internalPivots.filter(function(p) { return p.type === 'low'; });
        smcState.swingStructures = swingStructure.structures;
        smcState.internalStructures = internalStructure.structures;
        smcState.orderBlocks = orderBlocks;
        smcState.fvgZones = fvgZones;
        smcState.equalHighs = equalHL.equalHighs;
        smcState.equalLows = equalHL.equalLows;
        smcState.trend = swingStructure.trend;
        smcState.internalTrend = internalStructure.trend;
        smcState.swingLabels = labelSwingPoints(swingPivots);

        // Premium/Discount/Equilibrium — using trailing extremes (matching LuxAlgo)
        // LuxAlgo tracks trailing.top/bottom: initialized at swing pivot, then
        // updated every bar with math.max(high)/math.min(low)
        var lastSH = smcState.swingHighs.length > 0 ? smcState.swingHighs[smcState.swingHighs.length - 1] : null;
        var lastSL = smcState.swingLows.length > 0 ? smcState.swingLows[smcState.swingLows.length - 1] : null;
        if (lastSH && lastSL) {
            // Find which swing was detected more recently — that's the anchor
            var anchorIndex = Math.max(lastSH.index, lastSL.index);
            var anchorTime = Math.max(lastSH.time, lastSL.time);

            // Initialize trailing from swing pivot prices
            var trailingTop = lastSH.price;
            var trailingBottom = lastSL.price;
            var trailingTopTime = lastSH.time;
            var trailingBottomTime = lastSL.time;

            // Scan from anchor to end of data — track actual max high / min low
            var scanStart = Math.min(lastSH.index, lastSL.index);
            for (var ti = scanStart; ti < data.length; ti++) {
                if (data[ti].high > trailingTop) {
                    trailingTop = data[ti].high;
                    trailingTopTime = data[ti].timestamp;
                }
                if (data[ti].low < trailingBottom) {
                    trailingBottom = data[ti].low;
                    trailingBottomTime = data[ti].timestamp;
                }
            }

            smcState.premiumDiscount = {
                high: trailingTop, low: trailingBottom,
                eq: (trailingTop + trailingBottom) / 2,
                startTime: anchorTime,
                topTime: trailingTopTime,
                bottomTime: trailingBottomTime
            };
            // Strong/Weak
            if (smcState.trend === 1) {
                smcState.strongLevel = { price: lastSL.price, time: lastSL.time, type: 'low', label: 'Strong Low' };
                smcState.weakLevel = { price: lastSH.price, time: lastSH.time, type: 'high', label: 'Weak High' };
            } else {
                smcState.strongLevel = { price: lastSH.price, time: lastSH.time, type: 'high', label: 'Strong High' };
                smcState.weakLevel = { price: lastSL.price, time: lastSL.time, type: 'low', label: 'Weak Low' };
            }
        } else {
            smcState.premiumDiscount = null;
            smcState.strongLevel = null;
            smcState.weakLevel = null;
        }

        return smcState;
    };

    /**
     * Draw SMC overlays on KlineChart
     */
    // Check which SMC sub-indicators are active
    function _smcActive(key) {
        return typeof klineActiveIndicators !== 'undefined' && klineActiveIndicators[key];
    }
    function _anySMCActive() {
        return _smcActive('MKT_STRUCTURE') || _smcActive('ORDER_BLOCKS') ||
               _smcActive('EQ_HL') || _smcActive('VALUE_ZONES');
    }

    window.drawSMCOverlays = function(chart, data) {
        if (!chart || !data || data.length === 0) return;
        if (!_anySMCActive()) return;

        var smc = calculateSMC(data);
        var _pd = smc.premiumDiscount;

        try { chart.removeOverlay({ groupId: 'smc_overlays' }); } catch (e) {}

        var overlays = [];
        var lastDataTime = data[data.length - 1].timestamp;
        var barSpan = data.length > 1 ? data[1].timestamp - data[0].timestamp : 86400000;
        var extendTime = lastDataTime + (smcConfig.orderBlockExtend * barSpan);

        // ── Order Blocks ── (gated by ORDER_BLOCKS indicator)
        // Swing OBs: colored fills. Internal OBs: gray fills.
        if (_smcActive('ORDER_BLOCKS') && (smcConfig.showSwingOrderBlocks || smcConfig.showInternalOrderBlocks)) {
            smc.orderBlocks.forEach(function(ob) {
                if (ob.isSwing && !smcConfig.showSwingOrderBlocks) return;
                if (!ob.isSwing && !smcConfig.showInternalOrderBlocks) return;

                var fillColor, borderColor;
                if (ob.isSwing) {
                    fillColor = ob.type === 'bull' ? smcConfig.swingOBBullFill : smcConfig.swingOBBearFill;
                    borderColor = ob.type === 'bull' ? smcConfig.swingOBBullBorder : smcConfig.swingOBBearBorder;
                } else {
                    fillColor = smcConfig.intOBFill;
                    borderColor = smcConfig.intOBBorder;
                }

                overlays.push({
                    name: 'rect', groupId: 'smc_overlays', lock: true,
                    points: [
                        { timestamp: ob.startTime, value: ob.top },
                        { timestamp: extendTime, value: ob.bottom }
                    ],
                    styles: { polygon: { color: fillColor, borderColor: borderColor, borderSize: 1 } }
                });
            });
        }

        // ── Fair Value Gaps ── (part of ORDER_BLOCKS sub-indicator)
        if (_smcActive('ORDER_BLOCKS') && smcConfig.showFVG) {
            smc.fvgZones.forEach(function(fvg) {
                var endTime = smcConfig.fvgExtend ? extendTime : data[Math.min(fvg.startIndex + 20, data.length - 1)].timestamp;
                overlays.push({
                    name: 'rect', groupId: 'smc_overlays', lock: true,
                    points: [
                        { timestamp: fvg.startTime, value: fvg.top },
                        { timestamp: endTime, value: fvg.bottom }
                    ],
                    styles: { polygon: {
                        color: fvg.type === 'bull' ? smcConfig.fvgBullColor : smcConfig.fvgBearColor,
                        borderColor: 'transparent', borderSize: 0
                    } }
                });
            });
        }

        // ── Equal Highs/Lows ── (gated by LIQ_LEVELS indicator)
        // Dotted line connecting the two equal pivots + EQH/EQL label at midpoint
        if (_smcActive('EQ_HL') && smcConfig.showEqualHL) {
            smc.equalHighs.forEach(function(eq) {
                // Dotted line between the two equal highs
                overlays.push({
                    name: 'segment', groupId: 'smc_overlays', lock: true,
                    points: [{ timestamp: eq.time1, value: eq.price }, { timestamp: eq.time2, value: eq.price }],
                    styles: { line: { color: smcConfig.eqHighColor, size: 1, style: 'dashed' } }
                });
                // EQH label at midpoint
                var midTime = Math.round((eq.time1 + eq.time2) / 2);
                try {
                    chart.createOverlay({
                        name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                        points: [{ timestamp: midTime, value: eq.price }],
                        extendData: { text: 'EQH', color: smcConfig.eqHighColor, size: 8 }
                    });
                } catch (e) {}
            });
            smc.equalLows.forEach(function(eq) {
                // Dotted line between the two equal lows
                overlays.push({
                    name: 'segment', groupId: 'smc_overlays', lock: true,
                    points: [{ timestamp: eq.time1, value: eq.price }, { timestamp: eq.time2, value: eq.price }],
                    styles: { line: { color: smcConfig.eqLowColor, size: 1, style: 'dashed' } }
                });
                // EQL label at midpoint
                var midTime = Math.round((eq.time1 + eq.time2) / 2);
                try {
                    chart.createOverlay({
                        name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                        points: [{ timestamp: midTime, value: eq.price }],
                        extendData: { text: 'EQL', color: smcConfig.eqLowColor, size: 8 }
                    });
                } catch (e) {}
            });
        }

        // ── Premium / Discount / Equilibrium ── (gated by VALUE_ZONES indicator)
        // Matching LuxAlgo: 3 thin zone bands (top 5%, middle 5%, bottom 5%) + labels at right edge
        if (_smcActive('VALUE_ZONES') && smcConfig.showPremiumDiscount && smc.premiumDiscount) {
            var pd = smc.premiumDiscount;
            var range = pd.high - pd.low;

            // Premium zone: top 5% of range (LuxAlgo: trailing.top → 0.95*top + 0.05*bottom)
            var premTop = pd.high;
            var premBot = 0.95 * pd.high + 0.05 * pd.low;
            overlays.push({
                name: 'rect', groupId: 'smc_overlays', lock: true,
                points: [
                    { timestamp: pd.startTime, value: premTop },
                    { timestamp: extendTime, value: premBot }
                ],
                styles: { polygon: { color: 'rgba(242, 54, 69, 0.15)', borderColor: 'transparent', borderSize: 0 } }
            });

            // Equilibrium zone: middle 5% of range (LuxAlgo: 0.525*top+0.475*bot → 0.525*bot+0.475*top)
            var eqTop = 0.525 * pd.high + 0.475 * pd.low;
            var eqBot = 0.475 * pd.high + 0.525 * pd.low;
            overlays.push({
                name: 'rect', groupId: 'smc_overlays', lock: true,
                points: [
                    { timestamp: pd.startTime, value: eqTop },
                    { timestamp: extendTime, value: eqBot }
                ],
                styles: { polygon: { color: 'rgba(135, 139, 148, 0.15)', borderColor: 'transparent', borderSize: 0 } }
            });

            // Discount zone: bottom 5% of range (LuxAlgo: 0.95*bottom + 0.05*top → trailing.bottom)
            var discTop = 0.05 * pd.high + 0.95 * pd.low;
            var discBot = pd.low;
            overlays.push({
                name: 'rect', groupId: 'smc_overlays', lock: true,
                points: [
                    { timestamp: pd.startTime, value: discTop },
                    { timestamp: extendTime, value: discBot }
                ],
                styles: { polygon: { color: 'rgba(8, 153, 129, 0.15)', borderColor: 'transparent', borderSize: 0 } }
            });

            // Labels inside each zone, vertically centered, right-aligned at zone edge
            var labelTime = extendTime;
            var zoneLabel = { baseline: 'middle', size: 10 };
            try {
                chart.createOverlay({
                    name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                    points: [{ timestamp: labelTime, value: (premTop + premBot) / 2 }],
                    extendData: { text: 'Premium', color: smcConfig.premiumColor, baseline: 'middle', align: 'right', size: 10 }
                });
            } catch (e) {}
            try {
                chart.createOverlay({
                    name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                    points: [{ timestamp: labelTime, value: (eqTop + eqBot) / 2 }],
                    extendData: { text: 'Equilibrium', color: smcConfig.eqLineColor, baseline: 'middle', align: 'right', size: 9 }
                });
            } catch (e) {}
            try {
                chart.createOverlay({
                    name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                    points: [{ timestamp: labelTime, value: (discTop + discBot) / 2 }],
                    extendData: { text: 'Discount', color: smcConfig.discountColor, baseline: 'middle', align: 'right', size: 10 }
                });
            } catch (e) {}
        }

        // ── Strong / Weak High/Low ── (gated by MKT_STRUCTURE indicator)
        if (_smcActive('MKT_STRUCTURE') && smcConfig.showStrongWeakHL) {
            if (smc.strongLevel) {
                // Solid line extending to right edge
                overlays.push({
                    name: 'segment', groupId: 'smc_overlays', lock: true,
                    points: [
                        { timestamp: smc.strongLevel.time, value: smc.strongLevel.price },
                        { timestamp: extendTime, value: smc.strongLevel.price }
                    ],
                    styles: { line: { color: smcConfig.strongColor, size: 1, style: 'solid' } }
                });
                // Label at right edge
                try {
                    chart.createOverlay({
                        name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                        points: [{ timestamp: extendTime, value: smc.strongLevel.price }],
                        extendData: { text: smc.strongLevel.label, color: smcConfig.strongColor, size: 9 }
                    });
                } catch (e) {}
            }
            if (smc.weakLevel) {
                overlays.push({
                    name: 'segment', groupId: 'smc_overlays', lock: true,
                    points: [
                        { timestamp: smc.weakLevel.time, value: smc.weakLevel.price },
                        { timestamp: extendTime, value: smc.weakLevel.price }
                    ],
                    styles: { line: { color: smcConfig.weakColor, size: 1, style: 'solid' } }
                });
                try {
                    chart.createOverlay({
                        name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                        points: [{ timestamp: extendTime, value: smc.weakLevel.price }],
                        extendData: { text: smc.weakLevel.label, color: smcConfig.weakColor, size: 9 }
                    });
                } catch (e) {}
            }
        }

        // ── Batch-add overlays ──
        overlays.forEach(function(overlay) {
            try { chart.createOverlay(overlay); } catch (e) {
                console.error('[SMC] Overlay error:', overlay.name, e);
            }
        });

        // ── Structure labels (BOS/CHoCH) ── (gated by MKT_STRUCTURE indicator)
        if (_smcActive('MKT_STRUCTURE')) {
            drawStructureLabels(chart, data, smc);

            // ── Swing labels (HH, HL, LH, LL) ──
            drawSwingLabels(chart, data, smc);
        }
    };

    /**
     * Draw BOS/CHoCH labels
     * BOS = solid line, CHoCH = dashed line (matching LuxAlgo)
     */
    function drawStructureLabels(chart, data, smc) {
        // Swing structure — thicker, larger labels
        if (smcConfig.showSwingStructure && smc.swingStructures) {
            smc.swingStructures.slice(-10).forEach(function(s) {
                var color = s.direction === 'bull' ? smcConfig.bullColor : smcConfig.bearColor;
                var levelPrice = s.start.price;
                var lineStyle = s.type === 'BOS' ? 'solid' : 'dashed';

                try {
                    chart.createOverlay({
                        name: 'segment', groupId: 'smc_overlays', lock: true,
                        points: [
                            { timestamp: s.start.time, value: levelPrice },
                            { timestamp: s.end.time, value: levelPrice }
                        ],
                        styles: { line: { color: color, size: 1, style: lineStyle } }
                    });
                } catch (e) {}

                try {
                    var midTime = Math.round((s.start.time + s.end.time) / 2);
                    var labelY = s.direction === 'bull' ? levelPrice * 1.003 : levelPrice * 0.997;
                    chart.createOverlay({
                        name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                        points: [{ timestamp: midTime, value: labelY }],
                        extendData: { text: s.type, color: color, size: 10 }
                    });
                } catch (e) {}
            });
        }

        // Internal structure — thinner, smaller labels, always dashed
        if (smcConfig.showInternalStructure && smc.internalStructures) {
            smc.internalStructures.slice(-15).forEach(function(s) {
                var color = s.direction === 'bull' ? smcConfig.bullColor : smcConfig.bearColor;
                var levelPrice = s.start.price;
                var lineStyle = s.type === 'BOS' ? 'solid' : 'dashed';

                try {
                    chart.createOverlay({
                        name: 'segment', groupId: 'smc_overlays', lock: true,
                        points: [
                            { timestamp: s.start.time, value: levelPrice },
                            { timestamp: s.end.time, value: levelPrice }
                        ],
                        styles: { line: { color: color, size: 0.5, style: lineStyle } }
                    });
                } catch (e) {}

                try {
                    var midTime = Math.round((s.start.time + s.end.time) / 2);
                    var labelY = s.direction === 'bull' ? levelPrice * 1.002 : levelPrice * 0.998;
                    chart.createOverlay({
                        name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                        points: [{ timestamp: midTime, value: labelY }],
                        extendData: { text: s.type.toLowerCase(), color: color, size: 8 }
                    });
                } catch (e) {}
            });
        }
    }

    /**
     * Draw Swing Labels (HH, HL, LH, LL) at swing pivot points
     */
    function drawSwingLabels(chart, data, smc) {
        if (!smcConfig.showSwingLabels || !smc.swingLabels) return;

        smc.swingLabels.slice(-12).forEach(function(sl) {
            var color = (sl.label === 'HH' || sl.label === 'HL') ? smcConfig.bullColor : smcConfig.bearColor;
            var yOffset = sl.type === 'high' ? sl.price * 1.005 : sl.price * 0.995;

            try {
                chart.createOverlay({
                    name: 'smcLabel', groupId: 'smc_overlays', lock: true,
                    points: [{ timestamp: sl.time, value: yOffset }],
                    extendData: { text: sl.label, color: color, size: 9 }
                });
            } catch (e) {}
        });
    }

    /**
     * Toggle SMC sub-indicators — redraws all active SMC overlays or removes them
     */
    window.toggleSMC = function(chart, data) {
        try { chart.removeOverlay({ groupId: 'smc_overlays' }); } catch (e) {}
        if (_anySMCActive()) {
            drawSMCOverlays(chart, data);
        }
    };

    /**
     * Update SMC settings
     */
    window.updateSMCConfig = function(newConfig) {
        Object.assign(smcConfig, newConfig);
    };

})();
