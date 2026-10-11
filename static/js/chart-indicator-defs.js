// chart-indicator-defs.js — the 14 custom klinecharts indicator registrations
// (RSI, VOL, WILLIAMS_R, CCI, STOCH, STOCH_RSI, OBV, MA, EMA, BOLL, MACD and
// the derivatives family).
// The block is self-contained: it reads nothing from renderKlineChart's
// scope. It keeps its own `window._chartTypesRegistered` gate — the flag is
// set at the end of renderKlineChart's chart-init block (chart-render.js), so
// registration retries per render until a chart instance exists. Loaded via
// <script defer> before the chart-app files in index.html and
// chart_standalone.html; renderKlineChart calls registerCarracIndicatorDefs().
// Contract sentinel: registerCarracIndicatorDefs.

function registerCarracIndicatorDefs() {
    if (!window._chartTypesRegistered) {
        try {
            klinecharts.registerIndicator({
                name: 'RSI',
                shortName: 'RSI',
                calcParams: [14, 14],
                precision: 1,
                figures: [
                    { key: 'rsi', title: 'RSI: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#7E57C2' }; } },
                    { key: 'rsiMa', title: 'MA: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#E6A117' }; } }
                ],
                calc: function(dataList, ind) {
                    var period = (ind && ind.calcParams && ind.calcParams[0]) || 14;
                    // Period 14 = the precomputed server-side Wilder RSI (gold-standard
                    // default) — must stay byte-identical, so keep this path untouched.
                    if (period === 14) {
                        var store = window._serverRsiData;
                        if (!store || !store.length) {
                            return dataList.map(function() { return {}; });
                        }
                        var lookup = {};
                        for (var i = 0; i < store.length; i++) {
                            lookup[store[i].time * 1000] = store[i];
                        }
                        return dataList.map(function(d) {
                            var entry = lookup[d.timestamp];
                            if (!entry) return {};
                            var r = { rsi: entry.value };
                            if (entry.ma !== undefined) r.rsiMa = entry.ma;
                            return r;
                        });
                    }
                    // Non-default period: compute Wilder RSI client-side from candles
                    // (otherwise the Period setting would have no effect).
                    var rsiSeries = _wilderRsiFromDataList(dataList, period);
                    return dataList.map(function(d, i) {
                        if (d._phantom || rsiSeries[i] === undefined) return {};
                        return { rsi: rsiSeries[i] };
                    });
                },
                draw: function(params) {
                    var ctx = params.ctx;
                    var bounding = params.bounding;
                    var yAxis = params.yAxis;
                    var w = bounding.width, h = bounding.height;
                    ctx.save();
                    // Background fill between 30-70
                    var y70 = yAxis.convertToPixel(70);
                    var y30 = yAxis.convertToPixel(30);
                    if (y70 >= 0 && y30 <= h) {
                        ctx.fillStyle = 'rgba(126, 87, 194, 0.06)';
                        ctx.fillRect(0, Math.max(0, y70), w, Math.min(h, y30) - Math.max(0, y70));
                    }
                    // Dashed reference lines at 30, 50, 70
                    var levels = [
                        { val: 70, color: 'rgba(120, 123, 134, 0.5)' },
                        { val: 50, color: 'rgba(120, 123, 134, 0.25)' },
                        { val: 30, color: 'rgba(120, 123, 134, 0.5)' }
                    ];
                    ctx.lineWidth = 1;
                    ctx.setLineDash([4, 4]);
                    for (var i = 0; i < levels.length; i++) {
                        var y = yAxis.convertToPixel(levels[i].val);
                        if (y >= 0 && y <= h) {
                            ctx.strokeStyle = levels[i].color;
                            ctx.beginPath();
                            ctx.moveTo(0, y);
                            ctx.lineTo(w, y);
                            ctx.stroke();
                        }
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) { console.warn('[RSI] registerIndicator failed:', e); }

        // Custom VOL indicator — identical to built-in but stops MA at phantom bars.
        // Without this, the built-in VOL MA line extends into the future area.
        try {
            klinecharts.registerIndicator({
                name: 'VOL',
                shortName: 'Vol',
                calcParams: [20],
                precision: 0,
                shouldFormatBigNumber: true,
                figures: [
                    { key: 'volume', title: 'Vol: ', type: 'bar',
                      baseValue: 0,
                      styles: function(data) {
                          var c = (data.current && data.current.kpiData) || {};
                          var kd = data.current && data.current.kLineData;
                          if (!kd || kd._phantom) return { color: 'transparent' };
                          var up = kd.close >= kd.open;
                          return { color: up ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)' };
                      }
                    },
                    { key: 'ma', title: 'MA: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#E6A117' }; } }
                ],
                calc: function(dataList, indicator) {
                    var period = (indicator.calcParams && indicator.calcParams[0]) || 20;
                    var result = [];
                    var sum = 0;
                    for (var i = 0; i < dataList.length; i++) {
                        var d = dataList[i];
                        var vol = (d.volume !== undefined && d.volume !== null) ? d.volume : 0;
                        // Phantom bars: return empty so MA line stops
                        if (d._phantom) {
                            result.push({});
                            continue;
                        }
                        sum += vol;
                        if (i >= period) sum -= ((dataList[i - period].volume) || 0);
                        var ma = (i >= period - 1) ? sum / period : null;
                        result.push({ volume: vol, ma: ma });
                    }
                    return result;
                }
            });
        } catch (e) { console.warn('[VOL] registerIndicator failed:', e); }

        // Williams %R oscillator (0 to -100)
        try {
            klinecharts.registerIndicator({
                name: 'WILLIAMS_R',
                shortName: '%R',
                calcParams: [14],
                precision: 1,
                figures: [
                    { key: 'wr', title: '%R: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#FF6D00' }; } }
                ],
                calc: function(dataList, params) {
                    var period = (params && params.calcParams && params.calcParams[0]) || 14;
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        if (i < period - 1) return {};
                        var hh = -Infinity, ll = Infinity;
                        for (var j = i - period + 1; j <= i; j++) {
                            if (dataList[j].high > hh) hh = dataList[j].high;
                            if (dataList[j].low < ll) ll = dataList[j].low;
                        }
                        if (hh === ll) return { wr: 0 };
                        return { wr: ((hh - d.close) / (hh - ll)) * -100 };
                    });
                },
                draw: function(params) {
                    var ctx = params.ctx;
                    var bounding = params.bounding;
                    var yAxis = params.yAxis;
                    var w = bounding.width, h = bounding.height;
                    ctx.save();
                    // Background fill between -80 and -20 (OB/OS zone)
                    var y20 = yAxis.convertToPixel(-20);
                    var y80 = yAxis.convertToPixel(-80);
                    if (y20 >= 0 && y80 <= h) {
                        ctx.fillStyle = 'rgba(255, 109, 0, 0.06)';
                        ctx.fillRect(0, Math.max(0, y20), w, Math.min(h, y80) - Math.max(0, y20));
                    }
                    // Dashed reference lines at -20, -50, -80
                    var levels = [
                        { val: -20, color: 'rgba(120, 123, 134, 0.5)' },
                        { val: -50, color: 'rgba(120, 123, 134, 0.25)' },
                        { val: -80, color: 'rgba(120, 123, 134, 0.5)' }
                    ];
                    ctx.lineWidth = 1;
                    ctx.setLineDash([4, 4]);
                    for (var i = 0; i < levels.length; i++) {
                        var y = yAxis.convertToPixel(levels[i].val);
                        if (y >= 0 && y <= h) {
                            ctx.strokeStyle = levels[i].color;
                            ctx.beginPath();
                            ctx.moveTo(0, y);
                            ctx.lineTo(w, y);
                            ctx.stroke();
                        }
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) { console.warn('[WILLIAMS_R] registerIndicator failed:', e); }

        // CCI oscillator (oscillates around 0)
        try {
            klinecharts.registerIndicator({
                name: 'CCI',
                shortName: 'CCI',
                calcParams: [20],
                precision: 1,
                figures: [
                    { key: 'cci', title: 'CCI: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#00BCD4' }; } }
                ],
                calc: function(dataList, params) {
                    var period = (params && params.calcParams && params.calcParams[0]) || 20;
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        if (i < period - 1) return {};
                        // Typical price
                        var tpSum = 0;
                        for (var j = i - period + 1; j <= i; j++) {
                            tpSum += (dataList[j].high + dataList[j].low + dataList[j].close) / 3;
                        }
                        var tpMean = tpSum / period;
                        // Mean deviation
                        var mdSum = 0;
                        for (var j = i - period + 1; j <= i; j++) {
                            mdSum += Math.abs((dataList[j].high + dataList[j].low + dataList[j].close) / 3 - tpMean);
                        }
                        var md = mdSum / period;
                        var tp = (d.high + d.low + d.close) / 3;
                        if (md === 0) return { cci: 0 };
                        return { cci: (tp - tpMean) / (0.015 * md) };
                    });
                },
                draw: function(params) {
                    var ctx = params.ctx;
                    var bounding = params.bounding;
                    var yAxis = params.yAxis;
                    var w = bounding.width, h = bounding.height;
                    ctx.save();
                    // Background fill between -100 and +100
                    var y100 = yAxis.convertToPixel(100);
                    var yn100 = yAxis.convertToPixel(-100);
                    if (y100 >= 0 && yn100 <= h) {
                        ctx.fillStyle = 'rgba(0, 188, 212, 0.06)';
                        ctx.fillRect(0, Math.max(0, y100), w, Math.min(h, yn100) - Math.max(0, y100));
                    }
                    // Dashed reference lines at +100, 0, -100
                    var levels = [
                        { val: 100, color: 'rgba(120, 123, 134, 0.5)' },
                        { val: 0, color: 'rgba(120, 123, 134, 0.25)' },
                        { val: -100, color: 'rgba(120, 123, 134, 0.5)' }
                    ];
                    ctx.lineWidth = 1;
                    ctx.setLineDash([4, 4]);
                    for (var i = 0; i < levels.length; i++) {
                        var y = yAxis.convertToPixel(levels[i].val);
                        if (y >= 0 && y <= h) {
                            ctx.strokeStyle = levels[i].color;
                            ctx.beginPath();
                            ctx.moveTo(0, y);
                            ctx.lineTo(w, y);
                            ctx.stroke();
                        }
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) { console.warn('[CCI] registerIndicator failed:', e); }

        // Stochastic Oscillator (%K/%D, 0-100)
        try {
            klinecharts.registerIndicator({
                name: 'STOCH',
                shortName: 'Stoch',
                calcParams: [14, 3, 3],
                precision: 1,
                figures: [
                    { key: 'k', title: '%K: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#2196F3' }; } },
                    { key: 'd', title: '%D: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#FF9800' }; } }
                ],
                calc: function(dataList, params) {
                    var kPeriod = (params && params.calcParams && params.calcParams[0]) || 14;
                    var smooth = (params && params.calcParams && params.calcParams[1]) || 3;
                    var dPeriod = (params && params.calcParams && params.calcParams[2]) || 3;
                    var n = dataList.length;
                    // Raw %K
                    var rawK = new Array(n);
                    for (var i = kPeriod - 1; i < n; i++) {
                        var hh = -Infinity, ll = Infinity;
                        for (var j = i - kPeriod + 1; j <= i; j++) {
                            if (dataList[j].high > hh) hh = dataList[j].high;
                            if (dataList[j].low < ll) ll = dataList[j].low;
                        }
                        rawK[i] = hh === ll ? 50 : ((dataList[i].close - ll) / (hh - ll)) * 100;
                    }
                    // Smooth %K = SMA(rawK, smooth)
                    var smoothK = new Array(n);
                    for (var i = 0; i < n; i++) {
                        if (rawK[i] === undefined) continue;
                        var sum = 0, cnt = 0;
                        for (var j = Math.max(0, i - smooth + 1); j <= i; j++) {
                            if (rawK[j] !== undefined) { sum += rawK[j]; cnt++; }
                        }
                        if (cnt === smooth) smoothK[i] = sum / cnt;
                    }
                    // %D = SMA(smoothK, dPeriod)
                    var dLine = new Array(n);
                    for (var i = 0; i < n; i++) {
                        if (smoothK[i] === undefined) continue;
                        var sum = 0, cnt = 0;
                        for (var j = Math.max(0, i - dPeriod + 1); j <= i; j++) {
                            if (smoothK[j] !== undefined) { sum += smoothK[j]; cnt++; }
                        }
                        if (cnt === dPeriod) dLine[i] = sum / cnt;
                    }
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        var r = {};
                        if (smoothK[i] !== undefined) r.k = smoothK[i];
                        if (dLine[i] !== undefined) r.d = dLine[i];
                        return r;
                    });
                },
                draw: function(params) {
                    var ctx = params.ctx;
                    var bounding = params.bounding;
                    var yAxis = params.yAxis;
                    var w = bounding.width, h = bounding.height;
                    ctx.save();
                    var y80 = yAxis.convertToPixel(80);
                    var y20 = yAxis.convertToPixel(20);
                    if (y80 >= 0 && y20 <= h) {
                        ctx.fillStyle = 'rgba(33, 150, 243, 0.06)';
                        ctx.fillRect(0, Math.max(0, y80), w, Math.min(h, y20) - Math.max(0, y80));
                    }
                    var levels = [
                        { val: 80, color: 'rgba(120, 123, 134, 0.5)' },
                        { val: 50, color: 'rgba(120, 123, 134, 0.25)' },
                        { val: 20, color: 'rgba(120, 123, 134, 0.5)' }
                    ];
                    ctx.lineWidth = 1;
                    ctx.setLineDash([4, 4]);
                    for (var i = 0; i < levels.length; i++) {
                        var y = yAxis.convertToPixel(levels[i].val);
                        if (y >= 0 && y <= h) {
                            ctx.strokeStyle = levels[i].color;
                            ctx.beginPath();
                            ctx.moveTo(0, y);
                            ctx.lineTo(w, y);
                            ctx.stroke();
                        }
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) { console.warn('[STOCH] registerIndicator failed:', e); }

        // Stochastic RSI (0-100)
        try {
            klinecharts.registerIndicator({
                name: 'STOCH_RSI',
                shortName: 'StochRSI',
                calcParams: [14, 14, 3, 3],
                precision: 1,
                figures: [
                    { key: 'k', title: '%K: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#9C27B0' }; } },
                    { key: 'd', title: '%D: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#E91E63' }; } }
                ],
                calc: function(dataList, params) {
                    var rsiPeriod = (params && params.calcParams && params.calcParams[0]) || 14;
                    var stochPeriod = (params && params.calcParams && params.calcParams[1]) || 14;
                    var kSmooth = (params && params.calcParams && params.calcParams[2]) || 3;
                    var dSmooth = (params && params.calcParams && params.calcParams[3]) || 3;
                    var n = dataList.length;
                    // Step 1: Calculate RSI
                    var rsi = new Array(n);
                    var avgGain = 0, avgLoss = 0;
                    for (var i = 1; i <= rsiPeriod && i < n; i++) {
                        var ch = dataList[i].close - dataList[i - 1].close;
                        if (ch > 0) avgGain += ch; else avgLoss -= ch;
                    }
                    if (rsiPeriod < n) {
                        avgGain /= rsiPeriod;
                        avgLoss /= rsiPeriod;
                        rsi[rsiPeriod] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
                        for (var i = rsiPeriod + 1; i < n; i++) {
                            var ch = dataList[i].close - dataList[i - 1].close;
                            avgGain = (avgGain * (rsiPeriod - 1) + (ch > 0 ? ch : 0)) / rsiPeriod;
                            avgLoss = (avgLoss * (rsiPeriod - 1) + (ch < 0 ? -ch : 0)) / rsiPeriod;
                            rsi[i] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
                        }
                    }
                    // Step 2: Stochastic of RSI
                    var rawK = new Array(n);
                    for (var i = 0; i < n; i++) {
                        if (rsi[i] === undefined) continue;
                        var hh = -Infinity, ll = Infinity;
                        var validCount = 0;
                        for (var j = Math.max(0, i - stochPeriod + 1); j <= i; j++) {
                            if (rsi[j] !== undefined) {
                                if (rsi[j] > hh) hh = rsi[j];
                                if (rsi[j] < ll) ll = rsi[j];
                                validCount++;
                            }
                        }
                        if (validCount >= stochPeriod) {
                            rawK[i] = hh === ll ? 50 : ((rsi[i] - ll) / (hh - ll)) * 100;
                        }
                    }
                    // Step 3: Smooth %K
                    var smoothK = new Array(n);
                    for (var i = 0; i < n; i++) {
                        if (rawK[i] === undefined) continue;
                        var sum = 0, cnt = 0;
                        for (var j = Math.max(0, i - kSmooth + 1); j <= i; j++) {
                            if (rawK[j] !== undefined) { sum += rawK[j]; cnt++; }
                        }
                        if (cnt === kSmooth) smoothK[i] = sum / cnt;
                    }
                    // Step 4: %D = SMA(smoothK, dSmooth)
                    var dLine = new Array(n);
                    for (var i = 0; i < n; i++) {
                        if (smoothK[i] === undefined) continue;
                        var sum = 0, cnt = 0;
                        for (var j = Math.max(0, i - dSmooth + 1); j <= i; j++) {
                            if (smoothK[j] !== undefined) { sum += smoothK[j]; cnt++; }
                        }
                        if (cnt === dSmooth) dLine[i] = sum / cnt;
                    }
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        var r = {};
                        if (smoothK[i] !== undefined) r.k = smoothK[i];
                        if (dLine[i] !== undefined) r.d = dLine[i];
                        return r;
                    });
                },
                draw: function(params) {
                    var ctx = params.ctx;
                    var bounding = params.bounding;
                    var yAxis = params.yAxis;
                    var w = bounding.width, h = bounding.height;
                    ctx.save();
                    var y80 = yAxis.convertToPixel(80);
                    var y20 = yAxis.convertToPixel(20);
                    if (y80 >= 0 && y20 <= h) {
                        ctx.fillStyle = 'rgba(156, 39, 176, 0.06)';
                        ctx.fillRect(0, Math.max(0, y80), w, Math.min(h, y20) - Math.max(0, y80));
                    }
                    var levels = [
                        { val: 80, color: 'rgba(120, 123, 134, 0.5)' },
                        { val: 50, color: 'rgba(120, 123, 134, 0.25)' },
                        { val: 20, color: 'rgba(120, 123, 134, 0.5)' }
                    ];
                    ctx.lineWidth = 1;
                    ctx.setLineDash([4, 4]);
                    for (var i = 0; i < levels.length; i++) {
                        var y = yAxis.convertToPixel(levels[i].val);
                        if (y >= 0 && y <= h) {
                            ctx.strokeStyle = levels[i].color;
                            ctx.beginPath();
                            ctx.moveTo(0, y);
                            ctx.lineTo(w, y);
                            ctx.stroke();
                        }
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) { console.warn('[STOCH_RSI] registerIndicator failed:', e); }

        // OBV (On Balance Volume — cumulative)
        try {
            klinecharts.registerIndicator({
                name: 'OBV',
                shortName: 'OBV',
                calcParams: [],
                precision: 0,
                figures: [
                    { key: 'obv', title: 'OBV: ', type: 'line',
                      styles: function() { return { style: 'solid', size: 1, color: '#4CAF50' }; } }
                ],
                calc: function(dataList) {
                    var obv = 0;
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        if (i === 0) { obv = d.volume || 0; return { obv: obv }; }
                        if (d.close > dataList[i - 1].close) obv += (d.volume || 0);
                        else if (d.close < dataList[i - 1].close) obv -= (d.volume || 0);
                        return { obv: obv };
                    });
                },
                draw: function(params) {
                    var ctx = params.ctx;
                    var bounding = params.bounding;
                    var yAxis = params.yAxis;
                    var w = bounding.width, h = bounding.height;
                    ctx.save();
                    // Zero line
                    var y0 = yAxis.convertToPixel(0);
                    if (y0 >= 0 && y0 <= h) {
                        ctx.strokeStyle = 'rgba(120, 123, 134, 0.25)';
                        ctx.lineWidth = 1;
                        ctx.setLineDash([4, 4]);
                        ctx.beginPath();
                        ctx.moveTo(0, y0);
                        ctx.lineTo(w, y0);
                        ctx.stroke();
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) { console.warn('[OBV] registerIndicator failed:', e); }

        // Custom MA — stops at phantom bars
        try {
            klinecharts.registerIndicator({
                name: 'MA', shortName: 'MA', calcParams: [20], precision: 2, shouldOhlc: false,
                figures: [{ key: 'ma', title: 'MA: ', type: 'line', styles: function() { return { style: 'solid', size: 1, color: '#E6A117' }; } }],
                calc: function(dataList, ind) {
                    var p = (ind.calcParams && ind.calcParams[0]) || 20;
                    var sum = 0;
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        sum += d.close;
                        if (i >= p) sum -= dataList[i - p].close;
                        return i >= p - 1 ? { ma: sum / p } : {};
                    });
                }
            });
        } catch(e) {}

        // Custom EMA — stops at phantom bars
        try {
            klinecharts.registerIndicator({
                name: 'EMA', shortName: 'EMA', calcParams: [21], precision: 2, shouldOhlc: false,
                figures: [{ key: 'ema', title: 'EMA: ', type: 'line', styles: function() { return { style: 'solid', size: 1, color: '#2196F3' }; } }],
                calc: function(dataList, ind) {
                    var p = (ind.calcParams && ind.calcParams[0]) || 21;
                    var k = 2 / (p + 1);
                    var ema = null;
                    return dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        if (ema === null) { ema = d.close; return i >= p - 1 ? { ema: ema } : {}; }
                        ema = d.close * k + ema * (1 - k);
                        return i >= p - 1 ? { ema: ema } : {};
                    });
                }
            });
        } catch(e) {}

        // Custom BOLL — stops at phantom bars, supports time-axis offset
        try {
            klinecharts.registerIndicator({
                name: 'BOLL', shortName: 'BOLL', calcParams: [20, 2, 0], precision: 2, shouldOhlc: false,
                figures: [
                    { key: 'mid', title: 'MID: ', type: 'line', styles: function() { return { color: '#E6A117' }; } },
                    { key: 'upper', title: 'UP: ', type: 'line', styles: function() { return { color: '#2196F3' }; } },
                    { key: 'lower', title: 'LOW: ', type: 'line', styles: function() { return { color: '#E91E63' }; } }
                ],
                calc: function(dataList, ind) {
                    var p = (ind.calcParams && ind.calcParams[0]) || 20;
                    var mult = (ind.calcParams && ind.calcParams[1]) || 2;
                    var offset = (ind.calcParams && ind.calcParams[2]) || 0;
                    // Compute raw bands first (unshifted)
                    var raw = dataList.map(function(d, i) {
                        if (d._phantom || i < p - 1) return null;
                        var sum = 0;
                        for (var j = i - p + 1; j <= i; j++) sum += dataList[j].close;
                        var mid = sum / p;
                        var sqSum = 0;
                        for (var j = i - p + 1; j <= i; j++) sqSum += Math.pow(dataList[j].close - mid, 2);
                        var sd = Math.sqrt(sqSum / p);
                        return { mid: mid, upper: mid + mult * sd, lower: mid - mult * sd };
                    });
                    // Apply offset: positive shifts right (lag), negative shifts left (lead)
                    if (offset === 0) return raw.map(function(v) { return v || {}; });
                    var n = raw.length;
                    var shifted = new Array(n);
                    for (var i = 0; i < n; i++) {
                        var src = i - offset;
                        shifted[i] = (src >= 0 && src < n && raw[src]) ? raw[src] : {};
                    }
                    return shifted;
                }
            });
        } catch(e) {}

        // Custom MACD — stops at phantom bars
        try {
            klinecharts.registerIndicator({
                name: 'MACD', shortName: 'MACD', calcParams: [12, 26, 9], precision: 2,
                figures: [
                    { key: 'dif', title: 'DIF: ', type: 'line', styles: function() { return { color: '#2196F3' }; } },
                    { key: 'dea', title: 'DEA: ', type: 'line', styles: function() { return { color: '#E6A117' }; } },
                    { key: 'macd', title: 'MACD: ', type: 'bar', baseValue: 0,
                      styles: function(data) {
                          var kd = data.current && data.current.kLineData;
                          if (!kd) return { color: 'rgba(120,123,134,0.3)' };
                          var map = window._macdColorMap;
                          if (map) {
                              var v = map[kd.timestamp];
                              if (v !== undefined) return { color: v >= 0 ? 'rgba(38,166,154,0.7)' : 'rgba(239,83,80,0.7)' };
                          }
                          return { color: 'rgba(120,123,134,0.3)' };
                      }
                    }
                ],
                calc: function(dataList, ind) {
                    var fast = (ind.calcParams && ind.calcParams[0]) || 12;
                    var slow = (ind.calcParams && ind.calcParams[1]) || 26;
                    var sig = (ind.calcParams && ind.calcParams[2]) || 9;
                    var kf = 2 / (fast + 1), ks = 2 / (slow + 1), ksg = 2 / (sig + 1);
                    var emaFast = null, emaSlow = null, dea = null;
                    var results = dataList.map(function(d, i) {
                        if (d._phantom) return {};
                        var c = d.close;
                        emaFast = emaFast === null ? c : c * kf + emaFast * (1 - kf);
                        emaSlow = emaSlow === null ? c : c * ks + emaSlow * (1 - ks);
                        if (i < slow - 1) return {};
                        var dif = emaFast - emaSlow;
                        dea = dea === null ? dif : dif * ksg + dea * (1 - ksg);
                        return { dif: dif, dea: dea, macd: (dif - dea) * 2 };
                    });
                    // Build timestamp→macd map for O(1) color lookup in styles()
                    var colorMap = {};
                    for (var mi = 0; mi < results.length; mi++) {
                        if (results[mi].macd !== undefined) colorMap[dataList[mi].timestamp] = results[mi].macd;
                    }
                    window._macdColorMap = colorMap;
                    return results;
                }
            });
        } catch(e) {}

        window._derivPaneData = window._derivPaneData || {};

        // CVD Spot: custom draw for a continuous colored line (no segment gaps)
        try {
            klinecharts.registerIndicator({
                name: 'DERIV_CVD_SPOT',
                shortName: 'CVD Spot',
                precision: 0,
                calc: function(dataList) {
                    var store = (window._derivPaneData || {})['cvd_spot'];
                    if (!store || !store.data || !store.data.length) {
                        return dataList.map(function() { return {}; });
                    }
                    var lookup = {};
                    var vk = store.valueKey;
                    for (var i = 0; i < store.data.length; i++) {
                        lookup[store.data[i].time * 1000] = store.data[i];
                    }
                    return dataList.map(function(d) {
                        var entry = lookup[d.timestamp];
                        return entry ? { val: entry[vk] } : {};
                    });
                },
                figures: [
                    { key: 'val', title: ' ', type: 'line', styles: function() { return { style: 'stroke', color: 'transparent' }; } }
                ],
                draw: function(params) {
                    var ctx = params.ctx;
                    var dataList = params.dataList;
                    var indicator = params.indicator;
                    var bounding = params.bounding;
                    var visibleRange = params.visibleRange;
                    var xAxis = params.xAxis;
                    var yAxis = params.yAxis;
                    if (!indicator.result || !indicator.result.length) return false;
                    var results = indicator.result;
                    var from = visibleRange.from;
                    var to = Math.min(visibleRange.to, results.length);

                    // Draw zero line
                    var y0 = yAxis.convertToPixel(0);
                    if (y0 >= 0 && y0 <= bounding.height) {
                        ctx.save();
                        ctx.strokeStyle = 'rgba(120, 123, 134, 0.3)';
                        ctx.lineWidth = 1;
                        ctx.setLineDash([4, 4]);
                        ctx.beginPath();
                        ctx.moveTo(0, y0);
                        ctx.lineTo(bounding.width, y0);
                        ctx.stroke();
                        ctx.restore();
                    }

                    // Draw colored CVD line: green when rising, red when falling
                    ctx.save();
                    ctx.lineWidth = 1.5;
                    ctx.lineJoin = 'round';
                    ctx.lineCap = 'round';

                    var prevX = null, prevY = null, prevVal = null;
                    for (var i = from; i < to; i++) {
                        var r = results[i];
                        if (!r || r.val === undefined || r.val === null) { prevX = null; continue; }
                        var x = xAxis.convertToPixel(i);
                        var y = yAxis.convertToPixel(r.val);
                        if (prevX !== null) {
                            ctx.strokeStyle = r.val >= prevVal ? '#26a69a' : '#ef5350';
                            ctx.beginPath();
                            ctx.moveTo(prevX, prevY);
                            ctx.lineTo(x, y);
                            ctx.stroke();
                        }
                        prevX = x; prevY = y; prevVal = r.val;
                    }
                    ctx.restore();
                    return false;
                }
            });
        } catch (e) {}

        // Other derivative pane indicators (OI, CVD Futures)
        var _derivTypes = [
            { name: 'DERIV_OI', shortName: 'Open Interest', key: 'oi_line' },
            { name: 'DERIV_CVD_USDT', shortName: 'CVD USDT-M', key: 'cvd_usdt' },
            { name: 'DERIV_CVD_COIN', shortName: 'CVD COIN-M', key: 'cvd_coin' }
        ];
        _derivTypes.forEach(function(dt) {
            try {
                klinecharts.registerIndicator({
                    name: dt.name,
                    shortName: dt.shortName,
                    precision: 0,
                    calc: function(dataList) {
                        var store = (window._derivPaneData || {})[dt.key];
                        if (!store || !store.data || !store.data.length) {
                            return dataList.map(function() { return {}; });
                        }
                        var lookup = {};
                        var vk = store.valueKey;
                        for (var i = 0; i < store.data.length; i++) {
                            lookup[store.data[i].time * 1000] = store.data[i];
                        }
                        return dataList.map(function(d) {
                            var entry = lookup[d.timestamp];
                            return entry ? { val: entry[vk] } : {};
                        });
                    },
                    figures: [
                        { key: 'val', title: ' ', type: 'line', styles: function() { return { style: 'stroke', color: 'transparent' }; } }
                    ]
                });
            } catch (e) {}
        });

        // Combined derivatives indicator: OI + CVD USDT + CVD COIN in one pane, min-max normalized
        try {
            klinecharts.registerIndicator({
                name: 'DERIV_COMBINED',
                shortName: 'Derivatives',
                precision: 1,
                calc: function(dataList) {
                    var store = window._derivCombinedData || {};
                    var keys = Object.keys(store);
                    if (keys.length === 0) return dataList.map(function() { return {}; });
                    // Build per-series lookup + min/max for normalization
                    var lookups = {};
                    var mins = {};
                    var maxs = {};
                    keys.forEach(function(k) {
                        var series = store[k];
                        lookups[k] = {};
                        mins[k] = Infinity;
                        maxs[k] = -Infinity;
                        for (var i = 0; i < series.data.length; i++) {
                            var val = series.data[i][series.valueKey];
                            lookups[k][series.data[i].time * 1000] = val;
                            if (val < mins[k]) mins[k] = val;
                            if (val > maxs[k]) maxs[k] = val;
                        }
                    });
                    return dataList.map(function(d) {
                        var row = {};
                        keys.forEach(function(k) {
                            var raw = lookups[k][d.timestamp];
                            if (raw !== undefined) {
                                var range = maxs[k] - mins[k];
                                row[k] = range > 0 ? ((raw - mins[k]) / range) * 100 : 50;
                            }
                        });
                        return row;
                    });
                },
                figures: [
                    { key: 'oi', title: 'OI: ', type: 'line', styles: function() { return { color: '#FF9800' }; } },
                    { key: 'cvd_usdt', title: 'CVD-U: ', type: 'line', styles: function() { return { color: '#00BCD4' }; } },
                    { key: 'cvd_coin', title: 'CVD-C: ', type: 'line', styles: function() { return { color: '#E040FB' }; } }
                ]
            });
        } catch (e) {}
    }
}
