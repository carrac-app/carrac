/* Chart Preview Tooltip */
// ==================== //
// Chart Preview System //
// ==================== //

function initChartTooltip() {
    chartTooltip = document.getElementById('chart-tooltip');
}

function showChartTooltip(symbol, event) {
    // Skip hover preview in split layout — chart is always visible
    if (splitLayoutActive) return;
    if (!chartTooltip) initChartTooltip();
    if (!chartTooltip) return;

    // Clear any pending timeout
    if (chartHoverTimeout) {
        clearTimeout(chartHoverTimeout);
    }

    // Debounce the chart display
    chartHoverTimeout = setTimeout(function() {
        displayChart(symbol, event);
    }, chartHoverDelay);
}

function hideChartTooltip() {
    // Clear pending timeout
    if (chartHoverTimeout) {
        clearTimeout(chartHoverTimeout);
        chartHoverTimeout = null;
    }

    // Hide tooltip
    if (chartTooltip) {
        chartTooltip.classList.remove('visible');
    }

    // Clean up chart instance
    if (chartInstance) {
        chartInstance.remove();
        chartInstance = null;
    }
}

function cleanupChartCache() {
    // Remove expired entries and enforce max size
    var keys = Object.keys(chartDataCache);
    var now = Date.now();

    // Remove expired entries first
    keys.forEach(function(key) {
        if (now - chartDataCache[key].timestamp > chartCacheTTL) {
            delete chartDataCache[key];
        }
    });

    // If still over limit, remove oldest entries
    keys = Object.keys(chartDataCache);
    if (keys.length > chartCacheMaxSize) {
        keys.sort(function(a, b) {
            return chartDataCache[a].timestamp - chartDataCache[b].timestamp;
        });
        var toRemove = keys.length - chartCacheMaxSize;
        for (var i = 0; i < toRemove; i++) {
            delete chartDataCache[keys[i]];
        }
    }
}

function displayChart(symbol, event) {
    if (!chartTooltip) return;

    // Cancel any in-flight request
    if (chartAbortController) {
        chartAbortController.abort();
    }
    chartAbortController = new AbortController();

    // Show loading state
    chartTooltip.innerHTML = '<div class="chart-tooltip-loading">Loading chart...</div>';
    positionTooltip(event);
    chartTooltip.classList.add('visible');

    // Always fetch daily (1D) data for preview chart
    var cacheKey = symbol + '_1D';

    // Check cache
    var cached = chartDataCache[cacheKey];
    if (cached && (Date.now() - cached.timestamp < chartCacheTTL)) {
        renderChart(symbol, cached.data);
        return;
    }

    // Cleanup cache periodically (every 10th fetch)
    if (Math.random() < 0.1) {
        cleanupChartCache();
    }

    // Fetch daily chart data
    fetch('/api/chart/' + encodeURIComponent(symbol) + '?days=180&mode=htf', {
        credentials: 'same-origin',
        signal: chartAbortController.signal
    })
        .then(function(response) {
            if (!response.ok) {
                return response.text().then(function(text) {
                    throw new Error('HTTP ' + response.status + ': ' + text.substring(0, 100));
                });
            }
            return response.json();
        })
        .then(function(data) {
            if (data.error) {
                throw new Error(data.error);
            }
            // Cache the data
            chartDataCache[cacheKey] = {
                data: data,
                timestamp: Date.now()
            };
            renderChart(symbol, data);
        })
        .catch(function(error) {
            // Ignore abort errors (user moved mouse before request completed)
            if (error.name === 'AbortError') {
                return;
            }
            console.error('Chart fetch error:', error);
            if (chartTooltip) {
                chartTooltip.innerHTML = '<div class="chart-tooltip-loading">Error: ' + escapeHtml(error.message) + '</div>';
            }
        });
}

function renderChart(symbol, data) {
    if (!chartTooltip || !data.ohlcv || data.ohlcv.length === 0) {
        chartTooltip.innerHTML = '<div class="chart-tooltip-loading">No chart data available</div>';
        return;
    }

    // Get asset info for header
    var asset = allAssets[symbol] || {};
    var price = asset.latest_price || data.ohlcv[data.ohlcv.length - 1].close;
    var firstPrice = data.ohlcv[0].close;
    var changePercent = ((price - firstPrice) / firstPrice * 100).toFixed(2);
    var changeClass = changePercent >= 0 ? 'positive' : 'negative';
    var changeSign = changePercent >= 0 ? '+' : '';

    var chartW = 520;
    var chartH = 350;
    var rsiH = 60;
    var hasRsi = data.rsi && data.rsi.length > 0;

    // Minimalist header + chart container + optional RSI pane
    chartTooltip.innerHTML =
        '<div class="chart-tooltip-header">' +
            '<span class="chart-tooltip-symbol">' + escapeHtml(symbol) + '</span>' +
            '<span class="chart-tooltip-price">' +
                formatPrice(price, symbol, asset.type) +
                '<span class="chart-tooltip-change ' + changeClass + '">' + changeSign + changePercent + '%</span>' +
            '</span>' +
            '<span class="chart-tooltip-actions">' +
                '<button class="chart-fullscreen-btn" title="Open fullscreen chart">Open</button>' +
            '</span>' +
        '</div>' +
        '<div id="chart-container" style="width:' + chartW + 'px;height:' + chartH + 'px;"></div>' +
        (hasRsi ? '<div id="rsi-container" style="width:' + chartW + 'px;height:' + rsiH + 'px;border-top:1px solid #21262d;"></div>' : '');

    var fullscreenBtn = chartTooltip.querySelector('.chart-fullscreen-btn');
    if (fullscreenBtn) {
        fullscreenBtn.addEventListener('click', function(ev) {
            ev.stopPropagation();
            openChartModalFullscreen(symbol);
        });
    }

    var container = document.getElementById('chart-container');
    if (!container) return;

    // Check if LightweightCharts is loaded
    if (typeof LightweightCharts === 'undefined') {
        container.innerHTML = '<div class="chart-tooltip-loading">Chart library not loaded</div>';
        return;
    }

    // Chart matching KLineChart dark theme
    chartInstance = LightweightCharts.createChart(container, {
        width: chartW,
        height: chartH,
        layout: {
            background: { type: 'solid', color: '#1b1b1f' },
            textColor: '#929AA5',
            fontSize: 10
        },
        grid: {
            vertLines: { color: '#292929' },
            horzLines: { color: '#292929' }
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: '#4c525e', width: 1, style: 2 },
            horzLine: { color: '#4c525e', width: 1, style: 2 }
        },
        rightPriceScale: {
            borderColor: '#2B2B2F',
            scaleMargins: { top: 0.05, bottom: 0.15 },
            entireTextOnly: true,
            autoScale: true
        },
        timeScale: {
            borderColor: '#2B2B2F',
            timeVisible: false,
            secondsVisible: false,
            rightOffset: 2
        },
        handleScroll: false,
        handleScale: false,
        localization: {
            priceFormatter: function(price) {
                if (price >= 10000) return price.toLocaleString('en-US', {maximumFractionDigits: 0});
                if (price >= 100) return price.toFixed(1);
                if (price >= 1) return price.toFixed(2);
                return price.toFixed(4);
            }
        }
    });

    // Candlestick series — KLineChart colors
    var candleSeries = chartInstance.addCandlestickSeries({
        upColor: '#26a69a',
        downColor: '#ef5350',
        borderVisible: false,
        wickUpColor: '#26a69a',
        wickDownColor: '#ef5350'
    });

    candleSeries.setData(data.ohlcv);

    // Volume overlay at bottom of candle area
    if (data.volume && data.volume.length > 0) {
        var volumeSeries = chartInstance.addHistogramSeries({
            priceFormat: { type: 'volume' },
            priceScaleId: 'volume',
        });
        volumeSeries.priceScale().applyOptions({
            scaleMargins: { top: 0.82, bottom: 0 },
            borderVisible: false
        });
        volumeSeries.setData(data.volume);
    }

    // EMA Ribbon only (no weekly MAs, no squeeze, no consensio)
    if (data.indicators && data.indicators.ema_ribbon && data.indicators.ema_ribbon.fast && data.indicators.ema_ribbon.fast.length > 0) {
        var emaRibbon = data.indicators.ema_ribbon;
        var ribbonFastSeries = chartInstance.addLineSeries({
            color: '#7cb518',
            lineWidth: 2,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            priceLineVisible: false
        });
        ribbonFastSeries.setData(emaRibbon.fast);

        var ribbonSlowSeries = chartInstance.addLineSeries({
            color: '#7cb518',
            lineWidth: 2,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            priceLineVisible: false
        });
        ribbonSlowSeries.setData(emaRibbon.slow);
    }

    // CPR bounded lines (matching KLineChart style - not infinite)
    if (data.cpr && data.cpr.pivot) {
        var cpr = data.cpr;
        var lastTime = data.ohlcv[data.ohlcv.length - 1].time;
        var periodStart = cpr.periodStart || data.ohlcv[Math.max(0, data.ohlcv.length - 30)].time;
        var pivotColor = '#9C27B0';  // Purple
        var rColor = '#00E676';      // Green
        var sColor = '#FF5252';      // Red

        var cprLevels = [
            { val: cpr.tc, color: pivotColor },
            { val: cpr.pivot, color: pivotColor },
            { val: cpr.bc, color: pivotColor },
            { val: cpr.r1, color: rColor },
            { val: cpr.r2, color: rColor },
            { val: cpr.r3, color: rColor },
            { val: cpr.s1, color: sColor },
            { val: cpr.s2, color: sColor },
            { val: cpr.s3, color: sColor }
        ];

        cprLevels.forEach(function(lvl) {
            if (lvl.val == null) return;
            var series = chartInstance.addLineSeries({
                color: lvl.color,
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                lastValueVisible: false,
                crosshairMarkerVisible: false,
                priceLineVisible: false
            });
            series.setData([
                { time: periodStart, value: lvl.val },
                { time: lastTime, value: lvl.val }
            ]);
        });
    }

    // Saved drawings
    addKlineDrawingsToPopupChart(symbol, candleSeries, '1D');

    // Markers
    if (data.markers && data.markers.length > 0) {
        candleSeries.setMarkers(data.markers);
    }

    chartInstance.timeScale().fitContent();

    // RSI sub-chart
    if (hasRsi) {
        var rsiContainer = document.getElementById('rsi-container');
        if (rsiContainer) {
            var rsiChart = LightweightCharts.createChart(rsiContainer, {
                width: chartW,
                height: rsiH,
                layout: {
                    background: { type: 'solid', color: '#1b1b1f' },
                    textColor: '#929AA5',
                    fontSize: 9
                },
                grid: {
                    vertLines: { visible: false },
                    horzLines: { color: '#292929' }
                },
                crosshair: {
                    mode: LightweightCharts.CrosshairMode.Normal,
                    vertLine: { color: '#4c525e', width: 1, style: 2 },
                    horzLine: { color: '#4c525e', width: 1, style: 2 }
                },
                rightPriceScale: {
                    borderColor: '#2B2B2F',
                    scaleMargins: { top: 0.08, bottom: 0.08 }
                },
                timeScale: { visible: false },
                handleScroll: false,
                handleScale: false
            });

            var rsiSeries = rsiChart.addLineSeries({
                color: '#a78bfa',
                lineWidth: 1,
                lastValueVisible: true,
                crosshairMarkerVisible: false,
                priceLineVisible: false
            });
            rsiSeries.setData(data.rsi);

            // RSI 30/70 reference lines
            rsiSeries.createPriceLine({ price: 70, color: '#ef535066', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
            rsiSeries.createPriceLine({ price: 30, color: '#26a69a66', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });

            // Sync time scales
            chartInstance.timeScale().subscribeVisibleLogicalRangeChange(function(range) {
                if (range) rsiChart.timeScale().setVisibleLogicalRange(range);
            });
            rsiChart.timeScale().fitContent();
        }
    }
}

function positionTooltip(event) {
    if (!chartTooltip) return;

    var tooltipWidth = 540;
    var tooltipHeight = 440;
    var margin = 12;

    var x = event.clientX + margin;
    var y = event.clientY - tooltipHeight / 2;

    // Keep within viewport
    if (x + tooltipWidth > window.innerWidth) {
        x = event.clientX - tooltipWidth - margin;
    }
    if (y < margin) {
        y = margin;
    }
    if (y + tooltipHeight > window.innerHeight - margin) {
        y = window.innerHeight - tooltipHeight - margin;
    }

    chartTooltip.style.left = x + 'px';
    chartTooltip.style.top = y + 'px';
}

