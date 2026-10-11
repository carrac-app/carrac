/**
 * Journal — playbooks, goals and CSV/JSON export.
 *
 * Extracted VERBATIM from journal-page.js. Measured first: these three
 * sections need exactly three things from the page core (api,
 * closeModal, MONTH_NAMES), each declared once and never reassigned. They
 * are otherwise self-contained — _playbooks and _goalsMonth were already
 * private to them.
 *
 * Own IIFE, same pattern as journal-playbook-analytics.js. journal-page.js
 * is deferred and listed FIRST in journal.html, so window.JournalCore is
 * populated before this file runs.
 */
(function() {
'use strict';

var C = window.JournalCore;
var api = C.api, closeModal = C.closeModal;
var MONTH_NAMES = C.MONTH_NAMES;

// =============================================
// ======== PLAYBOOKS ========
// =============================================

var _playbooks = [];

function loadPlaybooks() {
    api('GET', '/api/journal/playbooks').then(function(data) {
        _playbooks = data || [];
        renderPlaybooks();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function renderPlaybooks() {
    var container = document.getElementById('j-playbook-list');
    var empty = document.getElementById('j-playbook-empty');

    if (!_playbooks.length) {
        container.innerHTML = '';
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    var html = '';
    _playbooks.forEach(function(pb) {
        html += '<div class="j-pb-card" data-pb-id="' + pb.id + '">';
        html += '<div class="j-pb-header">';
        html += '<div><span class="j-pb-name">' + escapeHtml(pb.name) + '</span>';
        if (pb.setup_type) html += ' <span class="j-pb-setup">' + escapeHtml(pb.setup_type) + '</span>';
        html += '</div>';
        html += '<div style="display:flex;gap:4px">';
        html += '<button class="j-row-btn" data-action="edit-pb" data-id="' + pb.id + '">Edit</button>';
        html += '<button class="j-row-btn delete-btn" data-action="delete-pb" data-id="' + pb.id + '">Del</button>';
        html += '</div></div>';

        html += '<div class="j-pb-body">';
        if (pb.description) html += '<div>' + escapeHtml(pb.description) + '</div>';
        if (pb.ideal_conditions) html += '<strong>When:</strong><div>' + escapeHtml(pb.ideal_conditions) + '</div>';
        if (pb.entry_rules) html += '<strong>Entry:</strong><div>' + escapeHtml(pb.entry_rules) + '</div>';
        if (pb.exit_rules) html += '<strong>Exit:</strong><div>' + escapeHtml(pb.exit_rules) + '</div>';
        html += '</div>';

        html += '<div class="j-pb-stats" id="j-pb-stats-' + pb.id + '">Loading stats...</div>';
        html += '</div>';
    });

    container.innerHTML = html;

    // Load stats for each playbook
    _playbooks.forEach(function(pb) {
        if (pb.setup_type) {
            api('GET', '/api/journal/playbooks/' + pb.id + '/stats').then(function(stats) {
                var el = document.getElementById('j-pb-stats-' + pb.id);
                if (!el || !stats.trades) { if (el) el.innerHTML = '<span style="color:#484f58">No trades yet</span>'; return; }
                el.innerHTML = '<span>' + stats.trades + '</span> trades · ' +
                    '<span>' + stats.win_rate + '%</span> WR · ' +
                    'Net: <span class="' + (stats.net_r >= 0 ? 'positive' : 'negative') + '">' + (stats.net_r >= 0 ? '+' : '') + stats.net_r.toFixed(2) + 'R</span> · ' +
                    'Avg: <span>' + (stats.avg_r >= 0 ? '+' : '') + stats.avg_r.toFixed(2) + 'R</span>';
            }).catch(function() {
                var el = document.getElementById('j-pb-stats-' + pb.id);
                if (el) el.innerHTML = '';
            });
        } else {
            var el = document.getElementById('j-pb-stats-' + pb.id);
            if (el) el.innerHTML = '<span style="color:#484f58">Set a setup type to see stats</span>';
        }
    });

    // Event delegation
    container.querySelectorAll('[data-action]').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var action = btn.getAttribute('data-action');
            var id = parseInt(btn.getAttribute('data-id'));
            if (action === 'edit-pb') editPlaybook(id);
            else if (action === 'delete-pb') deletePlaybook(id);
        });
    });
}

function openNewPlaybookModal() {
    document.getElementById('j-pb-modal-title').textContent = 'New Playbook';
    document.getElementById('j-pb-form-id').value = '';
    document.getElementById('j-pb-form-name').value = '';
    document.getElementById('j-pb-form-setup').value = '';
    document.getElementById('j-pb-form-desc').value = '';
    document.getElementById('j-pb-form-conditions').value = '';
    document.getElementById('j-pb-form-entry').value = '';
    document.getElementById('j-pb-form-exit').value = '';
    document.getElementById('j-modal-playbook').classList.add('active');
}

function editPlaybook(id) {
    var pb = _playbooks.find(function(x) { return x.id === id; });
    if (!pb) return;
    document.getElementById('j-pb-modal-title').textContent = 'Edit Playbook';
    document.getElementById('j-pb-form-id').value = pb.id;
    document.getElementById('j-pb-form-name').value = pb.name || '';
    document.getElementById('j-pb-form-setup').value = pb.setup_type || '';
    document.getElementById('j-pb-form-desc').value = pb.description || '';
    document.getElementById('j-pb-form-conditions').value = pb.ideal_conditions || '';
    document.getElementById('j-pb-form-entry').value = pb.entry_rules || '';
    document.getElementById('j-pb-form-exit').value = pb.exit_rules || '';
    document.getElementById('j-modal-playbook').classList.add('active');
}

function savePlaybook() {
    var editId = document.getElementById('j-pb-form-id').value;
    var body = {
        name: document.getElementById('j-pb-form-name').value.trim(),
        setup_type: document.getElementById('j-pb-form-setup').value.trim() || null,
        description: document.getElementById('j-pb-form-desc').value.trim() || null,
        ideal_conditions: document.getElementById('j-pb-form-conditions').value.trim() || null,
        entry_rules: document.getElementById('j-pb-form-entry').value.trim() || null,
        exit_rules: document.getElementById('j-pb-form-exit').value.trim() || null,
    };
    if (!body.name) { showToast('Name is required', 'error'); return; }

    var method = editId ? 'PATCH' : 'POST';
    var url = editId ? '/api/journal/playbooks/' + editId : '/api/journal/playbooks';

    api(method, url, body).then(function() {
        closeModal('j-modal-playbook');
        showToast(editId ? 'Playbook updated' : 'Playbook created', 'success');
        loadPlaybooks();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function deletePlaybook(id) {
    if (!confirm('Delete this playbook?')) return;
    api('DELETE', '/api/journal/playbooks/' + id).then(function() {
        showToast('Playbook deleted', 'success');
        loadPlaybooks();
    }).catch(function(e) { showToast(e.message, 'error'); });
}


// =============================================
// ======== GOALS ========
// =============================================

var _goalsMonth;

function initGoals() {
    var now = new Date();
    _goalsMonth = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0');

    var sel = document.getElementById('j-goals-month');
    for (var m = 0; m < 12; m++) {
        var yr = now.getFullYear();
        var mo = now.getMonth() + 1 - m;
        if (mo < 1) { mo += 12; yr--; }
        var opt = document.createElement('option');
        opt.value = yr + '-' + String(mo).padStart(2, '0');
        opt.textContent = MONTH_NAMES[mo - 1] + ' ' + yr;
        if (m === 0) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.addEventListener('change', function() { _goalsMonth = this.value; loadGoals(); });
}

function loadGoals() {
    api('GET', '/api/journal/goals?month=' + _goalsMonth).then(function(data) {
        renderGoals(data);
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}

function renderGoals(data) {
    var el = document.getElementById('j-goals-display');
    var goals = data.goals;
    var progress = data.progress || {};

    if (!goals || !goals.goals) {
        el.innerHTML = '<div style="font-size:0.65rem;color:#484f58;padding:8px 0">No goals set for this month. Click Edit Goals to set them.</div>';
        return;
    }

    var g;
    try { g = typeof goals.goals === 'string' ? JSON.parse(goals.goals) : goals.goals; }
    catch(e) { el.innerHTML = ''; return; }

    var html = '';

    if (g.max_daily_loss_r) {
        var actual = Math.abs(progress.max_daily_loss_r || 0);
        var cls = actual <= g.max_daily_loss_r ? 'j-goal-ok' : 'j-goal-fail';
        var ratio = g.max_daily_loss_r > 0 ? Math.min(actual / g.max_daily_loss_r, 1) : 0;
        html += goalRow('Max Daily Loss', g.max_daily_loss_r + 'R limit', actual.toFixed(2) + 'R worst', cls, ratio);
    }
    if (g.max_trades_per_day) {
        var actual = progress.max_trades_per_day || 0;
        var cls = actual <= g.max_trades_per_day ? 'j-goal-ok' : 'j-goal-fail';
        var ratio = g.max_trades_per_day > 0 ? Math.min(actual / g.max_trades_per_day, 1) : 0;
        html += goalRow('Max Trades/Day', g.max_trades_per_day + ' limit', actual + ' max', cls, ratio);
    }
    if (g.min_rr_target) {
        var actual = progress.avg_rr || 0;
        var cls = actual >= g.min_rr_target ? 'j-goal-ok' : actual >= 0 ? 'j-goal-warn' : 'j-goal-fail';
        var ratio = g.min_rr_target > 0 ? Math.min(Math.max(actual, 0) / g.min_rr_target, 1) : 0;
        html += goalRow('Min R:R Target', g.min_rr_target + 'R', actual.toFixed(2) + 'R avg', cls, ratio);
    }
    if (g.max_consecutive_losses_before_stop) {
        var actual = progress.max_consecutive_losses || 0;
        var cls = actual <= g.max_consecutive_losses_before_stop ? 'j-goal-ok' : 'j-goal-fail';
        var ratio = g.max_consecutive_losses_before_stop > 0 ? Math.min(actual / g.max_consecutive_losses_before_stop, 1) : 0;
        html += goalRow('Max Consec. Losses', g.max_consecutive_losses_before_stop + ' limit', actual + ' max', cls, ratio);
    }

    html += '<div style="margin-top:8px;font-size:0.6rem;color:#484f58">' +
            progress.trades + ' trades · ' + progress.trading_days + ' trading days · ' +
            'Net: ' + (progress.net_r >= 0 ? '+' : '') + (progress.net_r || 0).toFixed(2) + 'R</div>';

    el.innerHTML = html;
}

function goalRow(label, target, actual, cls, ratio) {
    var barColor = cls === 'j-goal-ok' ? '#3fb950' : cls === 'j-goal-warn' ? '#d29922' : '#ef5350';
    var pct = Math.round((ratio || 0) * 100);
    return '<div class="j-goal-row">' +
        '<div class="j-goal-label">' + label + '</div>' +
        '<div class="j-goal-bar"><div class="j-goal-bar-fill" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
        '<div class="j-goal-target">' + target + '</div>' +
        '<div class="j-goal-actual ' + cls + '">' + actual + '</div></div>';
}

function openGoalsModal() {
    // Pre-fill from current goals
    api('GET', '/api/journal/goals?month=' + _goalsMonth).then(function(data) {
        var g = {};
        if (data.goals && data.goals.goals) {
            try { g = typeof data.goals.goals === 'string' ? JSON.parse(data.goals.goals) : data.goals.goals; }
            catch(e) { g = {}; }
        }
        document.getElementById('j-goal-max-loss').value = g.max_daily_loss_r || '';
        document.getElementById('j-goal-max-trades').value = g.max_trades_per_day || '';
        document.getElementById('j-goal-min-rr').value = g.min_rr_target || '';
        document.getElementById('j-goal-max-consec').value = g.max_consecutive_losses_before_stop || '';
        document.getElementById('j-modal-goals').classList.add('active');
    }).catch(function() {
        document.getElementById('j-modal-goals').classList.add('active');
    });
}

function saveGoals() {
    var goals = {};
    var v;
    v = parseFloat(document.getElementById('j-goal-max-loss').value);
    if (v > 0) goals.max_daily_loss_r = v;
    v = parseInt(document.getElementById('j-goal-max-trades').value);
    if (v > 0) goals.max_trades_per_day = v;
    v = parseFloat(document.getElementById('j-goal-min-rr').value);
    if (v > 0) goals.min_rr_target = v;
    v = parseInt(document.getElementById('j-goal-max-consec').value);
    if (v > 0) goals.max_consecutive_losses_before_stop = v;

    api('POST', '/api/journal/goals', {month: _goalsMonth, goals: goals}).then(function() {
        closeModal('j-modal-goals');
        showToast('Goals saved', 'success');
        loadGoals();
    }).catch(function(e) { showToast(e.message, 'error'); });
}


// =============================================
// ======== EXPORT ========
// =============================================

function exportCSV() {
    if (!window._isPremium) { if (typeof showUpgradeModal === 'function') showUpgradeModal('Data export requires a subscription'); return; }
    var days = document.getElementById('j-filter-days').value;
    window.open('/api/journal/export/csv?days=' + days, '_blank');
}

function exportJSON() {
    if (!window._isPremium) { if (typeof showUpgradeModal === 'function') showUpgradeModal('Data export requires a subscription'); return; }
    window.open('/api/journal/export/json', '_blank');
}

window.JournalPlaybooksGoals = {
    loadPlaybooks: loadPlaybooks,
    openNewPlaybookModal: openNewPlaybookModal,
    savePlaybook: savePlaybook,
    initGoals: initGoals,
    loadGoals: loadGoals,
    openGoalsModal: openGoalsModal,
    saveGoals: saveGoals,
    exportCSV: exportCSV,
    exportJSON: exportJSON
};

})();
