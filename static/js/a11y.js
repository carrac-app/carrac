/**
 * Accessibility utilities for Carrac.
 * Focus trap, focus restore, aria-live announcements.
 */

(function() {
    'use strict';

    // ── Focus Trap ──────────────────────────────────────────────
    var _trapStack = []; // stack of { el, previousFocus, restoreHidden }

    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function trapFocus(el, triggerEl) {
        if (!el) return;
        var prev = triggerEl || document.activeElement;

        // aria-hidden on background content
        var siblings = [];
        var node = el;
        while (node && node !== document.body) node = node.parentElement;
        // Hide siblings of the modal's top-level parent from screen readers
        Array.prototype.forEach.call(document.body.children, function(child) {
            if (child === el || child.contains(el) || child === el.parentElement) return;
            if (child.getAttribute('aria-hidden') === 'true') return;
            child.setAttribute('aria-hidden', 'true');
            siblings.push(child);
        });

        _trapStack.push({ el: el, previousFocus: prev, restoreHidden: siblings });

        // Focus first focusable element
        var first = el.querySelector(FOCUSABLE);
        if (first) setTimeout(function() { first.focus(); }, 50);

        // Tab trap handler
        el._a11yTrapHandler = function(e) {
            if (e.key !== 'Tab') return;
            var focusable = Array.prototype.filter.call(
                el.querySelectorAll(FOCUSABLE),
                function(f) { return f.offsetParent !== null; }
            );
            if (focusable.length === 0) { e.preventDefault(); return; }
            var firstF = focusable[0];
            var lastF = focusable[focusable.length - 1];
            if (e.shiftKey) {
                if (document.activeElement === firstF) {
                    e.preventDefault();
                    lastF.focus();
                }
            } else {
                if (document.activeElement === lastF) {
                    e.preventDefault();
                    firstF.focus();
                }
            }
        };
        el.addEventListener('keydown', el._a11yTrapHandler);
    }

    function releaseFocus(el) {
        if (!el) return;
        // Remove trap handler
        if (el._a11yTrapHandler) {
            el.removeEventListener('keydown', el._a11yTrapHandler);
            delete el._a11yTrapHandler;
        }
        // Find matching stack entry
        var idx = -1;
        for (var i = _trapStack.length - 1; i >= 0; i--) {
            if (_trapStack[i].el === el) { idx = i; break; }
        }
        if (idx === -1) return;
        var entry = _trapStack.splice(idx, 1)[0];

        // Restore aria-hidden
        entry.restoreHidden.forEach(function(child) {
            child.removeAttribute('aria-hidden');
        });

        // Restore focus to trigger element
        if (entry.previousFocus && entry.previousFocus.focus) {
            try { entry.previousFocus.focus(); } catch(e) {}
        }
    }

    // ── Aria-live Announcements ─────────────────────────────────
    var _liveRegion = null;

    function _ensureLiveRegion() {
        if (_liveRegion) return _liveRegion;
        _liveRegion = document.createElement('div');
        _liveRegion.id = 'a11y-live-region';
        _liveRegion.setAttribute('role', 'status');
        _liveRegion.setAttribute('aria-live', 'polite');
        _liveRegion.setAttribute('aria-atomic', 'true');
        _liveRegion.className = 'sr-only';
        document.body.appendChild(_liveRegion);
        return _liveRegion;
    }

    function announce(message, priority) {
        var region = _ensureLiveRegion();
        region.setAttribute('aria-live', priority === 'assertive' ? 'assertive' : 'polite');
        // Clear then set to force re-announce
        region.textContent = '';
        setTimeout(function() {
            if (region.isConnected) region.textContent = message;
        }, 100);
    }

    // ── Tab Strip Keyboard Nav ──────────────────────────────────
    function initTabStripKeyboard(container, options) {
        if (!container) return;
        var opts = options || {};
        var selector = opts.tabSelector || '[role="tab"]';
        var vertical = opts.vertical || false;

        container.addEventListener('keydown', function(e) {
            var tabs = Array.prototype.filter.call(
                container.querySelectorAll(selector),
                function(t) { return t.offsetParent !== null; }
            );
            var idx = tabs.indexOf(document.activeElement);
            if (idx === -1) return;

            var nextKey = vertical ? 'ArrowDown' : 'ArrowRight';
            var prevKey = vertical ? 'ArrowUp' : 'ArrowLeft';

            if (e.key === nextKey) {
                e.preventDefault();
                tabs[(idx + 1) % tabs.length].focus();
            } else if (e.key === prevKey) {
                e.preventDefault();
                tabs[(idx - 1 + tabs.length) % tabs.length].focus();
            } else if (e.key === 'Home') {
                e.preventDefault();
                tabs[0].focus();
            } else if (e.key === 'End') {
                e.preventDefault();
                tabs[tabs.length - 1].focus();
            }
        });
    }

    // ── Make onclick divs keyboard-accessible ───────────────────
    function makeClickable(el) {
        if (!el) return;
        if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
        if (!el.getAttribute('role')) el.setAttribute('role', 'button');
        el.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                el.click();
            }
        });
    }

    // ── Global Escape → close topmost modal ──────────
    // Modals register themselves by calling a11y.trapFocus; on Escape,
    // we invoke the close callback for the topmost active modal.
    var _escapeCallbacks = []; // stack of close-callback functions

    function registerModalEscape(closeFn) {
        _escapeCallbacks.push(closeFn);
    }
    function unregisterModalEscape(closeFn) {
        var idx = _escapeCallbacks.lastIndexOf(closeFn);
        if (idx !== -1) _escapeCallbacks.splice(idx, 1);
    }

    document.addEventListener('keydown', function(e) {
        if (e.key !== 'Escape') return;
        // Walk from innermost modal outward
        for (var i = _escapeCallbacks.length - 1; i >= 0; i--) {
            var fn = _escapeCallbacks[i];
            if (typeof fn === 'function') {
                try { fn(); } catch (err) {}
                _escapeCallbacks.splice(i, 1);
                e.preventDefault();
                return;
            }
        }
        // Fallback: close any .modal.active element visible on screen
        var activeModal = document.querySelector('.modal-overlay.active, [role="dialog"][aria-modal="true"]');
        if (activeModal) {
            // Try generic close button
            var closeBtn = activeModal.querySelector('.modal-close, [data-action="closeModal"], .close-btn');
            if (closeBtn) { closeBtn.click(); e.preventDefault(); }
        }
    });

    // ── Export ───────────────────────────────────────────────────
    window.a11y = {
        trapFocus: trapFocus,
        releaseFocus: releaseFocus,
        announce: announce,
        initTabStripKeyboard: initTabStripKeyboard,
        makeClickable: makeClickable,
        registerModalEscape: registerModalEscape,
        unregisterModalEscape: unregisterModalEscape
    };
})();
