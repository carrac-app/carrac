/**
 * Intelligence Dashboard — Quant DB visualization.
 * Admin-only, vanilla JS, no external dependencies.
 */

(function () {
'use strict';

// ── Metric ordering (grouped by category) ──────────────────────────
const METRIC_ORDER = [
    // Price (12)
    'BTC','ETH','SOL','SPX','NASDAQ','GOLD','OIL','DXY','TLT','HYG','COPPER','EEM',
    // Macro (12)
    'FED_FUNDS','YIELD_2Y','YIELD_10Y','YIELD_10Y2Y','YIELD_10Y3M',
    'M2','NET_LIQUIDITY','FED_BALANCE','HY_SPREAD','HY_SPREAD_EU','BREAKEVEN_10Y','VIX',
    // Derivatives (6)
    'FUNDING_BTC','FUNDING_ETH','OPTIONS_PC_BTC','DVOL_BTC','RVOL_BTC','RVOL_ETH',
    // Sentiment (8)
    'FEAR_GREED','STABLECOIN_SUPPLY','STABLECOIN_DOM','ALTCOIN_SEASON',
    'GTREND_BUY_BTC','COT_BTC_SPEC','ETF_BTC_FLOW','CROSS_DISPERSION',
    // Factors (3)
    'RISK_APPETITE','LIQUIDITY','CREDIT_STRESS',
];

const CATEGORY_BREAKS = [12, 24, 30, 38]; // price|macro|deriv|sentiment|factors

// Category map for heatmap filter
const METRIC_CATEGORIES = {};
(function() {
    var cats = ['Price','Macro','Derivatives','Sentiment','Factors'];
    var breaks = [0].concat(CATEGORY_BREAKS).concat([METRIC_ORDER.length]);
    for (var c = 0; c < cats.length; c++) {
        for (var i = breaks[c]; i < breaks[c+1]; i++) {
            METRIC_CATEGORIES[METRIC_ORDER[i]] = cats[c];
        }
    }
})();

var _heatmapCategoryFilter = null; // null = show all

const PRESETS = {
    'Risk-off':          'VIX:gt:2,HY_SPREAD:gt:1.5',
    'Euphoria':          'FEAR_GREED:gt:2,FUNDING_BTC:gt:1.5',
    'Liquidity squeeze': 'M2:lt:-1,DXY:gt:1.5',
    'Credit stress':     'HY_SPREAD:gt:2,YIELD_10Y:gt:2',
    'Macro calm':        'VIX:lt:-1,HY_SPREAD:lt:-1',
};

// ── State ───────────────────────────────────────────────────────────
var _tabLoaded = {};
var _snapshotCache = null;
var _zscoresCache = null;
var _corrPairsCache = null;
var _regimeCache = null;
var _heatmapData = null;
var _corrRegime = ''; // '' = all, 'favorable', 'unfavorable'

// ── ECharts Infrastructure ─────────────────────────────────────────
var _chartInstances = {};
var _resizeTimer = null;

if (typeof echarts !== 'undefined') {
    echarts.registerTheme('carrac', {
        color: ['#6bc985', '#26a69a', '#ef5350', '#58a6ff', '#ff9800', '#a78bfa', '#4dd0c4', '#ff7b72'],
        backgroundColor: 'transparent',
        textStyle: { color: '#b0b8c4', fontFamily: "'JetBrains Mono', 'SF Mono', Consolas, monospace", fontSize: 12 },
        title: { textStyle: { color: '#e6edf3' } },
        legend: { textStyle: { color: '#7a8494', fontSize: 11 } },
        tooltip: {
            backgroundColor: 'rgba(36, 43, 53, 0.95)',
            borderColor: '#2a3140',
            textStyle: { color: '#e6edf3', fontSize: 12, fontFamily: "'JetBrains Mono', monospace" },
        },
        categoryAxis: { axisLine: { lineStyle: { color: '#2a3140' } }, splitLine: { lineStyle: { color: '#1e2430' } }, axisLabel: { color: '#7a8494' } },
        valueAxis:    { axisLine: { lineStyle: { color: '#2a3140' } }, splitLine: { lineStyle: { color: '#1e2430' } }, axisLabel: { color: '#7a8494' } },
        radar: { splitArea: { areaStyle: { color: ['transparent'] } }, axisLine: { lineStyle: { color: '#1e2430' } }, splitLine: { lineStyle: { color: '#1e2430' } } },
        graph: { color: ['#26a69a', '#58a6ff', '#a78bfa', '#ff9800'] },
        visualMap: { textStyle: { color: '#7a8494' } },
        dataZoom: { backgroundColor: '#131921', fillerColor: 'rgba(107,201,133,0.15)', borderColor: '#2a3140', handleStyle: { color: '#6bc985' }, textStyle: { color: '#7a8494' } },
    });

    window.addEventListener('resize', function () {
        clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(function () {
            Object.keys(_chartInstances).forEach(function (id) {
                if (_chartInstances[id] && !_chartInstances[id].isDisposed()) {
                    _chartInstances[id].resize();
                }
            });
        }, 200);
    });
}

function initChart(containerId, opts) {
    if (typeof echarts === 'undefined') return null;
    var container = el(containerId);
    if (!container) return null;
    if (_chartInstances[containerId] && !_chartInstances[containerId].isDisposed()) {
        _chartInstances[containerId].dispose();
    }
    var chart = echarts.init(container, 'carrac', opts || {});
    _chartInstances[containerId] = chart;
    return chart;
}

// ── Tab switching ───────────────────────────────────────────────────
function switchTab(tabId) {
    document.querySelectorAll('.tab-pane').forEach(function (p) {
        p.classList.remove('active');
    });
    var pane = document.getElementById('pane-' + tabId);
    if (!pane) return;
    pane.classList.add('active');

    document.querySelectorAll('.int-tab[data-tab]').forEach(function (t) {
        t.classList.toggle('active', t.dataset.tab === tabId);
    });

    history.replaceState(null, '', '#' + tabId);

    if (!_tabLoaded[tabId]) {
        loadTab(tabId);
        _tabLoaded[tabId] = true;
    }
    window.scrollTo({ top: 0 });

    // Move tab sliding indicator + scroll active tab into view (mobile)
    requestAnimationFrame(function() {
        var ind = document.getElementById('tab-indicator');
        var activeBtn = document.querySelector('.int-tab.active');
        if (ind && activeBtn) {
            ind.style.left = activeBtn.offsetLeft + 'px';
            ind.style.width = activeBtn.offsetWidth + 'px';
            activeBtn.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
        }
    });
}

function loadTab(tabId) {
    switch (tabId) {
        case 'overview': loadOverview(); break;
        case 'correlations': loadCorrelations(); break;
        case 'regime': loadRegime(); break;
        case 'conditions': loadConditions(); break;
        case 'metrics': loadMetrics(); break;
        case 'series': loadSeriesTab(); break;
        case 'datalab': loadDataLab(); break;
        case 'chains': loadChains(); break;
        // 'about' is static HTML
    }
}

// ── Init ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    // Populate engine-freshness header pill asap (separate from tab loads)
    getHealth();

    // Asset picker navigation — keep the current tab via hash
    var picker = document.getElementById('intel-asset-picker');
    if (picker) {
        picker.addEventListener('change', function () {
            var next = this.value.toUpperCase();
            if (!next || next === (window.INTEL_ASSET || 'BTC').toUpperCase()) return;
            var hash = location.hash || '';
            location.href = '/intelligence/' + encodeURIComponent(next) + hash;
        });
    }

    // Tab click handlers
    document.querySelectorAll('.int-tab[data-tab]').forEach(function (btn) {
        btn.addEventListener('click', function () { switchTab(this.dataset.tab); });
    });

    // Keyboard shortcuts 1-5
    document.addEventListener('keydown', function (e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
        var tabs = ['overview', 'correlations', 'regime', 'conditions', 'metrics', 'series', 'datalab', 'chains', 'about'];
        var idx = parseInt(e.key) - 1;
        if (idx >= 0 && idx < tabs.length) switchTab(tabs[idx]);
    });

    // Load initial tab from hash or default
    var hash = location.hash.replace('#', '');
    switchTab(hash || 'overview');

    // Position indicator after first render
    requestAnimationFrame(function() {
        var ind = el('tab-indicator');
        var activeBtn = document.querySelector('.int-tab.active');
        if (ind && activeBtn) {
            ind.style.left = activeBtn.offsetLeft + 'px';
            ind.style.width = activeBtn.offsetWidth + 'px';
        }
    });

    // Info icon tap handler (mobile — CSS :hover doesn't work on touch)
    document.addEventListener('click', function(e) {
        var icon = e.target.closest('.info-icon');
        document.querySelectorAll('.info-icon.tip-visible').forEach(function(el) {
            if (el !== icon) el.classList.remove('tip-visible');
        });
        if (icon) {
            icon.classList.toggle('tip-visible');
            // Prevent tooltip from overflowing viewport
            var tip = icon.querySelector('.info-tip');
            if (tip && icon.classList.contains('tip-visible')) {
                var rect = tip.getBoundingClientRect();
                if (rect.left < 8) tip.style.transform = 'translateX(' + (-rect.left + 8) + 'px)';
                else if (rect.right > window.innerWidth - 8) tip.style.transform = 'translateX(' + (window.innerWidth - 8 - rect.right) + 'px)';
                else tip.style.transform = 'translateX(-50%)';
            }
        }
    });

    // Explainer panel toggle (delegated)
    document.addEventListener('click', function(e) {
        var btn = e.target.closest('.explainer-btn');
        if (!btn) return;
        var key = btn.dataset.explainer;
        var panel = document.getElementById('explainer-' + key);
        if (!panel) return;
        btn.classList.toggle('active');
        panel.classList.toggle('open');
    });
});


// ── API fetch helper ────────────────────────────────────────────────
function apiFetch(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    });
}

// ── Freshness helpers ───────────────────────────────────────────────
// Health payload is reused across tabs; fetch once and cache.
var _healthCache = null;
var _healthPromise = null;
function getHealth() {
    if (_healthCache) return Promise.resolve(_healthCache);
    if (_healthPromise) return _healthPromise;
    _healthPromise = apiFetch('/api/quant/health').then(function(h) {
        _healthCache = h;
        updateEngineFreshnessBadge(h);
        return h;
    }).catch(function() { return null; });
    return _healthPromise;
}

// Format the header pill: "Last engine run: 3h ago" with tier class.
function updateEngineFreshnessBadge(health) {
    var node = document.getElementById('engine-freshness');
    if (!node || !health || !Array.isArray(health.collectors)) return;
    var latest = 0;
    health.collectors.forEach(function(c) {
        if (c.status !== 'success' || !c.finished_at) return;
        var t = new Date(c.finished_at).getTime();
        if (t > latest) latest = t;
    });
    if (!latest) { node.textContent = 'Engine status unknown'; return; }
    var ageH = (Date.now() - latest) / (1000 * 60 * 60);
    var label;
    if (ageH < 1) label = Math.round(ageH * 60) + 'm ago';
    else if (ageH < 48) label = Math.round(ageH) + 'h ago';
    else label = Math.round(ageH / 24) + 'd ago';
    node.textContent = 'Engine ran ' + label;
    node.classList.remove('fresh', 'stale', 'broken');
    if (ageH < 30) node.classList.add('fresh');
    else if (ageH < 72) node.classList.add('stale');
    else node.classList.add('broken');
}

function el(id) { return document.getElementById(id); }

function setHTML(id, html) {
    var node = el(id);
    if (node) node.innerHTML = html;
}

function showLoading(id) {
    var skeletons = {
        'overview-content':
            '<div class="skeleton skeleton-banner"></div>' +
            '<div class="two-col"><div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div></div>' +
            '<div class="skeleton skeleton-card-sm"></div>',
        'heatmap-container':
            '<div class="skeleton skeleton-card" style="height:400px"></div>',
        'regime-content':
            '<div class="skeleton skeleton-banner"></div>' +
            '<div class="two-col"><div class="skeleton skeleton-card-sm"></div><div class="skeleton skeleton-card-sm"></div></div>' +
            '<div class="skeleton skeleton-card-sm"></div>',
        'condition-analogues':
            '<div class="skeleton skeleton-card-sm" style="margin-bottom:8px"></div>' +
            '<div class="skeleton skeleton-card"></div>',
    };
    setHTML(id, skeletons[id] || '<div class="loading-spinner">Loading...</div>');
}

function showError(id, msg) {
    setHTML(id, '<div class="error-msg">Error: ' + escapeHtml(msg) + '</div>');
}


function infoIcon(tip) {
    return '<span class="info-icon">i<span class="info-tip">' + tip + '</span></span>';
}

// Section tooltip descriptions. '{asset}' is substituted with the current
// focus asset (window.INTEL_ASSET) via sectionTip() at render time.
// References to BTC that are NOT about the focus asset (HMM training data,
// heatmap example BTC↔SPX) are left alone on purpose.
var SECTION_TIPS = {
    regime: 'HMM classifies the market into favorable (higher BTC returns, low VIX) or unfavorable (lower returns, high VIX) regimes. Probability shows model confidence. Duration counts consecutive days in the current state.',
    anomalyCluster: 'An anomaly cluster triggers when 3+ metrics across 2+ domains have |z-score| > 2 simultaneously. More extreme metrics and more domains affected = stronger signal.',
    corrTop: 'Spearman rank correlations between {asset} and other metrics over a 60-day rolling window. Positive = moves together, negative = moves inversely. Only pairs with |r| >= 0.15 shown.',
    leadlag: 'Cross-correlations at different time lags. A positive lag means the leader metric today correlates with {asset} N days later. Negative lag means {asset} moves first. Most crypto lead-lag is reactive (sentiment follows price), not predictive.',
    analogues: 'Cosine similarity of today\'s 9-metric z-score vector vs every historical date (excluding last 90d). Higher similarity = more similar macro environment. Forward returns show what actually happened after each analogue date.',
    heatmap: 'Spearman rank correlations between all 23 canonical metrics. Green = positive correlation, red = negative. Click a cell for details. Category breaks separate price/macro/derivatives/sentiment.',
    zscores: 'How many standard deviations each metric is from its 90-day rolling mean. Bars extend right for positive z-scores, left for negative. |z| > 2 is considered extreme.',
    condQuery: 'Define z-score conditions and see what happened historically when they held. Uses 90-day z-scores. Presets offer common macro scenarios. Results show forward return distribution for the target asset.',
    condAnalogues: 'Top 5 historical dates most similar to today\'s macro environment, based on cosine similarity of z-score vectors across VIX, HY spread, yields, M2, Fed Funds, sentiment, and funding.',
    chains: 'Transmission chains show how macro signals propagate to {asset}. Chain strength is the geometric mean of |r| at each link. The weakest link identifies where the signal breaks down.',
    factors: 'PCA-derived composite factors combine correlated metrics into a single signal. Risk Appetite (VIX, HY spread, TLT, DXY), Liquidity (M2, Net Liquidity, Fed Balance), Credit Stress (HY spreads, yield curve).',
};

// Returns the tip with {asset} substituted to the current focus asset.
function sectionTip(key) {
    var asset = (window.INTEL_ASSET || 'BTC').toUpperCase();
    var raw = SECTION_TIPS[key] || '';
    return raw.replace(/\{asset\}/g, asset);
}

// ── Explainer panel content ────────────────────────────────────────

var EXPLAINERS = {
    regime:
        '<h4>What is this?</h4>' +
        '<p>The Hidden Markov Model classifies each day into one of two regimes based on BTC returns, VIX, and HY spread:</p>' +
        '<ul><li><strong>Favorable</strong>: the state the model fitted to higher average BTC returns, lower volatility and tighter credit spreads</li>' +
        '<li><strong>Unfavorable</strong>: the state fitted to lower average BTC returns, a higher VIX and wider spreads</li></ul>' +
        '<h4>How to interpret</h4>' +
        '<p>Probability shows model confidence. Above 80% = strong conviction. Duration shows persistence \u2014 longer regimes tend to continue.</p>' +
        '<h4>What it does not say</h4>' +
        '<p>The regime describes the backdrop the model sees. It is not a signal to buy, sell or change position size; what to do with it is your call.</p>' +
        '<div class="caveat">Caveat: Regime is identified in hindsight and can flip suddenly. It is context, not a signal.</div>',
    chains:
        '<h4>What is this?</h4>' +
        '<p>Transmission chains trace how macro variables propagate through to {asset}. Each link shows the Spearman correlation (r) and time lag between adjacent variables.</p>' +
        '<h4>How to read the diagram</h4>' +
        '<ul><li><strong>Green links</strong> (|r| > 0.3): Strong signal propagation</li>' +
        '<li><strong>Yellow links</strong> (0.15\u20130.3): Moderate \u2014 signal degrades here</li>' +
        '<li><strong>Red links</strong> (&lt; 0.15): Weak \u2014 chain effectively broken at this point</li>' +
        '<li><strong>Solid lines</strong>: Persistent across sub-periods (reliable)</li>' +
        '<li><strong>Dashed lines</strong>: Unstable \u2014 may not hold in different conditions</li></ul>' +
        '<h4>Chain strength</h4>' +
        '<p>Geometric mean of |r| across all links. A chain is only as strong as its weakest link. Higher = more reliable transmission.</p>' +
        '<div class="caveat">Caveat: Correlations \u2260 causation. Chains describe statistical relationships, not guaranteed mechanisms.</div>',
    heatmap:
        '<h4>What is this?</h4>' +
        '<p>Spearman rank correlations between all tracked metrics over a rolling window (60d or 120d). Green = positive (move together), red = negative (move inversely).</p>' +
        '<h4>How to interpret</h4>' +
        '<ul><li>Diagonal = 1.0 (self-correlation)</li>' +
        '<li>Category breaks separate Price / Macro / Derivatives / Sentiment / Factors</li>' +
        '<li>Use the filter pills to focus on specific categories</li></ul>' +
        '<h4>What to watch for</h4>' +
        '<p>BTC-SPX correlation rising \u2192 crypto trading as risk asset. BTC-GOLD rising \u2192 crypto trading as safe haven. These regimes alternate and signal structural shifts.</p>' +
        '<div class="caveat">Caveat: 60-day window is backward-looking. Correlations during crises differ from calm periods.</div>',
    leadlag:
        '<h4>What is this?</h4>' +
        '<p>Cross-correlations at different time lags identify which variables move before {asset}.</p>' +
        '<h4>Reading the table</h4>' +
        '<ul><li><strong>\u2192 N days</strong>: Leader moves first, {asset} follows N days later</li>' +
        '<li><strong>\u2190 N days</strong>: {asset} moves first (leader lags)</li>' +
        '<li><strong>Positive r</strong>: Both move in same direction at that lag</li>' +
        '<li><strong>Negative r</strong>: Inverse relationship at that lag</li></ul>' +
        '<h4>Daily vs Weekly</h4>' +
        '<p>Weekly aggregation smooths daily noise and often produces stronger macro correlations. Toggle between them to compare.</p>' +
        '<div class="caveat">Caveat: Most lead-lag signals are weak (r &lt; 0.3). True predictive leads are rare and unstable across regimes.</div>',
    factors:
        '<h4>What is this?</h4>' +
        '<p>PCA (Principal Component Analysis) extracts composite factors from groups of related metrics:</p>' +
        '<ul><li><strong>Risk Appetite</strong>: VIX + HY spread + TLT + DXY. Positive = risk-on, negative = risk-off</li>' +
        '<li><strong>Liquidity</strong>: M2 + Net Liquidity + Fed Balance. Positive = expanding, negative = tightening</li>' +
        '<li><strong>Credit Stress</strong>: HY spread + EU HY spread + yield curve. Positive = stress rising, negative = calm</li></ul>' +
        '<h4>Why factors instead of individual metrics?</h4>' +
        '<p>Individual metrics are noisy. A composite factor captures the dominant shared variance from 3\u20134 correlated inputs in one number, reducing noise and producing stronger correlations.</p>' +
        '<div class="caveat">Caveat: PCA loadings can shift over time. Factor interpretation is based on the training period.</div>',
};

function explainerBtn(key) {
    return '<button class="explainer-btn" data-explainer="' + key + '" title="What is this?">?</button>';
}

// Returns EXPLAINERS[key] with {asset} substituted to the current focus asset.
function explainerHtml(key) {
    var asset = (window.INTEL_ASSET || 'BTC').toUpperCase();
    return (EXPLAINERS[key] || '').replace(/\{asset\}/g, asset);
}

// ── SVG components ─────────────────────────────────────────────────

function progressRing(pct, size, strokeW, color, label) {
    var r = (size - strokeW) / 2;
    var circ = 2 * Math.PI * r;
    var offset = circ * (1 - pct);
    return '<svg width="' + size + '" height="' + size + '" style="vertical-align:middle">' +
        '<circle cx="' + size/2 + '" cy="' + size/2 + '" r="' + r + '" ' +
            'fill="none" stroke="var(--bg-elevated)" stroke-width="' + strokeW + '"/>' +
        '<circle cx="' + size/2 + '" cy="' + size/2 + '" r="' + r + '" ' +
            'fill="none" stroke="' + color + '" stroke-width="' + strokeW + '" ' +
            'stroke-dasharray="' + circ.toFixed(1) + '" stroke-dashoffset="' + offset.toFixed(1) + '" ' +
            'stroke-linecap="round" transform="rotate(-90 ' + size/2 + ' ' + size/2 + ')" ' +
            'style="transition:stroke-dashoffset 0.8s ease"/>' +
        '<text x="' + size/2 + '" y="' + size/2 + '" text-anchor="middle" ' +
            'dominant-baseline="central" fill="' + color + '" font-size="' + Math.round(size * 0.22) + '" ' +
            'font-family="var(--font-ui)" font-weight="700">' + label + '</text>' +
    '</svg>';
}

// radarChart() — removed, replaced by ECharts initOverviewRadar()

// sparkline() — removed, replaced by ECharts initMiniSparkline() and initRegimeHistory()


// ═══════════════════════════════════════════════════════════════════
// TAB 1: OVERVIEW
// ═══════════════════════════════════════════════════════════════════

function buildNLSummary(snap, analogues) {
    var bullets = [];

    // 1. Regime state
    if (snap.regime) {
        var r = snap.regime;
        var conf = Math.round(r.probability * 100);
        var desc = r.descriptions && r.descriptions[r.state] ? ' — ' + r.descriptions[r.state] : '';
        var cls = r.state === 'favorable' ? 'nl-bullish' : 'nl-bearish';
        bullets.push('Market is in a <span class="' + cls + '">' +
            escapeHtml(r.state.toUpperCase()) + '</span> regime (' +
            conf + '% confidence, ' + r.duration_days + ' days)' + escapeHtml(desc));
    }

    // 2. Anomaly cluster
    if (snap.anomaly_cluster && snap.anomaly_cluster.extreme_count) {
        var ac = snap.anomaly_cluster;
        var top3 = (ac.metrics || []).slice(0, 3);
        var mList = top3.map(function (m) {
            return escapeHtml(m.metric || m) + ' (' + fmtZ(m.zscore) + '\u03C3)';
        }).join(', ');
        var suffix = ac.domains_affected >= 2 ? ' — multi-domain stress cluster' : '';
        bullets.push(ac.extreme_count + ' extreme metrics: ' + mList + suffix);
    }

    // 3. Strongest BTC correlation
    if (snap.top_correlations && snap.top_correlations.length) {
        var tc = snap.top_correlations[0];
        var corrSign = tc.correlation > 0 ? 'coupled to' : 'inversely correlated with';
        bullets.push('Strongest BTC correlation: <strong>' + escapeHtml(tc.metric) +
            '</strong> (' + fmtR(tc.correlation) + ', 60d) — crypto is ' + corrSign + ' ' +
            escapeHtml(tc.metric.toLowerCase()));
    }

    // 4. Lead-lag highlight — prefer signals with a projected_move_z
    if (snap.leadlag_signals && snap.leadlag_signals.length) {
        var focusAssetLL = (snap.asset || window.INTEL_ASSET || 'BTC').toUpperCase();
        // Pick the signal with the strongest projection if any has one, else fall back to highest |r|
        var projected = snap.leadlag_signals.filter(function(s) { return s.projected_move_z != null; });
        var ll = projected.length
            ? projected.reduce(function(a, b) { return Math.abs(a.projected_move_z) > Math.abs(b.projected_move_z) ? a : b; })
            : snap.leadlag_signals[0];
        var dir = ll.lag_days > 0
            ? escapeHtml(ll.leader) + ' leads ' + focusAssetLL + ' by ' + ll.lag_days + ' days'
            : focusAssetLL + ' leads ' + escapeHtml(ll.leader) + ' by ' + Math.abs(ll.lag_days) + ' days';
        var core = dir + ' (r=' + fmtR(ll.correlation) + ')';
        if (ll.projected_move_z != null) {
            var sign = ll.projected_move_z > 0 ? '+' : '';
            var proj = ' \u2192 ' + focusAssetLL + ' projected ' + sign + ll.projected_move_z.toFixed(2) + '\u03C3';
            if (ll.lag_days > 0) proj += ' over the next ' + ll.lag_days + 'd';
            if (ll.projected_ci_z) {
                proj += ' (95% CI ' + ll.projected_ci_z[0] + ' to ' + ll.projected_ci_z[1] + ')';
            }
            core += proj;
        } else if (ll.correlation < 0 && ll.lag_days > 0) {
            core += ' — recent ' + escapeHtml(ll.leader) + ' strength \u2192 ' + focusAssetLL + ' headwinds in ~' + ll.lag_days + 'd';
        } else if (ll.correlation > 0 && ll.lag_days > 0) {
            core += ' — recent ' + escapeHtml(ll.leader) + ' moves may echo into ' + focusAssetLL + ' in ~' + ll.lag_days + 'd';
        }
        bullets.push(core);
    }

    // 5. Closest analogue
    if (analogues && analogues.analogues && analogues.analogues.length) {
        var a = analogues.analogues[0];
        var sim = (a.similarity * 100).toFixed(0);
        var fwd = a.forward_return || {};
        var ret30 = fwd['30d'];
        var retStr = ret30 != null
            ? ' — BTC ' + (ret30 >= 0 ? 'gained' : 'fell') + ' ' + fmtPct(ret30) + ' over 30d'
            : '';
        bullets.push('Closest analogue: <strong>' + a.date + '</strong> (' + sim + '% similarity)' + retStr);
    }

    if (!bullets.length) return '';

    var html = '<div class="macro-card nl-summary"><h3>Market Summary</h3><ul class="nl-bullets">';
    bullets.forEach(function (b) {
        html += '<li>' + b + '</li>';
    });
    html += '</ul></div>';
    return html;
}

// Sparkline data extracted during HTML build, rendered after setHTML
var _overviewSparkData = { funding: null, etf: null };

function buildSparklineCards(fundingSeries, etfSeries, zscores) {
    _overviewSparkData = { funding: null, etf: null };

    // Look up ETF_BTC_FLOW percentile from zscores (90d window, already fetched)
    var etfPercentile = null;
    if (zscores && zscores.data) {
        zscores.data.forEach(function(z) {
            if (z.metric === 'ETF_BTC_FLOW' && z.percentile != null) {
                etfPercentile = z.percentile;
            }
        });
    }

    var html = '<div class="two-col">';

    // Funding Rate card
    html += '<div class="macro-card"><h3>BTC Funding Rate (90d) ' +
        infoIcon('Perpetual futures funding rate. Near zero = neutral. Extreme positive (>0.05%) = overleveraged longs. Negative = shorts paying longs.') + '</h3>';
    if (fundingSeries && fundingSeries.data && fundingSeries.data.length > 1) {
        var fData = fundingSeries.data.slice().reverse();
        var fValues = fData.map(function(d) { return parseFloat(d.funding_rate_avg || 0); }).filter(function(v) { return !isNaN(v); });
        if (fValues.length > 1) {
            var latest = fValues[fValues.length - 1];
            var color = Math.abs(latest) <= 0.0003 ? '#26a69a' : '#ef5350';
            var fmtVal = (latest * 100).toFixed(4) + '%';
            _overviewSparkData.funding = { values: fValues, color: color };
            html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">' +
                '<span style="font-size:1.2rem;font-weight:700;color:' + color + '">' + fmtVal + '</span>' +
                '<div id="echart-spark-funding" style="display:inline-block;width:160px;height:36px"></div>' +
            '</div>' +
            '<div style="font-size:0.65rem;color:var(--text-muted)">' + fData[fData.length - 1].ts + '</div>';
        } else {
            html += '<p class="text-muted">Insufficient data</p>';
        }
    } else {
        html += '<p class="text-muted">No funding data</p>';
    }
    html += '</div>';

    // ETF Flow card
    html += '<div class="macro-card"><h3>BTC ETF Net Flows (90d) ' +
        infoIcon('Net BTC ETF inflows/outflows in USD. Green = net buying (inflows). Red = net selling (outflows). Strong signal for institutional demand.') + '</h3>';
    if (etfSeries && etfSeries.data && etfSeries.data.length > 1) {
        var eData = etfSeries.data.slice().reverse();
        var eValues = eData.map(function(d) { return parseFloat(d.value || 0); }).filter(function(v) { return !isNaN(v); });
        if (eValues.length > 1) {
            var latestFlow = eValues[eValues.length - 1];
            var flowColor = latestFlow >= 0 ? '#26a69a' : '#ef5350';
            var flowFmt = latestFlow >= 0 ? '+' : '';
            if (Math.abs(latestFlow) >= 1e9) {
                flowFmt += (latestFlow / 1e9).toFixed(2) + 'B';
            } else if (Math.abs(latestFlow) >= 1e6) {
                flowFmt += (latestFlow / 1e6).toFixed(1) + 'M';
            } else {
                flowFmt += latestFlow.toFixed(0);
            }
            var etfPctStr = etfPercentile != null
                ? ' <span style="font-size:0.7rem;color:var(--text-muted);font-weight:400">(' + Math.round(etfPercentile) + 'th pctl)</span>'
                : '';
            _overviewSparkData.etf = { values: eValues, color: flowColor };
            html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">' +
                '<span style="font-size:1.2rem;font-weight:700;color:' + flowColor + '">' + flowFmt + etfPctStr + '</span>' +
                '<div id="echart-spark-etf" style="display:inline-block;width:160px;height:36px"></div>' +
            '</div>' +
            '<div style="font-size:0.65rem;color:var(--text-muted)">' + eData[eData.length - 1].ts + '</div>';
        } else {
            html += '<p class="text-muted">Insufficient data</p>';
        }
    } else {
        html += '<p class="text-muted">No ETF flow data</p>';
    }
    html += '</div>';

    html += '</div>'; // end two-col
    return html;
}


// buildAnomalyTimeline() — removed, replaced by ECharts initAnomalyTimeline()


function loadOverview() {
    showLoading('overview-content');

    var asset = (window.INTEL_ASSET || 'BTC').toUpperCase();
    // Derivatives series keyed by asset where data exists (BTC/ETH); fall back to BTC otherwise.
    var derivAsset = (asset === 'ETH') ? 'ETH' : 'BTC';
    Promise.all([
        apiFetch('/api/quant/snapshot?asset=' + asset),
        apiFetch('/api/quant/analogues?target=' + asset + '&top=5'),
        apiFetch('/api/quant/health'),
        apiFetch('/api/quant/zscores?window=90&min_abs=0'),
        apiFetch('/api/quant/events'),
        apiFetch('/api/quant/series?domain=derivatives&asset=' + derivAsset + '&limit=90'),
        apiFetch('/api/quant/series?domain=onchain&metric=ETF_BTC_NET_FLOW&limit=90'),
        apiFetch('/api/quant/anomalies?limit=20'),
    ]).then(function (results) {
        _snapshotCache = results[0];
        var analogues = results[1];
        var health = results[2];
        _zscoresCache = results[3];
        var events = results[4];
        var fundingSeries = results[5];
        var etfSeries = results[6];
        var anomalyHistory = results[7];

        // leadlag_signals carry `persistent` directly from the snapshot payload
        renderOverview(_snapshotCache, analogues, health, _zscoresCache, events, fundingSeries, etfSeries, anomalyHistory);
    }).catch(function (err) {
        showError('overview-content', err.message);
    });
}

function renderOverview(snap, analogues, health, zscores, events, fundingSeries, etfSeries, anomalyHistory) {
    var html = '';

    // 0. Natural language summary
    html += buildNLSummary(snap, analogues);

    // Cache + populate header pill once health is known (may have been set earlier by getHealth)
    if (health) { _healthCache = health; updateEngineFreshnessBadge(health); }

    // 0b. Data freshness banner — domain-level chips for anything >48h old.
    // Broken/stale distinction: 48-72h = stale, >72h = broken.
    if (health && health.freshness) {
        var chips = [];
        var broken = false;
        var now = Date.now();
        health.freshness.forEach(function(f) {
            if (!f.latest) return;
            var age = (now - new Date(f.latest).getTime()) / (1000 * 60 * 60);
            if (age < 48) return;
            if (age > 72) broken = true;
            var days = Math.round(age / 24);
            var cls = age > 72 ? 'broken' : 'stale';
            chips.push('<span class="freshness-chip ' + cls + '" title="' + f.domain + ' last updated ' + f.latest + '">' + f.domain + ' ' + days + 'd old</span>');
        });
        if (chips.length) {
            var tone = broken ? 'rgba(239,83,80,0.08)' : 'rgba(255,152,0,0.08)';
            var border = broken ? 'rgba(239,83,80,0.3)' : 'rgba(255,152,0,0.35)';
            html += '<div style="background:' + tone + ';border:1px solid ' + border + ';border-radius:var(--radius-sm);padding:8px 14px;margin-bottom:12px;font-size:0.75rem;display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
                '<span style="color:var(--text-tertiary)">Some data is behind cadence:</span>' + chips.join('') + '</div>';
        }
    }

    // 1. Regime banner with progress ring
    if (snap.regime) {
        var r = snap.regime;
        var cls = r.state === 'favorable' ? 'regime-favorable' : 'regime-unfavorable';
        var ringColor = r.state === 'favorable' ? 'var(--color-bullish-bright)' : 'var(--color-bearish-bright)';
        var desc = r.descriptions && r.descriptions[r.state] ? r.descriptions[r.state] : '';
        // Narrative: contextualize duration using historical_pct (state-share of history).
        // A favorable regime at 14d when most favorable runs are longer → "early". At 60d → "mature".
        var narrative = '';
        if (snap.regime_hist_median_days && snap.regime_hist_median_days[r.state]) {
            var med = snap.regime_hist_median_days[r.state];
            var ratio = r.duration_days / med;
            var phase = ratio < 0.3 ? 'early in' : ratio < 0.8 ? 'mid-run of' : ratio < 1.3 ? 'mature in' : 'late-stage of';
            narrative = 'We are ' + phase + ' this ' + escapeHtml(r.state) + ' regime — ' + r.duration_days + 'd elapsed vs historical median ~' + med + 'd.';
        }
        html += '<div class="regime-banner ' + cls + '">' +
            '<div class="regime-banner-main">' +
                progressRing(r.probability, 48, 4, ringColor, Math.round(r.probability * 100) + '%') +
                '<span class="regime-label">' + escapeHtml(r.state.toUpperCase()) + '</span>' +
                infoIcon(SECTION_TIPS.regime) +
                '<span class="regime-duration">' + r.duration_days + ' days since ' + r.since + '</span>' +
                '<button class="alert-btn" id="regime-alert-btn" title="Notify me on regime change">\u2301 Alert on change</button>' +
            '</div>' +
            (desc ? '<div class="regime-banner-desc">' + escapeHtml(desc) + '</div>' : '') +
            (narrative ? '<div class="widget-narrative">' + narrative + '</div>' : '') +
        '</div>';
    }

    // 2. Anomaly cluster alert
    if (snap.anomaly_cluster) {
        var ac = snap.anomaly_cluster;
        var severity = ac.extreme_count >= 8 ? 'cluster-red' : 'cluster-yellow';
        html += '<div class="anomaly-cluster ' + severity + '">' +
            '<div class="cluster-title">' + ac.extreme_count + ' extreme metrics across ' +
            ac.domains_affected + ' domains ' + infoIcon(SECTION_TIPS.anomalyCluster) +
            ' <button class="alert-btn" id="anomaly-alert-btn" title="Notify me when a cluster activates">\u2301 Alert on cluster</button>' +
            '</div>' +
            '<div class="cluster-metrics">';
        var top5 = (ac.metrics || []).slice(0, 5);
        top5.forEach(function (m) {
            html += '<span class="cluster-tag">' + escapeHtml(m.metric || m) +
                (m.zscore ? ' (' + fmtZ(m.zscore) + ')' : '') + '</span>';
        });
        html += '</div></div>';
    }

    // 2b. Full anomalies table (all extreme z-scores)
    if (snap.anomalies && snap.anomalies.length > 0) {
        html += '<div class="macro-card"><h3>Active Anomalies (' + snap.anomaly_count + ') ' +
            infoIcon('All metrics with |z-score| >= 2 on the latest date. Sorted by extremeness.') + '</h3>';
        html += '<table class="data-table"><thead><tr><th>Domain</th><th>Metric</th><th>Z-Score</th><th>Pctl</th><th>Value</th></tr></thead><tbody>';
        snap.anomalies.forEach(function(a) {
            var zColor = a.zscore > 0 ? 'var(--color-bearish)' : 'var(--color-bullish)';
            var pctl = a.percentile != null ? a.percentile.toFixed(0) + '%' : '-';
            html += '<tr>' +
                '<td><span style="color:var(--text-muted);font-size:0.65rem;text-transform:uppercase">' + a.domain + '</span></td>' +
                '<td>' + a.metric + (a.asset !== '_GLOBAL_' ? ':' + a.asset : '') + '</td>' +
                '<td style="color:' + zColor + ';font-weight:600">' + fmtZ(a.zscore) + '</td>' +
                '<td>' + pctl + '</td>' +
                '<td>' + (a.value != null ? parseFloat(a.value).toFixed(2) : '-') + '</td>' +
            '</tr>';
        });
        html += '</tbody></table></div>';
    }

    // 3. Two-column: correlations + lead-lag
    html += '<div class="two-col">';

    // Left: Top correlations for focus asset
    var focusAsset = (snap.asset || window.INTEL_ASSET || 'BTC').toUpperCase();
    html += '<div class="macro-card"><h3>Top ' + focusAsset + ' Correlations (60d) ' + infoIcon(sectionTip('corrTop')) + '</h3>';
    if (snap.top_correlations && snap.top_correlations.length) {
        html += '<div class="corr-list">';
        snap.top_correlations.forEach(function (c) {
            var pct = Math.abs(c.correlation) * 100;
            var color = c.correlation > 0 ? 'var(--color-bullish)' : 'var(--color-bearish)';
            var stabBadge = '';
            if (c.stability != null) {
                var stabCls = c.stability < 0.12 ? 'badge-green' : c.stability < 0.20 ? 'badge-yellow' : 'badge-red';
                var stabLabel = c.stability < 0.12 ? 'stable' : c.stability < 0.20 ? 'moderate' : 'unstable';
                stabBadge = ' <span class="badge ' + stabCls + '" style="font-size:0.55rem;padding:1px 4px">' + stabLabel + '</span>';
            }
            html += '<div class="corr-row">' +
                '<span class="corr-metric">' + c.metric + stabBadge + '</span>' +
                '<div class="corr-bar-bg"><div class="corr-bar" style="width:' + pct + '%;background:' + color + '"></div></div>' +
                '<span class="corr-val" style="color:' + color + '">' + fmtR(c.correlation) + '</span>' +
            '</div>';
        });
        html += '</div>';
    } else {
        html += '<p class="text-muted">No significant correlations</p>';
    }
    html += '</div>';

    // Right: Lead-lag signals (with daily/weekly toggle)
    html += '<div class="macro-card"><h3>Lead-Lag Signals &rarr; ' + focusAsset + ' ' +
        '<span id="ll-freq-toggle" data-freq="daily" style="' +
        'font-size:0.6rem;background:var(--bg-elevated);border:1px solid var(--border-default);' +
        'color:var(--text-muted);padding:2px 8px;border-radius:9999px;cursor:pointer;' +
        'font-weight:500;margin-left:8px;vertical-align:middle">daily</span> ' +
        infoIcon(sectionTip('leadlag')) + explainerBtn('leadlag-ov') + '</h3>';
    html += '<div class="explainer-panel" id="explainer-leadlag-ov">' + explainerHtml('leadlag') + '</div>';
    html += '<div id="leadlag-table-container">' + buildLeadLagTable(snap.leadlag_signals) + '</div>';
    // Narrative: surface the strongest projection (if any has one) as a one-liner.
    var llNarr = buildLeadLagNarrative(snap.leadlag_signals, focusAsset);
    if (llNarr) html += '<div class="widget-narrative">' + llNarr + '</div>';
    html += '</div>';

    html += '</div>'; // end two-col

    // 3a-bis. Transmission Chain Summary
    if (snap.chains && snap.chains.length) {
        html += '<div class="macro-card"><h3>Transmission Chains ' +
            infoIcon(sectionTip('chains')) + explainerBtn('chains-ov') + '</h3>';
        html += '<div class="explainer-panel" id="explainer-chains-ov">' + explainerHtml('chains') + '</div>';
        snap.chains.forEach(function(ch) {
            var pct = Math.min(ch.chain_strength * 100 / 0.5, 100);
            var color = ch.chain_strength > 0.3 ? 'var(--color-bullish)' : ch.chain_strength > 0.15 ? '#ff9800' : 'var(--color-bearish)';
            var wk = ch.weakest_link || {};
            var chainLabel = ch.chain.replace(/_/g, ' ');
            html += '<div class="chain-jump-row" style="display:flex;align-items:center;gap:10px;margin-bottom:8px;cursor:pointer">' +
                '<span style="min-width:120px;font-size:0.7rem;color:var(--text-secondary)">' + escapeHtml(chainLabel) + '</span>' +
                '<div class="chain-strength-bar">' +
                '<div class="chain-strength-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
                '<span class="chain-strength-val" style="color:' + color + '">' + ch.chain_strength.toFixed(2) + '</span>' +
                '<span class="chain-weakest">weakest: ' + escapeHtml((wk.from || '?') + '\u2192' + (wk.to || '?')) + '</span>' +
            '</div>';
        });
        html += '</div>';
    }

    // 3b. Macro snapshot + Radar chart (two-col)
    html += '<div class="two-col">';

    // Left: Macro snapshot
    html += '<div class="macro-card"><h3>Macro Snapshot ' + infoIcon('Latest macro readings from FRED + market data. Values update daily.') + '</h3>';
    var macroKeys = ['FED_FUNDS', 'YIELD_10Y', 'YIELD_10Y2Y', 'HY_SPREAD', 'VIX', 'M2'];
    if (snap.macro) {
        html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">';
        macroKeys.forEach(function(key) {
            var m = snap.macro[key];
            if (!m) return;
            var val = parseFloat(m.value);
            var fmt = Math.abs(val) >= 100 ? val.toFixed(0) : val.toFixed(2);
            html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;background:rgba(255,255,255,0.02);border-radius:4px">' +
                '<span style="color:var(--text-muted);font-size:0.7rem">' + key + '</span>' +
                '<span style="font-weight:600;font-size:0.8rem">' + fmt + '</span>' +
            '</div>';
        });
        html += '</div>';
    }
    // Sentiment
    if (snap.sentiment) {
        html += '<div style="margin-top:10px;border-top:1px solid var(--border-subtle);padding-top:8px">';
        var sentKeys = ['FEAR_GREED', 'STABLECOIN_SUPPLY:USDT'];
        sentKeys.forEach(function(key) {
            var s = snap.sentiment[key];
            if (!s) return;
            var val = parseFloat(s.value);
            var label = key.split(':')[0];
            var fmt = Math.abs(val) >= 100 ? val.toFixed(0) : val.toFixed(1);
            html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 8px">' +
                '<span style="color:var(--text-muted);font-size:0.7rem">' + label + '</span>' +
                '<span style="font-weight:600;font-size:0.8rem">' + fmt + '</span>' +
            '</div>';
        });
        html += '</div>';
    }
    html += '</div>';

    // Right: Z-Score Radar (ECharts)
    html += '<div class="macro-card"><h3>Macro Z-Score Radar ' + infoIcon('Spider chart of 9 core z-scores. Each axis shows how many std devs from the 90-day mean. Red dots = extreme (|z| > 2). Rings at z=1, 2, 3.') + '</h3>';
    var radarMetrics = ['VIX','HY_SPREAD','YIELD_10Y','YIELD_10Y2Y','M2','FED_FUNDS','FEAR_GREED','FUNDING_BTC','DVOL_BTC'];
    var radarZMap = {};
    if (zscores && zscores.data) {
        zscores.data.forEach(function(z) {
            var key = z.metric || z.asset;
            if (!radarZMap[key] || Math.abs(z.zscore) > Math.abs(radarZMap[key])) {
                radarZMap[key] = z.zscore;
            }
        });
    }
    var radarValues = radarMetrics.map(function(m) { return radarZMap[m] || 0; });
    var _overviewRadarData = radarValues.some(function(v) { return v !== 0; }) ? { metrics: radarMetrics, values: radarValues } : null;
    if (_overviewRadarData) {
        html += '<div id="echart-radar" style="height:240px"></div>';
    } else {
        html += '<p class="text-muted">No z-score data</p>';
    }
    html += '</div>';

    html += '</div>'; // end two-col

    // 3c. Funding Rate + ETF Flow sparklines
    html += buildSparklineCards(fundingSeries, etfSeries, zscores);

    // 4. Historical analogues
    var analogueAsset = (window.INTEL_ASSET || 'BTC').toUpperCase();
    html += '<div class="macro-card"><h3>Historical Analogues (' + analogueAsset + ') ' + infoIcon(sectionTip('analogues')) + '</h3>';
    if (analogues.analogues && analogues.analogues.length) {
        html += '<p class="text-muted" style="margin-bottom:10px">Reference date: ' + analogues.reference_date + '</p>';
        html += '<table class="data-table"><thead><tr>' +
            '<th>Date</th><th>Similarity</th><th>7d</th><th>30d</th><th>90d</th>' +
        '</tr></thead><tbody>';
        analogues.analogues.forEach(function (a) {
            var fwd = a.forward_return || {};
            html += '<tr>' +
                '<td>' + a.date + '</td>' +
                '<td>' + (a.similarity * 100).toFixed(0) + '%</td>' +
                '<td class="' + retClass(fwd['7d']) + '">' + fmtPct(fwd['7d']) + '</td>' +
                '<td class="' + retClass(fwd['30d']) + '">' + fmtPct(fwd['30d']) + '</td>' +
                '<td class="' + retClass(fwd['90d']) + '">' + fmtPct(fwd['90d']) + '</td>' +
            '</tr>';
        });
        html += '</tbody></table>';
    } else {
        html += '<p class="text-muted">No analogue data available</p>';
    }
    html += '</div>';

    // 9. Upcoming Events card
    if (events && Array.isArray(events) && events.length > 0) {
        var today = new Date().toISOString().substring(0, 10);
        var upcoming = events.filter(function(e) { return (e.ts || '').substring(0, 10) >= today; }).slice(0, 7);
        if (upcoming.length > 0) {
            html += '<div class="macro-card"><h3>Upcoming Events</h3>';
            html += '<table class="data-table"><thead><tr><th>Date</th><th>Type</th><th>Title</th><th>Impact</th></tr></thead><tbody>';
            upcoming.forEach(function(e) {
                var d = (e.ts || '').substring(0, 10);
                var daysOut = Math.round((new Date(d) - new Date(today)) / 86400000);
                var urgent = daysOut <= 7 ? ' style="color:#f59e0b;font-weight:600"' : '';
                var impactColor = e.impact === 'high' ? '#ef4444' : '#f59e0b';
                html += '<tr' + urgent + '><td>' + d + (daysOut <= 7 ? ' (' + daysOut + 'd)' : '') + '</td>'
                    + '<td>' + escapeHtml(e.event_type || '') + '</td>'
                    + '<td>' + escapeHtml(e.title || '') + '</td>'
                    + '<td style="color:' + impactColor + '">' + escapeHtml(e.impact || '') + '</td></tr>';
            });
            html += '</tbody></table></div>';
        }
    }

    // 10. Anomaly cluster timeline (ECharts)
    var _overviewAnomalyData = null;
    if (anomalyHistory && anomalyHistory.data && anomalyHistory.data.length >= 2) {
        _overviewAnomalyData = anomalyHistory.data.slice().reverse(); // chronological
        html += '<div class="macro-card"><h3>Anomaly Cluster History ' +
            infoIcon('Historical multi-domain anomaly clusters. Taller bars = more extreme metrics firing simultaneously.') + '</h3>' +
            '<div id="echart-anomaly-timeline" style="height:180px"></div>';
        // Compact list of recent clusters
        var recent = anomalyHistory.data.slice(0, 5);
        html += '<table class="data-table" style="margin-top:8px"><thead><tr><th>Date</th><th>Extremes</th><th>Domains</th><th>Top Metrics</th></tr></thead><tbody>';
        recent.forEach(function(c) {
            var topM = (c.metrics || []).slice(0, 3).map(function(m) {
                var name = m.metric || m;
                var z = m.zscore ? ' (' + fmtZ(m.zscore) + ')' : '';
                return escapeHtml(name) + z;
            }).join(', ');
            var severity = c.extreme_count >= 8 ? 'var(--color-bearish)' : 'var(--color-neutral)';
            html += '<tr><td>' + c.date + '</td>' +
                '<td style="color:' + severity + ';font-weight:600">' + c.extreme_count + '</td>' +
                '<td>' + c.domains_affected + '</td>' +
                '<td style="font-size:0.7rem">' + topM + '</td></tr>';
        });
        html += '</tbody></table></div>';
    }

    setHTML('overview-content', html);

    // Wire transmission-chain rows → jump to the Chains tab
    document.querySelectorAll('#overview-content .chain-jump-row').forEach(function(row) {
        row.addEventListener('click', function() { switchTab('chains'); });
    });

    // Wire regime-alert button
    var rBtn = document.getElementById('regime-alert-btn');
    if (rBtn) rBtn.addEventListener('click', createRegimeAlert);

    // Wire anomaly-cluster alert button
    var aBtn = document.getElementById('anomaly-alert-btn');
    if (aBtn) aBtn.addEventListener('click', function() {
        var ac = (_snapshotCache && _snapshotCache.anomaly_cluster) || {};
        createAnomalyClusterAlert(ac.domains_affected, ac.extreme_count);
    });

    // ── Post-render: initialize ECharts on placeholder divs ──
    if (_overviewRadarData) {
        initOverviewRadar(_overviewRadarData.metrics, _overviewRadarData.values);
    }
    if (_overviewSparkData.funding) {
        initMiniSparkline('echart-spark-funding', _overviewSparkData.funding.values, _overviewSparkData.funding.color);
    }
    if (_overviewSparkData.etf) {
        initMiniSparkline('echart-spark-etf', _overviewSparkData.etf.values, _overviewSparkData.etf.color);
    }
    if (_overviewAnomalyData) {
        initAnomalyTimeline(_overviewAnomalyData);
    }

    // Lead-lag frequency toggle handler
    var llToggle = el('ll-freq-toggle');
    if (llToggle) {
        llToggle.addEventListener('click', function() {
            var cur = this.dataset.freq || 'daily';
            var next = cur === 'daily' ? 'weekly' : 'daily';
            this.dataset.freq = next;
            this.textContent = next;
            var signals = next === 'weekly' ? _snapshotCache.leadlag_signals_weekly : _snapshotCache.leadlag_signals;
            setHTML('leadlag-table-container', buildLeadLagTable(signals));
        });
    }
}

function buildLeadLagNarrative(signals, focusAsset) {
    if (!signals || !signals.length) return '';
    var projected = signals.filter(function(s) { return s.projected_move_z != null; });
    if (!projected.length) return '';
    var top = projected.reduce(function(a, b) {
        return Math.abs(a.projected_move_z) > Math.abs(b.projected_move_z) ? a : b;
    });
    var sign = top.projected_move_z > 0 ? '+' : '';
    var ago = top.lag_days > 0
        ? 'projected over the next ' + top.lag_days + 'd'
        : 'already reflected ' + Math.abs(top.lag_days) + 'd ago';
    var ci = top.projected_ci_z ? ' (95% CI ' + top.projected_ci_z[0] + ' to ' + top.projected_ci_z[1] + ')' : '';
    return '<strong>' + escapeHtml(top.leader) + '</strong> z=' + (top.leader_z != null ? top.leader_z : '?') +
        ' \u00D7 r=' + top.correlation + ' \u2192 ' + (focusAsset || 'target') +
        ' ' + sign + top.projected_move_z.toFixed(2) + '\u03C3 ' + ago + ci + '.';
}


function buildLeadLagTable(signals) {
    if (!signals || !signals.length) return '<p class="text-muted">No lead-lag signals</p>';
    var projHdr = 'Projection (\u03C3)';
    var projTip = 'Projected move of target, in standard deviations, based on leader\'s current z-score \u00D7 best-lag correlation. CI shown on hover.';
    var html = '<table class="data-table"><thead><tr>' +
        '<th>Leader</th><th>Lag</th><th>r</th>' +
        '<th title="' + projTip + '">' + projHdr + '</th>' +
        '</tr></thead><tbody>';
    signals.forEach(function(ll) {
        var arrow = ll.lag_days > 0 ? '&rarr;' : '&larr;';
        var color = ll.correlation > 0 ? 'var(--color-bullish)' : 'var(--color-bearish)';
        var projCell = '<span class="text-muted">\u2014</span>';
        if (ll.projected_move_z != null) {
            var pm = ll.projected_move_z;
            var pmColor = pm > 0 ? 'var(--color-bullish)' : pm < 0 ? 'var(--color-bearish)' : 'var(--text-tertiary)';
            var arrow2 = pm > 0 ? '\u2191' : pm < 0 ? '\u2193' : '\u2022';
            var tip = 'leader z=' + (ll.leader_z != null ? ll.leader_z : '?') +
                      ' \u00D7 r=' + ll.correlation + ' = ' + pm + '\u03C3';
            if (ll.projected_ci_z) {
                tip += '. 95% CI [' + ll.projected_ci_z[0] + ', ' + ll.projected_ci_z[1] + ']';
            }
            projCell = '<span title="' + tip + '" style="color:' + pmColor + ';cursor:help">' +
                       arrow2 + ' ' + pm.toFixed(2) + '\u03C3</span>';
        }
        // Stability tag: persistent=true → stable, persistent=false → ⚠, null → omit
        var stabilityTag = '';
        if (ll.persistent === true) {
            stabilityTag = ' <span title="Signal consistent across sub-periods" style="' +
                'font-size:0.6rem;background:rgba(38,166,154,0.15);color:#26a69a;' +
                'border:1px solid rgba(38,166,154,0.3);border-radius:3px;padding:0 4px;' +
                'vertical-align:middle">stable</span>';
        } else if (ll.persistent === false) {
            stabilityTag = ' <span title="Signal inconsistent across sub-periods — may not hold" style="' +
                'font-size:0.6rem;color:#7a8494;vertical-align:middle">⚠</span>';
        }
        html += '<tr>' +
            '<td>' + ll.leader + stabilityTag + '</td>' +
            '<td>' + arrow + ' ' + Math.abs(ll.lag_days) + 'd</td>' +
            '<td style="color:' + color + '">' + fmtR(ll.correlation) + '</td>' +
            '<td>' + projCell + '</td>' +
        '</tr>';
    });
    html += '</tbody></table>';
    return html;
}


// ── ECharts: Overview chart init functions ─────────────────────────

function initOverviewRadar(metrics, values) {
    var chart = initChart('echart-radar');
    if (!chart) return;
    var indicators = metrics.map(function(m) {
        return { name: shortLabel(m), max: 3 };
    });
    var absValues = values.map(function(v) { return Math.min(Math.abs(v || 0), 3); });
    chart.setOption({
        radar: {
            indicator: indicators,
            radius: '70%',
            splitNumber: 3,
            axisName: { color: '#7a8494', fontSize: 10 },
            splitArea: { areaStyle: { color: ['transparent'] } },
            splitLine: { lineStyle: { color: '#1e2430', width: 0.5, type: 'dashed' } },
            axisLine: { lineStyle: { color: '#1e2430', width: 0.5 } },
        },
        tooltip: {
            trigger: 'item',
            formatter: function(params) {
                var tip = '';
                metrics.forEach(function(m, i) {
                    var z = values[i];
                    var color = Math.abs(z) >= 2 ? '#ef5350' : '#6bc985';
                    tip += '<span style="color:' + color + '">' + shortLabel(m) + ': ' + fmtZ(z) + '\u03C3</span><br/>';
                });
                return tip;
            },
        },
        series: [{
            type: 'radar',
            data: [{ value: absValues, name: 'Z-Scores' }],
            symbol: 'circle',
            symbolSize: 6,
            lineStyle: { color: '#6bc985', width: 1.5 },
            areaStyle: { color: 'rgba(107,201,133,0.12)' },
            itemStyle: {
                color: function(params) {
                    return Math.abs(values[params.dataIndex]) >= 2 ? '#ef5350' : '#6bc985';
                },
            },
        }],
    });
}

function initMiniSparkline(containerId, values, color) {
    var chart = initChart(containerId);
    if (!chart) return;
    chart.setOption({
        animation: false,
        grid: { left: 0, right: 0, top: 2, bottom: 2 },
        xAxis: { type: 'category', show: false, data: values.map(function(_, i) { return i; }) },
        yAxis: { type: 'value', show: false },
        series: [{
            type: 'line',
            data: values,
            showSymbol: false,
            lineStyle: { width: 1.5, color: color },
            areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                colorStops: [{ offset: 0, color: color.replace(')', ',0.15)').replace('rgb', 'rgba') || 'rgba(107,201,133,0.15)' }, { offset: 1, color: 'transparent' }]
            } },
        }],
        tooltip: { show: false },
    });
}

function initAnomalyTimeline(clusters) {
    var chart = initChart('echart-anomaly-timeline');
    if (!chart) return;
    var dates = clusters.map(function(c) { return (c.date || '').substring(5); }); // MM-DD
    var counts = clusters.map(function(c) { return c.extreme_count; });
    var colors = clusters.map(function(c) {
        return c.extreme_count >= 8 ? '#ef5350' : c.extreme_count >= 5 ? '#ff9800' : '#26a69a';
    });
    chart.setOption({
        grid: { left: 40, right: 16, top: 10, bottom: 30 },
        xAxis: {
            type: 'category',
            data: dates,
            axisLabel: { fontSize: 9, interval: Math.max(0, Math.floor(clusters.length / 6) - 1), rotate: 30 },
        },
        yAxis: {
            type: 'value',
            name: 'Extremes',
            nameTextStyle: { fontSize: 9, color: '#7a8494' },
            minInterval: 1,
        },
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var p = params[0];
                var c = clusters[p.dataIndex];
                var topM = (c.metrics || []).slice(0, 3).map(function(m) { return (m.metric || m); }).join(', ');
                return '<strong>' + c.date + '</strong><br/>' +
                    c.extreme_count + ' extremes, ' + c.domains_affected + ' domains<br/>' +
                    '<span style="color:var(--text-muted)">' + topM + '</span>';
            },
        },
        series: [{
            type: 'bar',
            data: counts.map(function(v, i) {
                return { value: v, itemStyle: { color: colors[i], borderRadius: [2, 2, 0, 0] } };
            }),
            barMaxWidth: 12,
        }],
    });
}


function initRegimeHistory(history, events) {
    var chart = initChart('echart-regime-history');
    if (!chart) return;

    var dates = history.map(function(h) { return h.date; });
    // Regime as colored bar: 1=favorable, -1=unfavorable
    var regimeData = history.map(function(h) {
        return {
            value: 1,
            itemStyle: { color: h.state === 'favorable' ? '#26a69a' : '#ef5350' },
        };
    });
    var probData = history.map(function(h) { return h.probability || 0; });

    // Event markPoints
    var eventMarks = [];
    if (events && Array.isArray(events)) {
        var dateSet = {};
        dates.forEach(function(d, i) { dateSet[d] = i; });
        events.forEach(function(e) {
            var d = (e.ts || '').substring(0, 10);
            if (dateSet[d] != null) {
                eventMarks.push({
                    coord: [dateSet[d], 1],
                    symbol: 'triangle',
                    symbolSize: 8,
                    itemStyle: { color: e.impact === 'high' ? '#ef4444' : '#f59e0b' },
                    label: { show: false },
                    name: (e.event_type || '') + ': ' + (e.title || ''),
                });
            }
        });
    }

    chart.setOption({
        grid: [
            { left: 50, right: 20, top: 10, height: 30 },
            { left: 50, right: 20, top: 60, bottom: 30 },
        ],
        xAxis: [
            { type: 'category', data: dates, gridIndex: 0, show: false },
            { type: 'category', data: dates, gridIndex: 1, axisLabel: { fontSize: 9, interval: Math.floor(dates.length / 6) } },
        ],
        yAxis: [
            { type: 'value', gridIndex: 0, show: false, max: 1 },
            { type: 'value', gridIndex: 1, name: 'Prob', nameTextStyle: { fontSize: 9 }, max: 1, min: 0 },
        ],
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: function(params) {
                var idx = params[0].dataIndex;
                var h = history[idx];
                return '<strong>' + h.date + '</strong><br/>' +
                    'State: <span style="color:' + (h.state === 'favorable' ? '#26a69a' : '#ef5350') + '">' + h.state + '</span><br/>' +
                    'Confidence: ' + Math.round((h.probability || 0) * 100) + '%';
            },
        },
        dataZoom: [{ type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 18 }],
        series: [
            {
                type: 'bar',
                data: regimeData,
                xAxisIndex: 0, yAxisIndex: 0,
                barWidth: '100%',
                markPoint: eventMarks.length ? { data: eventMarks, tooltip: { formatter: function(p) { return p.name; } } } : undefined,
            },
            {
                type: 'line',
                data: probData,
                xAxisIndex: 1, yAxisIndex: 1,
                showSymbol: false,
                lineStyle: { width: 1.2, color: '#6bc985' },
                areaStyle: { color: 'rgba(107,201,133,0.1)' },
            },
        ],
    });
}


// ═══════════════════════════════════════════════════════════════════
// TAB 2: CORRELATIONS
// ═══════════════════════════════════════════════════════════════════

function loadCorrelations() {
    loadHeatmap(60);

    // Window toggle
    el('corr-window-toggle').addEventListener('click', function () {
        var cur = parseInt(this.dataset.window || '60');
        var next = cur === 60 ? 120 : 60;
        this.dataset.window = next;
        this.textContent = next + 'd window';
        loadHeatmap(next);
    });

    // Regime segmented control
    document.querySelectorAll('#corr-regime-toggle .corr-regime-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            document.querySelectorAll('#corr-regime-toggle .corr-regime-btn').forEach(function(b) {
                b.classList.remove('active');
            });
            this.classList.add('active');
            _corrRegime = this.dataset.regime || '';
            var win = parseInt((el('corr-window-toggle') && el('corr-window-toggle').dataset.window) || '60');
            loadHeatmap(win);
        });
    });

    // Category filter pills
    document.querySelectorAll('#heatmap-category-filter .filter-pill').forEach(function(pill) {
        pill.addEventListener('click', function() {
            this.classList.toggle('active');
            _heatmapCategoryFilter = getActiveCategories();
            if (_heatmapData) {
                renderHeatmap(_heatmapData);
                renderCorrTable(_heatmapData);
            }
        });
    });

    // View toggle: heatmap ↔ network
    el('corr-view-toggle').addEventListener('click', function () {
        var view = this.dataset.view || 'heatmap';
        if (view === 'heatmap') {
            this.dataset.view = 'network';
            this.textContent = 'Heatmap';
            el('heatmap-container').style.display = 'none';
            el('network-container').style.display = '';
            if (_heatmapData) renderNetwork(_heatmapData);
        } else {
            this.dataset.view = 'heatmap';
            this.textContent = 'Network';
            el('heatmap-container').style.display = '';
            el('network-container').style.display = 'none';
        }
    });
}

function getActiveCategories() {
    var active = [];
    document.querySelectorAll('#heatmap-category-filter .filter-pill.active').forEach(function(p) {
        active.push(p.dataset.cat);
    });
    // null means show all (no filtering)
    return active.length >= 5 ? null : (active.length === 0 ? [] : active);
}

function getFilteredMetrics() {
    if (!_heatmapCategoryFilter || _heatmapCategoryFilter.length === 0) {
        return _heatmapCategoryFilter === null ? METRIC_ORDER : [];
    }
    return METRIC_ORDER.filter(function(m) {
        return _heatmapCategoryFilter.indexOf(METRIC_CATEGORIES[m]) >= 0;
    });
}

function loadHeatmap(windowSize) {
    showLoading('heatmap-container');
    showLoading('corr-table-container');

    var regimeParam = _corrRegime ? '&regime=' + _corrRegime : '';
    apiFetch('/api/quant/correlations?matrix=true&window=' + windowSize + regimeParam)
    .then(function (data) {
        _heatmapData = data.data;
        renderHeatmap(data.data);
        renderCorrTable(data.data);
    })
    .catch(function (err) {
        showError('heatmap-container', err.message);
    });
}

function renderHeatmap(pairs) {
    // Build symmetric matrix lookup (preserve significant + q_value for rendering)
    var matrix = {};
    pairs.forEach(function (p) {
        matrix[p.metric_a + '|' + p.metric_b] = p;
        matrix[p.metric_b + '|' + p.metric_a] = {
            metric_a: p.metric_b, metric_b: p.metric_a,
            correlation: p.correlation, p_value: p.p_value,
            q_value: p.q_value, significant: p.significant,
        };
    });

    // Use filtered metrics if category filter is active
    var filteredMetrics = getFilteredMetrics();
    if (filteredMetrics.length === 0) {
        setHTML('heatmap-container', '<p class="text-muted" style="padding:2rem;text-align:center">Select at least one category to display</p>');
        return;
    }

    var n = filteredMetrics.length;
    var labels = filteredMetrics.map(shortLabel);

    // Check if any pair has significance info (DB may or may not have computed it)
    var hasSigInfo = pairs.some(function(p) { return p.significant != null; });

    // Build heatmap data: [colIdx, rowIdx, value] or object with itemStyle for non-sig cells
    var data = [];
    for (var r = 0; r < n; r++) {
        for (var c = 0; c < n; c++) {
            if (r === c) {
                data.push([c, r, 1]); // diagonal
            } else {
                var key = filteredMetrics[r] + '|' + filteredMetrics[c];
                var p = matrix[key];
                var cell = { value: [c, r, p ? parseFloat(p.correlation) : 0] };
                // Mute cells that are explicitly flagged not-significant
                if (hasSigInfo && p && p.significant === false) {
                    cell.itemStyle = { opacity: 0.28 };
                }
                data.push(cell);
            }
        }
    }

    // Compute dynamic height based on metric count
    var hmHeight = Math.max(400, Math.min(700, n * 18 + 80));

    // Significance legend (shown only when sig info is available)
    var sigLegend = hasSigInfo
        ? '<p style="font-size:0.65rem;color:var(--text-muted);margin:6px 0 0">muted = not statistically significant (FDR q ≥ 0.05)</p>'
        : '';

    // Title + container
    setHTML('heatmap-container', '<h3 style="margin-bottom:12px">Correlation Heatmap (' + n + ' metrics) ' +
        infoIcon(SECTION_TIPS.heatmap) + explainerBtn('heatmap-tab') + '</h3>' +
        '<div class="explainer-panel" id="explainer-heatmap-tab">' + EXPLAINERS.heatmap + '</div>' +
        '<div id="echart-heatmap" style="height:' + hmHeight + 'px"></div>' +
        sigLegend);

    var chart = initChart('echart-heatmap');
    if (!chart) return;

    // Compute category breaks for filtered list
    var catBreaks = [];
    var prevCat = null;
    filteredMetrics.forEach(function(m, i) {
        var cat = METRIC_CATEGORIES[m];
        if (prevCat && cat !== prevCat) catBreaks.push(i);
        prevCat = cat;
    });

    // Category break markLines
    var markLines = catBreaks.map(function(idx) {
        return [
            { xAxis: idx - 0.5, yAxis: -0.5 },
            { xAxis: idx - 0.5, yAxis: n - 0.5 },
        ];
    });
    var yMarkLines = catBreaks.map(function(idx) {
        return [
            { xAxis: -0.5, yAxis: idx - 0.5 },
            { xAxis: n - 0.5, yAxis: idx - 0.5 },
        ];
    });

    chart.setOption({
        tooltip: {
            formatter: function(params) {
                var ri = params.value[1], ci = params.value[0];
                var val = Array.isArray(params.value) ? params.value[2] : params.data.value[2];
                if (ri === ci) return filteredMetrics[ri] + ' (diagonal)';
                var key = filteredMetrics[ri] + '|' + filteredMetrics[ci];
                var p = matrix[key];
                var pStr = p && p.p_value != null ? '<br/>p=' + parseFloat(p.p_value).toExponential(2) : '';
                var qStr = p && p.q_value != null ? '<br/>q=' + parseFloat(p.q_value).toExponential(2) : '';
                var sigStr = (p && p.significant != null)
                    ? '<br/><span style="color:' + (p.significant ? '#6bc985' : '#7a8494') + '">' +
                      (p.significant ? 'significant' : 'not significant') + '</span>'
                    : '';
                return '<strong>' + filteredMetrics[ri] + ' \u2194 ' + filteredMetrics[ci] + '</strong><br/>r = ' + val.toFixed(4) + pStr + qStr + sigStr;
            },
        },
        grid: { left: 80, right: 20, top: 40, bottom: 80 },
        xAxis: {
            type: 'category',
            data: labels,
            position: 'top',
            axisLabel: { rotate: 45, fontSize: 10, interval: 0 },
            splitArea: { show: false },
        },
        yAxis: {
            type: 'category',
            data: labels,
            inverse: true,
            axisLabel: { fontSize: 10, interval: 0 },
            splitArea: { show: false },
        },
        visualMap: {
            min: -1, max: 1,
            calculable: true,
            orient: 'horizontal',
            left: 'center',
            bottom: 4,
            itemWidth: 14,
            itemHeight: 140,
            inRange: {
                color: ['#3b82f6', '#1e3a5f', '#1c2129', '#5c2020', '#ef4444'],
            },
            textStyle: { color: '#7a8494', fontSize: 10 },
        },
        series: [{
            type: 'heatmap',
            data: data,
            itemStyle: { borderWidth: 0.5, borderColor: '#0a0e14' },
            emphasis: { itemStyle: { borderColor: '#e6edf3', borderWidth: 1.5 } },
            markLine: {
                silent: true,
                symbol: 'none',
                lineStyle: { color: '#3d4757', width: 2 },
                data: markLines.concat(yMarkLines),
                label: { show: false },
            },
        }],
    });

    // Click handler: scroll to table row
    chart.on('click', function(params) {
        var ri = params.value[1], ci = params.value[0];
        if (ri === ci) return;
        var rowId = 'corr-row-' + Math.min(ri, ci) + '-' + Math.max(ri, ci);
        var row = document.getElementById(rowId);
        if (row) {
            row.scrollIntoView({ behavior: 'smooth', block: 'center' });
            row.classList.add('highlight');
            setTimeout(function () { row.classList.remove('highlight'); }, 2000);
        }
    });
}

function renderCorrTable(pairs) {
    var filteredMetrics = getFilteredMetrics();
    var filteredSet = {};
    filteredMetrics.forEach(function(m) { filteredSet[m] = true; });

    // Filter pairs to only include selected categories
    var filtered = pairs.filter(function(p) {
        return filteredSet[p.metric_a] && filteredSet[p.metric_b];
    });

    // Sort by |correlation| descending
    var sorted = filtered.slice().sort(function (a, b) {
        return Math.abs(parseFloat(b.correlation)) - Math.abs(parseFloat(a.correlation));
    });

    var hasSigCol = sorted.some(function(p) { return p.significant != null; });
    var html = '<table class="data-table sortable-table" id="corr-detail-table">' +
        '<thead><tr><th>Metric A</th><th>Metric B</th><th>r</th><th>p-value</th>' +
        (hasSigCol ? '<th title="FDR-adjusted q-value">q-value</th>' : '') +
        '</tr></thead><tbody>';

    sorted.forEach(function (p) {
        var idxA = filteredMetrics.indexOf(p.metric_a);
        var idxB = filteredMetrics.indexOf(p.metric_b);
        var rowId = 'corr-row-' + Math.min(idxA, idxB) + '-' + Math.max(idxA, idxB);
        var val = parseFloat(p.correlation);
        var color = val > 0 ? 'var(--color-bullish)' : 'var(--color-bearish)';
        // Mute rows that are not statistically significant
        var rowStyle = (hasSigCol && p.significant === false) ? ' style="opacity:0.45"' : '';
        var qCell = hasSigCol
            ? '<td>' + (p.q_value != null ? parseFloat(p.q_value).toExponential(2) : '—') + '</td>'
            : '';
        html += '<tr id="' + rowId + '"' + rowStyle + '>' +
            '<td>' + p.metric_a + '</td>' +
            '<td>' + p.metric_b + '</td>' +
            '<td style="color:' + color + '">' + val.toFixed(4) + '</td>' +
            '<td>' + parseFloat(p.p_value).toExponential(2) + '</td>' +
            qCell +
        '</tr>';
    });

    html += '</tbody></table>';
    setHTML('corr-table-container', html);

    // Make table sortable
    var table = el('corr-detail-table');
    if (table) {
        table.querySelectorAll('thead th').forEach(function (th, idx) {
            th.style.cursor = 'pointer';
            th.addEventListener('click', function () { sortTable(table, idx); });
        });
    }
}

var _sortDir = {};
function sortTable(table, colIdx) {
    var dir = _sortDir[colIdx] === 'asc' ? 'desc' : 'asc';
    _sortDir[colIdx] = dir;
    var tbody = table.tBodies[0];
    var rows = Array.from(tbody.rows);
    rows.sort(function (a, b) {
        var av = a.cells[colIdx].textContent;
        var bv = b.cells[colIdx].textContent;
        var an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) {
            return dir === 'asc' ? an - bn : bn - an;
        }
        return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
}


function renderNetwork(pairs) {
    var DOMAIN_COLORS = ['#26a69a', '#58a6ff', '#a78bfa', '#ff9800', '#6bc985'];
    var DOMAIN_NAMES = ['Price', 'Macro', 'Derivatives', 'Sentiment', 'Factors'];

    function domainIdx(i) {
        for (var d = CATEGORY_BREAKS.length - 1; d >= 0; d--) {
            if (i >= CATEGORY_BREAKS[d]) return d + 1;
        }
        return 0;
    }

    // Filter edges: |r| > 0.3 and p < 0.05
    var n = METRIC_ORDER.length;
    var conns = new Array(n).fill(0);
    var graphLinks = [];
    pairs.forEach(function(p) {
        var r = parseFloat(p.correlation);
        var pv = parseFloat(p.p_value);
        var ai = METRIC_ORDER.indexOf(p.metric_a);
        var bi = METRIC_ORDER.indexOf(p.metric_b);
        if (ai >= 0 && bi >= 0 && Math.abs(r) > 0.3 && pv < 0.05) {
            conns[ai]++; conns[bi]++;
            graphLinks.push({
                source: METRIC_ORDER[ai],
                target: METRIC_ORDER[bi],
                value: r,
                lineStyle: {
                    color: r > 0 ? '#26a69a' : '#ef5350',
                    width: Math.abs(r) * 3,
                    opacity: Math.max(0.3, 1 - pv * 10),
                },
            });
        }
    });
    var maxConns = Math.max.apply(null, conns) || 1;

    var categories = DOMAIN_NAMES.map(function(name, i) {
        return { name: name, itemStyle: { color: DOMAIN_COLORS[i] } };
    });

    var graphNodes = METRIC_ORDER.map(function(m, i) {
        return {
            name: m,
            label: { show: true, formatter: shortLabel(m), fontSize: 9, color: '#b0b8c4' },
            symbolSize: 10 + (conns[i] / maxConns) * 16,
            category: domainIdx(i),
            value: conns[i],
        };
    });

    setHTML('network-container', '<div id="echart-network" style="height:520px"></div>');
    var chart = initChart('echart-network');
    if (!chart) return;

    chart.setOption({
        tooltip: {
            formatter: function(params) {
                if (params.dataType === 'node') {
                    return '<strong>' + params.name + '</strong><br/>' + params.value + ' connections';
                }
                if (params.dataType === 'edge') {
                    return params.data.source + ' \u2194 ' + params.data.target + '<br/>r = ' + params.data.value.toFixed(3);
                }
                return '';
            },
        },
        legend: {
            data: DOMAIN_NAMES,
            bottom: 0,
            textStyle: { fontSize: 10 },
        },
        series: [{
            type: 'graph',
            layout: 'force',
            data: graphNodes,
            links: graphLinks,
            categories: categories,
            roam: true,
            force: { repulsion: 800, edgeLength: [50, 200], gravity: 0.1 },
            emphasis: { focus: 'adjacency', lineStyle: { width: 4 } },
            label: { position: 'top', distance: 5 },
            itemStyle: { borderColor: '#0a0e14', borderWidth: 1.5 },
        }],
    });
}


// ═══════════════════════════════════════════════════════════════════
// TAB 8: CHAINS
// ═══════════════════════════════════════════════════════════════════

function loadChains() {
    showLoading('chains-content');
    var freq = (el('chain-freq-toggle') && el('chain-freq-toggle').dataset.freq) || 'daily';
    var chainNames = ['monetary_policy', 'dollar_liquidity', 'risk_appetite'];

    Promise.all([
        // Chain details
        apiFetch('/api/quant/chain?chain=monetary_policy&frequency=' + freq),
        apiFetch('/api/quant/chain?chain=dollar_liquidity&frequency=' + freq),
        apiFetch('/api/quant/chain?chain=risk_appetite&frequency=' + freq),
        // Chain history — 180d for trend visibility
        apiFetch('/api/quant/chain-history?chain=monetary_policy&days=180').catch(function() { return []; }),
        apiFetch('/api/quant/chain-history?chain=dollar_liquidity&days=180').catch(function() { return []; }),
        apiFetch('/api/quant/chain-history?chain=risk_appetite&days=180').catch(function() { return []; }),
    ]).then(function(results) {
        var chains = results.slice(0, 3);
        var histories = results.slice(3, 6);
        chains.forEach(function(c, i) { c._history = histories[i] || []; });
        renderChains(chains);
    }).catch(function(err) {
        showError('chains-content', err.message);
    });

    // Frequency toggle handler (bind once)
    if (!loadChains._bound) {
        el('chain-freq-toggle').addEventListener('click', function() {
            var cur = this.dataset.freq || 'daily';
            var next = cur === 'daily' ? 'weekly' : 'daily';
            this.dataset.freq = next;
            this.textContent = next === 'daily' ? 'Daily' : 'Weekly';
            _tabLoaded['chains'] = false;
            loadChains();
        });
        loadChains._bound = true;
    }
}

function renderChains(chains) {
    var html = '<div style="margin-bottom:12px">' + explainerBtn('chains-tab') + '</div>' +
        '<div class="explainer-panel" id="explainer-chains-tab">' + explainerHtml('chains') + '</div>';

    chains.forEach(function(chain) {
        var chainLabel = chain.chain.replace(/_/g, ' ');
        var strengthColor = chain.chain_strength > 0.3 ? 'var(--color-bullish)' :
            chain.chain_strength > 0.15 ? '#ff9800' : 'var(--color-bearish)';
        var wk = chain.weakest_link || {};
        var pct = Math.min(chain.chain_strength * 100 / 0.5, 100);

        html += '<div class="macro-card chain-section">';

        // Header (with alert button)
        html += '<div class="chain-header">' +
            '<h4>' + escapeHtml(chainLabel) + '</h4>' +
            '<button class="alert-btn chain-alert-btn" data-chain="' + escapeHtml(chain.chain) +
            '" title="Notify me when this chain strength crosses a threshold">\u2301 Alert</button>' +
        '</div>';
        html += '<div class="chain-desc">' + escapeHtml(chain.description) + '</div>';

        // Strength bar + sparkline + weakest link (clickable drill-through)
        var sparkId = 'echart-chain-spark-' + chain.chain;
        var wkClickable = (wk.from && wk.to)
            ? '<button class="chain-weakest-btn" data-leader="' + escapeHtml(wk.from) + '" data-target="' + escapeHtml(wk.to) + '" title="View lag correlogram for this pair">'
              + 'weakest: ' + escapeHtml(wk.from + ' \u2192 ' + wk.to)
              + (wk.r != null ? ' (r=' + fmtR(wk.r) + ')' : '') + ' \u2197'
              + '</button>'
            : '<span class="chain-weakest">weakest: unknown</span>';
        html += '<div class="chain-strength-row">' +
            '<span style="font-size:0.65rem;color:var(--text-muted);min-width:56px">strength</span>' +
            '<div class="chain-strength-bar">' +
            '<div class="chain-strength-fill" style="width:' + pct + '%;background:' + strengthColor + '"></div></div>' +
            '<span class="chain-strength-val" style="color:' + strengthColor + '">' + chain.chain_strength.toFixed(3) + '</span>' +
            '<div class="chain-sparkline" id="' + sparkId + '"></div>' +
            wkClickable +
        '</div>';

        // Flow diagram
        html += buildChainFlow(chain);

        html += '</div>'; // end chain-section
    });

    setHTML('chains-content', html);

    // Initialize sparklines
    chains.forEach(function(chain) {
        if (chain._history && chain._history.length > 1) {
            var vals = chain._history.slice().reverse().map(function(h) { return h.strength; });
            var lastVal = vals[vals.length - 1];
            var color = lastVal > 0.3 ? '#26a69a' : lastVal > 0.15 ? '#ff9800' : '#ef5350';
            initMiniSparkline('echart-chain-spark-' + chain.chain, vals, color);
        }
    });

    // Wire weakest-link drill-through
    document.querySelectorAll('.chain-weakest-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            openPairCorrelogram(this.dataset.leader, this.dataset.target);
        });
    });

    // Wire chain-strength alert buttons
    document.querySelectorAll('.chain-alert-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            createChainAlert(this.dataset.chain);
        });
    });
}

// Pair lag-correlogram modal. Fetches the full cross-correlogram
// for (leader → target) from the already-stored leadlag rows and renders
// an ECharts bar chart showing r at each lag.
function openPairCorrelogram(leader, target) {
    var modal = document.getElementById('pair-correlogram-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'pair-correlogram-modal';
        modal.className = 'pair-modal';
        modal.innerHTML = '<div class="pair-modal-backdrop"></div>' +
            '<div class="pair-modal-card">' +
                '<div class="pair-modal-header">' +
                    '<h3 id="pair-modal-title">Pair details</h3>' +
                    '<button class="pair-modal-close" aria-label="Close">&times;</button>' +
                '</div>' +
                '<div id="pair-modal-body">' +
                    '<div class="loading-spinner">Loading...</div>' +
                '</div>' +
            '</div>';
        document.body.appendChild(modal);
        modal.querySelector('.pair-modal-backdrop').addEventListener('click', closePairCorrelogram);
        modal.querySelector('.pair-modal-close').addEventListener('click', closePairCorrelogram);
    }
    document.getElementById('pair-modal-title').textContent = leader + ' \u2192 ' + target + ' — lag correlogram';
    document.getElementById('pair-modal-body').innerHTML = '<div class="loading-spinner">Loading...</div>';
    modal.classList.add('open');

    apiFetch('/api/quant/leadlag-pair?leader=' + encodeURIComponent(leader) +
             '&target=' + encodeURIComponent(target))
        .then(function(data) {
            if (!data.correlogram || !data.correlogram.length) {
                document.getElementById('pair-modal-body').innerHTML =
                    '<p class="text-muted">No correlogram data available for this pair.</p>';
                return;
            }
            var best = data.best || {};
            var ciTxt = (best.ci_low != null) ? ' (95% CI ' + best.ci_low + ' to ' + best.ci_high + ')' : '';
            var summary = '<p style="font-size:0.82rem;margin:0 0 12px 0">' +
                '<strong>Best lag:</strong> ' + best.lag + 'd at <strong>r=' + fmtR(best.correlation) + '</strong>' +
                ciTxt + '</p>';
            var chartDiv = '<div id="pair-correlogram-chart" style="width:100%;height:320px"></div>';
            document.getElementById('pair-modal-body').innerHTML = summary + chartDiv;
            var chart = initChart('pair-correlogram-chart');
            if (!chart) return;
            var rows = data.correlogram.slice().sort(function(a, b) { return a.lag - b.lag; });
            var lags = rows.map(function(r) { return r.lag; });
            var rs = rows.map(function(r) { return r.correlation; });
            chart.setOption({
                grid: { top: 20, right: 20, bottom: 36, left: 56 },
                tooltip: { trigger: 'axis' },
                xAxis: { type: 'category', data: lags, name: 'lag (days)', nameLocation: 'middle', nameGap: 24 },
                yAxis: { type: 'value', name: 'r', min: -1, max: 1 },
                series: [{
                    type: 'bar',
                    data: rs.map(function(v) {
                        var c = v > 0 ? 'rgba(38,166,91,0.75)' : 'rgba(239,83,80,0.75)';
                        return { value: v, itemStyle: { color: c } };
                    }),
                    barMaxWidth: 8,
                }],
            });
        })
        .catch(function(err) {
            document.getElementById('pair-modal-body').innerHTML =
                '<p class="text-muted">Failed to load correlogram: ' + err.message + '</p>';
        });
}

function closePairCorrelogram() {
    var modal = document.getElementById('pair-correlogram-modal');
    if (modal) modal.classList.remove('open');
}

// ── Save-as-alert handlers ──────────────────────────────────
function postAlert(url, body, successMsg) {
    return fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).then(function(r) {
        return r.json().then(function(data) { return { ok: r.ok, status: r.status, data: data }; });
    }).then(function(resp) {
        if (resp.ok) {
            showFlash(successMsg + ' (manage in Alerts)');
        } else if (resp.status === 403 && resp.data && resp.data.upgrade_required) {
            showFlash('Requires a paid subscription.', true);
        } else {
            showFlash('Alert failed: ' + (resp.data && resp.data.error || resp.status), true);
        }
    }).catch(function(err) {
        showFlash('Alert failed: ' + err.message, true);
    });
}

function createRegimeAlert() {
    postAlert('/api/alerts/regime', { frequency: 'recurring' },
              'Regime-change alert created');
}

function createAnomalyClusterAlert(defaultDomains, defaultMetrics) {
    var md = parseInt(prompt('Alert when cluster spans at least N domains:',
                             defaultDomains || 2), 10);
    if (!md || md < 1) { showFlash('Invalid domain count.', true); return; }
    var mm = parseInt(prompt('...and has at least N extreme metrics:',
                             defaultMetrics || 3), 10);
    if (!mm || mm < 1) { showFlash('Invalid metric count.', true); return; }
    postAlert('/api/alerts/anomaly_cluster', {
        min_domains: md,
        min_metrics: mm,
        frequency: 'once',
    }, 'Anomaly-cluster alert (\u2265' + mm + ' metrics / \u2265' + md + ' domains) created');
}

function createConditionalAlert() {
    if (!_lastCondQuery || !_lastCondQuery.conditions) {
        showFlash('Run a condition query first.', true);
        return;
    }
    postAlert('/api/alerts/conditional', {
        conditions: _lastCondQuery.conditions,
        target: _lastCondQuery.target || 'BTC',
        horizon: _lastCondQuery.horizon || 30,
        frequency: 'recurring',
    }, 'Conditional alert saved (fires when match flips true)');
}

function createChainAlert(chainName) {
    var label = chainName.replace(/_/g, ' ');
    var input = prompt('Alert when ' + label + ' chain strength drops below…', '0.15');
    if (!input) return;
    var threshold = parseFloat(input);
    if (isNaN(threshold) || threshold <= 0) {
        showFlash('Invalid threshold.', true);
        return;
    }
    postAlert('/api/alerts/chain_strength', {
        chain: chainName,
        condition: 'below',
        threshold: threshold,
        frequency: 'once',
    }, 'Chain-strength alert on ' + label + ' < ' + threshold + ' created');
}

function showFlash(msg, isError) {
    var flash = document.getElementById('qe-flash');
    if (!flash) {
        flash = document.createElement('div');
        flash.id = 'qe-flash';
        flash.className = 'qe-flash';
        document.body.appendChild(flash);
    }
    flash.textContent = msg;
    flash.classList.remove('qe-flash-error', 'show');
    if (isError) flash.classList.add('qe-flash-error');
    // force reflow before adding show class to restart CSS transition
    void flash.offsetWidth;
    flash.classList.add('show');
    clearTimeout(showFlash._timer);
    showFlash._timer = setTimeout(function() { flash.classList.remove('show'); }, 3500);
}

function buildChainFlow(chain) {
    if (!chain.links || !chain.links.length) return '';
    var html = '<div class="chain-flow">';

    // First node
    html += '<div class="chain-node"><div class="chain-node-label">' +
        escapeHtml(chain.links[0].from) + '</div></div>';

    chain.links.forEach(function(link) {
        var r = link.correlation || 0;
        var absR = Math.abs(r);
        var lineColor = absR > 0.3 ? 'var(--color-bullish)' : absR >= 0.15 ? '#ff9800' : 'var(--color-bearish)';
        var lineStyle = link.persistent ? 'solid' : 'dashed';
        var lagLabel = link.best_lag ? Math.abs(link.best_lag) + 'd' : '0d';
        var persistLabel = link.persistent ? ' \u2713' : '';

        html += '<div class="chain-link">' +
            '<span class="chain-link-lag">' + lagLabel + '</span>' +
            '<div class="chain-link-line" style="border-top:2px ' + lineStyle + ' ' + lineColor + '"></div>' +
            '<span class="chain-link-r" style="color:' + lineColor + '">' + fmtR(r) + persistLabel + '</span>' +
        '</div>';
        html += '<div class="chain-node"><div class="chain-node-label">' +
            escapeHtml(link.to) + '</div></div>';
    });

    html += '</div>';
    return html;
}


// ═══════════════════════════════════════════════════════════════════
// TAB 3: REGIME
// ═══════════════════════════════════════════════════════════════════

function loadRegime() {
    showLoading('regime-content');

    Promise.all([
        apiFetch('/api/quant/regime?history=true'),
        apiFetch('/api/quant/events'),
    ]).then(function (results) { _regimeCache = results[0]; renderRegime(results[0], results[1]); })
    .catch(function (err) { showError('regime-content', err.message); });
}

function renderRegime(data, events) {
    var html = '';
    var cur = data.current || {};
    var states = data.states || {};
    var means = data.state_means || {};
    var tm = data.transition_matrix || [];
    var cps = data.changepoints || [];
    var hist_pct = data.historical_pct || {};

    // 1. Current state card with progress ring
    var cls = cur.state === 'favorable' ? 'regime-favorable' : 'regime-unfavorable';
    var regRingColor = cur.state === 'favorable' ? 'var(--color-bullish-bright)' : 'var(--color-bearish-bright)';
    html += '<div class="regime-current ' + cls + '">' +
        '<div style="margin-bottom:8px">' + progressRing(cur.probability || 0, 80, 6, regRingColor, Math.round((cur.probability || 0) * 100) + '%') + '</div>' +
        '<div class="regime-state-label">' + (cur.state || 'unknown').toUpperCase() + ' ' + infoIcon(SECTION_TIPS.regime) + '</div>' +
        '<div class="regime-meta">' + (cur.duration_days || 0) + ' days &middot; since ' + (cur.since || '?') + '</div>' +
    '</div>';

    // 1b. Regime forecast widget
    if (tm.length >= 2) {
        var stateIdx = cur.state === 'favorable' ? 0 : 1;
        var pStay = tm[stateIdx][stateIdx];
        var forecasts = [
            { label: '7 days', prob: Math.pow(pStay, 7) },
            { label: '30 days', prob: Math.pow(pStay, 30) },
            { label: '90 days', prob: Math.pow(pStay, 90) },
        ];
        var expectedDur = pStay < 1 ? Math.round(1 / (1 - pStay)) : 999;
        var fColor = cur.state === 'favorable' ? 'var(--color-bullish)' : 'var(--color-bearish)';

        html += '<div class="macro-card"><h3>Regime Forecast ' +
            infoIcon('Probability of staying in the current ' + cur.state + ' regime, derived from the HMM transition matrix. Expected duration = 1/(1-p) where p is the self-transition probability.') +
            '</h3><div class="two-col" style="align-items:start">';

        // Left: progress ring for current staying probability
        html += '<div style="display:flex;flex-direction:column;align-items:center;gap:8px">' +
            progressRing(pStay, 72, 5, fColor, Math.round(pStay * 100) + '%') +
            '<div style="font-size:0.7rem;color:var(--text-muted)">Daily stay prob</div>' +
            '<div style="font-size:0.85rem;font-weight:600;color:' + fColor + '">~' + expectedDur + ' day expected duration</div>' +
        '</div>';

        // Right: forecast bars
        html += '<div style="flex:1;display:flex;flex-direction:column;gap:10px">';
        forecasts.forEach(function(f) {
            var pct = Math.round(f.prob * 100);
            html += '<div>' +
                '<div style="display:flex;justify-content:space-between;margin-bottom:3px;font-size:0.7rem">' +
                    '<span style="color:var(--text-secondary)">Still ' + cur.state + ' in ' + f.label + '</span>' +
                    '<span style="font-weight:600;color:' + fColor + '">' + pct + '%</span>' +
                '</div>' +
                '<div style="height:8px;background:var(--bg-elevated);border-radius:4px;overflow:hidden">' +
                    '<div style="width:' + pct + '%;height:100%;background:' + fColor + ';border-radius:4px;transition:width 0.6s ease"></div>' +
                '</div>' +
            '</div>';
        });
        html += '</div>';

        html += '</div></div>'; // end two-col + macro-card
    }

    // 2. State comparison cards
    html += '<div class="two-col">';
    ['favorable', 'unfavorable'].forEach(function (s) {
        var desc = states[s] || '';
        var m = means[s] || {};
        var pct = hist_pct[s] || 0;
        var cardCls = s === 'favorable' ? 'regime-favorable' : 'regime-unfavorable';
        html += '<div class="macro-card ' + cardCls + '-card">' +
            '<h3>' + s.charAt(0).toUpperCase() + s.slice(1) + '</h3>' +
            (desc ? '<p class="text-secondary">' + escapeHtml(desc) + '</p>' : '') +
            '<div class="regime-stats">';
        if (m.BTC != null) html += '<div class="stat-item"><span class="stat-label">BTC daily mean</span><span class="stat-val">' + (m.BTC * 100).toFixed(3) + '%</span></div>';
        if (m.VIX != null) html += '<div class="stat-item"><span class="stat-label">VIX mean</span><span class="stat-val">' + m.VIX.toFixed(1) + '</span></div>';
        if (m.HY_SPREAD != null) html += '<div class="stat-item"><span class="stat-label">HY Spread mean</span><span class="stat-val">' + m.HY_SPREAD.toFixed(2) + '</span></div>';
        html += '<div class="stat-item"><span class="stat-label">Historical %</span><span class="stat-val">' + (pct * 100).toFixed(0) + '%</span></div>';
        html += '</div></div>';
    });
    html += '</div>';

    // 3. Transition matrix
    if (tm.length >= 2) {
        html += '<div class="macro-card"><h3>Transition Matrix</h3>';
        html += '<div class="transition-grid">';
        html += '<div class="tm-corner"></div><div class="tm-header">Favorable</div><div class="tm-header">Unfavorable</div>';
        html += '<div class="tm-label">Favorable</div>';
        html += '<div class="tm-cell tm-stay">' + (tm[0][0] * 100).toFixed(1) + '%</div>';
        html += '<div class="tm-cell tm-switch">' + (tm[0][1] * 100).toFixed(1) + '%</div>';
        html += '<div class="tm-label">Unfavorable</div>';
        html += '<div class="tm-cell tm-switch">' + (tm[1][0] * 100).toFixed(1) + '%</div>';
        html += '<div class="tm-cell tm-stay">' + (tm[1][1] * 100).toFixed(1) + '%</div>';
        html += '</div>';

        // Expected durations
        var durFav = tm[0][1] > 0 ? Math.round(1 / tm[0][1]) : '?';
        var durUnf = tm[1][0] > 0 ? Math.round(1 / tm[1][0]) : '?';
        html += '<div class="tm-durations">' +
            '<span>Expected favorable duration: ~' + durFav + ' days</span>' +
            '<span>Expected unfavorable duration: ~' + durUnf + ' days</span>' +
        '</div>';
        html += '</div>';
    }

    // 4. PELT changepoints
    if (cps.length) {
        html += '<div class="macro-card"><h3>Structural Changepoints (PELT)</h3>';
        html += '<div class="changepoint-timeline">';
        cps.forEach(function (cp) {
            var date = typeof cp === 'string' ? cp : (cp.date || cp);
            var label = cp.label || '';
            html += '<div class="cp-item">' +
                '<div class="cp-dot"></div>' +
                '<div class="cp-date">' + date + '</div>' +
                (label ? '<div class="cp-label">' + escapeHtml(label) + '</div>' : '') +
            '</div>';
        });
        html += '</div></div>';
    }

    // 5. Regime history + confidence (ECharts)
    var _regimeHistoryData = null;
    if (data.history && data.history.length > 10) {
        _regimeHistoryData = { history: data.history, events: events };
        html += '<div class="macro-card"><h3>Regime History &amp; Confidence ' + infoIcon('Top: regime states over time (green = favorable, red = unfavorable). Bottom: model confidence — dips indicate uncertain transitions. Event markers show macro events.') + '</h3>';
        html += '<div id="echart-regime-history" style="height:220px"></div>';
        html += '</div>';
    }

    setHTML('regime-content', html);

    // Post-render: init regime history ECharts
    if (_regimeHistoryData) {
        initRegimeHistory(_regimeHistoryData.history, _regimeHistoryData.events);
    }
}


// ═══════════════════════════════════════════════════════════════════
// TAB 4: CONDITIONS
// ═══════════════════════════════════════════════════════════════════

function loadConditions() {
    // Wire up the query builder
    var form = el('condition-form');
    if (!form) return;

    // Preselect the target to match the focus asset when it maps to one of
    // the dropdown options (BTC/ETH/SOL/SPX/GOLD/OIL/DXY/TLT/HYG).
    var targetSel = el('cond-target');
    if (targetSel) {
        var focus = (window.INTEL_ASSET || 'BTC').toUpperCase();
        var options = Array.prototype.map.call(targetSel.options, function(o) { return o.value; });
        if (options.indexOf(focus) >= 0) targetSel.value = focus;
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        runConditionQuery();
    });

    // Preset buttons
    el('preset-buttons').addEventListener('click', function (e) {
        var btn = e.target.closest('[data-preset]');
        if (!btn) return;
        var preset = btn.dataset.preset;
        var conds = PRESETS[preset];
        if (!conds) return;
        // Parse and fill form
        var parts = conds.split(',');
        for (var i = 0; i < 3; i++) {
            var row = parts[i] ? parts[i].split(':') : ['', 'gt', ''];
            var selMetric = el('cond-metric-' + i);
            var selOp = el('cond-op-' + i);
            var inpVal = el('cond-val-' + i);
            if (selMetric) selMetric.value = row[0] || '';
            if (selOp) selOp.value = row[1] || 'gt';
            if (inpVal) inpVal.value = row[2] || '';
        }
        runConditionQuery();
    });

    // Load analogues (always visible at bottom)
    loadConditionAnalogues();
}

function runConditionQuery() {
    var conditions = [];
    for (var i = 0; i < 3; i++) {
        var m = el('cond-metric-' + i).value;
        var op = el('cond-op-' + i).value;
        var v = el('cond-val-' + i).value;
        if (m && v) conditions.push(m + ':' + op + ':' + v);
    }
    if (!conditions.length) return;

    var target = el('cond-target').value;
    var horizon = el('cond-horizon').value || '30';

    showLoading('condition-results');

    apiFetch('/api/quant/conditional?detail=true&conditions=' + encodeURIComponent(conditions.join(',')) +
        '&target=' + target + '&horizon=' + horizon)
    .then(function (data) { renderConditionResults(data); })
    .catch(function (err) { showError('condition-results', err.message); });
}

// Last-run conditional query params, used by createConditionalAlert()
var _lastCondQuery = null;

function renderConditionResults(data) {
    var html = '';
    var fr = data.forward_return; // nested stats object

    // Expose last query so the "Save as alert" button can replay it.
    if (data.conditions) {
        _lastCondQuery = {
            conditions: data.conditions,
            target: data.target,
            horizon: data.horizon_days || 30,
        };
    }

    // "Save as alert" button — only enabled when a full query has run
    html += '<div style="margin-bottom:10px">' +
        '<button id="cond-alert-btn" class="alert-btn" ' +
        'title="Notify me when these conditions match on a new day">' +
        '\u2301 Save as alert' +
        '</button></div>';

    // Stats row
    html += '<div class="cond-stats">';
    html += '<div class="stat-item"><span class="stat-label">Instances</span><span class="stat-val">' + (data.instances || 0) + '</span></div>';
    if (fr) {
        var hrColor = fr.hit_rate >= 0.5 ? 'var(--color-bullish)' : 'var(--color-bearish)';
        html += '<div class="stat-item">' + progressRing(fr.hit_rate, 56, 4, hrColor, (fr.hit_rate * 100).toFixed(0) + '%') +
            '<span class="stat-label" style="margin-top:4px">Hit Rate</span></div>';
        html += '<div class="stat-item"><span class="stat-label">Mean Return</span><span class="stat-val ' + retClass(fr.mean) + '">' + fmtPct(fr.mean) + '</span></div>';
        html += '<div class="stat-item"><span class="stat-label">Median Return</span><span class="stat-val ' + retClass(fr.median) + '">' + fmtPct(fr.median) + '</span></div>';
        html += '<div class="stat-item"><span class="stat-label">Std Dev</span><span class="stat-val">' + fmtPct(fr.std) + '</span></div>';
        // Edge vs unconditional baseline
        if (data.unconditional_baseline) {
            var bl = data.unconditional_baseline;
            var edge = fr.median - bl.median;
            var edgeCls = edge >= 0 ? 'text-bullish' : 'text-bearish';
            html += '<div class="stat-item"><span class="stat-label">Baseline Median</span><span class="stat-val">' + fmtPct(bl.median) + '</span></div>';
            html += '<div class="stat-item"><span class="stat-label">Edge</span><span class="stat-val ' + edgeCls + '">' + fmtPct(edge) + '</span></div>';
        }
    }
    if (data.current_match != null) {
        var matchCls = data.current_match ? 'badge-green' : 'badge-red';
        var matchLabel = data.current_match ? 'YES' : 'NO';
        html += '<div class="stat-item"><span class="stat-label">Current Match</span><span class="badge ' + matchCls + '">' + matchLabel + '</span></div>';
    }
    html += '</div>';

    // Distribution histogram from individual forward returns
    if (data.returns && data.returns.length > 3) {
        html += buildReturnHistogram(data.returns, data.unconditional_baseline);
    } else if (fr && fr.min != null && fr.max != null && data.instances > 2) {
        var range = fr.max - fr.min;
        if (range > 0) {
            html += '<div class="cond-range">' +
                '<span class="text-muted">Range: </span>' +
                '<span class="text-bearish">' + fmtPct(fr.min) + '</span>' +
                ' &mdash; ' +
                '<span class="text-bullish">' + fmtPct(fr.max) + '</span>' +
                ' &nbsp; (p25: ' + fmtPct(fr.p25) + ', p75: ' + fmtPct(fr.p75) + ')' +
            '</div>';
        }
    }

    // Instance dates
    if (data.dates && data.dates.length) {
        html += '<details class="macro-card" style="margin-top:12px"><summary>Matching dates (' + data.total_matching_dates + ' total, showing last ' + data.dates.length + ')</summary>';
        html += '<div class="instance-dates">';
        data.dates.forEach(function (d) {
            html += '<span class="date-tag">' + d + '</span>';
        });
        html += '</div></details>';
    }

    setHTML('condition-results', html);

    // Post-render: init ECharts histogram if present
    if (_condHistogramData) {
        initCondHistogram();
        _condHistogramData = null;
    }

    var cBtn = document.getElementById('cond-alert-btn');
    if (cBtn) cBtn.addEventListener('click', createConditionalAlert);
}

function loadConditionAnalogues() {
    showLoading('condition-analogues');
    var asset = (window.INTEL_ASSET || 'BTC').toUpperCase();

    Promise.all([
        apiFetch('/api/quant/zscores?window=90&min_abs=0'),
        apiFetch('/api/quant/analogues?target=' + asset + '&top=5'),
    ]).then(function (results) {
        renderConditionAnalogues(results[0], results[1]);
    }).catch(function (err) {
        showError('condition-analogues', err.message);
    });
}

function renderConditionAnalogues(zscores, analogues) {
    var html = '';

    // Current z-score bars
    html += '<h3>Current Z-Score Vector ' + infoIcon(SECTION_TIPS.zscores) + '</h3>';
    html += '<div class="zscore-bars">';
    var zMap = {};
    (zscores.data || []).forEach(function (z) {
        var key = z.metric || z.asset;
        if (!zMap[key] || Math.abs(z.zscore) > Math.abs(zMap[key])) {
            zMap[key] = z.zscore;
        }
    });

    // Show analogue metrics in order
    var analyticMetrics = ['VIX','HY_SPREAD','YIELD_10Y','YIELD_10Y2Y','M2','FED_FUNDS','FEAR_GREED','COT_BTC_SPEC_NET','FUNDING_BTC'];
    analyticMetrics.forEach(function (m) {
        var z = zMap[m] || 0;
        var width = Math.min(Math.abs(z) * 20, 100);
        var color = z > 0 ? 'var(--color-bearish)' : 'var(--color-bullish)';
        var dir = z >= 0 ? 'right' : 'left';
        var extreme = Math.abs(z) >= 2 ? ' zbar-extreme' : '';
        html += '<div class="zbar-row">' +
            '<span class="zbar-label">' + shortLabel(m) + '</span>' +
            '<div class="zbar-track">' +
                '<div class="zbar-center"></div>' +
                '<div class="zbar-fill zbar-' + dir + extreme + '" data-animate data-width="' + width + '%" style="background:' + color + '"></div>' +
            '</div>' +
            '<span class="zbar-val" style="color:' + color + '">' + fmtZ(z) + '</span>' +
        '</div>';
    });
    html += '</div>';

    // Analogues table
    if (analogues.analogues && analogues.analogues.length) {
        html += '<h3 style="margin-top:20px">Top Analogues ' + infoIcon(SECTION_TIPS.condAnalogues) + '</h3>';
        html += '<table class="data-table"><thead><tr>' +
            '<th>Date</th><th>Sim</th><th>7d</th><th>30d</th><th>90d</th>';
        analyticMetrics.forEach(function (m) {
            html += '<th>' + shortLabel(m) + '</th>';
        });
        html += '</tr></thead><tbody>';

        analogues.analogues.forEach(function (a) {
            var fwd = a.forward_return || {};
            html += '<tr>' +
                '<td>' + a.date + '</td>' +
                '<td>' + (a.similarity * 100).toFixed(0) + '%</td>' +
                '<td class="' + retClass(fwd['7d']) + '">' + fmtPct(fwd['7d']) + '</td>' +
                '<td class="' + retClass(fwd['30d']) + '">' + fmtPct(fwd['30d']) + '</td>' +
                '<td class="' + retClass(fwd['90d']) + '">' + fmtPct(fwd['90d']) + '</td>';
            var zv = a.vector || {};
            analyticMetrics.forEach(function (m) {
                var val = zv[m];
                html += '<td>' + (val != null ? fmtZ(val) : '-') + '</td>';
            });
            html += '</tr>';
        });
        html += '</tbody></table>';
    }

    setHTML('condition-analogues', html);

    // Trigger z-score bar animations after DOM insert
    requestAnimationFrame(function() {
        document.querySelectorAll('.zbar-fill[data-animate]').forEach(function(bar) {
            bar.classList.add('animated');
            bar.style.width = bar.dataset.width;
            bar.removeAttribute('data-animate');
        });
    });
}


// ═══════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════

function fmtR(v) { return (v >= 0 ? '+' : '') + parseFloat(v).toFixed(3); }
function fmtZ(v) { return (v >= 0 ? '+' : '') + parseFloat(v).toFixed(2); }
function fmtPct(v) {
    if (v == null) return '-';
    return (v >= 0 ? '+' : '') + (parseFloat(v) * 100).toFixed(1) + '%';
}
function retClass(v) {
    if (v == null) return '';
    return parseFloat(v) >= 0 ? 'text-bullish' : 'text-bearish';
}

function shortLabel(m) {
    var map = {
        'YIELD_10Y': '10Y', 'YIELD_10Y2Y': '10Y2Y', 'YIELD_10Y3M': '10Y3M',
        'YIELD_2Y': '2Y', 'FED_FUNDS': 'FED', 'FED_BALANCE': 'FedBal',
        'HY_SPREAD': 'HY', 'HY_SPREAD_EU': 'HY_EU', 'BREAKEVEN_10Y': 'BE10Y',
        'NET_LIQUIDITY': 'NetLiq',
        'FUNDING_BTC': 'F_BTC', 'FUNDING_ETH': 'F_ETH',
        'OPTIONS_PC_BTC': 'P/C', 'DVOL_BTC': 'DVOL',
        'FEAR_GREED': 'F&G', 'STABLECOIN_SUPPLY': 'STBL', 'STABLECOIN_DOM': 'StblD',
        'GTREND_BUY_BTC': 'GTRND', 'CROSS_DISPERSION': 'Disp',
        'COT_BTC_SPEC': 'COT', 'COT_BTC_SPEC_NET': 'COT',
        'ETF_BTC_FLOW': 'ETF',
        'NASDAQ': 'NDX', 'COPPER': 'CU', 'EEM': 'EEM',
        'RVOL_BTC': 'RV_B', 'RVOL_ETH': 'RV_E',
        'RISK_APPETITE': 'Risk', 'LIQUIDITY': 'Liq', 'CREDIT_STRESS': 'CrdStr',
        'ALTCOIN_SEASON': 'AltSzn',
    };
    return map[m] || m;
}

// corrColor() — removed, replaced by ECharts visualMap in heatmap

// ── Metrics Browser ───────────────────────────────────────────────
var _metricsFlat = null;

function loadMetrics() {
    if (_metricsFlat) { renderMetrics(_metricsFlat); return; }
    showLoading('metrics-content');
    apiFetch('/api/quant/metrics').then(function(data) {
        // Flatten domain-grouped response into a single array
        var flat = [];
        ['macro', 'price', 'derivatives', 'sentiment'].forEach(function(domain) {
            (data[domain] || []).forEach(function(r) {
                r.domain = domain;
                flat.push(r);
            });
        });
        _metricsFlat = flat;
        renderMetrics(flat);
        var search = el('metrics-search');
        if (search) search.addEventListener('input', function() {
            renderMetrics(_metricsFlat, this.value.toLowerCase());
        });
    }).catch(function(err) { showError('metrics-content', err.message); });
}

function renderMetrics(rows, filter) {
    if (filter) {
        rows = rows.filter(function(r) {
            return (r.metric || '').toLowerCase().indexOf(filter) >= 0
                || (r.domain || '').toLowerCase().indexOf(filter) >= 0
                || (r.asset || '').toLowerCase().indexOf(filter) >= 0;
        });
    }
    var html = '<div class="macro-card"><table class="data-table"><thead><tr>'
        + '<th>Metric</th><th>Domain</th><th>Asset</th><th>Rows</th><th>First</th><th>Latest</th><th></th>'
        + '</tr></thead><tbody>';
    rows.forEach(function(r) {
        var metric = r.metric || '';
        var asset = r.asset || '';
        var domain = r.domain || '';
        var plotVal = domain + ':' + metric + ':' + asset;
        html += '<tr>'
            + '<td><strong>' + escapeHtml(metric || asset) + '</strong></td>'
            + '<td>' + escapeHtml(domain) + '</td>'
            + '<td>' + escapeHtml(asset) + '</td>'
            + '<td>' + (r.n || r.rows || r.count || 0) + '</td>'
            + '<td>' + escapeHtml((r.first_date || r.min_ts || '').substring(0, 10)) + '</td>'
            + '<td>' + escapeHtml((r.last_date || r.max_ts || '').substring(0, 10)) + '</td>'
            + '<td><a href="#" class="metric-plot-link" data-spec="' + escapeHtml(plotVal) + '" style="color:var(--color-accent);font-size:0.7rem">Plot &rarr;</a></td>'
            + '</tr>';
    });
    html += '</tbody></table></div>';
    if (rows.length === 0) html = '<div class="macro-card"><p style="text-align:center;opacity:0.5">No metrics found</p></div>';
    setHTML('metrics-content', html);

    // Wire up plot links
    el('metrics-content').querySelectorAll('.metric-plot-link').forEach(function(link) {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            var spec = this.dataset.spec;
            switchTab('series');
            // Wait for series tab to init, then set value and plot
            setTimeout(function() {
                var sel = el('series-metric-1');
                if (sel) { sel.value = spec; plotSeries(); }
            }, 300);
        });
    });
}

// ── CSV Export Utility ─────────────────────────────────────────────
function downloadCSV(filename, headers, rows) {
    var csv = headers.join(',') + '\n';
    rows.forEach(function(r) {
        csv += r.map(function(v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; }).join(',') + '\n';
    });
    var blob = new Blob([csv], {type: 'text/csv'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
}

function exportCorrelations() {
    if (!_heatmapData) return;
    downloadCSV('correlations.csv',
        ['metric_a', 'metric_b', 'correlation', 'p_value'],
        _heatmapData.map(function(r) { return [r.metric_a, r.metric_b, r.correlation, r.p_value]; })
    );
}

function exportZscores() {
    var rows = _zscoresCache && (_zscoresCache.data || _zscoresCache);
    if (!rows || !Array.isArray(rows)) return;
    downloadCSV('zscores.csv',
        ['metric', 'asset', 'zscore', 'percentile', 'window'],
        rows.map(function(r) { return [r.metric, r.asset, r.zscore, r.percentile, r.window_days]; })
    );
}

function exportAnomalies() {
    if (!_snapshotCache || !_snapshotCache.anomalies) return;
    downloadCSV('anomalies.csv',
        ['domain', 'metric', 'asset', 'zscore', 'percentile'],
        _snapshotCache.anomalies.map(function(r) { return [r.domain, r.metric, r.asset, r.zscore, r.percentile]; })
    );
}

function exportRegimeHistory() {
    if (!_regimeCache || !_regimeCache.history) return;
    downloadCSV('regime_history.csv',
        ['date', 'state', 'probability'],
        _regimeCache.history.map(function(r) { return [r.date, r.state, r.probability]; })
    );
}

// ── Return Distribution Histogram ───────────────────────────────────
function buildReturnHistogram(returns, baseline) {
    var minR = Math.min.apply(null, returns);
    var maxR = Math.max.apply(null, returns);
    var spread = maxR - minR;
    if (spread <= 0) return '';
    var nBins = Math.min(15, Math.max(5, Math.round(Math.sqrt(returns.length))));
    var binWidth = spread / nBins;
    var bins = [];
    for (var i = 0; i < nBins; i++) {
        bins.push({lo: minR + i * binWidth, hi: minR + (i + 1) * binWidth, count: 0});
    }
    returns.forEach(function(r) {
        var idx = Math.min(nBins - 1, Math.max(0, Math.floor((r - minR) / binWidth)));
        bins[idx].count++;
    });

    // Store data for post-render ECharts init
    _condHistogramData = { bins: bins, returns: returns, baseline: baseline };
    return '<div class="macro-card" style="margin-top:12px"><h3 style="font-size:0.85rem;margin-bottom:8px">Forward Return Distribution (' + returns.length + ' instances)</h3>' +
        '<div id="echart-histogram" style="height:220px"></div></div>';
}

var _condHistogramData = null;

function initCondHistogram() {
    if (!_condHistogramData) return;
    var d = _condHistogramData;
    var chart = initChart('echart-histogram');
    if (!chart) return;

    var labels = d.bins.map(function(b) { return fmtPct(b.lo); });
    var counts = d.bins.map(function(b, i) {
        var mid = (b.lo + b.hi) / 2;
        return { value: b.count, itemStyle: { color: mid >= 0 ? 'rgba(107,201,133,0.7)' : 'rgba(239,68,68,0.7)', borderRadius: [2, 2, 0, 0] } };
    });

    var markLines = [];
    // Conditional median
    var sorted = d.returns.slice().sort(function(a, b) { return a - b; });
    var condMedian = sorted[Math.floor(sorted.length / 2)];
    markLines.push({ xAxis: fmtPct(d.bins[Math.min(d.bins.length - 1, Math.max(0, Math.floor((condMedian - d.bins[0].lo) / (d.bins[0].hi - d.bins[0].lo))))].lo), label: { formatter: 'median ' + fmtPct(condMedian), color: '#6bc985', fontSize: 10, position: 'insideStartTop' }, lineStyle: { color: '#6bc985', width: 2, type: 'solid' } });
    // Baseline median
    if (d.baseline && d.baseline.median != null) {
        markLines.push({ xAxis: fmtPct(d.bins[Math.min(d.bins.length - 1, Math.max(0, Math.floor((d.baseline.median - d.bins[0].lo) / (d.bins[0].hi - d.bins[0].lo))))].lo), label: { formatter: 'baseline', color: '#f59e0b', fontSize: 10, position: 'insideStartTop' }, lineStyle: { color: '#f59e0b', width: 1.5, type: 'dashed' } });
    }

    chart.setOption({
        grid: { left: 45, right: 15, top: 20, bottom: 35 },
        xAxis: {
            type: 'category',
            data: labels,
            axisLabel: { fontSize: 9, interval: Math.max(0, Math.floor(d.bins.length / 6) - 1) },
        },
        yAxis: {
            type: 'value',
            minInterval: 1,
            axisLabel: { fontSize: 9 },
        },
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var p = params[0];
                var b = d.bins[p.dataIndex];
                return fmtPct(b.lo) + ' to ' + fmtPct(b.hi) + '<br/><strong>' + b.count + '</strong> instances';
            },
        },
        series: [{
            type: 'bar',
            data: counts,
            barMaxWidth: 30,
            markLine: {
                silent: true,
                symbol: 'none',
                data: markLines,
            },
        }],
    });
}

// ── Time Series Viewer (Tab 7) ─────────────────────────────────────
var _seriesTabInit = false;

function loadSeriesTab() {
    if (_seriesTabInit) return;
    _seriesTabInit = true;

    // Populate dropdowns from /api/quant/metrics (response grouped by domain)
    apiFetch('/api/quant/metrics').then(function(data) {
        var sel1 = el('series-metric-1');
        var sel2 = el('series-metric-2');
        var domains = ['macro', 'price', 'derivatives', 'sentiment'];
        domains.forEach(function(domain) {
            var rows = data[domain];
            if (!Array.isArray(rows)) return;
            rows.forEach(function(r) {
                var metric = r.metric || '';
                var asset = r.asset || '';
                var label = (metric || asset) + (asset && asset !== '_GLOBAL_' && metric ? ' (' + asset + ')' : '');
                if (!label) return;
                var val = domain + ':' + metric + ':' + asset;
                var o1 = document.createElement('option'); o1.value = val; o1.textContent = label;
                var o2 = o1.cloneNode(true);
                sel1.appendChild(o1);
                sel2.appendChild(o2);
            });
        });
    });

    el('series-plot-btn').addEventListener('click', plotSeries);
}

function plotSeries() {
    var sel1 = el('series-metric-1').value;
    if (!sel1) return;
    var sel2 = el('series-metric-2').value;
    var days = parseInt(el('series-range').value) || 0;
    var from = '';
    if (days > 0) {
        var d = new Date(); d.setDate(d.getDate() - days);
        from = d.toISOString().substring(0, 10);
    }

    showLoading('series-chart');

    var fetches = [fetchSeries(sel1, from)];
    if (sel2) fetches.push(fetchSeries(sel2, from));

    Promise.all(fetches).then(function(results) {
        renderSeriesChart(results[0], results[1] || null);
    }).catch(function(err) { showError('series-chart', err.message); });
}

function fetchSeries(spec, from) {
    var parts = spec.split(':');
    var domain = parts[0], metric = parts[1], asset = parts[2] || '';
    var url = '/api/quant/series?domain=' + encodeURIComponent(domain) + '&limit=5000';
    if (metric) url += '&metric=' + encodeURIComponent(metric);
    if (asset && asset !== '_GLOBAL_') url += '&asset=' + encodeURIComponent(asset);
    if (from) url += '&from=' + encodeURIComponent(from);
    return apiFetch(url).then(function(d) {
        return { label: metric + (asset && asset !== '_GLOBAL_' ? ' (' + asset + ')' : ''), data: d.data || [] };
    });
}

function renderSeriesChart(s1, s2) {
    if (!s1.data.length) { setHTML('series-chart', '<p style="text-align:center;color:var(--text-muted)">No data</p>'); return; }

    // Extract and sort chronologically
    var d1 = s1.data.map(function(r) { return {x: (r.ts || r.date || '').substring(0, 10), y: parseFloat(r.value || r.close || 0)}; }).filter(function(r) { return !isNaN(r.y) && r.x; });
    d1.sort(function(a, b) { return a.x < b.x ? -1 : a.x > b.x ? 1 : 0; });
    var d2 = s2 ? s2.data.map(function(r) { return {x: (r.ts || r.date || '').substring(0, 10), y: parseFloat(r.value || r.close || 0)}; }).filter(function(r) { return !isNaN(r.y) && r.x; }) : [];
    if (d2.length) d2.sort(function(a, b) { return a.x < b.x ? -1 : a.x > b.x ? 1 : 0; });

    var chart = initChart('series-chart');
    if (!chart) {
        // Fallback: show text if ECharts unavailable
        setHTML('series-chart', '<p style="text-align:center;color:var(--text-muted)">Charts unavailable</p>');
        return;
    }

    var dates1 = d1.map(function(p) { return p.x; });
    var vals1 = d1.map(function(p) { return p.y; });

    var series = [{
        name: s1.label,
        type: 'line',
        showSymbol: false,
        lineStyle: { width: 1.5 },
        data: vals1,
        yAxisIndex: 0,
    }];

    var yAxes = [{
        type: 'value',
        name: s1.label,
        nameTextStyle: { color: '#6bc985', fontSize: 11 },
        axisLabel: {
            formatter: function(v) { return Math.abs(v) >= 1e6 ? (v/1e6).toFixed(1)+'M' : Math.abs(v) >= 1000 ? (v/1000).toFixed(1)+'k' : v.toFixed(2); }
        },
        splitLine: { lineStyle: { color: '#1e2430' } },
    }];

    if (d2.length > 0) {
        var dates2 = d2.map(function(p) { return p.x; });
        var vals2 = d2.map(function(p) { return p.y; });

        series.push({
            name: s2.label,
            type: 'line',
            showSymbol: false,
            lineStyle: { width: 1.5, type: 'dashed' },
            data: vals2,
            xAxisIndex: 1,
            yAxisIndex: 1,
        });

        yAxes.push({
            type: 'value',
            name: s2.label,
            nameTextStyle: { color: '#58a6ff', fontSize: 11 },
            position: 'right',
            axisLabel: {
                formatter: function(v) { return Math.abs(v) >= 1e6 ? (v/1e6).toFixed(1)+'M' : Math.abs(v) >= 1000 ? (v/1000).toFixed(1)+'k' : v.toFixed(2); }
            },
            splitLine: { show: false },
        });
    }

    var xAxes = [{
        type: 'category',
        data: dates1,
        axisLabel: { rotate: 30, fontSize: 10 },
        boundaryGap: false,
    }];

    if (d2.length > 0) {
        xAxes.push({
            type: 'category',
            data: d2.map(function(p) { return p.x; }),
            show: false,
            boundaryGap: false,
        });
    }

    chart.setOption({
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', label: { backgroundColor: '#242b35' } },
        },
        legend: {
            data: d2.length > 0 ? [s1.label, s2.label] : [s1.label],
            top: 0,
        },
        grid: { left: 60, right: d2.length > 0 ? 60 : 20, top: 40, bottom: 70 },
        xAxis: xAxes,
        yAxis: yAxes,
        series: series,
        dataZoom: [
            { type: 'slider', xAxisIndex: d2.length > 0 ? [0, 1] : [0], bottom: 8, height: 24 },
            { type: 'inside', xAxisIndex: d2.length > 0 ? [0, 1] : [0] },
        ],
        toolbox: {
            right: 16, top: 0,
            feature: { saveAsImage: { title: 'Save', pixelRatio: 2 } },
        },
    });
}

// ═══════════════════════════════════════════════════════════════════
// TAB 8: DATA LAB
// ═══════════════════════════════════════════════════════════════════

var _dlInit = false;
var _dlMetrics = []; // selected metric names
var _dlLastResult = null;

function loadDataLab() {
    if (_dlInit) return;
    _dlInit = true;

    // Populate metric dropdown from canonical metric names (same as METRIC_ORDER + extras)
    var DL_METRICS = [
        'BTC','ETH','SOL','SPX','GOLD','OIL','DXY','TLT','HYG','VIX',
        'FED_FUNDS','YIELD_10Y','YIELD_10Y2Y','M2','NET_LIQUIDITY','HY_SPREAD',
        'FUNDING_BTC','FUNDING_ETH','OPTIONS_PC_BTC','DVOL_BTC','RVOL_BTC','RVOL_ETH',
        'FEAR_GREED','STABLECOIN_SUPPLY','STABLECOIN_DOM','ALTCOIN_SEASON',
        'GTREND_BUY_BTC','COT_BTC_SPEC','ETF_BTC_FLOW','CROSS_DISPERSION',
    ];
    var sel = el('dl-metric-add');
    DL_METRICS.forEach(function(name) {
        var o = document.createElement('option');
        o.value = name;
        o.textContent = name;
        sel.appendChild(o);
    });

    // Add metric on select
    el('dl-metric-add').addEventListener('change', function() {
        var v = this.value;
        if (!v || _dlMetrics.indexOf(v) >= 0) { this.value = ''; return; }
        if (_dlMetrics.length >= 10) { this.value = ''; return; }
        _dlMetrics.push(v);
        this.value = '';
        renderDlTags();
    });

    el('dl-run').addEventListener('click', runPivotQuery);
    el('dl-export').addEventListener('click', exportPivot);
}

function renderDlTags() {
    var html = '';
    _dlMetrics.forEach(function(m, i) {
        html += '<span class="dl-tag">' + escapeHtml(m) +
            ' <span class="dl-tag-x" data-idx="' + i + '">&times;</span></span>';
    });
    setHTML('dl-metric-tags', html);
    // Remove handler
    el('dl-metric-tags').querySelectorAll('.dl-tag-x').forEach(function(x) {
        x.addEventListener('click', function() {
            _dlMetrics.splice(parseInt(this.dataset.idx), 1);
            renderDlTags();
        });
    });
}

function runPivotQuery() {
    if (!_dlMetrics.length) return;
    var group = el('dl-group').value;
    var agg = el('dl-agg').value;
    var from = el('dl-from').value;
    var to = el('dl-to').value;

    var url = '/api/quant/pivot?metrics=' + encodeURIComponent(_dlMetrics.join(','));
    if (group) url += '&group_by=' + group;
    if (agg) url += '&agg=' + agg;
    if (from) url += '&from=' + from;
    if (to) url += '&to=' + to;

    setHTML('dl-table', '<div class="loading-spinner">Loading...</div>');
    setHTML('dl-chart', '<div class="loading-spinner">Loading...</div>');

    apiFetch(url).then(function(data) {
        _dlLastResult = data;
        renderDlTable(data);
        renderDlChart(data);
    }).catch(function(err) {
        showError('dl-table', err.message);
        showError('dl-chart', err.message);
    });
}

function renderDlTable(data) {
    if (!data.groups || !data.groups.length) {
        setHTML('dl-table', '<p class="text-muted" style="text-align:center;padding:1rem">No data</p>');
        return;
    }
    var metrics = data.metrics;
    var html = '<table class="data-table"><thead><tr><th>Group</th><th>N</th>';
    metrics.forEach(function(m) { html += '<th>' + escapeHtml(m) + '</th>'; });
    html += '</tr></thead><tbody>';
    data.groups.forEach(function(g) {
        html += '<tr><td><strong>' + escapeHtml(g.label) + '</strong></td><td>' + g.n + '</td>';
        metrics.forEach(function(m) {
            var v = g[m];
            html += '<td>' + (v != null ? v.toFixed(3) : '-') + '</td>';
        });
        html += '</tr>';
    });
    html += '</tbody></table>';
    html += '<div style="font-size:0.65rem;color:var(--text-muted);margin-top:4px">' + data.total_rows + ' total observations</div>';
    setHTML('dl-table', html);
}

function renderDlChart(data) {
    if (!data.groups || !data.groups.length) {
        setHTML('dl-chart', '<p class="text-muted" style="text-align:center;padding:1rem">No data</p>');
        return;
    }
    var chart = initChart('dl-chart');
    if (!chart) return;

    var labels = data.groups.map(function(g) { return g.label; });
    var metrics = data.metrics;
    var groupBy = data.group_by;

    // Auto chart type
    var chartType = el('dl-chart-type').value;
    if (chartType === 'auto') {
        if (groupBy === 'monthly' || groupBy === 'quarterly') chartType = 'line';
        else if (groupBy === 'regime' || groupBy === 'zscore_bucket') chartType = 'bar';
        else if (metrics.length === 2 && groupBy === 'none') chartType = 'scatter';
        else chartType = 'bar';
    }

    if (chartType === 'scatter' && metrics.length >= 2) {
        // Scatter: first metric X, second metric Y
        var scatterData = data.groups.map(function(g) {
            return [g[metrics[0]] || 0, g[metrics[1]] || 0];
        });
        chart.setOption({
            tooltip: { formatter: function(p) { return metrics[0] + ': ' + p.value[0].toFixed(3) + '<br/>' + metrics[1] + ': ' + p.value[1].toFixed(3); } },
            grid: { left: 50, right: 20, top: 20, bottom: 40 },
            xAxis: { type: 'value', name: metrics[0], nameLocation: 'center', nameGap: 28 },
            yAxis: { type: 'value', name: metrics[1] },
            series: [{ type: 'scatter', data: scatterData, symbolSize: 8 }],
        }, true);
    } else {
        // Line or bar
        var series = metrics.map(function(m) {
            return {
                name: m,
                type: chartType,
                data: data.groups.map(function(g) { return g[m] != null ? g[m] : null; }),
                barMaxWidth: 30,
            };
        });
        chart.setOption({
            tooltip: { trigger: 'axis' },
            legend: { data: metrics, top: 0 },
            grid: { left: 50, right: 20, top: 30, bottom: groupBy === 'monthly' ? 60 : 30 },
            xAxis: {
                type: 'category',
                data: labels,
                axisLabel: { rotate: groupBy === 'monthly' ? 40 : 0, fontSize: 10 },
            },
            yAxis: { type: 'value' },
            series: series,
            dataZoom: labels.length > 24 ? [{ type: 'slider', bottom: 4, height: 20 }] : [],
        }, true);
    }
}

// Expose export functions for onclick handlers in HTML
window.exportCorrelations = exportCorrelations;
window.exportZscores = exportZscores;
window.exportAnomalies = exportAnomalies;
window.exportRegimeHistory = exportRegimeHistory;

function exportPivot() {
    if (!_dlLastResult || !_dlLastResult.groups) return;
    var d = _dlLastResult;
    var headers = ['group', 'n'].concat(d.metrics);
    var rows = d.groups.map(function(g) {
        var row = [g.label, g.n];
        d.metrics.forEach(function(m) { row.push(g[m]); });
        return row;
    });
    downloadCSV('datalab_pivot.csv', headers, rows);
}

})();
