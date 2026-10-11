/**
 * Carrac — Cookie consent banner.
 *
 * Replaces the auto-dismissing informational bar with a real Accept / Reject
 * gate. Decision is persisted in localStorage as cvx_consent = "accepted" |
 * "rejected". PostHog (and Sentry session replay) only initialise after
 * Accept. EU and unknown-country visitors default to "no analytics" until
 * an explicit Accept; non-EU users still see the banner but PostHog is
 * pre-armed via the geo opt-in path in templates/index.html.
 *
 * Public API:
 *   window.cvxConsent.get()         → "accepted" | "rejected" | null
 *   window.cvxConsent.set(value)    → persist + apply (loads PostHog or opts out)
 *   window.cvxConsent.reopen()      → re-show the banner (used by Privacy page)
 *
 * GDPR Art 7(3): withdrawing consent must be as easy as giving it. The app
 * footer's "Manage cookie preferences" control (#manage-cookie-prefs, see
 * templates/index.html) re-opens this banner via reopen() below — bound with
 * addEventListener, never inline onclick (CSP nonce mode blocks inline JS).
 */
(function() {
    'use strict';
    var KEY = 'cvx_consent';
    var LEGACY_KEY = 'cvx_cookie_notice';

    function getConsent() {
        var v = localStorage.getItem(KEY);
        return (v === 'accepted' || v === 'rejected') ? v : null;
    }

    function applyAccepted() {
        try {
            if (typeof window._cvxInitPostHog === 'function') {
                window._cvxInitPostHog();
            } else if (typeof posthog !== 'undefined' && posthog.opt_in_capturing) {
                posthog.opt_in_capturing();
            }
        } catch (e) {}
    }

    function applyRejected() {
        try {
            if (typeof posthog !== 'undefined' && posthog.opt_out_capturing) {
                posthog.opt_out_capturing();
            }
            // Sentry session replay (if loaded) — stop recording.
            if (typeof Sentry !== 'undefined' && Sentry.getReplay && Sentry.getReplay()) {
                try { Sentry.getReplay().stop(); } catch (e) {}
            }
        } catch (e) {}
    }

    function setConsent(value) {
        localStorage.setItem(KEY, value);
        // Migrate the legacy informational-banner key so old visitors aren't
        // re-prompted on every page load.
        try { localStorage.removeItem(LEGACY_KEY); } catch (e) {}
        if (value === 'accepted') applyAccepted();
        else applyRejected();
    }

    function buildBanner() {
        var banner = document.createElement('div');
        banner.id = 'cvx-consent-banner';
        banner.setAttribute('role', 'dialog');
        banner.setAttribute('aria-label', 'Cookie consent');
        banner.innerHTML =
            '<div class="cvx-consent-inner">' +
              '<div class="cvx-consent-text">' +
                '<strong>We use cookies.</strong> Strictly-necessary cookies keep you signed in. ' +
                'Optional analytics cookies help us improve the product (page views, click events, ' +
                'anonymised session replay with passwords masked). ' +
                '<a href="/privacy" style="color:#58a6ff;">Read the policy</a>.' +
              '</div>' +
              '<div class="cvx-consent-actions">' +
                '<button id="cvx-consent-reject" type="button">Reject optional</button>' +
                '<button id="cvx-consent-accept" type="button">Accept all</button>' +
              '</div>' +
            '</div>';

        var s = banner.style;
        s.position = 'fixed'; s.bottom = '0'; s.left = '0'; s.right = '0';
        s.zIndex = '99999';
        s.background = '#0d1117';
        s.borderTop = '1px solid #30363d';
        s.padding = '14px 18px';
        s.fontFamily = "'JetBrains Mono', monospace";
        s.color = '#c9d1d9';
        s.fontSize = '13px';
        s.lineHeight = '1.5';
        s.boxShadow = '0 -4px 16px rgba(0,0,0,0.6)';

        var styleEl = document.createElement('style');
        styleEl.textContent =
            '#cvx-consent-banner .cvx-consent-inner{display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap;max-width:1100px;margin:0 auto;}' +
            '#cvx-consent-banner .cvx-consent-text{flex:1;min-width:240px;}' +
            '#cvx-consent-banner .cvx-consent-actions{display:flex;gap:10px;flex-shrink:0;}' +
            '#cvx-consent-banner button{padding:8px 16px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;font-family:inherit;font-size:13px;cursor:pointer;}' +
            '#cvx-consent-banner button#cvx-consent-accept{background:#D8B56A;border-color:#D8B56A;color:#1a1407;font-weight:600;}' +
            '#cvx-consent-banner button#cvx-consent-accept:hover{background:#E6CD86;}' +
            '#cvx-consent-banner button#cvx-consent-reject:hover{border-color:#58a6ff;}';
        document.head.appendChild(styleEl);

        document.body.appendChild(banner);

        function close() { banner.remove(); }
        var acceptBtn = banner.querySelector('#cvx-consent-accept');
        var rejectBtn = banner.querySelector('#cvx-consent-reject');
        if (acceptBtn) acceptBtn.addEventListener('click', function() {
            setConsent('accepted'); close();
        });
        if (rejectBtn) rejectBtn.addEventListener('click', function() {
            setConsent('rejected'); close();
        });
    }

    function init() {
        var v = getConsent();
        if (v === 'accepted') { applyAccepted(); return; }
        if (v === 'rejected') { applyRejected(); return; }
        // No decision yet — show the banner.
        if (document.body) buildBanner();
        else document.addEventListener('DOMContentLoaded', buildBanner);
    }

    window.cvxConsent = {
        get: getConsent,
        set: setConsent,
        reopen: function() {
            try { localStorage.removeItem(KEY); } catch (e) {}
            if (!document.body) { document.addEventListener('DOMContentLoaded', buildBanner); return; }
            buildBanner();
        }
    };

    // C3: reset the PostHog distinct_id on logout so the next signed-in
    // session doesn't inherit the previous user's identity. Capture phase +
    // action substring match so this fires regardless of which page's
    // logout form submits (this file loads on every authenticated page that
    // includes _posthog_init.html).
    document.addEventListener('submit', function(e) {
        if (e.target && e.target.action && e.target.action.indexOf('/logout') !== -1) {
            try { if (window.posthog && posthog.reset) posthog.reset(); } catch (err) {}
        }
    }, true);

    // GDPR Art 7(3): bind the footer "Manage cookie preferences" control (if
    // present on this page) to reopen the consent banner. CSP-safe —
    // addEventListener, no inline onclick.
    function bindManagePrefsLink() {
        var btn = document.getElementById('manage-cookie-prefs');
        if (btn) {
            btn.addEventListener('click', function() {
                window.cvxConsent.reopen();
            });
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindManagePrefsLink);
    } else {
        bindManagePrefsLink();
    }

    init();
})();
