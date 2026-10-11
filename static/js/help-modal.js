/* Help & Premium Modals
 * Deps: a11y (global), getCSRFToken() (csrf-utils.js) */

function openHelpModal() {
    var modal = document.getElementById('help-modal');
    if (!modal) return;
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    // Initialize collapsible sections on first open
    setTimeout(initHelpSections, 50);
    if (window.a11y) {
        a11y.trapFocus(modal.querySelector('[role="dialog"]'));
        a11y.registerModalEscape(closeHelpModal);
    }
}

function closeHelpModal() {
    var modal = document.getElementById('help-modal');
    if (!modal) return;
    if (window.a11y) a11y.releaseFocus(modal.querySelector('[role="dialog"]'));
    modal.classList.remove('active');
    document.body.style.overflow = '';
}

// Premium Modal
var premiumModalLoaded = false;
function openPremiumModal() {
    var modal = document.getElementById('premium-modal');
    if (!modal) return;
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    if (!premiumModalLoaded) {
        premiumModalLoaded = true;
        loadPremiumStatus();
    }
    if (window.a11y) {
        a11y.trapFocus(modal.querySelector('[role="dialog"]'));
        a11y.registerModalEscape(closePremiumModal);
    }
}
function closePremiumModal() {
    var modal = document.getElementById('premium-modal');
    if (!modal) return;
    if (window.a11y) a11y.releaseFocus(modal.querySelector('[role="dialog"]'));
    modal.classList.remove('active');
    document.body.style.overflow = '';
}
function loadPremiumStatus() {
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrf = csrfMeta ? csrfMeta.content : '';
    fetch('/api/billing/status', {headers: {'Content-Type':'application/json','X-CSRFToken':csrf}})
    .then(function(r){ return r.json(); })
    .then(function(d){
        var badge = document.getElementById('pm-tier-badge');
        var status = document.getElementById('pm-tier-status');
        var action = document.getElementById('pm-upgrade-section');
        var modalContent = document.querySelector('#premium-modal .premium-modal-content');
        var tier = d.tier || 'free';
        if (badge) badge.textContent = tier;
        if (badge) badge.className = 'tier-badge ' + tier;
        // Scope the static feature comparison table: it's hardcoded "Free vs Premium",
        // which is misleading for Members (already paying) — hide it for them via a class.
        if (modalContent) {
            modalContent.classList.remove('modal-tier-free','modal-tier-member','modal-tier-premium','modal-tier-admin','modal-tier-tester');
            modalContent.classList.add('modal-tier-' + tier);
        }
        if (tier === 'admin') {
            status.innerHTML = 'Admin account — <strong>all features unlocked</strong>';
            action.innerHTML = '';
        } else if (tier === 'tester') {
            status.innerHTML = 'Tester account — <strong>Member features enabled</strong>';
            action.innerHTML = '';
        } else {
            var bal = d.balance_cents ? ' | Credits: $' + (d.balance_cents/100).toFixed(2) : '';
            status.textContent = tier.charAt(0).toUpperCase() + tier.slice(1) + ' plan' + bal;
            action.innerHTML = (tier === 'premium') ? '' : '<div class="upgrade-note">This needs a higher plan. An admin can change plans under Admin → Users.</div>';
        }
    });
}
// ── Upgrade modal for gated features ──
// Accepts either a string reason or an options object:
//   { reason: 'text', source: 'alerts_limit', feature: 'alerts',
//     current: 5, max: 5, tierCaps: {free:5, member:50, premium:200} }
// Fires `upgrade_prompt_viewed` PostHog event with the source tag so we can
// segment which gates fire.
function showUpgradeModal(arg) {
    var opts = (arg && typeof arg === 'object') ? arg : { reason: arg };
    var reason = opts.reason || 'This feature requires a subscription.';
    var existing = document.getElementById('upgrade-gate-modal');
    if (existing) existing.remove();

    var capsHtml = '';
    if (opts.tierCaps) {
        var tc = opts.tierCaps;
        capsHtml = '<div style="color:#c9d1d9;margin:0 0 18px;font-size:13px;line-height:1.5;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px 14px;">' +
            'Your plan: <b>' + (tc.free != null ? tc.free : 0) + '</b>' +
            '&nbsp;·&nbsp;Member: <b>' + (tc.member != null ? tc.member : '\u2014') + '</b>' +
            '&nbsp;·&nbsp;Premium: <b>' + (tc.premium != null ? tc.premium : '\u2014') + '</b>' +
            '</div>';
    } else if (opts.current != null && opts.max != null) {
        capsHtml = '<div style="color:#f85149;margin:0 0 14px;font-size:13px;">Used ' + opts.current + ' of ' + opts.max + '.</div>';
    }

    var overlay = document.createElement('div');
    overlay.id = 'upgrade-gate-modal';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);z-index:99999;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML =
        '<div style="background:#1e222d;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:440px;width:90%;text-align:center;">' +
        '<div style="font-size:24px;margin-bottom:8px;">Higher plan needed</div>' +
        '<div style="color:#8b949e;margin-bottom:20px;font-size:14px;">' + reason + '</div>' +
        capsHtml +
        '<div style="color:#8b949e;font-size:13px;line-height:1.5;">This needs a higher plan. An admin can change plans under Admin → Users.</div>' +
        '<div style="margin-top:16px;"><button style="background:none;border:1px solid #30363d;color:#8b949e;padding:6px 16px;border-radius:6px;cursor:pointer;">' + 'Got it' + '</button></div>' +
        '</div>';
    document.body.appendChild(overlay);

    // upgrade_abandoned — fires when the modal closes without the
    // user clicking one of the subscribe links. The `_cvxUpgradeClicked` flag
    // is flipped by the subscribe links below so we can tell intent apart.
    window._cvxUpgradeClicked = false;
    var upgradeOpts = opts;
    var upgradeStart = Date.now();
    function _trackAbandonOnce() {
        if (overlay._abandonTracked) return;
        overlay._abandonTracked = true;
        if (window._cvxUpgradeClicked) return;
        try {
            if (typeof posthog !== 'undefined' && posthog.capture) {
                posthog.capture('upgrade_abandoned', {
                    source: upgradeOpts.source || 'generic',
                    feature: upgradeOpts.feature || null,
                    viewed_ms: Date.now() - upgradeStart
                });
            }
        } catch (e) {}
    }
    overlay.addEventListener('click', function(e) { if (e.target === overlay) { _trackAbandonOnce(); overlay.remove(); } });
    overlay.querySelectorAll('a').forEach(function(a) {
        a.addEventListener('click', function() { window._cvxUpgradeClicked = true; });
    });
    var cancelBtn = overlay.querySelector('button');
    if (cancelBtn) cancelBtn.addEventListener('click', function(e) { _trackAbandonOnce(); e.currentTarget.closest('#upgrade-gate-modal').remove(); });

    try {
        if (typeof posthog !== 'undefined' && posthog.capture) {
            posthog.capture('upgrade_prompt_viewed', {
                source: opts.source || 'generic',
                feature: opts.feature || null,
                current: opts.current != null ? opts.current : null,
                max: opts.max != null ? opts.max : null
            });
        }
    } catch (e) {}
}

// Shared handler for 403 responses that carry limit-hit metadata
// from the backend. The server returns {limit_type, current, max, error}
// for alerts, plays, scenarios, custom formulas, patterns, scanners. This
// helper turns any of those into a consistent upgrade modal without every
// caller re-implementing the same two-step toast-then-redirect.
// free/member values mirror TIER_LIMITS in carrac/auth/decorators.py
// (max_plays, max_alerts) — keep in sync with any future matrix change.
var LIMIT_TYPE_CAPS = {
    alert:          { free: 5,  member: 100, premium: 200, feature: 'alerts' },
    play:           { free: 3,  member: 999, premium: 20,  feature: 'plays' },
    scenario:       { free: 0,  member: 15,  premium: 999, feature: 'scenarios' },
    custom_formula: { free: 0,  member: 999, premium: 999, feature: 'custom formulas' },
    pattern:        { free: 0,  member: 20,  premium: 99,  feature: 'pattern matches' }
};

function handleLimitHitResponse(data, sourceFeature) {
    if (!data || !data.limit_type) return false;
    var caps = LIMIT_TYPE_CAPS[data.limit_type] || {};
    var reason = data.error || ((caps.feature || 'This feature') + ' limit reached. Upgrade to create more.');
    showUpgradeModal({
        reason: reason,
        source: sourceFeature || (data.limit_type + '_limit'),
        feature: data.limit_type,
        current: (typeof data.current === 'number') ? data.current : null,
        max: (typeof data.max === 'number') ? data.max : null,
        tierCaps: { free: caps.free, member: caps.member, premium: caps.premium }
    });
    return true;
}

// Help Modal Collapsible Sections
var helpSectionsInitialized = false;

function initHelpSections() {
    if (helpSectionsInitialized) return;
    var modal = document.querySelector('#help-modal .modal');
    if (!modal) return;

    var h3s = modal.querySelectorAll('h3');
    h3s.forEach(function(h3) {
        if (h3.classList.contains('collapsible')) return;
        if (h3.closest('.help-expand-controls')) return;

        // Collect content until next h3 or end
        var content = [];
        var sibling = h3.nextElementSibling;
        while (sibling && sibling.tagName !== 'H3' && !sibling.classList.contains('help-expand-controls')) {
            content.push(sibling);
            sibling = sibling.nextElementSibling;
        }

        if (content.length === 0) return;

        // Wrap content in collapsible div
        var wrapper = document.createElement('div');
        wrapper.className = 'help-section-content';
        var maxHeight = 0;
        content.forEach(function(el) {
            maxHeight += el.offsetHeight + 20;
            wrapper.appendChild(el);
        });
        wrapper.style.maxHeight = maxHeight + 'px';
        h3.parentNode.insertBefore(wrapper, sibling);

        // Make h3 clickable
        h3.classList.add('collapsible');
        h3.addEventListener('click', function() {
            toggleHelpSection(this);
        });
    });
    helpSectionsInitialized = true;
}

function toggleHelpSection(h3) {
    var content = h3.nextElementSibling;
    if (!content || !content.classList.contains('help-section-content')) return;

    h3.classList.toggle('collapsed');
    content.classList.toggle('collapsed');
}

function expandAllHelpSections() {
    initHelpSections();
    var modal = document.querySelector('#help-modal .modal');
    if (!modal) return;

    modal.querySelectorAll('h3.collapsible').forEach(function(h3) {
        h3.classList.remove('collapsed');
    });
    modal.querySelectorAll('.help-section-content').forEach(function(content) {
        content.classList.remove('collapsed');
    });
}

function collapseAllHelpSections() {
    initHelpSections();
    var modal = document.querySelector('#help-modal .modal');
    if (!modal) return;

    modal.querySelectorAll('h3.collapsible').forEach(function(h3) {
        h3.classList.add('collapsed');
    });
    modal.querySelectorAll('.help-section-content').forEach(function(content) {
        content.classList.add('collapsed');
    });
}
