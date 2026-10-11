/**
 * Playbook Analytics — per-setup / archetype-tag / symbol / side /
 * time-of-day expectancy breakdown across closed journal trades
 *.
 *
 * Strictly descriptive — no advice/recommendation language. The server is
 * authoritative on tier gating (`limits.journal_analytics`); this file
 * only renders whatever /api/journal/analytics/playbook returns, either
 * the locked teaser ({locked: true, reason}) or the full breakdown.
 *
 * Standalone module (own IIFE, not journal-page.js) so it can attach its
 * own listeners without touching the existing analytics tab-load logic.
 */
(function() {
'use strict';

var _pbData = null;
var _pbDim = 'by_setup';

function pbFmtNum(v, digits, suffix) {
    if (v === null || v === undefined) return '—';
    return v.toFixed(digits == null ? 2 : digits) + (suffix || '');
}

function pbFmtSigned(v, digits, suffix) {
    if (v === null || v === undefined) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(digits == null ? 2 : digits) + (suffix || '');
}

function loadPlaybookAnalytics() {
    var daysEl = document.getElementById('j-analytics-days');
    var days = daysEl ? daysEl.value : 90;
    fetch('/api/journal/analytics/playbook?days=' + encodeURIComponent(days))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            _pbData = data;
            renderPlaybookAnalytics();
        })
        .catch(function() { /* non-critical panel — fail silent */ });
}

function renderPlaybookAnalytics() {
    var lockedEl = document.getElementById('j-pb-locked');
    var unlockedEl = document.getElementById('j-pb-unlocked');
    if (!lockedEl || !unlockedEl) return;

    if (!_pbData || _pbData.locked) {
        lockedEl.style.display = '';
        unlockedEl.style.display = 'none';
        if (_pbData && _pbData.reason) {
            var textEl = document.getElementById('j-pb-locked-text');
            if (textEl) textEl.textContent = _pbData.reason;
        }
        return;
    }
    lockedEl.style.display = 'none';
    unlockedEl.style.display = '';
    renderPlaybookTable();
}

function renderPlaybookTable() {
    var tbody = document.getElementById('j-pb-tbody');
    var empty = document.getElementById('j-pb-empty');
    if (!tbody || !_pbData) return;

    var groups = _pbData[_pbDim] || {};
    var keys = Object.keys(groups).filter(function(k) { return groups[k] && groups[k].n > 0; });

    if (!keys.length) {
        tbody.innerHTML = '';
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    // Keep the server's key order (setup/tag/symbol/side are insertion
    // order; hour-of-day/day-of-week are chronological) rather than
    // re-sorting by expectancy — R-based and pct-based groups aren't on
    // the same scale, so sorting across bases would be misleading.
    var html = '';
    keys.forEach(function(k) {
        var g = groups[k];
        var isPct = g.basis === 'pct';
        var expStr = isPct
            ? pbFmtSigned(g.expectancy == null ? null : g.expectancy * 100, 1, '%')
            : pbFmtSigned(g.expectancy, 2, 'R');
        var winStr = pbFmtNum(g.win_rate, 1, '%');
        var pfStr = pbFmtNum(g.profit_factor, 2);
        var avgWinStr = isPct
            ? pbFmtNum(g.avg_win == null ? null : g.avg_win * 100, 1, '%')
            : pbFmtNum(g.avg_win, 2, 'R');
        var avgLossStr = isPct
            ? pbFmtNum(g.avg_loss == null ? null : g.avg_loss * 100, 1, '%')
            : pbFmtNum(g.avg_loss, 2, 'R');
        var mfeStr = g.avg_mfe == null ? '—' : pbFmtNum(g.avg_mfe, 2, 'R') + ' (n=' + g.avg_mfe_n + ')';
        var maeStr = g.avg_mae == null ? '—' : pbFmtNum(g.avg_mae, 2, 'R') + ' (n=' + g.avg_mae_n + ')';

        var maxCount = 1;
        (g.r_distribution || []).forEach(function(b) { maxCount = Math.max(maxCount, b.count); });
        var bars = (g.r_distribution || []).map(function(b) {
            var h = Math.max(2, Math.round((b.count / maxCount) * 20));
            var cls = (b.bucket.indexOf('<=') === 0 || b.bucket.indexOf('-') === 0) ? 'neg' : 'pos';
            var unitLabel = isPct ? b.bucket.replace('R', '') + ' x100%' : b.bucket;
            return '<div class="j-pb-mini-bar ' + cls + '" style="height:' + h + 'px" title="' +
                escapeHtml(unitLabel) + ': ' + b.count + ' trade' + (b.count === 1 ? '' : 's') + '"></div>';
        }).join('');

        var smallFlag = g.small_sample
            ? '<span class="j-pb-small-flag" title="Small sample (n&lt;20) — descriptive only">*</span>'
            : '';
        var basisTag = isPct
            ? ' <span style="color:#8b949e;font-size:0.55rem" title="No stop recorded for at least one trade in this group — shown as percent return instead of R">(pct-based)</span>'
            : '';

        html += '<tr>';
        html += '<td>' + escapeHtml(k) + '</td>';
        html += '<td>' + g.n + smallFlag + '</td>';
        html += '<td>' + winStr + '</td>';
        html += '<td>' + expStr + basisTag + '</td>';
        html += '<td>' + pfStr + '</td>';
        html += '<td>' + avgWinStr + '</td>';
        html += '<td>' + avgLossStr + '</td>';
        html += '<td>' + mfeStr + '</td>';
        html += '<td>' + maeStr + '</td>';
        html += '<td><div class="j-pb-mini-bars">' + bars + '</div></td>';
        html += '</tr>';
    });
    tbody.innerHTML = html;
}

function initDimTabs() {
    var tabs = document.querySelectorAll('.j-pb-dim-tab');
    tabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
            tabs.forEach(function(t) { t.classList.remove('active'); });
            tab.classList.add('active');
            _pbDim = tab.getAttribute('data-dim');
            renderPlaybookTable();
        });
    });
}

function init() {
    initDimTabs();

    // Refresh whenever the shared analytics period selector changes...
    var daysEl = document.getElementById('j-analytics-days');
    if (daysEl) daysEl.addEventListener('change', loadPlaybookAnalytics);

    // ...and whenever the Analytics tab is (re)activated.
    document.querySelectorAll('.j-tab').forEach(function(tab) {
        if (tab.getAttribute('data-tab') === 'analytics') {
            tab.addEventListener('click', loadPlaybookAnalytics);
        }
    });

    // In case the Analytics tab is already active on page load.
    var panel = document.getElementById('j-panel-analytics');
    if (panel && panel.classList.contains('active')) {
        loadPlaybookAnalytics();
    }
}

document.addEventListener('DOMContentLoaded', init);

})();
