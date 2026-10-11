/* Plays Panel — thesis-first market belief tracking
 * Deps: split-layout.js (setViewMode), shared-utils.js (showToast), csrf-utils.js (getCSRFToken) */

var _playsData = [];
var _playsCreating = false;
var _playsExpandedId = null;
var _playsDropdownId = null;
// The creation-form state moved to plays-creation-form.js.

// _playBacktestCache moved to plays-backtest-panel.js.

// ── Tooltips ──

var PLAY_TYPE_TOOLTIPS = {
    directional: 'Price goes to a specific level',
    macro: 'Market conditions lead to an outcome',
    event: 'Specific event triggers a reaction'
};
var PLAY_APPROACH_TOOLTIPS = {
    single: 'One entry, one SL, one TP',
    scaled: 'Build position at multiple levels',
    defensive: 'Hedge or lighter exposure'
};
var PLAY_DIRECTION_LABELS = {
    long: { arrow: '\u25B2', cls: 'long' },
    short: { arrow: '\u25BC', cls: 'short' },
    neutral: { arrow: '\u25C6', cls: 'neutral' },
    conditional: { arrow: '\u25C7', cls: 'neutral' }
};

var PLAY_STATUS_ORDER = ['acting', 'watching', 'expired', 'invalidated', 'completed'];

// ── Initialization ──

function initPlaysPanel() {
    var btn = document.getElementById('plays-new-btn');
    if (btn) btn.addEventListener('click', function() {
        // Free tier has max_plays=0 — intercept before the form opens
        // rather than letting the server bounce the submit with a 403.
        if (_isPlaysLocked()) { _showPlaysUpgrade(); return; }
        _showCreationForm();
    });

    // Close dropdown on outside click
    document.addEventListener('click', function(e) {
        if (_playsDropdownId && !e.target.closest('.play-card-menu') && !e.target.closest('.play-card-dropdown')) {
            _closeDropdown();
        }
    });
}

// Tier gate helpers. Plays is a Member feature; Free users see a
// locked upsell card instead of the "Create your first play" bait.
//
// Three sources of truth disagreed, and the one a new user saw was
// the wrong one. The server grants free users max_plays=3 (TIER_LIMITS in
// carrac/auth/decorators.py) and only 403s on the 4th, while this returned true
// for every free user — rendering a paywall directly beneath the "FREE during
// beta" badge in templates/index.html. The comment above claims free
// tier has max_plays=0; that has not been true for a long time.
//
// While payments are off, beta means beta: nothing is locked. The tier branch
// is kept for when payments go live — but note it still disagrees with the
// server's 3-play allowance, so the post-beta rule is a product decision, not
// something to infer from this file.
function _isPlaysLocked() {
    if (window.PAYMENTS_ENABLED === false) return false;
    var tier = (window._userTier || 'free').toLowerCase();
    return tier === 'free';
}

function _showPlaysUpgrade() {
    if (typeof showUpgradeModal === 'function') {
        showUpgradeModal({ source: 'plays_locked', feature: 'plays' });
        return;
    }
    if (typeof showToast === 'function') {
        showToast('This needs a higher plan. An admin can change plans under Admin → Users.');
    }
}

// ── Data Fetch ──

function refreshPlays() {
    var headers = {};
    var csrf = document.querySelector('meta[name="csrf-token"]');
    if (csrf) headers['X-CSRFToken'] = csrf.content;

    fetch('/api/plays?include_resolved=true&limit=200', {
        credentials: 'same-origin',
        headers: headers
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        _playsData = data.plays || [];
        _renderPlaysList(_playsData);
    })
    .catch(function(err) {
        console.error('Failed to load plays:', err);
        var el = document.getElementById('plays-content');
        if (el) el.innerHTML = '<div class="plays-empty-state"><div class="plays-empty-title">Failed to load plays</div></div>';
    });
}

// ── Rendering ──

function _renderPlaysList(plays) {
    var el = document.getElementById('plays-content');
    if (!el) return;

    // Update count
    var countEl = document.getElementById('plays-count');
    var activeCount = plays.filter(function(p) { return p.status !== 'completed' && p.status !== 'invalidated'; }).length;
    if (countEl) countEl.textContent = activeCount > 0 ? '(' + activeCount + ')' : '';

    if (_playsCreating) return; // Don't clobber form

    if (!plays.length) {
        el.innerHTML = _renderEmptyState();
        _initEmptyStateCTA(el);
        return;
    }

    // Group by status
    var groups = {};
    PLAY_STATUS_ORDER.forEach(function(s) { groups[s] = []; });
    plays.forEach(function(p) {
        if (groups[p.status]) groups[p.status].push(p);
    });

    var html = '';
    PLAY_STATUS_ORDER.forEach(function(status) {
        var g = groups[status];
        if (!g || !g.length) return;
        html += '<div class="play-status-group">';
        html += '<div class="play-status-group-header">' + status + ' (' + g.length + ')</div>';
        g.forEach(function(play) {
            html += _renderPlayCard(play);
        });
        html += '</div>';
    });

    el.innerHTML = html;
    _bindCardEvents(el);

    // lazy-load Hold-N backtest for each visible play (cached server-side 6h)
    plays.forEach(function(p) {
        if (p && _canBacktestPlay(p)) _loadPlayBacktest(p.id);
    });
}

function _renderPlayCard(play) {
    var dir = PLAY_DIRECTION_LABELS[play.direction] || PLAY_DIRECTION_LABELS.neutral;
    var conditions = play.conditions || { rules: [] };
    var rules = conditions.rules || [];
    var metCount = rules.filter(function(r) { return r.met; }).length;
    var totalCount = rules.length;
    var pct = totalCount > 0 ? Math.round(metCount / totalCount * 100) : 0;
    var expanded = _playsExpandedId === play.id;

    var html = '<div class="play-card ' + dir.cls + (expanded ? ' expanded' : '') + '" data-play-id="' + play.id + '">';

    // Header
    html += '<div class="play-card-header">';
    html += '<span class="play-direction-arrow ' + dir.cls + '">' + dir.arrow + '</span>';
    html += '<span class="play-card-title">' + escapeHtml(play.title) + '</span>';
    html += '<button class="play-card-menu" data-play-id="' + play.id + '">\u22EF</button>';
    html += '</div>';

    // Meta
    html += '<div class="play-card-meta">';
    html += '<span class="play-type-badge">' + (play.type || 'directional') + '</span>';
    html += '<span class="play-approach-badge">' + (play.approach || 'single') + '</span>';
    if (play.primary_asset) html += '<span class="play-type-badge">' + play.primary_asset + '</span>';
    html += '</div>';

    // Conditions meter — B3 per-segment ticks (A1); falls back to nothing when no rules
    if (totalCount > 0) {
        html += '<div class="play-conditions-bar b3-segmented">';
        for (var si = 0; si < totalCount; si++) {
            html += '<span class="b3-seg' + (si < metCount ? ' met' : '') + '"></span>';
        }
        html += '<span class="play-conditions-label">' + metCount + '/' + totalCount + '</span>';
        html += '</div>';
        // B3 readout line (A2-lite): the next unmet condition, collapsed cards only
        if (!expanded) {
            var nextRule = null;
            for (var ri = 0; ri < rules.length; ri++) { if (!rules[ri].met) { nextRule = rules[ri]; break; } }
            if (nextRule) {
                html += '<div class="play-next-line">next · ' + escapeHtml(nextRule.label || nextRule.type || 'condition') + '</div>';
            }
        }
    }

    // Footer
    html += '<div class="play-card-footer">';
    html += '<span class="play-status-pill ' + play.status + '">' + play.status + '</span>';
    if (play.updated_at && totalCount > 0) {
        html += '<span class="play-last-checked" title="Last condition check">' + _timeAgo(play.updated_at) + '</span>';
    }
    // Hold-N evidence badge (async)
    html += _renderPlayBacktestBadge(play);
    // Shared-state chip (slug link + view count from list enrichment)
    if (play.share) {
        html += '<span class="play-share-chip" data-play-id="' + play.id +
            '" title="Public share link — ' + (play.share.view_count || 0) +
            ' views. Click to manage." style="cursor:pointer;color:#3fb950;font-size:0.62rem">🔗 ' +
            (play.share.view_count || 0) + '</span>';
    }
    html += '<span class="play-age">' + _timeAgo(play.created_at) + '</span>';
    html += '<span class="play-expand-hint">' + (expanded ? '\u25B4' : '\u25BE') + '</span>';
    html += '</div>';

    // Expanded detail
    if (expanded) {
        html += '<div class="play-card-detail">';
        if (play.thesis) html += '<div class="play-thesis">' + escapeHtml(play.thesis) + '</div>';

        if (totalCount > 0) {
            html += '<div class="play-conditions-list">';
            rules.forEach(function(r) {
                var cls = r.met ? 'met' : 'unmet';
                var icon = r.met ? '\u2713' : '\u25CB';
                var label = r.label || r.type || 'condition';
                var evidence = r.evidence ? ' \u2014 ' + escapeHtml(r.evidence) : '';
                html += '<div class="play-condition ' + cls + '">' + icon + ' ' + escapeHtml(label) +
                    '<span class="play-condition-evidence">' + evidence + '</span></div>';
            });
            html += '</div>';
        }

        // Invalidation condition
        if (play.invalidation && play.invalidation.type) {
            var invMet = play.invalidation.met;
            html += '<div class="play-invalidation">';
            html += '<span class="play-invalidation-label">Invalidation:</span> ';
            html += '<span class="play-condition ' + (invMet ? 'met' : 'unmet') + '">';
            html += (invMet ? '\u26A0' : '\u25CB') + ' ' + escapeHtml(play.invalidation.label || play.invalidation.type);
            if (play.invalidation.evidence) html += ' \u2014 ' + escapeHtml(play.invalidation.evidence);
            html += '</span></div>';
        }

        // Condition/invalidation edit buttons
        html += '<div class="play-cond-actions">';
        if (totalCount > 0) {
            html += '<button class="play-cond-btn" data-play-id="' + play.id + '" data-mode="conditions">\u270E Edit Conditions</button>';
        } else {
            html += '<button class="play-cond-btn" data-play-id="' + play.id + '" data-mode="conditions">+ Add Conditions</button>';
        }
        html += '<button class="play-inv-btn" data-play-id="' + play.id + '" data-mode="invalidation">' +
            (play.invalidation && play.invalidation.type ? '\u270E Edit Invalidation' : '+ Add Invalidation') + '</button>';
        html += '</div>';

        if (play.action_plan) html += '<div class="play-action-plan">' + escapeHtml(play.action_plan) + '</div>';

        // Refresh button + eval result container
        html += '<button class="play-refresh-btn" data-play-id="' + play.id + '" title="Re-evaluate conditions">\u21BB Check</button>';
        html += '<div class="play-eval-result" id="play-eval-' + play.id + '" style="display:none"></div>';

        // Hold-N backtest evidence block (expanded detail)
        html += _renderPlayBacktestDetail(play);

        // Alert buttons for price-based conditions
        html += _renderAlertButtons(play);

        // Trade fields (acting status only)
        html += _renderTradeFields(play);

        // Milestones checklist
        html += _renderMilestones(play);

        // Draw Path button (for non-terminal plays)
        if (play.status !== 'completed' && play.status !== 'invalidated') {
            html += '<button class="play-action-btn play-path-btn" data-play-id="' + play.id + '">Draw Path</button>';
        }

        // Status action buttons
        html += '<div class="play-detail-actions">';
        if (play.status === 'watching') {
            html += '<button class="play-action-btn" data-action="acting" data-play-id="' + play.id + '">Enter Trade</button>';
            html += '<button class="play-action-btn" data-action="invalidated" data-play-id="' + play.id + '">Invalidate</button>';
            html += '<button class="play-action-btn" data-action="completed" data-play-id="' + play.id + '">Complete</button>';
        } else if (play.status === 'acting') {
            html += '<button class="play-action-btn" data-action="completed" data-play-id="' + play.id + '">Complete</button>';
            html += '<button class="play-action-btn" data-action="invalidated" data-play-id="' + play.id + '">Invalidate</button>';
        } else if (play.status === 'expired') {
            html += '<button class="play-action-btn" data-action="watching" data-play-id="' + play.id + '">Reactivate</button>';
        }
        // Public share link (any status — completed plays are the brag/process artifact)
        html += '<button class="play-action-btn play-share-btn" data-play-id="' + play.id + '">' +
            (play.share ? '🔗 Shared' : 'Share') + '</button>';
        html += '</div>';

        if (play.resolution) html += '<div class="play-thesis" style="margin-top:6px;color:#c9d1d9;font-style:normal"><strong>Resolution:</strong> ' + escapeHtml(play.resolution) + '</div>';

        // Journal button for completed/invalidated plays
        if (play.status === 'completed' || play.status === 'invalidated') {
            html += '<button class="play-action-btn play-journal-btn" data-play-id="' + play.id + '">Log to Journal</button>';
        }

        html += '</div>';
    }

    html += '</div>';
    return html;
}

// The backtest evidence panel moved to plays-backtest-panel.js.

function _renderEmptyState() {
    var ctaHtml, descHtml;
    if (_isPlaysLocked()) {
        // Free tier — the Create button was a dead bait that ended in
        // a 403. Replace with a Member upsell card so the tier boundary is
        // visible before the user types a single character.
        descHtml =
            'Plays are testable hypotheses about the market \u2014 a thesis with conditions ' +
            'that confirm or invalidate it. Member unlocks unlimited plays, auto-evaluated ' +
            'conditions, and backtests on each thesis.';
        ctaHtml = '<button class="plays-empty-cta" id="plays-empty-cta-btn">Unlock Plays with Member</button>' +
                  '<div class="plays-empty-sub">Plays are a Member feature.</div>';
    } else {
        descHtml =
            'Plays are testable hypotheses about the market \u2014 a thesis with conditions ' +
            'that confirm or invalidate it. Track directional setups, macro views, ' +
            'and event plays with live condition monitoring.';
        ctaHtml = '<button class="plays-empty-cta" id="plays-empty-cta-btn">Create your first play</button>';
    }
    return '<div class="plays-empty-state">' +
        '<div class="plays-empty-icon"><svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg></div>' +
        '<div class="plays-empty-title">Track your market beliefs</div>' +
        '<div class="plays-empty-desc">' + descHtml + '</div>' +
        ctaHtml +
    '</div>';
}

function _initEmptyStateCTA(container) {
    var btn = container.querySelector('#plays-empty-cta-btn');
    if (!btn) return;
    if (_isPlaysLocked()) {
        btn.addEventListener('click', function() { _showPlaysUpgrade(); });
    } else {
        btn.addEventListener('click', function() { _showCreationForm(); });
    }
}

// The 4-step guided creation form moved to plays-creation-form.js.

// ── Quick Create from Position Drawing ──

function quickCreatePlay(posData) {
    var dir = posData.direction || 'long';
    var title = (dir === 'long' ? 'Long' : dir === 'short' ? 'Short' : dir.charAt(0).toUpperCase() + dir.slice(1)) +
                ' ' + (posData.asset || '?') + ' from $' + (posData.entry ? posData.entry.toLocaleString() : '?');

    fetch('/api/plays', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': _getCSRF()
        },
        body: JSON.stringify({
            title: title,
            type: 'directional',
            approach: 'single',
            direction: dir,
            primary_asset: (posData.asset || '').toUpperCase()
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.play) {
            if (typeof showToast === 'function') showToast('Play created: ' + title);
            if (posData.drawingId) {
                fetch('/api/plays/' + data.play.id + '/link-drawing', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _getCSRF() },
                    body: JSON.stringify({ drawing_id: posData.drawingId })
                });
            }
            refreshPlays();
        } else if (data.error) {
            if (typeof showToast === 'function') showToast(data.error);
        }
    })
    .catch(function() {
        if (typeof showToast === 'function') showToast('Failed to create play');
    });
}

function showTrackAsPlayPrompt(posData) {
    // Remove existing prompt
    var old = document.querySelector('.play-prompt-toast');
    if (old) old.remove();

    var el = document.createElement('div');
    el.className = 'play-prompt-toast';
    el.innerHTML = '<span>Track as Play?</span>' +
        '<button class="play-prompt-yes">Yes</button>' +
        '<button class="play-prompt-no">\u00D7</button>';
    document.body.appendChild(el);

    el.querySelector('.play-prompt-yes').addEventListener('click', function() {
        quickCreatePlay(posData);
        el.remove();
    });
    el.querySelector('.play-prompt-no').addEventListener('click', function() { el.remove(); });
    setTimeout(function() { if (el.parentNode) el.remove(); }, 8000);
}

// ── Alert Buttons ──

function _conditionAlertType(rule) {
    var t = rule.type;
    if (t === 'price_level' || t === 'candle_close' || t === 'price_zone') return 'price';
    if (t === 'indicator_state' || t === 'indicator_crosses' || t === 'indicator_value') return 'indicator';
    if (t === 'consensio_threshold') return 'indicator';
    if (t === 'cross_asset') return 'price';
    return null;
}

function _renderAlertButtons(play) {
    if (!play.primary_asset) return '';
    var rules = (play.conditions || {}).rules || [];
    var inv = play.invalidation;
    var hasAlertable = rules.some(function(r) { return _conditionAlertType(r); }) ||
        (inv && _conditionAlertType(inv));
    if (!hasAlertable) return '';
    return '<button class="play-alert-btn play-create-alert-btn" data-play-id="' + play.id + '">Create Alert</button>';
}

function _showAlertChecklist(playId) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var rules = (play.conditions || {}).rules || [];
    var inv = play.invalidation;

    // Remove existing checklist
    var old = document.querySelector('.play-alert-checklist');
    if (old) old.remove();

    var html = '<div class="play-alert-checklist">';
    html += '<div style="font-size:0.65rem;color:#8b949e;margin-bottom:6px;font-weight:600">Select conditions to create alerts for:</div>';
    var idx = 0;
    rules.forEach(function(r, i) {
        var aType = _conditionAlertType(r);
        if (!aType) return;
        var summary = typeof _condSummary === 'function' ? _condSummary(r) : r.type;
        var badge = aType === 'price' ? 'Price' : 'Indicator';
        html += '<label class="play-alert-check-item"><input type="checkbox" checked data-rule-idx="' + i + '" data-is-inv="0">' +
            '<span class="play-alert-check-label">' + escapeHtml(summary) + '</span>' +
            '<span class="play-alert-check-badge">' + badge + '</span></label>';
        idx++;
    });
    if (inv && _conditionAlertType(inv)) {
        var invSummary = typeof _condSummary === 'function' ? _condSummary(inv) : inv.type;
        html += '<label class="play-alert-check-item"><input type="checkbox" checked data-rule-idx="-1" data-is-inv="1">' +
            '<span class="play-alert-check-label" style="color:#f0b90b">Invalidation: ' + escapeHtml(invSummary) + '</span>' +
            '<span class="play-alert-check-badge">Price</span></label>';
    }
    html += '<div style="margin-top:6px;display:flex;gap:6px">' +
        '<button class="play-alert-create-selected" data-play-id="' + playId + '">Create Selected</button>' +
        '<button class="play-alert-cancel" style="background:none;border:1px solid #30363d;color:#8b949e;padding:2px 8px;border-radius:3px;font-size:0.6rem;cursor:pointer">Cancel</button>' +
        '</div></div>';

    // Insert after the button
    var btn = document.querySelector('.play-create-alert-btn[data-play-id="' + playId + '"]');
    if (btn) btn.insertAdjacentHTML('afterend', html);

    // Wire cancel
    var cancel = document.querySelector('.play-alert-cancel');
    if (cancel) cancel.addEventListener('click', function(e) {
        e.stopPropagation();
        var cl = document.querySelector('.play-alert-checklist');
        if (cl) cl.remove();
    });

    // Wire create selected
    var createBtn = document.querySelector('.play-alert-create-selected');
    if (createBtn) createBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        _createAlertsFromChecklist(playId);
    });
}

function _createAlertsFromChecklist(playId) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var rules = (play.conditions || {}).rules || [];
    var inv = play.invalidation;
    var checks = document.querySelectorAll('.play-alert-checklist input[type="checkbox"]:checked');
    var priceCount = 0;
    var indicatorRules = [];

    checks.forEach(function(cb) {
        var isInv = cb.getAttribute('data-is-inv') === '1';
        var rule = isInv ? inv : rules[parseInt(cb.getAttribute('data-rule-idx'))];
        if (!rule) return;
        var aType = _conditionAlertType(rule);
        if (aType === 'price') {
            var p = rule.params || rule;
            var price = p.level || p.high || p.low;
            if (!price) return;
            // Create price alert directly
            fetch('/api/alerts', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _getCSRF() },
                body: JSON.stringify({
                    symbol: (rule.type === 'cross_asset' ? (p.asset || play.primary_asset) : play.primary_asset).toUpperCase(),
                    price: parseFloat(price),
                    condition: p.relation || 'above',
                    note: 'From play: ' + play.title,
                    frequency: 'once_per_bar'
                })
            });
            priceCount++;
        } else if (aType === 'indicator') {
            indicatorRules.push(rule);
        }
    });

    // Remove checklist
    var cl = document.querySelector('.play-alert-checklist');
    if (cl) cl.remove();

    if (priceCount) {
        if (typeof showToast === 'function') showToast('Created ' + priceCount + ' price alert' + (priceCount > 1 ? 's' : ''));
    }

    // For indicator alerts, pre-fill the drawer one at a time
    if (indicatorRules.length && typeof prefillAlertFromCondition === 'function') {
        prefillAlertFromCondition(indicatorRules[0], play.primary_asset, play.title);
        if (indicatorRules.length > 1) {
            if (typeof showToast === 'function') showToast('Review indicator alert in drawer. ' + (indicatorRules.length - 1) + ' more to set up manually.');
        }
    }
}

// ── Journal Logging ──

function _logPlayToJournal(playId) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;

    var params = [];
    params.push('prefill=1');
    if (play.primary_asset) params.push('asset=' + encodeURIComponent(play.primary_asset));
    if (play.direction && play.direction !== 'neutral' && play.direction !== 'conditional') {
        params.push('direction=' + encodeURIComponent(play.direction));
    }

    // Extract TP/SL from linked position drawings
    var linked = play.linked_drawing_ids || [];
    if (linked.length && typeof localStorage !== 'undefined') {
        var sym = (play.primary_asset || '').toUpperCase();
        try {
            var stored = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            var drawings = stored[sym] || [];
            for (var i = 0; i < linked.length; i++) {
                var d = drawings.find(function(dr) { return dr.id === linked[i]; });
                if (d && d.points && d.points.length >= 3 &&
                    (d.name === 'position' || d.name === 'longPosition' || d.name === 'shortPosition')) {
                    // Position drawing: pts[0]=entry, pts[1]=TP, pts[2]=SL (swap 1&2 for standard format)
                    var entry = d.points[0] ? d.points[0].value : null;
                    var tp = d.points[1] ? d.points[1].value : null;
                    var sl = d.points[2] ? d.points[2].value : null;
                    if (entry) params.push('entry=' + entry);
                    if (sl) params.push('sl=' + sl);
                    if (tp) params.push('tp=' + tp);
                    break;
                }
            }
        } catch (e) { /* localStorage parse error, skip */ }
    }

    // Add thesis as notes
    var notes = [];
    if (play.thesis) notes.push(play.thesis);
    if (play.resolution) notes.push('Resolution: ' + play.resolution);
    if (notes.length) params.push('notes=' + encodeURIComponent(notes.join('\n\n')));

    window.location.href = '/journal?' + params.join('&');
}

// ── Trade Fields ──

function _renderTradeFields(play) {
    if (play.status !== 'acting') return '';
    var m = play.metadata || {};
    var hasFields = m.conviction || m.entry_low || m.entry_high || m.stop_loss || (m.targets && m.targets.length);

    var html = '<div class="play-trade-fields" data-play-id="' + play.id + '">';
    html += '<div class="play-trade-header"><span style="font-size:0.65rem;color:#8b949e;font-weight:600">Trade Details</span>';
    html += '<button class="play-trade-edit-btn" data-play-id="' + play.id + '" style="font-size:0.6rem;background:none;border:none;color:#3fb950;cursor:pointer">\u270E</button></div>';

    // Conviction stars
    var conv = m.conviction || 0;
    html += '<div class="play-trade-row"><span class="play-trade-label">Conviction</span>';
    html += '<span class="play-conviction-stars" data-play-id="' + play.id + '">';
    for (var i = 1; i <= 5; i++) {
        html += '<span class="play-star" data-star="' + i + '" data-play-id="' + play.id + '">' + (i <= conv ? '\u2605' : '\u2606') + '</span>';
    }
    html += '</span></div>';

    // Entry range
    if (m.entry_low || m.entry_high) {
        html += '<div class="play-trade-row"><span class="play-trade-label">Entry</span>';
        html += '<span class="play-trade-value">$' + _fmtNum(m.entry_low) + (m.entry_high ? ' \u2013 $' + _fmtNum(m.entry_high) : '') + '</span></div>';
    }

    // Stop loss
    if (m.stop_loss) {
        html += '<div class="play-trade-row"><span class="play-trade-label">Stop Loss</span>';
        html += '<span class="play-trade-value play-sl">$' + _fmtNum(m.stop_loss) + '</span></div>';
    }

    // Targets
    if (m.targets && m.targets.length) {
        m.targets.forEach(function(t, i) {
            html += '<div class="play-trade-row"><span class="play-trade-label">' + (t.label || 'TP' + (i + 1)) + '</span>';
            html += '<span class="play-trade-value" style="color:#3fb950">$' + _fmtNum(t.price) + '</span></div>';
        });
    }

    // Inline edit form (hidden by default)
    html += '<div class="play-trade-edit" id="play-trade-edit-' + play.id + '" style="display:none">';
    html += '<div class="play-trade-row"><label class="play-trade-label">Entry Low</label><input class="plays-form-input play-trade-input" data-field="entry_low" type="number" step="any" value="' + (m.entry_low || '') + '"></div>';
    html += '<div class="play-trade-row"><label class="play-trade-label">Entry High</label><input class="plays-form-input play-trade-input" data-field="entry_high" type="number" step="any" value="' + (m.entry_high || '') + '"></div>';
    html += '<div class="play-trade-row"><label class="play-trade-label">Stop Loss</label><input class="plays-form-input play-trade-input" data-field="stop_loss" type="number" step="any" value="' + (m.stop_loss || '') + '"></div>';
    html += '<div class="play-trade-row"><label class="play-trade-label">Targets</label><input class="plays-form-input play-trade-input" data-field="targets" placeholder="68000, 72000" value="' + _targetsToStr(m.targets) + '"></div>';
    html += '<button class="play-trade-save-btn" data-play-id="' + play.id + '" style="font-size:0.6rem;padding:2px 8px;margin-top:4px;background:#3fb950;color:#0d1117;border:none;border-radius:3px;cursor:pointer">Save</button>';
    html += '</div>';

    html += '</div>';
    return html;
}

function _targetsToStr(targets) {
    if (!targets || !targets.length) return '';
    return targets.map(function(t) { return t.price; }).join(', ');
}

function _fmtNum(n) {
    if (n == null || n === '') return '?';
    n = parseFloat(n);
    return n >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : n.toString();
}

// ── Milestones ──

function _renderMilestones(play) {
    var m = play.metadata || {};
    var milestones = m.milestones || [];

    var html = '<div class="play-milestones" data-play-id="' + play.id + '">';
    var done = milestones.filter(function(ms) { return ms.done; }).length;
    html += '<div class="play-milestones-header">';
    html += '<span style="font-size:0.65rem;color:#8b949e;font-weight:600">Steps</span>';
    if (milestones.length) html += '<span class="play-ms-count">' + done + '/' + milestones.length + '</span>';
    html += '</div>';

    milestones.forEach(function(ms, idx) {
        html += '<div class="play-ms-item">';
        html += '<input type="checkbox" class="play-ms-check" data-play-id="' + play.id + '" data-idx="' + idx + '"' + (ms.done ? ' checked' : '') + '>';
        html += '<span class="play-ms-label' + (ms.done ? ' done' : '') + '">' + escapeHtml(ms.label) + '</span>';
        html += '<button class="play-ms-remove" data-play-id="' + play.id + '" data-idx="' + idx + '">\u00D7</button>';
        html += '</div>';
    });

    html += '<input class="play-ms-input" data-play-id="' + play.id + '" placeholder="Add a step..." maxlength="200">';
    html += '</div>';
    return html;
}

// ── Price Path Drawing ──

var _playPathDrawMode = false;
var _playPathTempPoints = [];
var _playPathPlayId = null;
var _playPathLastClickTime = 0;

function _startPlayPathDraw(playId) {
    _playPathDrawMode = true;
    _playPathPlayId = playId;
    _playPathTempPoints = [];
    _playPathLastClickTime = 0;

    // Change cursor
    var chartEl = document.getElementById('kline-chart');
    if (chartEl) chartEl.style.cursor = 'crosshair';

    // Show hint
    var hint = document.createElement('div');
    hint.className = 'play-path-hint';
    hint.id = 'play-path-hint';
    hint.textContent = 'Click to add waypoints. Double-click to finish. Esc to cancel.';
    document.body.appendChild(hint);

    document.addEventListener('keydown', _playPathKeyHandler);
}

function _playPathKeyHandler(e) {
    if (e.key === 'Escape') {
        _cancelPlayPathDraw();
    } else if (e.key === 'Enter' && _playPathTempPoints.length >= 2) {
        _finalizePlayPath();
    }
}

function _handlePlayPathClick(e) {
    if (!_playPathDrawMode || typeof klineChart === 'undefined' || !klineChart) return;

    var now = Date.now();
    var dblClick = (now - _playPathLastClickTime) < 350;
    _playPathLastClickTime = now;

    if (dblClick && _playPathTempPoints.length >= 2) {
        _finalizePlayPath();
        return;
    }

    var container = document.getElementById('kline-chart');
    if (!container) return;
    var rect = container.getBoundingClientRect();
    var x = e.clientX - rect.left;
    var y = e.clientY - rect.top;

    try {
        var coord = klineChart.convertFromPixel({ x: x, y: y }, { paneId: 'candle_pane' });
        if (coord && coord.timestamp && coord.value) {
            _playPathTempPoints.push({ timestamp: coord.timestamp, value: coord.value });
        }
    } catch (err) { /* pixel conversion failed, skip */ }
}

function _finalizePlayPath() {
    if (_playPathTempPoints.length < 2) {
        _cancelPlayPathDraw();
        return;
    }

    try {
        var overlay = klineChart.createOverlay({
            name: 'scenarioPath',
            points: _playPathTempPoints.slice()
        });

        // Link drawing to play
        if (_playPathPlayId && overlay) {
            // Save to localStorage via existing drawing system
            if (typeof saveKlineDrawing === 'function') {
                saveKlineDrawing();
            }
            // Link to play via API
            fetch('/api/plays/' + _playPathPlayId + '/link-drawing', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': _getCSRF()
                },
                body: JSON.stringify({ drawing_id: typeof overlay === 'string' ? overlay : 0 })
            }).catch(function(e) {
                if (typeof window.carracLog === 'function') window.carracLog('drawing', 'link-drawing-to-play failed — drawing saved but not linked to the play', String((e && e.message) || e));
                if (typeof showToast === 'function') showToast('Drawing saved, but could not link it to the play');
            });
        }

        if (typeof showToast === 'function') showToast('Path drawn and linked to play');
    } catch (err) {
        if (typeof showToast === 'function') showToast('Failed to create path');
    }

    _cleanupPlayPathDraw();
}

function _cancelPlayPathDraw() {
    _cleanupPlayPathDraw();
    if (typeof showToast === 'function') showToast('Path drawing cancelled');
}

function _cleanupPlayPathDraw() {
    _playPathDrawMode = false;
    _playPathTempPoints = [];
    _playPathPlayId = null;
    _playPathLastClickTime = 0;

    var chartEl = document.getElementById('kline-chart');
    if (chartEl) chartEl.style.cursor = '';

    var hint = document.getElementById('play-path-hint');
    if (hint) hint.remove();

    document.removeEventListener('keydown', _playPathKeyHandler);
}

// ── Condition Modal Integration ──

function _openPlayConditionModal(playId, mode) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;

    if (mode === 'invalidation') {
        // Wrap single invalidation into conditions format for the modal
        var inv = play.invalidation && play.invalidation.type ? play.invalidation : null;
        var wrapped = { rules: inv ? [Object.assign({}, inv, { id: inv.id || 'inv1' })] : [], logic: 'all' };
        openConditionModal('play', playId, wrapped, function(conditions) {
            // Unwrap: take first rule as the invalidation object (or null if empty)
            var newInv = (conditions.rules && conditions.rules.length > 0)
                ? conditions.rules[0] : null;
            _savePlayField(playId, { invalidation: newInv });
        });
    } else {
        openConditionModal('play', playId, play.conditions || { rules: [], logic: 'all' }, function(conditions) {
            // Soft direction-mismatch warning before saving
            if (!_warnDirectionMismatch(play.direction || 'long', conditions)) return;
            _savePlayField(playId, { conditions: conditions });
        });
    }
}

function _savePlayField(playId, fields) {
    fetch('/api/plays/' + playId, {
        method: 'PUT',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': _getCSRF()
        },
        body: JSON.stringify(fields)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.play) {
            if (typeof showToast === 'function') showToast('Play updated');
            refreshPlays();
        } else if (data.error) {
            if (typeof showToast === 'function') showToast(data.error);
        }
    })
    .catch(function() {
        if (typeof showToast === 'function') showToast('Failed to update play');
    });
}

// ── Inline Evaluation Results ──

function _renderEvalResult(playId, play, evalData) {
    var el = document.getElementById('play-eval-' + playId);
    if (!el) return;

    var rules = (play.conditions || {}).rules || [];
    var met = evalData.met_count != null ? evalData.met_count : 0;
    var total = evalData.total_count != null ? evalData.total_count : rules.length;

    var html = '<div class="play-eval-header">' + met + '/' + total + ' conditions met</div>';
    rules.forEach(function(r) {
        var icon = r.met ? '\u2713' : '\u2717';
        var cls = r.met ? 'cond-test-met' : 'cond-test-unmet';
        var text = r.evidence || (typeof _condSummary === 'function' ? _condSummary(r) : r.label || r.type);
        html += '<div class="' + cls + '">' + icon + ' ' + escapeHtml(text) + '</div>';
    });
    if (evalData.invalidation_triggered) {
        html += '<div class="play-eval-warn">\u26A0 Invalidation condition triggered!</div>';
    }

    el.innerHTML = html;
    el.style.display = 'block';

    // Auto-hide after 10 seconds
    setTimeout(function() {
        if (el) el.style.display = 'none';
    }, 10000);
}

// ── Card Events ──

function _bindCardEvents(container) {
    // Card click to expand/collapse
    container.querySelectorAll('.play-card').forEach(function(card) {
        card.addEventListener('click', function(e) {
            if (e.target.closest('.play-card-menu') || e.target.closest('.play-action-btn') || e.target.closest('.play-card-dropdown') || e.target.closest('.play-refresh-btn') || e.target.closest('.play-cond-btn') || e.target.closest('.play-inv-btn') || e.target.closest('.play-trade-fields') || e.target.closest('.play-milestones') || e.target.closest('.play-alert-btn') || e.target.closest('.play-journal-btn') || e.target.closest('.play-path-btn') || e.target.closest('.play-bt-open-btn') || e.target.closest('.play-bt-detail') || e.target.closest('.play-share-btn') || e.target.closest('.play-share-chip')) return;
            var id = parseInt(card.getAttribute('data-play-id'));
            _playsExpandedId = _playsExpandedId === id ? null : id;
            _renderPlaysList(_playsData);
        });
    });

    // Menu button
    container.querySelectorAll('.play-card-menu').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var id = parseInt(btn.getAttribute('data-play-id'));
            if (_playsDropdownId === id) {
                _closeDropdown();
            } else {
                _showDropdown(btn, id);
            }
        });
    });

    // Condition/invalidation edit buttons
    container.querySelectorAll('.play-cond-btn, .play-inv-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            var mode = btn.getAttribute('data-mode');
            _openPlayConditionModal(playId, mode);
        });
    });

    // Create Alert button (shows condition checklist)
    container.querySelectorAll('.play-create-alert-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            _showAlertChecklist(playId);
        });
    });

    // Journal logging button
    container.querySelectorAll('.play-journal-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            _logPlayToJournal(playId);
        });
    });

    // Draw Path button
    container.querySelectorAll('.play-path-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            _startPlayPathDraw(playId);
        });
    });

    // Status transition buttons
    container.querySelectorAll('.play-action-btn[data-action]').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            var action = btn.getAttribute('data-action');
            _handleStatusTransition(playId, action);
        });
    });

    // Share button (expanded detail) + shared-chip (card footer)
    container.querySelectorAll('.play-share-btn, .play-share-chip').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            var play = null;
            for (var i = 0; i < _playsData.length; i++) {
                if (_playsData[i].id === playId) { play = _playsData[i]; break; }
            }
            if (window.PlayShare) PlayShare.open(playId, play);
        });
    });

    // Open-in-Backtester button from play expanded view
    container.querySelectorAll('.play-bt-open-btn').forEach(function(btn) {
        _bindPlayBacktestOpenBtn(btn);
    });

    // Refresh / evaluate buttons (enhanced inline results)
    container.querySelectorAll('.play-refresh-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            btn.textContent = '\u21BB ...';
            btn.disabled = true;
            fetch('/api/plays/' + playId + '/evaluate', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': _getCSRF() }
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.play) {
                    // Update local data with fresh conditions
                    var idx = _playsData.findIndex(function(p) { return p.id === playId; });
                    if (idx >= 0) _playsData[idx] = data.play;
                    // Show inline results
                    _renderEvalResult(playId, data.play, data.evaluation || {});
                    btn.textContent = '\u21BB Check';
                    btn.disabled = false;
                } else {
                    btn.textContent = '\u21BB Check';
                    btn.disabled = false;
                    if (typeof showToast === 'function') showToast(data.error || 'Evaluation failed');
                }
            })
            .catch(function() {
                btn.textContent = '\u21BB Check';
                btn.disabled = false;
            });
        });
    });

    // Conviction stars
    container.querySelectorAll('.play-star').forEach(function(star) {
        star.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(star.getAttribute('data-play-id'));
            var val = parseInt(star.getAttribute('data-star'));
            _savePlayMetadata(playId, 'conviction', val);
        });
    });

    // Trade fields edit toggle
    container.querySelectorAll('.play-trade-edit-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            var editEl = document.getElementById('play-trade-edit-' + playId);
            if (editEl) editEl.style.display = editEl.style.display === 'none' ? 'block' : 'none';
        });
    });

    // Trade fields save
    container.querySelectorAll('.play-trade-save-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            var editEl = document.getElementById('play-trade-edit-' + playId);
            if (!editEl) return;
            var fields = {};
            editEl.querySelectorAll('.play-trade-input').forEach(function(inp) {
                var f = inp.getAttribute('data-field');
                if (f === 'targets') {
                    var val = inp.value.trim();
                    fields.targets = val ? val.split(',').map(function(p, i) {
                        return { price: parseFloat(p.trim()), label: 'TP' + (i + 1) };
                    }).filter(function(t) { return !isNaN(t.price); }) : [];
                } else {
                    var v = inp.value.trim();
                    fields[f] = v ? parseFloat(v) : null;
                }
            });
            _savePlayMetadataBulk(playId, fields);
        });
    });

    // Milestone checkboxes
    container.querySelectorAll('.play-ms-check').forEach(function(cb) {
        cb.addEventListener('change', function(e) {
            e.stopPropagation();
            var playId = parseInt(cb.getAttribute('data-play-id'));
            var idx = parseInt(cb.getAttribute('data-idx'));
            _toggleMilestone(playId, idx);
        });
    });

    // Milestone remove buttons
    container.querySelectorAll('.play-ms-remove').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var playId = parseInt(btn.getAttribute('data-play-id'));
            var idx = parseInt(btn.getAttribute('data-idx'));
            _removeMilestone(playId, idx);
        });
    });

    // Milestone add input
    container.querySelectorAll('.play-ms-input').forEach(function(inp) {
        inp.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.stopPropagation();
                e.preventDefault();
                var label = inp.value.trim();
                if (!label) return;
                var playId = parseInt(inp.getAttribute('data-play-id'));
                inp.value = '';
                _addMilestone(playId, label);
            }
        });
        inp.addEventListener('click', function(e) { e.stopPropagation(); });
    });
}

function _showDropdown(anchor, playId) {
    _closeDropdown();
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;

    var dd = document.createElement('div');
    dd.className = 'play-card-dropdown';
    dd.id = 'play-dropdown-' + playId;

    var items = [];

    // Tools — always available for non-terminal plays
    if (play.status !== 'completed' && play.status !== 'invalidated') {
        items.push({ label: 'Edit Conditions', action: 'edit-conditions' });
        items.push({ label: 'Edit Invalidation', action: 'edit-invalidation' });
        items.push({ label: 'Check Conditions', action: 'check' });
        items.push({ label: 'Create Alert', action: 'create-alert' });
        items.push({ label: 'Draw Path', action: 'draw-path' });
        items.push({ label: '---' }); // separator
    }

    // Status transitions
    if (play.status === 'watching') {
        items.push({ label: 'Enter Trade', action: 'acting' });
        items.push({ label: 'Complete', action: 'completed' });
        items.push({ label: 'Invalidate', action: 'invalidated' });
    } else if (play.status === 'acting') {
        items.push({ label: 'Complete', action: 'completed' });
        items.push({ label: 'Invalidate', action: 'invalidated' });
    } else if (play.status === 'expired') {
        items.push({ label: 'Reactivate', action: 'watching' });
    }

    // Journal for terminal plays
    if (play.status === 'completed' || play.status === 'invalidated') {
        items.push({ label: 'Log to Journal', action: 'journal' });
    }

    items.push({ label: 'Delete', action: 'delete', cls: 'danger' });

    items.forEach(function(item) {
        if (item.label === '---') {
            var sep = document.createElement('div');
            sep.className = 'play-card-dropdown-sep';
            dd.appendChild(sep);
            return;
        }
        var btn = document.createElement('button');
        btn.className = 'play-card-dropdown-item' + (item.cls ? ' ' + item.cls : '');
        btn.textContent = item.label;
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (item.action === 'delete') {
                _handleDelete(playId, btn);
                return;
            }
            _closeDropdown();
            if (item.action === 'edit-conditions') {
                _playsExpandedId = playId;
                _renderPlaysList(_playsData);
                _openPlayConditionModal(playId, 'conditions');
            } else if (item.action === 'edit-invalidation') {
                _playsExpandedId = playId;
                _renderPlaysList(_playsData);
                _openPlayConditionModal(playId, 'invalidation');
            } else if (item.action === 'check') {
                _playsExpandedId = playId;
                _renderPlaysList(_playsData);
                var refreshBtn = document.querySelector('.play-refresh-btn[data-play-id="' + playId + '"]');
                if (refreshBtn) refreshBtn.click();
            } else if (item.action === 'create-alert') {
                _playsExpandedId = playId;
                _renderPlaysList(_playsData);
                _showAlertChecklist(playId);
            } else if (item.action === 'draw-path') {
                _startPlayPathDraw(playId);
            } else if (item.action === 'journal') {
                _logPlayToJournal(playId);
            } else {
                _handleStatusTransition(playId, item.action);
            }
        });
        dd.appendChild(btn);
    });

    var card = anchor.closest('.play-card');
    card.style.position = 'relative';
    card.appendChild(dd);
    _playsDropdownId = playId;
}

function _closeDropdown() {
    var dd = document.querySelector('.play-card-dropdown');
    if (dd) dd.remove();
    _playsDropdownId = null;
}

// ── Actions ──

function _handleStatusTransition(playId, newStatus) {
    var body = { status: newStatus };
    if (newStatus === 'completed' || newStatus === 'invalidated') {
        var note = prompt(newStatus === 'completed' ? 'Resolution note (optional):' : 'Why invalidated? (optional):');
        if (note) body.resolution = note;
    }

    fetch('/api/plays/' + playId + '/status', {
        method: 'PUT',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': _getCSRF()
        },
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.play) {
            if (typeof showToast === 'function') showToast('Status: ' + newStatus);
            refreshPlays();
            // Closing a position (completing the play) is the natural
            // share moment. Ask first — opening the modal mints the link
            // (POST get-or-create), so it must stay opt-in.
            if (newStatus === 'completed' && window.PlayShare &&
                window.confirm('Play completed — share the result publicly?')) {
                PlayShare.open(playId, data.play);
            }
        } else if (data.error) {
            if (typeof showToast === 'function') showToast(data.error);
        }
    });
}

var _playDeletePending = null;
var _playDeleteTimer = null;

function _handleDelete(playId, btnEl) {
    // Two-click delete: first click shows "Confirm?", second click deletes
    if (_playDeletePending === playId) {
        _playDeletePending = null;
        if (_playDeleteTimer) { clearTimeout(_playDeleteTimer); _playDeleteTimer = null; }
        _closeDropdown();
        fetch('/api/plays/' + playId, {
            method: 'DELETE',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': _getCSRF() }
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ok) {
                if (_playsExpandedId === playId) _playsExpandedId = null;
                if (typeof showToast === 'function') showToast('Play deleted');
                refreshPlays();
            }
        });
        return;
    }

    _playDeletePending = playId;
    if (btnEl) { btnEl.textContent = 'Confirm Delete?'; btnEl.style.fontWeight = '600'; }
    // Auto-reset after 3 seconds
    _playDeleteTimer = setTimeout(function() {
        _playDeletePending = null;
        _closeDropdown();
    }, 3000);
}

// ── Metadata Helpers ──

function _savePlayMetadata(playId, key, value) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var meta = Object.assign({}, play.metadata || {});
    meta[key] = value;
    _savePlayField(playId, { metadata: meta });
}

function _savePlayMetadataBulk(playId, fields) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var meta = Object.assign({}, play.metadata || {});
    Object.keys(fields).forEach(function(k) { meta[k] = fields[k]; });
    _savePlayField(playId, { metadata: meta });
}

function _toggleMilestone(playId, idx) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var meta = Object.assign({}, play.metadata || {});
    var milestones = (meta.milestones || []).slice();
    if (milestones[idx]) {
        milestones[idx] = Object.assign({}, milestones[idx], { done: !milestones[idx].done });
        meta.milestones = milestones;
        _savePlayField(playId, { metadata: meta });
    }
}

function _addMilestone(playId, label) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var meta = Object.assign({}, play.metadata || {});
    var milestones = (meta.milestones || []).slice();
    if (milestones.length >= 50) {
        if (typeof showToast === 'function') showToast('Max 50 steps');
        return;
    }
    milestones.push({ label: label, done: false });
    meta.milestones = milestones;
    _savePlayField(playId, { metadata: meta });
}

function _removeMilestone(playId, idx) {
    var play = _playsData.find(function(p) { return p.id === playId; });
    if (!play) return;
    var meta = Object.assign({}, play.metadata || {});
    var milestones = (meta.milestones || []).slice();
    milestones.splice(idx, 1);
    meta.milestones = milestones;
    _savePlayField(playId, { metadata: meta });
}

// ── Direction / Condition Mismatch Warning ──

// Rule types that carry a direction param (bullish/bearish) that can disagree
// with Play.direction (long/short). Extend this list when new directional rules
// are added (e.g. bb_squeeze_breakout).
var _DIRECTIONAL_RULE_TYPES = [
    'engulfing_detected',
    'bb_fade_engulfing',
    'bb_breakout_retest_engulfing',
    'bb_squeeze_breakout'
];

// Returns false if the user cancelled (mismatched + declined). Returns true otherwise.
// Callers must gate the save on this return value.
function _warnDirectionMismatch(playDirection, conditions) {
    var directional = playDirection === 'long' || playDirection === 'short';
    if (!directional) return true; // neutral/conditional — no warning needed

    var expectedRuleDir = playDirection === 'long' ? 'bullish' : 'bearish';
    var rules = (conditions && conditions.rules) || [];
    var mismatched = rules.filter(function(r) {
        return _DIRECTIONAL_RULE_TYPES.indexOf(r.type) !== -1
            && r.params
            && r.params.direction
            && r.params.direction !== expectedRuleDir;
    });

    if (!mismatched.length) return true; // all good

    var ruleList = mismatched.map(function(r) {
        return r.type + ' (direction: ' + r.params.direction + ')';
    }).join('\n  • ');

    return confirm(
        'Heads up: this Play is marked “' + playDirection + '” but the following rules ' +
        'fire on the opposite direction:\n\n' +
        '  • ' + ruleList + '\n\n' +
        'The Play will still work — Play direction is a label, not a trigger. ' +
        'But the directions disagree, which can look confusing in reports. Save anyway?'
    );
}

// ── Helpers ──

function _getCSRF() {
    return (typeof window !== 'undefined' && window.getCSRFToken) ? window.getCSRFToken() : '';
}


function _timeAgo(iso) {
    if (!iso) return '';
    var ms = Date.now() - new Date(iso).getTime();
    var mins = Math.floor(ms / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + 'h ago';
    var days = Math.floor(hours / 24);
    if (days < 30) return days + 'd ago';
    return Math.floor(days / 30) + 'mo ago';
}

// Expose the tier gate for tests. Guarded exactly like
// chart-live-tick.js so the browser bundle is unaffected.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { _isPlaysLocked: _isPlaysLocked };
}
