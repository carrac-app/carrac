/* Toolbar tooltips (fast, with kbd shortcut chips). Self-registering document listeners; couples to
   the rest of the app only through DOM selectors (.adv-chart-toolbar,
   #drawing-ctx-bar, #fav-rail, .tool-menu-item) and #cx-tooltip in CSS. */
// ============= Toolbar tooltips (fast, with kbd shortcut chips) =============
// Replaces the slow native title tooltip on toolbar controls: ~300ms delay,
// "Name (Alt+W)" titles render the shortcut as a kbd chip. Scoped to the chart
// toolbar + the floating context bar; titles migrate to data-tip on first hover.
var _cxTipEl = null;
var _cxTipTimer = null;
var _cxTipFor = null;

function _cxTipHide() {
    if (_cxTipTimer) { clearTimeout(_cxTipTimer); _cxTipTimer = null; }
    if (_cxTipEl) _cxTipEl.style.display = 'none';
    _cxTipFor = null;
}

function _cxTipShow(el) {
    var tip = el.getAttribute('data-tip');
    if (!tip) return;
    if (!_cxTipEl) {
        _cxTipEl = document.createElement('div');
        _cxTipEl.id = 'cx-tooltip';
        document.body.appendChild(_cxTipEl);
    }
    var m = tip.match(/^(.*?)\s*\(([^()]+)\)\s*$/);
    if (m && !/^(Alt|Ctrl|Cmd|Shift|Del|Esc|hold Shift|[⌘⌥⇧])/.test(m[2])) m = null;
    if (m) {
        _cxTipEl.innerHTML = '';
        _cxTipEl.appendChild(document.createTextNode(m[1]));
        var kbd = document.createElement('kbd');
        kbd.textContent = m[2];
        _cxTipEl.appendChild(kbd);
    } else {
        _cxTipEl.textContent = tip;
    }
    _cxTipEl.style.display = 'block';
    var r = el.getBoundingClientRect();
    var tr = _cxTipEl.getBoundingClientRect();
    var left = Math.max(6, Math.min(window.innerWidth - tr.width - 6, r.left + r.width / 2 - tr.width / 2));
    var top = r.bottom + 6;
    if (top + tr.height > window.innerHeight - 6) top = r.top - tr.height - 6;
    _cxTipEl.style.left = Math.round(left) + 'px';
    _cxTipEl.style.top = Math.round(top) + 'px';
}

document.addEventListener('mouseover', function(e) {
    if (!e.target || !e.target.closest) return;
    var el = e.target.closest('.adv-chart-toolbar button, #drawing-ctx-bar [title], #drawing-ctx-bar [data-tip], #fav-rail button, #fav-rail-grip');
    if (!el || el.closest('.tool-menu-item')) { return; }
    if (el === _cxTipFor) return;
    _cxTipHide();
    // Migrate native title → data-tip so the slow browser tooltip never shows
    if (el.hasAttribute('title')) {
        el.setAttribute('data-tip', el.getAttribute('title'));
        el.removeAttribute('title');
    }
    if (!el.getAttribute('data-tip')) return;
    _cxTipFor = el;
    _cxTipTimer = setTimeout(function() { _cxTipShow(el); }, 300);
});
document.addEventListener('mouseout', function(e) {
    if (_cxTipFor && e.target && e.target.closest && e.target.closest('.adv-chart-toolbar button, #drawing-ctx-bar [data-tip], #fav-rail button, #fav-rail-grip') === _cxTipFor) {
        _cxTipHide();
    }
});
document.addEventListener('mousedown', _cxTipHide, true);
