/**
 * CSRF token helper — single source of truth.
 *
 * Loaded early in templates so every later script can rely on window.getCSRFToken().
 * Reads the token from the <meta name="csrf-token"> tag, falling back to the
 * csrf_token cookie for cookie-only contexts.
 */
(function () {
    'use strict';

    function getCSRFToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.getAttribute('content')) {
            return meta.getAttribute('content');
        }
        var cookies = (document.cookie || '').split(';');
        for (var i = 0; i < cookies.length; i++) {
            var cookie = cookies[i].trim();
            if (cookie.indexOf('csrf_token=') === 0) {
                return cookie.substring('csrf_token='.length);
            }
        }
        return '';
    }

    // Expose both names so callers that used the private _getCSRF alias still work.
    window.getCSRFToken = getCSRFToken;
    window._getCSRF = getCSRFToken;
})();
