/**
 * Contextual Nudges — Deferred onboarding prompts.
 * Non-blocking slide-in notifications triggered by user actions.
 * Replaces old onboarding steps 3-5 with progressive profiling.
 */

(function() {
    'use strict';

    var _nudgeActive = null;  // currently visible nudge ID
    var _nudgeDismissed = {};  // local cache from preferences

    // ── Init ──
    function initNudges() {
        // Only for users who completed v2 onboarding
        if (!window._onboardingCompleted || window._onboardingVersion < 2) return;

        // Widget nudge: fires on the 3rd+ visit, once, independent of
        // profile completeness (it's a feature-discovery nudge, not profiling)
        _tryShowWidgetNudge();

        // Load dismissed state from server
        fetch('/api/user/me', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                if (!data) return;
                // Check completeness — if profile is 100%, no nudges needed
                if (data.profile_completeness >= 100) return;
                _startNudgeListeners();
            })
            .catch(function() { _startNudgeListeners(); });
    }

    function _startNudgeListeners() {
        // Load dismissed state from localStorage for instant checks
        try {
            _nudgeDismissed = JSON.parse(localStorage.getItem('_nudgeDismissed') || '{}');
        } catch(e) { _nudgeDismissed = {}; }

        // Listen for view mode changes (dispatched from split-layout.js)
        document.addEventListener('carrac:viewmode', function(e) {
            var mode = e.detail && e.detail.mode;
            if (mode === 'report') _tryShowAnalystNudge();
            if (mode === 'journal') _tryShowPsychologyNudge('journal');
        });

        // AI panel nudge (floating panel)
        var _fabBtn = document.getElementById('ai-fab');
        if (_fabBtn) _fabBtn.addEventListener('click', function() {
            setTimeout(function() { _tryShowProfileNudge('ai_panel'); }, 500);
        });

        // Listen for scenario creation
        document.addEventListener('carrac:scenario-created', function() {
            _tryShowPsychologyNudge('scenario');
        });
    }

    // ── Nudge triggers ──

    function _tryShowProfileNudge(trigger) {
        if (_nudgeActive) return;
        if (_nudgeDismissed.profile) return;
        // Check if trading profile already has core fields
        fetch('/api/ai/trading-profile', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : {}; })
            .then(function(profile) {
                if (profile.styles && profile.styles.length > 0) return;  // already filled
                _showNudge('profile', trigger);
            })
            // SILENCE-OK: this check re-runs from several independent trigger
            // points (dashboard load, chart open, scenario save) — a miss here
            // just means the onboarding nudge doesn't show this trigger; the
            // next trigger point retries it. Not a write, nothing is lost.
            .catch(function() {});
    }

    function _tryShowAnalystNudge() {
        if (_nudgeActive) return;
        if (_nudgeDismissed.analysts) return;
        fetch('/api/user/analyst-sources', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : {}; })
            .then(function(data) {
                var handles = data.x_handles || [];
                var channels = data.yt_channels || {};
                // Check if user has custom sources (not defaults)
                if (handles.length > 0 && !_isDefaultHandles(handles)) return;
                if (Object.keys(channels).length > 0) return;
                _showNudge('analysts', 'morning_report');
            })
            // SILENCE-OK: same as the profile-nudge check above — re-triggered
            // from multiple entry points, a miss just skips this occurrence.
            .catch(function() {});
    }

    function _isDefaultHandles(handles) {
        // If the handles match defaults, user hasn't customized
        var defaults = ['CryptoCapo_', 'AltcoinSherpa', 'DaanCrypto', 'ColdBloodShill', 'Pentosh1'];
        if (handles.length !== defaults.length) return false;
        return handles.every(function(h) { return defaults.indexOf(h) !== -1; });
    }

    function _tryShowPsychologyNudge(trigger) {
        if (_nudgeActive) return;
        if (_nudgeDismissed.psychology) return;
        fetch('/api/ai/trading-profile', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : {}; })
            .then(function(profile) {
                if (profile.emotional_patterns && profile.emotional_patterns.length > 0) return;
                _showNudge('psychology', trigger);
            })
            // SILENCE-OK: same as the profile-nudge check above — re-triggered
            // from multiple entry points, a miss just skips this occurrence.
            .catch(function() {});
    }

    // ── Show nudge ──

    var NUDGE_CONFIG = {
        profile: {
            title: 'Personalize your AI',
            desc: 'Tell the AI about your trading style so it can tailor analysis, morning reports, and play feedback to how you trade.',
            cta: 'Set up now',
            icon: '&#9881;'
        },
        analysts: {
            title: 'Add your analysts',
            desc: 'Include your favorite X/Twitter analysts and YouTube channels in your personalized morning report.',
            cta: 'Add sources',
            icon: '&#128240;'
        },
        psychology: {
            title: 'Know your patterns',
            desc: 'Select the emotional patterns you recognize in your trading. The AI will flag when a setup might trigger one.',
            cta: 'Set up now',
            icon: '&#129504;'
        },
        widget: {
            title: 'Your watchlist on your home screen',
            desc: 'Carrac has free home-screen widgets for iOS and Android — live prices, positions, sentiment and alerts without opening the app.',
            cta: 'Set up a widget',
            icon: '&#128241;'
        }
    };

    function _tryShowWidgetNudge() {
        if (_nudgeDismissed.widget) return;
        try {
            var d = JSON.parse(localStorage.getItem('_nudgeDismissed') || '{}');
            if (d.widget) return;
            var visits = (parseInt(localStorage.getItem('_carracVisits') || '0', 10) || 0) + 1;
            localStorage.setItem('_carracVisits', String(visits));
            if (visits < 3) return;
        } catch(e) { return; }
        setTimeout(function() {
            if (!_nudgeActive) _showNudge('widget', 'visit_count');
        }, 20000);
    }

    function _showNudge(id, trigger) {
        if (_nudgeActive) return;
        _nudgeActive = id;

        var config = NUDGE_CONFIG[id];
        if (!config) return;

        _onbTrack('nudge_shown', { nudge_id: id, trigger: trigger });

        var nudge = document.createElement('div');
        nudge.id = 'ctx-nudge';
        nudge.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:99998;max-width:380px;width:calc(100% - 40px);' +
            'background:#161b22;border:1px solid #30363d;border-left:3px solid #58a6ff;border-radius:8px;padding:16px 18px;' +
            'box-shadow:0 8px 24px rgba(0,0,0,0.4);transform:translateY(120%);transition:transform 0.3s ease-out;';

        nudge.innerHTML =
            '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">' +
                '<div style="font-size:14px;font-weight:600;color:#e6edf3;">' + config.icon + ' ' + config.title + '</div>' +
                '<button id="ctx-nudge-close" style="background:none;border:none;color:#484f58;cursor:pointer;font-size:16px;padding:0 0 0 8px;line-height:1;">&times;</button>' +
            '</div>' +
            '<div style="font-size:13px;color:#8b949e;margin-bottom:12px;line-height:1.4;">' + config.desc + '</div>' +
            '<div style="display:flex;justify-content:space-between;align-items:center;">' +
                '<button id="ctx-nudge-dismiss" style="background:none;border:none;color:#484f58;cursor:pointer;font-size:12px;">Not now</button>' +
                '<button id="ctx-nudge-action" style="background:#238636;color:#fff;border:none;border-radius:5px;padding:6px 16px;font-size:13px;font-weight:600;cursor:pointer;">' + config.cta + '</button>' +
            '</div>';

        document.body.appendChild(nudge);

        // Animate in
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                nudge.style.transform = 'translateY(0)';
            });
        });

        // Close / dismiss
        document.getElementById('ctx-nudge-close').addEventListener('click', function() {
            _dismissNudge(id, trigger);
        });
        document.getElementById('ctx-nudge-dismiss').addEventListener('click', function() {
            _dismissNudge(id, trigger);
        });

        // CTA action
        document.getElementById('ctx-nudge-action').addEventListener('click', function() {
            _removeNudgeEl();
            if (id === 'profile') _showProfileForm();
            else if (id === 'analysts') _showAnalystForm();
            else if (id === 'psychology') _showPsychologyForm();
            else if (id === 'widget') { _nudgeDismissed.widget = true; try { localStorage.setItem('_nudgeDismissed', JSON.stringify(_nudgeDismissed)); } catch(e) {} window.location.href = '/settings#widget'; }
        });
    }

    function _dismissNudge(id, trigger) {
        _onbTrack('nudge_dismissed', { nudge_id: id, trigger: trigger });
        _nudgeDismissed[id] = true;
        try { localStorage.setItem('_nudgeDismissed', JSON.stringify(_nudgeDismissed)); } catch(e) {}

        // Persist to server
        var key = 'nudge_' + id + '_dismissed';
        var payload = {};
        payload[key] = true;
        var csrf = typeof getCSRFToken === 'function' ? getCSRFToken() : '';
        // SILENCE-OK: localStorage.setItem('_nudgeDismissed', ...) above already
        // ran and is authoritative for this device — the nudge stays dismissed
        // locally even if this server mirror fails. Only cross-device dismissal
        // sync is lost, not the dismissal itself.
        fetch('/api/user/me', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            body: JSON.stringify(payload)
        }).catch(function() {});

        _removeNudgeEl();

        // Show "access from Settings" toast
        _showSettingsToast();
    }

    function _removeNudgeEl() {
        var el = document.getElementById('ctx-nudge');
        if (el) {
            el.style.transform = 'translateY(120%)';
            setTimeout(function() { el.remove(); }, 300);
        }
        _nudgeActive = null;
    }

    function _showSettingsToast() {
        if (document.getElementById('settings-toast')) return;
        var toast = document.createElement('div');
        toast.id = 'settings-toast';
        toast.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:99997;background:#21262d;border:1px solid #30363d;' +
            'border-radius:6px;padding:10px 16px;color:#8b949e;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,0.3);' +
            'transform:translateY(120%);transition:transform 0.3s ease-out;';
        toast.textContent = 'You can always do this from Settings > Trading Profile';
        document.body.appendChild(toast);
        requestAnimationFrame(function() {
            requestAnimationFrame(function() { toast.style.transform = 'translateY(0)'; });
        });
        setTimeout(function() {
            toast.style.transform = 'translateY(120%)';
            setTimeout(function() { toast.remove(); }, 300);
        }, 4000);
    }

    // ── Profile form (slide-out panel) ──

    function _showProfileForm() {
        _onbTrack('nudge_completed', { nudge_id: 'profile', action: 'opened_form' });
        var overlay = document.createElement('div');
        overlay.id = 'ctx-profile-overlay';
        overlay.style.cssText = 'position:fixed;top:0;right:0;z-index:99999;width:420px;max-width:100%;height:100%;' +
            'background:#161b22;border-left:1px solid #30363d;box-shadow:-4px 0 20px rgba(0,0,0,0.4);' +
            'overflow-y:auto;padding:24px;transform:translateX(100%);transition:transform 0.3s ease-out;';

        var html = '';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">';
        html += '<h3 style="margin:0;font-size:18px;color:#e6edf3;">Your Trading Style</h3>';
        html += '<button id="ctx-profile-close" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:20px;">&times;</button>';
        html += '</div>';
        html += '<p style="color:#8b949e;font-size:13px;margin:0 0 20px 0;">This helps the AI personalize analysis. You can change this in Settings anytime.</p>';

        // Core fields (always shown)
        html += '<div style="margin-bottom:16px;">';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:6px;">Trading Style</div>';
        html += '<div id="ctx-styles" style="display:flex;flex-wrap:wrap;gap:4px;">';
        html += onbChip('hodl', 'HODLer') + onbChip('swing', 'Swing') + onbChip('day', 'Day') + onbChip('scalp', 'Scalp');
        html += '</div></div>';

        html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;">';
        html += '<div>';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:4px;">Risk Tolerance</div>';
        html += '<select id="ctx-risk" style="width:100%;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-family:inherit;font-size:12px;">';
        html += '<option value="">-</option><option value="conservative">Conservative</option><option value="moderate">Moderate</option><option value="aggressive">Aggressive</option>';
        html += '</select></div>';
        html += '<div>';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:4px;">Holding Period</div>';
        html += '<select id="ctx-holding" style="width:100%;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-family:inherit;font-size:12px;">';
        html += '<option value="">-</option><option value="minutes">Minutes</option><option value="hours">Hours</option>';
        html += '<option value="days_to_weeks">Days to weeks</option><option value="weeks_to_months">Weeks to months</option><option value="months_plus">Months+</option>';
        html += '</select></div></div>';

        html += '<button id="ctx-profile-save-core" style="background:#238636;color:#fff;border:none;border-radius:5px;padding:8px 20px;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:20px;">Save</button>';

        // Expandable "Tell me more"
        html += '<div id="ctx-profile-expand" style="border-top:1px solid #21262d;padding-top:16px;">';
        html += '<button id="ctx-expand-toggle" style="background:none;border:none;color:#58a6ff;cursor:pointer;font-size:13px;padding:0;margin-bottom:12px;">&#9662; Tell me more (optional)</button>';
        html += '<div id="ctx-expand-body" style="display:none;">';

        // Entry conditions
        html += '<div style="margin-bottom:14px;">';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:4px;">Entry conditions</div>';
        html += '<div id="ctx-entry" style="display:flex;flex-wrap:wrap;gap:4px;">';
        html += onbChip('htf_trend_aligned', 'HTF trend aligned');
        html += onbChip('key_level_test', 'Key level test');
        html += onbChip('indicator_confluence', 'Indicator confluence');
        html += onbChip('volume_confirmation', 'Volume confirmation');
        html += onbChip('pullback_to_ma', 'Pullback to MA');
        html += onbChip('breakout_retest', 'Breakout retest');
        html += onbChip('divergence', 'Divergence');
        html += onbChip('funding_rate_extreme', 'Funding rate extreme');
        html += onbChip('pattern_completion', 'Pattern completion');
        html += '</div></div>';

        // Exit strategy
        html += '<div style="margin-bottom:14px;">';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:4px;">Exit strategy</div>';
        html += '<div id="ctx-exit" style="display:flex;flex-wrap:wrap;gap:4px;">';
        html += onbChip('fixed_target', 'Fixed target');
        html += onbChip('trailing_stop', 'Trailing stop');
        html += onbChip('scale_out', 'Scale out');
        html += onbChip('invalidation', 'Thesis invalidation');
        html += onbChip('indicator_signal', 'Indicator signal');
        html += onbChip('technical_level_target', 'Technical level');
        html += '</div></div>';

        // Notes
        html += '<div style="margin-bottom:14px;">';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:4px;">Your edge / rules</div>';
        html += '<textarea id="ctx-notes" maxlength="1200" rows="3" style="width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:8px;color:#d1d4dc;font-family:inherit;font-size:12px;resize:vertical;" placeholder="e.g. I fade funding rate extremes, only enter on HTF confluence zones..."></textarea>';
        html += '</div>';

        html += '<button id="ctx-profile-save-extra" style="background:#238636;color:#fff;border:none;border-radius:5px;padding:8px 20px;font-size:13px;font-weight:600;cursor:pointer;">Save details</button>';
        html += '</div></div>';

        overlay.innerHTML = html;
        document.body.appendChild(overlay);

        // Animate in
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                overlay.style.transform = 'translateX(0)';
            });
        });

        // Setup chip toggles
        setupChipToggles(document.getElementById('ctx-styles'));
        setupChipToggles(document.getElementById('ctx-entry'));
        setupChipToggles(document.getElementById('ctx-exit'));

        // a11y: focus trap + Escape to close
        if (window.a11y) a11y.trapFocus(overlay);
        overlay.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                if (window.a11y) a11y.releaseFocus(overlay);
                overlay.style.transform = 'translateX(100%)';
                setTimeout(function() { overlay.remove(); }, 300);
            }
        });

        // Close
        document.getElementById('ctx-profile-close').addEventListener('click', function() {
            if (window.a11y) a11y.releaseFocus(overlay);
            overlay.style.transform = 'translateX(100%)';
            setTimeout(function() { overlay.remove(); }, 300);
        });

        // Expand toggle
        document.getElementById('ctx-expand-toggle').addEventListener('click', function() {
            var body = document.getElementById('ctx-expand-body');
            var visible = body.style.display !== 'none';
            body.style.display = visible ? 'none' : 'block';
            this.innerHTML = (visible ? '&#9662;' : '&#9652;') + ' Tell me more (optional)';
        });

        // Save core
        document.getElementById('ctx-profile-save-core').addEventListener('click', function() {
            _saveProfileSection('core', this);
        });

        // Save extra
        document.getElementById('ctx-profile-save-extra').addEventListener('click', function() {
            _saveProfileSection('extra', this);
        });
    }

    function _saveProfileSection(section, btn) {
        var profile = {};

        if (section === 'core' || section === 'extra') {
            // Always gather core fields
            var styles = [];
            document.querySelectorAll('#ctx-styles input:checked').forEach(function(cb) { styles.push(cb.value); });
            profile.styles = styles;
            profile.risk = document.getElementById('ctx-risk').value;
            profile.holding_period = document.getElementById('ctx-holding').value;
        }

        if (section === 'extra') {
            var entry = [];
            document.querySelectorAll('#ctx-entry input:checked').forEach(function(cb) { entry.push(cb.value); });
            profile.entry_conditions = entry;
            var exit = [];
            document.querySelectorAll('#ctx-exit input:checked').forEach(function(cb) { exit.push(cb.value); });
            profile.exit_strategy = exit;
            profile.notes = (document.getElementById('ctx-notes').value || '').trim();
        }

        var csrf = typeof getCSRFToken === 'function' ? getCSRFToken() : '';
        var origText = btn.textContent;
        btn.textContent = 'Saving...';
        btn.disabled = true;

        fetch('/api/ai/trading-profile', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            body: JSON.stringify(profile)
        })
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            btn.textContent = 'Saved!';
            btn.style.background = '#1a7f37';
            _onbTrack('trading_profile_section_saved', { section: section, fields_count: Object.keys(profile).length });
            setTimeout(function() {
                btn.textContent = origText;
                btn.style.background = '#238636';
                btn.disabled = false;
            }, 2000);
        })
        .catch(function() {
            btn.textContent = 'Error - try again';
            btn.style.background = '#da3633';
            btn.disabled = false;
            setTimeout(function() { btn.textContent = origText; btn.style.background = '#238636'; }, 2000);
        });
    }

    // ── Analyst sources form ──

    function _showAnalystForm() {
        _onbTrack('nudge_completed', { nudge_id: 'analysts', action: 'opened_form' });
        // Redirect to settings page with hash
        window.location.href = '/settings#analyst-sources';
    }

    // ── Psychology form ──

    function _showPsychologyForm() {
        _onbTrack('nudge_completed', { nudge_id: 'psychology', action: 'opened_form' });

        var overlay = document.createElement('div');
        overlay.id = 'ctx-psych-overlay';
        overlay.style.cssText = 'position:fixed;top:0;right:0;z-index:99999;width:420px;max-width:100%;height:100%;' +
            'background:#161b22;border-left:1px solid #30363d;box-shadow:-4px 0 20px rgba(0,0,0,0.4);' +
            'overflow-y:auto;padding:24px;transform:translateX(100%);transition:transform 0.3s ease-out;';

        var html = '';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">';
        html += '<h3 style="margin:0;font-size:18px;color:#e6edf3;">Know Thyself</h3>';
        html += '<button id="ctx-psych-close" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:20px;">&times;</button>';
        html += '</div>';
        html += '<p style="color:#8b949e;font-size:13px;margin:0 0 20px 0;">Most traders experience some of these. Selecting helps the AI give better feedback on your plays and trades.</p>';

        // Emotional patterns
        html += '<div style="margin-bottom:16px;">';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:6px;">Emotional Patterns</div>';
        html += '<div id="ctx-emotional" style="display:flex;flex-wrap:wrap;gap:5px;">';
        html += onbChip('hesitation_on_setups', 'Hesitating on good setups');
        html += onbChip('fomo_early_entry', 'Entering too early (FOMO)');
        html += onbChip('revenge_trading', 'Revenge trading after losses');
        html += onbChip('overconfidence_after_wins', 'Overconfidence after wins');
        html += onbChip('overtrading_frustration', 'Overtrading from boredom');
        html += onbChip('holding_losers', 'Holding losers too long');
        html += onbChip('watching_winners_reverse', 'Watching winners reverse');
        html += onbChip('moving_stops', 'Moving stops emotionally');
        html += '</div></div>';

        // Pre-trading routine
        html += '<div style="margin-bottom:20px;">';
        html += '<div style="font-size:11px;color:#484f58;text-transform:uppercase;margin-bottom:4px;">Pre-Trading Routine</div>';
        html += '<select id="ctx-routine" style="width:100%;max-width:300px;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-family:inherit;font-size:12px;">';
        html += '<option value="">-</option>';
        html += '<option value="very_structured">Very structured (daily plan, levels pre-marked)</option>';
        html += '<option value="somewhat_structured">Somewhat structured (key levels, flexible execution)</option>';
        html += '<option value="minimal">Minimal (scan when markets open)</option>';
        html += '<option value="no_routine">No consistent routine</option>';
        html += '</select></div>';

        html += '<button id="ctx-psych-save" style="background:#238636;color:#fff;border:none;border-radius:5px;padding:8px 20px;font-size:13px;font-weight:600;cursor:pointer;">Save</button>';

        overlay.innerHTML = html;
        document.body.appendChild(overlay);

        requestAnimationFrame(function() {
            requestAnimationFrame(function() { overlay.style.transform = 'translateX(0)'; });
        });

        setupChipToggles(document.getElementById('ctx-emotional'));

        // a11y: focus trap + Escape to close
        if (window.a11y) a11y.trapFocus(overlay);
        overlay.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                if (window.a11y) a11y.releaseFocus(overlay);
                overlay.style.transform = 'translateX(100%)';
                setTimeout(function() { overlay.remove(); }, 300);
            }
        });

        document.getElementById('ctx-psych-close').addEventListener('click', function() {
            if (window.a11y) a11y.releaseFocus(overlay);
            overlay.style.transform = 'translateX(100%)';
            setTimeout(function() { overlay.remove(); }, 300);
        });

        document.getElementById('ctx-psych-save').addEventListener('click', function() {
            var emotional = [];
            document.querySelectorAll('#ctx-emotional input:checked').forEach(function(cb) { emotional.push(cb.value); });
            var routine = document.getElementById('ctx-routine').value;

            var profile = { emotional_patterns: emotional };
            if (routine) profile.pre_trading_routine = routine;

            var csrf = typeof getCSRFToken === 'function' ? getCSRFToken() : '';
            var btn = this;
            btn.textContent = 'Saving...';
            btn.disabled = true;

            fetch('/api/ai/trading-profile', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                body: JSON.stringify(profile)
            })
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                btn.textContent = 'Saved!';
                btn.style.background = '#1a7f37';
                _nudgeDismissed.psychology = true;
                try { localStorage.setItem('_nudgeDismissed', JSON.stringify(_nudgeDismissed)); } catch(e) {}
                _onbTrack('trading_profile_section_saved', { section: 'psychology', fields_count: emotional.length + (routine ? 1 : 0) });
                setTimeout(function() {
                    overlay.style.transform = 'translateX(100%)';
                    setTimeout(function() { overlay.remove(); }, 300);
                }, 1500);
            })
            .catch(function() {
                btn.textContent = 'Error - try again';
                btn.style.background = '#da3633';
                btn.disabled = false;
                setTimeout(function() { btn.textContent = 'Save'; btn.style.background = '#238636'; }, 2000);
            });
        });
    }

    // ── PostHog helper (reuse from onboarding.js) ──
    function _onbTrack(event, props) {
        if (typeof posthog !== 'undefined' && posthog.capture) posthog.capture(event, props || {});
    }

    // ── Expose init ──
    window.initContextualNudges = initNudges;

    // Auto-init when DOM ready (welcome cards removed — contextual nudges are enough)
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initNudges);
    } else {
        initNudges();
    }
})();
