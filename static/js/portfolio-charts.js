/**
 * Portfolio Charts — ECharts-based visuals for the /portfolio page.
 * Book-value curve (with cost basis beneath it), allocation donut,
 * and spot/futures + long/short composition bars.
 */
(function() {
'use strict';

var COLORS = {
    green: '#3fb950', red: '#ef5350', blue: '#58a6ff', dim: '#484f58',
    bg: '#161b22', border: '#30363d', text: '#d1d4dc', grid: '#21262d',
};

// Fixed categorical order for the donut's asset slices — never cycled,
// never reassigned when the list length changes. Falls back to a dim
// gray once the palette is exhausted (only "Other" should reach this).
// Orange/aqua/yellow/magenta/violet are the dataviz skill's dark-mode
// categorical steps (references/palette.md), validated against Carrac's
// own dark surface #161b22 with scripts/validate_palette.js — the
// original light-mode hex here (#eb6834, #eda100, #e87ba4) failed the
// OKLCH lightness band on a dark surface. #58a6ff and #3fb950 (blue,
// green) are pre-existing journal-charts.js house colors and also fail
// the same band; left as-is — they are shared with other pages.
var DONUT_PALETTE = [
    COLORS.blue, '#d95926', '#1baf7a', '#c98500',
    '#d55181', COLORS.green, '#9085e9', COLORS.red,
];
var OTHER_COLOR = COLORS.dim;

var MOBILE_WIDTH = 420;

var _charts = {};    // container-id -> echart instance
var _rerender = {};  // container-id -> fn that rebuilds today's option for today's width

function initChart(containerId) {
    if (typeof document === 'undefined' || !document.getElementById) return null;
    var el = document.getElementById(containerId);
    if (!el) return null;
    if (_charts[containerId]) {
        _charts[containerId].dispose();
    }
    var chart = echarts.init(el, null, {renderer: 'canvas'});
    _charts[containerId] = chart;
    return chart;
}

function isMobileWidth(el) {
    var w = (el && el.clientWidth) || (typeof window !== 'undefined' && window.innerWidth) || 0;
    return w > 0 && w < MOBILE_WIDTH;
}

function fmtUsd(v) {
    var abs = Math.abs(v);
    var sign = v < 0 ? '-' : '';
    if (abs >= 1000000) return sign + '$' + (abs / 1000000).toFixed(2) + 'M';
    if (abs >= 1000) return sign + '$' + (abs / 1000).toFixed(1) + 'K';
    return sign + '$' + abs.toFixed(2);
}

// ── Book value curve ──
// The gap between the two lines IS the unrealised P&L, and it must be the
// shaded region — not a fill from the book-value line down to zero, which
// says nothing about cost basis at all and looks identical whether the
// book is up or down.
//
// Mechanism: three invisible-line ECharts series sharing one `stack`, in
// the standard "stacked band" idiom —
//   1. `_pnlBaseline` = min(book, cost) at each point (0-opacity line,
//      0-opacity fill — it only exists to be the stack's floor)
//   2. `_pnlProfit`   = (book - cost) where book >= cost, else 0 — stacked
//      on the baseline, green fill
//   3. `_pnlLoss`     = (cost - book) where book <  cost, else 0 — stacked
//      on the baseline, red fill
// At every index exactly one of profit/loss is nonzero, so the two stacked
// areas never overlap and the visible band is always the true gap, colored
// by sign — including at a mid-series crossing, where the color simply
// hands off from one series to the other at the vertex nearest the flip.
// `Book value` and `Cost basis` are then drawn as plain lines on top,
// carrying identity (which line is which); the band beneath carries state
// (profit vs loss). `yAxis.scale = true` lets the range fit the data
// instead of forcing zero into view — the band's correctness does not
// depend on the axis baseline, and wasting most of the chart on an empty
// region below the real values would hurt legibility.

function buildPnlBand(bookValues, costBasis) {
    var lower = bookValues.map(function(b, i) {
        return b >= costBasis[i] ? costBasis[i] : b;
    });
    var profit = bookValues.map(function(b, i) {
        return b >= costBasis[i] ? b - costBasis[i] : 0;
    });
    var loss = bookValues.map(function(b, i) {
        return b >= costBasis[i] ? 0 : costBasis[i] - b;
    });
    return {lower: lower, profit: profit, loss: loss};
}

function buildCurveOption(curve) {
    curve = curve || [];
    var dates = curve.map(function(d) { return d.date; });
    var bookValues = curve.map(function(d) { return d.book_value; });
    var costBasis = curve.map(function(d) { return d.cost_basis; });
    var band = buildPnlBand(bookValues, costBasis);

    return {
        grid: {top: 20, right: 16, bottom: 32, left: 56, containLabel: false},
        legend: {
            show: true,
            data: ['Book value', 'Cost basis'],
            top: 0, right: 10,
            textStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            itemWidth: 14, itemHeight: 2,
        },
        tooltip: {
            trigger: 'axis',
            backgroundColor: COLORS.bg,
            borderColor: COLORS.border,
            textStyle: {color: COLORS.text, fontFamily: 'JetBrains Mono', fontSize: 11},
            formatter: function(params) {
                if (!params || !params.length) return '';
                var date = params[0].axisValue;
                var book = null, cost = null;
                params.forEach(function(p) {
                    if (p.seriesName === 'Book value') book = p.value;
                    if (p.seriesName === 'Cost basis') cost = p.value;
                });
                var lines = [date];
                if (book != null) lines.push('Book: <b>' + fmtUsd(book) + '</b>');
                if (cost != null) {
                    lines.push('Cost basis: ' + fmtUsd(cost));
                    lines.push('Unrealised: ' + fmtUsd(book - cost));
                }
                return lines.join('<br>');
            }
        },
        xAxis: {
            type: 'category', data: dates, boundaryGap: false,
            axisLabel: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            axisLine: {lineStyle: {color: COLORS.grid}},
            axisTick: {show: false},
        },
        yAxis: {
            type: 'value', scale: true,
            axisLabel: {
                color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono',
                formatter: function(v) { return fmtUsd(v); },
            },
            axisLine: {show: false},
            splitLine: {lineStyle: {color: COLORS.grid, type: 'dashed'}},
        },
        series: [
            {
                name: '_pnlBaseline', type: 'line', data: band.lower,
                stack: 'pnlBand', symbol: 'none', silent: true,
                lineStyle: {opacity: 0}, areaStyle: {opacity: 0},
                tooltip: {show: false}, z: 0,
            },
            {
                name: '_pnlProfit', type: 'line', data: band.profit,
                stack: 'pnlBand', symbol: 'none', silent: true,
                lineStyle: {opacity: 0}, areaStyle: {color: COLORS.green, opacity: 0.18},
                tooltip: {show: false}, z: 1,
            },
            {
                name: '_pnlLoss', type: 'line', data: band.loss,
                stack: 'pnlBand', symbol: 'none', silent: true,
                lineStyle: {opacity: 0}, areaStyle: {color: COLORS.red, opacity: 0.18},
                tooltip: {show: false}, z: 1,
            },
            {
                name: 'Book value',
                type: 'line',
                data: bookValues,
                showSymbol: false,
                lineStyle: {color: COLORS.blue, width: 2},
                z: 3,
            },
            {
                name: 'Cost basis',
                type: 'line',
                data: costBasis,
                showSymbol: false,
                lineStyle: {color: COLORS.dim, width: 1.5, type: 'dashed'},
                z: 2,
            },
        ],
    };
}

// ── Allocation donut ──
// Part-to-whole with a token ceiling: the series-count ladder caps a pie
// at 7-8 slices before identity stops being legible, so anything past the
// top 8 assets folds into a final "Other" slice rather than growing the
// palette. Total renders as the centred hero figure.

function buildDonutOption(byAsset) {
    var rows = (byAsset || []).slice().sort(function(a, b) { return b.value - a.value; });
    var TOP_N = 8;
    var kept = rows.slice(0, TOP_N);
    var rest = rows.slice(TOP_N);

    var data = kept.map(function(r, i) {
        return {
            name: r.asset,
            value: r.value,
            itemStyle: {color: DONUT_PALETTE[i % DONUT_PALETTE.length]},
        };
    });
    if (rest.length) {
        var otherTotal = rest.reduce(function(sum, r) { return sum + r.value; }, 0);
        data.push({name: 'Other', value: otherTotal, itemStyle: {color: OTHER_COLOR}});
    }

    var total = data.reduce(function(sum, d) { return sum + d.value; }, 0);

    return {
        tooltip: {
            trigger: 'item',
            backgroundColor: COLORS.bg,
            borderColor: COLORS.border,
            textStyle: {color: COLORS.text, fontFamily: 'JetBrains Mono', fontSize: 11},
            formatter: function(p) {
                var pct = total ? (p.value / total * 100).toFixed(1) : '0.0';
                return p.name + ': <b>' + fmtUsd(p.value) + '</b> (' + pct + '%)';
            }
        },
        legend: {
            show: true,
            orient: 'vertical',
            right: 0,
            top: 'middle',
            type: 'scroll',
            textStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            itemWidth: 10,
            itemHeight: 10,
        },
        title: {
            text: fmtUsd(total),
            subtext: 'Total',
            left: '32%',
            top: 'middle',
            textAlign: 'center',
            textStyle: {color: COLORS.text, fontSize: 16, fontFamily: 'JetBrains Mono', fontWeight: 600},
            subtextStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
        },
        series: [{
            type: 'pie',
            radius: ['55%', '75%'],
            center: ['32%', '50%'],
            avoidLabelOverlap: true,
            label: {show: false},
            labelLine: {show: false},
            data: data,
        }],
    };
}

function buildMobileDonutOption(opt) {
    // Legend below instead of beside on narrow viewports, and the donut
    // recenters so the hero total stays inside the ring.
    opt.legend.orient = 'horizontal';
    opt.legend.right = 'center';
    opt.legend.left = 'center';
    opt.legend.top = 'bottom';
    opt.legend.type = 'scroll';
    opt.title.left = 'center';
    opt.series[0].center = ['50%', '40%'];
    return opt;
}

// ── Composition bars ──
// Two stacked horizontal bars (spot/futures, long/short) — categorical
// part-to-whole, one bar per split so nothing competes for the same track.

function splitRows(byKind, byDirection) {
    byKind = byKind || {spot: 0, futures: 0};
    byDirection = byDirection || {long: 0, short: 0};
    return [
        {
            label: 'Spot / Futures',
            segments: [
                {name: 'Spot', value: byKind.spot || 0, color: COLORS.blue},
                {name: 'Futures', value: byKind.futures || 0, color: COLORS.dim},
            ],
        },
        {
            label: 'Long / Short',
            segments: [
                {name: 'Long', value: byDirection.long || 0, color: COLORS.green},
                {name: 'Short', value: byDirection.short || 0, color: COLORS.red},
            ],
        },
    ];
}

function buildCompositionOption(byKind, byDirection, mobile) {
    var rows = splitRows(byKind, byDirection);
    var categories = rows.map(function(r) { return r.label; });

    // Each row stacks under its own stack id, so a segment's data array
    // is non-null only at its own row's index — segments from different
    // rows never share a bar.
    var series = [];
    rows.forEach(function(row, rowIdx) {
        row.segments.forEach(function(seg) {
            var data = categories.map(function(_, i) { return i === rowIdx ? seg.value : null; });
            series.push({
                name: seg.name,
                type: 'bar',
                stack: 'row' + rowIdx,
                barMaxWidth: mobile ? 20 : 28,
                itemStyle: {color: seg.color},
                data: data,
                label: {show: false},
            });
        });
    });

    return {
        grid: {top: 20, right: mobile ? 12 : 20, bottom: 20, left: mobile ? 90 : 110, containLabel: false},
        tooltip: {
            trigger: 'item',
            backgroundColor: COLORS.bg,
            borderColor: COLORS.border,
            textStyle: {color: COLORS.text, fontFamily: 'JetBrains Mono', fontSize: 11},
            formatter: function(p) {
                if (p.value == null) return '';
                return p.seriesName + ': <b>' + fmtUsd(p.value) + '</b>';
            }
        },
        legend: {
            show: true,
            top: 0,
            textStyle: {color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono'},
            itemWidth: 10,
            itemHeight: 10,
        },
        xAxis: {
            type: 'value',
            axisLabel: {
                color: COLORS.dim, fontSize: 10, fontFamily: 'JetBrains Mono',
                formatter: function(v) { return fmtUsd(v); },
                show: !mobile,
            },
            axisLine: {show: false},
            splitLine: {lineStyle: {color: COLORS.grid, type: 'dashed'}},
        },
        yAxis: {
            type: 'category',
            data: categories,
            axisLabel: {color: COLORS.text, fontSize: 11, fontFamily: 'JetBrains Mono'},
            axisLine: {lineStyle: {color: COLORS.grid}},
            axisTick: {show: false},
        },
        series: series,
    };
}

// ── Render functions ──

window.renderBookCurve = function(containerId, curve) {
    var chart = initChart(containerId);
    if (!chart) return;
    chart.setOption(buildCurveOption(curve));
};

window.renderAllocationDonut = function(containerId, byAsset) {
    var chart = initChart(containerId);
    if (!chart) return;
    var draw = function() {
        var opt = buildDonutOption(byAsset);
        var el = document.getElementById(containerId);
        if (isMobileWidth(el)) opt = buildMobileDonutOption(opt);
        chart.setOption(opt, true);
    };
    _rerender[containerId] = draw;
    draw();
};

window.renderCompositionBars = function(containerId, byKind, byDirection) {
    var chart = initChart(containerId);
    if (!chart) return;
    var draw = function() {
        var el = document.getElementById(containerId);
        var mobile = isMobileWidth(el);
        chart.setOption(buildCompositionOption(byKind, byDirection, mobile), true);
    };
    _rerender[containerId] = draw;
    draw();
};

// ── Resize handling — window resize, orientation change, visualViewport ──
// A phone rotated inside the installed PWA fires orientationchange (and,
// where supported, a visualViewport resize) without always firing a plain
// window resize — cover all three so no chart is left sized for the old
// orientation. Guarded so a bare vm sandbox without addEventListener never
// throws.

var _resizeTimer;
function scheduleResize() {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function() {
        Object.keys(_charts).forEach(function(id) {
            // Re-run the mobile/desktop branch first — a rotation can
            // cross the width threshold, and resize() alone only rescales
            // the existing option, it does not re-pick legend/bar layout.
            if (_rerender[id]) _rerender[id]();
            if (_charts[id]) _charts[id].resize();
        });
    }, 200);
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('resize', scheduleResize);
    window.addEventListener('orientationchange', scheduleResize);
    if (typeof window.visualViewport !== 'undefined' && window.visualViewport
            && typeof window.visualViewport.addEventListener === 'function') {
        window.visualViewport.addEventListener('resize', scheduleResize);
    }
}

window.PortfolioCharts = {
    buildCurveOption: buildCurveOption,
    buildDonutOption: buildDonutOption,
};

})();
