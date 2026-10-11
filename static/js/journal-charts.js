/**
 * Journal Charts — ECharts-based analytics charts for the journal page.
 * R-multiple histogram, MFE/MAE scatter, and optional equity curve.
 */
(function() {
'use strict';

var COLORS = {
    green: '#3fb950', red: '#ef5350', blue: '#58a6ff', dim: '#484f58',
    bg: '#161b22', border: '#30363d', text: '#d1d4dc', grid: '#21262d',
};

var _charts = {};  // container-id -> echart instance

function initChart(containerId) {
    var el = document.getElementById(containerId);
    if (!el) return null;
    if (_charts[containerId]) {
        _charts[containerId].dispose();
    }
    var chart = echarts.init(el, null, {renderer: 'canvas'});
    _charts[containerId] = chart;
    return chart;
}

// ── R-Multiple Histogram ──

window.renderRHistogram = function(containerId, histogramData, avgR) {
    var chart = initChart(containerId);
    if (!chart || !histogramData || !histogramData.length) return;

    var categories = histogramData.map(function(d) { return d.bucket; });
    var values = histogramData.map(function(d) { return d.count; });
    var barColors = histogramData.map(function(d) {
        return (d.lo != null && d.lo >= 0) ? COLORS.green : COLORS.red;
    });

    chart.setOption({
        grid: {top: 30, right: 20, bottom: 40, left: 40, containLabel: false},
        tooltip: {
            trigger: 'axis',
            backgroundColor: COLORS.bg,
            borderColor: COLORS.border,
            textStyle: {color: COLORS.text, fontFamily: 'JetBrains Mono', fontSize: 11},
            formatter: function(params) {
                var p = params[0];
                return p.name + 'R: <b>' + p.value + '</b> trade' + (p.value !== 1 ? 's' : '');
            }
        },
        xAxis: {
            type: 'category', data: categories,
            axisLabel: {color: COLORS.dim, fontSize: 9, fontFamily: 'JetBrains Mono',
                        rotate: 45, interval: 1},
            axisLine: {lineStyle: {color: COLORS.grid}},
            axisTick: {show: false},
        },
        yAxis: {
            type: 'value',
            axisLabel: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            axisLine: {show: false},
            splitLine: {lineStyle: {color: COLORS.grid, type: 'dashed'}},
            minInterval: 1,
        },
        series: [{
            type: 'bar',
            data: values.map(function(v, i) {
                return {value: v, itemStyle: {color: barColors[i]}};
            }),
            barMaxWidth: 24,
            markLine: avgR != null ? {
                silent: true,
                data: [
                    {xAxis: 0, lineStyle: {color: COLORS.dim, type: 'solid'}},
                    {
                        name: 'Avg ' + avgR.toFixed(2) + 'R',
                        xAxis: avgR >= 0 ? '+' + avgR.toFixed(1) : avgR.toFixed(1),
                        lineStyle: {color: COLORS.blue, type: 'dashed'},
                        label: {
                            formatter: 'Avg: ' + avgR.toFixed(2) + 'R',
                            color: COLORS.blue, fontSize: 10, fontFamily: 'JetBrains Mono',
                        }
                    }
                ],
            } : undefined,
        }],
    });
};


// ── MFE/MAE Scatter Plot ──

window.renderMfeMaeScatter = function(containerId, mfeData) {
    var chart = initChart(containerId);
    if (!chart || !mfeData || !mfeData.length) return;

    var wins = [], losses = [];
    mfeData.forEach(function(d) {
        var point = [d.mae || 0, d.mfe || 0, d.asset, d.rr_actual, d.exit_efficiency];
        if (d.is_win) wins.push(point);
        else losses.push(point);
    });

    // Find axis range
    var allX = mfeData.map(function(d) { return d.mae || 0; });
    var allY = mfeData.map(function(d) { return d.mfe || 0; });
    var maxVal = Math.max(
        Math.abs(Math.min.apply(null, allX.concat(allY))),
        Math.abs(Math.max.apply(null, allX.concat(allY))),
        2
    );
    maxVal = Math.ceil(maxVal) + 0.5;

    chart.setOption({
        grid: {top: 30, right: 30, bottom: 40, left: 50, containLabel: false},
        tooltip: {
            backgroundColor: COLORS.bg,
            borderColor: COLORS.border,
            textStyle: {color: COLORS.text, fontFamily: 'JetBrains Mono', fontSize: 11},
            formatter: function(params) {
                var d = params.data;
                var lines = [d[2] + ' ' + (d[3] >= 0 ? '+' : '') + d[3].toFixed(2) + 'R'];
                lines.push('MAE: ' + d[0].toFixed(2) + 'R  MFE: ' + d[1].toFixed(2) + 'R');
                if (d[4] != null) lines.push('Efficiency: ' + (d[4] * 100).toFixed(0) + '%');
                return lines.join('<br>');
            }
        },
        xAxis: {
            name: 'MAE (R)', nameLocation: 'center', nameGap: 25,
            nameTextStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            min: 0, max: maxVal,
            axisLabel: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            axisLine: {lineStyle: {color: COLORS.grid}},
            splitLine: {lineStyle: {color: COLORS.grid, type: 'dashed'}},
        },
        yAxis: {
            name: 'MFE (R)', nameLocation: 'center', nameGap: 35,
            nameTextStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            min: 0, max: maxVal,
            axisLabel: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            axisLine: {lineStyle: {color: COLORS.grid}},
            splitLine: {lineStyle: {color: COLORS.grid, type: 'dashed'}},
        },
        series: [
            {
                name: 'Wins', type: 'scatter', data: wins,
                symbolSize: 10,
                itemStyle: {color: COLORS.green, opacity: 0.7},
            },
            {
                name: 'Losses', type: 'scatter', data: losses,
                symbolSize: 10,
                itemStyle: {color: COLORS.red, opacity: 0.7},
            },
            {
                // Breakeven diagonal line (MAE = MFE)
                type: 'line', data: [[0, 0], [maxVal, maxVal]],
                lineStyle: {color: COLORS.dim, type: 'dashed', width: 1},
                symbol: 'none', silent: true,
            }
        ],
        legend: {
            data: ['Wins', 'Losses'],
            textStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            top: 0, right: 10,
        },
    });
};


// ── Resize handler ──

var _resizeTimer;
window.addEventListener('resize', function() {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function() {
        Object.keys(_charts).forEach(function(id) {
            if (_charts[id]) _charts[id].resize();
        });
    }, 200);
});

})();
