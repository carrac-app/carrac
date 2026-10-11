// PURE MODULE — no DOM, no chart, no globals beyond window.TALib
// Consumed by: custom-indicators.js (chart indicators), backtester.js (strategy simulation),
// formula-parser.js (DSL evaluation)
//
// Every function: number[] in → number[] out (same length, NaN for insufficient lookback)
// Period/length args are integers, data args are arrays.
// EMA uses SMA seed (matches TradingView). ATR/RSI use Wilder's smoothing.

(function() {
    'use strict';

    // ── Internal Helpers ────────────────────────────────────────────────

    function _nanArray(len) {
        var a = new Array(len);
        for (var i = 0; i < len; i++) a[i] = NaN;
        return a;
    }

    // Find first index where src has `period` consecutive non-NaN values.
    // Returns the end index of that window, or -1.
    function _findSeed(src, period) {
        var consecutive = 0;
        for (var i = 0; i < src.length; i++) {
            if (isNaN(src[i])) { consecutive = 0; continue; }
            if (++consecutive === period) return i;
        }
        return -1;
    }

    // Wilder's smoothing (RMA): first value = SMA of first `period` valid values, then exponential
    function _wilders(src, period) {
        var n = src.length, out = _nanArray(n);
        var seedEnd = _findSeed(src, period);
        if (seedEnd < 0) return out;
        var sum = 0;
        for (var i = seedEnd - period + 1; i <= seedEnd; i++) sum += src[i];
        out[seedEnd] = sum / period;
        for (var i = seedEnd + 1; i < n; i++) {
            if (isNaN(src[i]) || isNaN(out[i - 1])) continue;
            out[i] = (out[i - 1] * (period - 1) + src[i]) / period;
        }
        return out;
    }

    // ── Price Shorthands ────────────────────────────────────────────────

    function ohlc4(o, h, l, c) {
        var n = o.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = (o[i] + h[i] + l[i] + c[i]) / 4;
        return out;
    }

    function hlc3(h, l, c) {
        var n = h.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = (h[i] + l[i] + c[i]) / 3;
        return out;
    }

    function hl2(h, l) {
        var n = h.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = (h[i] + l[i]) / 2;
        return out;
    }

    // ── Moving Averages ─────────────────────────────────────────────────

    function sma(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var s = 0, nanCount = 0;
        for (var i = 0; i < len; i++) {
            if (isNaN(src[i])) nanCount++; else s += src[i];
        }
        if (nanCount === 0) out[len - 1] = s / len;
        for (var i = len; i < n; i++) {
            var leaving = src[i - len], entering = src[i];
            if (isNaN(leaving)) nanCount--; else s -= leaving;
            if (isNaN(entering)) nanCount++; else s += entering;
            if (nanCount === 0) out[i] = s / len;
        }
        return out;
    }

    function ema(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var k = 2 / (len + 1);
        var seedEnd = _findSeed(src, len);
        if (seedEnd < 0) return out;
        var s = 0;
        for (var i = seedEnd - len + 1; i <= seedEnd; i++) s += src[i];
        out[seedEnd] = s / len;
        for (var i = seedEnd + 1; i < n; i++) {
            if (isNaN(src[i]) || isNaN(out[i - 1])) continue;
            out[i] = src[i] * k + out[i - 1] * (1 - k);
        }
        return out;
    }

    function wma(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var denom = len * (len + 1) / 2;
        for (var i = len - 1; i < n; i++) {
            var s = 0, bad = false;
            for (var j = 0; j < len; j++) {
                var v = src[i - len + 1 + j];
                if (isNaN(v)) { bad = true; break; }
                s += v * (j + 1);
            }
            if (!bad) out[i] = s / denom;
        }
        return out;
    }

    function hma(src, len) {
        var n = src.length;
        if (n < len || len < 1) return _nanArray(n);
        var halfLen = Math.max(1, Math.floor(len / 2));
        var sqrtLen = Math.max(1, Math.round(Math.sqrt(len)));
        var wmaHalf = wma(src, halfLen);
        var wmaFull = wma(src, len);
        var diff = _nanArray(n);
        for (var i = 0; i < n; i++) {
            if (!isNaN(wmaHalf[i]) && !isNaN(wmaFull[i]))
                diff[i] = 2 * wmaHalf[i] - wmaFull[i];
        }
        return wma(diff, sqrtLen);
    }

    function alma(src, len, off, sig) {
        if (off === undefined) off = 0.85;
        if (sig === undefined) sig = 6;
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var m = Math.floor(off * (len - 1));
        var s = len / sig;
        var weights = new Array(len), wSum = 0;
        for (var i = 0; i < len; i++) {
            weights[i] = Math.exp(-((i - m) * (i - m)) / (2 * s * s));
            wSum += weights[i];
        }
        for (var i = len - 1; i < n; i++) {
            var val = 0, bad = false;
            for (var j = 0; j < len; j++) {
                var v = src[i - len + 1 + j];
                if (isNaN(v)) { bad = true; break; }
                val += v * weights[j];
            }
            if (!bad) out[i] = val / wSum;
        }
        return out;
    }

    function kama(src, len, fast, slow) {
        if (fast === undefined) fast = 2;
        if (slow === undefined) slow = 30;
        var n = src.length, out = _nanArray(n);
        if (n < len + 1 || len < 1) return out;
        var fastSC = 2 / (fast + 1);
        var slowSC = 2 / (slow + 1);
        var seedEnd = _findSeed(src, len + 1);
        if (seedEnd < 0) return out;
        out[seedEnd] = src[seedEnd];
        for (var i = seedEnd + 1; i < n; i++) {
            if (isNaN(src[i]) || isNaN(out[i - 1])) continue;
            var direction = Math.abs(src[i] - src[i - len]);
            var volatility = 0;
            for (var j = i - len + 1; j <= i; j++) {
                volatility += Math.abs(src[j] - src[j - 1]);
            }
            var er = volatility === 0 ? 0 : direction / volatility;
            var sc = er * (fastSC - slowSC) + slowSC;
            sc = sc * sc;
            out[i] = out[i - 1] + sc * (src[i] - out[i - 1]);
        }
        return out;
    }

    // ── Oscillators ─────────────────────────────────────────────────────

    function rsi(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len + 1 || len < 1) return out;
        var seedEnd = _findSeed(src, len + 1);
        if (seedEnd < 0) return out;
        var seedStart = seedEnd - len;
        var avgGain = 0, avgLoss = 0;
        for (var i = seedStart + 1; i <= seedEnd; i++) {
            var ch = src[i] - src[i - 1];
            if (ch > 0) avgGain += ch; else avgLoss -= ch;
        }
        avgGain /= len;
        avgLoss /= len;
        out[seedEnd] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
        for (var i = seedEnd + 1; i < n; i++) {
            if (isNaN(src[i]) || isNaN(src[i - 1])) continue;
            var ch = src[i] - src[i - 1];
            avgGain = (avgGain * (len - 1) + (ch > 0 ? ch : 0)) / len;
            avgLoss = (avgLoss * (len - 1) + (ch < 0 ? -ch : 0)) / len;
            out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
        }
        return out;
    }

    function cci(h, l, c, len) {
        var n = h.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var tp = hlc3(h, l, c);
        var tpSma = sma(tp, len);
        for (var i = len - 1; i < n; i++) {
            if (isNaN(tpSma[i])) continue;
            var md = 0;
            for (var j = 0; j < len; j++) md += Math.abs(tp[i - j] - tpSma[i]);
            md /= len;
            out[i] = md === 0 ? 0 : (tp[i] - tpSma[i]) / (0.015 * md);
        }
        return out;
    }

    function willr(h, l, c, len) {
        var n = h.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            var hh = -Infinity, ll = Infinity;
            for (var j = i - len + 1; j <= i; j++) {
                if (h[j] > hh) hh = h[j];
                if (l[j] < ll) ll = l[j];
            }
            var range = hh - ll;
            out[i] = range === 0 ? 0 : ((hh - c[i]) / range) * -100;
        }
        return out;
    }

    function stoch_k(h, l, c, len, smooth) {
        if (smooth === undefined) smooth = 1;
        var n = h.length, raw = _nanArray(n);
        if (n < len || len < 1) return raw;
        for (var i = len - 1; i < n; i++) {
            var hh = -Infinity, ll = Infinity;
            for (var j = i - len + 1; j <= i; j++) {
                if (h[j] > hh) hh = h[j];
                if (l[j] < ll) ll = l[j];
            }
            var range = hh - ll;
            raw[i] = range === 0 ? 50 : ((c[i] - ll) / range) * 100;
        }
        return smooth <= 1 ? raw : sma(raw, smooth);
    }

    function stoch_d(h, l, c, len, sk, sd) {
        if (sk === undefined) sk = 1;
        if (sd === undefined) sd = 3;
        return sma(stoch_k(h, l, c, len, sk), sd);
    }

    function mfi(h, l, c, v, len) {
        var n = h.length, out = _nanArray(n);
        if (n < len + 1 || len < 1) return out;
        var tp = hlc3(h, l, c);
        for (var i = len; i < n; i++) {
            var posMF = 0, negMF = 0;
            for (var j = i - len + 1; j <= i; j++) {
                var flow = tp[j] * v[j];
                if (tp[j] > tp[j - 1]) posMF += flow; else negMF += flow;
            }
            out[i] = negMF === 0 ? 100 : 100 - 100 / (1 + posMF / negMF);
        }
        return out;
    }

    // ADX / +DI / -DI share the DM/DI computation
    function _diCalc(h, l, c, len) {
        var n = h.length;
        if (n < len + 1 || len < 1) return null;
        var trArr = tr(h, l, c);
        var pDM = _nanArray(n), nDM = _nanArray(n);
        for (var i = 1; i < n; i++) {
            var upMove = h[i] - h[i - 1];
            var downMove = l[i - 1] - l[i];
            pDM[i] = (upMove > downMove && upMove > 0) ? upMove : 0;
            nDM[i] = (downMove > upMove && downMove > 0) ? downMove : 0;
        }
        // Wilder's smooth of TR, +DM, -DM starting from index 1
        var smoothTR = _nanArray(n), smoothPDM = _nanArray(n), smoothNDM = _nanArray(n);
        var trSum = 0, pSum = 0, nSum = 0;
        for (var i = 1; i <= len; i++) {
            trSum += trArr[i]; pSum += pDM[i]; nSum += nDM[i];
        }
        smoothTR[len] = trSum / len;
        smoothPDM[len] = pSum / len;
        smoothNDM[len] = nSum / len;
        for (var i = len + 1; i < n; i++) {
            smoothTR[i] = (smoothTR[i - 1] * (len - 1) + trArr[i]) / len;
            smoothPDM[i] = (smoothPDM[i - 1] * (len - 1) + pDM[i]) / len;
            smoothNDM[i] = (smoothNDM[i - 1] * (len - 1) + nDM[i]) / len;
        }
        var pDI = _nanArray(n), nDI = _nanArray(n), dx = _nanArray(n);
        for (var i = len; i < n; i++) {
            pDI[i] = smoothTR[i] === 0 ? 0 : 100 * smoothPDM[i] / smoothTR[i];
            nDI[i] = smoothTR[i] === 0 ? 0 : 100 * smoothNDM[i] / smoothTR[i];
            var diSum = pDI[i] + nDI[i];
            dx[i] = diSum === 0 ? 0 : 100 * Math.abs(pDI[i] - nDI[i]) / diSum;
        }
        return { pDI: pDI, nDI: nDI, dx: dx };
    }

    function adx(h, l, c, len) {
        var calc = _diCalc(h, l, c, len);
        if (!calc) return _nanArray(h.length);
        var n = h.length, dx = calc.dx, out = _nanArray(n);
        var startIdx = 2 * len;
        if (n <= startIdx) return out;
        var s = 0;
        for (var i = len; i < startIdx; i++) s += dx[i];
        out[startIdx - 1] = s / len;
        for (var i = startIdx; i < n; i++) {
            out[i] = (out[i - 1] * (len - 1) + dx[i]) / len;
        }
        return out;
    }

    function plus_di(h, l, c, len) {
        var calc = _diCalc(h, l, c, len);
        return calc ? calc.pDI : _nanArray(h.length);
    }

    function minus_di(h, l, c, len) {
        var calc = _diCalc(h, l, c, len);
        return calc ? calc.nDI : _nanArray(h.length);
    }

    // ── Volatility ──────────────────────────────────────────────────────

    function tr(h, l, c) {
        var n = h.length, out = _nanArray(n);
        if (n === 0) return out;
        out[0] = h[0] - l[0];
        for (var i = 1; i < n; i++) {
            out[i] = Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1]));
        }
        return out;
    }

    function atr(h, l, c, len) {
        return _wilders(tr(h, l, c), len);
    }

    function stdev(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var mean = sma(src, len);
        for (var i = len - 1; i < n; i++) {
            if (isNaN(mean[i])) continue;
            var sumSq = 0;
            for (var j = 0; j < len; j++) {
                var d = src[i - j] - mean[i];
                sumSq += d * d;
            }
            out[i] = Math.sqrt(sumSq / len);
        }
        return out;
    }

    function bb_upper(src, len, mult) {
        var n = src.length, out = _nanArray(n);
        var mid = sma(src, len), sd = stdev(src, len);
        for (var i = 0; i < n; i++) {
            if (!isNaN(mid[i]) && !isNaN(sd[i])) out[i] = mid[i] + sd[i] * mult;
        }
        return out;
    }

    function bb_lower(src, len, mult) {
        var n = src.length, out = _nanArray(n);
        var mid = sma(src, len), sd = stdev(src, len);
        for (var i = 0; i < n; i++) {
            if (!isNaN(mid[i]) && !isNaN(sd[i])) out[i] = mid[i] - sd[i] * mult;
        }
        return out;
    }

    function bb_mid(src, len) {
        return sma(src, len);
    }

    function keltner_upper(h, l, c, len, mult) {
        var n = h.length, out = _nanArray(n);
        var mid = ema(c, len), a = atr(h, l, c, len);
        for (var i = 0; i < n; i++) {
            if (!isNaN(mid[i]) && !isNaN(a[i])) out[i] = mid[i] + a[i] * mult;
        }
        return out;
    }

    function keltner_lower(h, l, c, len, mult) {
        var n = h.length, out = _nanArray(n);
        var mid = ema(c, len), a = atr(h, l, c, len);
        for (var i = 0; i < n; i++) {
            if (!isNaN(mid[i]) && !isNaN(a[i])) out[i] = mid[i] - a[i] * mult;
        }
        return out;
    }

    // ── Trend ───────────────────────────────────────────────────────────

    function supertrend(h, l, c, atr_len, mult) {
        var n = h.length, out = _nanArray(n);
        var a = atr(h, l, c, atr_len);
        var startIdx = -1;
        for (var i = 0; i < n; i++) { if (!isNaN(a[i])) { startIdx = i; break; } }
        if (startIdx < 0) return out;

        var upperBand, lowerBand, dir = 1;
        for (var i = startIdx; i < n; i++) {
            var mid = (h[i] + l[i]) / 2;
            var basicUpper = mid + mult * a[i];
            var basicLower = mid - mult * a[i];
            if (i === startIdx) {
                upperBand = basicUpper;
                lowerBand = basicLower;
                dir = c[i] <= basicUpper ? 1 : -1;
            } else {
                upperBand = (basicUpper < upperBand || c[i - 1] > upperBand) ? basicUpper : upperBand;
                lowerBand = (basicLower > lowerBand || c[i - 1] < lowerBand) ? basicLower : lowerBand;
                if (dir === 1 && c[i] < lowerBand) dir = -1;
                else if (dir === -1 && c[i] > upperBand) dir = 1;
            }
            out[i] = dir === 1 ? lowerBand : upperBand;
        }
        return out;
    }

    function psar(h, l, step, maxAF) {
        if (step === undefined) step = 0.02;
        if (maxAF === undefined) maxAF = 0.2;
        var n = h.length, out = _nanArray(n);
        if (n < 2) return out;

        var isLong = h[1] >= h[0];
        var af = step;
        var ep = isLong ? h[0] : l[0];
        var sar = isLong ? l[0] : h[0];
        out[0] = sar;

        for (var i = 1; i < n; i++) {
            var prevSar = sar;
            sar = prevSar + af * (ep - prevSar);

            if (isLong) {
                sar = Math.min(sar, l[i - 1]);
                if (i >= 2) sar = Math.min(sar, l[i - 2]);
                if (l[i] < sar) {
                    isLong = false; sar = ep; ep = l[i]; af = step;
                } else {
                    if (h[i] > ep) { ep = h[i]; af = Math.min(af + step, maxAF); }
                }
            } else {
                sar = Math.max(sar, h[i - 1]);
                if (i >= 2) sar = Math.max(sar, h[i - 2]);
                if (h[i] > sar) {
                    isLong = true; sar = ep; ep = h[i]; af = step;
                } else {
                    if (l[i] < ep) { ep = l[i]; af = Math.min(af + step, maxAF); }
                }
            }
            out[i] = sar;
        }
        return out;
    }

    // MACD: computed once, returned as object; public API wraps individual outputs
    function _macd(src, fast, slow, sig) {
        var n = src.length;
        var fastEma = ema(src, fast), slowEma = ema(src, slow);
        var line = _nanArray(n);
        for (var i = 0; i < n; i++) {
            if (!isNaN(fastEma[i]) && !isNaN(slowEma[i])) line[i] = fastEma[i] - slowEma[i];
        }
        // Signal = EMA of MACD line (handles leading NaN via _findSeed in ema())
        var signal = ema(line, sig);
        var hist = _nanArray(n);
        for (var i = 0; i < n; i++) {
            if (!isNaN(line[i]) && !isNaN(signal[i])) hist[i] = line[i] - signal[i];
        }
        return { line: line, signal: signal, hist: hist };
    }

    // ── Volume ──────────────────────────────────────────────────────────

    function obv(c, v) {
        var n = c.length, out = new Array(n);
        out[0] = 0;
        for (var i = 1; i < n; i++) {
            out[i] = out[i - 1] + (c[i] > c[i - 1] ? v[i] : c[i] < c[i - 1] ? -v[i] : 0);
        }
        return out;
    }

    function ad(h, l, c, v) {
        var n = h.length, out = new Array(n);
        for (var i = 0; i < n; i++) {
            var range = h[i] - l[i];
            var clv = range === 0 ? 0 : ((c[i] - l[i]) - (h[i] - c[i])) / range;
            out[i] = (i === 0 ? 0 : out[i - 1]) + clv * v[i];
        }
        return out;
    }

    function cmf(h, l, c, v, len) {
        var n = h.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var clvVol = new Array(n);
        for (var i = 0; i < n; i++) {
            var range = h[i] - l[i];
            var clv = range === 0 ? 0 : ((c[i] - l[i]) - (h[i] - c[i])) / range;
            clvVol[i] = clv * v[i];
        }
        var sumCV = 0, sumV = 0;
        for (var i = 0; i < len; i++) { sumCV += clvVol[i]; sumV += v[i]; }
        out[len - 1] = sumV === 0 ? 0 : sumCV / sumV;
        for (var i = len; i < n; i++) {
            sumCV += clvVol[i] - clvVol[i - len];
            sumV += v[i] - v[i - len];
            out[i] = sumV === 0 ? 0 : sumCV / sumV;
        }
        return out;
    }

    function vwap(h, l, c, v, len) {
        var n = h.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var tp = hlc3(h, l, c);
        var sumTPV = 0, sumV = 0;
        for (var i = 0; i < len; i++) { sumTPV += tp[i] * v[i]; sumV += v[i]; }
        out[len - 1] = sumV === 0 ? NaN : sumTPV / sumV;
        for (var i = len; i < n; i++) {
            sumTPV += tp[i] * v[i] - tp[i - len] * v[i - len];
            sumV += v[i] - v[i - len];
            out[i] = sumV === 0 ? NaN : sumTPV / sumV;
        }
        return out;
    }

    // ── Bar Reference ───────────────────────────────────────────────────

    function highest(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            var mx = -Infinity;
            for (var j = i - len + 1; j <= i; j++) {
                if (!isNaN(src[j]) && src[j] > mx) mx = src[j];
            }
            out[i] = mx === -Infinity ? NaN : mx;
        }
        return out;
    }

    function lowest(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            var mn = Infinity;
            for (var j = i - len + 1; j <= i; j++) {
                if (!isNaN(src[j]) && src[j] < mn) mn = src[j];
            }
            out[i] = mn === Infinity ? NaN : mn;
        }
        return out;
    }

    function median(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            var win = [];
            for (var j = i - len + 1; j <= i; j++) {
                if (!isNaN(src[j])) win.push(src[j]);
            }
            if (win.length === 0) continue;
            win.sort(function(a, b) { return a - b; });
            var mid = Math.floor(win.length / 2);
            out[i] = (win.length % 2 === 0) ? (win[mid - 1] + win[mid]) / 2 : win[mid];
        }
        return out;
    }

    function rolling_sum(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var s = 0, nanCount = 0;
        for (var i = 0; i < len; i++) {
            if (isNaN(src[i])) nanCount++; else s += src[i];
        }
        if (nanCount === 0) out[len - 1] = s;
        for (var i = len; i < n; i++) {
            var leaving = src[i - len], entering = src[i];
            if (isNaN(leaving)) nanCount--; else s -= leaving;
            if (isNaN(entering)) nanCount++; else s += entering;
            if (nanCount === 0) out[i] = s;
        }
        return out;
    }

    function change(src, len) {
        if (len === undefined) len = 1;
        var n = src.length, out = _nanArray(n);
        for (var i = len; i < n; i++) {
            if (!isNaN(src[i]) && !isNaN(src[i - len])) out[i] = src[i] - src[i - len];
        }
        return out;
    }

    function roc(src, len) {
        var n = src.length, out = _nanArray(n);
        for (var i = len; i < n; i++) {
            if (!isNaN(src[i]) && !isNaN(src[i - len]) && src[i - len] !== 0)
                out[i] = ((src[i] - src[i - len]) / src[i - len]) * 100;
        }
        return out;
    }

    function offset_fn(src, n_offset) {
        var n = src.length, out = _nanArray(n);
        for (var i = 0; i < n; i++) {
            var j = i - n_offset;
            if (j >= 0 && j < n) out[i] = src[j];
        }
        return out;
    }

    // ── Logic / Signals ─────────────────────────────────────────────────

    function crossover(a, b) {
        var n = a.length, out = _nanArray(n);
        out[0] = 0;
        for (var i = 1; i < n; i++) {
            out[i] = (a[i] > b[i] && a[i - 1] <= b[i - 1]) ? 1 : 0;
        }
        return out;
    }

    function crossunder(a, b) {
        var n = a.length, out = _nanArray(n);
        out[0] = 0;
        for (var i = 1; i < n; i++) {
            out[i] = (a[i] < b[i] && a[i - 1] >= b[i - 1]) ? 1 : 0;
        }
        return out;
    }

    function valuewhen(cond, src) {
        var n = cond.length, out = _nanArray(n);
        var lastVal = NaN;
        for (var i = 0; i < n; i++) {
            if (cond[i] && cond[i] !== 0 && !isNaN(cond[i])) lastVal = src[i];
            out[i] = lastVal;
        }
        return out;
    }

    function barssince(cond) {
        var n = cond.length, out = _nanArray(n);
        var cnt = NaN;
        for (var i = 0; i < n; i++) {
            if (cond[i] && cond[i] !== 0 && !isNaN(cond[i])) cnt = 0;
            else if (!isNaN(cnt)) cnt++;
            out[i] = cnt;
        }
        return out;
    }

    function count_fn(cond, len) {
        var n = cond.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var c = 0;
        for (var i = 0; i < len; i++) {
            if (cond[i] && cond[i] !== 0 && !isNaN(cond[i])) c++;
        }
        out[len - 1] = c;
        for (var i = len; i < n; i++) {
            var entering = cond[i], leaving = cond[i - len];
            if (entering && entering !== 0 && !isNaN(entering)) c++;
            if (leaving && leaving !== 0 && !isNaN(leaving)) c--;
            out[i] = c;
        }
        return out;
    }

    // ── Stateful / Structure ──────────────────────────────────────────

    function cum(src) {
        var n = src.length, out = _nanArray(n);
        var total = 0;
        for (var i = 0; i < n; i++) {
            if (!isNaN(src[i])) { total += src[i]; out[i] = total; }
        }
        return out;
    }

    function streak(cond) {
        var n = cond.length, out = new Array(n);
        for (var i = 0; i < n; i++) {
            if (cond[i] && cond[i] !== 0 && !isNaN(cond[i])) {
                out[i] = i > 0 ? (out[i - 1] || 0) + 1 : 1;
            } else {
                out[i] = 0;
            }
        }
        return out;
    }

    function highest_bars(src, period) {
        var p = Math.floor(period), n = src.length, out = _nanArray(n);
        if (n < p || p < 1) return out;
        for (var i = p - 1; i < n; i++) {
            var maxIdx = 0, maxVal = -Infinity, skip = false;
            for (var j = 0; j < p; j++) {
                var v = src[i - p + 1 + j];
                if (isNaN(v)) { skip = true; break; }
                if (v > maxVal) { maxVal = v; maxIdx = j; }
            }
            if (!skip) out[i] = p - 1 - maxIdx;
        }
        return out;
    }

    function lowest_bars(src, period) {
        var p = Math.floor(period), n = src.length, out = _nanArray(n);
        if (n < p || p < 1) return out;
        for (var i = p - 1; i < n; i++) {
            var minIdx = 0, minVal = Infinity, skip = false;
            for (var j = 0; j < p; j++) {
                var v = src[i - p + 1 + j];
                if (isNaN(v)) { skip = true; break; }
                if (v < minVal) { minVal = v; minIdx = j; }
            }
            if (!skip) out[i] = p - 1 - minIdx;
        }
        return out;
    }

    function pivothigh(src, left, right) {
        var lb = Math.floor(left), rb = Math.floor(right);
        var n = src.length, out = _nanArray(n);
        if (lb < 1 || rb < 1) return out;
        for (var i = lb + rb; i < n; i++) {
            var pi = i - rb, pv = src[pi], ok = true;
            if (isNaN(pv)) continue;
            for (var j = pi - lb; j < pi; j++) {
                if (isNaN(src[j]) || src[j] >= pv) { ok = false; break; }
            }
            if (ok) {
                for (var j = pi + 1; j <= pi + rb; j++) {
                    if (j >= n || isNaN(src[j]) || src[j] >= pv) { ok = false; break; }
                }
            }
            if (ok) out[i] = pv;
        }
        return out;
    }

    function pivotlow(src, left, right) {
        var lb = Math.floor(left), rb = Math.floor(right);
        var n = src.length, out = _nanArray(n);
        if (lb < 1 || rb < 1) return out;
        for (var i = lb + rb; i < n; i++) {
            var pi = i - rb, pv = src[pi], ok = true;
            if (isNaN(pv)) continue;
            for (var j = pi - lb; j < pi; j++) {
                if (isNaN(src[j]) || src[j] <= pv) { ok = false; break; }
            }
            if (ok) {
                for (var j = pi + 1; j <= pi + rb; j++) {
                    if (j >= n || isNaN(src[j]) || src[j] <= pv) { ok = false; break; }
                }
            }
            if (ok) out[i] = pv;
        }
        return out;
    }

    // ── Math ────────────────────────────────────────────────────────────

    function abs_fn(src) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = Math.abs(src[i]);
        return out;
    }

    function sqrt_fn(src) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = src[i] < 0 ? NaN : Math.sqrt(src[i]);
        return out;
    }

    function pow_fn(src, exp) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = Math.pow(src[i], exp[i]);
        return out;
    }

    function log_fn(src) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = src[i] <= 0 ? NaN : Math.log(src[i]);
        return out;
    }

    function exp_fn(src) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = Math.exp(src[i]);
        return out;
    }

    function round_fn(src, decimals) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) {
            var f = Math.pow(10, decimals[i]);
            out[i] = Math.round(src[i] * f) / f;
        }
        return out;
    }

    function clamp_fn(src, lo, hi) {
        var n = src.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = Math.max(lo[i], Math.min(hi[i], src[i]));
        return out;
    }

    function max_val(a, b) {
        var n = a.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = Math.max(a[i], b[i]);
        return out;
    }

    function min_val(a, b) {
        var n = a.length, out = new Array(n);
        for (var i = 0; i < n; i++) out[i] = Math.min(a[i], b[i]);
        return out;
    }

    // ── Rolling Helpers (shared by quant functions) ─────────────────────

    // Rolling mean over trailing window of `len` bars. NaN-aware.
    function _rollingMean(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        var s = 0, nanCount = 0;
        for (var i = 0; i < len; i++) {
            if (isNaN(src[i])) nanCount++; else s += src[i];
        }
        if (nanCount === 0) out[len - 1] = s / len;
        for (var i = len; i < n; i++) {
            var leaving = src[i - len], entering = src[i];
            if (isNaN(leaving)) nanCount--; else s -= leaving;
            if (isNaN(entering)) nanCount++; else s += entering;
            if (nanCount === 0) out[i] = s / len;
        }
        return out;
    }

    // Rolling population standard deviation. Uses precomputed mean for efficiency.
    function _rollingStd(src, len, mean) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            if (isNaN(mean[i])) continue;
            var sumSq = 0, bad = false;
            for (var j = i - len + 1; j <= i; j++) {
                if (isNaN(src[j])) { bad = true; break; }
                var d = src[j] - mean[i];
                sumSq += d * d;
            }
            if (!bad) out[i] = Math.sqrt(sumSq / len);
        }
        return out;
    }

    // Rolling covariance between two series over trailing window.
    function _rollingCov(a, b, len, meanA, meanB) {
        var n = a.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            if (isNaN(meanA[i]) || isNaN(meanB[i])) continue;
            var s = 0, bad = false;
            for (var j = i - len + 1; j <= i; j++) {
                if (isNaN(a[j]) || isNaN(b[j])) { bad = true; break; }
                s += (a[j] - meanA[i]) * (b[j] - meanB[i]);
            }
            if (!bad) out[i] = s / len;
        }
        return out;
    }

    // ── Quant Functions: Normalization ───────────────────────────────────

    // Rolling z-score: (value - mean) / stdev
    function zscore(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 2) return out;
        var mean = _rollingMean(src, len);
        var sd = _rollingStd(src, len, mean);
        for (var i = 0; i < n; i++) {
            if (!isNaN(mean[i]) && !isNaN(sd[i]) && sd[i] > 0)
                out[i] = (src[i] - mean[i]) / sd[i];
            else if (!isNaN(mean[i]) && !isNaN(sd[i]) && sd[i] === 0)
                out[i] = 0;
        }
        return out;
    }

    // Rolling percentile rank (0-100)
    function percentile(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            if (isNaN(src[i])) continue;
            var countBelow = 0, valid = 0, bad = false;
            for (var j = i - len + 1; j <= i; j++) {
                if (isNaN(src[j])) { bad = true; break; }
                valid++;
                if (src[j] <= src[i]) countBelow++;
            }
            if (!bad && valid > 0) out[i] = (countBelow / valid) * 100;
        }
        return out;
    }

    // Rolling min-max normalization to [0, 1]
    function normalize(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 1) return out;
        for (var i = len - 1; i < n; i++) {
            var mn = Infinity, mx = -Infinity, bad = false;
            for (var j = i - len + 1; j <= i; j++) {
                if (isNaN(src[j])) { bad = true; break; }
                if (src[j] < mn) mn = src[j];
                if (src[j] > mx) mx = src[j];
            }
            if (bad) continue;
            out[i] = mx === mn ? 0.5 : (src[i] - mn) / (mx - mn);
        }
        return out;
    }

    // ── Quant Functions: Correlation & Sensitivity ──────────────────────

    // Rolling Pearson correlation coefficient between two series
    function corr(a, b, len) {
        var n = Math.min(a.length, b.length), out = _nanArray(n);
        if (n < len || len < 2) return out;
        var meanA = _rollingMean(a, len);
        var meanB = _rollingMean(b, len);
        var cov = _rollingCov(a, b, len, meanA, meanB);
        var stdA = _rollingStd(a, len, meanA);
        var stdB = _rollingStd(b, len, meanB);
        for (var i = 0; i < n; i++) {
            if (!isNaN(cov[i]) && !isNaN(stdA[i]) && !isNaN(stdB[i]) && stdA[i] > 0 && stdB[i] > 0)
                out[i] = cov[i] / (stdA[i] * stdB[i]);
            else if (!isNaN(cov[i]) && (stdA[i] === 0 || stdB[i] === 0))
                out[i] = 0;
        }
        return out;
    }

    // Rolling beta: cov(a, b) / var(b) — sensitivity of a to b
    function beta(a, b, len) {
        var n = Math.min(a.length, b.length), out = _nanArray(n);
        if (n < len || len < 2) return out;
        var meanA = _rollingMean(a, len);
        var meanB = _rollingMean(b, len);
        var cov = _rollingCov(a, b, len, meanA, meanB);
        var stdB = _rollingStd(b, len, meanB);
        for (var i = 0; i < n; i++) {
            if (!isNaN(cov[i]) && !isNaN(stdB[i]) && stdB[i] > 0)
                out[i] = cov[i] / (stdB[i] * stdB[i]);
        }
        return out;
    }

    // ── Quant Functions: Linear Fit & Trend Quality ─────────────────────

    // Ordinary least squares regression over trailing window.
    // Returns { slope, intercept, value, r2 } arrays.
    function _ols(src, len) {
        var n = src.length;
        var slope = _nanArray(n), intercept = _nanArray(n);
        var value = _nanArray(n), r2 = _nanArray(n);
        if (n < len || len < 2) return { slope: slope, intercept: intercept, value: value, r2: r2 };
        // Precompute constants for x = 0..len-1
        var sumX = 0, sumX2 = 0;
        for (var k = 0; k < len; k++) { sumX += k; sumX2 += k * k; }
        var denom = len * sumX2 - sumX * sumX;
        for (var i = len - 1; i < n; i++) {
            var sumY = 0, sumXY = 0, bad = false;
            for (var j = 0; j < len; j++) {
                var v = src[i - len + 1 + j];
                if (isNaN(v)) { bad = true; break; }
                sumY += v;
                sumXY += j * v;
            }
            if (bad) continue;
            var b = (len * sumXY - sumX * sumY) / denom;
            var a = (sumY - b * sumX) / len;
            slope[i] = b;
            intercept[i] = a;
            value[i] = a + b * (len - 1);
            // R²: 1 - SSresid / SStotal
            var meanY = sumY / len, ssRes = 0, ssTot = 0;
            for (var j = 0; j < len; j++) {
                var v = src[i - len + 1 + j];
                var fitted = a + b * j;
                ssRes += (v - fitted) * (v - fitted);
                ssTot += (v - meanY) * (v - meanY);
            }
            r2[i] = ssTot === 0 ? 1 : 1 - ssRes / ssTot;
        }
        return { slope: slope, intercept: intercept, value: value, r2: r2 };
    }

    function linreg(src, len) { return _ols(src, len).value; }
    function linreg_slope(src, len) { return _ols(src, len).slope; }
    function r_squared(src, len) { return _ols(src, len).r2; }

    // ── Quant Functions: Distribution & Volatility ──────────────────────

    // Rolling skewness (Fisher's formula)
    function skew_fn(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len || len < 3) return out;
        var mean = _rollingMean(src, len);
        var sd = _rollingStd(src, len, mean);
        for (var i = len - 1; i < n; i++) {
            if (isNaN(mean[i]) || isNaN(sd[i]) || sd[i] === 0) continue;
            var s = 0, bad = false;
            for (var j = i - len + 1; j <= i; j++) {
                if (isNaN(src[j])) { bad = true; break; }
                var d = (src[j] - mean[i]) / sd[i];
                s += d * d * d;
            }
            if (!bad) out[i] = s / len;
        }
        return out;
    }

    // Annualized realized volatility from log returns
    function realized_vol(src, len) {
        var n = src.length, out = _nanArray(n);
        if (n < len + 1 || len < 2) return out;
        // Compute log returns
        var logRet = _nanArray(n);
        for (var i = 1; i < n; i++) {
            if (!isNaN(src[i]) && !isNaN(src[i - 1]) && src[i - 1] > 0 && src[i] > 0)
                logRet[i] = Math.log(src[i] / src[i - 1]);
        }
        // Rolling stdev of log returns, then annualize
        var mean = _rollingMean(logRet, len);
        var sd = _rollingStd(logRet, len, mean);
        var annFactor = Math.sqrt(365);
        for (var i = 0; i < n; i++) {
            if (!isNaN(sd[i])) out[i] = sd[i] * annFactor * 100;
        }
        return out;
    }

    // ── Ichimoku ────────────────────────────────────────────────────────

    function tenkan(h, l, period) {
        if (period === undefined) period = 9;
        var n = h.length, out = _nanArray(n);
        var hh = highest(h, period), ll = lowest(l, period);
        for (var i = 0; i < n; i++) {
            if (!isNaN(hh[i]) && !isNaN(ll[i])) out[i] = (hh[i] + ll[i]) / 2;
        }
        return out;
    }

    function kijun(h, l, period) {
        if (period === undefined) period = 26;
        var n = h.length, out = _nanArray(n);
        var hh = highest(h, period), ll = lowest(l, period);
        for (var i = 0; i < n; i++) {
            if (!isNaN(hh[i]) && !isNaN(ll[i])) out[i] = (hh[i] + ll[i]) / 2;
        }
        return out;
    }

    function senkou_a(h, l) {
        var n = h.length, out = _nanArray(n);
        var t = tenkan(h, l, 9), k = kijun(h, l, 26);
        for (var i = 0; i < n; i++) {
            if (!isNaN(t[i]) && !isNaN(k[i])) out[i] = (t[i] + k[i]) / 2;
        }
        return out;
    }

    function senkou_b(h, l, period) {
        if (period === undefined) period = 52;
        var n = h.length, out = _nanArray(n);
        var hh = highest(h, period), ll = lowest(l, period);
        for (var i = 0; i < n; i++) {
            if (!isNaN(hh[i]) && !isNaN(ll[i])) out[i] = (hh[i] + ll[i]) / 2;
        }
        return out;
    }

    function chikou(c) {
        return offset_fn(c, -26);
    }

    // ── Alligator ──────────────────────────────────────────────────────

    function alligator_jaw(h, l, period, shift) {
        if (period === undefined) period = 13;
        if (shift === undefined) shift = 8;
        var mid = hl2(h, l);
        return offset_fn(_wilders(mid, period), shift);
    }

    function alligator_teeth(h, l, period, shift) {
        if (period === undefined) period = 8;
        if (shift === undefined) shift = 5;
        var mid = hl2(h, l);
        return offset_fn(_wilders(mid, period), shift);
    }

    function alligator_lips(h, l, period, shift) {
        if (period === undefined) period = 5;
        if (shift === undefined) shift = 3;
        var mid = hl2(h, l);
        return offset_fn(_wilders(mid, period), shift);
    }

    // ── Stochastic RSI ─────────────────────────────────────────────────

    function stoch_rsi_k(c, period, k_smooth) {
        if (period === undefined) period = 14;
        if (k_smooth === undefined) k_smooth = 3;
        var n = c.length, rsiArr = rsi(c, period);
        var raw = _nanArray(n);
        for (var i = period; i < n; i++) {
            if (isNaN(rsiArr[i])) continue;
            var hh = -Infinity, ll = Infinity, bad = false;
            for (var j = i - period + 1; j <= i; j++) {
                if (isNaN(rsiArr[j])) { bad = true; break; }
                if (rsiArr[j] > hh) hh = rsiArr[j];
                if (rsiArr[j] < ll) ll = rsiArr[j];
            }
            if (bad) continue;
            var range = hh - ll;
            raw[i] = range === 0 ? 50 : ((rsiArr[i] - ll) / range) * 100;
        }
        return k_smooth <= 1 ? raw : sma(raw, k_smooth);
    }

    function stoch_rsi_d(c, period, k_smooth, d_smooth) {
        if (period === undefined) period = 14;
        if (k_smooth === undefined) k_smooth = 3;
        if (d_smooth === undefined) d_smooth = 3;
        return sma(stoch_rsi_k(c, period, k_smooth), d_smooth);
    }

    // ── Donchian (aliases) ─────────────────────────────────────────────

    function donchian_high(h, period) {
        if (period === undefined) period = 20;
        return highest(h, period);
    }

    function donchian_low(l, period) {
        if (period === undefined) period = 20;
        return lowest(l, period);
    }

    // ── Detection Functions ────────────────────────────────────────────

    function nr7(h, l) {
        var n = h.length, out = _nanArray(n);
        if (n < 7) return out;
        for (var i = 6; i < n; i++) {
            var curRange = h[i] - l[i];
            var isMin = true;
            for (var j = 1; j <= 6; j++) {
                if ((h[i - j] - l[i - j]) <= curRange) { isMin = false; break; }
            }
            out[i] = isMin ? 1 : 0;
        }
        return out;
    }

    // ── SMC pattern primitives ──────────────────────────
    // Mirrors carrac/formula_eval.py. Kept in lockstep by
    // tests/test_formula_registry_parity.py.

    // atrMult defaults to 0.25 to match the FVG chart indicator's
    // atrThresholdPct (calculateFVG in carrac-indicator-pack.js). Pass 0 for
    // the raw unfiltered gap. This codebase has already
    // been bitten by two disagreeing definitions of "fair value gap".
    function gap_up(h, l, c, atrMult) {
        var m = atrMult, n = h.length, out = _nanArray(n);
        if (n < 3) return out;
        var a = m > 0 ? atr(h, l, c, 14) : null;
        for (var i = 2; i < n; i++) {
            if (isNaN(l[i]) || isNaN(h[i - 2])) continue;
            var gap = l[i] - h[i - 2];
            if (gap <= 0) { out[i] = 0; continue; }
            if (a) {
                if (isNaN(a[i])) continue;
                out[i] = gap > a[i] * m ? 1 : 0;
            } else {
                out[i] = 1;
            }
        }
        return out;
    }

    function gap_down(h, l, c, atrMult) {
        var m = atrMult, n = h.length, out = _nanArray(n);
        if (n < 3) return out;
        var a = m > 0 ? atr(h, l, c, 14) : null;
        for (var i = 2; i < n; i++) {
            if (isNaN(h[i]) || isNaN(l[i - 2])) continue;
            var gap = l[i - 2] - h[i];
            if (gap <= 0) { out[i] = 0; continue; }
            if (a) {
                if (isNaN(a[i])) continue;
                out[i] = gap > a[i] * m ? 1 : 0;
            } else {
                out[i] = 1;
            }
        }
        return out;
    }

    function swept_low(l, c, period) {
        var p = Math.floor(period), n = l.length, out = _nanArray(n);
        if (p < 1 || n < p + 1) return out;
        for (var i = p; i < n; i++) {
            var priorLow = Infinity, bad = false;
            for (var j = i - p; j < i; j++) {
                if (isNaN(l[j])) { bad = true; break; }
                if (l[j] < priorLow) priorLow = l[j];
            }
            if (bad || isNaN(l[i]) || isNaN(c[i])) continue;
            out[i] = (l[i] < priorLow && c[i] > priorLow) ? 1 : 0;
        }
        return out;
    }

    function swept_high(h, c, period) {
        var p = Math.floor(period), n = h.length, out = _nanArray(n);
        if (p < 1 || n < p + 1) return out;
        for (var i = p; i < n; i++) {
            var priorHigh = -Infinity, bad = false;
            for (var j = i - p; j < i; j++) {
                if (isNaN(h[j])) { bad = true; break; }
                if (h[j] > priorHigh) priorHigh = h[j];
            }
            if (bad || isNaN(h[i]) || isNaN(c[i])) continue;
            out[i] = (h[i] > priorHigh && c[i] < priorHigh) ? 1 : 0;
        }
        return out;
    }

    function displacement(h, l, c, mult) {
        var m = mult, n = h.length, out = _nanArray(n);
        if (!(m > 0) || n < 16) return out;
        var a = atr(h, l, c, 14);
        for (var i = 1; i < n; i++) {
            var prevAtr = a[i - 1];
            if (isNaN(prevAtr) || prevAtr <= 0) continue;
            if (isNaN(h[i]) || isNaN(l[i])) continue;
            out[i] = (h[i] - l[i]) > m * prevAtr ? 1 : 0;
        }
        return out;
    }

    function reversal_close_up(o, c, h, count) {
        var k = Math.floor(count), n = c.length, out = _nanArray(n);
        if (k < 1 || n < k + 1) return out;
        for (var i = k; i < n; i++) {
            var allBearish = true, windowHigh = -Infinity, bad = false;
            for (var j = i - k; j < i; j++) {
                if (isNaN(o[j]) || isNaN(c[j]) || isNaN(h[j])) { bad = true; break; }
                if (!(c[j] < o[j])) allBearish = false;
                if (h[j] > windowHigh) windowHigh = h[j];
            }
            if (bad || isNaN(c[i])) continue;
            out[i] = (allBearish && c[i] > windowHigh) ? 1 : 0;
        }
        return out;
    }

    function reversal_close_down(o, c, l, count) {
        var k = Math.floor(count), n = c.length, out = _nanArray(n);
        if (k < 1 || n < k + 1) return out;
        for (var i = k; i < n; i++) {
            var allBullish = true, windowLow = Infinity, bad = false;
            for (var j = i - k; j < i; j++) {
                if (isNaN(o[j]) || isNaN(c[j]) || isNaN(l[j])) { bad = true; break; }
                if (!(c[j] > o[j])) allBullish = false;
                if (l[j] < windowLow) windowLow = l[j];
            }
            if (bad || isNaN(c[i])) continue;
            out[i] = (allBullish && c[i] < windowLow) ? 1 : 0;
        }
        return out;
    }

    function atr_percentile(h, l, c, period, lookback) {
        if (period === undefined) period = 14;
        if (lookback === undefined) lookback = 90;
        return percentile(atr(h, l, c, period), lookback);
    }

    function bbw_percentile(c, period, mult, lookback) {
        if (period === undefined) period = 20;
        if (mult === undefined) mult = 2;
        if (lookback === undefined) lookback = 90;
        var n = c.length, bbw = _nanArray(n);
        var upper = bb_upper(c, period, mult), lower = bb_lower(c, period, mult);
        var mid = sma(c, period);
        for (var i = 0; i < n; i++) {
            if (!isNaN(upper[i]) && !isNaN(lower[i]) && !isNaN(mid[i]) && mid[i] !== 0)
                bbw[i] = ((upper[i] - lower[i]) / mid[i]) * 100;
        }
        return percentile(bbw, lookback);
    }

    function squeeze_on(h, l, c) {
        var n = h.length, out = _nanArray(n);
        var bbUp = bb_upper(c, 20, 2), bbLo = bb_lower(c, 20, 2);
        var kUp = keltner_upper(h, l, c, 20, 1.5), kLo = keltner_lower(h, l, c, 20, 1.5);
        for (var i = 0; i < n; i++) {
            if (!isNaN(bbUp[i]) && !isNaN(kUp[i]) && !isNaN(bbLo[i]) && !isNaN(kLo[i]))
                out[i] = (bbLo[i] > kLo[i] && bbUp[i] < kUp[i]) ? 1 : 0;
        }
        return out;
    }

    // ── CPR (Central Pivot Range) ──────────────────────────────────────

    function cpr_pivot(h, l, c) {
        var n = h.length, out = _nanArray(n);
        for (var i = 1; i < n; i++) {
            if (!isNaN(h[i - 1]) && !isNaN(l[i - 1]) && !isNaN(c[i - 1]))
                out[i] = (h[i - 1] + l[i - 1] + c[i - 1]) / 3;
        }
        return out;
    }

    function cpr_r1(h, l, c) {
        var n = h.length, out = _nanArray(n), pv = cpr_pivot(h, l, c);
        for (var i = 1; i < n; i++) {
            if (!isNaN(pv[i])) out[i] = 2 * pv[i] - l[i - 1];
        }
        return out;
    }

    function cpr_r2(h, l, c) {
        var n = h.length, out = _nanArray(n), pv = cpr_pivot(h, l, c);
        for (var i = 1; i < n; i++) {
            if (!isNaN(pv[i])) out[i] = pv[i] + (h[i - 1] - l[i - 1]);
        }
        return out;
    }

    function cpr_r3(h, l, c) {
        var n = h.length, out = _nanArray(n), pv = cpr_pivot(h, l, c);
        for (var i = 1; i < n; i++) {
            if (!isNaN(pv[i])) out[i] = h[i - 1] + 2 * (pv[i] - l[i - 1]);
        }
        return out;
    }

    function cpr_s1(h, l, c) {
        var n = h.length, out = _nanArray(n), pv = cpr_pivot(h, l, c);
        for (var i = 1; i < n; i++) {
            if (!isNaN(pv[i])) out[i] = 2 * pv[i] - h[i - 1];
        }
        return out;
    }

    function cpr_s2(h, l, c) {
        var n = h.length, out = _nanArray(n), pv = cpr_pivot(h, l, c);
        for (var i = 1; i < n; i++) {
            if (!isNaN(pv[i])) out[i] = pv[i] - (h[i - 1] - l[i - 1]);
        }
        return out;
    }

    function cpr_s3(h, l, c) {
        var n = h.length, out = _nanArray(n), pv = cpr_pivot(h, l, c);
        for (var i = 1; i < n; i++) {
            if (!isNaN(pv[i])) out[i] = l[i - 1] - 2 * (h[i - 1] - pv[i]);
        }
        return out;
    }

    // ── Composite State Functions ──────────────────────────────────────

    function ema_ribbon_trend(h, l, c) {
        var n = h.length, out = _nanArray(n);
        var e30 = ema(c, 30), e60 = ema(c, 60);
        var a = atr(h, l, c, 60);
        for (var i = 0; i < n; i++) {
            if (isNaN(e30[i]) || isNaN(e60[i]) || isNaN(a[i])) continue;
            var margin = a[i] * 0.3;
            var diff = e30[i] - e60[i];
            out[i] = diff > margin ? 1 : (diff < -margin ? -1 : 0);
        }
        return out;
    }

    function consensio_fn(c) {
        var n = c.length, out = _nanArray(n);
        var s3 = sma(c, 3), s7 = sma(c, 7), s30 = sma(c, 30);
        for (var i = 0; i < n; i++) {
            if (isNaN(s3[i]) || isNaN(s7[i]) || isNaN(s30[i]) || isNaN(c[i])) continue;
            out[i] = (c[i] > s3[i] ? 1 : 0) + (c[i] > s7[i] ? 1 : 0) + (c[i] > s30[i] ? 1 : 0);
        }
        return out;
    }

    function structure_trend(h, l, c) {
        var n = h.length, out = _nanArray(n);
        if (n < 11) return out;

        // Detect swing highs/lows via 5-bar local extrema
        var swingHigh = _nanArray(n), swingLow = _nanArray(n);
        for (var i = 5; i < n - 5; i++) {
            var isHigh = true, isLow = true;
            for (var j = 1; j <= 5; j++) {
                if (h[i] <= h[i - j] || h[i] <= h[i + j]) isHigh = false;
                if (l[i] >= l[i - j] || l[i] >= l[i + j]) isLow = false;
            }
            if (isHigh) swingHigh[i] = h[i];
            if (isLow) swingLow[i] = l[i];
        }

        // Classify: track last two swing highs and lows
        var lastSH = NaN, prevSH = NaN, lastSL = NaN, prevSL = NaN;
        for (var i = 0; i < n; i++) {
            if (!isNaN(swingHigh[i])) { prevSH = lastSH; lastSH = swingHigh[i]; }
            if (!isNaN(swingLow[i])) { prevSL = lastSL; lastSL = swingLow[i]; }
            if (isNaN(prevSH) || isNaN(prevSL)) { out[i] = 0; continue; }
            var hh = lastSH > prevSH, hl = lastSL > prevSL;
            var lh = lastSH < prevSH, ll = lastSL < prevSL;
            out[i] = (hh && hl) ? 1 : ((lh && ll) ? -1 : 0);
        }
        return out;
    }

    // ── Public API ──────────────────────────────────────────────────────

    window.TALib = {
        // Price shorthands
        ohlc4: ohlc4, hlc3: hlc3, hl2: hl2,
        // Moving averages
        sma: sma, ema: ema, wma: wma, hma: hma, alma: alma, kama: kama,
        // Oscillators
        rsi: rsi, cci: cci, willr: willr,
        stoch_k: stoch_k, stoch_d: stoch_d, mfi: mfi,
        adx: adx, plus_di: plus_di, minus_di: minus_di,
        // Volatility
        tr: tr, atr: atr, stdev: stdev,
        bb_upper: bb_upper, bb_lower: bb_lower, bb_mid: bb_mid,
        keltner_upper: keltner_upper, keltner_lower: keltner_lower,
        // Trend
        supertrend: supertrend, psar: psar,
        macd_line: function(s, f, sl, sig) { return _macd(s, f, sl, sig).line; },
        macd_signal: function(s, f, sl, sig) { return _macd(s, f, sl, sig).signal; },
        macd_hist: function(s, f, sl, sig) { return _macd(s, f, sl, sig).hist; },
        // Volume
        obv: obv, ad: ad, cmf: cmf, vwap: vwap,
        // Bar reference
        highest: highest, lowest: lowest, median: median,
        sum: rolling_sum, change: change, roc: roc, mom: change, offset: offset_fn,
        // Logic / signals
        crossover: crossover, crossunder: crossunder,
        valuewhen: valuewhen, barssince: barssince, count: count_fn,
        cum: cum, streak: streak,
        // Structure
        highest_bars: highest_bars, lowest_bars: lowest_bars,
        pivothigh: pivothigh, pivotlow: pivotlow,
        // Math
        abs: abs_fn, sqrt: sqrt_fn, pow: pow_fn, log: log_fn, exp: exp_fn,
        round: round_fn, clamp: clamp_fn, max_val: max_val, min_val: min_val,
        // Quant: Normalization
        zscore: zscore, percentile: percentile, normalize: normalize,
        // Quant: Correlation & Sensitivity
        corr: corr, beta: beta,
        // Quant: Linear Fit & Trend Quality
        linreg: linreg, linreg_slope: linreg_slope, r_squared: r_squared,
        // Quant: Distribution & Volatility
        skew: skew_fn, realized_vol: realized_vol,
        // Ichimoku
        tenkan: tenkan, kijun: kijun, senkou_a: senkou_a, senkou_b: senkou_b, chikou: chikou,
        // Alligator
        alligator_jaw: alligator_jaw, alligator_teeth: alligator_teeth, alligator_lips: alligator_lips,
        // Stoch RSI
        stoch_rsi_k: stoch_rsi_k, stoch_rsi_d: stoch_rsi_d,
        // Donchian
        donchian_high: donchian_high, donchian_low: donchian_low,
        // Detection
        nr7: nr7, atr_percentile: atr_percentile, bbw_percentile: bbw_percentile, squeeze_on: squeeze_on,
        // SMC pattern primitives
        gap_up: gap_up, gap_down: gap_down,
        swept_low: swept_low, swept_high: swept_high, displacement: displacement,
        reversal_close_up: reversal_close_up, reversal_close_down: reversal_close_down,
        // CPR
        cpr_pivot: cpr_pivot, cpr_r1: cpr_r1, cpr_r2: cpr_r2, cpr_r3: cpr_r3,
        cpr_s1: cpr_s1, cpr_s2: cpr_s2, cpr_s3: cpr_s3,
        // Composite
        ema_ribbon_trend: ema_ribbon_trend, consensio: consensio_fn, structure_trend: structure_trend
    };
})();
