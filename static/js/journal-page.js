/**
 * Journal Page — Trade list, filtering, log/close forms, bulk operations.
 */
(function() {
'use strict';

var _trades = [];       // all trades (open + closed)
var _selected = new Set();
var _sortField = 'opened_at';
var _sortAsc = false;
var _mistakeTags = [];
var _checklists = {};   // setup_type -> checklist
var _setupTypes = new Set();

var CSRF = document.querySelector('meta[name="csrf-token"]').getAttribute('content');

// ── API helpers ──

function api(method, url, body) {
    var opts = {
        method: method,
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF},
    };
    if (body) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function(r) {
        if (!r.ok) return r.json().then(function(e) { throw new Error(e.error || 'Request failed'); });
        return r.json();
    });
}

// ── Data loading ──

function loadTrades() {
    var days = document.getElementById('j-filter-days').value;
    var empty = document.getElementById('j-empty');
    if (!_trades.length) empty.style.display = '';
    empty.textContent = 'Loading...';
    api('GET', '/api/journal/trades?days=' + days + '&limit=500').then(function(data) {
        _trades = (data.open || []).concat(data.history || []);
        // Collect setup types for datalist
        _setupTypes.clear();
        _trades.forEach(function(t) { if (t.setup_type) _setupTypes.add(t.setup_type); });
        populateSetupDatalist();
        renderKPIs(data.stats);
        renderTable();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function loadMistakeTags() {
    api('GET', '/api/journal/mistake-tags').then(function(data) {
        _mistakeTags = data.tags || [];
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}

function loadChecklists() {
    api('GET', '/api/journal/checklists').then(function(data) {
        _checklists = {};
        (data || []).forEach(function(c) { _checklists[c.setup_type] = c; });
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}

// ── KPI rendering ──

function renderKPIs(stats) {
    if (!stats) return;
    var bar = document.getElementById('j-kpi-bar');
    var wr = stats.win_rate || 0;
    var avgRR = stats.avg_rr || 0;
    bar.innerHTML =
        kpi('Trades', stats.trades) +
        kpi('Wins', stats.wins, 'positive') +
        kpi('Losses', stats.losses, 'negative') +
        kpi('Win Rate', wr.toFixed(1) + '%', wr >= 50 ? 'positive' : wr > 0 ? 'negative' : '') +
        kpi('Avg R', (avgRR >= 0 ? '+' : '') + avgRR.toFixed(2), avgRR >= 0 ? 'positive' : 'negative') +
        kpi('Best R', stats.best_rr ? '+' + stats.best_rr.toFixed(2) : '-', 'positive') +
        kpi('Worst R', stats.worst_rr ? stats.worst_rr.toFixed(2) : '-', 'negative');
}

function kpi(label, value, cls) {
    var accentCls = cls === 'positive' ? ' j-kpi-accent-green' : cls === 'negative' ? ' j-kpi-accent-red' : '';
    return '<div class="j-kpi' + accentCls + '"><div class="j-kpi-label">' + label +
           '</div><div class="j-kpi-value ' + (cls || '') + '">' + value + '</div></div>';
}

// ── Filtering ──

function getFilteredTrades() {
    var asset = document.getElementById('j-filter-asset').value.toUpperCase().trim();
    var dir = document.getElementById('j-filter-direction').value;
    var setup = document.getElementById('j-filter-setup').value.trim();
    var mistakes = document.getElementById('j-filter-mistakes').value;

    return _trades.filter(function(t) {
        if (asset && (t.asset || '').toUpperCase() !== asset) return false;
        if (dir && t.direction !== dir) return false;
        if (setup && (t.setup_type || '').toLowerCase().indexOf(setup.toLowerCase()) === -1) return false;
        if (mistakes === 'true') {
            var mt = parseTags(t.mistake_tags);
            if (!mt.length) return false;
        } else if (mistakes === 'false') {
            var mt2 = parseTags(t.mistake_tags);
            if (mt2.length) return false;
        }
        return true;
    });
}

// ── Sorting ──

function sortTrades(trades) {
    var field = _sortField;
    var asc = _sortAsc;
    return trades.slice().sort(function(a, b) {
        var va = a[field], vb = b[field];
        // Duration is computed
        if (field === 'duration') {
            va = tradeDurationMs(a);
            vb = tradeDurationMs(b);
        }
        if (va == null) va = asc ? Infinity : -Infinity;
        if (vb == null) vb = asc ? Infinity : -Infinity;
        if (typeof va === 'string') {
            va = va.toLowerCase();
            vb = (vb || '').toLowerCase();
        }
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
        return 0;
    });
}

// ── Table rendering ──

function renderTable() {
    var filtered = getFilteredTrades();
    var sorted = sortTrades(filtered);
    var tbody = document.getElementById('j-trade-tbody');
    var empty = document.getElementById('j-empty');

    if (!sorted.length) {
        tbody.innerHTML = '';
        empty.style.display = '';
        if (!_trades.length) {
            empty.innerHTML = 'No trades yet. Click <strong>+ New Trade</strong> to log your first trade, or use <strong>Import</strong> to pull in position drawings from your charts.';
        } else {
            empty.textContent = 'No trades match the current filters.';
        }
        return;
    }
    empty.style.display = 'none';

    var html = '';
    sorted.forEach(function(t) {
        var isOpen = !t.closed_at;
        var checked = _selected.has(t.id) ? ' checked' : '';
        var rowCls = isOpen ? ' class="open-trade"' : (_selected.has(t.id) ? ' class="selected"' : '');

        var tags = parseTags(t.tags);
        var mistakes = parseTags(t.mistake_tags);
        var grade = parseGrade(t.trade_grade);
        var dur = formatDuration(t);

        html += '<tr' + rowCls + ' data-id="' + t.id + '">';
        html += '<td><input type="checkbox" class="j-checkbox j-row-check" data-id="' + t.id + '"' + checked + '></td>';
        html += '<td>' + formatDate(t.opened_at) + '</td>';
        html += '<td>' + escapeHtml(t.asset) + '</td>';
        html += '<td><span class="j-dir ' + t.direction + '">' + t.direction.toUpperCase() + '</span></td>';
        html += '<td>' + formatPrice(t.entry_price);
        if (t.exit_price) html += ' &rarr; ' + formatPrice(t.exit_price);
        html += '</td>';
        html += '<td>' + formatRR(t.rr_planned) + '</td>';
        html += '<td>' + formatRR(t.rr_actual) + '</td>';
        html += '<td>' + (t.setup_type ? '<span class="j-tag setup">' + escapeHtml(t.setup_type) + '</span>' : '-') + '</td>';
        html += '<td>' + renderGrade(grade) + '</td>';
        html += '<td>';
        mistakes.forEach(function(m) { html += '<span class="j-tag mistake">' + escapeHtml(m) + '</span>'; });
        html += mistakes.length ? '' : '-';
        html += '</td>';
        html += '<td>' + dur + '</td>';
        html += '<td><div class="j-row-actions">';
        if (isOpen) {
            html += '<button class="j-row-btn close-btn" data-action="close" data-id="' + t.id + '">Close</button>';
        } else {
            html += '<button class="j-row-btn" data-action="share" data-id="' + t.id + '">Share</button>';
        }
        html += '<button class="j-row-btn" data-action="edit" data-id="' + t.id + '">Edit</button>';
        html += '<button class="j-row-btn" data-action="expand" data-id="' + t.id + '">Details</button>';
        html += '<button class="j-row-btn delete-btn" data-action="delete" data-id="' + t.id + '">Del</button>';
        html += '</div></td>';
        html += '</tr>';
    });

    tbody.innerHTML = html;
    updateBulkButtons();
    updateSortArrows();
}

function renderDetailRow(tradeId) {
    var t = _trades.find(function(x) { return x.id === tradeId; });
    if (!t) return;

    // Check if already expanded
    var existing = document.querySelector('.j-detail-row[data-detail-id="' + tradeId + '"]');
    if (existing) { existing.remove(); return; }

    var tr = document.querySelector('tr[data-id="' + tradeId + '"]');
    if (!tr) return;

    var detailTr = document.createElement('tr');
    detailTr.className = 'j-detail-row';
    detailTr.setAttribute('data-detail-id', tradeId);

    var notes = t.notes || '';
    var aiReview = t.ai_review || '';
    var tags = parseTags(t.tags);
    var checklist = t.checklist_results ? safeParseJSON(t.checklist_results) : null;

    var html = '<td colspan="12"><div class="j-detail-content">';
    // Left column
    html += '<div class="j-detail-section">';
    if (notes) {
        html += '<div class="j-detail-label">Notes</div>';
        html += '<div class="j-detail-text">' + escapeHtml(notes) + '</div>';
    }
    if (checklist) {
        html += '<div class="j-detail-label" style="margin-top:8px">Checklist</div>';
        Object.keys(checklist).forEach(function(item) {
            var ok = checklist[item];
            html += '<div class="j-checklist-item" style="opacity:0.8">' +
                    (ok ? '&#9745;' : '&#9744;') + ' ' + escapeHtml(item) + '</div>';
        });
    }
    if (tags.length) {
        html += '<div class="j-detail-label" style="margin-top:8px">Tags</div>';
        tags.forEach(function(tg) { html += '<span class="j-tag">' + escapeHtml(tg) + '</span> '; });
    }
    // MFE/MAE
    if (t.mfe != null || t.mae != null) {
        html += '<div class="j-mfe-mae-bar">';
        if (t.mfe != null) html += '<div class="j-mfe-mae-item">MFE: <span class="positive">' + t.mfe.toFixed(2) + 'R</span></div>';
        if (t.mae != null) html += '<div class="j-mfe-mae-item">MAE: <span class="negative">' + t.mae.toFixed(2) + 'R</span></div>';
        if (t.exit_efficiency != null) html += '<div class="j-mfe-mae-item">Efficiency: <span>' + (t.exit_efficiency * 100).toFixed(0) + '%</span></div>';
        html += '</div>';
    }
    // Score snapshot placeholder (loaded async)
    html += '<div class="j-snapshot-card" id="j-snapshot-' + tradeId + '"></div>';
    html += '</div>';
    // Right column
    html += '<div class="j-detail-section">';
    if (aiReview) {
        html += '<div class="j-detail-label">AI Review</div>';
        html += '<div class="j-ai-review-text">' + renderMarkdown(aiReview) + '</div>';
    }
    // AI Review button for closed trades
    if (t.closed_at) {
        html += '<button class="j-row-btn j-ai-review-btn" data-action="ai-review" data-id="' + t.id + '">'
              + (aiReview ? 'Regenerate AI Review' : 'Generate AI Review') + '</button>';
    }
    if (t.trade_grade) {
        var g = parseGrade(t.trade_grade);
        if (g) {
            html += '<div class="j-grade-detail">';
            html += '<span>Setup: ' + g.setup + '</span>';
            html += '<span>Execution: ' + g.execution + '</span>';
            html += '<span>Exit: ' + g.exit + '</span>';
            html += '<span>Discipline: ' + g.discipline + '</span>';
            html += '</div>';
        }
    }
    // Screenshots placeholder
    html += '<div class="j-detail-label" style="margin-top:8px">Screenshots</div>';
    html += '<div class="j-screenshots" id="j-screenshots-' + tradeId + '"><span style="font-size:0.65rem;color:#484f58">Loading...</span></div>';
    // Extra info
    html += '<div style="margin-top:8px;font-size:0.62rem;color:#484f58">';
    if (t.size) html += 'Size: ' + t.size + ' &middot; ';
    if (t.fees) html += 'Fees: $' + t.fees.toFixed(2) + ' &middot; ';
    if (t.confidence) html += 'Confidence: ' + t.confidence + '/5 &middot; ';
    if (t.mood) html += 'Mood: ' + t.mood + '/5 &middot; ';
    if (t.account_tag) html += 'Account: ' + escapeHtml(t.account_tag);
    html += '</div>';
    html += '</div>';
    html += '</div></td>';

    detailTr.innerHTML = html;
    tr.parentNode.insertBefore(detailTr, tr.nextSibling);

    // Load screenshots
    api('GET', '/api/journal/trades/' + tradeId + '/screenshots').then(function(screenshots) {
        var container = document.getElementById('j-screenshots-' + tradeId);
        if (!container) return;
        if (!screenshots || !screenshots.length) {
            container.innerHTML = '<span style="font-size:0.62rem;color:#484f58">No screenshots</span>';
            return;
        }
        container.innerHTML = screenshots.map(function(s) {
            return '<img class="j-screenshot-thumb" src="/data/' + escapeHtml(s.file_path) + '" alt="' + escapeHtml(s.screenshot_type) + '" title="' + escapeHtml(s.screenshot_type) + '">';
        }).join('');
    }).catch(function() {
        var container = document.getElementById('j-screenshots-' + tradeId);
        if (container) container.innerHTML = '<span style="font-size:0.62rem;color:#484f58">Failed to load</span>';
    });

    // Load score snapshot
    api('GET', '/api/scanner/snapshots/' + tradeId).then(function(data) {
        var card = document.getElementById('j-snapshot-' + tradeId);
        if (!card || !data.snapshots || !data.snapshots.length) return;
        var snap = data.snapshots[0]; // entry snapshot
        var h = '<div class="j-detail-label" style="margin-top:8px">Entry Score</div>';
        h += '<div class="j-snapshot-grid">';
        var names = ['trend', 'momentum', 'rsi', 'structure', 'level', 'macro'];
        names.forEach(function(name) {
            var s = snap['score_' + name];
            if (s == null) s = '-';
            var cls = s === 2 ? 'j-snap-high' : s === 1 ? 'j-snap-mid' : s === 0 ? 'j-snap-low' : '';
            h += '<div class="j-snapshot-item ' + cls + '">' +
                 '<span class="j-snap-label">' + name + '</span>' +
                 '<span class="j-snap-score">' + s + '/2</span></div>';
        });
        h += '</div>';
        var vc = 'j-snap-verdict-' + (snap.verdict || 'avoid').toLowerCase();
        h += '<div class="j-snapshot-total"><span class="' + vc + '">' +
             (snap.score_total || 0) + '/12 ' + (snap.verdict || '?') + '</span>';
        h += '<span class="j-snap-regime">' + (snap.regime || '') + '</span></div>';
        card.innerHTML = h;
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}

// ── Formatting helpers ──

function formatDate(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    var mon = (d.getMonth() + 1).toString().padStart(2, '0');
    var day = d.getDate().toString().padStart(2, '0');
    var hr = d.getHours().toString().padStart(2, '0');
    var min = d.getMinutes().toString().padStart(2, '0');
    return mon + '/' + day + ' ' + hr + ':' + min;
}

function formatPrice(p) {
    if (p == null) return '-';
    if (p >= 1000) return p.toLocaleString(undefined, {maximumFractionDigits: 2});
    if (p >= 1) return p.toFixed(4);
    return p.toPrecision(4);
}

function formatRR(rr) {
    if (rr == null) return '-';
    var cls = rr >= 0 ? 'positive' : 'negative';
    var sign = rr >= 0 ? '+' : '';
    return '<span class="j-rr ' + cls + '">' + sign + rr.toFixed(2) + 'R</span>';
}

function formatDuration(t) {
    var ms = tradeDurationMs(t);
    if (ms == null) return '<span style="color:#58a6ff">OPEN</span>';
    var hours = ms / 3600000;
    if (hours < 1) return Math.round(hours * 60) + 'm';
    if (hours < 24) return hours.toFixed(1) + 'h';
    return (hours / 24).toFixed(1) + 'd';
}

function tradeDurationMs(t) {
    if (!t.opened_at) return null;
    var start = new Date(t.opened_at).getTime();
    var end = t.closed_at ? new Date(t.closed_at).getTime() : null;
    if (!end) return null;
    return end - start;
}

function renderGrade(grade) {
    if (!grade) return '-';
    var avg = Math.round((grade.setup + grade.execution + grade.exit + grade.discipline) / 4);
    var cls = avg >= 7 ? 'high' : avg <= 4 ? 'low' : 'filled';
    var dots = '';
    for (var i = 0; i < 5; i++) {
        dots += '<div class="j-grade-dot' + (i < Math.round(avg / 2) ? ' ' + cls : '') + '"></div>';
    }
    return '<div class="j-grade-dots" title="Avg: ' + avg + '/10">' + dots + '</div>';
}

// The API returns tags and mistake_tags as arrays (null when never set).
function parseTags(val) {
    return Array.isArray(val) ? val : [];
}

function parseGrade(val) {
    if (!val) return null;
    try { var obj = typeof val === 'string' ? JSON.parse(val) : val; return obj && obj.setup != null ? obj : null; }
    catch(e) { return null; }
}

function safeParseJSON(val) {
    try { return JSON.parse(val); } catch(e) { return null; }
}

function populateSetupDatalist() {
    var dl = document.getElementById('j-setup-list');
    dl.innerHTML = '';
    _setupTypes.forEach(function(st) {
        var opt = document.createElement('option');
        opt.value = st;
        dl.appendChild(opt);
    });
}

// ── Sort arrows ──

function updateSortArrows() {
    document.querySelectorAll('.j-table th[data-sort]').forEach(function(th) {
        var arrow = th.querySelector('.sort-arrow');
        if (th.getAttribute('data-sort') === _sortField) {
            arrow.textContent = _sortAsc ? '\u25B2' : '\u25BC';
        } else {
            arrow.textContent = '';
        }
    });
}

// ── Bulk selection ──

function updateBulkButtons() {
    var count = _selected.size;
    document.getElementById('j-select-count').textContent = count ? count + ' selected' : '';
    document.getElementById('j-btn-bulk-tag').disabled = !count;
    document.getElementById('j-btn-bulk-delete').disabled = !count;
}

// ── R:R live preview ──

function updateRRPreview() {
    var entry = parseFloat(document.getElementById('j-form-entry').value);
    var stop = parseFloat(document.getElementById('j-form-stop').value);
    var target = parseFloat(document.getElementById('j-form-target').value);
    var preview = document.getElementById('j-rr-preview');

    if (!entry || !stop || !target || stop === entry) {
        preview.textContent = '';
        return;
    }
    var risk = Math.abs(entry - stop);
    var reward = Math.abs(target - entry);
    var rr = (reward / risk).toFixed(2);
    preview.textContent = 'Planned R:R = ' + rr;
    preview.style.color = rr >= 1 ? '#3fb950' : '#ef5350';
}

// ── Checklist loading ──

function loadChecklistForSetup(setupType) {
    var section = document.getElementById('j-checklist-section');
    var container = document.getElementById('j-checklist-items');
    if (!setupType || !_checklists[setupType]) {
        section.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    var cl = _checklists[setupType];
    var items = cl.checklist_items || [];
    if (!items.length) { section.style.display = 'none'; return; }

    section.style.display = '';
    container.innerHTML = items.map(function(item, i) {
        return '<div class="j-checklist-item"><input type="checkbox" id="j-cl-' + i + '"> <label for="j-cl-' + i + '">' + escapeHtml(item) + '</label></div>';
    }).join('');
}

function getChecklistResults() {
    var section = document.getElementById('j-checklist-section');
    if (section.style.display === 'none') return null;
    var checks = section.querySelectorAll('.j-checklist-item');
    var results = {};
    checks.forEach(function(div) {
        var cb = div.querySelector('input[type="checkbox"]');
        var label = div.querySelector('label');
        if (label) results[label.textContent] = cb.checked;
    });
    return Object.keys(results).length ? results : null;
}

// ── Modal management ──

function openNewTradeModal() {
    document.getElementById('j-modal-title').textContent = 'Log New Trade';
    document.getElementById('j-form-id').value = '';
    document.getElementById('j-form-asset').value = '';
    document.getElementById('j-form-entry').value = '';
    document.getElementById('j-form-stop').value = '';
    document.getElementById('j-form-target').value = '';
    document.getElementById('j-form-size').value = '';
    document.getElementById('j-form-fees').value = '';
    document.getElementById('j-form-setup').value = '';
    document.getElementById('j-form-confidence').value = 3;
    document.getElementById('j-form-confidence-val').textContent = '3';
    document.getElementById('j-form-mood').value = 3;
    document.getElementById('j-form-mood-val').textContent = '3';
    document.getElementById('j-form-tags').value = '';
    document.getElementById('j-form-notes').value = '';
    document.getElementById('j-form-account').value = '';
    document.getElementById('j-form-timeframe').value = '1D';
    setDirection('long');
    document.getElementById('j-rr-preview').textContent = '';
    document.getElementById('j-checklist-section').style.display = 'none';
    document.getElementById('j-modal-new').classList.add('active');
    setTimeout(function() { document.getElementById('j-form-asset').focus(); }, 100);
}

function editTrade(tradeId) {
    var t = _trades.find(function(x) { return x.id === tradeId; });
    if (!t) return;
    document.getElementById('j-modal-title').textContent = 'Edit Trade';
    document.getElementById('j-form-id').value = t.id;
    document.getElementById('j-form-asset').value = t.asset || '';
    document.getElementById('j-form-entry').value = t.entry_price || '';
    document.getElementById('j-form-stop').value = t.stop_price || '';
    document.getElementById('j-form-target').value = t.target_price || '';
    document.getElementById('j-form-size').value = t.size || '';
    document.getElementById('j-form-fees').value = t.fees || '';
    document.getElementById('j-form-setup').value = t.setup_type || '';
    document.getElementById('j-form-confidence').value = t.confidence || 3;
    document.getElementById('j-form-confidence-val').textContent = t.confidence || 3;
    document.getElementById('j-form-mood').value = t.mood || 3;
    document.getElementById('j-form-mood-val').textContent = t.mood || 3;
    document.getElementById('j-form-notes').value = t.notes || '';
    document.getElementById('j-form-account').value = t.account_tag || '';
    document.getElementById('j-form-timeframe').value = t.timeframe || '1D';
    setDirection(t.direction || 'long');
    var tags = parseTags(t.tags);
    document.getElementById('j-form-tags').value = tags.join(', ');
    document.getElementById('j-rr-preview').textContent = '';
    document.getElementById('j-checklist-section').style.display = 'none';
    document.getElementById('j-modal-new').classList.add('active');
    setTimeout(function() { document.getElementById('j-form-asset').focus(); }, 100);
}

function openCloseModal(tradeId) {
    var t = _trades.find(function(x) { return x.id === tradeId; });
    if (!t) return;
    document.getElementById('j-close-id').value = tradeId;
    document.getElementById('j-close-exit').value = '';
    document.getElementById('j-close-reason').value = 'manual';
    document.getElementById('j-close-notes').value = '';

    // Show trade context at top of close modal
    var ctx = document.getElementById('j-close-context');
    if (ctx) {
        ctx.innerHTML = '<span class="j-dir ' + t.direction + '">' + t.direction.toUpperCase() + '</span> ' +
            escapeHtml(t.asset) + ' @ ' + formatPrice(t.entry_price) +
            (t.stop_price ? ' &middot; SL ' + formatPrice(t.stop_price) : '') +
            (t.target_price ? ' &middot; TP ' + formatPrice(t.target_price) : '');
    }

    // Render mistake pills
    var container = document.getElementById('j-close-mistakes');
    container.innerHTML = _mistakeTags.map(function(tag) {
        return '<button type="button" class="j-mistake-pill" data-tag="' + escapeHtml(tag) + '">' + escapeHtml(tag) + '</button>';
    }).join('');

    document.getElementById('j-modal-close').classList.add('active');
    setTimeout(function() { document.getElementById('j-close-exit').focus(); }, 100);
}

function closeModal(id) {
    document.getElementById(id).classList.remove('active');
}

function setDirection(dir) {
    document.querySelectorAll('#j-form-direction .j-dir-toggle-btn').forEach(function(btn) {
        btn.classList.toggle('active', btn.getAttribute('data-dir') === dir);
    });
}

function getDirection() {
    var active = document.querySelector('#j-form-direction .j-dir-toggle-btn.active');
    return active ? active.getAttribute('data-dir') : 'long';
}

function getSelectedMistakes() {
    var selected = [];
    document.querySelectorAll('#j-close-mistakes .j-mistake-pill.selected').forEach(function(el) {
        selected.push(el.getAttribute('data-tag'));
    });
    return selected;
}

// ── Save handlers ──

function saveTrade() {
    var editId = document.getElementById('j-form-id').value;
    var assetVal = document.getElementById('j-form-asset').value.trim();
    var entryVal = parseFloat(document.getElementById('j-form-entry').value);
    if (!editId && (!assetVal || !/^[A-Za-z0-9]{1,20}$/.test(assetVal))) {
        showToast('Valid asset symbol required', 'error'); return;
    }
    if (!editId && (!entryVal || entryVal <= 0)) {
        showToast('Positive entry price required', 'error'); return;
    }
    var body = {
        asset: assetVal,
        timeframe: document.getElementById('j-form-timeframe').value,
        direction: getDirection(),
        entry_price: parseFloat(document.getElementById('j-form-entry').value) || null,
        stop_price: parseFloat(document.getElementById('j-form-stop').value) || null,
        target_price: parseFloat(document.getElementById('j-form-target').value) || null,
        size: parseFloat(document.getElementById('j-form-size').value) || null,
        fees: parseFloat(document.getElementById('j-form-fees').value) || null,
        setup_type: document.getElementById('j-form-setup').value.trim() || null,
        confidence: parseInt(document.getElementById('j-form-confidence').value),
        mood: parseInt(document.getElementById('j-form-mood').value),
        notes: document.getElementById('j-form-notes').value.trim() || null,
        account_tag: document.getElementById('j-form-account').value.trim() || null,
    };

    var tagsStr = document.getElementById('j-form-tags').value.trim();
    if (tagsStr) body.tags = tagsStr.split(',').map(function(s) { return s.trim(); }).filter(Boolean);

    var checklistResults = getChecklistResults();
    if (checklistResults) body.checklist_results = checklistResults;

    var method = editId ? 'PATCH' : 'POST';
    var url = editId ? '/api/journal/trades/' + editId : '/api/journal/trades';

    api(method, url, body).then(function() {
        closeModal('j-modal-new');
        showToast(editId ? 'Trade updated' : 'Trade logged', 'success');
        loadTrades();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function closeTrade() {
    var tradeId = document.getElementById('j-close-id').value;
    var exitVal = parseFloat(document.getElementById('j-close-exit').value);
    if (!exitVal || exitVal <= 0) {
        showToast('Positive exit price required', 'error'); return;
    }
    var body = {
        exit_price: exitVal,
        exit_reason: document.getElementById('j-close-reason').value,
        notes: document.getElementById('j-close-notes').value.trim() || null,
        mistake_tags: getSelectedMistakes(),
    };

    api('POST', '/api/journal/trades/' + tradeId + '/close', body).then(function() {
        closeModal('j-modal-close');
        showToast('Trade closed', 'success');
        loadTrades();
        // Offer to share the result — never silent-mint (POST is a side
        // effect: it creates a public page + charges share quota).
        var tid = parseInt(tradeId, 10);
        if (window.TradeShare && window.confirm('Share the result?')) {
            // Find the just-closed trade for label/snapshot hints.
            var t = _trades.find(function(x) { return x.id === tid; });
            TradeShare.open(tid, t || null);
        }
    }).catch(function(e) { showToast(e.message, 'error'); });
}

// Open the share modal for a closed trade (journal list "Share" button +
// post-close confirm). The trade object gives the modal label/snapshot hints.
function shareTrade(tradeId) {
    if (!window.TradeShare) { showToast('Share unavailable', 'error'); return; }
    var t = _trades.find(function(x) { return x.id === tradeId; });
    if (t && !t.closed_at) { showToast('Only closed trades can be shared', 'error'); return; }
    TradeShare.open(tradeId, t || null);
}

function deleteTrade(tradeId) {
    if (!confirm('Delete this trade? This cannot be undone.')) return;
    api('DELETE', '/api/journal/trades/' + tradeId).then(function() {
        _selected.delete(tradeId);
        showToast('Trade deleted', 'success');
        loadTrades();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function bulkDelete() {
    if (!_selected.size) return;
    if (!confirm('Delete ' + _selected.size + ' trades? This cannot be undone.')) return;
    api('POST', '/api/journal/trades/bulk', {
        trade_ids: Array.from(_selected),
        action: 'delete'
    }).then(function(data) {
        _selected.clear();
        showToast(data.affected + ' trades deleted', 'success');
        loadTrades();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function importDrawings() {
    api('POST', '/api/journal/trades/import').then(function(data) {
        showToast(data.imported + ' trades imported from drawings', 'success');
        if (data.imported > 0) loadTrades();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

// (init function defined below after equity curve, calendar, sessions code)

// =============================================
// ======== CREDIT BALANCE ========
// =============================================

function loadCreditBalance() {
    api('GET', '/api/journal/credits').then(function(data) {
        var el = document.getElementById('j-credit-balance');
        if (!el || data.balance_cents == null) return;
        var dollars = (data.balance_cents / 100).toFixed(2);
        el.textContent = 'AI Credits: $' + dollars;
        el.style.display = '';
        el.style.color = data.balance_cents > 0 ? '#8b949e' : '#ef5350';
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', 'dynamic failed — rendered as empty', String((e && e.message) || e)); });
}


// The equity curve moved to journal-analytics-charts.js.


// =============================================
// ======== CALENDAR ========
// =============================================

var _calYear, _calMonth, _calData = {};
var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December'];

function initCalendar() {
    var now = new Date();
    _calYear = now.getFullYear();
    _calMonth = now.getMonth() + 1;
    document.getElementById('j-cal-prev').addEventListener('click', function() {
        _calMonth--;
        if (_calMonth < 1) { _calMonth = 12; _calYear--; }
        loadCalendar();
    });
    document.getElementById('j-cal-next').addEventListener('click', function() {
        _calMonth++;
        if (_calMonth > 12) { _calMonth = 1; _calYear++; }
        loadCalendar();
    });
    document.getElementById('j-cal-drill-close').addEventListener('click', function() {
        document.getElementById('j-cal-drill').style.display = 'none';
    });
}

function loadCalendar() {
    document.getElementById('j-cal-month-label').textContent = MONTH_NAMES[_calMonth - 1] + ' ' + _calYear;
    api('GET', '/api/journal/calendar?year=' + _calYear + '&month=' + _calMonth).then(function(data) {
        _calData = data;
        renderCalendar(data);
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function renderCalendar(data) {
    var grid = document.getElementById('j-cal-grid');
    var today = new Date();
    var todayStr = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');

    // First day of month
    var firstDay = new Date(_calYear, _calMonth - 1, 1);
    var startDow = (firstDay.getDay() + 6) % 7; // Monday = 0
    var daysInMonth = new Date(_calYear, _calMonth, 0).getDate();

    var html = '';
    // Header
    ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach(function(d) {
        html += '<div class="j-cal-header">' + d + '</div>';
    });

    // Empty cells before first day
    for (var e = 0; e < startDow; e++) {
        html += '<div class="j-cal-day empty"></div>';
    }

    // Day cells
    var days = data.days || {};
    for (var d = 1; d <= daysInMonth; d++) {
        var dateStr = _calYear + '-' + String(_calMonth).padStart(2, '0') + '-' + String(d).padStart(2, '0');
        var dayData = days[dateStr];
        var cls = 'j-cal-day';
        if (dateStr === todayStr) cls += ' today';
        if (dayData) {
            cls += ' has-trades';
            if (dayData.net_r > 0) cls += ' positive-day';
            else if (dayData.net_r < 0) cls += ' negative-day';
        }

        html += '<div class="' + cls + '" data-date="' + dateStr + '">';
        html += '<div class="j-cal-day-num">' + d + '</div>';

        if (dayData) {
            var rCls = dayData.net_r > 0 ? 'positive' : dayData.net_r < 0 ? 'negative' : '';
            var sign = dayData.net_r > 0 ? '+' : '';
            html += '<div class="j-cal-day-r ' + rCls + '">' + sign + dayData.net_r.toFixed(2) + 'R</div>';
            html += '<div class="j-cal-day-meta">' + dayData.trades + ' trade' + (dayData.trades !== 1 ? 's' : '');
            if (dayData.wins) html += ' | ' + dayData.wins + 'W';
            if (dayData.losses) html += ' ' + dayData.losses + 'L';
            html += '</div>';
        }
        html += '</div>';
    }

    grid.innerHTML = html;

    // Summary
    var summary = data.summary || {};
    var sumEl = document.getElementById('j-cal-summary');
    sumEl.innerHTML =
        kpi('Trading Days', summary.trading_days || 0) +
        kpi('Total Trades', summary.total_trades || 0) +
        kpi('Net R', (summary.total_net_r >= 0 ? '+' : '') + (summary.total_net_r || 0).toFixed(2),
            summary.total_net_r >= 0 ? 'positive' : 'negative') +
        kpi('Best Day', summary.best_day_r ? '+' + summary.best_day_r.toFixed(2) + 'R' : '-', 'positive') +
        kpi('Worst Day', summary.worst_day_r ? summary.worst_day_r.toFixed(2) + 'R' : '-', 'negative');

    // Click handlers for drill-down
    grid.querySelectorAll('.j-cal-day:not(.empty)').forEach(function(cell) {
        cell.addEventListener('click', function() {
            var date = cell.getAttribute('data-date');
            showCalendarDrillDown(date);
        });
    });
}

function showCalendarDrillDown(dateStr) {
    var drill = document.getElementById('j-cal-drill');
    var title = document.getElementById('j-cal-drill-title');
    var content = document.getElementById('j-cal-drill-content');

    title.textContent = formatCalDate(dateStr);
    drill.style.display = '';

    // Find closed trades for this date (consistent with calendar data source)
    var dayTrades = _trades.filter(function(t) {
        if (!t.closed_at) return false;
        var closedDate = t.closed_at.split('T')[0].split(' ')[0];
        return closedDate === dateStr;
    });

    if (!dayTrades.length) {
        content.innerHTML = '<div style="font-size:0.7rem;color:#484f58;padding:8px 0">No trades on this day.</div>';
        return;
    }

    var html = '<table class="j-table" style="font-size:0.65rem"><thead><tr>';
    html += '<th>Asset</th><th>Dir</th><th>Entry</th><th>Exit</th><th>R</th><th>Setup</th></tr></thead><tbody>';
    dayTrades.forEach(function(t) {
        html += '<tr>';
        html += '<td>' + escapeHtml(t.asset) + '</td>';
        html += '<td><span class="j-dir ' + t.direction + '">' + t.direction.toUpperCase() + '</span></td>';
        html += '<td>' + formatPrice(t.entry_price) + '</td>';
        html += '<td>' + (t.exit_price ? formatPrice(t.exit_price) : '-') + '</td>';
        html += '<td>' + formatRR(t.rr_actual) + '</td>';
        html += '<td>' + (t.setup_type || '-') + '</td>';
        html += '</tr>';
    });
    html += '</tbody></table>';
    content.innerHTML = html;
}

function formatCalDate(dateStr) {
    var parts = dateStr.split('-');
    return MONTH_NAMES[parseInt(parts[1]) - 1] + ' ' + parseInt(parts[2]) + ', ' + parts[0];
}


// =============================================
// ======== SESSIONS ========
// =============================================

var _sessions = [];
var _sessionYear, _sessionMonth;

function initSessions() {
    var now = new Date();
    _sessionYear = now.getFullYear();
    _sessionMonth = now.getMonth() + 1;

    // Populate month selector (12 months)
    var sel = document.getElementById('j-session-month');
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

    sel.addEventListener('change', function() {
        var parts = this.value.split('-');
        _sessionYear = parseInt(parts[0]);
        _sessionMonth = parseInt(parts[1]);
        loadSessions();
    });

    document.getElementById('j-btn-new-session').addEventListener('click', openNewSessionModal);
    document.getElementById('j-session-cancel').addEventListener('click', function() { closeModal('j-modal-session'); });
    document.getElementById('j-session-save').addEventListener('click', saveSession);

    document.getElementById('j-session-form-mood-pre').addEventListener('input', function() {
        document.getElementById('j-session-mood-pre-val').textContent = this.value;
    });
    document.getElementById('j-session-form-mood-post').addEventListener('input', function() {
        document.getElementById('j-session-mood-post-val').textContent = this.value;
    });
}

function loadSessions() {
    api('GET', '/api/journal/sessions?year=' + _sessionYear + '&month=' + _sessionMonth).then(function(data) {
        _sessions = data || [];
        renderSessions();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function renderSessions() {
    var container = document.getElementById('j-session-list');
    var empty = document.getElementById('j-session-empty');

    if (!_sessions.length) {
        container.innerHTML = '';
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    var html = '';
    _sessions.forEach(function(s) {
        var netCls = s.net_r > 0 ? 'positive' : s.net_r < 0 ? 'negative' : '';
        var sessionCls = 'j-session-card';
        if (s.net_r > 0) sessionCls += ' positive-session';
        else if (s.net_r < 0) sessionCls += ' negative-session';
        html += '<div class="' + sessionCls + '" data-session-id="' + s.id + '">';

        // Header
        html += '<div class="j-session-header">';
        html += '<div><span class="j-session-date">' + formatCalDate(s.date) + '</span>';
        if (s.session_label && s.session_label !== 'default') {
            html += '<span class="j-session-label">' + escapeHtml(s.session_label) + '</span>';
        }
        html += '</div>';
        html += '<div class="j-session-actions">';
        if (s.trade_count > 0) {
            html += '<button class="j-row-btn" data-action="ai-session-summary" data-id="' + s.id + '">' + (s.ai_summary ? 'Refresh AI' : 'AI Summary') + '</button>';
        }
        html += '<button class="j-row-btn" data-action="edit-session" data-id="' + s.id + '">Edit</button>';
        html += '<button class="j-row-btn delete-btn" data-action="delete-session" data-id="' + s.id + '">Del</button>';
        html += '</div>';
        html += '</div>';

        // Stats
        html += '<div class="j-session-stats">';
        html += '<div>' + s.trade_count + ' trade' + (s.trade_count !== 1 ? 's' : '') + '</div>';
        if (s.trade_count > 0) {
            html += '<div>Net: <span class="' + netCls + '">' + (s.net_r >= 0 ? '+' : '') + s.net_r.toFixed(2) + 'R</span></div>';
            html += '<div>' + s.win_count + 'W / ' + s.loss_count + 'L</div>';
        }
        if (s.mood_pre) html += '<div>Mood: ' + s.mood_pre + (s.mood_post ? ' → ' + s.mood_post : '') + '/5</div>';
        html += '</div>';

        // Notes
        if (s.pre_notes) {
            html += '<div class="j-session-notes-label">Pre-session</div>';
            html += '<div class="j-session-notes">' + escapeHtml(s.pre_notes) + '</div>';
        }
        if (s.post_notes) {
            html += '<div class="j-session-notes-label">Post-session</div>';
            html += '<div class="j-session-notes">' + escapeHtml(s.post_notes) + '</div>';
        }
        if (s.ai_summary) {
            html += '<div class="j-session-notes-label" style="color:#58a6ff">AI Summary</div>';
            html += '<div class="j-session-notes j-ai-summary">' + renderMarkdown(s.ai_summary) + '</div>';
        }

        html += '</div>';
    });

    container.innerHTML = html;

    // Event delegation for session actions
    container.querySelectorAll('[data-action]').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var action = btn.getAttribute('data-action');
            var id = parseInt(btn.getAttribute('data-id'));
            if (action === 'edit-session') editSession(id);
            else if (action === 'delete-session') deleteSession(id);
            else if (action === 'ai-session-summary') requestSessionSummary(id);
        });
    });
}

function openNewSessionModal() {
    document.getElementById('j-session-modal-title').textContent = 'New Session';
    document.getElementById('j-session-form-id').value = '';
    document.getElementById('j-session-form-date').value = new Date().toISOString().split('T')[0];
    document.getElementById('j-session-form-label').value = '';
    document.getElementById('j-session-form-pre').value = '';
    document.getElementById('j-session-form-post').value = '';
    document.getElementById('j-session-form-mood-pre').value = 3;
    document.getElementById('j-session-mood-pre-val').textContent = '3';
    document.getElementById('j-session-form-mood-post').value = 3;
    document.getElementById('j-session-mood-post-val').textContent = '3';
    document.getElementById('j-modal-session').classList.add('active');
}

function editSession(sessionId) {
    var s = _sessions.find(function(x) { return x.id === sessionId; });
    if (!s) return;
    document.getElementById('j-session-modal-title').textContent = 'Edit Session';
    document.getElementById('j-session-form-id').value = s.id;
    document.getElementById('j-session-form-date').value = s.date;
    document.getElementById('j-session-form-label').value = s.session_label || '';
    document.getElementById('j-session-form-pre').value = s.pre_notes || '';
    document.getElementById('j-session-form-post').value = s.post_notes || '';
    document.getElementById('j-session-form-mood-pre').value = s.mood_pre || 3;
    document.getElementById('j-session-mood-pre-val').textContent = s.mood_pre || 3;
    document.getElementById('j-session-form-mood-post').value = s.mood_post || 3;
    document.getElementById('j-session-mood-post-val').textContent = s.mood_post || 3;
    document.getElementById('j-modal-session').classList.add('active');
}

function saveSession() {
    var editId = document.getElementById('j-session-form-id').value;
    var body = {
        date: document.getElementById('j-session-form-date').value,
        session_label: document.getElementById('j-session-form-label').value.trim() || null,
        pre_notes: document.getElementById('j-session-form-pre').value.trim() || null,
        post_notes: document.getElementById('j-session-form-post').value.trim() || null,
        mood_pre: parseInt(document.getElementById('j-session-form-mood-pre').value),
        mood_post: parseInt(document.getElementById('j-session-form-mood-post').value),
    };

    var method = editId ? 'PATCH' : 'POST';
    var url = editId ? '/api/journal/sessions/' + editId : '/api/journal/sessions';

    api(method, url, body).then(function() {
        closeModal('j-modal-session');
        showToast(editId ? 'Session updated' : 'Session created', 'success');
        loadSessions();
    }).catch(function(e) { showToast(e.message, 'error'); });
}

function deleteSession(sessionId) {
    if (!confirm('Delete this session? Trades will be unlinked but not deleted.')) return;
    api('DELETE', '/api/journal/sessions/' + sessionId).then(function() {
        showToast('Session deleted', 'success');
        loadSessions();
    }).catch(function(e) { showToast(e.message, 'error'); });
}


// The analytics tab moved to journal-analytics-charts.js.


// =============================================
// ======== AI FEATURES ========
// =============================================

function renderMarkdown(text) {
    if (!text) return '';
    // Simple markdown: **bold**, \n→<br>, - list items
    return escapeHtml(text)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/^- (.+)/gm, '<div style="padding-left:12px">• $1</div>')
        .replace(/\n/g, '<br>');
}

function requestAIReview(tradeId) {
    var btn = document.querySelector('[data-action="ai-review"][data-id="' + tradeId + '"]');
    var prevLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }

    api('POST', '/api/journal/trades/' + tradeId + '/ai-review').then(function(data) {
        // Update trade in local state
        var t = _trades.find(function(x) { return x.id === tradeId; });
        if (t) {
            t.ai_review = data.review;
            if (data.grade) t.trade_grade = JSON.stringify(data.grade);
        }
        // Re-render the detail row
        var existing = document.querySelector('.j-detail-row[data-detail-id="' + tradeId + '"]');
        if (existing) existing.remove();
        renderDetailRow(tradeId);
        var cost = data.charged || 0;
        showToast('AI review generated' + (cost ? ' (' + cost + '¢)' : ''), 'success');

        // Show suggested mistakes
        if (data.suggested_mistakes && data.suggested_mistakes.length) {
            showToast('AI suggests: ' + data.suggested_mistakes.join(', '));
        }
    }).catch(function(e) {
        showToast(e.message, 'error');
        if (btn) { btn.disabled = false; btn.textContent = prevLabel || 'Generate AI Review'; }
    });
}

function requestSessionSummary(sessionId) {
    var btn = document.querySelector('[data-action="ai-session-summary"][data-id="' + sessionId + '"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }

    api('POST', '/api/journal/sessions/' + sessionId + '/ai-summary').then(function(data) {
        var cost = data.charged || 0;
        showToast('Session summary generated' + (cost ? ' (' + cost + '¢)' : ''), 'success');
        loadSessions(); // Refresh to show summary
    }).catch(function(e) {
        showToast(e.message, 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'AI Summary'; }
    });
}

function requestWeeklyReview() {
    var btn = document.getElementById('j-btn-weekly-review');
    if (btn) { btn.disabled = true; btn.textContent = 'Generating...'; }

    var days = parseInt(document.getElementById('j-analytics-days').value) || 90;
    api('POST', '/api/journal/ai-review/weekly', {days: Math.min(days, 90)}).then(function(data) {
        if (btn) { btn.disabled = false; btn.textContent = 'AI Review'; }
        // Show in modal
        var modal = document.getElementById('j-modal-ai-review');
        var content = document.getElementById('j-ai-review-content');
        content.innerHTML = renderMarkdown(data.review || '');
        var cost = data.charged || 0;
        if (cost) content.innerHTML += '<div style="margin-top:12px;font-size:0.6rem;color:#484f58">Cost: ' + cost + '¢</div>';
        modal.classList.add('active');
    }).catch(function(e) {
        showToast(e.message, 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'AI Review'; }
    });
}

function sendNLQuery() {
    var input = document.getElementById('j-nl-input');
    var question = input.value.trim();
    if (!question) return;

    var output = document.getElementById('j-nl-output');
    output.innerHTML = '<span style="color:#787b86">Thinking...</span>';
    input.disabled = true;

    var text = '';
    var queryTimeout = setTimeout(function() {
        if (!text) {
            output.innerHTML = '<span style="color:#ef5350">Query timed out. Try a simpler question.</span>';
            input.disabled = false;
        }
    }, 30000);

    fetch('/api/journal/ai-query', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF},
        body: JSON.stringify({question: question})
    }).then(function(resp) {
        if (!resp.ok) return resp.json().then(function(e) { throw new Error(e.error || 'Query failed'); });

        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';

        function processStream() {
            return reader.read().then(function(result) {
                if (result.done) {
                    clearTimeout(queryTimeout);
                    input.disabled = false;
                    return;
                }
                buffer += decoder.decode(result.value, {stream: true});
                var lines = buffer.split('\n');
                buffer = lines.pop(); // keep incomplete line

                lines.forEach(function(line) {
                    if (!line.startsWith('data: ')) return;
                    try {
                        var data = JSON.parse(line.substring(6));
                        if (data.error) {
                            clearTimeout(queryTimeout);
                            output.innerHTML = '<span style="color:#ef5350">' + escapeHtml(data.error) + '</span>';
                            input.disabled = false;
                            return;
                        }
                        if (data.t) {
                            clearTimeout(queryTimeout);
                            if (!text) output.innerHTML = '';
                            text += data.t;
                            output.innerHTML = renderMarkdown(text);
                        }
                        if (data.done) {
                            clearTimeout(queryTimeout);
                            input.disabled = false;
                            var cost = data.charged || 0;
                            if (cost) output.innerHTML += '<div style="margin-top:6px;font-size:0.55rem;color:#484f58">' + cost + '¢ · ' + (data.row_count || 0) + ' rows</div>';
                        }
                    } catch(e) {}
                });
                return processStream();
            });
        }

        return processStream();
    }).catch(function(e) {
        clearTimeout(queryTimeout);
        output.innerHTML = '<span style="color:#ef5350">' + escapeHtml(e.message) + '</span>';
        input.disabled = false;
    });
}


// Playbooks, goals and export moved to journal-playbooks-goals.js.


// =============================================
// ======== INIT (UPDATED) ========
// =============================================

function init() {
    // Tabs — load data when tab changes
    document.querySelectorAll('.j-tab').forEach(function(tab) {
        tab.addEventListener('click', function() {
            document.querySelectorAll('.j-tab').forEach(function(t) { t.classList.remove('active'); });
            document.querySelectorAll('.j-panel').forEach(function(p) { p.classList.remove('active'); });
            tab.classList.add('active');
            var tabName = tab.getAttribute('data-tab');
            document.getElementById('j-panel-' + tabName).classList.add('active');
            // Lazy-load tab data
            if (tabName === 'calendar') loadCalendar();
            else if (tabName === 'analytics') JournalAnalyticsCharts.loadAnalytics();
            else if (tabName === 'sessions') loadSessions();
            else if (tabName === 'playbooks') { JournalPlaybooksGoals.loadPlaybooks(); JournalPlaybooksGoals.loadGoals(); }
        });
    });

    // New trade
    document.getElementById('j-btn-new-trade').addEventListener('click', openNewTradeModal);
    document.getElementById('j-modal-cancel').addEventListener('click', function() { closeModal('j-modal-new'); });
    document.getElementById('j-modal-save').addEventListener('click', saveTrade);

    // Close trade
    document.getElementById('j-close-cancel').addEventListener('click', function() { closeModal('j-modal-close'); });
    document.getElementById('j-close-save').addEventListener('click', closeTrade);

    // Direction toggle
    document.querySelectorAll('#j-form-direction .j-dir-toggle-btn').forEach(function(btn) {
        btn.addEventListener('click', function() { setDirection(btn.getAttribute('data-dir')); });
    });

    // R:R preview
    ['j-form-entry', 'j-form-stop', 'j-form-target'].forEach(function(id) {
        document.getElementById(id).addEventListener('input', updateRRPreview);
    });

    // Sliders
    document.getElementById('j-form-confidence').addEventListener('input', function() {
        document.getElementById('j-form-confidence-val').textContent = this.value;
    });
    document.getElementById('j-form-mood').addEventListener('input', function() {
        document.getElementById('j-form-mood-val').textContent = this.value;
    });

    // Setup type -> checklist
    document.getElementById('j-form-setup').addEventListener('change', function() {
        loadChecklistForSetup(this.value.trim());
    });
    document.getElementById('j-form-setup').addEventListener('blur', function() {
        loadChecklistForSetup(this.value.trim());
    });

    // Mistake pills
    document.getElementById('j-close-mistakes').addEventListener('click', function(e) {
        if (e.target.classList.contains('j-mistake-pill')) {
            e.target.classList.toggle('selected');
        }
    });

    // Filters
    ['j-filter-days', 'j-filter-asset', 'j-filter-direction', 'j-filter-setup', 'j-filter-mistakes'].forEach(function(id) {
        var el = document.getElementById(id);
        el.addEventListener('change', function() {
            if (id === 'j-filter-days') loadTrades();
            else renderTable();
        });
        if (el.tagName === 'INPUT') {
            el.addEventListener('input', renderTable);
        }
    });

    document.getElementById('j-filter-clear').addEventListener('click', function() {
        document.getElementById('j-filter-asset').value = '';
        document.getElementById('j-filter-direction').value = '';
        document.getElementById('j-filter-setup').value = '';
        document.getElementById('j-filter-mistakes').value = '';
        renderTable();
    });

    // Sort
    document.querySelectorAll('.j-table th[data-sort]').forEach(function(th) {
        th.addEventListener('click', function() {
            var field = th.getAttribute('data-sort');
            if (_sortField === field) _sortAsc = !_sortAsc;
            else { _sortField = field; _sortAsc = true; }
            renderTable();
        });
    });

    // Table click delegation
    document.getElementById('j-trade-tbody').addEventListener('click', function(e) {
        var btn = e.target.closest('[data-action]');
        if (btn) {
            var action = btn.getAttribute('data-action');
            var id = parseInt(btn.getAttribute('data-id'));
            if (action === 'close') openCloseModal(id);
            else if (action === 'edit') editTrade(id);
            else if (action === 'expand') renderDetailRow(id);
            else if (action === 'delete') deleteTrade(id);
            else if (action === 'share') shareTrade(id);
            else if (action === 'ai-review') requestAIReview(id);
            return;
        }
        var cb = e.target.closest('.j-row-check');
        if (cb) {
            var tid = parseInt(cb.getAttribute('data-id'));
            if (cb.checked) _selected.add(tid);
            else _selected.delete(tid);
            updateBulkButtons();
            var row = cb.closest('tr');
            if (row) row.classList.toggle('selected', cb.checked);
        }
    });

    // Select all
    document.getElementById('j-select-all').addEventListener('change', function() {
        var checked = this.checked;
        _selected.clear();
        document.querySelectorAll('.j-row-check').forEach(function(cb) {
            cb.checked = checked;
            var tid = parseInt(cb.getAttribute('data-id'));
            if (checked) _selected.add(tid);
            var row = cb.closest('tr');
            if (row) row.classList.toggle('selected', checked);
        });
        updateBulkButtons();
    });

    // Bulk actions
    document.getElementById('j-btn-bulk-delete').addEventListener('click', bulkDelete);
    document.getElementById('j-btn-bulk-tag').addEventListener('click', function() {
        document.getElementById('j-bulk-tag-input').value = '';
        document.getElementById('j-modal-bulk-tag').classList.add('active');
        setTimeout(function() { document.getElementById('j-bulk-tag-input').focus(); }, 50);
    });
    document.getElementById('j-bulk-tag-cancel').addEventListener('click', function() {
        closeModal('j-modal-bulk-tag');
    });
    document.getElementById('j-bulk-tag-apply').addEventListener('click', function() {
        var tag = document.getElementById('j-bulk-tag-input').value.trim();
        if (!tag) { showToast('Enter a setup type', 'error'); return; }
        closeModal('j-modal-bulk-tag');
        api('POST', '/api/journal/trades/bulk', {
            trade_ids: Array.from(_selected),
            action: 'update',
            setup_type: tag
        }).then(function(data) {
            _selected.clear();
            showToast(data.affected + ' trades updated', 'success');
            loadTrades();
        }).catch(function(e) { showToast(e.message, 'error'); });
    });

    // Import
    document.getElementById('j-btn-import').addEventListener('click', importDrawings);

    // Equity curve
    document.getElementById('j-equity-days').addEventListener('change', JournalAnalyticsCharts.loadEquityCurve);

    // Analytics period selector
    document.getElementById('j-analytics-days').addEventListener('change', JournalAnalyticsCharts.loadAnalytics);

    // AI features
    document.getElementById('j-btn-weekly-review').addEventListener('click', requestWeeklyReview);
    document.getElementById('j-btn-nl-query').addEventListener('click', sendNLQuery);
    document.getElementById('j-nl-input').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') sendNLQuery();
    });
    // Show NL output area on first input focus
    document.getElementById('j-nl-input').addEventListener('focus', function() {
        document.getElementById('j-nl-output').style.display = '';
    });
    document.getElementById('j-ai-review-close').addEventListener('click', function() {
        closeModal('j-modal-ai-review');
    });

    // Close modals on overlay click
    document.querySelectorAll('.j-modal-overlay').forEach(function(overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) overlay.classList.remove('active');
        });
    });

    // Escape closes modals
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            document.querySelectorAll('.j-modal-overlay.active').forEach(function(o) { o.classList.remove('active'); });
        }
    });

    // Playbooks
    document.getElementById('j-btn-new-playbook').addEventListener('click', JournalPlaybooksGoals.openNewPlaybookModal);
    document.getElementById('j-pb-cancel').addEventListener('click', function() { closeModal('j-modal-playbook'); });
    document.getElementById('j-pb-save').addEventListener('click', JournalPlaybooksGoals.savePlaybook);

    // Goals
    document.getElementById('j-btn-edit-goals').addEventListener('click', JournalPlaybooksGoals.openGoalsModal);
    document.getElementById('j-goal-cancel').addEventListener('click', function() { closeModal('j-modal-goals'); });
    document.getElementById('j-goal-save').addEventListener('click', JournalPlaybooksGoals.saveGoals);

    // Export
    document.getElementById('j-btn-export-csv').addEventListener('click', JournalPlaybooksGoals.exportCSV);
    document.getElementById('j-btn-export-json').addEventListener('click', JournalPlaybooksGoals.exportJSON);
    if (!window._isPremium) {
        ['j-btn-export-csv', 'j-btn-export-json'].forEach(function(id) {
            var btn = document.getElementById(id);
            if (btn) { btn.style.opacity = '0.4'; btn.title = 'Export requires a subscription'; }
        });
    }

    // Initialize sub-modules
    initCalendar();
    initSessions();
    JournalPlaybooksGoals.initGoals();

    // Initial load
    loadTrades();
    loadMistakeTags();
    loadChecklists();
    JournalAnalyticsCharts.loadEquityCurve();
    loadCreditBalance();

    // Resize handler for equity curve
    var resizeTimer;
    window.addEventListener('resize', function() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(JournalAnalyticsCharts.loadEquityCurve, 250);
    });
}

// Shared surface for the extracted journal modules. Assigned
// HERE, at the end of the IIFE, not at the top: MONTH_NAMES is a `var`
// initialised further down, so an early export would capture undefined.
// All four are declared exactly once and never reassigned, so binding them
// by reference is safe. journal-page.js is deferred FIRST in journal.html,
// so this runs before the extracted modules' IIFEs.
window.JournalCore = {
    api: api,
    kpi: kpi,
    closeModal: closeModal,
    MONTH_NAMES: MONTH_NAMES
};

document.addEventListener('DOMContentLoaded', init);

})();
