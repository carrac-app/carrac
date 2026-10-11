/**
 * Satisfaction Survey — Monthly non-intrusive toast.
 * Bottom-right slide-in, 5-point text scale, optional comment.
 * Follows the same pattern as contextual-nudges.js.
 */

(function() {
    'use strict';

    var DELAY_MS = 3 * 60 * 1000;  // 3 minutes after page load
    var LABELS = [
        { value: 5, label: 'Excellent', cls: 'sat-5' },
        { value: 4, label: 'Good',      cls: 'sat-4' },
        { value: 3, label: 'Meh',       cls: 'sat-3' },
        { value: 2, label: 'Not great', cls: 'sat-2' },
        { value: 1, label: 'Terrible',  cls: 'sat-1' }
    ];

    function init() {
        // Only for authenticated users who completed onboarding
        if (!window._onboardingCompleted || window._onboardingVersion < 2) return;

        // Wait before checking eligibility
        setTimeout(function() {
            // Don't stack with contextual nudges
            if (document.getElementById('ctx-nudge')) return;
            _checkEligibility();
        }, DELAY_MS);
    }

    function _checkEligibility() {
        fetch('/api/satisfaction/should-show', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                if (data && data.show) _showSurvey();
            })
            .catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/satisfaction/should-show failed — survey stays hidden this load', String((e && e.message) || e)); });
    }

    function _showSurvey() {
        // Don't show if a nudge appeared in the meantime
        if (document.getElementById('ctx-nudge')) return;

        _track('satisfaction_survey_shown');

        var toast = document.createElement('div');
        toast.id = 'satisfaction-toast';
        toast.className = 'satisfaction-toast';

        var html = '';
        html += '<div class="satisfaction-header">';
        html += '<span class="satisfaction-title">How\'s Carrac working for you?</span>';
        html += '<button class="satisfaction-close" id="sat-close">&times;</button>';
        html += '</div>';
        html += '<div class="satisfaction-subtitle">Quick monthly check-in</div>';
        html += '<div class="satisfaction-options" id="sat-options">';
        for (var i = 0; i < LABELS.length; i++) {
            html += '<button class="satisfaction-option ' + LABELS[i].cls + '" data-rating="' + LABELS[i].value + '">' + LABELS[i].label + '</button>';
        }
        html += '</div>';
        html += '<div class="satisfaction-dismiss">';
        html += '<button id="sat-dismiss" class="satisfaction-dismiss-btn">Not now</button>';
        html += '</div>';

        toast.innerHTML = html;
        toast.setAttribute('role', 'dialog');
        toast.setAttribute('aria-label', 'Satisfaction survey');
        document.body.appendChild(toast);

        // Animate in
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                toast.classList.add('visible');
            });
        });

        // Close / dismiss
        document.getElementById('sat-close').addEventListener('click', _dismiss);
        document.getElementById('sat-dismiss').addEventListener('click', _dismiss);

        // Escape to close
        toast.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') _dismiss();
        });

        // Rating buttons
        var buttons = document.querySelectorAll('#sat-options .satisfaction-option');
        for (var j = 0; j < buttons.length; j++) {
            buttons[j].addEventListener('click', function() {
                var rating = parseInt(this.getAttribute('data-rating'), 10);
                _showCommentStep(rating);
            });
        }
    }

    function _showCommentStep(rating) {
        var toast = document.getElementById('satisfaction-toast');
        if (!toast) return;
        if (toast.querySelector('.satisfaction-comment')) return;

        // Mark the selected rating
        var buttons = toast.querySelectorAll('.satisfaction-option');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            if (parseInt(btn.getAttribute('data-rating'), 10) === rating) {
                btn.classList.add('selected');
            } else {
                btn.classList.add('faded');
            }
            btn.style.pointerEvents = 'none';
        }

        // Hide dismiss row
        var dismissRow = toast.querySelector('.satisfaction-dismiss');
        if (dismissRow) dismissRow.style.display = 'none';

        // Add comment area
        var commentArea = document.createElement('div');
        commentArea.className = 'satisfaction-comment';
        commentArea.innerHTML =
            '<div class="satisfaction-comment-label">Anything specific? <span style="color:#484f58">(optional)</span></div>' +
            '<textarea id="sat-comment" class="satisfaction-textarea" rows="2" maxlength="2000" placeholder="What could be better?"></textarea>' +
            '<button id="sat-submit" class="satisfaction-submit">Submit</button>';
        toast.appendChild(commentArea);

        // Focus textarea
        var ta = document.getElementById('sat-comment');
        if (ta) ta.focus();

        document.getElementById('sat-submit').addEventListener('click', function() {
            var comment = (document.getElementById('sat-comment').value || '').trim();
            _submit(rating, comment);
        });
    }

    function _submit(rating, comment) {
        var csrf = typeof getCSRFToken === 'function' ? getCSRFToken() : '';
        var toast = document.getElementById('satisfaction-toast');
        fetch('/api/satisfaction', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            body: JSON.stringify({ rating: rating, comment: comment || '' })
        }).then(function() {
            if (toast) {
                toast.innerHTML = '<div class="satisfaction-thanks">Thanks for the feedback!</div>';
                setTimeout(function() { _removeToast(); }, 1500);
            }
        }).catch(function() {
            if (toast) {
                toast.innerHTML = '<div class="satisfaction-thanks" style="color:#f85149">Failed to send. Try again later.</div>';
                setTimeout(function() { _removeToast(); }, 3000);
            }
        });
    }

    function _dismiss() {
        var csrf = typeof getCSRFToken === 'function' ? getCSRFToken() : '';
        // carracLog only, no toast: the toast is already being removed regardless
        // of this call's outcome (see .then below), so a "could not save" toast
        // would appear confusingly after the dismissal UI has already gone away.
        // Worst case on failure: the survey may resurface later — low stakes.
        fetch('/api/satisfaction/dismiss', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }
        }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/satisfaction/dismiss failed — survey may resurface later', String((e && e.message) || e)); }).then(function() { _removeToast(); });
    }

    function _removeToast() {
        var el = document.getElementById('satisfaction-toast');
        if (el) {
            el.classList.remove('visible');
            setTimeout(function() { el.remove(); }, 300);
        }
    }

    function _track(event, props) {
        if (typeof posthog !== 'undefined' && posthog.capture) posthog.capture(event, props || {});
    }

    // Auto-init
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
