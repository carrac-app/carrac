/* Floating drawing context bar (desktop). Classic script, global scope —
   reads klineChart, klineSelectedOverlayId (app-core.js), drawing tool tables
   and helpers from draw-tool-palette.js / kline-drawings.js at call time. Registers its own mouseup/keyup/resize
   listeners; creates the #drawing-ctx-bar element other code styles. */
// ============= Floating drawing context bar (desktop) =============
// TV-style context toolbar: appears when a drawing is selected, anchored under
// the chart toolbar. Styles the SELECTED drawing only — the row pickers stay
// the defaults for NEW drawings. Mobile/touch uses the mobile context bar in
// mobile-draw.js instead.
var _dcbEl = null;
var _dcbLastId = null;

function _dcbIsTouch() {
    if (typeof isTouchDevice === 'function' && isTouchDevice()) return true;
    try { return window.matchMedia('(pointer: coarse)').matches; } catch(e) { return false; }
}

function _dcbHide() {
    if (_dcbEl) { _dcbEl.remove(); _dcbEl = null; }
    _dcbLastId = null;
}

function _dcbPosition() {
    if (!_dcbEl) return;
    var anchor = null;
    try { if (klineChart && klineChart.getDom) anchor = klineChart.getDom(); } catch(e) {}
    if (!anchor) anchor = document.getElementById('kline-chart-container');
    if (!anchor) { _dcbHide(); return; }
    var rect = anchor.getBoundingClientRect();
    var content = anchor.closest('.adv-chart-content');
    var toolbar = content ? content.querySelector('.adv-chart-toolbar') : null;
    var top = toolbar ? toolbar.getBoundingClientRect().bottom + 8 : rect.top + 40;
    _dcbEl.style.top = Math.round(top) + 'px';
    _dcbEl.style.left = Math.round(rect.left + rect.width / 2) + 'px';
}

function _dcbRender() {
    var id = (typeof klineSelectedOverlayId !== 'undefined') ? klineSelectedOverlayId : null;
    if (!id || !klineChart || _dcbIsTouch()) { _dcbHide(); return; }
    // kline-drawings.js is lazy-loaded; selection implies it's there, but guard anyway
    if (typeof getKlineDrawingById !== 'function') { _dcbHide(); return; }
    var rec = getOrCreateDrawingRecord(id);
    if (!rec) { _dcbHide(); return; }
    var curColor = rec.color || klineDrawingColor;
    var curWidth = rec.width || 1;
    var locked = !!(rec.extendData && rec.extendData._locked);
    var name = (typeof klineToolNames !== 'undefined' && klineToolNames[rec.name]) ? klineToolNames[rec.name] : '';

    if (!_dcbEl) {
        _dcbEl = document.createElement('div');
        _dcbEl.id = 'drawing-ctx-bar';
        _dcbEl.addEventListener('mousedown', function(e) { e.stopPropagation(); });
        _dcbEl.addEventListener('click', function(e) {
            e.stopPropagation();
            var btn = e.target.closest('[data-dcb]');
            if (!btn) return;
            var act = btn.getAttribute('data-dcb');
            var selId = klineSelectedOverlayId;
            if (!selId) return;
            if (act === 'color') {
                var d = getOrCreateDrawingRecord(selId);
                if (d) { d.color = btn.getAttribute('data-val'); recreateOverlayWithStyles(d); }
            } else if (act === 'width') {
                var d2 = getOrCreateDrawingRecord(selId);
                if (d2) { d2.width = parseInt(btn.getAttribute('data-val'), 10); recreateOverlayWithStyles(d2); }
            } else if (act === 'opacity') {
                if (typeof showOpacityPicker === 'function') {
                    var _obr = btn.getBoundingClientRect();
                    showOpacityPicker(selId, Math.round(_obr.left), Math.round(_obr.bottom + 6));
                }
            } else if (act === 'lock') {
                if (typeof handleContextMenuAction === 'function') handleContextMenuAction('toggle-lock', selId);
            } else if (act === 'delete') {
                if (typeof handleContextMenuAction === 'function') handleContextMenuAction('delete', selId);
            }
            _dcbScheduleSync();
        });
        document.body.appendChild(_dcbEl);
    }

    var html = name ? '<span class="dcb-name">' + name + '</span><span class="dcb-sep"></span>' : '';
    klineDrawingColors.forEach(function(c) {
        html += '<button class="dcb-dot' + (c === curColor ? ' active' : '') + '" data-dcb="color" data-val="' + c + '" style="background:' + c + '" title="Color"></button>';
    });
    html += '<span class="dcb-sep"></span>';
    klineDrawingWidths.forEach(function(w) {
        html += '<button class="dcb-w' + (w === curWidth ? ' active' : '') + '" data-dcb="width" data-val="' + w + '" title="Thickness ' + w + 'px"><span style="height:' + w + 'px"></span></button>';
    });
    html += '<span class="dcb-sep"></span>';
    html += '<button class="dcb-btn" data-dcb="opacity" title="Opacity ([ ])">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2z"/><path d="M12 2v20" stroke-dasharray="2 2"/></svg></button>';
    html += '<button class="dcb-btn' + (locked ? ' active' : '') + '" data-dcb="lock" title="' + (locked ? 'Unlock' : 'Lock') + '">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="' + (locked ? 'M7 11V7a5 5 0 0 1 10 0v4' : 'M7 11V7a5 5 0 0 1 9.9-1') + '"/></svg></button>';
    html += '<button class="dcb-btn dcb-danger" data-dcb="delete" title="Delete (Del)">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>';
    _dcbEl.innerHTML = html;
    _dcbLastId = id;
    _dcbPosition();
}

function _dcbSync() {
    var id = (typeof klineSelectedOverlayId !== 'undefined') ? klineSelectedOverlayId : null;
    if (!id) { _dcbHide(); return; }
    _dcbRender();
}

// Selection only changes through user input — check after mouseup/keyup settle.
// recreateOverlayWithStyles restores selection async (~100ms), so check twice.
function _dcbScheduleSync() {
    setTimeout(_dcbSync, 0);
    setTimeout(_dcbSync, 180);
}
document.addEventListener('mouseup', _dcbScheduleSync);
document.addEventListener('keyup', function(e) {
    if (e.key === 'Escape' || e.key === 'Delete' || e.key === 'Backspace' || e.key === 'c' || e.key === 'C'
        || e.key === '+' || e.key === '=' || e.key === '-') _dcbScheduleSync();
});
window.addEventListener('resize', function() { if (_dcbEl) setTimeout(_dcbPosition, 200); });
