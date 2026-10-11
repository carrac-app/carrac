/* tick-color.js — TradingView-style tick-direction + partial-digit price coloring.
 * Exposes: window.CarracTick.paint(el, newFormattedPrice)
 * ES5-style; terser-safe; no console.log. */
(function(win) {
    'use strict';

    /* ─── constants ─────────────────────────────────────────────── */
    var FLASH_MS  = 400;
    var DATA_PREV = 'data-prev';

    /* ─── helpers ────────────────────────────────────────────────── */
    /* Strip all non-numeric characters and parse to float. */
    function _parseNum(str) {
        if (!str || str === '--' || str === '—' || str === '—') return NaN;
        var cleaned = str.replace(/[^0-9.\-]/g, '');
        return parseFloat(cleaned);
    }

    /* True if the string looks like a placeholder/em-dash. */
    function _isPlaceholder(str) {
        if (!str) return true;
        var t = str.trim();
        return t === '--' || t === '—' || t === '–' || t === '' || t === '-';
    }

    /* Index of first character that differs between two strings. */
    function _firstDiff(a, b) {
        var len = Math.min(a.length, b.length);
        for (var i = 0; i < len; i++) {
            if (a[i] !== b[i]) return i;
        }
        return len; /* one is a prefix of the other */
    }

    /* Flash the element briefly with tk-flash, then remove it. */
    function _flash(el) {
        el.classList.remove('tk-flash');
        /* Re-add on the next frame so the CSS animation restarts WITHOUT a
           forced synchronous reflow — the old `void el.offsetWidth` here ran
           on every WS tick (~100×/s aggregate) and flushed layout for the
           whole document each time, the main post-Scanner/Radar jank source. */
        if (el._tkFlashRaf) cancelAnimationFrame(el._tkFlashRaf);
        el._tkFlashRaf = requestAnimationFrame(function() {
            el._tkFlashRaf = null;
            el.classList.add('tk-flash');
        });
        if (el._tkFlashTimer) clearTimeout(el._tkFlashTimer);
        el._tkFlashTimer = setTimeout(function() { el.classList.remove('tk-flash'); }, FLASH_MS + 50);
    }

    /* ─── paint ──────────────────────────────────────────────────── */
    /**
     * paint(el, newStr)
     *   el      — the DOM element whose text content to update
     *   newStr  — the already-formatted price string (e.g. "$67,438.55")
     *
     * Behaviour:
     *   • First paint (no data-prev)   → plain text, neutral, store data-prev
     *   • Same value                   → no-op (no repaint, no flash)
     *   • Placeholder / NaN            → plain text, clear data-prev
     *   • Real tick                    → prefix neutral + suffix colored, flash, store data-prev
     */
    function paint(el, newStr) {
        if (!el) return;
        var prev = el.getAttribute(DATA_PREV);

        /* ── placeholder → render plain and reset ── */
        if (_isPlaceholder(newStr)) {
            el.textContent = newStr;
            el.removeAttribute(DATA_PREV);
            return;
        }

        /* ── no change → skip entirely ── */
        if (prev === newStr) return;

        /* ── first paint (no previous value stored) ── */
        if (prev === null) {
            el.textContent = newStr;
            el.setAttribute(DATA_PREV, newStr);
            return;
        }

        /* ── determine direction ── */
        var prevNum = _parseNum(prev);
        var newNum  = _parseNum(newStr);
        var dirUp   = null; /* null = indeterminate */

        if (!isNaN(prevNum) && !isNaN(newNum)) {
            if (newNum > prevNum) dirUp = true;
            else if (newNum < prevNum) dirUp = false;
            /* equal numerically → keep dirUp null → render plain, still store */
        }

        /* ── build partial-digit colored HTML ── */
        if (dirUp !== null) {
            var splitIdx = _firstDiff(prev, newStr);
            var prefix   = newStr.slice(0, splitIdx);
            var suffix   = newStr.slice(splitIdx);
            var cls      = dirUp ? 'tk-up' : 'tk-down';

            /* Always render both spans so the CSS targets are stable */
            el.innerHTML = (prefix ? '<span class="tk-prefix">' + escapeHtml(prefix) + '</span>' : '') +
                           '<span class="tk-delta ' + cls + '">' + escapeHtml(suffix) + '</span>';
        } else {
            /* Numerically identical but string changed (rare: rounding artefact) */
            el.textContent = newStr;
        }

        /* ── bookkeeping ── */
        el.setAttribute(DATA_PREV, newStr);
        _flash(el);
    }

    /* ─── export ─────────────────────────────────────────────────── */
    win.CarracTick = { paint: paint };

}(window));
