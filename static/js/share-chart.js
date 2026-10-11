// Public chart-share renderer — draws the FROZEN candles from the snapshot
// (embedded as a non-executable JSON <script> tag) with the already-shipped
// lightweight-charts lib. Frozen, not live: no network, no ongoing data cost.
// Any frozen price-level drawing renders as a horizontal price line (v1
// scope; richer drawing types are deferred).

(function() {
    'use strict';

    function init() {
        var dataEl = document.getElementById('chart-data');
        var container = document.getElementById('lw-chart');
        if (!dataEl || !container) return;
        if (typeof LightweightCharts === 'undefined') return;

        var snap;
        try { snap = JSON.parse(dataEl.textContent || '{}'); } catch (e) { return; }
        var candles = snap.candles || [];
        if (!candles.length) {
            container.innerHTML = '<div style="color:#8b949e;text-align:center;padding:40px 0">Chart data unavailable.</div>';
            return;
        }

        var chart = LightweightCharts.createChart(container, {
            width: container.clientWidth,
            height: 380,
            layout: { background: { color: '#0d1117' }, textColor: '#8b949e' },
            grid: {
                vertLines: { color: 'rgba(48,54,61,0.4)' },
                horzLines: { color: 'rgba(48,54,61,0.4)' }
            },
            rightPriceScale: { borderColor: '#30363d' },
            timeScale: { borderColor: '#30363d', timeVisible: true },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            handleScroll: false,
            handleScale: false
        });

        var series = chart.addCandlestickSeries({
            upColor: '#3fb950', downColor: '#ef5350', borderVisible: false,
            wickUpColor: '#3fb950', wickDownColor: '#ef5350'
        });
        series.setData(candles);

        (snap.drawings || []).forEach(function(dr) {
            var price = parseFloat(dr.price_level);
            if (isNaN(price)) return;
            series.createPriceLine({
                price: price,
                color: dr.color || '#8b949e',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: (dr.label || '').slice(0, 24)
            });
        });

        chart.timeScale().fitContent();
        window.addEventListener('resize', function() {
            chart.applyOptions({ width: container.clientWidth });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
