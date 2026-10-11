/*
 * Helpers shared by every page script: HTML escaping and the floating toast.
 *
 * Classic <script> (no modules) — the names below are globals. Every page that
 * loads a script calling escapeHtml() or showToast() must include this file
 * BEFORE it. Keep this file dependency-free: it is loaded first.
 */

/**
 * Escape a value for safe use as HTML text OR inside a quoted attribute.
 * Escapes & < > " ' ; null/undefined become the empty string and anything
 * else is coerced with String().
 */
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
}

/**
 * Show a short floating message at the bottom centre of the page.
 *   showToast(msg)            default duration (2.2 s)
 *   showToast(msg, 4000)      explicit duration in ms
 *   showToast(msg, 'error')   typed toast ('success' | 'error' | 'info'),
 *                             coloured accent edge, 3 s
 * One element is reused, so a new toast replaces the one on screen.
 */
function showToast(msg, durationOrType) {
    var type = typeof durationOrType === 'string' ? durationOrType : '';
    var duration = (typeof durationOrType === 'number' && durationOrType) || (type ? 3000 : 2200);
    var accents = { success: '#3fb950', error: '#ef5350', info: '#58a6ff' };
    var el = document.getElementById('chart-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'chart-toast';
        el.style.cssText = 'position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:rgba(30,34,45,0.92);color:#d1d4dc;padding:8px 18px;border-radius:6px;font-size:13px;z-index:90000;pointer-events:none;opacity:0;transition:opacity .25s;border-left:3px solid transparent';
        document.body.appendChild(el);
    }
    el.style.borderLeftColor = accents[type] || 'transparent';
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.style.opacity = '0'; }, duration);
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { escapeHtml: escapeHtml, showToast: showToast };
}
