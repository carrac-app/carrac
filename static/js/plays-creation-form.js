/* Plays — the 4-step guided creation form.
 *
 * Extracted VERBATIM from plays-panel.js, which is a flat classic script
 * with no IIFE, so these move into global scope as-is. Measured: the
 * region has ZERO top-level executable statements, so every shared name
 * is late-bound at call time and script order does not matter beyond both
 * files being present.
 *
 * The three creation-form state vars moved WITH the section — each had
 * exactly one reference outside the region, its own declaration.
 * _playsCreating deliberately did NOT move: _renderPlaysList reads it, so
 * it stays a genuine shared global in plays-panel.js.
 *
 * Reads from plays-panel.js: _playsCreating, _getCSRF,
 * _warnDirectionMismatch, refreshPlays.
 * Provides: _showCreationForm (the section's ONLY consumer outside it).
 */

var _playsCreateConditions = null;  // conditions for new play being created
var _playsCreateInvalidation = null; // invalidation for new play being created
var _creationStep = 1; // guided form step: 1=Setup 2=Thesis 3=Conditions 4=Review

// ── Creation Form — 4-step guided flow ──
// Steps: 1=Setup (type+approach+asset)  2=Thesis (title+thesis)
//        3=Conditions  4=Review+Submit
// All field IDs that _handleCreateSubmit reads are kept:
//   plays-form-title, plays-form-asset, plays-form-thesis,
//   plays-type-pills, plays-approach-pills, plays-form-submit

function _showCreationForm() {
    _playsCreating = true;
    _creationStep = 1;
    _playsCreateConditions = null;
    _playsCreateInvalidation = null;
    _cfTitle = '';
    _cfAsset = '';
    _cfThesis = '';
    _cfType = 'directional';
    _cfApproach = 'single';
    _renderCreationForm();
}

// Shadow vars: field values preserved across step transitions
var _cfTitle = '';
var _cfAsset = '';
var _cfThesis = '';
var _cfType = 'directional';
var _cfApproach = 'single';

function _snapshotCreationForm() {
    var t = document.getElementById('plays-form-title');
    var a = document.getElementById('plays-form-asset');
    var th = document.getElementById('plays-form-thesis');
    if (t) _cfTitle = t.value;
    if (a) _cfAsset = a.value;
    if (th) _cfThesis = th.value;
    var typeCard = document.querySelector('.pcf-type-card.selected');
    if (typeCard) _cfType = typeCard.getAttribute('data-value');
    var apprBtn = document.querySelector('.pcf-approach-btn.selected');
    if (apprBtn) _cfApproach = apprBtn.getAttribute('data-value');
}

function _renderCreationForm() {
    var el = document.getElementById('plays-content');
    if (!el) return;

    var step = _creationStep;
    var html = '<div class="plays-creation-form pcf-guided">';

    // Stepper header
    var stepDefs = [
        { n: 1, label: 'Setup' },
        { n: 2, label: 'Thesis' },
        { n: 3, label: 'Conditions' },
        { n: 4, label: 'Review' }
    ];
    html += '<div class="pcf-stepper" role="navigation" aria-label="Create play steps">';
    for (var si = 0; si < stepDefs.length; si++) {
        var sd = stepDefs[si];
        var isCurrent = sd.n === step;
        var isDone = sd.n < step;
        var cls = 'pcf-step';
        if (isDone) { cls += ' pcf-step--done'; }
        var aria = isCurrent ? ' aria-current="step"' : '';
        html += '<button class="' + cls + '" type="button" data-goto="' + sd.n + '"' + aria + '>';
        html += '<span class="pcf-step-num">';
        if (isDone) {
            html += '<span class="pcf-step-num-inner" style="display:none">' + sd.n + '</span>';
        } else {
            html += '<span class="pcf-step-num-inner">' + sd.n + '</span>';
        }
        html += '</span>';
        html += '<span class="pcf-step-label">' + sd.label + '</span>';
        html += '</button>';
    }
    html += '</div>';

    // Hidden pill groups: _handleCreateSubmit reads these via _getSelectedPill
    html += '<div style="display:none">';
    html += '<div id="plays-type-pills">';
    var types = ['directional', 'macro', 'event'];
    for (var ti = 0; ti < types.length; ti++) {
        var tv = types[ti];
        html += '<button class="plays-pill' + (tv === _cfType ? ' selected' : '') + '" data-value="' + tv + '">' + tv + '</button>';
    }
    html += '</div>';
    html += '<div id="plays-approach-pills">';
    var approaches = ['single', 'scaled', 'defensive'];
    for (var ai = 0; ai < approaches.length; ai++) {
        var av = approaches[ai];
        html += '<button class="plays-pill' + (av === _cfApproach ? ' selected' : '') + '" data-value="' + av + '">' + av + '</button>';
    }
    html += '</div>';
    html += '</div>';

    // Step body
    html += '<div class="pcf-body">';
    if (step === 1) {
        html += _renderStep1();
    } else if (step === 2) {
        html += _renderStep2();
    } else if (step === 3) {
        html += _renderStep3();
    } else {
        html += _renderStep4();
    }
    html += '</div>';

    // Nav row
    html += '<div class="pcf-nav">';
    if (step > 1) {
        html += '<button class="pcf-nav-back" type="button" id="pcf-back">← Back</button>';
    }
    if (step < 4) {
        var nextLabel = step === 3 ? 'Review →' : 'Next →';
        var nextDisabled = step === 1 ? ' disabled' : '';
        html += '<button class="pcf-nav-next" type="button" id="pcf-next"' + nextDisabled + '>' + nextLabel + '</button>';
    } else {
        html += '<button class="plays-form-submit" id="plays-form-submit" type="button">Create Play</button>';
    }
    html += '<button class="pcf-nav-cancel" type="button" id="plays-form-cancel">Cancel</button>';
    html += '</div>';

    html += '</div>';

    el.innerHTML = html;

    // Restore values into visible inputs
    _restoreCreationFields();

    // Stepper: click on a past step to go back
    el.querySelectorAll('.pcf-step').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var goto = parseInt(btn.getAttribute('data-goto'), 10);
            if (goto < step) {
                _snapshotCreationForm();
                _creationStep = goto;
                _renderCreationForm();
            }
        });
    });

    var backBtn = document.getElementById('pcf-back');
    if (backBtn) {
        backBtn.addEventListener('click', function() {
            _snapshotCreationForm();
            _creationStep--;
            _renderCreationForm();
        });
    }

    var nextBtn = document.getElementById('pcf-next');
    if (nextBtn) {
        nextBtn.addEventListener('click', function() {
            _snapshotCreationForm();
            _creationStep++;
            _renderCreationForm();
        });
    }

    var cancelBtn = document.getElementById('plays-form-cancel');
    if (cancelBtn) { cancelBtn.addEventListener('click', _cancelCreation); }

    var submitBtn = document.getElementById('plays-form-submit');
    if (submitBtn) { submitBtn.addEventListener('click', _handleCreateSubmit); }

    if (step === 1) { _bindStep1(); }
    if (step === 2) { _bindStep2(); }
    if (step === 3) { _bindStep3(); }

    _syncHiddenPills();
}

function _cfStep1Valid() {
    return !!(_cfType && _cfAsset && _cfAsset.trim().length > 0);
}

function _cfStep2Valid() {
    return !!(_cfTitle && _cfTitle.trim().length > 0);
}

function _syncHiddenPills() {
    var tg = document.getElementById('plays-type-pills');
    if (tg) {
        tg.querySelectorAll('.plays-pill').forEach(function(p) {
            if (p.getAttribute('data-value') === _cfType) {
                p.classList.add('selected');
            } else {
                p.classList.remove('selected');
            }
        });
    }
    var ag = document.getElementById('plays-approach-pills');
    if (ag) {
        ag.querySelectorAll('.plays-pill').forEach(function(p) {
            if (p.getAttribute('data-value') === _cfApproach) {
                p.classList.add('selected');
            } else {
                p.classList.remove('selected');
            }
        });
    }
}

function _restoreCreationFields() {
    var t = document.getElementById('plays-form-title');
    if (t && _cfTitle) { t.value = _cfTitle; }

    var a = document.getElementById('plays-form-asset');
    if (a) {
        if (_cfAsset) {
            a.value = _cfAsset;
        } else if (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
            a.value = klineChartSymbol;
            _cfAsset = klineChartSymbol;
        }
    }

    var th = document.getElementById('plays-form-thesis');
    if (th && _cfThesis) { th.value = _cfThesis; }
}

// Step 1: type cards + asset + approach
function _renderStep1() {
    var html = '';
    html += '<p class="pcf-step-heading">What kind of play is this?</p>';
    html += '<p class="pcf-step-sub">Choose a type that matches how you are tracking the thesis.</p>';

    var typeDefs = [
        { v: 'directional', title: 'Directional', desc: 'Price moves your way — entry, target, invalidation.' },
        { v: 'macro', title: 'Macro', desc: 'A regime call tracked over weeks via milestone conditions.' },
        { v: 'event', title: 'Event', desc: 'Anchored to a dated catalyst — CPI, unlock, halving.' }
    ];

    html += '<div class="pcf-type-cards" role="radiogroup" aria-label="Play type">';
    for (var i = 0; i < typeDefs.length; i++) {
        var td = typeDefs[i];
        var isSel = td.v === _cfType;
        html += '<button class="pcf-type-card' + (isSel ? ' selected' : '') + '" type="button" data-value="' + td.v + '" role="radio" aria-checked="' + (isSel ? 'true' : 'false') + '">';
        html += '<span class="pcf-type-card-radio"></span>';
        html += '<span class="pcf-type-card-content">';
        html += '<span class="pcf-type-card-title">' + td.title + '</span>';
        html += '<span class="pcf-type-card-desc">' + td.desc + '</span>';
        html += '</span>';
        html += '</button>';
    }
    html += '</div>';

    html += '<div class="pcf-section-divider"></div>';

    html += '<div class="pcf-field">';
    html += '<label class="pcf-field-label" for="plays-form-asset">Asset</label>';
    html += '<div class="plays-asset-search-wrap">';
    html += '<input class="plays-form-input pcf-title-input" id="plays-form-asset" placeholder="Search assets..." maxlength="20" style="text-transform:uppercase" autocomplete="off">';
    html += '<div class="plays-asset-dropdown" id="plays-asset-dropdown" style="display:none"></div>';
    html += '</div></div>';

    html += '<div class="pcf-section-divider"></div>';

    html += '<div class="pcf-field">';
    html += '<label class="pcf-field-label">Approach</label>';
    var approachDefs = [
        { v: 'single', label: 'Single Entry' },
        { v: 'scaled', label: 'Scaled / DCA' },
        { v: 'defensive', label: 'Risk Defensive' }
    ];
    html += '<div class="pcf-approach-seg" role="group" aria-label="Approach">';
    for (var j = 0; j < approachDefs.length; j++) {
        var ad = approachDefs[j];
        html += '<button class="pcf-approach-btn' + (ad.v === _cfApproach ? ' selected' : '') + '" type="button" data-value="' + ad.v + '">' + ad.label + '</button>';
    }
    html += '</div>';
    html += '</div>';

    return html;
}

// Step 2: title + thesis
function _renderStep2() {
    var html = '';
    html += '<p class="pcf-step-heading">Give it a title and thesis.</p>';
    html += '<p class="pcf-step-sub">A clear title helps you recognise it at a glance. The thesis is the “why”.</p>';

    html += '<div class="pcf-field">';
    html += '<label class="pcf-field-label" for="plays-form-title">Title *</label>';
    html += '<input class="plays-form-input pcf-title-input" id="plays-form-title" placeholder="e.g. DXY breakdown → BTC rally" maxlength="200">';
    html += '</div>';

    html += '<div class="pcf-field">';
    html += '<label class="pcf-field-label" for="plays-form-thesis">Thesis <span class="plays-info-icon" data-info="thesis">ⓘ</span></label>';
    html += '<div class="plays-info-box" id="plays-info-thesis" style="display:none">' +
        '<strong>What makes a good thesis?</strong><br>' +
        'A thesis explains <em>why</em> you believe something will happen, not just <em>what</em>. ' +
        'The more specific, the better your conditions and the more useful the AI morning report becomes.<br><br>' +
        '<strong>Macro play —</strong> “DXY topping at 107 resistance with dovish Fed pivot approaching → liquidity rotation into risk assets, BTC breaks range”<br>' +
        '<strong>Mean-reversion —</strong> “ETH underperforming BTC for 6 months, ETH/BTC ratio at historical support. Catalysts: Pectra upgrade + ETF inflows picking up”<br>' +
        '<strong>Risk-off —</strong> “Tariff escalation + yield curve re-inverting = risk-off. SPX fails at ATH, crypto follows with a lag”' +
        '</div>';
    html += '<textarea class="plays-form-input pcf-thesis-input" id="plays-form-thesis" rows="4" placeholder="I think… because…"></textarea>';
    html += '</div>';

    return html;
}

// Step 3: conditions + invalidation builders (existing UI reused)
function _renderStep3() {
    var html = '';
    html += '<p class="pcf-step-heading">Conditions &amp; invalidation</p>';
    html += '<p class="pcf-step-sub">Optional but powerful — live-monitored against market data every 2 minutes.</p>';

    html += '<div class="plays-form-section">';
    html += '<label class="plays-form-label">Conditions <span class="plays-info-icon" data-info="conditions">ⓘ</span></label>';
    html += '<div class="plays-info-box" id="plays-info-conditions" style="display:none">' +
        '<strong>Live monitoring.</strong> Conditions are checked every 2 minutes against live market data. ' +
        'You can also hit <em>Check Conditions</em> anytime for instant validation with per-condition evidence.<br><br>' +
        '<strong>Morning AI integration.</strong> Your morning report sees every condition by name — ' +
        'it will tell you which conditions changed overnight, what\'s newly met, and what to watch for today.<br><br>' +
        '<strong>Simple setup:</strong> BTC long breakout<br>' +
        '• Price above $72,000 • Daily RSI above 55 • Scanner verdict Long >= GOOD<br><br>' +
        '<strong>Complex macro:</strong> DXY breakdown → crypto rally<br>' +
        '• Cross-asset: DXY below 100.5 • EMA Ribbon bullish on Weekly • Consensio >= 6<br>' +
        'Logic: Custom — (c1 AND c2) OR (c3 AND c4)' +
        '</div>';
    var condCount = _playsCreateConditions ? (_playsCreateConditions.rules || []).length : 0;
    var condLogic = _playsCreateConditions ? (_playsCreateConditions.logic || 'all') : 'all';
    var condSumText = condCount ? condCount + ' condition' + (condCount > 1 ? 's' : '') + ' (' + condLogic.toUpperCase() + ')' : 'No conditions set';
    var condBtnText = condCount ? '✎ Edit Conditions' : '+ Add Conditions';
    html += '<div id="plays-form-cond-summary" class="plays-form-cond-summary">' + escapeHtml(condSumText) + '</div>';
    html += '<button type="button" class="play-cond-btn" id="plays-form-add-cond">' + condBtnText + '</button>';
    html += '</div>';

    html += '<div class="plays-form-section">';
    html += '<label class="plays-form-label">Invalidation <span class="plays-info-icon" data-info="invalidation">ⓘ</span></label>';
    html += '<div class="plays-info-box" id="plays-info-invalidation" style="display:none">' +
        '<strong>The kill switch.</strong> If this condition is met, the play auto-invalidates and you get a notification. ' +
        'The morning report will flag it: <em>“Your BTC long was invalidated overnight — price broke below $62k support.”</em><br><br>' +
        '<strong>Examples:</strong><br>' +
        '• Long BTC play → invalidation: “Price below $62,000” (your SL level)<br>' +
        '• Macro play → “DXY above 108” (thesis broken)<br>' +
        '• Event play → “Time deadline: June 1” with auto-skip<br><br>' +
        'Without invalidation, plays stay open until you manually close them.' +
        '</div>';
    var invSumText = _playsCreateInvalidation
        ? (typeof _condSummary === 'function' ? _condSummary(_playsCreateInvalidation) : _playsCreateInvalidation.type)
        : 'No invalidation set';
    var invBtnText = _playsCreateInvalidation ? '✎ Edit Invalidation' : '+ Add Invalidation';
    html += '<div id="plays-form-inv-summary" class="plays-form-cond-summary">' + escapeHtml(invSumText) + '</div>';
    html += '<button type="button" class="play-inv-btn" id="plays-form-add-inv">' + invBtnText + '</button>';
    html += '</div>';

    return html;
}

// Step 4: live preview card + submit
function _renderStep4() {
    var html = '';
    html += '<p class="pcf-step-heading">Review &amp; create</p>';
    html += '<p class="pcf-step-sub">Looks good? Create it now, or go back to adjust.</p>';

    html += '<div class="pcf-review-card">';

    html += '<div class="pcf-review-row">';
    html += '<span class="pcf-review-title">' + (_cfTitle ? escapeHtml(_cfTitle) : '<em style="color:var(--fg4)">Untitled</em>') + '</span>';
    html += '<span class="pcf-review-badge">' + escapeHtml(_cfType || 'directional') + '</span>';
    if (_cfApproach && _cfApproach !== 'single') {
        html += '<span class="pcf-review-badge">' + escapeHtml(_cfApproach) + '</span>';
    }
    html += '</div>';

    if (_cfAsset) {
        html += '<div class="pcf-review-meta">';
        html += '<span class="pcf-review-badge">' + escapeHtml(_cfAsset) + '</span>';
        html += '</div>';
    }

    if (_cfThesis) {
        html += '<div class="pcf-review-thesis">' + escapeHtml(_cfThesis) + '</div>';
    }

    var condCount2 = _playsCreateConditions ? (_playsCreateConditions.rules || []).length : 0;
    var hasInv = !!_playsCreateInvalidation;
    html += '<div class="pcf-review-conditions">';
    html += '<span class="pcf-review-cond-item"><span class="pcf-review-cond-dot' + (condCount2 ? ' has' : '') + '"></span>';
    html += (condCount2 ? condCount2 + ' condition' + (condCount2 > 1 ? 's' : '') : 'No conditions') + '</span>';
    html += '<span class="pcf-review-cond-item"><span class="pcf-review-cond-dot' + (hasInv ? ' has' : '') + '"></span>';
    html += (hasInv ? 'Invalidation set' : 'No invalidation') + '</span>';
    html += '</div>';

    html += '</div>';

    return html;
}

// Step binding functions
function _bindStep1() {
    var el = document.getElementById('plays-content');
    if (!el) { return; }

    el.querySelectorAll('.pcf-type-card').forEach(function(card) {
        card.addEventListener('click', function() {
            el.querySelectorAll('.pcf-type-card').forEach(function(c) {
                c.classList.remove('selected');
                c.setAttribute('aria-checked', 'false');
            });
            card.classList.add('selected');
            card.setAttribute('aria-checked', 'true');
            _cfType = card.getAttribute('data-value');
            _syncHiddenPills();
            _updateStep1NextBtn();
        });
    });

    el.querySelectorAll('.pcf-approach-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            el.querySelectorAll('.pcf-approach-btn').forEach(function(b) { b.classList.remove('selected'); });
            btn.classList.add('selected');
            _cfApproach = btn.getAttribute('data-value');
            _syncHiddenPills();
        });
    });

    var assetInput = document.getElementById('plays-form-asset');
    var assetDD = document.getElementById('plays-asset-dropdown');
    if (assetInput && assetDD) {
        assetInput.addEventListener('input', function() {
            _cfAsset = assetInput.value;
            _updateStep1NextBtn();
            var q = assetInput.value.trim().toUpperCase();
            if (!q || typeof allAssets === 'undefined') { assetDD.style.display = 'none'; return; }
            var symbols = Object.keys(allAssets);
            var matches = symbols.filter(function(s) {
                var name = ((allAssets[s] || {}).name || '').toUpperCase();
                return s.indexOf(q) >= 0 || name.indexOf(q) >= 0;
            }).sort(function(a, b) {
                var aStart = a.indexOf(q) === 0 ? 0 : 1;
                var bStart = b.indexOf(q) === 0 ? 0 : 1;
                return aStart - bStart || a.localeCompare(b);
            }).slice(0, 8);
            if (!matches.length) { assetDD.style.display = 'none'; return; }
            assetDD.innerHTML = matches.map(function(s) {
                var name = (allAssets[s] || {}).name || '';
                return '<div class="plays-asset-option" data-symbol="' + s + '"><strong>' + s + '</strong>' +
                    (name ? ' <span style="color:#8b949e;font-size:0.6rem">' + escapeHtml(name) + '</span>' : '') + '</div>';
            }).join('');
            assetDD.style.display = 'block';
            assetDD.querySelectorAll('.plays-asset-option').forEach(function(opt) {
                opt.addEventListener('mousedown', function(e) {
                    e.preventDefault();
                    assetInput.value = opt.getAttribute('data-symbol');
                    _cfAsset = assetInput.value;
                    assetDD.style.display = 'none';
                    _updateStep1NextBtn();
                });
            });
        });
        assetInput.addEventListener('blur', function() {
            _cfAsset = assetInput.value;
            setTimeout(function() { assetDD.style.display = 'none'; }, 150);
        });
    }

    _updateStep1NextBtn();
    _bindInfoIcons();
}

function _updateStep1NextBtn() {
    var nextBtn = document.getElementById('pcf-next');
    if (nextBtn) { nextBtn.disabled = !_cfStep1Valid(); }
}

function _bindStep2() {
    var titleInput = document.getElementById('plays-form-title');
    if (titleInput) {
        var nextBtn = document.getElementById('pcf-next');
        if (nextBtn) { nextBtn.disabled = !_cfStep2Valid(); }
        titleInput.addEventListener('input', function() {
            _cfTitle = titleInput.value;
            var nb = document.getElementById('pcf-next');
            if (nb) { nb.disabled = !_cfStep2Valid(); }
        });
    }
    setTimeout(function() {
        var inp = document.getElementById('plays-form-title');
        if (inp) { inp.focus(); }
    }, 60);
    _bindInfoIcons();
}

function _bindStep3() {
    var condBtn = document.getElementById('plays-form-add-cond');
    if (condBtn) {
        condBtn.addEventListener('click', function() {
            openConditionModal('play', null, _playsCreateConditions || { rules: [], logic: 'all' }, function(conditions) {
                _playsCreateConditions = conditions;
                var summary = document.getElementById('plays-form-cond-summary');
                var btn = document.getElementById('plays-form-add-cond');
                var count = (conditions.rules || []).length;
                if (summary) { summary.textContent = count ? count + ' condition' + (count > 1 ? 's' : '') + ' (' + (conditions.logic || 'all').toUpperCase() + ')' : 'No conditions set'; }
                if (btn) { btn.textContent = count ? '✎ Edit Conditions' : '+ Add Conditions'; }
            });
        });
    }

    var invBtn = document.getElementById('plays-form-add-inv');
    if (invBtn) {
        invBtn.addEventListener('click', function() {
            var inv = _playsCreateInvalidation;
            var wrapped = { rules: inv ? [Object.assign({}, inv, { id: inv.id || 'inv1' })] : [], logic: 'all' };
            openConditionModal('play', null, wrapped, function(conditions) {
                _playsCreateInvalidation = (conditions.rules && conditions.rules.length > 0) ? conditions.rules[0] : null;
                var summary = document.getElementById('plays-form-inv-summary');
                var btn = document.getElementById('plays-form-add-inv');
                if (summary) { summary.textContent = _playsCreateInvalidation ? (typeof _condSummary === 'function' ? _condSummary(_playsCreateInvalidation) : _playsCreateInvalidation.type) : 'No invalidation set'; }
                if (btn) { btn.textContent = _playsCreateInvalidation ? '✎ Edit Invalidation' : '+ Add Invalidation'; }
            });
        });
    }

    _bindInfoIcons();
}

function _bindInfoIcons() {
    var el = document.getElementById('plays-content');
    if (!el) { return; }
    el.querySelectorAll('.plays-info-icon').forEach(function(icon) {
        icon.addEventListener('click', function(e) {
            e.stopPropagation();
            var key = icon.getAttribute('data-info');
            var box = document.getElementById('plays-info-' + key);
            if (box) { box.style.display = box.style.display === 'none' ? 'block' : 'none'; }
        });
    });
}

function _getSelectedPill(groupId) {
    var group = document.getElementById(groupId);
    if (!group) return null;
    var sel = group.querySelector('.plays-pill.selected');
    return sel ? sel.getAttribute('data-value') : null;
}

function _cancelCreation() {
    _playsCreating = false;
    refreshPlays();
}

function _handleCreateSubmit() {
    // the guided wizard re-renders ONE step at a time,
    // so the step-2 inputs (plays-form-title/-asset/-thesis) are unmounted by
    // the Review step and getElementById returns null — reading .value would throw.
    // Snapshot whatever is mounted, then
    // read the _cf* state the wizard has been stashing all along.
    _snapshotCreationForm();
    var titleEl = document.getElementById('plays-form-title');
    var title = (_cfTitle || '').trim();
    if (!title) {
        if (titleEl) titleEl.style.borderColor = '#ef5350';
        return;
    }

    var asset = (_cfAsset || '').trim().toUpperCase();
    var thesis = (_cfThesis || '').trim();
    var type = _getSelectedPill('plays-type-pills') || _cfType || 'directional';
    var approach = _getSelectedPill('plays-approach-pills') || _cfApproach || 'single';

    var body = {
        title: title,
        type: type,
        approach: approach,
    };
    if (asset) body.primary_asset = asset;
    if (thesis) body.thesis = thesis;
    if (_playsCreateConditions && _playsCreateConditions.rules && _playsCreateConditions.rules.length) {
        body.conditions = _playsCreateConditions;
    }
    if (_playsCreateInvalidation) {
        body.invalidation = _playsCreateInvalidation;
    }

    // Soft direction-mismatch warning (client-side only — server always allows it)
    var _createPlayDir = body.direction || 'long';
    if (!_warnDirectionMismatch(_createPlayDir, body.conditions || _playsCreateConditions)) return;

    var submitBtn = document.getElementById('plays-form-submit');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating...';

    fetch('/api/plays', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': _getCSRF()
        },
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json().then(function(d) { return { status: r.status, data: d }; }); })
    .then(function(res) {
        if (res.status === 201) {
            _playsCreating = false;
            if (typeof showToast === 'function') showToast('Play created: ' + title);
            refreshPlays();
        } else if (res.status === 403 && res.data.limit_type === 'play') {
            // Shared upgrade modal (falls back to toast on old clients).
            submitBtn.disabled = false;
            submitBtn.textContent = 'Create Play';
            if (typeof handleLimitHitResponse === 'function' && handleLimitHitResponse(res.data, 'plays_limit')) {
                // modal shown
            } else if (typeof showToast === 'function') {
                showToast('Play limit reached (' + res.data.current + '/' + res.data.max + ')');
            }
        } else {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Create Play';
            if (typeof showToast === 'function') showToast(res.data.error || 'Failed to create play');
        }
    })
    .catch(function() {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Create Play';
        if (typeof showToast === 'function') showToast('Network error');
    });
}
