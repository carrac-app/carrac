// PURE MODULE — no DOM, no chart, no globals beyond window.Backtester
// Consumed by: backtest-ui.js
//
// Backtester.run(config, ohlcv) → { trades, equity, stats, error }
// Pure computation. Formulas parsed by FormulaParser, TA by TALib.

(function() {
    'use strict';

    var FP = window.FormulaParser;

    var SECONDS_PER_YEAR = 365 * 24 * 3600;

    // ── Main Entry Point ──────────────────────────────────────────────────

    function run(config, ohlcv) {
        // ── Validate & defaults ──────────────────────────────────────────
        if (!config.entry) return err('Entry formula is required');
        if (!config.exit) return err('Exit formula is required');

        var closes = ohlcv.close;
        var highs = ohlcv.high || closes;
        var lows = ohlcv.low || closes;
        var opens = ohlcv.open || closes;
        var n = closes.length;
        if (!n) return err('No price data');

        var direction = config.direction || 'long';
        var sizePct = (config.sizePercent != null ? config.sizePercent : 100) / 100;
        var commPct = (config.commissionPercent || 0) / 100;
        var slipBps = Math.max(0, +config.slippageBps || 0);
        var slip = slipBps / 10000;
        var capital = config.initialCapital || 10000;
        var startBar = Math.max(0, config.startBar || 0);
        var endBar = Math.min(n - 1, config.endBar != null ? config.endBar : n - 1);

        if (startBar > endBar) return err('startBar must be <= endBar');

        var stopType = config.stopType || 'close';
        var slPct = (+config.stopLossPercent > 0) ? (+config.stopLossPercent) / 100 : 0;
        var tpPct = (+config.takeProfitPercent > 0) ? (+config.takeProfitPercent) / 100 : 0;
        // Default fill is the NEXT bar's open, not the same bar's
        // close. Evaluating `close > ema20` and then filling at that same
        // close is a look-ahead bias — the signal can only be known after
        // the bar closes. `entryMode: 'close'` stays callable for signal
        // study runs but must be explicitly opted into by the caller.
        var entryMode = config.entryMode || 'next_open';
        // optional regime series (1 = favorable, 0 = unfavorable, NaN = unknown)
        var regimeSeries = Array.isArray(config.regimeSeries) && config.regimeSeries.length === n
            ? config.regimeSeries : null;

        // ── Evaluate formulas ────────────────────────────────────────────
        var entryResult = FP.evaluate(config.entry, ohlcv);
        if (entryResult.error) return err('Entry: ' + entryResult.error);

        var exitResult = FP.evaluate(config.exit, ohlcv);
        if (exitResult.error) return err('Exit: ' + exitResult.error);

        var entrySig = entryResult.values;
        var exitSig = exitResult.values;

        // ── Walk bars ────────────────────────────────────────────────────
        var dirMult = direction === 'short' ? -1 : 1;
        var equity = capital;
        var position = null;  // { entryBar, entryPrice, dir, units, posValue, stopPrice, tpPrice }
        var trades = [];
        var equityCurve = new Array(n);
        var funding = config.instrument === 'perp' && ohlcv.funding ? ohlcv.funding : null;

        // Pre-fill before startBar
        for (var i = 0; i < startBar; i++) equityCurve[i] = capital;

        for (var bar = startBar; bar <= endBar; bar++) {

            // ── Intrabar stop/TP check (fires before exit formula) ──────
            if (position && stopType === 'intrabar') {
                var stopHit = null;  // 'stop' | 'tp' | null
                var fillPrice = null;
                if (position.dir > 0) {  // long
                    if (position.stopPrice && lows[bar] <= position.stopPrice) {
                        stopHit = 'stop'; fillPrice = position.stopPrice;
                    } else if (position.tpPrice && highs[bar] >= position.tpPrice) {
                        stopHit = 'tp'; fillPrice = position.tpPrice;
                    }
                } else {  // short
                    if (position.stopPrice && highs[bar] >= position.stopPrice) {
                        stopHit = 'stop'; fillPrice = position.stopPrice;
                    } else if (position.tpPrice && lows[bar] <= position.tpPrice) {
                        stopHit = 'tp'; fillPrice = position.tpPrice;
                    }
                }
                if (stopHit) {
                    // Apply slippage against position direction (worst case)
                    var slippedExit = applySlippage(fillPrice, position.dir, 'exit', slip);
                    var res = closeTrade(position, slippedExit, bar, commPct, funding, stopHit + '_intrabar');
                    equity += res.pnl;
                    trades.push(res.trade);
                    position = null;
                }
            }

            // ── Exit formula check ──────────────────────────────────────
            if (position && isSig(exitSig[bar])) {
                var slippedExit = applySlippage(closes[bar], position.dir, 'exit', slip);
                var res = closeTrade(position, slippedExit, bar, commPct, funding, 'signal');
                equity += res.pnl;
                trades.push(res.trade);

                if (direction === 'both') {
                    var reverseBar = bar;
                    var reverseEntryPrice = applySlippage(closes[bar], -position.dir, 'entry', slip);
                    position = openPosition(reverseBar, reverseEntryPrice, -position.dir, equity, sizePct, slPct, tpPct);
                } else {
                    position = null;
                }
            }

            // ── Entry formula check ─────────────────────────────────────
            if (!position && isSig(entrySig[bar])) {
                if (entryMode === 'next_open' && bar < endBar) {
                    // Fill at next bar's open
                    var openFill = applySlippage(opens[bar + 1], dirMult, 'entry', slip);
                    position = openPosition(bar + 1, openFill, dirMult, equity, sizePct, slPct, tpPct);
                } else if (entryMode !== 'next_open') {
                    var closeFill = applySlippage(closes[bar], dirMult, 'entry', slip);
                    position = openPosition(bar, closeFill, dirMult, equity, sizePct, slPct, tpPct);
                }
                // If next_open and bar === endBar → signal skipped (no next bar)
            }

            // ── Equity curve (mark to market) ────────────────────────────
            if (position) {
                var unrealized = (closes[bar] - position.entryPrice) * position.dir * position.units;
                equityCurve[bar] = equity + unrealized;
            } else {
                equityCurve[bar] = equity;
            }
        }

        // ── Close open position at end of data ───────────────────────────
        if (position) {
            var slippedExit = applySlippage(closes[endBar], position.dir, 'exit', slip);
            var res = closeTrade(position, slippedExit, endBar, commPct, funding, 'end_of_data');
            equity += res.pnl;
            trades.push(res.trade);
            equityCurve[endBar] = equity;
        }

        // Post-fill after endBar
        for (var i = endBar + 1; i < n; i++) equityCurve[i] = equity;

        // ── Tag each trade with regime at entry bar ────────────────
        if (regimeSeries) {
            for (var ti = 0; ti < trades.length; ti++) {
                var eb = trades[ti].entryBar;
                var rv = regimeSeries[eb];
                trades[ti].regime = (rv === 1 || rv === 0) ? rv : null;
            }
        }

        // ── Compute stats ────────────────────────────────────────────────
        var barSeconds = +config.barSeconds || 86400;
        var stats = computeStats(trades, equityCurve, capital, startBar, endBar, barSeconds);

        var statsRegime = null;
        if (regimeSeries) {
            var fav = trades.filter(function(t) { return t.regime === 1; });
            var unf = trades.filter(function(t) { return t.regime === 0; });
            var unk = trades.filter(function(t) { return t.regime !== 1 && t.regime !== 0; });
            statsRegime = {
                favorable: computeTradeStats(fav, capital),
                unfavorable: computeTradeStats(unf, capital),
                unknown: computeTradeStats(unk, capital)
            };
        }

        return {
            error: null,
            trades: trades,
            equity: equityCurve,
            stats: stats,
            statsRegime: statsRegime
        };
    }

    // ── Helpers ──────────────────────────────────────────────────────────

    function err(msg) {
        return { error: msg, trades: [], equity: [], stats: null };
    }

    function isSig(val) {
        return val && !isNaN(val) && val !== 0;
    }

    // Slippage moves fill price against trader's direction.
    // entry long  → pay more (price up)     | entry short → sell lower (price down)
    // exit  long  → sell lower (price down) | exit  short → cover higher (price up)
    function applySlippage(price, dir, side, slip) {
        if (!slip) return price;
        var sign;
        if (side === 'entry') sign = dir > 0 ? 1 : -1;
        else sign = dir > 0 ? -1 : 1;
        return price * (1 + sign * slip);
    }

    function openPosition(bar, price, dir, equity, sizePct, slPct, tpPct) {
        var posValue = equity * sizePct;
        if (posValue <= 0 || price <= 0) return null;
        var stopPrice = slPct > 0 ? (dir > 0 ? price * (1 - slPct) : price * (1 + slPct)) : null;
        var tpPrice = tpPct > 0 ? (dir > 0 ? price * (1 + tpPct) : price * (1 - tpPct)) : null;
        return {
            entryBar: bar,
            entryPrice: price,
            dir: dir,
            units: posValue / price,
            posValue: posValue,
            stopPrice: stopPrice,
            tpPrice: tpPrice
        };
    }

    function closeTrade(pos, exitPrice, bar, commPct, funding, exitReason) {
        var grossPnl = (exitPrice - pos.entryPrice) * pos.dir * pos.units;
        var commission = 2 * commPct * pos.posValue;
        // Funding is signed by direction.
        // Longs pay when funding > 0 and RECEIVE when funding < 0.
        // Shorts are the opposite. Using `Math.abs(rate)` would make every
        // position pay every bar and misstate delta-neutral or counter-trend
        // perp strategies.
        var fundingCost = 0;
        if (funding) {
            for (var fb = pos.entryBar; fb <= bar; fb++) {
                var rate = funding[fb];
                if (rate && !isNaN(rate)) {
                    fundingCost += (pos.dir > 0 ? rate : -rate) * pos.posValue;
                }
            }
        }
        var netPnl = grossPnl - commission - fundingCost;
        return {
            pnl: netPnl,
            trade: {
                entryBar: pos.entryBar,
                exitBar: bar,
                entryPrice: pos.entryPrice,
                exitPrice: exitPrice,
                direction: pos.dir > 0 ? 'long' : 'short',
                pnl: netPnl,
                pnlPercent: pos.posValue > 0 ? (netPnl / pos.posValue) * 100 : 0,
                bars: bar - pos.entryBar,
                fundingCost: fundingCost,
                exitReason: exitReason || 'signal',
                regime: null
            }
        };
    }

    // Trade-level stats only — used for regime-split blocks where the
    // portfolio equity curve isn't meaningful (trades are a filtered subset).
    // Omits Sharpe/Sortino/Calmar/Ulcer/MaxDD (equity-curve based).
    function computeTradeStats(trades, initialCapital) {
        var nTrades = trades.length;
        var empty = {
            totalTrades: 0, winners: 0, losers: 0, winRate: 0, profitFactor: 0,
            avgWin: 0, avgLoss: 0, avgRR: 0, avgBarsInTrade: 0,
            maxConsecutiveWins: 0, maxConsecutiveLosses: 0,
            totalReturn: 0, totalReturnPercent: 0,
            sumPnlPercent: 0
        };
        if (nTrades === 0) return empty;

        var winners = 0, losers = 0;
        var winPnl = 0, lossPnl = 0;
        var conWins = 0, conLosses = 0;
        var maxConWins = 0, maxConLosses = 0;
        var totalBars = 0;
        var sumPnl = 0;
        var sumPnlPct = 0;
        for (var i = 0; i < nTrades; i++) {
            var t = trades[i];
            totalBars += t.bars;
            sumPnl += t.pnl;
            sumPnlPct += (t.pnlPercent || 0);
            if (t.pnl > 0) {
                winners++; winPnl += t.pnl; conWins++; conLosses = 0;
                if (conWins > maxConWins) maxConWins = conWins;
            } else {
                losers++; lossPnl += t.pnl; conLosses++; conWins = 0;
                if (conLosses > maxConLosses) maxConLosses = conLosses;
            }
        }
        var avgWin = winners > 0 ? winPnl / winners : 0;
        var avgLoss = losers > 0 ? lossPnl / losers : 0;
        return {
            totalTrades: nTrades,
            winners: winners,
            losers: losers,
            winRate: winners / nTrades,
            profitFactor: lossPnl < 0 ? winPnl / -lossPnl : (winPnl > 0 ? Infinity : 0),
            avgWin: avgWin,
            avgLoss: avgLoss,
            avgRR: lossPnl < 0 ? avgWin / -avgLoss : 0,
            avgBarsInTrade: totalBars / nTrades,
            maxConsecutiveWins: maxConWins,
            maxConsecutiveLosses: maxConLosses,
            totalReturn: sumPnl,
            totalReturnPercent: initialCapital > 0 ? (sumPnl / initialCapital) * 100 : 0,
            sumPnlPercent: sumPnlPct
        };
    }

    // ── Stats ────────────────────────────────────────────────────────────

    function computeStats(trades, equityCurve, initialCapital, startBar, endBar, barSeconds) {
        var periodsPerYear = barSeconds > 0 ? SECONDS_PER_YEAR / barSeconds : 365;
        var nTrades = trades.length;
        var emptyStats = {
            totalTrades: 0, winners: 0, losers: 0,
            winRate: 0, profitFactor: 0, sharpeRatio: 0, sortinoRatio: 0,
            calmarRatio: 0, ulcerIndex: 0,
            maxDrawdown: 0, maxDrawdownPercent: 0,
            avgWin: 0, avgLoss: 0, avgRR: 0,
            totalReturn: 0, totalReturnPercent: 0,
            avgBarsInTrade: 0,
            maxConsecutiveWins: 0, maxConsecutiveLosses: 0,
            periodsPerYear: periodsPerYear
        };
        if (nTrades === 0) return emptyStats;

        // ── Win/loss accounting ──────────────────────────────────────────
        var winners = 0, losers = 0;
        var winPnl = 0, lossPnl = 0;
        var conWins = 0, conLosses = 0;
        var maxConWins = 0, maxConLosses = 0;
        var totalBars = 0;

        for (var i = 0; i < nTrades; i++) {
            var t = trades[i];
            totalBars += t.bars;
            if (t.pnl > 0) {
                winners++;
                winPnl += t.pnl;
                conWins++;
                conLosses = 0;
                if (conWins > maxConWins) maxConWins = conWins;
            } else {
                losers++;
                lossPnl += t.pnl;
                conLosses++;
                conWins = 0;
                if (conLosses > maxConLosses) maxConLosses = conLosses;
            }
        }

        // ── Max drawdown + Ulcer ─────────────────────────────────────────
        var peak = initialCapital, maxDD = 0, maxDDPct = 0;
        var ulcerSumSq = 0, ulcerN = 0;
        for (var i = startBar; i <= endBar; i++) {
            if (equityCurve[i] > peak) peak = equityCurve[i];
            var dd = peak - equityCurve[i];
            if (dd > maxDD) maxDD = dd;
            var ddPct = peak > 0 ? dd / peak : 0;
            if (ddPct > maxDDPct) maxDDPct = ddPct;
            ulcerSumSq += (ddPct * 100) * (ddPct * 100);
            ulcerN++;
        }
        var ulcerIndex = ulcerN > 0 ? Math.sqrt(ulcerSumSq / ulcerN) : 0;

        // ── Sharpe + Sortino (TF-aware) ─────────────────────────────────
        var sharpe = 0, sortino = 0;
        var nBars = endBar - startBar;
        if (nBars > 1) {
            var returns = new Array(nBars);
            for (var i = 0; i < nBars; i++) {
                var prev = equityCurve[startBar + i];
                var curr = equityCurve[startBar + i + 1];
                returns[i] = prev > 0 ? (curr - prev) / prev : 0;
            }
            var sum = 0;
            for (var i = 0; i < nBars; i++) sum += returns[i];
            var mean = sum / nBars;
            // Sharpe uses sample std (÷(N-1)) not biased (÷N).
            // Sortino downside deviation = sqrt(mean of (max(0, target-r))²)
            //   over ALL bars (not just negative ones), with target = 0 (risk-free rate).
            //   This matches the standard Sortino / vectorbt definition.
            var target = 0;  // risk-free / target return per bar
            var varSum = 0, downVarSum = 0;
            for (var i = 0; i < nBars; i++) {
                var d = returns[i] - mean;
                varSum += d * d;
                var shortfall = target - returns[i];
                if (shortfall > 0) downVarSum += shortfall * shortfall;
            }
            var stdev = nBars > 1 ? Math.sqrt(varSum / (nBars - 1)) : 0;
            var downStdev = Math.sqrt(downVarSum / nBars);
            var annFactor = Math.sqrt(periodsPerYear);
            if (stdev > 0) sharpe = (mean / stdev) * annFactor;
            if (downStdev > 0) sortino = ((mean - target) / downStdev) * annFactor;
        }

        // ── Derived stats ────────────────────────────────────────────────
        var avgWin = winners > 0 ? winPnl / winners : 0;
        var avgLoss = losers > 0 ? lossPnl / losers : 0;
        var finalEquity = equityCurve[endBar];
        var totalReturn = finalEquity - initialCapital;
        var totalReturnPct = (totalReturn / initialCapital) * 100;

        // Calmar = annualized return / |max DD %|
        var calmar = 0;
        if (nBars > 0 && initialCapital > 0 && maxDDPct > 0) {
            var years = nBars / periodsPerYear;
            if (years > 0) {
                var finalRatio = finalEquity / initialCapital;
                if (finalRatio > 0) {
                    var annReturn = Math.pow(finalRatio, 1 / years) - 1;
                    calmar = annReturn / maxDDPct;
                }
            }
        }

        return {
            totalTrades: nTrades,
            winners: winners,
            losers: losers,
            winRate: winners / nTrades,
            profitFactor: lossPnl < 0 ? winPnl / -lossPnl : (winPnl > 0 ? Infinity : 0),
            sharpeRatio: sharpe,
            sortinoRatio: sortino,
            calmarRatio: calmar,
            ulcerIndex: ulcerIndex,
            maxDrawdown: maxDD,
            maxDrawdownPercent: maxDDPct * 100,
            avgWin: avgWin,
            avgLoss: avgLoss,
            avgRR: lossPnl < 0 ? avgWin / -avgLoss : 0,
            totalReturn: totalReturn,
            totalReturnPercent: totalReturnPct,
            avgBarsInTrade: totalBars / nTrades,
            maxConsecutiveWins: maxConWins,
            maxConsecutiveLosses: maxConLosses,
            periodsPerYear: periodsPerYear
        };
    }

    // ── Public API ──────────────────────────────────────────────────────

    window.Backtester = { run: run };
})();
