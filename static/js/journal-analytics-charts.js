/**
 * Journal — equity curve + analytics tab.
 *
 * Extracted VERBATIM from journal-page.js. Both sections were measured
 * first: between them they need exactly two things from the page core
 * (api, kpi), each declared once and never reassigned, so the
 * aliases below are safe to bind at load time. Nothing else in the page
 * reaches into these sections — their only exports are the two loaders,
 * called from journal-page.js init().
 *
 * Own IIFE, same pattern as journal-playbook-analytics.js. journal-page.js
 * is deferred and listed FIRST in journal.html, so window.JournalCore is
 * populated before this file runs.
 */
(function() {
'use strict';

var C = window.JournalCore;
var api = C.api, kpi = C.kpi;

// =============================================
// ======== EQUITY CURVE ========
// =============================================

function loadEquityCurve() {
    var days = document.getElementById('j-equity-days').value;
    api('GET', '/api/journal/equity-curve?days=' + days).then(function(data) {
        var curve = data.curve || [];
        var wrap = document.getElementById('j-equity-wrap');
        if (curve.length < 2) { wrap.style.display = 'none'; return; }
        wrap.style.display = '';
        drawEquityCurve(curve);
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}

function drawEquityCurve(curve) {
    var canvas = document.getElementById('j-equity-canvas');
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    var W = rect.width, H = rect.height;
    var pad = {t: 10, r: 10, b: 24, l: 46};
    var cW = W - pad.l - pad.r, cH = H - pad.t - pad.b;

    // Data
    var rValues = curve.map(function(p) { return p.cumulative_r; });
    var ddValues = curve.map(function(p) { return p.drawdown; });
    var minR = Math.min(0, Math.min.apply(null, rValues), Math.min.apply(null, ddValues));
    var maxR = Math.max(0, Math.max.apply(null, rValues));
    var range = maxR - minR || 1;

    function yPos(val) { return pad.t + cH - ((val - minR) / range) * cH; }
    function xPos(i) { return pad.l + (i / (curve.length - 1)) * cW; }

    // Clear
    ctx.clearRect(0, 0, W, H);

    // Zero line
    var zeroY = yPos(0);
    ctx.strokeStyle = '#30363d';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.l, zeroY);
    ctx.lineTo(pad.l + cW, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Y axis labels
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.fillStyle = '#484f58';
    ctx.textAlign = 'right';
    var steps = 4;
    for (var s = 0; s <= steps; s++) {
        var val = minR + (range / steps) * s;
        var y = yPos(val);
        ctx.fillText(val.toFixed(1) + 'R', pad.l - 4, y + 3);
        if (s > 0 && s < steps) {
            ctx.strokeStyle = '#1b1f24';
            ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cW, y); ctx.stroke();
        }
    }

    // Drawdown area (red, between equity peak and drawdown)
    // Reconstruct the peak line (running maximum of cumulative_r)
    var peakValues = [];
    var runPeak = 0;
    for (var i = 0; i < curve.length; i++) {
        runPeak = Math.max(runPeak, rValues[i]);
        peakValues.push(runPeak);
    }
    ctx.beginPath();
    ctx.moveTo(xPos(0), yPos(peakValues[0]));
    for (var i = 0; i < curve.length; i++) {
        ctx.lineTo(xPos(i), yPos(peakValues[i]));
    }
    // Trace back along the equity line
    for (var i = curve.length - 1; i >= 0; i--) {
        ctx.lineTo(xPos(i), yPos(rValues[i]));
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(239, 83, 80, 0.12)';
    ctx.fill();

    // Equity area fill
    ctx.beginPath();
    ctx.moveTo(xPos(0), yPos(0));
    for (var i = 0; i < curve.length; i++) {
        ctx.lineTo(xPos(i), yPos(rValues[i]));
    }
    ctx.lineTo(xPos(curve.length - 1), yPos(0));
    ctx.closePath();
    var lastR = rValues[rValues.length - 1];
    ctx.fillStyle = lastR >= 0 ? 'rgba(63, 185, 80, 0.08)' : 'rgba(239, 83, 80, 0.08)';
    ctx.fill();

    // Equity line
    ctx.beginPath();
    ctx.moveTo(xPos(0), yPos(rValues[0]));
    for (var i = 1; i < curve.length; i++) {
        ctx.lineTo(xPos(i), yPos(rValues[i]));
    }
    ctx.strokeStyle = lastR >= 0 ? '#3fb950' : '#ef5350';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // X-axis labels (first, mid, last dates)
    ctx.fillStyle = '#484f58';
    ctx.textAlign = 'center';
    ctx.font = '9px JetBrains Mono, monospace';
    [0, Math.floor(curve.length / 2), curve.length - 1].forEach(function(idx) {
        var d = curve[idx].closed_at;
        if (d) {
            var label = d.substring(5, 10); // MM-DD
            ctx.fillText(label, xPos(idx), H - 4);
        }
    });

    // End value label
    ctx.font = '11px JetBrains Mono, monospace';
    ctx.fillStyle = lastR >= 0 ? '#3fb950' : '#ef5350';
    ctx.textAlign = 'left';
    ctx.fillText((lastR >= 0 ? '+' : '') + lastR.toFixed(2) + 'R', xPos(curve.length - 1) + 4, yPos(lastR) + 4);
}

// =============================================
// ======== ANALYTICS ========
// =============================================

var _analyticsData = null;

function loadAnalytics() {
    var days = document.getElementById('j-analytics-days').value;
    api('GET', '/api/journal/analytics?days=' + days).then(function(data) {
        _analyticsData = data;
        renderAnalytics(data);
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function renderAnalytics(data) {
    var empty = document.getElementById('j-analytics-empty');
    if (!data || data.trades < 1) {
        empty.style.display = '';
        document.getElementById('j-analytics-kpis').innerHTML = '';
        return;
    }
    empty.style.display = 'none';

    // KPI cards
    var wr = data.win_rate || 0;
    var exp = data.expectancy || 0;
    var pf = data.profit_factor;
    var avgR = data.avg_rr || 0;
    var maxDD = data.max_drawdown || 0;
    var kpis = document.getElementById('j-analytics-kpis');
    kpis.innerHTML =
        kpi('Win Rate', wr.toFixed(1) + '%', wr >= 50 ? 'positive' : wr > 0 ? 'negative' : '') +
        kpi('Expectancy', (exp >= 0 ? '+' : '') + exp.toFixed(3) + 'R', exp >= 0 ? 'positive' : 'negative') +
        kpi('Profit Factor', pf != null ? pf.toFixed(2) : '-', pf != null && pf >= 1 ? 'positive' : 'negative') +
        kpi('Avg R', (avgR >= 0 ? '+' : '') + avgR.toFixed(2), avgR >= 0 ? 'positive' : 'negative') +
        kpi('Max DD', maxDD.toFixed(2) + 'R', 'negative') +
        kpi('Trades', data.trades) +
        kpi('Avg Hold', data.avg_hold_hours != null ? formatHoldTime(data.avg_hold_hours) : '-');

    // R Histogram (ECharts)
    if (data.r_histogram && data.r_histogram.length && typeof renderRHistogram === 'function') {
        renderRHistogram('j-chart-r-hist', data.r_histogram, data.avg_rr);
    }

    // MFE/MAE scatter (ECharts)
    if (data.mfe_mae && data.mfe_mae.length && typeof renderMfeMaeScatter === 'function') {
        renderMfeMaeScatter('j-chart-mfe-mae', data.mfe_mae);
    } else {
        var mfeEl = document.getElementById('j-chart-mfe-mae');
        if (mfeEl) mfeEl.innerHTML = '<div class="j-bd-empty" style="text-align:center;padding:40px 0">No MFE/MAE data yet. Close trades with stop-loss set to get this data.</div>';
    }

    // Breakdowns
    renderBreakdown('j-bd-setup', data.by_setup, 'Setup');
    renderBreakdown('j-bd-asset', data.by_asset, 'Asset');
    renderBreakdown('j-bd-direction', data.by_direction, 'Dir');
    renderBreakdown('j-bd-weekday', data.by_weekday, 'Day', ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']);
    renderBreakdown('j-bd-hour', data.by_hour, 'Hour', ['00-04','04-08','08-12','12-16','16-20','20-24']);
    renderBreakdown('j-bd-duration', data.by_duration, 'Type', ['scalp','intraday','swing','position']);

    // Rule adherence
    renderAdherence('j-bd-adherence', data.rule_adherence);

    // Streak analysis
    renderStreaks('j-streak-analysis', data);
}

function renderBreakdown(containerId, breakdownData, label, keyOrder) {
    var el = document.getElementById(containerId);
    if (!el) return;
    if (!breakdownData || !Object.keys(breakdownData).length) {
        el.innerHTML = '<div class="j-bd-empty">No data</div>';
        return;
    }

    var keys = keyOrder || Object.keys(breakdownData).sort(function(a, b) {
        return (breakdownData[b].net_rr || 0) - (breakdownData[a].net_rr || 0);
    });

    // Find max absolute net_rr for bar scaling
    var maxAbs = 1;
    keys.forEach(function(k) {
        var d = breakdownData[k];
        if (d) maxAbs = Math.max(maxAbs, Math.abs(d.net_rr || 0));
    });

    var html = '';
    keys.forEach(function(k) {
        var d = breakdownData[k];
        if (!d || d.trades === 0) return;
        var pct = Math.abs(d.net_rr || 0) / maxAbs * 100;
        var cls = (d.net_rr || 0) >= 0 ? 'positive' : 'negative';
        var expCls = (d.expectancy || 0) >= 0 ? 'positive' : 'negative';
        html += '<div class="j-bd-row">';
        html += '<div class="j-bd-label" title="' + escapeHtml(k) + '">' + escapeHtml(k) + '</div>';
        html += '<div class="j-bd-bar-wrap"><div class="j-bd-bar ' + cls + '" style="width:' + pct.toFixed(1) + '%"></div></div>';
        html += '<div class="j-bd-stats">';
        html += '<span>' + d.trades + '</span>T';
        html += ' <span class="' + cls + '">' + (d.net_rr >= 0 ? '+' : '') + d.net_rr.toFixed(2) + 'R</span>';
        html += ' ' + d.win_rate.toFixed(0) + '%';
        html += ' E:<span class="' + expCls + '">' + (d.expectancy >= 0 ? '+' : '') + d.expectancy.toFixed(2) + '</span>';
        html += '</div></div>';
    });

    el.innerHTML = html || '<div class="j-bd-empty">No data</div>';
}

function renderAdherence(containerId, adherenceData) {
    var el = document.getElementById(containerId);
    if (!el) return;
    if (!adherenceData) { el.innerHTML = '<div class="j-bd-empty">No checklist data</div>'; return; }

    var labels = {full: 'Full checklist', partial: 'Partial', none: 'No checklist'};
    var keys = ['full', 'partial', 'none'];
    var html = '';
    var hasData = false;

    keys.forEach(function(k) {
        var d = adherenceData[k];
        if (!d || d.trades === 0) return;
        hasData = true;
        var expCls = (d.expectancy || 0) >= 0 ? 'positive' : 'negative';
        var wrCls = (d.win_rate || 0) >= 50 ? 'positive' : 'negative';
        html += '<div class="j-bd-row">';
        html += '<div class="j-bd-label">' + labels[k] + '</div>';
        html += '<div class="j-bd-stats" style="flex:1;justify-content:flex-start;gap:12px">';
        html += '<span>' + d.trades + '</span> trades';
        html += ' WR: <span class="' + wrCls + '">' + d.win_rate.toFixed(1) + '%</span>';
        html += ' Avg: <span class="' + (d.avg_rr >= 0 ? 'positive' : 'negative') + '">' + (d.avg_rr >= 0 ? '+' : '') + d.avg_rr.toFixed(2) + 'R</span>';
        html += ' Exp: <span class="' + expCls + '">' + (d.expectancy >= 0 ? '+' : '') + d.expectancy.toFixed(3) + '</span>';
        html += '</div></div>';
    });

    el.innerHTML = hasData ? html : '<div class="j-bd-empty">No checklist data yet. Create checklists and use them when logging trades.</div>';
}

function renderStreaks(containerId, data) {
    var el = document.getElementById(containerId);
    if (!el) return;

    var streaks = data.streak_history || [];
    var cur = data.current_streak || {};

    if (!streaks.length) {
        el.innerHTML = '<div class="j-bd-empty">No streak data</div>';
        return;
    }

    // Streak bar chart
    var maxCount = Math.max.apply(null, streaks.map(function(s) { return s.count; }).concat([1]));
    var html = '<div class="j-streak-bars">';
    streaks.forEach(function(s) {
        var h = Math.max(4, (s.count / maxCount) * 55);
        html += '<div class="j-streak-bar ' + s.type + '" style="height:' + h + 'px" title="' + s.count + ' ' + s.type + (s.count > 1 ? 's' : '') + '"></div>';
    });
    html += '</div>';

    // Meta
    html += '<div class="j-streak-meta">';
    if (cur.type && cur.type !== 'none') {
        var cls = cur.type === 'win' ? 'positive' : 'negative';
        html += '<div>Current: <span class="' + cls + '">' + cur.count + ' ' + cur.type + (cur.count > 1 ? 's' : '') + '</span></div>';
    }
    html += '<div>Best win: <span class="positive">' + (data.longest_win_streak || 0) + '</span></div>';
    html += '<div>Worst loss: <span class="negative">' + (data.longest_loss_streak || 0) + '</span></div>';
    html += '</div>';

    el.innerHTML = html;
}

function formatHoldTime(hours) {
    if (hours < 1) return Math.round(hours * 60) + 'm';
    if (hours < 24) return hours.toFixed(1) + 'h';
    return (hours / 24).toFixed(1) + 'd';
}

window.JournalAnalyticsCharts = {
    loadEquityCurve: loadEquityCurve,
    loadAnalytics: loadAnalytics
};

})();
