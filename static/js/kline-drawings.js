/* KLine Drawing Tools */

// ---- Drawing cursor management ----
var _drawingToolActive = false;
// Active one-click position tool's cleanup (Long/Short arm at most ONE at a
// time — a stale armed listener double-places on the next chart click)
var _posActiveDisarm = null;

// ---- Multi-select ----
// Set of overlay IDs currently multi-selected (custom shapes only).
var klineMultiSelectIds = [];

// Visual bookkeeping: { [overlayId]: origLineWidth } — restored on remove
var _msOrigWidth = {};

// Is overlay name eligible for multi-select?
var _MS_ELIGIBLE = ['rect', 'boundedChannel', 'position', 'longPosition', 'shortPosition',
    // native line shapes: selectable via marquee; group move/nudge/delete work
    // through overrideOverlay/delete paths (Ctrl+click toggle stays custom-only)
    'straightLine', 'rayLine', 'segment', 'horizontalStraightLine', 'horizontalRayLine',
    'horizontalSegment', 'verticalStraightLine', 'priceLine', 'priceChannelLine',
    'parallelStraightLine', 'fibonacciLine'];
function _msIsEligible(name) {
    return _MS_ELIGIBLE.indexOf(name) !== -1;
}

function _msHas(id) {
    return klineMultiSelectIds.indexOf(id) !== -1;
}

// Poke a position overlay to repaint (inherits _touchPositionOverlay pattern but can be
// called before that function is defined — forward ref handled by hoisting).
function _msTouchOverlay(overlayId) {
    if (!klineChart) return;
    try {
        var _ovs = klineChart.getOverlayById(overlayId);
        var _ov = Array.isArray(_ovs) ? _ovs[0] : _ovs;
        if (_ov) {
            var _ext = Object.assign({}, _ov.extendData || {});
            _ext._hoverTick = Date.now();
            klineChart.overrideOverlay({ id: overlayId, extendData: _ext });
        }
    } catch (e) {}
}

// Apply multi-select visual (line width +2 override) to a newly-added member
function _msApplyVisual(id) {
    if (!klineChart) return;
    try {
        var _ovs2 = klineChart.getOverlayById(id);
        var _ov2 = Array.isArray(_ovs2) ? _ovs2[0] : _ovs2;
        if (!_ov2) return;
        var _d = getKlineDrawingById(id);
        var _w = _d ? (_d.width || 1) : 1;
        _msOrigWidth[id] = _w;
        klineChart.overrideOverlay({ id: id, styles: { line: { size: _w + 2 } } });
    } catch (e) {}
}

// Restore visual for a member being removed from the set
function _msRestoreVisual(id) {
    if (!klineChart) return;
    try {
        var _origW = _msOrigWidth[id];
        if (_origW != null) {
            klineChart.overrideOverlay({ id: id, styles: { line: { size: _origW } } });
        }
        delete _msOrigWidth[id];
    } catch (e) {}
    // Poke position overlays so painter condition re-evaluates
    _msTouchOverlay(id);
}

// Toggle id into/out of the multi-select set; also updates klineSelectedOverlayId
function _msToggle(id) {
    try {
        var _drawing = getKlineDrawingById(id);
        if (!_drawing || !_msIsEligible(_drawing.name)) return;
        var idx = klineMultiSelectIds.indexOf(id);
        if (idx !== -1) {
            // Remove from set
            klineMultiSelectIds.splice(idx, 1);
            _msRestoreVisual(id);
        } else {
            // Add to set
            klineMultiSelectIds.push(id);
            klineSelectedOverlayId = id;
            _msApplyVisual(id);
        }
    } catch (e) {}
}

// Clear the entire multi-select set, restoring all visuals
function _msClear() {
    try {
        var _ids = klineMultiSelectIds.slice();
        klineMultiSelectIds = [];
        for (var _i = 0; _i < _ids.length; _i++) {
            _msRestoreVisual(_ids[_i]);
        }
    } catch (e) {}
}

// ---- Multi-select position Ctrl+click arm ----
// Armed when Ctrl is held on a position body mousedown; resolved on window mouseup
var _msPosClickArm = null; // { id, downX, downY }

// ---- Group move drag state ----
var _msDrag = null;
// { paneId, startDataIndex, startValue, members: [{ id, origPoints, origRightTs }], moved, downX, downY }

// ---- Ctrl+marquee selection drag state ----
var _marqueeState = null;
// { startX, startY, curX, curY, active } — local coords relative to container

function _handleMarquee(clientX, clientY, container) {
    if (!_marqueeState) return;
    var rect = container.getBoundingClientRect();
    _marqueeState.curX = clientX - rect.left;
    _marqueeState.curY = clientY - rect.top;
    var dx = _marqueeState.curX - _marqueeState.startX;
    var dy = _marqueeState.curY - _marqueeState.startY;
    if (!_marqueeState.active && Math.sqrt(dx * dx + dy * dy) > 3) _marqueeState.active = true;
    if (!_marqueeState.active) return;
    var el = document.getElementById('kline-marquee');
    if (!el) {
        el = document.createElement('div');
        el.id = 'kline-marquee';
        container.appendChild(el);
    }
    el.style.left = Math.min(_marqueeState.startX, _marqueeState.curX) + 'px';
    el.style.top = Math.min(_marqueeState.startY, _marqueeState.curY) + 'px';
    el.style.width = Math.abs(dx) + 'px';
    el.style.height = Math.abs(dy) + 'px';
    // Live preview: light up drawings as the box reaches them
    try {
        var nowIds = _marqueeSelectIds(_marqueeState.startX, _marqueeState.startY, _marqueeState.curX, _marqueeState.curY);
        var prev = _marqueeState.preview || [];
        for (var pi = 0; pi < prev.length; pi++) {
            if (nowIds.indexOf(prev[pi]) === -1 && !_msHas(prev[pi])) _msRestoreVisual(prev[pi]);
        }
        for (var ni = 0; ni < nowIds.length; ni++) {
            if (prev.indexOf(nowIds[ni]) === -1 && !_msHas(nowIds[ni])) _msApplyVisual(nowIds[ni]);
        }
        _marqueeState.preview = nowIds;
    } catch (e) {}
}

// ---- string-identity-cached drawings store ----
// localStorage.getItem('klineDrawings') + JSON.parse is done at ~30 sites in
// this file; the hover hit-test alone (_getRectDrawings/_getPositionDrawings/
// _getChannelCornerHit/_getFrvpEdgeHit) does it 2-4x PER FRAME. getItem is
// cheap; parsing the whole blob every time is not. FIVE files write this key
// (drawing-tools.js, freehand.js, drawing-editing.js, scenarios.js, this file) so a
// classic invalidate-on-write wiring isn't viable — instead this getter
// SELF-VALIDATES: if the raw string is === the last-seen raw string, hand
// back the cached parsed object; otherwise re-parse and re-cache. A write
// from ANY of those five files changes the raw string, so the very next
// getter call here self-heals — no invalidation call needed anywhere.
// READ-ONLY CONTRACT: the returned object (and its nested arrays/objects) is
// SHARED across every caller until the raw string changes — callers must
// NOT mutate it in place. Read-modify-write call sites (saveKlineDrawing,
// getKlineDrawingById, etc.) stay on their own fresh JSON.parse, or pass
// {fresh:true} here, so they get a private copy safe to mutate.
var _drawingsStoreCache = { raw: null, parsed: null };
function _readDrawingsStore(opts) {
    var raw = null;
    try { raw = localStorage.getItem('klineDrawings'); } catch (e) {}
    if (opts && opts.fresh) {
        // Caller intends to mutate the result — hand back a PRIVATE parse,
        // never touching the shared cache.
        try { return JSON.parse(raw || '{}'); } catch (e) { return {}; }
    }
    if (raw === _drawingsStoreCache.raw && _drawingsStoreCache.parsed !== null) {
        return _drawingsStoreCache.parsed;
    }
    var parsed;
    try { parsed = JSON.parse(raw || '{}'); } catch (e) { parsed = {}; }
    _drawingsStoreCache.raw = raw;
    _drawingsStoreCache.parsed = parsed;
    return parsed;
}

// Eligible drawing ids whose live bounding box intersects the rect
function _marqueeSelectIds(x1, y1, x2, y2) {
    var ids = [];
    try {
        var xMin = Math.min(x1, x2), xMax = Math.max(x1, x2);
        var yMin = Math.min(y1, y2), yMax = Math.max(y1, y2);
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        for (var i = 0; i < drawings.length; i++) {
            var d = drawings[i];
            if (!_msIsEligible(d.name)) continue;
            if (d.extendData && d.extendData._hidden) continue;
            var ovA = klineChart.getOverlayById(d.id);
            var ov = Array.isArray(ovA) ? ovA[0] : ovA;
            if (!ov || !ov.points || !ov.points.length) continue;
            var paneId = ov.paneId || 'candle_pane';
            var bxMin = Infinity, bxMax = -Infinity, byMin = Infinity, byMax = -Infinity;
            for (var j = 0; j < ov.points.length; j++) {
                var px = null;
                try { px = klineChart.convertToPixel({ dataIndex: ov.points[j].dataIndex, value: ov.points[j].value }, { paneId: paneId }); } catch(e) {}
                if (!px || px.x == null || px.y == null) continue;
                if (px.x < bxMin) bxMin = px.x;
                if (px.x > bxMax) bxMax = px.x;
                if (px.y < byMin) byMin = px.y;
                if (px.y > byMax) byMax = px.y;
            }
            // Position boxes extend to extendData.rightTs
            if (ov.extendData && typeof ov.extendData.rightTs === 'number') {
                try {
                    var rpx = klineChart.convertToPixel({ timestamp: ov.extendData.rightTs, value: ov.points[0].value }, { paneId: paneId });
                    if (rpx && rpx.x != null && rpx.x > bxMax) bxMax = rpx.x;
                } catch(e) {}
            }
            if (bxMin === Infinity || byMin === Infinity) continue;
            if (bxMax >= xMin && bxMin <= xMax && byMax >= yMin && byMin <= yMax) ids.push(d.id);
        }
    } catch(e) {}
    return ids;
}

function _finalizeMarquee() {
    if (!_marqueeState) return;
    var st = _marqueeState;
    _marqueeState = null;
    try {
        var el = (typeof document !== 'undefined') ? document.getElementById('kline-marquee') : null;
        if (el) el.remove();
    } catch(e) {}
    if (!st.active) return;
    // Drop preview visuals first — _msApplyVisual snapshots the original width,
    // and toggling on top of a boosted preview would snapshot the boost
    var prev = st.preview || [];
    for (var p = 0; p < prev.length; p++) {
        if (!_msHas(prev[p])) _msRestoreVisual(prev[p]);
    }
    var ids = _marqueeSelectIds(st.startX, st.startY, st.curX, st.curY);
    for (var i = 0; i < ids.length; i++) {
        if (!_msHas(ids[i])) _msToggle(ids[i]); // ADD only — never toggle members out
    }
}

function _armMsDrag(localX, localY, container, e) {
    if (!klineChart || klineMultiSelectIds.length < 1) return false;
    try {
        var _paneId = 'candle_pane';
        var _anchor;
        try { _anchor = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: _paneId }); } catch(e2) {}
        if (!_anchor || _anchor.dataIndex == null || _anchor.value == null) return false;

        var _members = [];
        var _ids = klineMultiSelectIds.slice();
        for (var _mi = 0; _mi < _ids.length; _mi++) {
            var _mid = _ids[_mi];
            var _movLocked = false;
            try {
                var _movOvArr = klineChart.getOverlayById(_mid);
                var _movOv = Array.isArray(_movOvArr) ? _movOvArr[0] : _movOvArr;
                if (_movOv && _movOv.lock === true) { _movLocked = true; }
                if (_movLocked) continue; // locked members stay put
                if (!_movOv || !_movOv.points || !_movOv.points.length) return false;
                // Require live dataIndex on all points
                for (var _pi2 = 0; _pi2 < _movOv.points.length; _pi2++) {
                    if (_movOv.points[_pi2].dataIndex == null) return false;
                }
                var _origPts = _movOv.points.map(function(p) { return { dataIndex: p.dataIndex, value: p.value }; });
                var _origRightTs = (_movOv.extendData && typeof _movOv.extendData.rightTs === 'number') ? _movOv.extendData.rightTs : undefined;
                // Resolve the right edge's bar index ONCE here — per-move shifts
                // are then a plain index offset (pixel math mis-shifts at any
                // bar spacing other than the guessed one)
                var _origRightIdx = (_origRightTs !== undefined)
                    ? _findNearestBarIndex(klineChart.getDataList(), _origRightTs)
                    : undefined;
                _members.push({ id: _mid, origPoints: _origPts, origRightTs: _origRightTs, origRightIdx: _origRightIdx });
            } catch(e3) { return false; }
        }
        if (!_members.length) return false;

        e.stopPropagation();
        e.preventDefault();
        _msDrag = {
            paneId: _paneId,
            startDataIndex: _anchor.dataIndex,
            startValue: _anchor.value,
            members: _members,
            moved: false,
            downX: e.clientX,
            downY: e.clientY
        };
        return true;
    } catch(e) { return false; }
}

function _handleMsDrag(clientX, clientY, container) {
    if (!_msDrag || !klineChart) return;
    try {
        var _rect = container.getBoundingClientRect();
        var _lx = clientX - _rect.left;
        var _ly = clientY - _rect.top;
        var _cur;
        try { _cur = klineChart.convertFromPixel({ x: _lx, y: _ly }, { paneId: _msDrag.paneId }); } catch(e) { return; }
        if (!_cur || _cur.dataIndex == null || _cur.value == null) return;
        var _dBars = _cur.dataIndex - _msDrag.startDataIndex;
        var _dVal = _cur.value - _msDrag.startValue;
        var _dataList = klineChart.getDataList();
        if (!_dataList || !_dataList.length) return;

        // 3px screen-distance threshold: skip translation until cursor travels >3px
        // from the mousedown point to avoid marking real clicks as drags.
        var _sdx = clientX - _msDrag.downX;
        var _sdy = clientY - _msDrag.downY;
        var _screenDist = Math.sqrt(_sdx * _sdx + _sdy * _sdy);
        if (_screenDist <= 3) return;

        // Edge check: abort if ANY point of ANY member would leave [0, dataList.length-1]
        var _members = _msDrag.members;
        for (var _ci = 0; _ci < _members.length; _ci++) {
            var _op = _members[_ci].origPoints;
            for (var _cj = 0; _cj < _op.length; _cj++) {
                var _nd = _op[_cj].dataIndex + _dBars;
                if (_nd < 0 || _nd >= _dataList.length) return;
            }
        }

        // Apply translation to every member
        for (var _ai = 0; _ai < _members.length; _ai++) {
            var _m = _members[_ai];
            var _newPts = _m.origPoints.map(function(p) {
                var _ndi = p.dataIndex + _dBars;
                return { dataIndex: _ndi, timestamp: _dataList[_ndi].timestamp, value: p.value + _dVal };
            });
            var _overrideObj = { id: _m.id, points: _newPts };
            // Shift rightTs by the same bar delta (index resolved once at arm time)
            if (_m.origRightIdx !== undefined) {
                try {
                    var _rOvArr = klineChart.getOverlayById(_m.id);
                    var _rOv = Array.isArray(_rOvArr) ? _rOvArr[0] : _rOvArr;
                    var _liveExt = (_rOv && _rOv.extendData) ? Object.assign({}, _rOv.extendData) : {};
                    var _rNewIdx = _m.origRightIdx + _dBars;
                    if (_rNewIdx >= 0 && _rNewIdx < _dataList.length) {
                        _liveExt.rightTs = _dataList[_rNewIdx].timestamp;
                    }
                    _overrideObj.extendData = _liveExt;
                } catch(e) {}
            }
            try { klineChart.overrideOverlay(_overrideObj); } catch(e) {}
        }
        _msDrag.moved = true;
    } catch(e) {}
}

// Helper: find bar index nearest to a timestamp
function _findNearestBarIndex(dataList, ts) {
    if (!dataList || !dataList.length) return 0;
    var _best = 0, _bestDiff = Math.abs(dataList[0].timestamp - ts);
    for (var _ni = 1; _ni < dataList.length; _ni++) {
        var _diff = Math.abs(dataList[_ni].timestamp - ts);
        if (_diff < _bestDiff) { _bestDiff = _diff; _best = _ni; }
    }
    return _best;
}

function _finalizeMsDrag() {
    if (!_msDrag) return;
    var _state = _msDrag;
    _msDrag = null;
    if (!_state.moved) return; // plain click on member — keep set, do nothing
    try {
        var _members = _state.members;
        for (var _fi = 0; _fi < _members.length; _fi++) {
            var _fm = _members[_fi];
            try {
                var _fOvArr = klineChart.getOverlayById(_fm.id);
                var _fOv = Array.isArray(_fOvArr) ? _fOvArr[0] : _fOvArr;
                if (!_fOv) continue;
                var _fDrawing = getKlineDrawingById(_fm.id);
                if (!_fDrawing) continue;
                _fDrawing.points = resolvePointTimestamps(_fOv.points);
                if (_fOv.extendData && typeof _fOv.extendData.rightTs === 'number') {
                    _fDrawing.extendData = Object.assign(_fDrawing.extendData || {}, { rightTs: _fOv.extendData.rightTs });
                }
                saveKlineDrawing(_fDrawing);
            } catch(e) {}
        }
    } catch(e) {}
}

// ---- Magnet snapping ----
// Modes: 'off' | 'weak' | 'strong'. Cycled by toolbar magnet button.
var _magnetMode = 'off';
// Live Ctrl state for the native-tool perform hooks (their params carry no event)
var _magnetCtrlHeld = false;
// Snap gates (CSS px). Weak was 8px — klinecharts' native modeSensitivity
// default, far tighter than this file's own hit-test radii (18-26px) and
// nearly impossible to hit by hand, which read as "magnet does nothing".
var _MAGNET_WEAK_PX = 12;
var _MAGNET_STRONG_MAX_PX = 30;
(function() {
    try {
        if (typeof localStorage !== 'undefined') {
            var _stored = localStorage.getItem('carracMagnetMode');
            if (_stored === 'weak' || _stored === 'strong') _magnetMode = _stored;
        }
    } catch (e) {}
})();

// Magnet for NATIVE KLineChart tools (trendlines, rays, h-lines, fibs…):
// the engine controls their point placement, so we harvest each built-in
// overlay template via getOverlayClass (instances expose all template fields)
// and re-register it with performEventMoveForDrawing/performEventPressedMove
// hooks that snap the dragged point. createOverlay per-instance hooks are NOT
// accepted by the engine (verified: the option is dropped, property stays null).
var _MAGNET_NATIVE_INSTALLED = false;
function _installMagnetNativeHooks() {
    if (_MAGNET_NATIVE_INSTALLED) return;
    if (typeof klinecharts === 'undefined' || typeof klinecharts.getOverlayClass !== 'function') return;
    _MAGNET_NATIVE_INSTALLED = true;
    var _names = ['straightLine', 'rayLine', 'segment', 'horizontalStraightLine',
                  'horizontalRayLine', 'horizontalSegment', 'verticalStraightLine',
                  'priceLine', 'priceChannelLine', 'parallelStraightLine', 'fibonacciLine'];
    var _keys = ['name', 'totalStep', 'lock', 'visible', 'zLevel',
                 'needDefaultPointFigure', 'needDefaultXAxisFigure', 'needDefaultYAxisFigure',
                 'mode', 'modeSensitivity', 'styles',
                 'createPointFigures', 'createXAxisFigures', 'createYAxisFigures'];
    function _magnetPerform(params) {
        try {
            var pp = params && params.performPoint;
            if (!pp || pp.dataIndex == null || pp.value == null) return;
            if (!klineChart) return;
            // The engine invokes these hooks as overlay methods, so `this` is
            // the overlay instance — only candle-pane drawings snap to OHLC.
            // (Also fixes the latent case where a sub-pane drawing on a symbol
            // whose price overlaps indicator values, e.g. OIL ~79 vs RSI 0-100,
            // slipped through the old distance-only guard.)
            var pane = (this && this.paneId) || 'candle_pane';
            if (pane !== 'candle_pane') return;
            var snapped = _magnetSnapValue(pp.dataIndex, pp.value, pane, _magnetCtrlHeld);
            if (snapped !== pp.value) pp.value = snapped;
        } catch (e) {}
    }
    for (var i = 0; i < _names.length; i++) {
        try {
            var C = klinecharts.getOverlayClass(_names[i]);
            if (!C) continue;
            var inst = new C();
            var tpl = {};
            for (var k = 0; k < _keys.length; k++) {
                if (inst[_keys[k]] != null) tpl[_keys[k]] = inst[_keys[k]];
            }
            if (!tpl.name || typeof tpl.createPointFigures !== 'function') continue;
            tpl.performEventMoveForDrawing = _magnetPerform;
            tpl.performEventPressedMove = _magnetPerform;
            klinecharts.registerOverlay(tpl);
        } catch (e) {}
    }
}

// Update the magnet toolbar button visual state.
function _updateMagnetButton() {
    try {
        var btn = document.getElementById('magnet-btn');
        if (!btn) return;
        btn.classList.remove('active', 'magnet-strong');
        var t = 'Magnet: off — click to cycle (weak → strong)';
        if (_magnetMode === 'weak') {
            btn.classList.add('active');
            t = 'Magnet: weak — snaps near OHLC. Click for strong';
        } else if (_magnetMode === 'strong') {
            btn.classList.add('active', 'magnet-strong');
            t = 'Magnet: strong — always snaps to OHLC. Click to turn off';
        }
        btn.title = t;
    } catch (e) {}
}

// Core snap helper. Pure function, exported for tests.
// Returns (possibly snapped) value for the given chart-space value at dataIndex/paneId.
// ctrlHeld inverts the current mode: off+ctrl→strong, weak/strong+ctrl→off.
// Log mode: dataList OHLC and drag values are BOTH chart-space — compare directly.
function _magnetSnapValue(dataIndex, rawValue, paneId, ctrlHeld) {
    // Resolve effective mode
    var mode = _magnetMode;
    if (ctrlHeld) {
        if (mode === 'off') { mode = 'strong'; }
        else { mode = 'off'; }
    }
    if (mode === 'off') return rawValue;

    // Get candle
    var dataList;
    try { dataList = klineChart.getDataList(); } catch (e) { return rawValue; }
    if (!dataList || dataIndex == null || dataIndex < 0 || dataIndex >= dataList.length) return rawValue;
    var candle = dataList[dataIndex];
    if (!candle) return rawValue;

    // Phantom bar guard: future bars are padding, not real data
    if (candle.timestamp > Date.now()) return rawValue;

    // Flat/phantom bar guard (all OHLC equal — padding bar)
    var o = candle.open, h = candle.high, l = candle.low, c = candle.close;
    if (o == null || h == null || l == null || c == null) return rawValue;

    var candidates = [o, h, l, c];

    // Find nearest candidate by absolute value difference
    var best = candidates[0];
    var bestDiff = Math.abs(rawValue - best);
    for (var i = 1; i < candidates.length; i++) {
        var diff = Math.abs(rawValue - candidates[i]);
        if (diff < bestDiff) { bestDiff = diff; best = candidates[i]; }
    }

    // Candle-pane strong mode: unconditional snap to the nearest OHLC of the
    // bar under the cursor, regardless of distance — TradingView parity.
    if (mode === 'strong' && (paneId || 'candle_pane') === 'candle_pane') {
        return best;
    }

    // Pixel distance to the nearest candidate, in the CALLER's pane space —
    // for sub-pane overlays a candle OHLC converts absurdly far away, so this
    // gate doubles as the pane filter for the paths below.
    var pxDist = null;
    try {
        var rawPx = klineChart.convertToPixel({ dataIndex: dataIndex, value: rawValue }, { paneId: paneId || 'candle_pane' });
        var bestPx = klineChart.convertToPixel({ dataIndex: dataIndex, value: best }, { paneId: paneId || 'candle_pane' });
        if (rawPx && bestPx && rawPx.y != null && bestPx.y != null) pxDist = Math.abs(rawPx.y - bestPx.y);
    } catch (e) {}

    if (mode === 'strong') {
        // Strong on a non-candle pane keeps the cap (= the pane filter).
        return (pxDist != null && pxDist <= _MAGNET_STRONG_MAX_PX) ? best : rawValue;
    }

    // weak: only snap within the weak gate
    return (pxDist != null && pxDist <= _MAGNET_WEAK_PX) ? best : rawValue;
}

// ---- Undo/Redo state-snapshot system ----
var _undoStacks = {};        // { symbol: [snapshotJSON, ...] }
var _redoStacks = {};        // { symbol: [snapshotJSON, ...] }
var _undoRedoActive = false; // blocks snapshot-taking during restore
var _undoCoalesceFlag = false; // same-tick coalescing
var _UNDO_MAX_DEPTH = 50;

// ---- Clipboard for copy/paste ----
var _drawingClipboard = null;  // { drawing: {...}, symbol: '...' }
var _NON_COPYABLE = ['position', 'longPosition', 'shortPosition', 'spot'];

function _isDrawingCopyable(name) {
    return _NON_COPYABLE.indexOf(name) === -1;
}

function _getDrawingsSnapshot() {
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        return JSON.stringify(drawings);
    } catch (e) { return '[]'; }
}

function _pushUndoSnapshot() {
    if (_undoRedoActive || _undoCoalesceFlag) return;
    if (!klineChartSymbol) return;
    var snap = _getDrawingsSnapshot();
    var sym = klineChartSymbol;
    if (!_undoStacks[sym]) _undoStacks[sym] = [];
    _undoStacks[sym].push(snap);
    if (_undoStacks[sym].length > _UNDO_MAX_DEPTH) _undoStacks[sym].shift();
    // New mutation clears redo
    _redoStacks[sym] = [];
    // Coalesce: block further snapshots this tick
    _undoCoalesceFlag = true;
    setTimeout(function() { _undoCoalesceFlag = false; }, 0);
}

function _removeDrawingOverlays() {
    if (!klineChart || !klineChartSymbol) return;
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        for (var i = 0; i < drawings.length; i++) {
            if (drawings[i].id) klineChart.removeOverlay(drawings[i].id);
        }
    } catch (e) {}
}

function _restoreSnapshot(json) {
    _undoRedoActive = true;
    try {
        _removeDrawingOverlays();
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        all[klineChartSymbol] = JSON.parse(json);
        localStorage.setItem('klineDrawings', JSON.stringify(all));
        loadKlineDrawings();
    } catch (e) {
    }
    _undoRedoActive = false;
}

function undoKlineDrawings() {
    var sym = klineChartSymbol;
    if (!sym || !_undoStacks[sym] || _undoStacks[sym].length === 0) return;
    // Push current state to redo
    if (!_redoStacks[sym]) _redoStacks[sym] = [];
    _redoStacks[sym].push(_getDrawingsSnapshot());
    // Pop and restore
    var snap = _undoStacks[sym].pop();
    _restoreSnapshot(snap);
}

function redoKlineDrawings() {
    var sym = klineChartSymbol;
    if (!sym || !_redoStacks[sym] || _redoStacks[sym].length === 0) return;
    // Push current state to undo
    if (!_undoStacks[sym]) _undoStacks[sym] = [];
    _undoStacks[sym].push(_getDrawingsSnapshot());
    // Pop and restore
    var snap = _redoStacks[sym].pop();
    _restoreSnapshot(snap);
}
var DRAWING_CURSOR = "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cline x1='12' y1='2' x2='12' y2='22' stroke='%23ffd700' stroke-width='1.5'/%3E%3Cline x1='2' y1='12' x2='22' y2='12' stroke='%23ffd700' stroke-width='1.5'/%3E%3Ccircle cx='12' cy='12' r='3' fill='none' stroke='%23ffd700' stroke-width='1'/%3E%3C/svg%3E\") 12 12, crosshair";

function _forceCanvasCursor(container, cursorValue) {
    // Read-before-write: inline-style reads don't force layout, and skipping
    // no-op writes keeps per-frame style recalc out of the crosshair's way.
    var canvases = container.querySelectorAll('canvas');
    for (var i = 0; i < canvases.length; i++) {
        var s = canvases[i].style;
        if (typeof s.getPropertyValue !== 'function' ||
            s.getPropertyValue('cursor') !== cursorValue || s.getPropertyPriority('cursor') !== 'important') {
            s.setProperty('cursor', cursorValue, 'important');
        }
    }
    var cs = container.style;
    if (typeof cs.getPropertyValue !== 'function' ||
        cs.getPropertyValue('cursor') !== cursorValue || cs.getPropertyPriority('cursor') !== 'important') {
        if (typeof cs.setProperty === 'function') cs.setProperty('cursor', cursorValue, 'important');
    }
}

function _clearCanvasCursor(container) {
    var canvases = container.querySelectorAll('canvas');
    for (var i = 0; i < canvases.length; i++) {
        canvases[i].style.removeProperty('cursor');
    }
    container.style.removeProperty('cursor');
}

// KLineChart only resets cursors during ITS own mousemove handler. We
// re-assert once per mousemove (event-driven, no polling loop), which
// reliably wins because !important on canvas elements beats KLineChart's inline
// style writes. Setting on the container alone is NOT viable: KLineChart targets
// the individual <canvas> elements with explicit cursor style, bypassing the
// container's inherited value. We must set every canvas directly.
function _setDrawingCursor(active) {
    _drawingToolActive = active;
    var container = document.getElementById('kline-chart-container');
    if (!container) return;
    if (active) {
        _forceCanvasCursor(container, DRAWING_CURSOR);
    } else {
        _clearCanvasCursor(container);
    }
}

// ---- Rect overlay hover detection, resize handles, cursor feedback ----
var _hoveredRectId = null;
var _rectResizeState = null; // { overlayId, handle, startX, startY, origPoints }
var _handleCursors = {
    TL: 'nwse-resize', TR: 'nesw-resize', BL: 'nesw-resize', BR: 'nwse-resize',
    TOP: 'ns-resize', BOTTOM: 'ns-resize', LEFT: 'ew-resize', RIGHT: 'ew-resize'
};

// Get all rect drawings for current symbol from localStorage
function _getRectDrawings() {
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        var rects = [];
        for (var i = 0; i < drawings.length; i++) {
            if (drawings[i].name === 'rect') rects.push(drawings[i]);
        }
        return rects;
    } catch (e) { return []; }
}

// Hit-test mouse position against all rect overlays
// Two-pass: handles (all rects) first, then bodies — so a handle is never shadowed by another rect's body.
// Selected/hovered rects are prioritized within each pass.
// Y-axis boundary — computed from container width minus Y-axis size
function _getYAxisStartX() {
    var cont = document.getElementById('kline-chart-container');
    if (!cont) return 99999;
    // Find the Y-axis width by looking for KLineChart's yAxis canvas
    try {
        // KLineChart wraps each pane in a div. The main area canvas and yAxis canvas are siblings.
        // The yAxis canvas is the last canvas child in the first pane row.
        var canvases = cont.querySelectorAll('canvas');
        if (canvases.length >= 2) {
            // The main chart canvas is the widest one. Y-axis canvas is narrower.
            var mainW = 0;
            for (var i = 0; i < canvases.length; i++) {
                var cw = canvases[i].offsetWidth;
                if (cw > mainW) mainW = cw;
            }
            if (mainW > 0 && mainW < cont.offsetWidth) return mainW;
        }
    } catch(e) {}
    // Fallback: container width minus typical Y-axis width
    return cont.offsetWidth - 80;
}

// X-axis strip boundary — the Y below which a click is on the time-axis label
// strip, never on a candle-pane position. Symmetric to _getYAxisStartX for the
// vertical axis; both localY and convertToPixel Y are container-relative
// (clientY - container rect.top), so this boundary is in the same space.
// short/long positions were grabbable on the time axis,
// hijacking KLineChart's native time-scrub gesture.
function _getXAxisStripTop() {
    var cont = document.getElementById('kline-chart-container');
    if (!cont) return 99999;
    var xH = 25; // KLineChart default x-axis height fallback
    try { var xs = klineChart.getSize('x_axis', 'root'); if (xs && xs.height) xH = xs.height; } catch(e) {}
    return cont.clientHeight - xH;
}

function _getRectHitAtPoint(localX, localY) {
    if (!klineChart) return null;
    // Reject hits in Y-axis zone
    if (localX > _getYAxisStartX()) return null;
    var rects = _getRectDrawings();
    var CORNER_R = 18, EDGE_R = 16;
    // Pre-compute pixel bounds for all rects
    var rd = [];
    for (var i = 0; i < rects.length; i++) {
        var d = rects[i];
        if (!d.points || d.points.length < 2) continue;
        var p0, p1;
        try {
            p0 = klineChart.convertToPixel({ timestamp: d.points[0].timestamp, value: d.points[0].value }, { paneId: d.paneId || 'candle_pane' });
            p1 = klineChart.convertToPixel({ timestamp: d.points[1].timestamp, value: d.points[1].value }, { paneId: d.paneId || 'candle_pane' });
        } catch (e) { continue; }
        if (!p0 || !p1 || p0.x == null || p1.x == null) continue;
        rd.push({
            d: d,
            x1: Math.min(p0.x, p1.x), y1: Math.min(p0.y, p1.y),
            x2: Math.max(p0.x, p1.x), y2: Math.max(p0.y, p1.y)
        });
    }
    // Keep array order for the body overlap stack (cycle sequence), then
    // sort the working list: selected/hovered first
    var rdOrig = rd.slice();
    rd.sort(function(a, b) {
        var ap = (a.d.id === klineSelectedOverlayId || a.d.id === _hoveredRectId) ? 0 : 1;
        var bp = (b.d.id === klineSelectedOverlayId || b.d.id === _hoveredRectId) ? 0 : 1;
        return ap - bp;
    });
    // Pass 1: corners + edges of ALL rects (handles always beat bodies)
    for (var i = 0; i < rd.length; i++) {
        var r = rd[i];
        var midX = (r.x1 + r.x2) / 2, midY = (r.y1 + r.y2) / 2;
        var corners = [
            { x: r.x1, y: r.y1, h: 'TL' }, { x: r.x2, y: r.y1, h: 'TR' },
            { x: r.x1, y: r.y2, h: 'BL' }, { x: r.x2, y: r.y2, h: 'BR' }
        ];
        for (var c = 0; c < 4; c++) {
            var dx = localX - corners[c].x, dy = localY - corners[c].y;
            if (dx * dx + dy * dy <= CORNER_R * CORNER_R) {
                return { overlayId: r.d.id, handle: corners[c].h, drawing: r.d };
            }
        }
        var edges = [
            { x: midX, y: r.y1, h: 'TOP' }, { x: midX, y: r.y2, h: 'BOTTOM' },
            { x: r.x1, y: midY, h: 'LEFT' }, { x: r.x2, y: midY, h: 'RIGHT' }
        ];
        for (var e = 0; e < 4; e++) {
            var dx = localX - edges[e].x, dy = localY - edges[e].y;
            if (dx * dx + dy * dy <= EDGE_R * EDGE_R) {
                return { overlayId: r.d.id, handle: edges[e].h, drawing: r.d };
            }
        }
    }
    // Pass 2: center move zones (selected/hovered rects only)
    for (var i = 0; i < rd.length; i++) {
        var r = rd[i];
        if (r.d.id !== klineSelectedOverlayId && r.d.id !== _hoveredRectId) continue;
        var cMidX = (r.x1 + r.x2) / 2, cMidY = (r.y1 + r.y2) / 2;
        var cW = Math.min(32, (r.x2 - r.x1) * 0.15), cH = Math.min(16, (r.y2 - r.y1) * 0.15);
        if (cW >= 8 && cH >= 6) {
            if (localX >= cMidX - cW / 2 && localX <= cMidX + cW / 2 && localY >= cMidY - cH / 2 && localY <= cMidY + cH / 2) {
                return { overlayId: r.d.id, handle: 'CENTER', drawing: r.d };
            }
        }
    }
    // Pass 3: bodies. `stack` lists ALL body hits in array order — the mouseup
    // handler cycles selection through it so occluded rects stay reachable.
    var bodyStack = [];
    for (var i = 0; i < rdOrig.length; i++) {
        var rO = rdOrig[i];
        if (localX >= rO.x1 && localX <= rO.x2 && localY >= rO.y1 && localY <= rO.y2) {
            bodyStack.push(rO.d.id);
        }
    }
    for (var i = 0; i < rd.length; i++) {
        var r = rd[i];
        if (localX >= r.x1 && localX <= r.x2 && localY >= r.y1 && localY <= r.y2) {
            return { overlayId: r.d.id, handle: 'BODY', drawing: r.d, stack: bodyStack };
        }
    }
    return null;
}

// Force re-render of a rect overlay by touching its extendData
function _touchRectOverlay(overlayId) {
    if (!klineChart) return;
    try {
        var ovs = klineChart.getOverlayById(overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (ov) {
            var ext = Object.assign({}, ov.extendData || {});
            ext._hoverTick = Date.now();
            klineChart.overrideOverlay({ id: overlayId, extendData: ext });
        }
    } catch (e) {}
}

// Set hover state for a rect overlay
function _setRectHovered(overlayId) {
    if (_hoveredRectId === overlayId) return;
    var prev = _hoveredRectId;
    _hoveredRectId = overlayId;
    if (prev) _touchRectOverlay(prev);
    if (overlayId) _touchRectOverlay(overlayId);
}

// Handle rect resize drag
function _handleRectResize(clientX, clientY, container, ctrlKey) {
    if (!_rectResizeState || !klineChart) return;
    var rect = container.getBoundingClientRect();
    var localX = clientX - rect.left;
    var localY = clientY - rect.top;
    var paneId = _rectResizeState.paneId || 'candle_pane';
    var point;
    try {
        point = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: paneId });
    } catch (e) { return; }
    if (!point) return;
    // Get timestamp from dataIndex
    var dataList = klineChart.getDataList();
    var ts = null;
    if (point.dataIndex != null && dataList[point.dataIndex]) {
        ts = dataList[point.dataIndex].timestamp;
    }
    var op = _rectResizeState.origPoints;
    var newP0 = { timestamp: op[0].timestamp, value: op[0].value };
    var newP1 = { timestamp: op[1].timestamp, value: op[1].value };
    var handle = _rectResizeState.handle;
    // snap helper (LEFT/RIGHT are time-only, no value snap)
    var _snap = function(val) { return _magnetSnapValue(point.dataIndex, val, paneId, !!ctrlKey); };
    // Compute new points based on handle
    if (handle === 'TL') { newP0.timestamp = ts || newP0.timestamp; newP0.value = _snap(point.value); }
    else if (handle === 'TR') { newP1.timestamp = ts || newP1.timestamp; newP0.value = _snap(point.value); }
    else if (handle === 'BL') { newP0.timestamp = ts || newP0.timestamp; newP1.value = _snap(point.value); }
    else if (handle === 'BR') { newP1.timestamp = ts || newP1.timestamp; newP1.value = _snap(point.value); }
    else if (handle === 'TOP') { newP0.value = _snap(point.value); }
    else if (handle === 'BOTTOM') { newP1.value = _snap(point.value); }
    else if (handle === 'LEFT') { newP0.timestamp = ts || newP0.timestamp; }
    else if (handle === 'RIGHT') { newP1.timestamp = ts || newP1.timestamp; }
    // Apply via overrideOverlay
    klineChart.overrideOverlay({
        id: _rectResizeState.overlayId,
        points: [newP0, newP1]
    });
    _rectResizeState.currentPoints = [newP0, newP1];
}

// Finalize resize — save to localStorage
function _finalizeRectResize() {
    if (!_rectResizeState) return;
    var state = _rectResizeState;
    _rectResizeState = null;
    if (state.currentPoints) {
        var drawing = getKlineDrawingById(state.overlayId);
        if (drawing) {
            drawing.points = state.currentPoints.map(function(p) {
                return { timestamp: p.timestamp, value: p.value };
            });
            saveKlineDrawing(drawing);
        }
    }
    _clearCanvasCursor(document.getElementById('kline-chart-container'));
}

// ---- Position overlay level drag system ----
var _hoveredPositionId = null;
var _hoveredChannelId = null;   // hover state for boundedChannel overlays
var _hoveredOverlayId = null;   // Generic hover tracking for all overlays (blue square handles)
var _nativeHoverOrigWidth = {}; // { [overlayId]: origWidth } — restore on leave
var _invisiblePointStyle = { color: 'transparent', borderColor: 'transparent', borderSize: 0, activeColor: 'transparent', activeBorderColor: 'transparent', activeBorderSize: 0 };
var _channelCornerDrag = null; // { overlayId, corner: 'p0'|'p1'|'off', paneId, origPoints, origOff } — TV-style handle drag
var _positionLevelDrag = null; // { overlayId, level, paneId, origValue, currentValue }
var _posBodyClickCycle = null; // { downX, downY, stack } — armed on body mousedown over an overlap stack
var _rectBodyClickCycle = null; // { downX, downY, stack, prevSelected } — rect-body equivalent

function _getPositionDrawings() {
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        var positions = [];
        for (var i = 0; i < drawings.length; i++) {
            var n = drawings[i].name;
            if (n === 'position' || n === 'longPosition' || n === 'shortPosition') positions.push(drawings[i]);
        }
        return positions;
    } catch (e) { return []; }
}

// ---- Bounded channel handle drag (TradingView-parity) ----
// A channel stores 3 points: p0/p1 = base trendline, p2 = a point the parallel
// line passes through (offset anchor). Handles match TradingView:
//   p0 / p1 — base-line endpoints. Dragging one pivots around the OTHER
//             endpoint and preserves the channel's price offset (rigid rotate).
//   off     — middle of the parallel line. Adjusts width only; base untouched.
// The painter formula (boundedChannel createPointFigures in chart-overlay-defs.js):
//   t = (p2.x - p0.x) / (p1.x - p0.x);  lineY = p0.y + t*(p1.y-p0.y);  offY = p2.y - lineY
//   bl = { x: p0.x, y: p0.y + offY },  br = { x: p1.x, y: p1.y + offY }
// Live overlay point values are chart-space (log-transformed when log scale is on).
function _channelHandlesFromCoords(c0, c1, c2) {
    var dx = c1.x - c0.x;
    var t = dx !== 0 ? (c2.x - c0.x) / dx : 0;
    var lineY = c0.y + t * (c1.y - c0.y);
    var offY = c2.y - lineY;
    var bl = { x: c0.x, y: c0.y + offY };
    var br = { x: c1.x, y: c1.y + offY };
    return { p0: c0, p1: c1, bl: bl, br: br, off: { x: (bl.x + br.x) / 2, y: (bl.y + br.y) / 2 } };
}

function _getChannelCornerPixels(overlayId, paneId) {
    if (!klineChart) return null;
    try {
        var _ovArr = klineChart.getOverlayById(overlayId);
        var _ov = Array.isArray(_ovArr) ? _ovArr[0] : _ovArr;
        if (!_ov || !_ov.points || _ov.points.length < 3) return null;
        var pts = _ov.points;
        var c0 = klineChart.convertToPixel({ dataIndex: pts[0].dataIndex, value: pts[0].value }, { paneId: paneId || 'candle_pane' });
        var c1 = klineChart.convertToPixel({ dataIndex: pts[1].dataIndex, value: pts[1].value }, { paneId: paneId || 'candle_pane' });
        var c2 = klineChart.convertToPixel({ dataIndex: pts[2].dataIndex, value: pts[2].value }, { paneId: paneId || 'candle_pane' });
        if (!c0 || !c1 || !c2) return null;
        return _channelHandlesFromCoords(c0, c1, c2);
    } catch (e) { return null; }
}

// Pure hit rule against an already-built channel geometry array (see
// _buildHoverGeomCache) — no store read, no convertToPixel. Split out so
// hitTestAtPoint's Pass 1 (below) can reuse the ONE geometry cache it already
// fetched for the frame instead of triggering a second _getHoverGeomCache() call
// (and therefore a second signature computation) through the public wrapper.
function _channelCornerHitFromChannels(channels, localX, localY) {
    var HR = _coarsePointer ? 26 : 18;
    for (var i = 0; i < channels.length; i++) {
        var entry = channels[i];
        var d = entry.d, handles = entry.handles, paneId = entry.paneId;
        // Nearest handle wins (zones can overlap on narrow channels)
        var candidates = [
            { corner: 'p0', pt: handles.p0 },
            { corner: 'p1', pt: handles.p1 },
            { corner: 'off', pt: handles.off }
        ];
        var best = null, bestD2 = HR * HR;
        for (var c = 0; c < candidates.length; c++) {
            var hdx = localX - candidates[c].pt.x, hdy = localY - candidates[c].pt.y;
            var d2 = hdx * hdx + hdy * hdy;
            if (d2 <= bestD2) { bestD2 = d2; best = candidates[c].corner; }
        }
        if (best) {
            return { overlayId: d.id, corner: best, paneId: paneId, drawing: d };
        }
    }
    return null;
}

function _getChannelCornerHit(localX, localY) {
    // Ignore points over the Y-axis strip
    if (localX > _getYAxisStartX()) return null;
    if (!klineChart) return null;
    // per-channel corner geometry comes from the shared
    // viewport-keyed cache (built lazily in _getHoverGeomCache below) instead of a
    // fresh convertToPixel x3 (+ fallback) recompute on every call. Called directly
    // by mousedown (discrete, not per-frame) and window._mobileHandleApi.getChannelHit —
    // hitTestAtPoint uses _channelCornerHitFromChannels directly instead (see there).
    return _channelCornerHitFromChannels(_getHoverGeomCache().channels, localX, localY);
}

// Resolve a dataIndex to a candle timestamp, extrapolating beyond both chart
// edges (same rules as resolvePointTimestamps, without the log→raw conversion —
// drag-time override points must stay in chart-space values).
function _channelTsForIndex(di) {
    try {
        var data = klineChart.getDataList();
        if (!data || !data.length) return null;
        var lastIdx = data.length - 1;
        if (di >= 0 && di <= lastIdx) return Math.round(data[di].timestamp);
        var interval = data.length > 1 ? data[lastIdx].timestamp - data[lastIdx - 1].timestamp : 86400000;
        if (di > lastIdx) return Math.round(data[lastIdx].timestamp + (di - lastIdx) * interval);
        var startInterval = data.length > 1 ? data[1].timestamp - data[0].timestamp : interval;
        return Math.round(data[0].timestamp + di * startInterval);
    } catch (e) { return null; }
}

// Build the drag state from the LIVE overlay (chart-space values + dataIndex —
// localStorage points are raw-space and would corrupt the math under log scale).
// origOff = p2's value distance from the base line, interpolated in index space.
function _armChannelDragState(hit) {
    if (!klineChart || !hit) return null;
    try {
        var ovs = klineChart.getOverlayById(hit.overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (!ov || !ov.points || ov.points.length < 3) return null;
        var pts = ov.points;
        var i0 = pts[0].dataIndex, i1 = pts[1].dataIndex, i2 = pts[2].dataIndex;
        var v0 = pts[0].value, v1 = pts[1].value, v2 = pts[2].value;
        if (i0 == null || i1 == null || i2 == null || v0 == null || v1 == null || v2 == null) return null;
        var t = (i1 === i0) ? 0 : (i2 - i0) / (i1 - i0);
        var origOff = v2 - (v0 + t * (v1 - v0));
        return {
            overlayId: hit.overlayId,
            corner: hit.corner,
            paneId: hit.paneId || 'candle_pane',
            origPoints: pts.map(function(p) { return { dataIndex: p.dataIndex, timestamp: p.timestamp, value: p.value }; }),
            origOff: origOff
        };
    } catch (e) { return null; }
}

// TradingView-parity drag semantics:
//   'p0'/'p1' — dragged endpoint follows the cursor (time + price, magnet-snapped);
//               the OPPOSITE endpoint stays fixed as the pivot; the price offset
//               is preserved, so the whole channel rotates rigidly.
//   'off'     — width only: base line untouched; the parallel line passes through
//               the cursor. The p2 anchor is kept normalized at the parallel
//               line's midpoint so its handle position stays predictable.
function _handleChannelCornerDrag(clientX, clientY, container, ctrlKey) {
    if (!_channelCornerDrag || !klineChart) return;
    var rect = container.getBoundingClientRect();
    var localX = clientX - rect.left;
    var localY = clientY - rect.top;
    var state = _channelCornerDrag;
    var pt = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: state.paneId });
    if (!pt || pt.dataIndex == null || pt.value == null) return;
    var orig = state.origPoints;
    var snappedVal = _magnetSnapValue(pt.dataIndex, pt.value, state.paneId || 'candle_pane', !!ctrlKey);
    function mkPoint(di, val) {
        var p = { dataIndex: di, value: val };
        var ts = _channelTsForIndex(di);
        if (ts != null) p.timestamp = ts;
        return p;
    }
    var newPoints;
    if (state.corner === 'off') {
        // New offset = cursor's value distance from the (unchanged) base line at the cursor's index
        var i0 = orig[0].dataIndex, i1 = orig[1].dataIndex;
        var v0 = orig[0].value, v1 = orig[1].value;
        var tC = (i1 === i0) ? 0 : (pt.dataIndex - i0) / (i1 - i0);
        var newOff = snappedVal - (v0 + tC * (v1 - v0));
        var iMid = Math.round((i0 + i1) / 2);
        var tM = (i1 === i0) ? 0 : (iMid - i0) / (i1 - i0);
        newPoints = [
            { dataIndex: i0, timestamp: orig[0].timestamp, value: v0 },
            { dataIndex: i1, timestamp: orig[1].timestamp, value: v1 },
            mkPoint(iMid, v0 + tM * (v1 - v0) + newOff)
        ];
    } else {
        var mi = state.corner === 'p0' ? 0 : 1;   // moving endpoint
        var fi = mi === 0 ? 1 : 0;                // fixed pivot endpoint
        var iM = pt.dataIndex, vM = snappedVal;
        var iF = orig[fi].dataIndex, vF = orig[fi].value;
        // p2 re-anchored at the new midpoint, original offset preserved
        var iMid2 = Math.round((iM + iF) / 2);
        var tM2 = (iF === iM) ? 0 : (iMid2 - iM) / (iF - iM);
        var vMid = vM + tM2 * (vF - vM) + state.origOff;
        newPoints = [null, null, mkPoint(iMid2, vMid)];
        newPoints[mi] = mkPoint(iM, vM);
        newPoints[fi] = { dataIndex: iF, timestamp: orig[fi].timestamp, value: vF };
    }
    try {
        klineChart.overrideOverlay({ id: state.overlayId, points: newPoints });
    } catch (e) {}
}

function _finalizeChannelCornerDrag() {
    if (!_channelCornerDrag || !klineChart) { _channelCornerDrag = null; return; }
    var state = _channelCornerDrag;
    _channelCornerDrag = null;
    // Unlock the overlay
    try { klineChart.overrideOverlay({ id: state.overlayId, lock: false }); } catch(e) {}
    try {
        var ovs = klineChart.getOverlayById(state.overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (ov) {
            var drawing = typeof getKlineDrawingById === 'function' ? getKlineDrawingById(state.overlayId) : null;
            if (drawing) {
                drawing.points = typeof resolvePointTimestamps === 'function' ? resolvePointTimestamps(ov.points) : ov.points;
                if (typeof saveKlineDrawing === 'function') saveKlineDrawing(drawing);
            }
        }
    } catch (e) {}
    var container = document.getElementById('kline-chart-container');
    if (container) _clearCanvasCursor(container);
}

// ---- FRVP edge drag (TradingView-parity) ----
// An FRVP range has two vertical boundaries. Dragging one re-anchors ONLY
// that boundary (the opposite edge stays pinned); the profile recomputes —
// instantly as a client approximation, then exactly via the debounced
// server refetch inside frvp.js's renderer (key-change driven, no hook here).
var _frvpEdgeDrag = null; // { overlayId, edge: 0|1, paneId }

// Pure hit rule against an already-built FRVP geometry array (see
// _buildHoverGeomCache) — no store read, no convertToPixel. Split out for the
// same reason as _channelCornerHitFromChannels above: lets hitTestAtPoint reuse
// its ONE per-frame geometry cache instead of triggering another
// _getHoverGeomCache() call (and signature computation) through the public wrapper.
function _frvpEdgeHitFromFrvps(frvps, localX, localY) {
    var HR = 8;
    for (var i = 0; i < frvps.length; i++) {
        var entry = frvps[i];
        var d = entry.d, geo = entry.geo;
        // Vertical gate: within the profile's price span when known (falls
        // back to full pane so a still-loading range is grabbable too)
        if (geo.yTop != null && geo.yBot != null && (localY < geo.yTop - HR || localY > geo.yBot + HR)) continue;
        if (Math.abs(localX - geo.x1) <= HR || Math.abs(localX - geo.x2) <= HR) {
            // Map the grabbed boundary back to the point index that owns it
            var edge = null;
            if (entry.c0x != null && entry.c1x != null) {
                edge = (Math.abs(localX - entry.c0x) <= Math.abs(localX - entry.c1x)) ? 0 : 1;
            }
            if (edge === null) continue;
            return { overlayId: d.id, edge: edge, paneId: 'candle_pane', drawing: d };
        }
    }
    return null;
}

function _getFrvpEdgeHit(localX, localY) {
    if (_allDrawingsHidden) return null; // hidden drawings must not grab drags
    if (localX > _getYAxisStartX()) return null;
    if (!klineChart || typeof frvpGetEdgeGeometry !== 'function') return null;
    // geometry (frvpGetEdgeGeometry's result + the edge-index
    // convertToPixel pair) comes from the shared viewport-keyed cache instead of a
    // fresh call on every hover frame. Called directly by mousedown (discrete, not
    // per-frame) and window._mobileHandleApi.getFrvpHit — hitTestAtPoint uses
    // _frvpEdgeHitFromFrvps directly instead (see there).
    return _frvpEdgeHitFromFrvps(_getHoverGeomCache().frvps, localX, localY);
}

// Extend Right: grabbing the LIVE right boundary switches the profile back
// to manual (extendRight=false) BEFORE the normal edge drag proceeds against
// the stored anchors — grab = re-anchor, TV parity. Left-edge grabs leave
// the flag alone. Persisted with the same pattern as the frvp-mode action.
function _frvpDisarmExtendRight(hit) {
    if (!hit || !klineChart) return;
    try {
        var d = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(hit.overlayId) : null;
        var ext = (d && d.extendData) || null;
        if (!ext || !ext.extendRight) return;
        // Only when the grabbed edge is the RIGHT (later) boundary
        var ovs = klineChart.getOverlayById(hit.overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (!ov || !ov.points || ov.points.length < 2) return;
        var di0 = (typeof _frvpPointDi === 'function') ? _frvpPointDi(ov.points[0]) : ov.points[0].dataIndex;
        var di1 = (typeof _frvpPointDi === 'function') ? _frvpPointDi(ov.points[1]) : ov.points[1].dataIndex;
        if (di0 == null || di1 == null) return;
        var rightEdge = (di1 >= di0) ? 1 : 0;
        if (hit.edge !== rightEdge) return;
        updateKlineDrawingExtendData(hit.overlayId, { extendRight: false });
        try { klineChart.overrideOverlay({ id: hit.overlayId, extendData: Object.assign({}, ext, { extendRight: false }) }); } catch (e2) {}
        var dd = getKlineDrawingById(hit.overlayId);
        if (dd && typeof saveKlineDrawing === 'function') saveKlineDrawing(dd);
    } catch (e) {}
}

function _handleFrvpEdgeDrag(clientX, clientY, container) {
    if (!_frvpEdgeDrag || !klineChart) return;
    var rect = container.getBoundingClientRect();
    var localX = clientX - rect.left;
    var localY = clientY - rect.top;
    var state = _frvpEdgeDrag;
    // Extend Right disarm is DEFERRED to real drag movement (>=3px). With
    // extendRight on, the live right boundary sits where users click all the
    // time (last candle / crosshair reads); disarming on mousedown made a
    // plain click silently revert the profile to manual. Until the threshold passes, the anchors
    // are not rewritten either, so a click leaves the drawing untouched.
    if (state.disarmHit) {
        if (state.startX == null) { state.startX = localX; return; } // touch: first move = baseline
        if (Math.abs(localX - state.startX) < 3) return;
        _frvpDisarmExtendRight(state.disarmHit);
        state.disarmHit = null;
    }
    var pt = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: state.paneId });
    if (!pt || pt.dataIndex == null) return;
    try {
        var ovs = klineChart.getOverlayById(state.overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (!ov || !ov.points || ov.points.length < 2) return;
        var dl = klineChart.getDataList();
        if (!dl || !dl.length) return;
        // Clamp to the last REAL bar — the dataList tail is padded with up to
        // 120 phantom (future) bars; anchoring there stores bogus future
        // timestamps and draws the box into blank space.
        var maxDi = (typeof _frvpLastRealDi === 'function') ? _frvpLastRealDi(dl) : (dl.length - 1);
        if (maxDi == null || maxDi < 0) maxDi = dl.length - 1;
        var di = Math.max(0, Math.min(maxDi, Math.round(pt.dataIndex)));
        var otherIdx = state.edge === 0 ? 1 : 0;
        var di0 = (typeof _frvpPointDi === 'function') ? _frvpPointDi(ov.points[0]) : ov.points[0].dataIndex;
        var di1 = (typeof _frvpPointDi === 'function') ? _frvpPointDi(ov.points[1]) : ov.points[1].dataIndex;
        var otherDi = state.edge === 0 ? di1 : di0;
        if (otherDi == null) return;
        // Never collapse to a zero-bar range: displace one bar off the pinned edge
        if (di === otherDi) di = (otherDi > 0) ? otherDi - 1 : otherDi + 1;
        if (di < 0 || di > dl.length - 1 || di === otherDi) return;
        var newPoints = [
            { dataIndex: di0, timestamp: ov.points[0].timestamp, value: 0 },
            { dataIndex: di1, timestamp: ov.points[1].timestamp, value: 0 }
        ];
        newPoints[state.edge] = { dataIndex: di, timestamp: _channelTsForIndex(di), value: 0 };
        klineChart.overrideOverlay({ id: state.overlayId, points: newPoints });
    } catch (e) {}
}

function _finalizeFrvpEdgeDrag() {
    if (!_frvpEdgeDrag || !klineChart) { _frvpEdgeDrag = null; return; }
    var state = _frvpEdgeDrag;
    _frvpEdgeDrag = null;
    try { klineChart.overrideOverlay({ id: state.overlayId, lock: false }); } catch(e) {}
    try {
        var ovs = klineChart.getOverlayById(state.overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (ov) {
            var drawing = typeof getKlineDrawingById === 'function' ? getKlineDrawingById(state.overlayId) : null;
            if (drawing) {
                drawing.points = typeof resolvePointTimestamps === 'function' ? resolvePointTimestamps(ov.points) : ov.points;
                if (typeof saveKlineDrawing === 'function') saveKlineDrawing(drawing);
            }
        }
    } catch (e) {}
    var container = document.getElementById('kline-chart-container');
    if (container) _clearCanvasCursor(container);
}

// Hit-test mouse against position level lines
// Returns { overlayId, level, value, drawing } or null
// Hit-test mouse position against all position overlays.
// Pass-based (parity with _getRectHitAtPoint): handle dots → level LINES (full
// box width) → edge strips → bodies, each pass across ALL positions with the
// selected/hovered one first — so one position's body never shadows another's
// handles, and the selected position always wins the grab.
// Coarse-pointer (touch) detection — computed once per session.
// Default to desktop radii when matchMedia is unavailable (test env).
var _coarsePointer = (typeof window !== 'undefined' && window.matchMedia)
    ? window.matchMedia('(pointer: coarse)').matches : false;

// Shared geometry rule: resolve the right edge pixel X for a position.
// If ext.rightTs is a finite number, convert it to pixels via klineChart.convertToPixel.
// Otherwise fall back to legacy +200px inflation when width < 60px.
// paneId and entryValue are used for the convertToPixel call (value only affects Y,
// but KLineChart needs a valid value for the call; using entryValue is consistent).
// PAINTER and HIT-TEST must call identical logic — see createPositionFigures in chart-position-overlays.js.
function _positionXRight(xLeft, xPointsMax, ext, paneId, entryValue) {
    var rightTs = ext && typeof ext.rightTs === 'number' && isFinite(ext.rightTs) ? ext.rightTs : null;
    if (rightTs !== null) {
        try {
            var rPx = klineChart.convertToPixel({ timestamp: rightTs, value: entryValue }, { paneId: paneId || 'candle_pane' });
            if (rPx && typeof rPx.x === 'number' && isFinite(rPx.x)) return rPx.x;
        } catch (e) {}
    }
    // Legacy: no rightTs — synthetic 200px minimum width
    var xRight = xPointsMax;
    if (xRight - xLeft < 60) xRight = xLeft + 200;
    return xRight;
}

function _getPositionHitAtPoint(localX, localY) {
    if (!klineChart) return null;
    // Reject hits in Y-axis zone
    if (localX > _getYAxisStartX()) return null;
    // Reject hits in the X-axis (time) strip — a mousedown there must scrub time,
    // never grab a position.
    if (localY > _getXAxisStripTop()) return null;
    var positions = _getPositionDrawings();
    // Coarse-pointer radii: touch gets larger grab zones
    var HANDLE_R = _coarsePointer ? 26 : 18;
    var LINE_R   = _coarsePointer ? 10 : 6;
    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    // Stored prices are raw; the chart works in log space when log scale is on.
    // Entry/SL/TP must transform like the ITP handles always did, or every
    // handle Y is computed wrong on log charts.
    function _toChartVal(v) { return (_isLog && v > 0) ? Math.log(v) : v; }
    // Precompute pixel geometry for all positions.
    // LIVE PATH: use getOverlayById().points[N].dataIndex → convertToPixel({dataIndex, value})
    // so the hit zone matches the painter's KLineChart-native coordinates exactly.
    // Live overlay point values are already in chart space (log-transformed when log is on)
    // — do NOT apply _toChartVal to them.
    // FALLBACK PATH (overlay not yet materialized): use stored timestamps with _toChartVal.
    var pd = [];
    for (var i = 0; i < positions.length; i++) {
        var d = positions[i];
        if (!d.points || d.points.length < 2) continue;
        var paneId = d.paneId || 'candle_pane';
        var p0, p1, p2;
        var _useLive = false;
        try {
            var _ov = klineChart.getOverlayById(d.id);
            _ov = Array.isArray(_ov) ? _ov[0] : _ov;
            if (_ov && _ov.points && _ov.points.length >= 2) {
                // Live path: dataIndex coordinates match the painter exactly
                p0 = klineChart.convertToPixel({ dataIndex: _ov.points[0].dataIndex, value: _ov.points[0].value }, { paneId: paneId });
                p1 = klineChart.convertToPixel({ dataIndex: _ov.points[1].dataIndex, value: _ov.points[1].value }, { paneId: paneId });
                if (_ov.points.length >= 3) p2 = klineChart.convertToPixel({ dataIndex: _ov.points[2].dataIndex, value: _ov.points[2].value }, { paneId: paneId });
                _useLive = true;
            }
        } catch (e) {}
        if (!_useLive) {
            // Fallback: stored timestamps (may clamp at right margin) + log transform
            try {
                p0 = klineChart.convertToPixel({ timestamp: d.points[0].timestamp, value: _toChartVal(d.points[0].value) }, { paneId: paneId });
                p1 = klineChart.convertToPixel({ timestamp: d.points[1].timestamp, value: _toChartVal(d.points[1].value) }, { paneId: paneId });
                if (d.points.length >= 3) p2 = klineChart.convertToPixel({ timestamp: d.points[2].timestamp, value: _toChartVal(d.points[2].value) }, { paneId: paneId });
            } catch (e) { continue; }
        }
        if (!p0 || !p1) continue;
        var xs = [p0.x, p1.x];
        if (p2) xs.push(p2.x);
        var xLeft = Math.min.apply(null, xs);
        var xPointsMax = Math.max.apply(null, xs);
        // Use rightTs for true right edge; legacy +200 only when no rightTs.
        // _positionXRight shares logic with createPositionFigures painter (chart-position-overlays.js).
        // Prefer the LIVE overlay's extendData — mid-drag the painter renders from it
        // while the stored copy lags until finalize; using stored here desyncs zones.
        var _extHit = (_useLive && _ov && _ov.extendData) ? _ov.extendData : d.extendData;
        var xRight = _positionXRight(xLeft, xPointsMax, _extHit, d.paneId || 'candle_pane', d.points[0].value);
        // Handle dots — TradingView style: specific grab points.
        // Returned values are always STORED raw values (drag-offset math uses raw).
        var handles = [
            { x: xLeft, y: p0.y, level: 'entry', value: d.points[0].value },
            { x: xRight, y: p0.y, level: 'right-edge', value: d.points[0].value },
            { x: xLeft, y: p1.y, level: 'sl', value: d.points[1].value }
        ];
        if (p2 && d.points.length >= 3) {
            handles.push({ x: xLeft, y: p2.y, level: 'tp', value: d.points[2].value });
        }
        var ext = d.extendData || {};
        var itps = ext.tps || [];
        // ITP values are always stored raw — always apply _toChartVal for pixel Y.
        // X anchor: same source as p0 (live dataIndex when live, stored timestamp in fallback).
        for (var t = 0; t < itps.length; t++) {
            try {
                var itpPx;
                if (_useLive) {
                    var _itpDi = (_ov && _ov.points && _ov.points[0]) ? _ov.points[0].dataIndex : null;
                    if (_itpDi !== null && _itpDi !== undefined) {
                        itpPx = klineChart.convertToPixel({ dataIndex: _itpDi, value: _toChartVal(itps[t]) }, { paneId: paneId });
                    }
                } else {
                    itpPx = klineChart.convertToPixel({ timestamp: d.points[0].timestamp, value: _toChartVal(itps[t]) }, { paneId: paneId });
                }
                if (itpPx) handles.push({ x: xLeft, y: itpPx.y, level: 'itp:' + t, value: itps[t] });
            } catch (e) {}
        }
        var allYs = handles.map(function(h) { return h.y; });
        pd.push({
            d: d, handles: handles, xLeft: xLeft, xRight: xRight,
            yTop: Math.min.apply(null, allYs) - 5,
            yBot: Math.max.apply(null, allYs) + 5
        });
    }
    // Selected/hovered first within each pass
    var sorted = pd.slice().sort(function(a, b) {
        var ap = (a.d.id === klineSelectedOverlayId || a.d.id === _hoveredPositionId) ? 0 : 1;
        var bp = (b.d.id === klineSelectedOverlayId || b.d.id === _hoveredPositionId) ? 0 : 1;
        return ap - bp;
    });
    var p, h;
    // Pass 1: handle dots of ALL positions — never shadowed by another's body
    for (i = 0; i < sorted.length; i++) {
        p = sorted[i];
        var closestHandle = null, closestDist = Infinity;
        for (h = 0; h < p.handles.length; h++) {
            var dx = localX - p.handles[h].x;
            var dy = localY - p.handles[h].y;
            var dist = Math.sqrt(dx * dx + dy * dy);
            if (dist <= HANDLE_R && dist < closestDist) {
                closestDist = dist;
                closestHandle = p.handles[h];
            }
        }
        if (closestHandle) {
            return { overlayId: p.d.id, level: closestHandle.level, value: closestHandle.value, drawing: p.d };
        }
    }
    // Pass 2: level LINES across the full box width — entry/SL/TP/ITP are
    // grabbable anywhere along the line, not only at the left-edge dot
    for (i = 0; i < sorted.length; i++) {
        p = sorted[i];
        if (localX < p.xLeft || localX > p.xRight) continue;
        var closestLine = null, closestLineDist = Infinity;
        for (h = 0; h < p.handles.length; h++) {
            if (p.handles[h].level === 'right-edge') continue; // not a price level
            var ldy = Math.abs(localY - p.handles[h].y);
            if (ldy <= LINE_R && ldy < closestLineDist) {
                closestLineDist = ldy;
                closestLine = p.handles[h];
            }
        }
        if (closestLine) {
            return { overlayId: p.d.id, level: closestLine.level, value: closestLine.value, drawing: p.d };
        }
    }
    // Pass 3: edge strips — full-height horizontal resize grab areas
    for (i = 0; i < sorted.length; i++) {
        p = sorted[i];
        if (localY < p.yTop || localY > p.yBot) continue;
        if (Math.abs(localX - p.xRight) <= 15) {
            return { overlayId: p.d.id, level: 'right-edge', value: p.d.points[0].value, drawing: p.d };
        }
        if (Math.abs(localX - p.xLeft) <= 18) {
            return { overlayId: p.d.id, level: 'left-edge', value: p.d.points[0].value, drawing: p.d };
        }
    }
    // Pass 4: bodies. `stack` lists ALL body hits in array order — the mouseup
    // handler cycles selection through it so occluded positions stay reachable.
    var stack = [];
    for (i = 0; i < pd.length; i++) {
        p = pd[i];
        if (localX >= p.xLeft && localX <= p.xRight && localY >= p.yTop && localY <= p.yBot) {
            stack.push(p.d.id);
        }
    }
    if (stack.length === 0) return null;
    // Drag priority: the selected/hovered one when it is in the stack
    var grabbed = stack[0];
    for (i = 0; i < sorted.length; i++) {
        if (stack.indexOf(sorted[i].d.id) !== -1) { grabbed = sorted[i].d.id; break; }
    }
    var gd = null;
    for (i = 0; i < pd.length; i++) {
        if (pd[i].d.id === grabbed) { gd = pd[i].d; break; }
    }
    return { overlayId: grabbed, level: 'body', value: null, drawing: gd, stack: stack };
}

// Next overlay in a body-hit stack (array order), wrapping. Pure — node-tested.
function _nextInCycleStack(stack, selectedId) {
    if (!stack || stack.length === 0) return null;
    var i = stack.indexOf(selectedId);
    return stack[(i + 1) % stack.length];
}

// ---- z-order helpers ----
// z_order lives inside drawing.metadata as z_order (persisted via _doSyncDrawing).
// On the client the drawing object carries a zOrder number (default 0 when absent).
function _maxZOrder() {
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        var max = 0;
        for (var i = 0; i < drawings.length; i++) {
            var z = (drawings[i].zOrder != null) ? drawings[i].zOrder : 0;
            if (z > max) max = z;
        }
        return max;
    } catch (e) { return 0; }
}

function _minZOrder() {
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        var min = 0;
        for (var i = 0; i < drawings.length; i++) {
            var z = (drawings[i].zOrder != null) ? drawings[i].zOrder : 0;
            if (z < min) min = z;
        }
        return min;
    } catch (e) { return 0; }
}

// ---- viewport-keyed pixel-geometry cache for hover hit-testing ----
// hitTestAtPoint (plus its per-frame callees _getChannelCornerHit/_getFrvpEdgeHit below)
// would recompute EVERY rect/position/channel/FRVP drawing's pixel geometry (2-4
// convertToPixel calls each) on EVERY rAF hover frame. The drawings store is parsed
// once (_readDrawingsStore); this cache avoids the redundant geometry MATH
// built from that parse — geometry only actually changes when the viewport pans/zooms/
// resizes, the drawings themselves change, or the price axis autoscales, never on a
// plain mouse move with nothing else different (the overwhelming majority of frames).
//
// Consumed mainly by hover frames: drags mutate overlays per-frame via overrideOverlay
// without touching localStorage until drop, and the mousemove dispatcher (see
// _scheduleDragRaf above) `return`s into the drag-rAF path before ever reaching the
// hover pass while a drag is live — so a mid-drag frame never reads this cache.
// _getChannelCornerHit/_getFrvpEdgeHit are also called once per mousedown (not per
// frame); they share the same cache harmlessly since it is signature-gated regardless
// of caller — a stale read is impossible by construction, not by convention.
var _hoverGeomCache = null; // { sig, rects: [...], positions: [...], channels: [...], frvps: [...] }

// "Has anything geometry-relevant changed since the last read" — a handful of cheap
// klinecharts getters + one container read + one convertToPixel call, versus the O(N)
// convertToPixel fan-out this replaces.
function _hoverGeomSignature() {
    var raw = null;
    try { _readDrawingsStore(); raw = _drawingsStoreCache.raw; } catch (e) {}
    var vr = null;
    try { if (klineChart && typeof klineChart.getVisibleRange === 'function') vr = klineChart.getVisibleRange(); } catch (e) {}
    var barSpace = null;
    try { if (klineChart && typeof klineChart.getBarSpace === 'function') barSpace = klineChart.getBarSpace(); } catch (e) {}
    var contW = null, contH = null;
    try {
        var cont = document.getElementById('kline-chart-container');
        if (cont) { contW = cont.offsetWidth; contH = cont.offsetHeight; }
    } catch (e) {}
    // Y-AXIS SENTINEL: convertToPixel's Y depends on the visible PRICE range, which
    // autoscale can reshape on a new tick WITHOUT the time-viewport (vr/barSpace) or
    // container size changing at all. klinecharts exposes no cheap y-axis-extremum
    // getter, so convert one FIXED chart-space reference point every frame and compare —
    // if its pixel Y moved, the price→pixel map changed and the cache is stale. A fixed
    // (dataIndex:0, value:0) sentinel is used rather than a drawing's price_level so this
    // still works with zero drawings on the symbol and never hinges on which particular
    // drawing happens to exist/survive a delete.
    var ySentinel = null;
    try {
        if (klineChart) {
            var sp = klineChart.convertToPixel({ dataIndex: 0, value: 0 }, { paneId: 'candle_pane' });
            if (sp && typeof sp.y === 'number') ySentinel = sp.y;
        }
    } catch (e) {}
    return {
        raw: raw,
        symbol: (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : null,
        timeframe: (typeof klineChartTimeframe !== 'undefined') ? klineChartTimeframe : null,
        vrFrom: vr ? vr.from : null, vrTo: vr ? vr.to : null,
        vrRealFrom: vr ? vr.realFrom : null, vrRealTo: vr ? vr.realTo : null,
        barSpace: barSpace, contW: contW, contH: contH, ySentinel: ySentinel
    };
}

function _hoverGeomSigEqual(a, b) {
    return !!a && !!b &&
        a.raw === b.raw && a.symbol === b.symbol && a.timeframe === b.timeframe &&
        a.vrFrom === b.vrFrom && a.vrTo === b.vrTo &&
        a.vrRealFrom === b.vrRealFrom && a.vrRealTo === b.vrRealTo &&
        a.barSpace === b.barSpace && a.contW === b.contW && a.contH === b.contH &&
        a.ySentinel === b.ySentinel;
}

// Build all four shapes' pixel geometry ONCE for the current signature. Mirrors the
// exact math of hitTestAtPoint (rect/position) and
// _getChannelCornerHit/_getFrvpEdgeHit (channel/FRVP) below — it runs once per
// signature instead of once per call.
function _buildHoverGeomCache(sig) {
    var rects = [];
    try {
        var rectDrawings = _getRectDrawings();
        for (var ri = 0; ri < rectDrawings.length; ri++) {
            var dr = rectDrawings[ri];
            if (!dr.points || dr.points.length < 2) continue;
            var p0r, p1r;
            try {
                p0r = klineChart.convertToPixel({ timestamp: dr.points[0].timestamp, value: dr.points[0].value }, { paneId: dr.paneId || 'candle_pane' });
                p1r = klineChart.convertToPixel({ timestamp: dr.points[1].timestamp, value: dr.points[1].value }, { paneId: dr.paneId || 'candle_pane' });
            } catch (e) { continue; }
            if (!p0r || !p1r || p0r.x == null || p1r.x == null) continue;
            var zr = (dr.zOrder != null) ? dr.zOrder : 0;
            rects.push({
                d: dr, zOrder: zr,
                x1: Math.min(p0r.x, p1r.x), y1: Math.min(p0r.y, p1r.y),
                x2: Math.max(p0r.x, p1r.x), y2: Math.max(p0r.y, p1r.y)
            });
        }
    } catch (e) {}

    var positions = [];
    try {
        var posDrawings = _getPositionDrawings();
        var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
        function _toChartValU(v) { return (_isLog && v > 0) ? Math.log(v) : v; }
        for (var pi = 0; pi < posDrawings.length; pi++) {
            var dp = posDrawings[pi];
            if (!dp.points || dp.points.length < 2) continue;
            var paneId = dp.paneId || 'candle_pane';
            var pp0, pp1, pp2;
            var _useLive = false;
            var _pov = null;
            try {
                _pov = klineChart.getOverlayById(dp.id);
                _pov = Array.isArray(_pov) ? _pov[0] : _pov;
                if (_pov && _pov.points && _pov.points.length >= 2) {
                    pp0 = klineChart.convertToPixel({ dataIndex: _pov.points[0].dataIndex, value: _pov.points[0].value }, { paneId: paneId });
                    pp1 = klineChart.convertToPixel({ dataIndex: _pov.points[1].dataIndex, value: _pov.points[1].value }, { paneId: paneId });
                    if (_pov.points.length >= 3) pp2 = klineChart.convertToPixel({ dataIndex: _pov.points[2].dataIndex, value: _pov.points[2].value }, { paneId: paneId });
                    _useLive = true;
                }
            } catch (e) {}
            if (!_useLive) {
                try {
                    pp0 = klineChart.convertToPixel({ timestamp: dp.points[0].timestamp, value: _toChartValU(dp.points[0].value) }, { paneId: paneId });
                    pp1 = klineChart.convertToPixel({ timestamp: dp.points[1].timestamp, value: _toChartValU(dp.points[1].value) }, { paneId: paneId });
                    if (dp.points.length >= 3) pp2 = klineChart.convertToPixel({ timestamp: dp.points[2].timestamp, value: _toChartValU(dp.points[2].value) }, { paneId: paneId });
                } catch (e) { continue; }
            }
            if (!pp0 || !pp1) continue;
            var xsPd = [pp0.x, pp1.x];
            if (pp2) xsPd.push(pp2.x);
            var xLeftPd = Math.min.apply(null, xsPd);
            var xPointsMaxPd = Math.max.apply(null, xsPd);
            // Use rightTs for true right edge; legacy +200 only when no rightTs.
            // Must match _positionXRight in _getPositionHitAtPoint and createPositionFigures painter.
            // Prefer the LIVE overlay's extendData (see _getPositionHitAtPoint note).
            var _extPd = (_useLive && _pov && _pov.extendData) ? _pov.extendData : dp.extendData;
            var xRightPd = _positionXRight(xLeftPd, xPointsMaxPd, _extPd, dp.paneId || 'candle_pane', dp.points[0].value);
            var handlesPd = [
                { x: xLeftPd, y: pp0.y, level: 'entry', value: dp.points[0].value },
                { x: xRightPd, y: pp0.y, level: 'right-edge', value: dp.points[0].value },
                { x: xLeftPd, y: pp1.y, level: 'sl', value: dp.points[1].value }
            ];
            if (pp2 && dp.points.length >= 3) {
                handlesPd.push({ x: xLeftPd, y: pp2.y, level: 'tp', value: dp.points[2].value });
            }
            var extPd = dp.extendData || {};
            var itpsPd = extPd.tps || [];
            for (var ti = 0; ti < itpsPd.length; ti++) {
                try {
                    var itpPx2;
                    if (_useLive && _pov) {
                        var _itpDi2 = (_pov.points && _pov.points[0]) ? _pov.points[0].dataIndex : null;
                        if (_itpDi2 !== null && _itpDi2 !== undefined) {
                            itpPx2 = klineChart.convertToPixel({ dataIndex: _itpDi2, value: _toChartValU(itpsPd[ti]) }, { paneId: paneId });
                        }
                    } else {
                        itpPx2 = klineChart.convertToPixel({ timestamp: dp.points[0].timestamp, value: _toChartValU(itpsPd[ti]) }, { paneId: paneId });
                    }
                    if (itpPx2) handlesPd.push({ x: xLeftPd, y: itpPx2.y, level: 'itp:' + ti, value: itpsPd[ti] });
                } catch (e2) {}
            }
            var allYsPd = handlesPd.map(function(h) { return h.y; });
            var zpd = (dp.zOrder != null) ? dp.zOrder : 0;
            positions.push({
                d: dp, zOrder: zpd, handles: handlesPd, xLeft: xLeftPd, xRight: xRightPd,
                yTop: Math.min.apply(null, allYsPd) - 5,
                yBot: Math.max.apply(null, allYsPd) + 5
            });
        }
    } catch (e) {}

    var channels = [];
    try {
        var all1 = _readDrawingsStore();
        var chDrawings = (all1 && klineChartSymbol) ? (all1[klineChartSymbol] || []) : [];
        for (var ci = 0; ci < chDrawings.length; ci++) {
            var cd = chDrawings[ci];
            if (cd.name !== 'boundedChannel' || !cd.points || cd.points.length < 3) continue;
            var cPaneId = cd.paneId || 'candle_pane';
            // Try live geometry first
            var handles = _getChannelCornerPixels(cd.id, cPaneId);
            if (!handles) {
                // Fallback: stored timestamps (may be less accurate but better than nothing)
                try {
                    var fb0 = klineChart.convertToPixel({ timestamp: cd.points[0].timestamp, value: _toChartVal(cd.points[0].value) }, { paneId: cPaneId });
                    var fb1 = klineChart.convertToPixel({ timestamp: cd.points[1].timestamp, value: _toChartVal(cd.points[1].value) }, { paneId: cPaneId });
                    var fb2 = klineChart.convertToPixel({ timestamp: cd.points[2].timestamp, value: _toChartVal(cd.points[2].value) }, { paneId: cPaneId });
                    if (fb0 && fb1 && fb2) handles = _channelHandlesFromCoords(fb0, fb1, fb2);
                } catch (e2) {}
            }
            if (handles) channels.push({ d: cd, paneId: cPaneId, handles: handles });
        }
    } catch (e) {}

    var frvps = [];
    try {
        if (typeof frvpGetEdgeGeometry === 'function') {
            var all2 = _readDrawingsStore();
            var fvDrawings = (all2 && klineChartSymbol) ? (all2[klineChartSymbol] || []) : [];
            for (var fi = 0; fi < fvDrawings.length; fi++) {
                var fd = fvDrawings[fi];
                if (fd.name !== 'frvp') continue;
                var geo = frvpGetEdgeGeometry(fd.id);
                if (!geo) continue;
                // Map the grabbed boundary back to the point index that owns it (same
                // convertToPixel pair _getFrvpEdgeHit computes).
                var c0x = null, c1x = null;
                try {
                    var fovs = klineChart.getOverlayById(fd.id);
                    var fov = Array.isArray(fovs) ? fovs[0] : fovs;
                    if (fov && fov.points && fov.points.length >= 2 && typeof _frvpPointDi === 'function') {
                        var c0 = klineChart.convertToPixel({ dataIndex: _frvpPointDi(fov.points[0]), value: 0 }, { paneId: 'candle_pane' });
                        var c1 = klineChart.convertToPixel({ dataIndex: _frvpPointDi(fov.points[1]), value: 0 }, { paneId: 'candle_pane' });
                        if (c0) c0x = c0.x;
                        if (c1) c1x = c1.x;
                    }
                } catch (e2) {}
                frvps.push({ d: fd, geo: geo, c0x: c0x, c1x: c1x });
            }
        }
    } catch (e) {}

    return { sig: sig, rects: rects, positions: positions, channels: channels, frvps: frvps };
}

// Returns the current geometry cache, rebuilding it iff the signature changed since the
// last read. Safe to call every frame AND from the (non-per-frame) mousedown hit-tests.
function _getHoverGeomCache() {
    var sig = _hoverGeomSignature();
    if (_hoverGeomCache && _hoverGeomSigEqual(_hoverGeomCache.sig, sig)) return _hoverGeomCache;
    _hoverGeomCache = _buildHoverGeomCache(sig);
    return _hoverGeomCache;
}

// ---- Unified hit-test ----
// Single entry point covering rect, boundedChannel, and position overlays.
// TradingView-like pass order across all shapes:
//   Pass 1: handle dots / corners of ALL shapes
//   Pass 2: position level lines (full-box-width; rect CENTER + strips come after)
//   Pass 3: rect CENTER zones + edge strips (rect LEFT/RIGHT, position right/left-edge)
//   Pass 4: bodies — ordered by zOrder descending (highest = topmost = grabbed first)
//
// Selected position beats all other overlays at the same point (preserves _selPosHit
// semantics from mousedown :791-798 in the refactored dispatch).
// Y-axis guard applied once at the top.
// Returns shape-typed result with normalized `shapeType` + `interaction` fields plus
// the original per-shape fields (handle / level / corner) so consumers need minimal change.
function hitTestAtPoint(localX, localY) {
    if (_allDrawingsHidden) return null; // hidden drawings must not grab clicks
    if (!klineChart) return null;
    if (localX > _getYAxisStartX()) return null;
    if (localY > _getXAxisStripTop()) return null;

    // rect/position pixel geometry comes from the shared,
    // viewport-keyed cache (_getHoverGeomCache above) instead of being recomputed via
    // convertToPixel on every call — same shapes ({d,zOrder,x1,y1,x2,y2} / {d,zOrder,
    // handles,xLeft,xRight,yTop,yBot}) the Pass 1-4 logic below already expects.
    var _geomCache = _getHoverGeomCache();
    var rd = _geomCache.rects;
    var CORNER_R = 18, EDGE_R = 16;

    var pd = _geomCache.positions;
    var HANDLE_R = _coarsePointer ? 26 : 18;
    var LINE_R   = _coarsePointer ? 10 : 6;

    // Sort helpers: selected/hovered first (within-pass priority), then zOrder desc
    function _selSort(id) {
        return (id === klineSelectedOverlayId || id === _hoveredRectId || id === _hoveredPositionId) ? 0 : 1;
    }
    function sortByPriority(arr, getId) {
        return arr.slice().sort(function(a, b) {
            var ap = _selSort(getId(a)), bp = _selSort(getId(b));
            if (ap !== bp) return ap - bp;
            // zOrder desc: higher zOrder = tested first (topmost)
            return (b.zOrder || 0) - (a.zOrder || 0);
        });
    }

    var rdSorted = sortByPriority(rd, function(r) { return r.d.id; });
    var pdSorted = sortByPriority(pd, function(p) { return p.d.id; });

    // ---- Pass 1: handle dots / corners of ALL shapes — interleaved by priority ----
    // Build a list of all hit candidates in Pass 1, then return the highest-priority one.
    // Each candidate: { id, selPri, zOrder, result } where selPri=0 if selected/hovered.
    var p1candidates = [];

    // Rect corners + edges
    for (var ri2 = 0; ri2 < rd.length; ri2++) {
        var rr = rd[ri2];
        var midX = (rr.x1 + rr.x2) / 2, midY = (rr.y1 + rr.y2) / 2;
        var corners2 = [
            { x: rr.x1, y: rr.y1, h: 'TL' }, { x: rr.x2, y: rr.y1, h: 'TR' },
            { x: rr.x1, y: rr.y2, h: 'BL' }, { x: rr.x2, y: rr.y2, h: 'BR' }
        ];
        var hitCorner = null;
        for (var ci = 0; ci < 4; ci++) {
            var dx2 = localX - corners2[ci].x, dy2 = localY - corners2[ci].y;
            if (dx2 * dx2 + dy2 * dy2 <= CORNER_R * CORNER_R) {
                hitCorner = { overlayId: rr.d.id, shapeType: 'rect', interaction: 'handle', handle: corners2[ci].h, drawing: rr.d };
                break;
            }
        }
        if (!hitCorner) {
            var edges2 = [
                { x: midX, y: rr.y1, h: 'TOP' }, { x: midX, y: rr.y2, h: 'BOTTOM' },
                { x: rr.x1, y: midY, h: 'LEFT' }, { x: rr.x2, y: midY, h: 'RIGHT' }
            ];
            for (var ei = 0; ei < 4; ei++) {
                var dxe = localX - edges2[ei].x, dye = localY - edges2[ei].y;
                if (dxe * dxe + dye * dye <= EDGE_R * EDGE_R) {
                    hitCorner = { overlayId: rr.d.id, shapeType: 'rect', interaction: 'handle', handle: edges2[ei].h, drawing: rr.d };
                    break;
                }
            }
        }
        if (hitCorner) {
            p1candidates.push({ id: rr.d.id, selPri: _selSort(rr.d.id), zOrder: rr.zOrder, result: hitCorner });
        }
    }
    // Position handle dots — find closest per position, if within HANDLE_R
    for (var pi2 = 0; pi2 < pd.length; pi2++) {
        var pp = pd[pi2];
        var closestH = null, closestD2 = Infinity;
        for (var hi = 0; hi < pp.handles.length; hi++) {
            var dxh = localX - pp.handles[hi].x, dyh = localY - pp.handles[hi].y;
            var dist2 = dxh * dxh + dyh * dyh;
            if (dist2 <= HANDLE_R * HANDLE_R && dist2 < closestD2) {
                closestD2 = dist2;
                closestH = pp.handles[hi];
            }
        }
        if (closestH) {
            p1candidates.push({
                id: pp.d.id, selPri: _selSort(pp.d.id), zOrder: pp.zOrder,
                result: { overlayId: pp.d.id, shapeType: 'position', interaction: 'handle', level: closestH.level, value: closestH.value, drawing: pp.d }
            });
        }
    }
    // Channel corners (no zOrder currently; treat as zOrder=0)
    // reuse the geometry cache already fetched above (_geomCache)
    // instead of calling the public _getChannelCornerHit wrapper, which would trigger
    // a second _getHoverGeomCache() → _hoverGeomSignature() call this same frame.
    var chHit2 = _channelCornerHitFromChannels(_geomCache.channels, localX, localY);
    if (chHit2) {
        var chZ = chHit2.drawing ? ((chHit2.drawing.zOrder != null) ? chHit2.drawing.zOrder : 0) : 0;
        p1candidates.push({
            id: chHit2.overlayId, selPri: _selSort(chHit2.overlayId), zOrder: chZ,
            result: { overlayId: chHit2.overlayId, shapeType: 'channel', interaction: 'corner', corner: chHit2.corner, paneId: chHit2.paneId, drawing: chHit2.drawing }
        });
    }
    // FRVP edge boundaries (ew-resize affordance)
    // same reuse as the channel-corner check above — one
    // _getHoverGeomCache() call per hitTestAtPoint invocation, not three.
    var fvHit2 = _frvpEdgeHitFromFrvps(_geomCache.frvps, localX, localY);
    if (fvHit2) {
        var fvZ = fvHit2.drawing ? ((fvHit2.drawing.zOrder != null) ? fvHit2.drawing.zOrder : 0) : 0;
        p1candidates.push({
            id: fvHit2.overlayId, selPri: _selSort(fvHit2.overlayId), zOrder: fvZ,
            result: { overlayId: fvHit2.overlayId, shapeType: 'frvp', interaction: 'edge', edge: fvHit2.edge, paneId: fvHit2.paneId, drawing: fvHit2.drawing }
        });
    }
    if (p1candidates.length > 0) {
        // Sort: selected/hovered first (selPri 0 < 1), then zOrder desc (topmost first)
        p1candidates.sort(function(a, b) {
            if (a.selPri !== b.selPri) return a.selPri - b.selPri;
            return (b.zOrder || 0) - (a.zOrder || 0);
        });
        return p1candidates[0].result;
    }

    // ---- Pass 2: position level lines (full-box-width) ----
    for (var pi3 = 0; pi3 < pdSorted.length; pi3++) {
        var pp2 = pdSorted[pi3];
        if (localX < pp2.xLeft || localX > pp2.xRight) continue;
        var closestL = null, closestLD = Infinity;
        for (var hi2 = 0; hi2 < pp2.handles.length; hi2++) {
            if (pp2.handles[hi2].level === 'right-edge') continue;
            var ldy = Math.abs(localY - pp2.handles[hi2].y);
            if (ldy <= LINE_R && ldy < closestLD) {
                closestLD = ldy;
                closestL = pp2.handles[hi2];
            }
        }
        if (closestL) {
            return { overlayId: pp2.d.id, shapeType: 'position', interaction: 'level', level: closestL.level, value: closestL.value, drawing: pp2.d };
        }
    }

    // ---- Pass 3: rect CENTER zones + edge strips ----
    // Rect CENTER (selected/hovered only)
    for (var ri3 = 0; ri3 < rdSorted.length; ri3++) {
        var rr3 = rdSorted[ri3];
        if (rr3.d.id !== klineSelectedOverlayId && rr3.d.id !== _hoveredRectId) continue;
        var cMidX = (rr3.x1 + rr3.x2) / 2, cMidY = (rr3.y1 + rr3.y2) / 2;
        var cW = Math.min(32, (rr3.x2 - rr3.x1) * 0.15), cH = Math.min(16, (rr3.y2 - rr3.y1) * 0.15);
        if (cW >= 8 && cH >= 6) {
            if (localX >= cMidX - cW / 2 && localX <= cMidX + cW / 2 && localY >= cMidY - cH / 2 && localY <= cMidY + cH / 2) {
                return { overlayId: rr3.d.id, shapeType: 'rect', interaction: 'handle', handle: 'CENTER', drawing: rr3.d };
            }
        }
    }
    // Position edge strips
    for (var pi4 = 0; pi4 < pdSorted.length; pi4++) {
        var pp4 = pdSorted[pi4];
        if (localY < pp4.yTop || localY > pp4.yBot) continue;
        if (Math.abs(localX - pp4.xRight) <= 15) {
            return { overlayId: pp4.d.id, shapeType: 'position', interaction: 'edge', level: 'right-edge', value: pp4.d.points[0].value, drawing: pp4.d };
        }
        if (Math.abs(localX - pp4.xLeft) <= 18) {
            return { overlayId: pp4.d.id, shapeType: 'position', interaction: 'edge', level: 'left-edge', value: pp4.d.points[0].value, drawing: pp4.d };
        }
    }

    // ---- Pass 4: bodies — merged rect+position ordered by zOrder descending ----
    // Build merged candidate list for body overlap
    var bodyCandidates = [];
    for (var ri4 = 0; ri4 < rd.length; ri4++) {
        var rr4 = rd[ri4];
        if (localX >= rr4.x1 && localX <= rr4.x2 && localY >= rr4.y1 && localY <= rr4.y2) {
            bodyCandidates.push({ id: rr4.d.id, shapeType: 'rect', zOrder: rr4.zOrder, rectData: rr4 });
        }
    }
    for (var pi5 = 0; pi5 < pd.length; pi5++) {
        var pp5 = pd[pi5];
        if (localX >= pp5.xLeft && localX <= pp5.xRight && localY >= pp5.yTop && localY <= pp5.yBot) {
            bodyCandidates.push({ id: pp5.d.id, shapeType: 'position', zOrder: pp5.zOrder, posData: pp5 });
        }
    }
    if (bodyCandidates.length === 0) return null;

    // Stack ordered by zOrder ascending (deterministic; used for cycle sequence)
    bodyCandidates.sort(function(a, b) { return (a.zOrder || 0) - (b.zOrder || 0); });
    var bodyStack = bodyCandidates.map(function(c) { return c.id; });

    // Grab priority: (a) selected position first, (b) selected/hovered others, (c) zOrder desc
    var grabbed = null;
    // (a) selected position in stack
    if (klineSelectedOverlayId) {
        for (var bc = 0; bc < bodyCandidates.length; bc++) {
            if (bodyCandidates[bc].id === klineSelectedOverlayId && bodyCandidates[bc].shapeType === 'position') {
                grabbed = bodyCandidates[bc];
                break;
            }
        }
    }
    if (!grabbed) {
        // (b) + (c): sort desc by priority then zOrder
        var grabSorted = bodyCandidates.slice().sort(function(a, b) {
            var ap = _selSort(a.id), bp = _selSort(b.id);
            if (ap !== bp) return ap - bp;
            return (b.zOrder || 0) - (a.zOrder || 0);
        });
        grabbed = grabSorted[0];
    }

    if (grabbed.shapeType === 'rect') {
        var rData = grabbed.rectData;
        return { overlayId: grabbed.id, shapeType: 'rect', interaction: 'body', handle: 'BODY', drawing: rData.d, stack: bodyStack };
    } else {
        var pData = grabbed.posData;
        return { overlayId: grabbed.id, shapeType: 'position', interaction: 'body', level: 'body', value: null, drawing: pData.d, stack: bodyStack };
    }
}

function _touchPositionOverlay(overlayId) {
    if (!klineChart) return;
    try {
        var ovs = klineChart.getOverlayById(overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (ov) {
            var ext = Object.assign({}, ov.extendData || {});
            ext._hoverTick = Date.now();
            klineChart.overrideOverlay({ id: overlayId, extendData: ext });
        }
    } catch (e) {}
}

function _setPositionHovered(overlayId) {
    if (_hoveredPositionId === overlayId) return;
    var prev = _hoveredPositionId;
    _hoveredPositionId = overlayId;
    if (prev) _touchPositionOverlay(prev);
    if (overlayId) _touchPositionOverlay(overlayId);
}

// channel hover — poke the overlay to force a repaint (same pattern as rect/position)
function _touchChannelOverlay(overlayId) {
    if (!klineChart) return;
    try {
        var ovs = klineChart.getOverlayById(overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (ov) {
            var ext = Object.assign({}, ov.extendData || {});
            ext._hoverTick = Date.now();
            klineChart.overrideOverlay({ id: overlayId, extendData: ext });
        }
    } catch (e) {}
}

function _setChannelHovered(overlayId) {
    if (_hoveredChannelId === overlayId) return;
    var prev = _hoveredChannelId;
    _hoveredChannelId = overlayId;
    if (prev) _touchChannelOverlay(prev);
    if (overlayId) _touchChannelOverlay(overlayId);
}

function _handlePositionLevelDrag(clientX, clientY, container, ctrlKey) {
    if (!_positionLevelDrag || !klineChart) return;
    var rect = container.getBoundingClientRect();
    var localX = clientX - rect.left;
    var localY = clientY - rect.top;
    var paneId = _positionLevelDrag.paneId || 'candle_pane';
    var point;
    try {
        point = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: paneId });
    } catch (e) { return; }
    if (!point || point.value == null) return;
    // Apply click offset so dragging starts from grab point, not absolute position
    var clickOffset = _positionLevelDrag.clickOffset || 0;
    var newValue = point.value - clickOffset;
    // snap single-level drags only (never body translations)
    var _lvl = _positionLevelDrag.level;
    if (_lvl !== 'body' && _lvl !== 'right-edge' && _lvl !== 'left-edge') {
        newValue = _magnetSnapValue(point.dataIndex, newValue, paneId, !!ctrlKey);
    }
    _positionLevelDrag.currentValue = newValue;
    var overlayId = _positionLevelDrag.overlayId;
    var level = _positionLevelDrag.level;
    try {
        var ovs = klineChart.getOverlayById(overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (!ov) return;
        var newPoints = ov.points.map(function(p) { return Object.assign({}, p); });
        var ext = Object.assign({}, ov.extendData || {});
        var entryVal = newPoints[0].value;
        var slVal = newPoints[1].value;
        var isLong = slVal < entryVal;
        var tps = (ext.tps || []).slice();
        var finalTP = newPoints.length >= 3 ? newPoints[2].value : null;

        // Body: move entire position — shift all points + intermediate TPs
        if (level === 'body') {
            var bodyDelta = newValue - entryVal;
            for (var _vi = 0; _vi < newPoints.length; _vi++) {
                newPoints[_vi].value += bodyDelta;
            }
            if (tps.length > 0) {
                for (var _ti = 0; _ti < tps.length; _ti++) tps[_ti] += bodyDelta;
                ext.tps = tps;
            }
        }
        // Entry: adjust entry price only — SL/TP stay fixed, but shift intermediate TPs
        else if (level === 'entry') {
            var entryDelta = newValue - entryVal;
            newPoints[0].value = newValue;
            if (tps.length > 0) {
                for (var _ti2 = 0; _ti2 < tps.length; _ti2++) tps[_ti2] += entryDelta;
                ext.tps = tps;
            }
        }
        else if (level === 'sl') {
            // SL must stay on correct side of entry
            if (isLong) newValue = Math.min(entryVal - 0.01, newValue);
            else newValue = Math.max(entryVal + 0.01, newValue);
            newPoints[1].value = newValue;
        }
        else if (level === 'tp' && newPoints.length >= 3) {
            // Final TP must stay beyond last intermediate TP
            var lastItp = tps.length > 0 ? tps[tps.length - 1] : entryVal;
            if (isLong) newValue = Math.max(lastItp + 0.01, newValue);
            else newValue = Math.min(lastItp - 0.01, newValue);
            newPoints[2].value = newValue;
        }
        else if (level.indexOf('itp:') === 0) {
            var idx = parseInt(level.split(':')[1]);
            if (idx >= 0 && idx < tps.length) {
                // Clamp between neighbors: prev level (or entry) and next level (or final TP)
                var lowerBound = idx === 0 ? entryVal : tps[idx - 1];
                var upperBound = idx === tps.length - 1 ? (finalTP != null ? finalTP : newValue) : tps[idx + 1];
                if (isLong) newValue = Math.max(lowerBound + 0.01, Math.min(upperBound - 0.01, newValue));
                else newValue = Math.min(lowerBound - 0.01, Math.max(upperBound + 0.01, newValue));
                tps[idx] = newValue;
                ext.tps = tps;
            }
        }
        // Horizontal movement: right-edge drag updates only ext.rightTs;
        // left-edge drag moves all point timestamps (anchor shift, rightTs stays fixed);
        // body drag moves all points AND shifts rightTs by same delta (box moves intact).
        if ((level === 'right-edge' || level === 'left-edge' || level === 'body') && _positionLevelDrag.initPixelXs && _positionLevelDrag.startX !== undefined) {
            var pixelDx = localX - _positionLevelDrag.startX;
            if (Math.abs(pixelDx) >= 2) {
                var paneId3 = _positionLevelDrag.paneId || 'candle_pane';
                var initXs3 = _positionLevelDrag.initPixelXs;
                // Minimum bar width for right-edge / left-edge clamp (2 bars)
                var _dl3min = null;
                try { _dl3min = klineChart.getDataList(); } catch(e) {}
                var _barMs = (_dl3min && _dl3min.length >= 2) ? (_dl3min[1].timestamp - _dl3min[0].timestamp) : 0;

                if (level === 'right-edge') {
                    // Only update ext.rightTs — points stay untouched
                    try {
                        var newPtR = klineChart.convertFromPixel({ x: _positionLevelDrag.initRightX + pixelDx, y: 0 }, { paneId: paneId3 });
                        if (newPtR && newPtR.dataIndex !== undefined && _dl3min) {
                            var _riIdx = Math.max(0, Math.min(newPtR.dataIndex, _dl3min.length - 1));
                            var newRightTs = _dl3min[_riIdx].timestamp;
                            // Clamp: rightTs >= entry bar + 2 bars min width.
                            // Use origEntryTs recorded at arm time (live overlay points have
                            // dataIndex not timestamp, so newPoints[0].timestamp may be undefined).
                            var _entryTs = _positionLevelDrag.origEntryTs != null
                                ? _positionLevelDrag.origEntryTs
                                : newPoints[0].timestamp;
                            if (_barMs > 0 && _entryTs != null) newRightTs = Math.max(newRightTs, _entryTs + 2 * _barMs);
                            ext.rightTs = newRightTs;
                        }
                    } catch(e) {}
                } else if (level === 'left-edge') {
                    // Left-edge: move all point timestamps but keep rightTs fixed (width changes from left)
                    for (var _liIdx = 0; _liIdx < newPoints.length; _liIdx++) {
                        if (initXs3[_liIdx] === null) continue;
                        try {
                            var newPtL = klineChart.convertFromPixel({ x: initXs3[_liIdx] + pixelDx, y: 0 }, { paneId: paneId3 });
                            if (newPtL && newPtL.dataIndex !== undefined && _dl3min && newPtL.dataIndex >= 0 && newPtL.dataIndex < _dl3min.length) {
                                var newLTs = _dl3min[newPtL.dataIndex].timestamp;
                                var newLIdx = newPtL.dataIndex;
                                // Clamp: entry bar must be <= rightTs - 2 bars
                                if (ext.rightTs && _barMs > 0 && newLTs > ext.rightTs - 2 * _barMs) {
                                    while (newLIdx > 0 && _dl3min[newLIdx].timestamp > ext.rightTs - 2 * _barMs) newLIdx--;
                                    newLTs = _dl3min[newLIdx].timestamp;
                                }
                                newPoints[_liIdx].timestamp = newLTs;
                                // overrideOverlay does NOT re-resolve dataIndex from timestamp —
                                // without this the render and the finalize save keep the old bar.
                                newPoints[_liIdx].dataIndex = newLIdx;
                            }
                        } catch(e) {}
                    }
                } else {
                    // Body: shift ALL point timestamps by same delta, shift rightTs too
                    for (var _bi = 0; _bi < newPoints.length; _bi++) {
                        if (initXs3[_bi] === null) continue;
                        try {
                            var newPtB = klineChart.convertFromPixel({ x: initXs3[_bi] + pixelDx, y: 0 }, { paneId: paneId3 });
                            if (newPtB && newPtB.dataIndex !== undefined) {
                                var dlB = klineChart.getDataList();
                                if (dlB && newPtB.dataIndex >= 0 && newPtB.dataIndex < dlB.length) {
                                    newPoints[_bi].timestamp = dlB[newPtB.dataIndex].timestamp;
                                    // overrideOverlay does NOT re-resolve dataIndex from timestamp —
                                    // without this the box never actually translates (stale bar saved).
                                    newPoints[_bi].dataIndex = newPtB.dataIndex;
                                }
                            }
                        } catch(e) {}
                    }
                    // Shift rightTs by the same bar delta if present
                    if (typeof ext.rightTs === 'number' && isFinite(ext.rightTs) && _positionLevelDrag.initRightTs !== undefined) {
                        try {
                            var _initRightPx = klineChart.convertToPixel({ timestamp: _positionLevelDrag.initRightTs, value: newPoints[0].value }, { paneId: paneId3 });
                            if (_initRightPx) {
                                var newPtBR = klineChart.convertFromPixel({ x: _initRightPx.x + pixelDx, y: 0 }, { paneId: paneId3 });
                                if (newPtBR && newPtBR.dataIndex !== undefined) {
                                    var dlBR = klineChart.getDataList();
                                    if (dlBR && newPtBR.dataIndex >= 0 && newPtBR.dataIndex < dlBR.length) {
                                        ext.rightTs = dlBR[newPtBR.dataIndex].timestamp;
                                    }
                                }
                            }
                        } catch(e) {}
                    }
                }
            }
        }
        klineChart.overrideOverlay({ id: overlayId, points: newPoints, extendData: ext });
    } catch (e) {}
}

function _finalizePositionLevelDrag() {
    if (!_positionLevelDrag) return;
    var state = _positionLevelDrag;
    _positionLevelDrag = null;
    // Guidelines off + repaint poke so the bounded box restores instantly
    window._positionGuidelinesFor = null;
    _msTouchOverlay(state.overlayId);
    try {
        var ovs = klineChart.getOverlayById(state.overlayId);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        if (ov) {
            var drawing = getKlineDrawingById(state.overlayId);
            if (drawing) {
                drawing.points = resolvePointTimestamps(ov.points);
                drawing.extendData = Object.assign(drawing.extendData || {}, ov.extendData || {});
                // Convert TPs from chart space (log) back to raw for storage
                var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                if (_isLog && drawing.extendData.tps) {
                    drawing.extendData.tps = drawing.extendData.tps.map(function(v) { return Math.exp(v); });
                }
                saveKlineDrawing(drawing);
            }
        }
    } catch (e) {}
    _clearCanvasCursor(document.getElementById('kline-chart-container'));
}

// TP-zone hover readout — when the cursor rests inside the SELECTED
// position's TP zone (body hit between entry and final TP), show the live
// price · % · R label (the _showTpReadout singleton in position-tools.js). Alt+click there
// sticks an intermediate TP (mousedown handler). Never active during drags,
// armed tools, or T-mode (T-mode drives the readout itself).
function _tpReadoutPass(localX, localY, hit) {
    var shown = false;
    try {
        // Locked positions can NEVER be selected by clicking the chart (the
        // mousedown handler nulls the position hit when lock === true), so
        // hover alone must be enough to engage the readout for them — that
        // is their ONLY way to reach it.
        var _rpLocked = !!(hit && hit.drawing && hit.drawing.extendData && hit.drawing.extendData._locked === true);
        if (hit && hit.shapeType === 'position' && hit.level === 'body' &&
            (hit.overlayId === klineSelectedOverlayId || _rpLocked) &&
            !_positionLevelDrag && !_drawingToolActive && !window._addTPModeActive &&
            typeof window.carracPositionStats === 'function' && typeof window._showTpReadout === 'function' &&
            hit.drawing && hit.drawing.points && hit.drawing.points.length >= 3) {
            var _rpPane = hit.drawing.paneId || 'candle_pane';
            var _rpCoord = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: _rpPane });
            if (_rpCoord && _rpCoord.value != null) {
                var _rpRaw = (typeof window.carracFromChartSpace === 'function') ? window.carracFromChartSpace(_rpCoord.value) : _rpCoord.value;
                // Drawing-store points are RAW prices (log-safe compare)
                var _rpE = hit.drawing.points[0].value, _rpS = hit.drawing.points[1].value, _rpT = hit.drawing.points[2].value;
                var _rpInTp = (_rpS < _rpE) ? (_rpRaw > _rpE && _rpRaw < _rpT) : (_rpRaw < _rpE && _rpRaw > _rpT);
                if (_rpInTp && _rpRaw != null) {
                    var _rpSt = window.carracPositionStats(_rpRaw, _rpE, _rpS);
                    var _rpFmt = (typeof _fmtObjPrice === 'function') ? _fmtObjPrice : function(v) { return String(v); };
                    // Locked positions can never reach Alt+click (unselectable), so the
                    // hint would be a lie there — only show it selected-and-unlocked.
                    var _rpSuffix = _rpLocked ? '' : '  ⌥click = TP';
                    window._showTpReadout(localX, localY,
                        _rpFmt(_rpRaw) + ' · ' + (_rpSt.pct >= 0 ? '+' : '') + _rpSt.pct.toFixed(2) + '%' + (_rpSt.rLabel ? ' · ' + _rpSt.rLabel : '') + _rpSuffix);
                    shown = true;
                }
            }
        }
    } catch (e) {}
    if (!shown && typeof window._hideTpReadout === 'function' && !window._addTPModeActive) window._hideTpReadout();
    return shown;
}

// Hover/hit/cursor pass — runs at most ONCE PER FRAME (rAF-coalesced from the
// mousemove listener). Raw mousemove fires at up to 1000 Hz on high-poll mice;
// running hitTestAtPoint + getBoundingClientRect + the !important cursor
// writes per event starved the engine's own crosshair update (laggy hairline).
// rAF also runs AFTER the engine's event handlers, so the cursor re-assert
// lands last in the frame and wins more reliably than the capture-phase write.
function _hoverCursorPass(clientX, clientY, altKey, container) {
    if (!klineChart) return;
    if (_rectResizeState || _channelCornerDrag || _frvpEdgeDrag || _positionLevelDrag || _ctrlCloneDrag || _msDrag || _marqueeState || _drawingToolActive) {
        if (typeof window._hideTpReadout === 'function' && !window._addTPModeActive) window._hideTpReadout();
        return;
    }
    var rect = container.getBoundingClientRect();
    var localX = clientX - rect.left;
    var localY = clientY - rect.top;
        // Alt held = pan override: skip hover/hit work entirely, show grab.
        // Placed AFTER the drag states so pressing Alt mid-drag can't orphan one.
        // Exception: keep the TP-zone readout live so ⌥click aiming stays visible
        // (only costs a hit test while a position is selected).
        if (altKey) {
            if (klineSelectedOverlayId) _tpReadoutPass(localX, localY, hitTestAtPoint(localX, localY));
            _forceCanvasCursor(container, 'grab');
            return;
        }

        // unified cursor dispatch via hitTestAtPoint.
        // Hover-state updates (_setRectHovered/_setPositionHovered/_setChannelHovered) are kept for
        // render-loop parity (e.g. CENTER zone needs selected/hovered to be set).
        var unifiedHit = hitTestAtPoint(localX, localY);

        if (unifiedHit && unifiedHit.shapeType === 'rect') {
            _setRectHovered(unifiedHit.overlayId);
            _setPositionHovered(null);
            _setChannelHovered(null);
            var _rHandle = unifiedHit.handle;
            if (_rHandle === 'CENTER' || _rHandle === 'BODY') {
                _forceCanvasCursor(container, 'move');
            } else if (_handleCursors[_rHandle]) {
                _forceCanvasCursor(container, _handleCursors[_rHandle]);
            }
        } else if (unifiedHit && unifiedHit.shapeType === 'channel') {
            _setRectHovered(null);
            _setPositionHovered(null);
            _setChannelHovered(unifiedHit.overlayId);
            _forceCanvasCursor(container, 'pointer');
        } else if (unifiedHit && unifiedHit.shapeType === 'frvp') {
            _setRectHovered(null);
            _setPositionHovered(null);
            _setChannelHovered(null);
            _hoveredOverlayId = unifiedHit.overlayId; // shows the edge handles
            _forceCanvasCursor(container, 'ew-resize');
        } else if (unifiedHit && unifiedHit.shapeType === 'position') {
            _setRectHovered(null);
            _setPositionHovered(unifiedHit.overlayId);
            _setChannelHovered(null);
            var _pLevel = unifiedHit.level;
            var _isEdgeHover = (_pLevel === 'right-edge' || _pLevel === 'left-edge');
            var _posCursor = _isEdgeHover ? 'ew-resize' : (_pLevel === 'body' ? 'move' : 'ns-resize');
            _forceCanvasCursor(container, _posCursor);
        } else {
            _setRectHovered(null);
            _setPositionHovered(null);
            _setChannelHovered(null);
            // KLineChart-hovered native overlay: pointer near endpoint, move on body
            if (_hoveredOverlayId && !_rectResizeState && !_positionLevelDrag) {
                var _nearPt = false;
                try {
                    var _ovArr = klineChart.getOverlayById(_hoveredOverlayId);
                    var _ov = Array.isArray(_ovArr) ? _ovArr[0] : _ovArr;
                    if (_ov && _ov.points) {
                        var _pid = _ov.paneId || 'candle_pane';
                        for (var _pi = 0; _pi < _ov.points.length; _pi++) {
                            var _pp = klineChart.convertToPixel(_ov.points[_pi], { paneId: _pid });
                            if (_pp) {
                                var _dpx = localX - _pp.x, _dpy = localY - _pp.y;
                                if (_dpx * _dpx + _dpy * _dpy <= 144) { _nearPt = true; break; }
                            }
                        }
                    }
                } catch(e) {}
                _forceCanvasCursor(container, _nearPt ? 'pointer' : 'move');
            } else if (!_rectResizeState && !_positionLevelDrag) {
                // Freehand fallback
                var fhId = (typeof freehandHitTest === 'function') ? freehandHitTest(localX, localY) : null;
                if (fhId) {
                    if (typeof _freehandHoveredId !== 'undefined') _freehandHoveredId = fhId;
                    _forceCanvasCursor(container, 'pointer');
                    if (typeof _freehandRedraw === 'function') _freehandRedraw();
                } else {
                    if (typeof _freehandHoveredId !== 'undefined' && _freehandHoveredId) {
                        _freehandHoveredId = null;
                        if (typeof _freehandRedraw === 'function') _freehandRedraw();
                    }
                    _clearCanvasCursor(container);
                }
            }
        }
        // TP-zone readout show/hide for this frame (position branch or none)
        _tpReadoutPass(localX, localY, unifiedHit);
}

// ---- rAF-coalesced drag repaint ----
// The six branches below (_handleMsDrag/_handleCtrlCloneDrag/_handleRectResize/
// _handleChannelCornerDrag/_handleFrvpEdgeDrag/_handlePositionLevelDrag) each do
// container.getBoundingClientRect() (forced layout) + klineChart.overrideOverlay()
// (a FULL O(N) overlay repaint — v9 repaints every overlay on any change, and
// overrideOverlay's own updatePane(1) triggers that repaint again). Called
// synchronously per raw mousemove they run at mouse-report rate (up to ~1000Hz
// on high-poll mice) instead of display rate — the same problem the
// hover pass below solves, here on the drag path that returns before it.
// Mirrors that pattern: own container-attached state (__dragEvt/__dragRaf, NOT
// __hoverEvt/__hoverRaf) so it coalesces correctly even if this container ever
// picks up more than one mousemove listener registration.
// The handler FUNCTIONS themselves are untouched and still run synchronously
// when called directly — window._mobileHandleApi (touch drag path in mobile-chart.js)
// and the node tests call them straight, bypassing this scheduler entirely.
// Only the mousemove dispatch below defers the CALL to at most once per frame.
var _dragBranchHandlers = {
    ms: function(x, y, c) { _handleMsDrag(x, y, c); },
    ctrlClone: function(x, y, c) { _handleCtrlCloneDrag(x, y, c); },
    rectResize: function(x, y, c, ctrl) { _handleRectResize(x, y, c, ctrl); },
    channelCorner: function(x, y, c, ctrl) { _handleChannelCornerDrag(x, y, c, ctrl); },
    frvpEdge: function(x, y, c) { _handleFrvpEdgeDrag(x, y, c); },
    positionLevel: function(x, y, c, ctrl) { _handlePositionLevelDrag(x, y, c, ctrl); }
};

// Run the pending drag branch (if any) with its LAST payload, right now —
// used both by the deferred rAF callback and by the mouseup sync-flush.
function _flushDragRaf(container) {
    var pending = container.__dragEvt;
    if (!pending) return;
    container.__dragEvt = null;
    var fn = _dragBranchHandlers[pending.branch];
    if (fn) fn(pending.clientX, pending.clientY, container, pending.ctrlKey);
}

// Store the latest payload for `branch` and schedule ONE rAF flush. The six
// branches are mutually exclusive (the mousemove dispatcher below returns
// after the first match) so a single pending slot per container is enough —
// no drag branch is ever active at the same time as another.
function _scheduleDragRaf(container, branch, clientX, clientY, ctrlKey) {
    container.__dragEvt = { branch: branch, clientX: clientX, clientY: clientY, ctrlKey: ctrlKey };
    if (!container.__dragRaf && typeof requestAnimationFrame === 'function') {
        container.__dragRaf = requestAnimationFrame(function() {
            container.__dragRaf = null;
            _flushDragRaf(container);
        });
    }
}

// The single window-bound mouseup listener installed by
// _initOverlayHoverDetection — kept module-level so a re-init can swap it
// out instead of stacking a second one (see the guard inside).
var _winMouseupHandler = null;

function _initOverlayHoverDetection() {
    var container = document.getElementById('kline-chart-container');
    if (!container) return;

    // reflect persisted magnet mode on the toolbar button
    _updateMagnetButton();
    // Magnet for native tools: wrap the built-in overlay templates once
    _installMagnetNativeHooks();

    // This runs on every chart render (symbol/TF switch). Desktop split
    // layout keeps ONE #kline-chart-container across switches, so without
    // this guard the listeners below would stack per switch. A recreated
    // container (mobile/modal reopen) has no flag and falls through to
    // rebind with fresh closures against the new element.
    if (container._hoverBound) return;
    container._hoverBound = true;

    // Mousedown in capture phase — intercept handle clicks before KLineChart
    container.addEventListener('mousedown', function(e) {
        // Track mouse position for onPressedMoveStart Y-axis check
        container._lastMouseX = e.clientX;
        container._lastMouseY = e.clientY;
        _magnetCtrlHeld = e.ctrlKey;
        if (_drawingToolActive || !klineChart) return;

        // Task 2 — Alt+drag pan override: skip ALL custom branches so
        // KLineChart's native pan handles the drag. Custom shape fills have
        // ignoreEvent, so the engine pans through them naturally.
        if (e.altKey) return;

        var rect = container.getBoundingClientRect();
        var localX = e.clientX - rect.left;
        var localY = e.clientY - rect.top;

        // Cross-type arbitration: a hit on the SELECTED position beats other
        // overlays' (possibly invisible) handles — the shape you selected is
        // the one your click acts on.
        var _selPosHit = null;
        if (klineSelectedOverlayId) {
            var _sp = _getPositionHitAtPoint(localX, localY);
            if (_sp && _sp.overlayId === klineSelectedOverlayId) _selPosHit = _sp;
        }

        // plain (non-ctrl) click — determine if it lands on a multi-select member body.
        // If not, clear the multi-select set. If it IS a member, group-move arm is handled below
        // in the per-shape branches.
        if (!e.ctrlKey && klineMultiSelectIds.length) {
            // Quick pre-check: if the hit body belongs to the set, keep it (group move handled below)
            var _msHitId = null;
            var _msRectHit = _getRectHitAtPoint(localX, localY);
            if (_msRectHit && (_msRectHit.handle === 'BODY' || _msRectHit.handle === 'CENTER') && _msHas(_msRectHit.overlayId)) {
                _msHitId = _msRectHit.overlayId;
            }
            if (!_msHitId) {
                var _msPosHit = _getPositionHitAtPoint(localX, localY);
                if (_msPosHit && _msPosHit.level === 'body' && _msHas(_msPosHit.overlayId)) {
                    _msHitId = _msPosHit.overlayId;
                }
            }
            if (!_msHitId) {
                // Check hovered channel body
                var _msChanId = _hoveredChannelId || null;
                if (!_msChanId && _hoveredOverlayId) {
                    var _msCDrw = getKlineDrawingById(_hoveredOverlayId);
                    if (_msCDrw && _msCDrw.name === 'boundedChannel') _msChanId = _hoveredOverlayId;
                }
                if (_msChanId && _msHas(_msChanId)) _msHitId = _msChanId;
            }
            if (!_msHitId) {
                // Hit is not on a set member — clear the set
                _msClear();
            }
        }

        // Check rect handles — all 4 corners + 4 edges use custom resize
        var hit = _selPosHit ? null : _getRectHitAtPoint(localX, localY);
        // Skip rect drag if locked
        if (hit && hit.handle !== 'BODY' && hit.handle !== 'CENTER' && _handleCursors[hit.handle] && !e.ctrlKey) {
            var _rOv = null;
            try { var _ra = klineChart.getOverlayById(hit.overlayId); _rOv = Array.isArray(_ra) ? _ra[0] : _ra; } catch(e) {}
            if (_rOv && _rOv.lock === true) { hit = null; }
        }
        if (hit && hit.handle !== 'BODY' && hit.handle !== 'CENTER' && _handleCursors[hit.handle] && !e.ctrlKey) {
            e.stopPropagation();
            e.preventDefault();
            var drawing = hit.drawing;
            var origP = drawing.points;
            var minTs = Math.min(origP[0].timestamp, origP[1].timestamp);
            var maxTs = Math.max(origP[0].timestamp, origP[1].timestamp);
            var minVal = Math.min(origP[0].value, origP[1].value);
            var maxVal = Math.max(origP[0].value, origP[1].value);
            _rectResizeState = {
                overlayId: hit.overlayId,
                handle: hit.handle,
                paneId: drawing.paneId || 'candle_pane',
                origPoints: [
                    { timestamp: minTs, value: maxVal },
                    { timestamp: maxTs, value: minVal }
                ],
                currentPoints: null
            };
            _forceCanvasCursor(container, _handleCursors[hit.handle]);
            return;
        }

        // Ctrl+drag clone — rect BODY move with Ctrl held leaves the
        // original in place and drags an in-place clone via a CUSTOM drag.
        // (The first ship tried locking the original so the engine would grab
        // the clone natively — the engine starts drags from its own hover
        // state, which still referenced the original, so nothing moved.)
        if (hit && e.ctrlKey) { // ANY rect grab point: short rects have no body zone (edge strips cover them)
            if (_armCtrlCloneDrag(hit.overlayId, localX, localY, container, e)) {
                _rectBodyClickCycle = null;
                return;
            }
        }

        // non-ctrl click on a rect body that is in the multi-select set → group move
        if (hit && (hit.handle === 'BODY' || hit.handle === 'CENTER') && !e.ctrlKey && klineMultiSelectIds.length > 1 && _msHas(hit.overlayId)) {
            if (_armMsDrag(localX, localY, container, e)) {
                _rectBodyClickCycle = null;
                return;
            }
        }

        // Rect body overlap cycling: arm when this click lands on a stack that
        // already contains the selection. Native select/drag proceeds as usual;
        // a plain click (no drag) advances the selection on mouseup.
        if (hit && hit.handle === 'BODY' && hit.stack && hit.stack.length > 1 &&
            hit.stack.indexOf(klineSelectedOverlayId) !== -1) {
            _rectBodyClickCycle = { downX: e.clientX, downY: e.clientY, stack: hit.stack, prevSelected: klineSelectedOverlayId };
        } else {
            _rectBodyClickCycle = null;
        }

        // Check channel handles (p0/p1 endpoints + parallel-line offset handle)
        var chHit = _selPosHit ? null : _getChannelCornerHit(localX, localY);
        if (chHit && e.ctrlKey) { chHit = null; } // Ctrl = select/clone, never handle-drag
        if (chHit) {
            var chState = _armChannelDragState(chHit);
            if (chState) {
                e.stopImmediatePropagation();
                e.preventDefault();
                klineSelectedOverlayId = chHit.overlayId;
                // Lock overlay to prevent KLineChart's own drag
                try { klineChart.overrideOverlay({ id: chHit.overlayId, lock: true }); } catch(e) {}
                _channelCornerDrag = chState;
                _forceCanvasCursor(container, 'pointer');
                return;
            }
        }

        // Check FRVP edge boundaries (drag re-anchors one side, TV-style)
        var fvHit = (_selPosHit || chHit) ? null : _getFrvpEdgeHit(localX, localY);
        if (fvHit && e.ctrlKey) { fvHit = null; } // Ctrl = select/clone, never edge-drag
        if (fvHit) {
            e.stopImmediatePropagation();
            e.preventDefault();
            klineSelectedOverlayId = fvHit.overlayId;
            // Extend Right: grabbing the live right boundary reverts to manual —
            // but only once a REAL drag starts (>=3px in _handleFrvpEdgeDrag).
            // A plain click must not disarm (see the deferred-disarm comment there).
            var _fvDrawing = getKlineDrawingById(fvHit.overlayId);
            var _fvPending = !!(_fvDrawing && _fvDrawing.extendData && _fvDrawing.extendData.extendRight);
            // Lock overlay to prevent KLineChart's own whole-shape drag
            try { klineChart.overrideOverlay({ id: fvHit.overlayId, lock: true }); } catch(e) {}
            _frvpEdgeDrag = { overlayId: fvHit.overlayId, edge: fvHit.edge, paneId: fvHit.paneId,
                disarmHit: _fvPending ? fvHit : null, startX: localX };
            _forceCanvasCursor(container, 'ew-resize');
            return;
        }

        // detect the hovered channel body (no channel-body hit-test
        // exists; resolve via _hoveredChannelId / _hoveredOverlayId).
        if (!chHit) {
            var _chBodyId = null;
            if (_hoveredChannelId) {
                _chBodyId = _hoveredChannelId;
            } else if (_hoveredOverlayId) {
                var _chDrw = getKlineDrawingById(_hoveredOverlayId);
                if (_chDrw && _chDrw.name === 'boundedChannel') _chBodyId = _hoveredOverlayId;
            }

            // non-ctrl click on a channel body that's in the multi-select set → group move
            if (!e.ctrlKey && _chBodyId && klineMultiSelectIds.length > 1 && _msHas(_chBodyId)) {
                if (_armMsDrag(localX, localY, container, e)) return;
            }

            // Ctrl+drag clone — channel BODY move with Ctrl held.
            if (e.ctrlKey && _chBodyId && _armCtrlCloneDrag(_chBodyId, localX, localY, container, e)) {
                _rectBodyClickCycle = null;
                return;
            }
        }

        // Check position handles + body
        var posHit = _selPosHit || _getPositionHitAtPoint(localX, localY);
        if (posHit) {
            // Skip drag if overlay is locked
            var _posOv = null;
            try { var _pa = klineChart.getOverlayById(posHit.overlayId); _posOv = Array.isArray(_pa) ? _pa[0] : _pa; } catch(e) {}
            if (_posOv && _posOv.lock === true) { posHit = null; }
        }
        if (posHit) {
            _rectBodyClickCycle = null; // position interaction wins over rect cycling

            // Alt+click inside the SELECTED position's TP zone → stick an
            // intermediate TP at that price (same path as T-mode's click;
            // pairs with the _tpReadoutPass hover label).
            if (e.altKey && !e.ctrlKey && posHit.overlayId === klineSelectedOverlayId &&
                posHit.drawing && posHit.drawing.points && posHit.drawing.points.length >= 3) {
                try {
                    var _asPane = posHit.drawing.paneId || 'candle_pane';
                    var _asCoord = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: _asPane });
                    if (_asCoord && _asCoord.value != null) {
                        var _asRaw = (typeof window.carracFromChartSpace === 'function') ? window.carracFromChartSpace(_asCoord.value) : _asCoord.value;
                        var _asE = posHit.drawing.points[0].value, _asS = posHit.drawing.points[1].value, _asT = posHit.drawing.points[2].value;
                        var _asInTp = (_asS < _asE) ? (_asRaw > _asE && _asRaw < _asT) : (_asRaw < _asE && _asRaw > _asT);
                        if (_asInTp) {
                            e.stopPropagation();
                            e.preventDefault();
                            var _asOvs = klineChart.getOverlayById(posHit.overlayId);
                            var _asOv = Array.isArray(_asOvs) ? _asOvs[0] : _asOvs;
                            if (_asOv) {
                                var _asExt = _asOv.extendData || {};
                                if (!_asExt.tps) _asExt.tps = [];
                                // Live overlay ext.tps live in CHART SPACE (ln under log)
                                _asExt.tps.push(_asCoord.value);
                                var _asEChart = _asOv.points[0].value;
                                _asExt.tps.sort(function(a, b) { return Math.abs(a - _asEChart) - Math.abs(b - _asEChart); });
                                klineChart.overrideOverlay({ id: _asOv.id, extendData: _asExt });
                                var _asRec = getKlineDrawingById(posHit.overlayId);
                                if (_asRec) {
                                    _asRec.extendData = Object.assign({}, _asExt);
                                    var _asLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                                    if (_asLog && _asRec.extendData.tps) {
                                        _asRec.extendData.tps = _asRec.extendData.tps.map(function(v) { return Math.exp(v); });
                                    }
                                    saveKlineDrawing(_asRec);
                                }
                            }
                            return;
                        }
                    }
                } catch (ex) {}
            }

            // Ctrl+click on position body → arm for toggle (resolved on mouseup)
            if (e.ctrlKey && _msIsEligible(posHit.drawing && posHit.drawing.name)) { // ANY level: Ctrl = select, never level-drag
                e.stopPropagation();
                e.preventDefault();
                _msPosClickArm = { id: posHit.overlayId, downX: e.clientX, downY: e.clientY };
                return;
            }

            // non-ctrl click on a position body that's in the multi-select set → group move
            if (!e.ctrlKey && posHit.level === 'body' && klineMultiSelectIds.length > 1 && _msHas(posHit.overlayId)) {
                e.stopPropagation();
                e.preventDefault();
                if (_armMsDrag(localX, localY, container, e)) return;
            }

            e.stopPropagation();
            e.preventDefault();
            // Plain click (no drag) on an ALREADY-selected body inside an overlap
            // stack cycles selection on mouseup — occluded positions stay reachable.
            _posBodyClickCycle = (posHit.level === 'body' && posHit.stack && posHit.stack.length > 1 &&
                                  klineSelectedOverlayId === posHit.overlayId)
                ? { downX: e.clientX, downY: e.clientY, stack: posHit.stack }
                : null;
            // Select the overlay so keyboard shortcuts (T for add TP, Del, etc.) work
            klineSelectedOverlayId = posHit.overlayId;
            var _paneId = posHit.drawing.paneId || 'candle_pane';
            // For body drag, use entry value as reference for delta
            var _hitValue = posHit.level === 'body' ? posHit.drawing.points[0].value : posHit.value;
            // Convert hit value to chart space (log scale) for correct offset computation
            var _isLogScale = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
            var _hitValueChart = (_isLogScale && _hitValue > 0) ? Math.log(_hitValue) : _hitValue;
            // Compute click offset for smooth delta-based dragging (no jump on grab)
            var _clickOffset = null;
            try {
                var _clickPt = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: _paneId });
                if (_clickPt && _clickPt.value != null && _hitValueChart != null) {
                    _clickOffset = _clickPt.value - _hitValueChart;
                }
            } catch(e) {}
            if (_clickOffset == null) {
                if (posHit.level === 'right-edge' || posHit.level === 'left-edge') {
                    // Horizontal-only drags don't use the value offset — arm anyway.
                    _clickOffset = 0;
                } else {
                    // No grab offset (convertFromPixel failed) — arming the drag would
                    // snap the level to the cursor on the first move. Select only.
                    _touchPositionOverlay(posHit.overlayId);
                    return;
                }
            }
            // Store initial pixel X positions for edge resize and body move
            var _initPixelXs = null;
            var _initRightX = null;
            var _initRightTs = undefined;
            if (posHit.level === 'right-edge' || posHit.level === 'left-edge' || posHit.level === 'body') {
                var _pts = posHit.drawing.points || [];
                _initPixelXs = _pts.map(function(p) {
                    try {
                        var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                        var val = (_isLog && p.value > 0) ? Math.log(p.value) : p.value;
                        var px = klineChart.convertToPixel({ timestamp: p.timestamp, value: val }, { paneId: _paneId });
                        return px ? px.x : null;
                    } catch(e) { return null; }
                });
                // For right-edge drag: record initial pixel X of the right edge handle
                // For body drag: record initial rightTs for shifting it by the same delta
                var _dExt = posHit.drawing.extendData || {};
                if (typeof _dExt.rightTs === 'number' && isFinite(_dExt.rightTs)) {
                    _initRightTs = _dExt.rightTs;
                }
                // The initial right-edge pixel is the resolved xRight for right-edge drag arming
                if (posHit.level === 'right-edge') {
                    try {
                        // Use the resolved xRight at arm time (where the handle visually is)
                        var _xsArm = _pts.map(function(p) {
                            try {
                                var _isLog2 = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                                var v2 = (_isLog2 && p.value > 0) ? Math.log(p.value) : p.value;
                                var px2 = klineChart.convertToPixel({ timestamp: p.timestamp, value: v2 }, { paneId: _paneId });
                                return px2 ? px2.x : null;
                            } catch(e2) { return null; }
                        }).filter(function(x) { return x !== null; });
                        var _xLArm = _xsArm.length ? Math.min.apply(null, _xsArm) : 0;
                        var _xPtsMax = _xsArm.length ? Math.max.apply(null, _xsArm) : 0;
                        _initRightX = _positionXRight(_xLArm, _xPtsMax, _dExt, _paneId, _pts[0] ? _pts[0].value : 0);
                    } catch(e) {}
                }
            }
            // Record origEntryTs so the right-edge clamp works even when live overlay
            // points only carry dataIndex (not timestamp).
            var _origEntryTs = (posHit.drawing && posHit.drawing.points && posHit.drawing.points[0])
                ? posHit.drawing.points[0].timestamp : null;
            _positionLevelDrag = {
                overlayId: posHit.overlayId,
                level: posHit.level,
                paneId: _paneId,
                origValue: _hitValue,
                clickOffset: _clickOffset,
                currentValue: null,
                startX: localX,
                initPixelXs: _initPixelXs,
                initRightX: _initRightX,
                initRightTs: _initRightTs,
                origEntryTs: _origEntryTs
            };
            // Full-width placement guidelines while dragging (read by the painter in chart-position-overlays.js)
            window._positionGuidelinesFor = _positionLevelDrag.overlayId;
            var _isEdge = posHit.level === 'right-edge' || posHit.level === 'left-edge';
            var _dragCursor = _isEdge ? 'ew-resize' : (posHit.level === 'body' ? 'move' : 'ns-resize');
            _forceCanvasCursor(container, _dragCursor);
            return;
        }
        // Clicked empty space — deselect any selected overlay
        if (klineSelectedOverlayId && !hit) {
            var prevSel = klineSelectedOverlayId;
            klineSelectedOverlayId = null;
            // Force re-render to remove selection handles instantly
            try {
                var ovs = klineChart.getOverlayById(prevSel);
                var ov = Array.isArray(ovs) ? ovs[0] : ovs;
                if (ov) {
                    var ext = Object.assign({}, ov.extendData || {});
                    ext._hoverTick = Date.now();
                    klineChart.overrideOverlay({ id: prevSel, extendData: ext });
                }
            } catch (e) {}
        }

        // Task 3 — Ctrl+marquee selection: arm when Ctrl is held but nothing
        // interactive was hit. Must be LAST in the dispatcher so all shape branches
        // have already returned. The existing multi-select-clear pre-check only runs
        // for !e.ctrlKey so this branch is reached on Ctrl+click on empty space.
        if (e.ctrlKey) {
            e.preventDefault();
            e.stopPropagation();
            _marqueeState = { startX: localX, startY: localY, curX: localX, curY: localY, active: false };
        }
    }, true); // capture phase

    // Mousemove — hover detection + drag
    container.addEventListener('mousemove', function(e) {
        // Magnet native-tool hooks read the live Ctrl state from here (the
        // engine's perform* callbacks don't carry the event)
        _magnetCtrlHeld = e.ctrlKey;
        // KLineChart only resets cursors during ITS mousemove handler; re-asserting
        // here on every move wins without polling.
        if (_drawingToolActive) { _forceCanvasCursor(container, DRAWING_CURSOR); return; }
        // Ctrl+marquee rubber-band in progress
        if (_marqueeState) {
            _handleMarquee(e.clientX, e.clientY, container);
            return;
        }
        // (no getBoundingClientRect here — the rAF hover pass computes local
        // coords once per frame; per-event forced layout was crosshair-lag fuel)

        // multi-select group drag takes priority over all other drags.
        // dispatch/priority order + the per-event `return` (which
        // suppresses the hover pass + native pan while a drag is live) are
        // UNCHANGED — only the heavy handler call is deferred to one rAF flush
        // per frame via _scheduleDragRaf (see definition above _initOverlayHoverDetection).
        if (_msDrag) {
            _scheduleDragRaf(container, 'ms', e.clientX, e.clientY, e.ctrlKey);
            return;
        }
        if (_ctrlCloneDrag) {
            _scheduleDragRaf(container, 'ctrlClone', e.clientX, e.clientY, e.ctrlKey);
            return;
        }
        if (_rectResizeState) {
            _scheduleDragRaf(container, 'rectResize', e.clientX, e.clientY, e.ctrlKey);
            return;
        }
        if (_channelCornerDrag) {
            _scheduleDragRaf(container, 'channelCorner', e.clientX, e.clientY, e.ctrlKey);
            return;
        }
        if (_frvpEdgeDrag) {
            _scheduleDragRaf(container, 'frvpEdge', e.clientX, e.clientY, e.ctrlKey);
            return;
        }
        if (_positionLevelDrag) {
            _scheduleDragRaf(container, 'positionLevel', e.clientX, e.clientY, e.ctrlKey);
            return;
        }

        // Coalesce the hover/hit/cursor pass to one run per animation frame
        container.__hoverEvt = { x: e.clientX, y: e.clientY, alt: e.altKey };
        if (!container.__hoverRaf && typeof requestAnimationFrame === 'function') {
            container.__hoverRaf = requestAnimationFrame(function() {
                container.__hoverRaf = null;
                var _he = container.__hoverEvt;
                if (_he) _hoverCursorPass(_he.x, _he.y, _he.alt, container);
            });
        }
    });

    // Mouseup — finalize drags. Window-bound (a drag can end outside the
    // container), so a recreated container would otherwise add a second
    // listener that stacks forever — remove the previous instance before
    // adding this one, so exactly one stays live, closed over the current
    // container.
    if (_winMouseupHandler) window.removeEventListener('mouseup', _winMouseupHandler);
    _winMouseupHandler = function(e) {
        // A coalesced drag repaint from the last mousemove may
        // still be pending (its rAF hasn't fired yet) — flush it SYNCHRONOUSLY
        // with its stored payload before any finalize* below runs, otherwise
        // the drop position (finalize* reads the overlay's CURRENT points)
        // would be one frame stale. Cancel the pending rAF so it can't also
        // fire and re-run the same branch a second time after drop.
        if (container.__dragEvt) {
            if (container.__dragRaf && typeof cancelAnimationFrame === 'function') {
                cancelAnimationFrame(container.__dragRaf);
            }
            container.__dragRaf = null;
            _flushDragRaf(container);
        }
        // Ctrl+marquee rubber-band: resolve the selection
        if (_marqueeState) { _finalizeMarquee(); return; }
        // finalize multi-select group drag
        if (_msDrag) { _finalizeMsDrag(); return; }
        // resolve position Ctrl+click arm
        if (_msPosClickArm) {
            var _arm = _msPosClickArm;
            _msPosClickArm = null;
            var _armMoved = Math.abs(e.clientX - _arm.downX) > 3 || Math.abs(e.clientY - _arm.downY) > 3;
            if (!_armMoved) { _msToggle(_arm.id); }
            return;
        }
        if (_ctrlCloneDrag) _finalizeCtrlCloneDrag();
        if (_rectResizeState) _finalizeRectResize();
        if (_channelCornerDrag) _finalizeChannelCornerDrag();
        if (_frvpEdgeDrag) _finalizeFrvpEdgeDrag();
        if (_positionLevelDrag) _finalizePositionLevelDrag();
        // Click-to-cycle through overlapping position bodies (armed on mousedown)
        if (_posBodyClickCycle) {
            var _cyc = _posBodyClickCycle;
            _posBodyClickCycle = null;
            var _moved = Math.abs(e.clientX - _cyc.downX) > 3 || Math.abs(e.clientY - _cyc.downY) > 3;
            if (!_moved) {
                var _prevSel = klineSelectedOverlayId;
                var _nextSel = _nextInCycleStack(_cyc.stack, _prevSel);
                if (_nextSel && _nextSel !== _prevSel) {
                    klineSelectedOverlayId = _nextSel;
                    if (_prevSel) _touchPositionOverlay(_prevSel);
                    _touchPositionOverlay(_nextSel);
                }
            }
        }
        // Click-to-cycle through overlapping rect bodies: advance from the
        // selection as it was BEFORE this click (native onSelected re-selects
        // its own pick on the way), then raise the cycled rect via the normal
        // recreate path — so the next press drags IT natively.
        if (_rectBodyClickCycle) {
            var _rcyc = _rectBodyClickCycle;
            _rectBodyClickCycle = null;
            var _rmoved = Math.abs(e.clientX - _rcyc.downX) > 3 || Math.abs(e.clientY - _rcyc.downY) > 3;
            if (!_rmoved) {
                var _rnext = _nextInCycleStack(_rcyc.stack, _rcyc.prevSelected);
                if (_rnext && _rnext !== _rcyc.prevSelected) {
                    var _rd = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(_rnext) : null;
                    klineSelectedOverlayId = _rnext;
                    // bump zOrder so cycled rect stays on top persistently
                    if (_rd) {
                        _rd.zOrder = _maxZOrder() + 1;
                        if (typeof saveKlineDrawing === 'function') saveKlineDrawing(_rd);
                    }
                    if (_rd && typeof recreateOverlayWithStyles === 'function') {
                        recreateOverlayWithStyles(_rd); // raises + selects + persists
                    } else {
                        _touchRectOverlay(_rcyc.prevSelected);
                        _touchRectOverlay(_rnext);
                    }
                }
            }
        }
    };
    window.addEventListener('mouseup', _winMouseupHandler);

    // Mouseleave — clear hover (also clears channel hover)
    container.addEventListener('mouseleave', function() {
        if (!_rectResizeState && !_channelCornerDrag && !_frvpEdgeDrag && !_positionLevelDrag && !_ctrlCloneDrag) {
            _setRectHovered(null);
            _setPositionHovered(null);
            _setChannelHovered(null);
            _clearCanvasCursor(container);
        }
    });
}

// ---- Sub-pane drawing support ----
// Tools that must always draw on candle_pane (price-based tools)
var CANDLE_ONLY_TOOLS = ['position', 'longPosition', 'shortPosition', 'spot', 'alertLine', 'alertLevel', 'measureTool', 'boundedChannel', 'parallelChannel', 'anchoredVwap', 'path', 'elliottImpulse', 'elliottCorrective', 'arrow', 'pitchfork', 'frvp', 'volumeProfileRange'];

// Default heights for indicator panes (restored after expand)
var PANE_DEFAULT_HEIGHTS = {
    'volume_pane': 100, 'rsi_pane': 90, 'macd_pane': 100,
    'cvd_spot_pane': 90, 'oi_pane': 90, 'cvd_usdt_pane': 90,
    'cvd_coin_pane': 90, 'deriv_combined_pane': 120
};

// State for manual fullscreen pane toggle
var _fullscreenPaneId = null;  // Currently fullscreen sub-pane, or null

// Get list of currently active pane IDs
function getActivePaneIds() {
    var panes = ['candle_pane'];
    if (typeof klineActiveIndicators !== 'undefined') {
        if (klineActiveIndicators.VOL) panes.push('volume_pane');
        if (klineActiveIndicators.RSI) panes.push('rsi_pane');
        if (klineActiveIndicators.MACD) panes.push('macd_pane');
        if (klineActiveIndicators.CVD_SPOT) panes.push('cvd_spot_pane');
        if (klineActiveIndicators.OI) panes.push('oi_pane');
        if (klineActiveIndicators.CVD_USDT) panes.push('cvd_usdt_pane');
        if (klineActiveIndicators.CVD_COIN) panes.push('cvd_coin_pane');
        if (klineActiveIndicators.DERIV_COMBINED) panes.push('deriv_combined_pane');
    }
    return panes;
}

// Detect which chart pane the cursor is currently over
function detectPaneUnderCursor() {
    if (!klineChart) return 'candle_pane';
    var container = document.getElementById('kline-chart-container');
    if (!container) return 'candle_pane';
    var mx = container._lastMouseX || 0;
    var my = container._lastMouseY || 0;
    if (!mx && !my) return 'candle_pane';

    var paneIds = getActivePaneIds();
    for (var i = 0; i < paneIds.length; i++) {
        var paneId = paneIds[i];
        try {
            // KLineChart v9.8.12 getDom API — try object form then positional
            var dom = null;
            try { dom = klineChart.getDom({ paneId: paneId, position: 'main' }); } catch(e1) {}
            if (!dom) try { dom = klineChart.getDom(paneId, 'main'); } catch(e2) {}
            if (!dom) continue;
            var rect = dom.getBoundingClientRect();
            if (my >= rect.top && my <= rect.bottom && mx >= rect.left && mx <= rect.right) {
                return paneId;
            }
        } catch (e) { continue; }
    }

    return 'candle_pane';
}

// Calculate max height for an expanded pane without triggering KLineChart v9.8.12 bug.
// Bug: _measurePaneHeight uses (a = r, e = Math.max(r - a, 0)) — comma operator
// sets a = r BEFORE computing e, so any pane whose cumulative sum exceeds available
// height gets e = 0. Fix: keep target height ≤ available - collapsedPanePixels.
function _calcExpandHeight(paneId) {
    var container = document.getElementById('kline-chart-container');
    if (!container) return 600;
    var totalH = container.clientHeight;
    // X-axis pane height
    var xH = 25;
    try { var xs = klineChart.getSize('x_axis', 'root'); if (xs) xH = xs.height; } catch(e) {}
    // Separator count = active panes - 1 (each 1px)
    var panes = getActivePaneIds();
    var sepCount = Math.max(panes.length - 1, 0);
    var available = totalH - xH - sepCount;
    // Each collapsed pane takes 1px; exclude candle_pane (gets remainder) and target
    var collapsedCount = panes.length - 2;
    return Math.max(available - collapsedCount, 100);
}

// Toggle a sub-pane to fullscreen
function togglePaneFullscreen(paneId) {
    if (!klineChart) return;
    if (_fullscreenPaneId === paneId) {
        exitPaneFullscreen();
        return;
    }
    if (_fullscreenPaneId) exitPaneFullscreen();

    _fullscreenPaneId = paneId;
    // Clean up stale hover buttons
    if (_hoverBtn) { _hoverBtn.remove(); _hoverBtn = null; }
    _hoveredPaneId = null;
    var targetH = _calcExpandHeight(paneId);

    // Collapse all other indicator panes to 1px
    getActivePaneIds().forEach(function(pid) {
        if (pid === 'candle_pane' || pid === paneId) return;
        try { klineChart.setPaneOptions({ id: pid, minHeight: 0, height: 1 }); } catch(e) {}
    });
    // Expand target to fill the chart (candle_pane auto-gets remainder ≈ 0)
    try { klineChart.setPaneOptions({ id: paneId, minHeight: 0, height: targetH }); } catch(e) {}

    // Hide collapsed pane DOMs for clean visuals (no 1px slivers)
    _toggleCollapsedPanes(false, paneId);

    requestAnimationFrame(function() {
        try { klineChart.resize(); } catch(e) {}
        setTimeout(function() {
            try { klineChart.resize(); } catch(e) {}
            _showPersistentMinBtn(paneId);
        }, 100);
    });
}

// Exit fullscreen, restore all panes to defaults
function exitPaneFullscreen() {
    if (!_fullscreenPaneId) return;
    _fullscreenPaneId = null;
    if (!klineChart) return;

    // Show all pane DOMs again
    _toggleCollapsedPanes(true, null);

    // Restore indicator panes to defaults (candle_pane auto-gets remainder)
    getActivePaneIds().forEach(function(pid) {
        if (pid === 'candle_pane') return;
        var defaultH = PANE_DEFAULT_HEIGHTS[pid] || 90;
        try { klineChart.setPaneOptions({ id: pid, minHeight: 60, height: defaultH }); } catch(e) {}
    });

    _hidePaneFsBtn();

    requestAnimationFrame(function() {
        try { klineChart.resize(); } catch(e) {}
        setTimeout(function() { try { klineChart.resize(); } catch(e) {} }, 100);
    });
}

// Hide/show collapsed pane DOMs during fullscreen toggle
function _toggleCollapsedPanes(visible, expandedPaneId) {
    if (!klineChart) return;
    var display = visible ? '' : 'none';
    getActivePaneIds().forEach(function(pid) {
        if (pid === expandedPaneId) return;
        var root = null;
        try { root = klineChart.getDom(pid, 'root'); } catch(e) {}
        if (root) root.style.display = display;
    });
}

// Get the pane rect for a given paneId (main area = excludes Y-axis)
function _getPaneRect(paneId) {
    if (!klineChart) return null;
    // Use getSize API which returns { width, height, left, top }
    var size = null;
    try { size = klineChart.getSize(paneId, 'main'); } catch(e) {}
    if (size && size.height > 10 && size.width > 10) {
        // getSize returns coords relative to chart container — convert to viewport
        var container = document.getElementById('kline-chart-container');
        if (!container) return null;
        var cr = container.getBoundingClientRect();
        return {
            top: cr.top + size.top,
            bottom: cr.top + size.top + size.height,
            left: cr.left + size.left,
            right: cr.left + size.left + size.width,
            height: size.height,
            width: size.width
        };
    }
    // Fallback to getDom
    var dom = null;
    try { dom = klineChart.getDom(paneId, 'main'); } catch(e) {}
    if (!dom) try { dom = klineChart.getDom(paneId, 'root'); } catch(e) {}
    if (!dom) return null;
    var r = dom.getBoundingClientRect();
    if (r.height < 15 || r.width < 15) return null;
    return r;
}

// Overlay div above KLineChart canvases for buttons
function _getPaneBtnOverlay() {
    var existing = document.getElementById('pane-fs-overlay');
    if (existing) return existing;
    var body = document.querySelector('.adv-chart-body');
    if (!body) return null;
    var overlay = document.createElement('div');
    overlay.id = 'pane-fs-overlay';
    overlay.style.cssText = 'position:absolute; top:0; left:0; right:0; bottom:0; pointer-events:none; z-index:400;';
    body.appendChild(overlay);
    return overlay;
}

// Pane ID → indicator key mapping (for close button)
var PANE_TO_INDICATOR = {
    'volume_pane': 'VOL', 'rsi_pane': 'RSI', 'macd_pane': 'MACD',
    'cvd_spot_pane': 'CVD_SPOT', 'oi_pane': 'OI', 'cvd_usdt_pane': 'CVD_USDT',
    'cvd_coin_pane': 'CVD_COIN', 'deriv_combined_pane': 'DERIV_COMBINED'
};

// Shared button style helper
function _paneBtnStyle(top, right) {
    return 'position:absolute; top:' + top + 'px; right:' + right + 'px; ' +
        'width:22px; height:22px; display:flex; align-items:center; justify-content:center; ' +
        'background:rgba(22,27,34,0.85); color:#8b949e; border:1px solid #30363d; border-radius:3px; ' +
        'cursor:pointer; pointer-events:auto; user-select:none; ' +
        'transition:color 0.15s,border-color 0.15s,background 0.15s;';
}

function _addBtnHover(btn, hoverColor, hoverBg) {
    btn.addEventListener('mouseenter', function() {
        btn.style.color = hoverColor || '#e6edf3'; btn.style.borderColor = '#58a6ff'; btn.style.background = hoverBg || 'rgba(56,139,253,0.2)';
    });
    btn.addEventListener('mouseleave', function() {
        btn.style.color = '#8b949e'; btn.style.borderColor = '#30363d'; btn.style.background = 'rgba(22,27,34,0.85)';
    });
}

// Create pane button group (fullscreen + optional close)
function _createPaneBtns(paneId) {
    var overlay = _getPaneBtnOverlay();
    if (!overlay) return null;
    var paneRect = _getPaneRect(paneId);
    if (!paneRect) return null;
    var bodyRect = overlay.parentElement.getBoundingClientRect();

    var isFs = (_fullscreenPaneId === paneId);
    // Position: push minimize button down 36px in fullscreen to avoid toolbar overlap
    var topOffset = isFs ? 40 : 4;
    var top = paneRect.top - bodyRect.top + topOffset;
    var right = bodyRect.right - paneRect.right + 6;

    var wrap = document.createElement('div');
    wrap.className = 'pane-btn-group';
    wrap.style.cssText = 'position:absolute; top:' + top + 'px; right:' + right + 'px; display:flex; gap:2px; pointer-events:auto;';

    // Fullscreen / minimize button
    var fsBtn = document.createElement('div');
    fsBtn.className = 'pane-fullscreen-btn';
    if (isFs) {
        fsBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 1H2a1 1 0 00-1 1v2M8 1h2a1 1 0 011 1v2M11 8v2a1 1 0 01-1 1H8M1 8v2a1 1 0 001 1h2"/><rect x="3.5" y="3.5" width="5" height="5" rx="0.5"/></svg>';
        fsBtn.title = 'Minimize pane (ESC)';
    } else {
        fsBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 5V2.5A.5.5 0 012.5 2H5M7 2h2.5a.5.5 0 01.5.5V5M10 7v2.5a.5.5 0 01-.5.5H7M5 10H2.5a.5.5 0 01-.5-.5V7"/></svg>';
        fsBtn.title = 'Fullscreen pane';
    }
    fsBtn.style.cssText = _paneBtnStyle(0, 0).replace(/position:absolute; top:\d+px; right:\d+px; /, 'position:relative; ');
    fsBtn.addEventListener('mousedown', function(e) { e.stopPropagation(); e.preventDefault(); });
    fsBtn.addEventListener('click', function(e) {
        e.stopPropagation(); e.preventDefault();
        togglePaneFullscreen(paneId);
    });
    _addBtnHover(fsBtn);
    wrap.appendChild(fsBtn);

    // Close (remove indicator) button — only in normal mode, not fullscreen
    if (!isFs && PANE_TO_INDICATOR[paneId]) {
        var closeBtn = document.createElement('div');
        closeBtn.className = 'pane-close-btn';
        closeBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M2 2l6 6M8 2l-6 6"/></svg>';
        closeBtn.title = 'Remove indicator';
        closeBtn.style.cssText = _paneBtnStyle(0, 0).replace(/position:absolute; top:\d+px; right:\d+px; /, 'position:relative; ');
        closeBtn.addEventListener('mousedown', function(e) { e.stopPropagation(); e.preventDefault(); });
        closeBtn.addEventListener('click', function(e) {
            e.stopPropagation(); e.preventDefault();
            var ind = PANE_TO_INDICATOR[paneId];
            if (ind && typeof toggleKlineIndicator === 'function') {
                toggleKlineIndicator(ind);
            }
        });
        _addBtnHover(closeBtn, '#f85149', 'rgba(248,81,73,0.15)');
        wrap.appendChild(closeBtn);
    }

    overlay.appendChild(wrap);
    return wrap;
}

// Persistent minimize button (always visible when fullscreen)
var _visiblePaneFsBtn = null;
function _showPersistentMinBtn(paneId) {
    _hidePaneFsBtn();
    _visiblePaneFsBtn = _createPaneBtns(paneId);
}

function _hidePaneFsBtn() {
    if (_visiblePaneFsBtn) { _visiblePaneFsBtn.remove(); _visiblePaneFsBtn = null; }
}

// Hover detection: show expand button when mouse hovers a sub-pane (only when NOT fullscreen)
var _hoveredPaneId = null;
var _hoverBtn = null;
function _onChartMouseMove(e) {
    if (!klineChart) return;
    // When fullscreen, the persistent minimize button handles it
    if (_fullscreenPaneId) return;

    var paneIds = getActivePaneIds();
    var found = null;
    for (var i = 0; i < paneIds.length; i++) {
        var pid = paneIds[i];
        if (pid === 'candle_pane') continue;
        var rect = _getPaneRect(pid);
        if (!rect) continue;
        if (e.clientY >= rect.top && e.clientY <= rect.bottom && e.clientX >= rect.left && e.clientX <= rect.right) {
            found = pid;
            break;
        }
    }
    if (found !== _hoveredPaneId) {
        _hoveredPaneId = found;
        if (_hoverBtn) { _hoverBtn.remove(); _hoverBtn = null; }
        if (found) {
            _hoverBtn = _createPaneBtns(found);
        }
    }
}

function _onChartMouseLeave() {
    if (_fullscreenPaneId) return; // keep persistent min button
    _hoveredPaneId = null;
    setTimeout(function() {
        if (!_hoveredPaneId && _hoverBtn) { _hoverBtn.remove(); _hoverBtn = null; }
    }, 150);
}

// Set up hover listeners (called once after chart init)
function updatePaneFullscreenButtons() {
    var body = document.querySelector('.adv-chart-body');
    if (!body || body._paneFsSetup) return;
    body._paneFsSetup = true;
    body.addEventListener('mousemove', _onChartMouseMove);
    body.addEventListener('mouseleave', _onChartMouseLeave);
}

function handleKlineToolClick(tool, button, allButtons, event) {
    if (tool === 'alert') {
        toggleDrawer('alerts');
        return;
    }

    if (tool === 'undo') {
        undoKlineDrawings();
        return;
    }

    if (tool === 'redo') {
        redoKlineDrawings();
        return;
    }

    if (tool === 'clear') {
        // Require shift key or confirmation to clear all
        var doClear = event && event.shiftKey;
        if (!doClear) {
            doClear = confirm('Clear all drawings? (Hold Shift+Click to skip this)');
        }

        if (doClear && klineChart) {
            // Clean up any promoted spot trades before the whole bucket is
            // wiped — otherwise this "Clear" button orphans every one of
            // them at once, silently . The Shift+Delete / Shift+D handlers in
            // chart-keyboard.js do the same; this button reaches storage through a
            // different path (_removeDrawingOverlays + clearKlineDrawingsFromStorage,
            // not handleContextMenuAction), so it needs its own call.
            if (typeof _cleanupPromotedSpotTradesForSymbol === 'function') {
                _cleanupPromotedSpotTradesForSymbol(klineChartSymbol);
            }
            _removeDrawingOverlays();
            clearKlineDrawingsFromStorage();
        }
        return;
    }

    // magnet mode toggle — cycle off→weak→strong→off.
    // Must be BEFORE _posActiveDisarm and button-class clearing so neither
    // disarms armed tools nor clears active-class state on other buttons.
    if (tool === 'magnet') {
        var _modes = ['off', 'weak', 'strong'];
        var _mIdx = _modes.indexOf(_magnetMode);
        _magnetMode = _modes[(_mIdx + 1) % _modes.length];
        try { if (typeof localStorage !== 'undefined') localStorage.setItem('carracMagnetMode', _magnetMode); } catch (e) {}
        _updateMagnetButton();
        if (typeof showToast === 'function') {
            showToast(_magnetMode === 'off' ? 'Magnet off'
                : _magnetMode === 'weak' ? 'Magnet: weak — snaps near OHLC'
                : 'Magnet: strong — always snaps to OHLC');
        }
        return;
    }

    // Deactivate freehand if switching to a different tool
    if (tool !== 'freehand' && typeof _freehandActive !== 'undefined' && _freehandActive) {
        freehandDeactivate();
    }

    // Disarm a pending one-click position tool before arming ANY tool — its
    // capture-phase mousedown listener stays live otherwise, and a second
    // arming stacks on top of it (one chart click then places BOTH, e.g. a
    // long AND a short). The position branch re-arms fresh below.
    if (_posActiveDisarm) {
        _posActiveDisarm();
    }
    // arming any drawing tool clears the multi-select set
    _msClear();

    // Clear ALL tool buttons, menu items, and group buttons globally
    if (klineModal) {
        klineModal.querySelectorAll('.tool-btn.active, .tool-menu-item.active, .tool-group-btn.active').forEach(function(el) {
            var t = el.getAttribute('data-tool');
            if (t === 'alert' || t === 'undo' || t === 'redo' || t === 'clear' || t === 'magnet') return;
            el.classList.remove('active');
        });
    }
    button.classList.add('active');
    // Also highlight the parent group button when a dropdown item is selected
    var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
    if (parentGroup) {
        var groupBtn = parentGroup.querySelector('.tool-group-btn');
        if (groupBtn) groupBtn.classList.add('active');
    }

    // Path / Elliott Wave: multi-click drawing with live preview
    if (tool === 'path' || tool === 'elliottImpulse' || tool === 'elliottCorrective') {
        if (!klineChart) return;
        var _mcToolName = tool;
        var _mcColor = klineDrawingColor;
        var _mcWidth = klineDrawingWidth;
        var _mcPoints = [];
        var _mcPixelPoints = []; // pixel coords for preview
        var _mcTempId = null;
        var _mcLastClick = 0;
        var container = document.getElementById('kline-chart-container');
        if (!container) return;
        _setDrawingCursor(true);
        _drawingToolActive = true;

        // Labels and max points per tool
        var _mcLabels, _mcMaxPoints;
        if (tool === 'elliottImpulse') { _mcLabels = ['', '1', '2', '3', '4', '5']; _mcMaxPoints = 6; }
        else if (tool === 'elliottCorrective') { _mcLabels = ['', 'A', 'B', 'C']; _mcMaxPoints = 4; }
        else { _mcLabels = null; _mcMaxPoints = 0; } // 0 = unlimited (path)

        var _mcOverlayName = (tool === 'path') ? 'scenarioPath' : 'elliottWave';

        // Preview canvas for rubber-band line
        var _mcCanvas = document.createElement('canvas');
        _mcCanvas.width = container.offsetWidth;
        _mcCanvas.height = container.offsetHeight;
        _mcCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;z-index:10000;pointer-events:none';
        container.appendChild(_mcCanvas);

        var hint = document.createElement('div');
        hint.id = 'multiclick-hint';
        hint.style.cssText = 'position:absolute;top:36px;left:50%;transform:translateX(-50%);z-index:10001;background:rgba(30,34,45,0.9);border:1px solid #363a45;color:#d1d4dc;padding:4px 14px;border-radius:4px;font:12px JetBrains Mono,monospace;pointer-events:none;white-space:nowrap';
        var _mcHintBase = _mcMaxPoints ? ('Click ' + _mcMaxPoints + ' points') : 'Click to add points \u2022 Double-click to finish';
        hint.textContent = _mcHintBase + ' \u2022 Esc to cancel';
        var chartContent = document.getElementById('adv-chart-content');
        if (chartContent) chartContent.appendChild(hint);

        function _mcDrawPreview(cursorX, cursorY) {
            var ctx = _mcCanvas.getContext('2d');
            ctx.clearRect(0, 0, _mcCanvas.width, _mcCanvas.height);
            if (_mcPixelPoints.length === 0) return;
            ctx.strokeStyle = _mcColor;
            ctx.lineWidth = _mcWidth;
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.setLineDash([]);
            // Draw placed segments
            ctx.beginPath();
            ctx.moveTo(_mcPixelPoints[0].x, _mcPixelPoints[0].y);
            for (var i = 1; i < _mcPixelPoints.length; i++) ctx.lineTo(_mcPixelPoints[i].x, _mcPixelPoints[i].y);
            ctx.stroke();
            // Rubber-band line to cursor
            if (cursorX != null) {
                ctx.setLineDash([4, 4]);
                ctx.globalAlpha = 0.5;
                ctx.beginPath();
                var last = _mcPixelPoints[_mcPixelPoints.length - 1];
                ctx.moveTo(last.x, last.y);
                ctx.lineTo(cursorX, cursorY);
                ctx.stroke();
                ctx.globalAlpha = 1;
                ctx.setLineDash([]);
            }
            // Draw point dots
            for (var j = 0; j < _mcPixelPoints.length; j++) {
                ctx.fillStyle = _mcColor;
                ctx.beginPath();
                ctx.arc(_mcPixelPoints[j].x, _mcPixelPoints[j].y, 4, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        function _mcUpdateOverlay() {
            if (_mcTempId) try { klineChart.removeOverlay(_mcTempId); } catch(e) {}
            if (_mcPoints.length < 2) { _mcTempId = null; return; }
            var ext = { _color: _mcColor, _width: _mcWidth };
            if (_mcLabels) ext._labels = _mcLabels.slice(0, _mcPoints.length);
            _mcTempId = klineChart.createOverlay({
                name: _mcOverlayName,
                lock: true,
                points: _mcPoints.map(function(p) { return { timestamp: p.timestamp, value: p.value }; }),
                extendData: ext
            });
        }

        function _mcFinalize() {
            if (_mcPoints.length >= 2) {
                if (_mcTempId) try { klineChart.removeOverlay(_mcTempId); } catch(e) {}
                var ext = { _color: _mcColor, _width: _mcWidth };
                if (_mcLabels) ext._labels = _mcLabels.slice(0, _mcPoints.length);
                var callbacks = getOverlayCallbacks();
                var newId = klineChart.createOverlay(Object.assign({
                    name: _mcOverlayName,
                    points: _mcPoints.map(function(p) { return { timestamp: p.timestamp, value: p.value }; }),
                    styles: { line: { color: _mcColor, size: _mcWidth }, point: _invisiblePointStyle },
                    extendData: ext
                }, callbacks), 'candle_pane');
                if (newId) {
                    saveKlineDrawing({
                        id: newId, name: _mcOverlayName,
                        points: _mcPoints.map(function(p) { return { timestamp: p.timestamp, value: p.value }; }),
                        color: _mcColor, width: _mcWidth, extendData: ext
                    });
                }
            }
            _mcCleanup();
        }

        function _mcCleanup() {
            container.removeEventListener('mousedown', _mcMouseDown, true);
            container.removeEventListener('mousemove', _mcMouseMove, true);
            document.removeEventListener('keydown', _mcKeyDown);
            if (_mcTempId && _mcPoints.length < 2) try { klineChart.removeOverlay(_mcTempId); } catch(e) {}
            _mcCanvas.remove();
            hint.remove();
            _setDrawingCursor(false);
            _drawingToolActive = false;
            container.style.cursor = '';
            button.classList.remove('active');
            var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
            if (parentGroup) {
                var groupBtn = parentGroup.querySelector('.tool-group-btn');
                if (groupBtn) groupBtn.classList.remove('active');
            }
        }

        function _mcMouseDown(e) {
            if (e.button !== 0) return;
            e.stopPropagation();
            e.preventDefault();
            var rect = container.getBoundingClientRect();
            var localX = e.clientX - rect.left, localY = e.clientY - rect.top;
            var coord = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: 'candle_pane' });
            if (!coord || coord.dataIndex == null || coord.value == null) return;
            var dataList = klineChart.getDataList();
            var ts = (coord.dataIndex >= 0 && coord.dataIndex < dataList.length) ? dataList[coord.dataIndex].timestamp : Date.now();
            var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
            var rawVal = (_isLog && coord.value != null) ? Math.exp(coord.value) : coord.value;
            // Double-click detection (path only — elliott has fixed points)
            var now = Date.now();
            if (!_mcMaxPoints && now - _mcLastClick < 400 && _mcPoints.length >= 2) {
                _mcFinalize();
                return;
            }
            _mcLastClick = now;
            _mcPoints.push({ timestamp: ts, value: rawVal });
            _mcPixelPoints.push({ x: localX, y: localY });
            _mcUpdateOverlay();
            _mcDrawPreview(null, null);
            // Auto-finalize when max points reached (Elliott)
            if (_mcMaxPoints && _mcPoints.length >= _mcMaxPoints) {
                _mcFinalize();
                return;
            }
            var remaining = _mcMaxPoints ? (' (' + (_mcMaxPoints - _mcPoints.length) + ' remaining)') : '';
            hint.textContent = _mcPoints.length + ' points' + remaining + ' \u2022 Esc to cancel';
        }

        function _mcMouseMove(e) {
            var rect = container.getBoundingClientRect();
            _mcDrawPreview(e.clientX - rect.left, e.clientY - rect.top);
        }

        function _mcKeyDown(e) {
            if (e.key === 'Escape') { _mcCleanup(); return; }
            if (e.key === 'Enter' && _mcPoints.length >= 2) { _mcFinalize(); return; }
        }

        container.addEventListener('mousedown', _mcMouseDown, true);
        container.addEventListener('mousemove', _mcMouseMove, true);
        document.addEventListener('keydown', _mcKeyDown);
        return;
    }

    // Volume Profile Range: standard 2-point overlay ('frvp') — falls through
    // to the generic createOverlay path below (mapped near parallelChannel).

    // Freehand drawing: capture mouse/touch strokes
    if (tool === 'freehand') {
        freehandActivate();
        return;
    }

    // Anchored VWAP: single click to place anchor
    if (tool === 'anchoredVwap') {
        if (!klineChart) return;
        var vwapColor = '#2196F3';
        var callbacks = getOverlayCallbacks();
        _setDrawingCursor(true);
        klineChart.createOverlay(Object.assign({
            name: 'anchoredVwap',
            extendData: { _color: vwapColor, _width: 2.5 },
            styles: {
                line: { color: vwapColor, size: 2.5 },
                point: _invisiblePointStyle
            },
            onDrawEnd: function(event) {
                _setDrawingCursor(false);
                button.classList.remove('active');
                var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
                if (parentGroup) {
                    var groupBtn = parentGroup.querySelector('.tool-group-btn');
                    if (groupBtn) groupBtn.classList.remove('active');
                }
                if (event && event.overlay) {
                    saveKlineDrawing({
                        id: event.overlay.id,
                        name: 'anchoredVwap',
                        points: resolvePointTimestamps(event.overlay.points),
                        color: vwapColor,
                        extendData: { _color: vwapColor, _width: 2.5 }
                    });
                }
            }
        }, callbacks), 'candle_pane');
        return;
    }

    // Alert tools: alertLine = 2-click bounded segment; alertLevel = one-click
    // infinite level. Both create a server alert at the placed price.
    if (tool === 'alertLine' || tool === 'alertLevel') {
        if (!klineChart) return;
        var alertColor = klineDrawingColor;
        var callbacks = getOverlayCallbacks();
        _setDrawingCursor(true);
        klineChart.createOverlay(Object.assign({
            name: tool,
            extendData: { _color: alertColor },
            styles: {
                line: { color: alertColor, size: 1.5 },
                point: _invisiblePointStyle
            },
            onDrawEnd: function(event) {
                _setDrawingCursor(false);
                button.classList.remove('active');
                var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
                if (parentGroup) {
                    var groupBtn = parentGroup.querySelector('.tool-group-btn');
                    if (groupBtn) groupBtn.classList.remove('active');
                }
                if (event && event.overlay) {
                    var pts = event.overlay.points;
                    var price = pts[0].value;
                    // In log mode, KLineChart returns ln(price) — convert back for server alert
                    if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled) price = Math.exp(price);
                    var currentPrice = allAssets[klineChartSymbol] ? allAssets[klineChartSymbol].latest_price : 0;
                    var direction = (price >= currentPrice) ? 'above' : 'below';
                    // Normalize both points to same price (horizontal line)
                    var resolvedPts = resolvePointTimestamps(pts);
                    resolvedPts.forEach(function(p) { p.value = price; });
                    // Create server alert
                    fetch('/api/alerts', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                        credentials: 'same-origin',
                        body: JSON.stringify({ symbol: klineChartSymbol, condition: direction, price: price, note: '[drawn]', frequency: 'once_per_bar' })
                    })
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        updateAlertBadge();
                        // keep the created alert's id on the
                        // drawing — without it, deleting the line orphans the
                        // server alert and drag-sync/Remove Alert can't work.
                        var _aid = data && data.alert && data.alert.id;
                        if (_aid) {
                            var _ad = getKlineDrawingById(event.overlay.id);
                            if (_ad) {
                                _ad.extendData = _ad.extendData || {};
                                _ad.extendData._alertId = _aid;
                                saveKlineDrawing(_ad);
                                try { klineChart.overrideOverlay({ id: event.overlay.id, extendData: Object.assign({}, _ad.extendData) }); } catch (e2) {}
                            }
                        }
                    })
                    .catch(function(e) { console.error('Failed to create alert:', e); });
                    // Save as drawing with normalized points
                    saveKlineDrawing({
                        id: event.overlay.id,
                        name: tool,
                        points: resolvedPts,
                        color: alertColor,
                        extendData: { _color: alertColor }
                    });
                }
            }
        }, callbacks));
        return;
    }

    // Spot buy: single click to place entry, then dialog for USD amount + label
    if (tool === 'spot') {
        if (!klineChart) return;
        var spotColor = '#4db6ac';
        var callbacks = getOverlayCallbacks();
        _setDrawingCursor(true);
        klineChart.createOverlay(Object.assign({
            name: 'spot',
            extendData: {},
            styles: {
                line: { color: spotColor, size: 1 },
                point: _invisiblePointStyle
            },
            onDrawEnd: function(event) {
                _setDrawingCursor(false);
                // Deactivate tool button
                try { document.querySelectorAll('.tool-btn.active[data-tool="spot"]').forEach(function(el) { el.classList.remove('active'); }); } catch(ex) {}
                if (event && event.overlay) {
                    var overlay = event.overlay;
                    var entryPrice = overlay.points[0].value;
                    // In log mode, convert back for dialog display
                    if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled) {
                        entryPrice = Math.exp(entryPrice);
                    }
                    // Lock to prevent accidental drags
                    klineChart.overrideOverlay({ id: overlay.id, lock: true });
                    showSpotBuyDialog(entryPrice, function(price, usd, qty, label) {
                        if (price === null) {
                            // Cancelled — remove overlay
                            try { klineChart.removeOverlay(overlay.id); } catch(ex2) {}
                            return;
                        }
                        // promote this buy into the journal so it
                        // is not invisible to /portfolio. klinecharts' own overlay
                        // id (e.g. "overlay_<ts>_<n>") is not a positive integer, and
                        // TradeCreateRequest.drawing_id requires one — so mint a
                        // numeric correlation id here and carry it on extendData for
                        // the sell path (selectPromotedTradeId) to find later.
                        var journalDrawingId = Date.now();
                        var ext = { usdAmount: usd, quantity: qty, label: label, _journalDrawingId: journalDrawingId };
                        klineChart.overrideOverlay({ id: overlay.id, extendData: ext });
                        saveKlineDrawing({
                            id: overlay.id,
                            name: 'spot',
                            points: resolvePointTimestamps(overlay.points),
                            color: spotColor,
                            extendData: ext
                        });
                        if (typeof _promoteSpotToJournal === 'function') {
                            _promoteSpotToJournal(klineChartSymbol, price, qty, journalDrawingId);
                        }
                        // Update drawer if visible
                        if (typeof updateDrawerPosition === 'function') updateDrawerPosition();
                    });
                }
            }
        }, callbacks), 'candle_pane');
        return;
    }

    // Spot Sell: pick an open spot to sell
    if (tool === 'spotSell') {
        _setDrawingCursor(false);
        try { document.querySelectorAll('.tool-btn.active[data-tool="spotSell"]').forEach(function(el) { el.classList.remove('active'); }); } catch(ex) {}
        if (typeof showSpotSellPicker === 'function') {
            showSpotSellPicker();
        }
        return;
    }

    // Text annotation: prompt for text and font size
    if (tool === 'textAnnotation') {
        showTextAnnotationDialog(function(text, fontSize, color, fmt) {
            if (!text || !klineChart) return;
            var extData = { text: text, fontSize: fontSize, color: color, bold: fmt.bold, italic: fmt.italic, underline: fmt.underline, strikethrough: fmt.strikethrough };
            var callbacks = getOverlayCallbacks();
            _setDrawingCursor(true);
            klineChart.createOverlay(Object.assign({
                name: 'textAnnotation',
                extendData: extData,
                styles: { line: { color: color } },
                onDrawEnd: function(event) {
                    _setDrawingCursor(false);
                    button.classList.remove('active');
                    var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
                    if (parentGroup) {
                        var groupBtn = parentGroup.querySelector('.tool-group-btn');
                        if (groupBtn) groupBtn.classList.remove('active');
                    }
                    if (event && event.overlay) {
                        saveKlineDrawing({
                            id: event.overlay.id,
                            name: 'textAnnotation',
                            points: resolvePointTimestamps(event.overlay.points),
                            color: color,
                            extendData: extData
                        });
                    }
                }
            }, callbacks));
        });
        return;
    }

    // single-click annotation glyph stamps (arrow up/down, flag,
    // circle) — fixed v1 set, no emoji picker. One click places the glyph at
    // the cursor; no dialog. Unlike 'spot' (a real trade marker, deliberately
    // locked after placement) these stay UNLOCKED so they're draggable
    // afterwards.
    if (tool === 'arrowUpMarker' || tool === 'arrowDownMarker' || tool === 'flagPin' || tool === 'circleMarker') {
        if (!klineChart) return;
        var _markerColor = klineDrawingColor;
        var _markerWidth = klineDrawingWidth;
        var _markerCallbacks = getOverlayCallbacks();
        _setDrawingCursor(true);
        klineChart.createOverlay(Object.assign({
            name: tool,
            extendData: { _color: _markerColor, _width: _markerWidth },
            styles: {
                line: { color: _markerColor, size: _markerWidth },
                point: _invisiblePointStyle
            },
            onDrawEnd: function(event) {
                _setDrawingCursor(false);
                button.classList.remove('active');
                var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
                if (parentGroup) {
                    var groupBtn = parentGroup.querySelector('.tool-group-btn');
                    if (groupBtn) groupBtn.classList.remove('active');
                }
                if (event && event.overlay) {
                    saveKlineDrawing({
                        id: event.overlay.id,
                        name: tool,
                        points: resolvePointTimestamps(event.overlay.points),
                        color: _markerColor,
                        width: _markerWidth,
                        extendData: { _color: _markerColor, _width: _markerWidth }
                    });
                }
            }
        }, _markerCallbacks), 'candle_pane');
        return;
    }

    // Callout: prompt for text (reuses the textAnnotation dialog), then a
    // single click places the anchor. The text box renders at a fixed pixel
    // offset from the anchor (extendData.offsetX/offsetY, default up-right)
    // connected by a leader line — reposition via right-click → "Text
    // Position" (same preset picker as rect text), not a continuous drag.
    if (tool === 'callout') {
        showTextAnnotationDialog(function(text, fontSize, color, fmt) {
            if (!text || !klineChart) return;
            var extData = { text: text, fontSize: fontSize, _color: color, bold: fmt.bold, italic: fmt.italic, underline: fmt.underline, strikethrough: fmt.strikethrough, offsetX: 60, offsetY: -60 };
            var callbacks = getOverlayCallbacks();
            _setDrawingCursor(true);
            klineChart.createOverlay(Object.assign({
                name: 'callout',
                extendData: extData,
                styles: { line: { color: color }, point: _invisiblePointStyle },
                onDrawEnd: function(event) {
                    _setDrawingCursor(false);
                    button.classList.remove('active');
                    var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
                    if (parentGroup) {
                        var groupBtn = parentGroup.querySelector('.tool-group-btn');
                        if (groupBtn) groupBtn.classList.remove('active');
                    }
                    if (event && event.overlay) {
                        saveKlineDrawing({
                            id: event.overlay.id,
                            name: 'callout',
                            points: resolvePointTimestamps(event.overlay.points),
                            color: color,
                            extendData: extData
                        });
                    }
                }
            }, callbacks), 'candle_pane');
        });
        return;
    }

    // One-click position placement (Long / Short)
    if ((tool === 'longPosition' || tool === 'shortPosition') && klineChart) {
        var _posDir = (tool === 'longPosition') ? 'long' : 'short';
        var _posColor = (_posDir === 'long') ? '#26a69a' : '#ef5350';
        var _posCallbacks = getOverlayCallbacks();
        var _posContainer = document.getElementById('kline-chart-container');
        if (!_posContainer) { button.classList.remove('active'); return; }
        _setDrawingCursor(true);
        _drawingToolActive = true;

        // ATR-based SL/TP derivation — shared by the placement click AND the
        // armed-tool guideline preview (they must always agree).
        function _posDeriveSlTp(entry, dir, atr) {
            if (atr && atr > 0) {
                return dir === 'long'
                    ? { sl: Math.max(entry - 1.5 * atr, 0.0000001), tp: entry + 3 * atr }
                    : { sl: entry + 1.5 * atr, tp: Math.max(entry - 3 * atr, 0.0000001) };
            }
            // Fallback: 2% SL, 4% TP
            return dir === 'long'
                ? { sl: Math.max(entry * 0.98, 0.0000001), tp: entry * 1.04 }
                : { sl: entry * 1.02, tp: Math.max(entry * 0.96, 0.0000001) };
        }

        function _posCleanup() {
            _posContainer.removeEventListener('mousedown', _posMouseDown, true);
            _posContainer.removeEventListener('touchend', _posTouchEnd, true);
            _posContainer.removeEventListener('mousemove', _posPreviewMove);
            _posContainer.removeEventListener('mouseleave', _posPreviewLeave);
            if (_gRaf) { cancelAnimationFrame(_gRaf); _gRaf = null; }
            if (_posGuides && _posGuides.parentNode) _posGuides.remove();
            document.removeEventListener('keydown', _posKeyDown);
            _setDrawingCursor(false);
            _drawingToolActive = false;
            button.classList.remove('active');
            if (_posActiveDisarm === _posCleanup) _posActiveDisarm = null;
        }

        // Placement preview: full-width entry/SL/TP guidelines with prices
        // while the tool is armed (mouse only — touch places on tap).
        var _posGuides = document.createElement('div');
        _posGuides.id = 'pos-place-guides';
        _posGuides.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:9999;display:none';
        var _gDefs = [
            { key: 'entry', color: _posColor, label: _posDir === 'long' ? 'LONG' : 'SHORT' },
            { key: 'sl', color: '#AB47BC', label: 'SL' },
            { key: 'tp', color: '#FFD700', label: 'TP' }
        ];
        var _gEls = {};
        _gDefs.forEach(function(gd) {
            var line = document.createElement('div');
            line.style.cssText = 'position:absolute;left:0;right:0;height:0;border-top:1px dashed ' + gd.color + ';opacity:0.75';
            var tag = document.createElement('div');
            tag.style.cssText = 'position:absolute;right:6px;transform:translateY(-50%);color:' + gd.color + ';font:10px JetBrains Mono,monospace;background:rgba(19,23,34,0.85);border:1px solid ' + gd.color + '44;border-radius:3px;padding:1px 5px;white-space:nowrap';
            _posGuides.appendChild(line); _posGuides.appendChild(tag);
            _gEls[gd.key] = { line: line, tag: tag, def: gd };
        });
        _posContainer.appendChild(_posGuides);
        var _gAtr = _computeATR(klineChart.getDataList(), 14);
        var _gRaf = null;
        function _posPreviewMove(e) {
            if (_gRaf) return;
            var rect = _posContainer.getBoundingClientRect();
            var localY = e.clientY - rect.top;
            _gRaf = requestAnimationFrame(function() {
                _gRaf = null;
                try {
                    var coord = klineChart.convertFromPixel({ x: 0, y: localY }, { paneId: 'candle_pane' });
                    if (!coord || coord.value == null) { _posGuides.style.display = 'none'; return; }
                    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                    var entry = _isLog ? Math.exp(coord.value) : coord.value;
                    var d2 = _posDeriveSlTp(entry, _posDir, _gAtr);
                    var vals = { entry: entry, sl: d2.sl, tp: d2.tp };
                    var shown = false;
                    _gDefs.forEach(function(gd) {
                        var raw = vals[gd.key];
                        var chartV = _isLog ? Math.log(raw) : raw;
                        var px = klineChart.convertToPixel({ value: chartV }, { paneId: 'candle_pane' });
                        var el = _gEls[gd.key];
                        if (!px || typeof px.y !== 'number' || !isFinite(px.y)) { el.line.style.display = 'none'; el.tag.style.display = 'none'; return; }
                        el.line.style.display = ''; el.tag.style.display = '';
                        el.line.style.top = px.y + 'px';
                        el.tag.style.top = px.y + 'px';
                        var stats = (gd.key === 'tp' && window.carracPositionStats) ? window.carracPositionStats(raw, entry, vals.sl) : null;
                        el.tag.textContent = gd.label + ' ' + (typeof _fmtObjPrice === 'function' ? _fmtObjPrice(raw) : raw.toFixed(2)) + (stats && stats.rLabel ? ' · ' + stats.rLabel : '');
                        shown = true;
                    });
                    _posGuides.style.display = shown ? '' : 'none';
                } catch (ex) { _posGuides.style.display = 'none'; }
            });
        }
        function _posPreviewLeave() { _posGuides.style.display = 'none'; }
        _posContainer.addEventListener('mousemove', _posPreviewMove);
        _posContainer.addEventListener('mouseleave', _posPreviewLeave);

        function _posPlace(localX, localY, ctrlKey) {
            var coord = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: 'candle_pane' });
            if (!coord || coord.dataIndex == null || coord.value == null) return;
            var dataList = klineChart.getDataList();
            var ts = (coord.dataIndex >= 0 && coord.dataIndex < dataList.length)
                ? dataList[coord.dataIndex].timestamp : Date.now();
            var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
            // snap entry in chart-space BEFORE exp (coord.value is chart-space)
            var _snapCoordValue = _magnetSnapValue(coord.dataIndex, coord.value, 'candle_pane', !!ctrlKey);
            var entry = (_isLog && _snapCoordValue != null) ? Math.exp(_snapCoordValue) : _snapCoordValue;

            var atr = _computeATR(dataList, 14);
            var _derived = _posDeriveSlTp(entry, _posDir, atr);
            var sl = _derived.sl, tp = _derived.tp;

            var pts = [
                { timestamp: ts, value: entry },
                { timestamp: ts, value: sl },
                { timestamp: ts, value: tp }
            ];

            // Create through the proven path (same as load/paste/duplicate):
            // createOverlayFromDrawing handles {timestamp,value} → snapPointsToData
            // (incl. log-space transform). Raw createOverlay with dataIndex-shaped
            // points renders a degenerate overlay ("just a line").
            var newDrawing = {
                name: tool,
                points: pts,
                color: _posColor,
                width: 1,
                extendData: {}
            };
            var newId = createOverlayFromDrawing(newDrawing);

            _posCleanup();

            if (newId) {
                newDrawing.id = newId;
                klineSelectedOverlayId = newId;
                saveKlineDrawing(newDrawing);
            }
        }

        function _posMouseDown(e) {
            if (e.button !== 0) return;
            e.stopPropagation();
            e.preventDefault();
            var rect = _posContainer.getBoundingClientRect();
            _posPlace(e.clientX - rect.left, e.clientY - rect.top, e.ctrlKey);
        }

        function _posTouchEnd(e) {
            e.preventDefault();
            e.stopPropagation();
            var touch = e.changedTouches && e.changedTouches[0];
            if (!touch) return;
            var rect = _posContainer.getBoundingClientRect();
            _posPlace(touch.clientX - rect.left, touch.clientY - rect.top, false);
        }

        function _posKeyDown(e) {
            if (e.key === 'Escape') { _posCleanup(); }
        }

        _posContainer.addEventListener('mousedown', _posMouseDown, true);
        _posContainer.addEventListener('touchend', _posTouchEnd, true);
        document.addEventListener('keydown', _posKeyDown);
        _posActiveDisarm = _posCleanup;
        return;
    }

    // Old 'position' tool name: route to longPosition by default (backward compat for saved prefs)
    if (tool === 'position') {
        button.setAttribute('data-tool', 'longPosition');
        handleKlineToolClick('longPosition', button, allButtons, event);
        return;
    }

    // Use KLineChart's built-in drawing tools with selected color and width
    if (klineChart && tool) {
        var currentColor = klineDrawingColor;
        var currentWidth = klineDrawingWidth;
        var callbacks = getOverlayCallbacks();
        var extData = {};
        // Rect: set default fill color with opacity
        if (tool === 'rect') {
            extData.fillColor = hexToRgba(currentColor, 0.15);
            if (currentWidth < 2) currentWidth = 2; // Default rect border thicker
        }
        // Parallel channel: pass color/width for createPointFigures
        // Map tool name to custom overlay name (avoids KLineChart built-in collisions)
        if (tool === 'parallelChannel') {
            extData._color = currentColor;
            extData._width = currentWidth;
        }
        var overlayName = tool;
        if (tool === 'parallelChannel') overlayName = 'boundedChannel';
        if (tool === 'volumeProfileRange') {
            overlayName = 'frvp';
            // Settings live in extendData (synced with the drawing); profile
            // data itself is runtime-cached in frvp.js, never persisted.
            extData = Object.assign({}, extData, (typeof FRVP_DEFAULT_SETTINGS !== 'undefined') ? FRVP_DEFAULT_SETTINGS : {});
        }
        if (tool === 'measureTool') {
            extData._color = '#2196F3';
            extData._width = 1;
        }
        // Fibonacci: use grey
        var fibStyles = null;
        if (tool === 'fibonacciRetrace' || tool === 'fibonacciLine' || tool === 'fibonacciInfinite') {
            currentColor = '#787b86';
            fibStyles = {
                line: { color: '#787b86', size: 1 },
                point: _invisiblePointStyle,
                text: { color: '#787b86' }
            };
        }
        // Detect which pane cursor is over — candle-only tools always go to candle_pane
        var targetPaneId = 'candle_pane';
        if (CANDLE_ONLY_TOOLS.indexOf(overlayName) === -1) {
            if (_fullscreenPaneId) {
                targetPaneId = _fullscreenPaneId;
            } else {
                targetPaneId = detectPaneUnderCursor();
            }
        }
        _setDrawingCursor(true);
        var overlayId = klineChart.createOverlay(Object.assign({
            name: overlayName,
            extendData: extData,
            styles: fibStyles || {
                line: { color: currentColor, size: currentWidth },
                point: _invisiblePointStyle
            },
            onDrawEnd: function(event) {
                _pendingDrawOverlayId = null;
                _setDrawingCursor(false);
                button.classList.remove('active');
                var parentGroup = button.closest ? button.closest('.tool-group-dropdown') : null;
                if (parentGroup) {
                    var groupBtn = parentGroup.querySelector('.tool-group-btn');
                    if (groupBtn) groupBtn.classList.remove('active');
                }
                if (event && event.overlay) {
                    var overlay = event.overlay;
                    // Positions start unlocked — user can lock/unlock via right-click
                    // Rects start unlocked (TradingView pattern: click to select, click away to deselect)
                    // Rect borderless detection: drawn upward = no border, downward = border
                    if (overlay.name === 'rect' && overlay.points && overlay.points.length >= 2) {
                        var _rExt = Object.assign({}, extData || {});
                        _rExt.noBorder = (overlay.points[0].value < overlay.points[1].value);
                        klineChart.overrideOverlay({ id: overlay.id, extendData: _rExt });
                        extData = _rExt;
                    }
                    var resolved = resolvePointTimestamps(overlay.points);
                    var drawObj = {
                        id: overlay.id,
                        name: overlay.name,
                        points: resolved,
                        color: currentColor,
                        width: currentWidth,
                        extendData: extData
                    };
                    // Save paneId for sub-pane drawings
                    if (targetPaneId !== 'candle_pane') {
                        drawObj.paneId = targetPaneId;
                    }
                    saveKlineDrawing(drawObj);
                }
            }
        }, callbacks), targetPaneId);
        _pendingDrawOverlayId = overlayId;
    }
}

// ---- Esc-cancel for an in-progress 2-point drawing ----
// Tracks the overlay armed by the generic createOverlay path above; Esc in
// chart-keyboard.js calls _cancelPendingDraw() to abort it (restores the old FRVP
// picker's Esc behavior, and extends it to every standard drawing tool).
var _pendingDrawOverlayId = null;

function _cancelPendingDraw() {
    if (!_pendingDrawOverlayId || !klineChart) return false;
    var id = _pendingDrawOverlayId;
    _pendingDrawOverlayId = null;
    var stillDrawing = false;
    try {
        var ovs = klineChart.getOverlayById(id);
        var ov = Array.isArray(ovs) ? ovs[0] : ovs;
        stillDrawing = !!(ov && ov.currentStep !== -1);
    } catch (e) {}
    if (!stillDrawing) return false;
    try { klineChart.removeOverlay(id); } catch (e) {}
    _setDrawingCursor(false);
    if (typeof klineModal !== 'undefined' && klineModal) {
        try {
            klineModal.querySelectorAll('.tool-btn.active, .tool-menu-item.active, .tool-group-btn.active').forEach(function(el) {
                var t = el.getAttribute('data-tool');
                if (t === 'alert' || t === 'undo' || t === 'redo' || t === 'clear' || t === 'magnet') return;
                el.classList.remove('active');
            });
        } catch (e) {}
    }
    return true;
}

// Resolve dataIndex to timestamp in overlay points so drawings survive TF switches.
// Saves { timestamp, value } — timestamp is always integer milliseconds.
// Handles right-margin points (dataIndex beyond data range) by extrapolating timestamp.
function resolvePointTimestamps(points) {
    if (!klineChart || !points) return points;
    var data = klineChart.getDataList();
    if (!data || !data.length) return points;
    var lastIdx = data.length - 1;
    // Compute candle interval for right-margin extrapolation
    var interval = data.length > 1 ? data[lastIdx].timestamp - data[lastIdx - 1].timestamp : 86400000;
    // When log scale active, KLineChart returns values in ln() space — convert back to raw for storage
    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);

    return points.map(function(p) {
        var rawVal = (_isLog && p.value != null) ? Math.exp(p.value) : p.value;
        var pt = { value: rawVal };

        // Prefer dataIndex — it captures right-margin positions that timestamp may not reflect
        if (p.dataIndex !== undefined && p.dataIndex !== null) {
            var di = p.dataIndex;
            if (di >= 0 && di <= lastIdx) {
                // Within data range: use exact candle timestamp
                pt.timestamp = Math.round(data[di].timestamp);
            } else if (di > lastIdx) {
                // Right margin: extrapolate timestamp beyond last candle
                pt.timestamp = Math.round(data[lastIdx].timestamp + (di - lastIdx) * interval);
            } else {
                // Before first candle: extrapolate backwards
                var startInterval = data.length > 1 ? data[1].timestamp - data[0].timestamp : interval;
                pt.timestamp = Math.round(data[0].timestamp + di * startInterval);
            }
            return pt;
        }

        // Fallback to timestamp if no dataIndex
        var ts = p.timestamp;
        if (ts) {
            if (ts < 1e12) ts = ts * 1000;
            pt.timestamp = Math.round(ts);
        }
        return pt;
    });
}

// Binary search for the nearest candle index matching a timestamp
// Returns dataIndex of the closest candle in the current data array
function timestampToDataIndex(data, ts) {
    if (!data || !data.length) return 0;
    var lo = 0, hi = data.length - 1;
    while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (data[mid].timestamp < ts) lo = mid + 1;
        else hi = mid;
    }
    if (lo > 0 && Math.abs(data[lo - 1].timestamp - ts) < Math.abs(data[lo].timestamp - ts)) {
        return lo - 1;
    }
    return lo;
}

// Prepare saved drawing points for createOverlay().
// Prepare saved drawing points for createOverlay().
// Returns ONLY { dataIndex, value } — no timestamp — because KLineChart v9.8.12
// ignores dataIndex when timestamp is present and uses its own internal resolution,
// which clamps right-margin points to the last candle. By omitting timestamp,
// we force KLineChart to use our explicit dataIndex for pixel-perfect positioning.
// The dataIndex is freshly computed from the saved timestamp for each TF.
function snapPointsToData(points) {
    if (!points) return points;
    var data = klineChart ? klineChart.getDataList() : null;
    // When log scale active, stored values are raw prices — transform to ln() space for chart
    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    return points.map(function(p, idx) {
        var v = (_isLog && p.value != null && p.value > 0) ? Math.log(p.value) : p.value;
        var ts = p.timestamp;
        if (!ts && p.dataIndex !== undefined) {
            // Point only has dataIndex (legacy) — use directly
            return { dataIndex: p.dataIndex, value: v };
        }
        if (!ts) {
            // No positioning info (value-only point) — assign to recent candles
            if (data && data.length) {
                var fallbackIdx = Math.max(0, data.length - 60 + idx * 10);
                fallbackIdx = Math.min(fallbackIdx, data.length - 1);
                return { dataIndex: fallbackIdx, value: v };
            }
            return { value: v };
        }
        // Detect seconds-vs-milliseconds: timestamps < 1e12 are likely seconds
        if (ts < 1e12) ts = ts * 1000;
        ts = Math.round(ts);
        // Resolve timestamp → dataIndex, with extrapolation for out-of-range timestamps
        if (data && data.length) {
            var lastIdx = data.length - 1;
            var lastTs = data[lastIdx].timestamp;
            var firstTs = data[0].timestamp;

            if (ts > lastTs) {
                // Beyond last candle: extrapolate dataIndex into right margin
                var interval = data.length > 1 ? lastTs - data[lastIdx - 1].timestamp : 86400000;
                var extraBars = interval > 0 ? Math.round((ts - lastTs) / interval) : 0;
                return { dataIndex: lastIdx + extraBars, value: v };
            } else if (ts < firstTs) {
                // Before first candle: extrapolate negative dataIndex
                var interval = data.length > 1 ? data[1].timestamp - firstTs : 86400000;
                var extraBars = interval > 0 ? Math.round((ts - firstTs) / interval) : 0;
                return { dataIndex: extraBars, value: v };
            } else {
                // Within data range: exact binary search
                var di = timestampToDataIndex(data, ts);
                return { dataIndex: di, value: v };
            }
        }
        return { timestamp: ts, value: v };
    });
}

// Save a single drawing to localStorage
function saveKlineDrawing(drawing) {
    if (!klineChartSymbol) return;
    // _hoverTick is a transient repaint poke on the LIVE overlay's extendData;
    // finalize merges live extendData into the drawing — keep it out of storage/sync.
    if (drawing && drawing.extendData) delete drawing.extendData._hoverTick;
    _pushUndoSnapshot();

    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        // Cross-symbol leak fix (B3): route by the drawing's OWN symbol, not the
        // current chart. Async writebacks (e.g. the trendline-alert POST .then)
        // can fire saveKlineDrawing after the user switched symbols; keying by
        // klineChartSymbol then pushed the foreign drawing into the wrong asset's
        // bucket. We stamp `symbol` on the first save (when we ARE on the drawing's
        // chart — the synchronous save always runs before any async writeback), so
        // every later save routes to the right bucket regardless of the live chart.
        var key = (drawing && drawing.symbol) ? drawing.symbol : klineChartSymbol;
        if (drawing && !drawing.symbol) drawing.symbol = key;

        if (!allDrawings[key]) {
            allDrawings[key] = [];
        }

        // assign default zOrder for new drawings (maxZ+1 so new land on top)
        var existingIndex = allDrawings[key].findIndex(function(d) { return d.id === drawing.id; });
        if (existingIndex < 0 && drawing.zOrder == null) {
            var _maxZ = 0;
            for (var _zi = 0; _zi < allDrawings[key].length; _zi++) {
                var _z = (allDrawings[key][_zi].zOrder != null) ? allDrawings[key][_zi].zOrder : 0;
                if (_z > _maxZ) _maxZ = _z;
            }
            drawing.zOrder = _maxZ + 1;
        }
        if (existingIndex >= 0) {
            allDrawings[key][existingIndex] = drawing;
        } else {
            allDrawings[key].push(drawing);
        }

        localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
        // Sync individual drawing to server (operation-level sync)
        // During overlay recreate (color/width change), sync immediately to avoid
        // race with the old-ID delete that fires right after
        if (typeof DrawingTools !== 'undefined' && DrawingTools.syncDrawingToServer) {
            DrawingTools.syncDrawingToServer(drawing, key, _isRecreatingOverlay);
        }
        // Update drawer position if a position drawing was saved
        if (drawing.name === 'position' || drawing.name === 'longPosition' || drawing.name === 'shortPosition') {
            updateDrawerPosition();
            // Auto-capture: open journal entry + create TP/SL alerts
            _autoCaptureTrade(drawing, key);
        }
        // refresh objects panel summary (count) and panel content if open
        if (typeof _refreshObjectsPanelSummary === 'function') _refreshObjectsPanelSummary();
        if (typeof document !== 'undefined' && typeof document.getElementById === 'function') {
            var _objSec = document.getElementById('drawer-section-objects');
            if (_objSec && _objSec.style && _objSec.style.display !== 'none' && typeof renderDrawingsObjectsPanel === 'function') {
                renderDrawingsObjectsPanel();
            }
        }
    } catch (e) {
    }
}

/**
 * Auto-capture trade from position drawing.
 * Extracts entry/SL/TP, opens journal entry, creates TP/SL alerts.
 */
function _autoCaptureTrade(drawing, asset) {
    var points = drawing.points || [];
    if (points.length < 2) return;

    // Already has a journal entry linked? (update instead of create)
    var existingJournalId = drawing.extendData && drawing.extendData._journalId;
    if (existingJournalId) {
        _autoUpdateTrade(existingJournalId, drawing, asset);
        return;
    }

    var entryPrice = points[0].value;
    var slPrice = points.length >= 2 ? points[1].value : null;
    var tpPrice = points.length >= 3 ? points[2].value : null;

    // Determine direction: longPosition or entry < TP (point 2)
    var direction = 'long';
    if (drawing.name === 'shortPosition') {
        direction = 'short';
    } else if (drawing.name === 'longPosition') {
        direction = 'long';
    } else if (tpPrice != null) {
        direction = tpPrice > entryPrice ? 'long' : 'short';
    } else if (slPrice != null) {
        direction = slPrice < entryPrice ? 'long' : 'short';
    }

    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';

    if (typeof autoOpenJournalEntry === 'function') {
        autoOpenJournalEntry({
            asset: asset,
            timeframe: tf,
            direction: direction,
            entry_price: entryPrice,
            target_price: tpPrice,
            stop_price: slPrice,
            drawing_id: drawing.id,
        }).then(function(journalId) {
            if (!journalId) return;
            // Store journal ID in drawing extendData for future edits
            drawing.extendData = drawing.extendData || {};
            drawing.extendData._journalId = journalId;
            // Re-save with journal ID
            try {
                var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
                var idx = (allDrawings[asset] || []).findIndex(function(d) { return d.id === drawing.id; });
                if (idx >= 0) {
                    allDrawings[asset][idx] = drawing;
                    localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
                }
            } catch(e) {}
            // Create TP/SL alerts
            if (typeof createJournalAlerts === 'function') {
                createJournalAlerts(journalId, asset, direction, tpPrice, slPrice);
            }
            // Kelly sizing tooltip
            if (typeof _getKellySuggestion === 'function' && slPrice) {
                var suggestion = _getKellySuggestion(entryPrice, slPrice);
                if (suggestion) {
                    _showKellyToast('Kelly: $' + suggestion.positionSize.toFixed(0) + ' position (risk $' + suggestion.riskAmount.toFixed(0) + ')');
                }
            }
            // Track as Play? prompt (paid users only)
            if (typeof showTrackAsPlayPrompt === 'function') {
                setTimeout(function() {
                    showTrackAsPlayPrompt({
                        asset: asset,
                        entry: entryPrice,
                        sl: slPrice,
                        tp: tpPrice,
                        direction: direction,
                        drawingId: drawing.id
                    });
                }, 1500); // Delay so it doesn't overlap Kelly toast
            }
        });
    }
}

/**
 * Compute ATR(period) from a KLineChart data list.
 * Uses simple mean of the last `period` true ranges (Wilder smoothing is NOT used;
 * simple mean gives stable defaults for one-click placement with small bar counts).
 * Returns null when dataList has fewer than 2 bars or period < 1.
 * The caller should fall back to a %-based estimate when null or 0 is returned.
 */
function _computeATR(dataList, period) {
    if (!dataList || dataList.length < 2 || !period || period < 1) return null;
    // Trim phantom future bars (X-axis padding: flat OHLC at the last close,
    // future timestamps). They zero the TR window → ATR collapses → one-click
    // positions render as a line. Check timestamps too in case the chart
    // engine copies candles and drops the _phantom flag.
    var n = dataList.length;
    var _now = Date.now();
    while (n > 0 && dataList[n - 1] &&
           (dataList[n - 1]._phantom === true || dataList[n - 1].timestamp > _now)) {
        n--;
    }
    if (n < 2) return null;
    // In log-scale mode dataList OHLC are Math.log values (raw kept in _orig*);
    // ATR must be a raw-space price delta.
    function _hiOf(c) { return (c._origHigh != null) ? c._origHigh : c.high; }
    function _loOf(c) { return (c._origLow != null) ? c._origLow : c.low; }
    function _clOf(c) { return (c._origClose != null) ? c._origClose : c.close; }
    // Collect true ranges over the most recent `period` real bars
    var trs = [];
    for (var i = Math.max(1, n - period); i < n; i++) {
        var c = dataList[i];
        var prev = dataList[i - 1];
        var hl = _hiOf(c) - _loOf(c);
        var hc = Math.abs(_hiOf(c) - _clOf(prev));
        var lc = Math.abs(_loOf(c) - _clOf(prev));
        trs.push(Math.max(hl, hc, lc));
    }
    if (trs.length === 0) return null;
    var sum = 0;
    for (var j = 0; j < trs.length; j++) sum += trs[j];
    return sum / trs.length;
}

function _showKellyToast(msg) {
    var toast = document.createElement('div');
    toast.className = 'kelly-toast';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(function() { toast.classList.add('visible'); }, 10);
    setTimeout(function() {
        toast.classList.remove('visible');
        setTimeout(function() { toast.remove(); }, 300);
    }, 5000);
}

/**
 * Update journal entry when position drawing is edited (SL/TP moved).
 */
function _autoUpdateTrade(journalId, drawing, asset) {
    var points = drawing.points || [];
    if (points.length < 2) return;

    var updates = {
        entry_price: points[0].value,
        stop_price: points.length >= 2 ? points[1].value : undefined,
        target_price: points.length >= 3 ? points[2].value : undefined,
    };

    if (typeof updateJournalFromDrawing === 'function') {
        updateJournalFromDrawing(journalId, updates);
    }

    // Recreate TP/SL alerts
    var direction = drawing.name === 'shortPosition' ? 'short' : 'long';
    if (drawing.name === 'position' && updates.target_price != null) {
        direction = updates.target_price > updates.entry_price ? 'long' : 'short';
    }

    if (typeof deleteJournalAlerts === 'function' && typeof createJournalAlerts === 'function') {
        deleteJournalAlerts(journalId).then(function() {
            createJournalAlerts(journalId, asset, direction, updates.target_price, updates.stop_price);
        });
    }
}

// Remove a drawing from storage
function removeKlineDrawingFromStorage(drawingId) {
    if (!klineChartSymbol) return;
    _pushUndoSnapshot();

    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;

        if (allDrawings[key]) {
            allDrawings[key] = allDrawings[key].filter(function(d) { return d.id !== drawingId; });
            localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
        }

        // Delete from server — but NOT during overlay recreation (color/width change)
        if (!_isRecreatingOverlay && typeof DrawingTools !== 'undefined' && DrawingTools.deleteDrawingFromServer) {
            DrawingTools.deleteDrawingFromServer(drawingId, key);
        }
        // Refresh drawer position in case a position was deleted
        updateDrawerPosition();
        // refresh objects panel summary and content if open
        if (typeof _refreshObjectsPanelSummary === 'function') _refreshObjectsPanelSummary();
        if (typeof document !== 'undefined' && typeof document.getElementById === 'function') {
            var _objSecR = document.getElementById('drawer-section-objects');
            if (_objSecR && _objSecR.style && _objSecR.style.display !== 'none' && typeof renderDrawingsObjectsPanel === 'function') {
                renderDrawingsObjectsPanel();
            }
        }
    } catch (e) {
    }
}

// Clear all drawings from storage
function clearKlineDrawingsFromStorage() {
    if (!klineChartSymbol) return;
    _pushUndoSnapshot();

    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        allDrawings[key] = [];
        localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
    } catch (e) {
    }
}

// ---- Drawing overlay callbacks (right-click, select, deselect) ----
function getOverlayCallbacks() {
    return {
        onPressedMoveStart: function(event) {
            // Block KLineChart's own drag when our custom channel corner drag is active
            if (_channelCornerDrag) return false;
            if (_frvpEdgeDrag) return false;
            // Block KLineChart drag for position overlays — our custom handler manages all interaction
            if (event && event.overlay) {
                var _nm = event.overlay.name;
                if (_nm === 'position' || _nm === 'longPosition' || _nm === 'shortPosition') return false;
            }
            // Block drag when mouse is in Y-axis zone (for other overlay types)
            try {
                var _cont = document.getElementById('kline-chart-container');
                if (_cont) {
                    var _cr = _cont.getBoundingClientRect();
                    var _localX = (_cont._lastMouseX || 0) - _cr.left;
                    if (_localX > _getYAxisStartX()) return false;
                }
            } catch(e) {}
        },
        onMouseEnter: function(event) {
            if (event && event.overlay) {
                _hoveredOverlayId = event.overlay.id;
                // thicken native overlay lines on hover (not selected, not custom trio)
                var _hovName = event.overlay.name;
                var _isCustomTrio = (_hovName === 'rect' || _hovName === 'boundedChannel' || _hovName === 'frvp' || _hovName === 'position' || _hovName === 'longPosition' || _hovName === 'shortPosition');
                if (!_isCustomTrio && event.overlay.id !== klineSelectedOverlayId) {
                    try {
                        var _origW = (event.overlay.styles && event.overlay.styles.line && event.overlay.styles.line.size) || 1;
                        _nativeHoverOrigWidth[event.overlay.id] = _origW;
                        klineChart.overrideOverlay({ id: event.overlay.id, styles: { line: { size: _origW + 1 } } });
                    } catch(e) {}
                }
            }
        },
        onMouseLeave: function(event) {
            if (event && event.overlay && _hoveredOverlayId === event.overlay.id) {
                _hoveredOverlayId = null;
                // restore native overlay line width on leave
                var _hovName = event.overlay.name;
                var _isCustomTrio = (_hovName === 'rect' || _hovName === 'boundedChannel' || _hovName === 'frvp' || _hovName === 'position' || _hovName === 'longPosition' || _hovName === 'shortPosition');
                if (!_isCustomTrio) {
                    try {
                        var _restoreW = _nativeHoverOrigWidth[event.overlay.id] || 1;
                        delete _nativeHoverOrigWidth[event.overlay.id];
                        klineChart.overrideOverlay({ id: event.overlay.id, styles: { line: { size: _restoreW } } });
                    } catch(e) {}
                }
            }
        },
        onRightClick: function(event) {
            if (event && event.overlay) {
                // Use event coordinates if available, fall back to tracked position
                var mx, my;
                if (event.event && event.event.clientX != null) {
                    mx = event.event.clientX; my = event.event.clientY;
                } else {
                    var container = document.getElementById('kline-chart-container');
                    mx = container ? (container._lastMouseX || 0) : 0;
                    my = container ? (container._lastMouseY || 0) : 0;
                }
                showDrawingContextMenu(mx, my, event.overlay.id);
                // Suppress browser context menu — KLineChart's return true may not work reliably
                var _suppressCtx = function(e) { e.preventDefault(); document.removeEventListener('contextmenu', _suppressCtx, true); };
                document.addEventListener('contextmenu', _suppressCtx, true);
                return true;
            }
        },
        onSelected: function(event) {
            if (event && event.overlay) {
                var oid = event.overlay.id;
                var nm = event.overlay.name;
                klineSelectedOverlayId = oid;

                // Double-click detection for text annotation editing
                if (nm === 'textAnnotation') {
                    var now = Date.now();
                    if (klineSelectedOverlayId === oid && window._lastTextSelectTime && now - window._lastTextSelectTime < 500 && window._lastTextSelectId === oid) {
                        window._lastTextSelectTime = 0;
                        var d = getKlineDrawingById(oid);
                        var ext = (d && d.extendData) || {};
                        showTextAnnotationDialog(function(text, fontSize, color, fmt) {
                            if (!text) return;
                            var extData = { text: text, fontSize: fontSize, color: color, bold: fmt.bold, italic: fmt.italic, underline: fmt.underline, strikethrough: fmt.strikethrough };
                            updateKlineDrawingExtendData(oid, extData);
                            klineChart.overrideOverlay({ id: oid, extendData: extData, styles: { line: { color: color } } });
                            var dd = getKlineDrawingById(oid);
                            if (dd) { dd.color = color; saveKlineDrawing(dd); }
                        }, { text: ext.text, fontSize: ext.fontSize, color: ext.color || (d && d.color), bold: ext.bold, italic: ext.italic, underline: ext.underline, strikethrough: ext.strikethrough });
                        return;
                    }
                    window._lastTextSelectTime = now;
                    window._lastTextSelectId = oid;
                }

                // Locked overlays: single click → deselect immediately (click passes through next time)
                // Position overlays are always KLineChart-locked (our custom handler manages them) — skip this check
                var _isPosition = nm === 'position' || nm === 'longPosition' || nm === 'shortPosition';
                if (!_isPosition) {
                    var _ov = null; try { _ov = klineChart.getOverlayById(oid); } catch(e) {}
                    var isLocked = _ov && _ov.lock === true;
                    if (isLocked) {
                        // Remember the swallowed click so a Delete keypress right
                        // after can explain why nothing is selected (chart-keyboard.js).
                        window._lastLockedClickTs = Date.now();
                        klineSelectedOverlayId = null;
                        return;
                    }
                }

                // Selecting a drawing does not change its z-order; use Bring to Front.
            }
        },
        onDeselected: function(event) {
            if (_isRecreatingOverlay) return;  // Suppress during overlay recreation
            klineSelectedOverlayId = null;
        },
        onPressedMoveEnd: function(event) {
            // Save updated points after dragging
            if (event && event.overlay) {
                var o = event.overlay;
                // Rect borderless detection on first creation
                if (o.name === 'rect' && o.points && o.points.length >= 2) {
                    var ext = o.extendData || {};
                    if (ext.noBorder === undefined) {
                        ext = Object.assign({}, ext);
                        // Drawn upward (second point higher price) = borderless
                        ext.noBorder = (o.points[0].value < o.points[1].value);
                        klineChart.overrideOverlay({ id: o.id, extendData: ext });
                    }
                }
                // Alert line with a linked server alert: moving the level MUST
                // move the alert — a tag showing a price the
                // server is not watching is worse than no tag. Server refuses →
                // revert the line and say so.
                if ((o.name === 'alertLine' || o.name === 'alertLevel') && o.extendData && o.extendData._alertId) {
                    var _alD = getOrCreateDrawingRecord(o.id);
                    if (_alD) {
                        var _alPrev = JSON.parse(JSON.stringify(_alD.points || []));
                        var _alPts = resolvePointTimestamps(o.points); // linear values (exp'd under log)
                        var _alPrice = _alPts[0] ? _alPts[0].value : null;
                        if (_alPrice != null && isFinite(_alPrice) && _alPrice > 0) {
                            _alPts.forEach(function(p) { p.value = _alPrice; }); // keep horizontal
                            _alD.points = _alPts;
                            saveKlineDrawing(_alD);
                            var _alCur = 0;
                            try { _alCur = allAssets[klineChartSymbol] ? allAssets[klineChartSymbol].latest_price : 0; } catch (e) {}
                            var _alBody = { price: _alPrice };
                            if (_alCur > 0) _alBody.condition = (_alPrice >= _alCur) ? 'above' : 'below';
                            var _alRevert = function() {
                                _alD.points = _alPrev;
                                saveKlineDrawing(_alD);
                                try { klineChart.overrideOverlay({ id: o.id, points: snapPointsToData(_alPrev) }); } catch (e) {}
                                if (typeof showToast === 'function') showToast('Could not move the alert — line restored');
                            };
                            fetch('/api/alerts/' + o.extendData._alertId, {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                                credentials: 'same-origin',
                                body: JSON.stringify(_alBody)
                            })
                            .then(function(r) { return r.json().then(function(j) { return { ok: r.ok, data: j }; }); })
                            .then(function(res) {
                                if (res.ok && res.data && res.data.alert) {
                                    if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts({ fresh: true });
                                    if (typeof showToast === 'function') showToast('Alert moved with the line');
                                } else {
                                    _alRevert();
                                }
                            })
                            .catch(function() { _alRevert(); });
                        }
                    }
                    return; // handled — skip the generic save below
                }
                // Position lock state managed by user via right-click
                var existing = getOrCreateDrawingRecord(o.id);
                if (existing) {
                    existing.points = resolvePointTimestamps(o.points);
                    saveKlineDrawing(existing);
                }
            }
        }
    };
}

// ---- Context menu for right-clicking drawings ----
var klineContextMenu = null;
var klineContextMenuOverlayId = null;

// Monochrome SVG icons for context menu (14x14, currentColor)
var _ico = {
    lock: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
    unlock: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>',
    opacity: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2z"/><path d="M12 2v20" stroke-dasharray="2 2"/></svg>',
    border: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>',
    noBorder: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="3" y="3" width="18" height="18" rx="2" stroke-dasharray="3 3"/></svg>',
    text: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><path d="M4 7V4h16v3"/><line x1="12" y1="4" x2="12" y2="20"/><line x1="8" y1="20" x2="16" y2="20"/></svg>',
    removeText: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    bellOn: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
    bellOff: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><path d="M13.7 21a2 2 0 0 1-3.4 0"/><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
    settings: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
    copy: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    duplicate: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/><line x1="15.5" y1="12.5" x2="15.5" y2="18.5"/><line x1="12.5" y1="15.5" x2="18.5" y2="15.5"/></svg>',
    paste: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>'
};

function showDrawingContextMenu(x, y, overlayId) {
    // Remove previous menu to rebuild (may have different items per overlay type)
    if (klineContextMenu) klineContextMenu.remove();

    klineContextMenu = document.createElement('div');
    klineContextMenu.className = 'drawing-context-menu';
    var drawingData = getKlineDrawingById(overlayId);
    // Fallback: if drawing not in storage, check KLineChart overlay directly
    if (!drawingData && klineChart) {
        try {
            var _ovArr = klineChart.getOverlayById(overlayId);
            var _ov = Array.isArray(_ovArr) ? _ovArr[0] : _ovArr;
            if (_ov && _ov.name) {
                drawingData = { id: overlayId, name: _ov.name, points: _ov.points || [], extendData: _ov.extendData || {}, color: (_ov.styles && _ov.styles.line && _ov.styles.line.color) };
            }
        } catch(e) {}
    }
    var menuHtml;
    if (drawingData && drawingData.name === 'rect') {
        var hasBorder = !(drawingData.extendData && drawingData.extendData.noBorder);
        var hasText = drawingData.extendData && drawingData.extendData.rectText;
        var _ov = null; try { _ov = klineChart.getOverlayById(overlayId); } catch(e) {}
        var isLocked = _ov && _ov.lock === true;
        menuHtml =
            '<div class="ctx-menu-item" data-action="toggle-lock">' + (isLocked ? _ico.unlock + ' Unlock' : _ico.lock + ' Lock') + '</div>' +
            '<div class="ctx-menu-item" data-action="edit-opacity">' + _ico.opacity + ' Opacity</div>' +
            '<div class="ctx-menu-item" data-action="toggle-border">' + (hasBorder ? _ico.noBorder + ' Remove Border' : _ico.border + ' Add Border') + '</div>' +
            (hasText
                ? '<div class="ctx-menu-item" data-action="rect-edit-text">' + _ico.text + ' Edit Text</div>' +
                  '<div class="ctx-menu-item" data-action="rect-remove-text">' + _ico.removeText + ' Remove Text</div>'
                : '<div class="ctx-menu-item" data-action="rect-add-text">' + _ico.text + ' Add Text</div>');
    } else {
        menuHtml = '';
    }
    if (drawingData && drawingData.name === 'textAnnotation') {
        menuHtml += '<div class="ctx-menu-item" data-action="edit-text">' + _ico.text + ' Edit Text</div>';
    }
    // Callout: its own edit-text action (preserves offsetX/offsetY — the
    // generic 'edit-text' action below replaces extendData wholesale, which
    // would reset the text-box position) + a "Text Position" preset picker.
    if (drawingData && drawingData.name === 'callout') {
        menuHtml += '<div class="ctx-menu-item" data-action="callout-edit-text">' + _ico.text + ' Edit Text</div>' +
            '<div class="ctx-menu-item" data-action="callout-text-pos">' + _ico.settings + ' Text Position</div>';
    }
    // Show "Remove Alert" for alertLine overlays OR any overlay with an _alertId
    var _isAlert = drawingData && (drawingData.name === 'alertLine' || drawingData.name === 'alertLevel' || (drawingData.extendData && drawingData.extendData._alertId));
    if (!_isAlert && klineChart) {
        try {
            var _aov = klineChart.getOverlayById(overlayId);
            var _ao = Array.isArray(_aov) ? _aov[0] : _aov;
            if (_ao && (_ao.name === 'alertLine' || _ao.name === 'alertLevel' || (_ao.extendData && _ao.extendData._alertId))) _isAlert = true;
        } catch(e) {}
    }
    if (_isAlert) {
        var _hasNote = drawingData && drawingData.extendData && drawingData.extendData.note;
        menuHtml += '<div class="ctx-menu-item" data-action="alert-note">' + _ico.text + (_hasNote ? ' Edit Note' : ' Add Note') + '</div>' +
            '<div class="ctx-menu-item" data-action="remove-alert">' + _ico.bellOff + ' Remove Alert</div>';
    }
    if (drawingData && (drawingData.name === 'fibonacciRetrace' || drawingData.name === 'fibonacciInfinite')) {
        menuHtml += '<div class="ctx-menu-item" data-action="edit-fib-levels">' + _ico.settings + ' Edit Levels</div>';
    }
    // FRVP: volume mode + value area % + display rows (click cycles values)
    if (drawingData && drawingData.name === 'frvp') {
        var _fvExt = drawingData.extendData || {};
        var _fvMode = _fvExt.vpMode || 'updown';
        var _fvVa = _fvExt.vaPct || 70;
        var _fvRows = _fvExt.vpRows || 100;
        var _fvModeLabel = _fvMode === 'total' ? 'Total' : (_fvMode === 'delta' ? 'Delta (taker)' : 'Up/Down');
        menuHtml += '<div class="ctx-menu-separator"></div>' +
            '<div class="ctx-menu-item" data-action="frvp-mode">' + _ico.settings + ' Volume: ' + _fvModeLabel + '</div>' +
            '<div class="ctx-menu-item" data-action="frvp-va">' + _ico.settings + ' Value Area: ' + _fvVa + '%</div>' +
            '<div class="ctx-menu-item" data-action="frvp-rows">' + _ico.settings + ' Rows: ' + _fvRows + '</div>' +
            '<div class="ctx-menu-item" data-action="frvp-extend">' + _ico.settings + ' Extend Right ' + (_fvExt.extendRight ? '✓' : '✗') + '</div>';
    }
    if (drawingData && ['straightLine', 'rayLine', 'segment'].indexOf(drawingData.name) !== -1) {
        var isWatched = drawingData.extendData && drawingData.extendData.watch;
        menuHtml += '<div class="ctx-menu-item" data-action="toggle-watch">' +
            (isWatched ? _ico.bellOff + ' Remove Alert' : _ico.bellOn + ' Add Alert To Trendline') + '</div>';
    }
    // Spot: sell + edit
    if (drawingData && drawingData.name === 'spot' && !(drawingData.extendData && drawingData.extendData.sold)) {
        menuHtml += '<div class="ctx-menu-item" data-action="sell-spot">' + _ico.tag + ' Sell Spot</div>';
    }
    // Lock/unlock + opacity for all overlay types (rect has its own branch above)
    if (drawingData && drawingData.name !== 'rect') {
        var _lockOv = null; try { _lockOv = klineChart.getOverlayById(overlayId); } catch(e) {}
        var _isLocked = _lockOv && _lockOv.lock === true;
        menuHtml += '<div class="ctx-menu-item" data-action="toggle-lock">' +
            (_isLocked ? _ico.unlock + ' Unlock' : _ico.lock + ' Lock') + '</div>' +
            '<div class="ctx-menu-item" data-action="edit-opacity">' + _ico.opacity + ' Opacity</div>';
    }
    // Copy / Duplicate for copyable drawing types
    if (drawingData && _isDrawingCopyable(drawingData.name)) {
        menuHtml += '<div class="ctx-menu-separator"></div>' +
            '<div class="ctx-menu-item" data-action="copy">' + _ico.copy + ' Copy</div>' +
            '<div class="ctx-menu-item" data-action="duplicate">' + _ico.duplicate + ' Duplicate</div>';
    }
    // Bring to Front / Send to Back — for any drawing with a localStorage record
    if (drawingData) {
        menuHtml += '<div class="ctx-menu-separator"></div>' +
            '<div class="ctx-menu-item" data-action="bring-to-front"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="3" y="3" width="10" height="10" rx="1"/><rect x="11" y="11" width="10" height="10" rx="1"/></svg> Bring to Front</div>' +
            '<div class="ctx-menu-item" data-action="send-to-back"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px"><rect x="11" y="11" width="10" height="10" rx="1"/><rect x="3" y="3" width="10" height="10" rx="1"/></svg> Send to Back</div>';
    }
    menuHtml += '<div class="ctx-menu-separator"></div>' +
        '<div class="ctx-menu-item ctx-delete" data-action="delete">' + _ico.trash + ' Delete</div>';

    klineContextMenu.innerHTML = menuHtml;
    document.body.appendChild(klineContextMenu);

    klineContextMenu.addEventListener('click', function(e) {
        var item = e.target.closest('[data-action]');
        if (!item) return;
        handleContextMenuAction(item.getAttribute('data-action'), klineContextMenuOverlayId);
        hideDrawingContextMenu();
    });

    klineContextMenuOverlayId = overlayId;
    var menuW = 160, menuH = 120;
    klineContextMenu.style.left = Math.min(x, window.innerWidth - menuW - 10) + 'px';
    klineContextMenu.style.top = Math.min(y, window.innerHeight - menuH - 10) + 'px';
    klineContextMenu.classList.add('visible');
}

function hideDrawingContextMenu() {
    if (klineContextMenu) klineContextMenu.classList.remove('visible');
    klineContextMenuOverlayId = null;
}

function showFibLevelEditor(overlayId) {
    var d = getKlineDrawingById(overlayId);
    if (!d) return;
    var ext = d.extendData || {};
    var allLevels = [
        0, 0.236, 0.382, 0.5, 0.618, 0.786, 1,
        1.272, 1.414, 1.618, 2, 2.272, 2.414, 2.618,
        3, 3.272, 3.414, 3.618, 4, 4.236, 4.272, 4.414, 4.618, 4.764
    ];
    var defaultEnabled = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.414, 1.618, 2, 2.272, 2.414, 2.618];
    // Per-fib levels > global defaults
    var currentEnabled = ext._fibLevels || (function() { try { var s = localStorage.getItem('fibEnabledLevels'); if (s) return JSON.parse(s); } catch(e) {} return defaultEnabled; })();

    var old = document.getElementById('fib-level-editor');
    if (old) old.remove();
    var modal = document.createElement('div');
    modal.id = 'fib-level-editor';
    modal.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:90000;background:#1e222d;border:1px solid #363a45;border-radius:8px;padding:16px 20px;min-width:320px;max-height:80vh;overflow-y:auto;color:#d1d4dc;font:13px JetBrains Mono,monospace;box-shadow:0 8px 32px rgba(0,0,0,0.5)';

    var half = Math.ceil(allLevels.length / 2);
    var leftCol = allLevels.slice(0, half);
    var rightCol = allLevels.slice(half);

    function makeRow(lv) {
        var checked = currentEnabled.indexOf(lv) !== -1;
        var label = lv % 1 === 0 ? lv.toString() : lv.toString();
        return '<label style="display:flex;align-items:center;gap:6px;padding:3px 0;cursor:pointer">' +
            '<input type="checkbox" data-lv="' + lv + '"' + (checked ? ' checked' : '') + ' style="accent-color:#2962ff;width:16px;height:16px;cursor:pointer">' +
            '<span style="color:' + (checked ? '#d1d4dc' : '#787b86') + '">' + label + '</span></label>';
    }

    var isExtended = !!(ext._fibExtend);
    modal.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">' +
            '<span style="font-size:14px;font-weight:bold">Fibonacci Levels</span>' +
            '<button id="fib-editor-close" style="background:none;border:none;color:#787b86;cursor:pointer;font-size:18px">&times;</button>' +
        '</div>' +
        '<label style="display:flex;align-items:center;gap:8px;margin-bottom:12px;cursor:pointer">' +
            '<input type="checkbox" id="fib-extend-toggle"' + (isExtended ? ' checked' : '') + ' style="accent-color:#2962ff;width:16px;height:16px;cursor:pointer">' +
            '<span>Extend to full width</span>' +
        '</label>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0 20px">' +
            '<div>' + leftCol.map(makeRow).join('') + '</div>' +
            '<div>' + rightCol.map(makeRow).join('') + '</div>' +
        '</div>' +
        '<div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">' +
            '<button id="fib-save-global" style="background:#2a2e39;border:1px solid #363a45;color:#787b86;padding:6px 12px;border-radius:4px;cursor:pointer;font:12px JetBrains Mono,monospace">Save as Default</button>' +
            '<button id="fib-apply" style="background:#2962ff;border:none;color:#fff;padding:6px 16px;border-radius:4px;cursor:pointer;font:12px JetBrains Mono,monospace">Apply</button>' +
        '</div>';

    document.body.appendChild(modal);

    function getChecked() {
        var inputs = modal.querySelectorAll('input[type=checkbox]');
        var levels = [];
        for (var i = 0; i < inputs.length; i++) {
            if (inputs[i].checked) levels.push(parseFloat(inputs[i].getAttribute('data-lv')));
        }
        return levels;
    }

    // Update label colors on toggle
    modal.addEventListener('change', function(e) {
        if (e.target.type === 'checkbox') {
            var span = e.target.nextElementSibling;
            if (span) span.style.color = e.target.checked ? '#d1d4dc' : '#787b86';
        }
    });

    document.getElementById('fib-editor-close').addEventListener('click', function() { modal.remove(); });

    document.getElementById('fib-apply').addEventListener('click', function() {
        var levels = getChecked();
        var extend = document.getElementById('fib-extend-toggle').checked;
        updateKlineDrawingExtendData(overlayId, { _fibLevels: levels, _fibExtend: extend });
        var dd = getKlineDrawingById(overlayId);
        if (dd) {
            dd.extendData = dd.extendData || {};
            dd.extendData._fibLevels = levels;
            dd.extendData._fibExtend = extend;
            klineChart.overrideOverlay({ id: overlayId, extendData: dd.extendData });
        }
        modal.remove();
        showToast('Levels updated');
    });

    document.getElementById('fib-save-global').addEventListener('click', function() {
        var levels = getChecked();
        try { localStorage.setItem('fibEnabledLevels', JSON.stringify(levels)); } catch(e) {}
        showToast('Saved as default for new fibs');
    });

    // Close on outside click
    setTimeout(function() {
        document.addEventListener('mousedown', function _fibClose(e) {
            if (!modal.contains(e.target)) { modal.remove(); document.removeEventListener('mousedown', _fibClose); }
        });
    }, 100);
}

function getKlineDrawingById(overlayId) {
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        var drawings = all[key] || [];
        for (var i = 0; i < drawings.length; i++) {
            if (drawings[i].id === overlayId) return drawings[i];
        }
    } catch (e) {}
    return null;
}

function updateKlineDrawingExtendData(overlayId, updates) {
    _pushUndoSnapshot();
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        var drawings = all[key] || [];
        var updated = null;
        for (var i = 0; i < drawings.length; i++) {
            if (drawings[i].id === overlayId) {
                drawings[i].extendData = Object.assign(drawings[i].extendData || {}, updates);
                updated = drawings[i];
                break;
            }
        }
        all[key] = drawings;
        localStorage.setItem('klineDrawings', JSON.stringify(all));
        if (updated && typeof DrawingTools !== 'undefined' && DrawingTools.syncDrawingToServer) {
            DrawingTools.syncDrawingToServer(updated, key);
        }
    } catch (e) {}
}

// reload all drawings overlays for the current symbol (used by Send to Back).
function _reloadAllDrawingOverlays() {
    if (!klineChart || !klineChartSymbol) return;
    // Remove all drawing overlays (but not alert/indicator overlays managed elsewhere)
    try {
        var allDr = _readDrawingsStore();
        var drList = allDr[klineChartSymbol] || [];
        for (var i = 0; i < drList.length; i++) {
            if (drList[i].id) { try { klineChart.removeOverlay(drList[i].id); } catch(e) {} }
        }
    } catch(e) {}
    // Reload in sorted zOrder
    loadKlineDrawings();
    // Re-render alert overlays that live in the drawing pane
    if (typeof renderAlertOverlays === 'function') { try { renderAlertOverlays(); } catch(e) {} }
}

function handleContextMenuAction(action, overlayId) {
    if (action === 'paste') { pasteDrawing(); return; }
    if (!overlayId || !klineChart) return;
    if (action === 'copy') { copySelectedDrawing(overlayId); return; }
    if (action === 'duplicate') { duplicateSelectedDrawing(overlayId); return; }

    // z-order context menu actions
    if (action === 'bring-to-front') {
        var _bfD = getKlineDrawingById(overlayId);
        if (_bfD) {
            _bfD.zOrder = _maxZOrder() + 1;
            saveKlineDrawing(_bfD);
            if (typeof recreateOverlayWithStyles === 'function') recreateOverlayWithStyles(_bfD);
        }
        return;
    }
    if (action === 'send-to-back') {
        var _sbD = getKlineDrawingById(overlayId);
        if (_sbD) {
            _sbD.zOrder = _minZOrder() - 1;
            saveKlineDrawing(_sbD);
            _reloadAllDrawingOverlays();
        }
        return;
    }

    // FRVP settings: each click cycles to the next value (stored in extendData,
    // synced with the drawing; profile data itself is never persisted)
    if (action === 'frvp-mode' || action === 'frvp-va' || action === 'frvp-rows' || action === 'frvp-extend') {
        var _fvD = getKlineDrawingById(overlayId);
        if (_fvD) {
            var _fvE = _fvD.extendData || {};
            var _fvPatch = {};
            if (action === 'frvp-mode') {
                var _modes = ['updown', 'total', 'delta'];
                _fvPatch.vpMode = _modes[(_modes.indexOf(_fvE.vpMode || 'updown') + 1) % _modes.length];
            } else if (action === 'frvp-va') {
                var _vas = [70, 75, 80, 66, 68];
                _fvPatch.vaPct = _vas[(_vas.indexOf(_fvE.vaPct || 70) + 1) % _vas.length];
            } else if (action === 'frvp-extend') {
                // Toggle: on → effective end tracks the last real bar; off →
                // reverts to the untouched stored points[1] anchor.
                _fvPatch.extendRight = !_fvE.extendRight;
            } else {
                var _rowsOpts = [100, 200, 24, 50];
                _fvPatch.vpRows = _rowsOpts[(_rowsOpts.indexOf(_fvE.vpRows || 100) + 1) % _rowsOpts.length];
            }
            updateKlineDrawingExtendData(overlayId, _fvPatch);
            try {
                klineChart.overrideOverlay({ id: overlayId, extendData: Object.assign({}, _fvE, _fvPatch) });
            } catch(e) {}
            var _fvDD = getKlineDrawingById(overlayId);
            if (_fvDD) saveKlineDrawing(_fvDD);
            if (typeof showToast === 'function') {
                var _tv = _fvPatch.vpMode ? ('Volume: ' + (_fvPatch.vpMode === 'total' ? 'Total' : _fvPatch.vpMode === 'delta' ? 'Delta (taker)' : 'Up/Down'))
                        : _fvPatch.vaPct ? ('Value Area: ' + _fvPatch.vaPct + '%')
                        : ('extendRight' in _fvPatch) ? ('Extend Right: ' + (_fvPatch.extendRight ? 'on' : 'off'))
                        : ('Rows: ' + _fvPatch.vpRows);
                showToast(_tv);
            }
        }
        return;
    }

    if (action === 'delete') {
        // Clean up server-side alert if this drawing has one
        var _delDrawing = getKlineDrawingById(overlayId);
        if (!_delDrawing && klineChart) {
            try { var _do = klineChart.getOverlayById(overlayId); _do = Array.isArray(_do) ? _do[0] : _do; if (_do) _delDrawing = { extendData: _do.extendData || {} }; } catch(e) {}
        }
        // Locked drawings don't delete from ANY path (right-click, Drawings-tab
        // trash, multi-select) — unlock first. Same toast as the keyboard guard.
        if (_delDrawing && _delDrawing.extendData && _delDrawing.extendData._locked) {
            if (typeof showToast === 'function') showToast('Locked — right-click → Unlock to delete');
            return;
        }
        var _delAlertId = _delDrawing && _delDrawing.extendData && _delDrawing.extendData._alertId;
        if (_delAlertId) {
            fetch('/api/alerts/' + _delAlertId, { method: 'DELETE', headers: { 'X-CSRFToken': getCSRFToken() }, credentials: 'same-origin' })
                .then(function() { if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts({fresh:true}); if (typeof updateAlertBadge === 'function') updateAlertBadge(); })
                .catch(function(e) {
                    if (typeof window.carracLog === 'function') window.carracLog('alert', 'linked alert delete failed — alert stays armed server-side after its drawing was removed', String((e && e.message) || e));
                    if (typeof showToast === 'function') showToast('Could not remove the linked alert — it may still fire');
                });
        }
        // This is the ONE delete implementation for right-click, keyboard
        // Delete/Backspace, the Drawings-tab trash icon, and multi-select
        // (they all route here) — so it is also the one place a promoted
        // spot buy's journal trade must be cleaned up on hard delete.
        if (typeof _deletePromotedSpotTrade === 'function') {
            _deletePromotedSpotTrade(klineChartSymbol, overlayId, _delDrawing && _delDrawing.extendData);
        }
        klineChart.removeOverlay(overlayId);
        removeKlineDrawingFromStorage(overlayId);
        klineSelectedOverlayId = null;
    } else if (action === 'edit-color') {
        showColorPickerForOverlay(overlayId);
    } else if (action === 'edit-fill') {
        showFillColorPicker(overlayId);
    } else if (action === 'edit-opacity') {
        showOpacityPicker(overlayId);
    } else if (action === 'edit-width') {
        showWidthPicker(overlayId);
    } else if (action === 'edit-text') {
        var d = getKlineDrawingById(overlayId);
        var ext = (d && d.extendData) || {};
        showTextAnnotationDialog(function(text, fontSize, color, fmt) {
            if (!text) return;
            var extData = { text: text, fontSize: fontSize, color: color, bold: fmt.bold, italic: fmt.italic, underline: fmt.underline, strikethrough: fmt.strikethrough };
            updateKlineDrawingExtendData(overlayId, extData);
            klineChart.overrideOverlay({ id: overlayId, extendData: extData, styles: { line: { color: color } } });
            // Also update color in localStorage
            var dd = getKlineDrawingById(overlayId);
            if (dd) { dd.color = color; saveKlineDrawing(dd); }
        }, { text: ext.text, fontSize: ext.fontSize, color: ext.color || (d && d.color), bold: ext.bold, italic: ext.italic, underline: ext.underline, strikethrough: ext.strikethrough });
    } else if (action === 'callout-edit-text') {
        // Separate from 'edit-text' — merges into extendData (Object.assign)
        // instead of replacing it wholesale, so offsetX/offsetY (the text
        // box's position relative to the anchor) survive the edit.
        var _coD = getKlineDrawingById(overlayId);
        var _coExt = (_coD && _coD.extendData) || {};
        showTextAnnotationDialog(function(text, fontSize, color, fmt) {
            if (!text) return;
            var _coPatch = { text: text, fontSize: fontSize, _color: color, bold: fmt.bold, italic: fmt.italic, underline: fmt.underline, strikethrough: fmt.strikethrough };
            updateKlineDrawingExtendData(overlayId, _coPatch);
            var _coMerged = Object.assign({}, _coExt, _coPatch);
            klineChart.overrideOverlay({ id: overlayId, extendData: _coMerged, styles: { line: { color: color } } });
            var _coDd = getKlineDrawingById(overlayId);
            if (_coDd) { _coDd.color = color; saveKlineDrawing(_coDd); }
        }, { text: _coExt.text, fontSize: _coExt.fontSize, color: _coExt._color || _coExt.color, bold: _coExt.bold, italic: _coExt.italic, underline: _coExt.underline, strikethrough: _coExt.strikethrough });
    } else if (action === 'callout-text-pos') {
        var _cpPosLeft = klineContextMenu ? klineContextMenu.style.left : '100px';
        var _cpPosTop = klineContextMenu ? (parseInt(klineContextMenu.style.top) + 80) + 'px' : '100px';
        setTimeout(function() {
            var existing = document.getElementById('text-pos-picker');
            if (existing) existing.remove();
            var picker = document.createElement('div');
            picker.id = 'text-pos-picker';
            picker.className = 'overlay-color-picker visible';
            picker.style.width = '140px';
            picker.style.flexDirection = 'column';
            picker.style.left = _cpPosLeft;
            picker.style.top = _cpPosTop;
            var _cpOffsets = {
                'top-left': { offsetX: -70, offsetY: -60 },
                'top-right': { offsetX: 70, offsetY: -60 },
                'center': { offsetX: 0, offsetY: -80 },
                'bottom-left': { offsetX: -70, offsetY: 60 },
                'bottom-right': { offsetX: 70, offsetY: 60 }
            };
            var positions = [
                { val: 'top-left', label: '↖ Top Left' },
                { val: 'top-right', label: '↗ Top Right' },
                { val: 'center', label: '◉ Above' },
                { val: 'bottom-left', label: '↙ Bottom Left' },
                { val: 'bottom-right', label: '↘ Bottom Right' }
            ];
            picker.innerHTML = positions.map(function(p) {
                return '<div class="opacity-option" style="text-align:left" data-pos="' + p.val + '">' + p.label + '</div>';
            }).join('');
            document.body.appendChild(picker);
            picker.addEventListener('click', function(e) {
                var el = e.target.closest('[data-pos]');
                if (!el) return;
                var pos = el.getAttribute('data-pos');
                var off = _cpOffsets[pos];
                if (off) {
                    updateKlineDrawingExtendData(overlayId, off);
                    var _cpD = getKlineDrawingById(overlayId);
                    var _cpExt = (_cpD && _cpD.extendData) || {};
                    try { klineChart.overrideOverlay({ id: overlayId, extendData: _cpExt }); } catch(e2) {}
                }
                picker.remove();
                showToast('Text: ' + pos);
            });
            setTimeout(function() {
                document.addEventListener('mousedown', function _dismiss(e) {
                    if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('mousedown', _dismiss); }
                });
            }, 50);
        }, 0);
    } else if (action === 'toggle-border') {
        var d = getKlineDrawingById(overlayId);
        if (d) {
            var noBorder = !(d.extendData && d.extendData.noBorder);
            updateKlineDrawingExtendData(overlayId, { noBorder: noBorder });
            klineChart.overrideOverlay({ id: overlayId, extendData: Object.assign({}, d.extendData || {}, { noBorder: noBorder }) });
            showToast(noBorder ? 'Border removed' : 'Border added');
        }
    } else if (action === 'alert-note') {
        // Note lives on BOTH the drawing (extendData.note — synced to the
        // drawings store, so MCP drawing_list sees it) and the server alert's
        // note field (behind the load-bearing '[drawn]' marker that keeps
        // renderAlertOverlays from double-drawing) — so the AI reads it from
        // either side.
        var _anD = getKlineDrawingById(overlayId);
        if (_anD) {
            _showAlertNoteDialog((_anD.extendData && _anD.extendData.note) || '', function(text) {
                text = String(text || '').trim();
                _anD.extendData = _anD.extendData || {};
                if (text) _anD.extendData.note = text; else delete _anD.extendData.note;
                saveKlineDrawing(_anD);
                try { klineChart.overrideOverlay({ id: overlayId, extendData: Object.assign({}, _anD.extendData) }); } catch (e) {}
                var _anId = _anD.extendData._alertId;
                if (_anId) {
                    fetch('/api/alerts/' + _anId, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                        credentials: 'same-origin',
                        body: JSON.stringify({ note: '[drawn]' + (text ? ' ' + text : '') })
                    })
                    .then(function() { if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts({ fresh: true }); })
                    .catch(function() { if (typeof showToast === 'function') showToast('Note saved on the chart, but the alert could not be updated'); });
                }
            });
        }
    } else if (action === 'remove-alert') {
        var d = getKlineDrawingById(overlayId);
        if (d) {
            // Delete server-side alert if exists
            var alertId = d.extendData && d.extendData._alertId;
            if (alertId) {
                fetch('/api/alerts/' + alertId, {
                    method: 'DELETE',
                    headers: { 'X-CSRFToken': getCSRFToken() },
                    credentials: 'same-origin'
                }).then(function() {
                    if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts({fresh:true});
                    if (typeof updateAlertBadge === 'function') updateAlertBadge();
                }).catch(function(e) {
                    if (typeof window.carracLog === 'function') window.carracLog('alert', 'linked alert delete failed (remove-alert action) — alert stays armed server-side', String((e && e.message) || e));
                    if (typeof showToast === 'function') showToast('Could not remove the linked alert — it may still fire');
                });
            }
            // Convert to horizontal line: remove old overlay, create horizontalStraightLine
            try { klineChart.removeOverlay(overlayId); } catch(e) {}
            var lineColor = d.color || (d.extendData && d.extendData._color) || '#ff9800';
            var lineWidth = d.width || 1;
            var newOpts = {
                name: 'horizontalStraightLine',
                points: [d.points[0]],
                styles: { line: { color: lineColor, size: lineWidth }, point: _invisiblePointStyle },
                extendData: { _color: lineColor, _width: lineWidth }
            };
            var callbacks = getOverlayCallbacks();
            var newId = klineChart.createOverlay(Object.assign(newOpts, callbacks), d.paneId || 'candle_pane');
            // Update storage
            removeKlineDrawingFromStorage(overlayId);
            if (newId) {
                saveKlineDrawing({
                    id: newId,
                    name: 'horizontalStraightLine',
                    points: d.points.slice(0, 1),
                    color: lineColor,
                    width: lineWidth,
                    extendData: { _color: lineColor, _width: lineWidth }
                });
            }
            showToast('Alert removed, line kept');
        }
    } else if (action === 'edit-fib-levels') {
        showFibLevelEditor(overlayId);
    } else if (action === 'toggle-lock') {
        var isLocked = false;
        try { var ov = klineChart.getOverlayById(overlayId); var _o = Array.isArray(ov) ? ov[0] : ov; isLocked = _o && _o.lock === true; } catch(e) {}
        klineChart.overrideOverlay({ id: overlayId, lock: !isLocked });
        // Persist lock state in extendData so it survives reload
        updateKlineDrawingExtendData(overlayId, { _locked: !isLocked });
        showToast(isLocked ? 'Unlocked' : 'Locked — clicks pass through');
    } else if (action === 'rect-add-text' || action === 'rect-edit-text') {
        var d = getKlineDrawingById(overlayId);
        var ext = (d && d.extendData) || {};
        showTextAnnotationDialog(function(text, fontSize, color, fmt, position) {
            if (!text) return;
            var updates = { rectText: text, rectTextSize: fontSize, rectTextColor: color, rectTextBold: fmt.bold, rectTextItalic: fmt.italic, rectTextPos: position || 'center' };
            updateKlineDrawingExtendData(overlayId, updates);
            var dd = getKlineDrawingById(overlayId);
            if (dd) recreateOverlayWithStyles(dd);
        }, { text: ext.rectText || '', fontSize: ext.rectTextSize || 12, color: ext.rectTextColor || '#d1d4dc', bold: ext.rectTextBold, italic: ext.rectTextItalic, showPosition: true, textPosition: ext.rectTextPos || 'center' });
    } else if (action === 'rect-remove-text') {
        updateKlineDrawingExtendData(overlayId, { rectText: null, rectTextSize: null, rectTextColor: null, rectTextPos: null, rectTextBold: null, rectTextItalic: null });
        var d = getKlineDrawingById(overlayId);
        if (d) recreateOverlayWithStyles(d);
        showToast('Text removed');
    } else if (action === 'rect-text-pos') {
        // Show position picker
        var posLeft = klineContextMenu ? klineContextMenu.style.left : '100px';
        var posTop = klineContextMenu ? (parseInt(klineContextMenu.style.top) + 80) + 'px' : '100px';
        setTimeout(function() {
            var existing = document.getElementById('text-pos-picker');
            if (existing) existing.remove();
            var picker = document.createElement('div');
            picker.id = 'text-pos-picker';
            picker.className = 'overlay-color-picker visible';
            picker.style.width = '140px';
            picker.style.flexDirection = 'column';
            picker.style.left = posLeft;
            picker.style.top = posTop;
            var positions = [
                { val: 'top-left', label: '↖ Top Left' },
                { val: 'top-right', label: '↗ Top Right' },
                { val: 'center', label: '◉ Center' },
                { val: 'bottom-left', label: '↙ Bottom Left' },
                { val: 'bottom-right', label: '↘ Bottom Right' }
            ];
            picker.innerHTML = positions.map(function(p) {
                return '<div class="opacity-option" style="text-align:left" data-pos="' + p.val + '">' + p.label + '</div>';
            }).join('');
            document.body.appendChild(picker);
            picker.addEventListener('click', function(e) {
                var el = e.target.closest('[data-pos]');
                if (!el) return;
                var pos = el.getAttribute('data-pos');
                updateKlineDrawingExtendData(overlayId, { rectTextPos: pos });
                var dd = getKlineDrawingById(overlayId);
                if (dd) recreateOverlayWithStyles(dd);
                picker.remove();
                showToast('Text: ' + pos);
            });
            setTimeout(function() {
                document.addEventListener('mousedown', function _dismiss(e) {
                    if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('mousedown', _dismiss); }
                });
            }, 50);
        }, 0);
    } else if (action === 'toggle-watch') {
        var d = getOrCreateDrawingRecord(overlayId);
        if (d) {
            d.extendData = d.extendData || {};
            d.extendData.watch = !d.extendData.watch;

            // Swap overlay type between built-in and watched_ variant
            var watchableTypes = ['segment', 'straightLine', 'rayLine'];
            var currentName = d.name || 'segment';
            var isCurrentlyWatched = currentName.indexOf('watched_') === 0;
            var baseName = isCurrentlyWatched ? currentName.replace('watched_', '') : currentName;

            if (watchableTypes.indexOf(baseName) !== -1) {
                var newName = d.extendData.watch ? ('watched_' + baseName) : baseName;
                // Remove old overlay, recreate with new name
                try { klineChart.removeOverlay(overlayId); } catch(e) {}
                d.name = newName;
                var lineColor = d.color || '#3fb950';
                var lineWidth = d.width || 1;
                var overlayOpts = {
                    name: newName,
                    points: snapPointsToData(d.points || []),
                    lock: !!(d.extendData && d.extendData._locked),
                    styles: {
                        line: { color: lineColor, size: lineWidth },
                        point: _invisiblePointStyle
                    },
                    extendData: d.extendData
                };
                var pId = d.paneId || 'candle_pane';
                var callbacks = getOverlayCallbacks();
                var newId = klineChart.createOverlay(Object.assign(overlayOpts, callbacks), pId);
                // Update drawing ID if changed
                if (newId && newId !== overlayId) {
                    d.id = newId;
                    klineSelectedOverlayId = newId;
                    // B5: the recreate left the old-id record behind in localStorage —
                    // drop it so no orphan lingers next to the new id (covers both the
                    // watch-on and the reverse un-watch recreate directions).
                    if (typeof removeKlineDrawingFromStorage === 'function') removeKlineDrawingFromStorage(overlayId);
                }
            } else {
                // Non-line overlays: just update extendData (no visual bell)
                klineChart.overrideOverlay({ id: overlayId, extendData: d.extendData });
            }
            saveKlineDrawing(d);
            if (d.extendData.watch) {
                // The "watched" visual (bell + watched_ overlay) is now OPTIMISTIC:
                // it is only KEPT if the server actually creates the alert. On any
                // failure we revert to the un-watched base overlay so the user never
                // ends up with a watched trendline that has no backing alert (B1/B2).
                // _reqSymbol = the symbol whose bucket holds d.id right now; if the
                // user switches symbols mid-request the revert must not touch the
                // (now foreign) live chart, only fix that bucket's stored record.
                var _reqSymbol = klineChartSymbol;
                var _revertWatch = function() {
                    if (klineChartSymbol === _reqSymbol) {
                        // Still on the drawing's chart — swap the optimistic watched
                        // overlay back to its base type visually, then persist.
                        var _curName = d.name || '';
                        var _base = _curName.indexOf('watched_') === 0 ? _curName.replace('watched_', '') : _curName;
                        if (watchableTypes.indexOf(_base) !== -1 && _curName !== _base) {
                            var _oldId = d.id;
                            try { klineChart.removeOverlay(_oldId); } catch(e) {}
                            d.name = _base;
                            var _rvId = klineChart.createOverlay(Object.assign({
                                name: _base,
                                points: snapPointsToData(d.points || []),
                                lock: !!(d.extendData && d.extendData._locked),
                                styles: { line: { color: d.color || '#3fb950', size: d.width || 1 }, point: _invisiblePointStyle },
                                extendData: d.extendData
                            }, getOverlayCallbacks()), d.paneId || 'candle_pane');
                            if (_rvId && _rvId !== _oldId) {
                                d.id = _rvId;
                                if (klineSelectedOverlayId === _oldId) klineSelectedOverlayId = _rvId;
                                // B5: drop the stale watched-id record this recreate left behind
                                if (typeof removeKlineDrawingFromStorage === 'function') removeKlineDrawingFromStorage(_oldId);
                            }
                        }
                        d.extendData.watch = false;
                        if (d.extendData._alertId) delete d.extendData._alertId;
                        saveKlineDrawing(d);
                        if (typeof updateDrawerAlerts === 'function') updateDrawerAlerts({fresh:true});
                        if (typeof updateAlertBadge === 'function') updateAlertBadge();
                    } else {
                        // User switched symbols mid-request — the drawing lives in
                        // _reqSymbol's bucket, off the current chart. Fix that record
                        // directly so it comes back un-watched (no phantom bell).
                        try {
                            var _all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
                            var _bucket = _all[_reqSymbol];
                            if (_bucket) {
                                for (var _bi = 0; _bi < _bucket.length; _bi++) {
                                    if (_bucket[_bi].id === d.id) {
                                        var _rec = _bucket[_bi];
                                        var _rn = _rec.name || '';
                                        if (_rn.indexOf('watched_') === 0) _rec.name = _rn.replace('watched_', '');
                                        _rec.extendData = _rec.extendData || {};
                                        _rec.extendData.watch = false;
                                        if (_rec.extendData._alertId) delete _rec.extendData._alertId;
                                        break;
                                    }
                                }
                                localStorage.setItem('klineDrawings', JSON.stringify(_all));
                            }
                        } catch(e) {}
                    }
                };
                // Auto-create server trendline alert
                var pts = d.points;
                if (pts && pts.length >= 2 && pts[0].timestamp && pts[1].timestamp) {
                    fetch('/api/alerts/trendline', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                        credentials: 'same-origin',
                        body: JSON.stringify({
                            symbol: klineChartSymbol,
                            drawing_id: d.id,
                            p1_ts: pts[0].timestamp,
                            p1_val: pts[0].value,
                            p2_ts: pts[1].timestamp,
                            p2_val: pts[1].value,
                            note: 'Trendline break on ' + klineChartSymbol,
                            frequency: 'once'
                        })
                    })
                    .then(function(r) { return r.json().then(function(j) { return {ok: r.ok, data: j}; }); })
                    .then(function(res) {
                        // Failure FIRST — only commit the watched state on real success.
                        if (res.data && res.data.no_channel) {
                            if (confirm('You need to set up at least one notification channel before creating alerts.\n\nGo to notification settings now?')) {
                                window.location.href = '/settings#notifications';
                            }
                            _revertWatch();
                            return;
                        }
                        if (!res.ok) {
                            // Any other error (e.g. 403 alert-limit) — surface the real
                            // message the same way the price-alert form does, then revert.
                            if (typeof handleLimitHitResponse === 'function' && handleLimitHitResponse(res.data)) {
                                // upgrade modal shown
                            } else {
                                showToast((res.data && res.data.error) || 'Could not create alert');
                            }
                            _revertWatch();
                            return;
                        }
                        if (res.ok && res.data.alert) {
                            d.extendData._alertId = res.data.alert.id;
                            saveKlineDrawing(d);
                            updateDrawerAlerts({fresh:true});
                            updateAlertBadge();
                            showToast('Watching trendline');
                        } else {
                            // 2xx but no alert payload — treat as failure, don't fake success.
                            showToast((res.data && res.data.error) || 'Could not create alert');
                            _revertWatch();
                        }
                    })
                    .catch(function(e) {
                        console.error('Failed to create trendline alert:', e);
                        showToast('Could not create alert');
                        _revertWatch();
                    });
                } else {
                    // Timestamp guard failed — do NOT silently succeed.
                    if (typeof window.carracLog === 'function') window.carracLog('alert', 'trendline_guard_skip', { id: d.id });
                    _revertWatch();
                    showToast('Could not set alert on this trendline');
                }
            } else {
                // Delete server trendline alert
                var alertId = d.extendData._alertId;
                if (alertId) {
                    fetch('/api/alerts/' + alertId, {
                        method: 'DELETE',
                        headers: { 'X-CSRFToken': getCSRFToken() },
                        credentials: 'same-origin'
                    })
                    .then(function() {
                        delete d.extendData._alertId;
                        saveKlineDrawing(d);
                        updateDrawerAlerts({fresh:true});
                        updateAlertBadge();
                    })
                    .catch(function(e) { console.error('Failed to delete trendline alert:', e); });
                }
                showToast('Trendline unwatched');
            }
        }
    } else if (action === 'sell-spot') {
        var d = getKlineDrawingById(overlayId);
        if (d && typeof showSpotSellDialog === 'function') {
            showSpotSellDialog(overlayId, d);
        }
    }
}

function showColorPickerForOverlay(overlayId) {
    var picker = document.getElementById('overlay-color-picker');
    if (!picker) {
        picker = document.createElement('div');
        picker.id = 'overlay-color-picker';
        picker.className = 'overlay-color-picker';
        picker.innerHTML = klineDrawingColors.map(function(c) {
            return '<div class="color-option" data-color="' + c + '" style="background:' + c + '"></div>';
        }).join('');
        document.body.appendChild(picker);

        picker.addEventListener('click', function(e) {
            var colorEl = e.target.closest('[data-color]');
            if (!colorEl) return;
            var newColor = colorEl.getAttribute('data-color');
            var targetId = picker.getAttribute('data-overlay-id');
            if (targetId && klineChart) {
                klineChart.overrideOverlay({
                    id: targetId,
                    styles: {
                        line: { color: newColor },
                        point: _invisiblePointStyle
                    }
                });
                updateDrawingColorInStorage(targetId, newColor);
                // For rects, also update fill color to match
                var d = getKlineDrawingById(targetId);
                if (d && d.name === 'rect') {
                    var opacity = (d.extendData && d.extendData.fillOpacity != null) ? d.extendData.fillOpacity : 0.15;
                    var fillColor = hexToRgba(newColor, opacity);
                    updateKlineDrawingExtendData(targetId, { fillColor: fillColor, fillHex: newColor, fillOpacity: opacity });
                    klineChart.overrideOverlay({ id: targetId, extendData: { fillColor: fillColor, fillHex: newColor, fillOpacity: opacity } });
                }
            }
            picker.classList.remove('visible');
        });
    }

    picker.setAttribute('data-overlay-id', overlayId);
    if (klineContextMenu) {
        picker.style.left = klineContextMenu.style.left;
        picker.style.top = (parseInt(klineContextMenu.style.top) + 80) + 'px';
    }
    picker.classList.add('visible');
}

function showFillColorPicker(overlayId) {
    var existing = document.getElementById('fill-color-picker');
    if (existing) existing.remove();

    var picker = document.createElement('div');
    picker.id = 'fill-color-picker';
    picker.className = 'overlay-color-picker visible';
    // Include a "none" (transparent) option
    picker.innerHTML = '<div class="color-option" data-color="transparent" style="background:repeating-conic-gradient(#808080 0% 25%, transparent 0% 50%) 50% / 8px 8px" title="None"></div>' +
        klineDrawingColors.map(function(c) {
            return '<div class="color-option" data-color="' + c + '" style="background:' + c + '"></div>';
        }).join('');

    if (klineContextMenu) {
        picker.style.left = klineContextMenu.style.left;
        picker.style.top = (parseInt(klineContextMenu.style.top) + 80) + 'px';
    }
    document.body.appendChild(picker);

    picker.addEventListener('click', function(e) {
        var colorEl = e.target.closest('[data-color]');
        if (!colorEl) return;
        var rawColor = colorEl.getAttribute('data-color');
        // Get current opacity from storage
        var d = getKlineDrawingById(overlayId);
        var opacity = (d && d.extendData && d.extendData.fillOpacity != null) ? d.extendData.fillOpacity : 0.15;
        var fillColor = rawColor === 'transparent' ? 'transparent' : hexToRgba(rawColor, opacity);
        var fillHex = rawColor;
        updateKlineDrawingExtendData(overlayId, { fillColor: fillColor, fillHex: fillHex });
        klineChart.overrideOverlay({ id: overlayId, extendData: { fillColor: fillColor, fillHex: fillHex, fillOpacity: opacity } });
        picker.remove();
    });
}

// Minimal single-line note dialog for alert lines (right-click → Add/Edit
// Note). cb(text) fires on Save (empty string clears); Cancel/Escape = no cb.
function _showAlertNoteDialog(currentText, cb) {
    var old = document.getElementById('alert-note-dialog');
    if (old) old.remove();
    var modal = document.createElement('div');
    modal.id = 'alert-note-dialog';
    modal.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:90000;background:#1e222d;border:1px solid #363a45;border-radius:8px;padding:14px 16px;min-width:300px;color:#d1d4dc;font:13px JetBrains Mono,monospace;box-shadow:0 8px 32px rgba(0,0,0,0.5)';
    modal.innerHTML = '<div style="margin-bottom:8px;color:#8b949e;font-size:11px">Alert note (shown on the chart, saved on the alert)</div>' +
        '<input id="alert-note-input" type="text" maxlength="120" style="width:100%;box-sizing:border-box;background:#131722;border:1px solid #363a45;border-radius:4px;color:#d1d4dc;padding:6px 8px;font:13px JetBrains Mono,monospace" />' +
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">' +
        '<button id="alert-note-cancel" style="background:none;border:1px solid #363a45;border-radius:4px;color:#8b949e;padding:4px 12px;cursor:pointer">Cancel</button>' +
        '<button id="alert-note-save" style="background:#2962ff;border:none;border-radius:4px;color:#fff;padding:4px 12px;cursor:pointer">Save</button></div>';
    document.body.appendChild(modal);
    var input = modal.querySelector('#alert-note-input');
    input.value = currentText || '';
    var close = function() { modal.remove(); document.removeEventListener('keydown', onKey, true); };
    var save = function() { var v = input.value; close(); cb(v); };
    function onKey(e) {
        if (e.key === 'Escape') { e.stopPropagation(); close(); }
        if (e.key === 'Enter' && document.activeElement === input) { e.stopPropagation(); save(); }
    }
    modal.querySelector('#alert-note-save').addEventListener('click', save);
    modal.querySelector('#alert-note-cancel').addEventListener('click', close);
    document.addEventListener('keydown', onKey, true);
    setTimeout(function() { try { input.focus(); input.select(); } catch (e) {} }, 30);
}

function showOpacityPicker(overlayId, anchorLeft, anchorTop) {
    // Anchor: explicit coords (floating context bar) beat the context-menu
    // position (right-click path); fall back to a fixed spot.
    var posLeft = (anchorLeft != null) ? anchorLeft + 'px'
        : (klineContextMenu ? klineContextMenu.style.left : '100px');
    var posTop = (anchorTop != null) ? anchorTop + 'px'
        : (klineContextMenu ? (parseInt(klineContextMenu.style.top) + 80) + 'px' : '100px');
    // Defer to next frame to avoid click event conflicts
    setTimeout(function() {
        var existing = document.getElementById('opacity-picker');
        if (existing) existing.remove();

        var picker = document.createElement('div');
        picker.id = 'opacity-picker';
        picker.className = 'overlay-color-picker visible';
        picker.style.width = '180px';
        picker.style.left = posLeft;
        picker.style.top = posTop;
        var opacities = [0.05, 0.1, 0.15, 0.25, 0.4, 0.6, 0.8, 0.9, 1.0];
        picker.innerHTML = opacities.map(function(o) {
            var pct = Math.round(o * 100);
            return '<div class="opacity-option" data-opacity="' + o + '" title="' + pct + '%">' + pct + '%</div>';
        }).join('');
        document.body.appendChild(picker);

        picker.addEventListener('click', function(e) {
            var el = e.target.closest('[data-opacity]');
            if (!el) return;
            var opacity = parseFloat(el.getAttribute('data-opacity'));
            var d = getKlineDrawingById(overlayId);
            if (d) {
                d.opacity = opacity;
                recreateOverlayWithStyles(d);
            }
            picker.remove();
        });
        // Click outside dismisses
        setTimeout(function() {
            document.addEventListener('mousedown', function _dismiss(e) {
                if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('mousedown', _dismiss); }
            });
        }, 50);
    }, 0);
}

function showWidthPicker(overlayId) {
    var existing = document.getElementById('width-picker');
    if (existing) existing.remove();

    var picker = document.createElement('div');
    picker.id = 'width-picker';
    picker.className = 'overlay-color-picker visible';
    picker.style.width = '160px';
    var widths = [1, 2, 3, 4, 5];
    picker.innerHTML = widths.map(function(w) {
        return '<div class="opacity-option" data-width="' + w + '" title="' + w + 'px">' +
            '<span style="display:inline-block;width:30px;height:' + w + 'px;background:#d1d4dc;vertical-align:middle;border-radius:1px"></span> ' + w + 'px</div>';
    }).join('');

    if (klineContextMenu) {
        picker.style.left = klineContextMenu.style.left;
        picker.style.top = (parseInt(klineContextMenu.style.top) + 80) + 'px';
    }
    document.body.appendChild(picker);

    picker.addEventListener('click', function(e) {
        var el = e.target.closest('[data-width]');
        if (!el) return;
        var w = parseInt(el.getAttribute('data-width'));
        klineChart.overrideOverlay({
            id: overlayId,
            styles: { line: { size: w }, point: { borderSize: w } }
        });
        updateDrawingWidthInStorage(overlayId, w);
        picker.remove();
    });
}

function updateDrawingWidthInStorage(drawingId, newWidth) {
    if (!klineChartSymbol) return;
    _pushUndoSnapshot();
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        var drawings = allDrawings[key] || [];
        var updated = null;
        for (var i = 0; i < drawings.length; i++) {
            if (drawings[i].id === drawingId) {
                drawings[i].width = newWidth;
                if (drawings[i].extendData) drawings[i].extendData._width = newWidth;
                updated = drawings[i];
                break;
            }
        }
        allDrawings[key] = drawings;
        localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
        if (updated && typeof DrawingTools !== 'undefined' && DrawingTools.syncDrawingToServer) {
            DrawingTools.syncDrawingToServer(updated, key);
        }
    } catch (e) {
    }
}

function updateDrawingColorInStorage(drawingId, newColor) {
    if (!klineChartSymbol) return;
    _pushUndoSnapshot();
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        var drawings = allDrawings[key] || [];
        var updated = null;
        for (var i = 0; i < drawings.length; i++) {
            if (drawings[i].id === drawingId) {
                drawings[i].color = newColor;
                if (drawings[i].extendData) drawings[i].extendData._color = newColor;
                updated = drawings[i];
                break;
            }
        }
        allDrawings[key] = drawings;
        localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
        if (updated && typeof DrawingTools !== 'undefined' && DrawingTools.syncDrawingToServer) {
            DrawingTools.syncDrawingToServer(updated, key);
        }
    } catch (e) {
    }
}

// ---- Copy / Paste / Duplicate ----

// Human-readable drawing type names for toasts
var _drawingTypeLabel = {
    horizontalStraightLine: 'horizontal line', horizontalRayLine: 'horizontal line', horizontalSegment: 'horizontal line',
    priceLine: 'price line', straightLine: 'trendline', rayLine: 'ray line', segment: 'segment',
    boundedChannel: 'channel', parallelStraightLine: 'channel', parallelChannel: 'channel', priceChannelLine: 'channel',
    fibonacciRetrace: 'fibonacci', fibonacciInfinite: 'fibonacci', fibonacciLine: 'fibonacci',
    fibonacciSegment: 'fibonacci', fibonacciCircle: 'fibonacci', fibonacciExtension: 'fibonacci',
    rect: 'rectangle', gannBox: 'gann box', textAnnotation: 'text', simpleAnnotation: 'annotation',
    arrow: 'arrow', pitchfork: 'pitchfork', elliottWave: 'elliott wave',
    anchoredVwap: 'anchored VWAP', alertLine: 'alert line', freehand: 'freehand',
    scenarioPath: 'scenario',
    arrowUpMarker: 'arrow up', arrowDownMarker: 'arrow down', flagPin: 'flag',
    circleMarker: 'circle', callout: 'callout', verticalStraightLine: 'vertical line'
};

function _getDrawingLabel(name) {
    return _drawingTypeLabel[name] || name;
}

/**
 * Copy the currently selected drawing (or a specific overlay) to clipboard.
 * Returns true on success, false if not copyable.
 */
function copySelectedDrawing(overlayId) {
    var id = overlayId || klineSelectedOverlayId;
    if (!id || !klineChartSymbol) return false;
    var drawing = getKlineDrawingById(id);
    if (!drawing || !drawing.name) return false;
    if (!_isDrawingCopyable(drawing.name)) {
        showToast('Cannot copy ' + _getDrawingLabel(drawing.name));
        return false;
    }
    _drawingClipboard = {
        drawing: JSON.parse(JSON.stringify(drawing)),
        symbol: klineChartSymbol
    };
    showToast('Copied ' + _getDrawingLabel(drawing.name));
    return true;
}

/**
 * Get the center of the visible chart area as { dataIndex, value } (raw price, not log).
 */
function _getVisibleCenter() {
    if (!klineChart) return null;
    var container = document.getElementById('kline-chart-container');
    if (!container) return null;
    var rect = container.getBoundingClientRect();
    var centerX = rect.width / 2;
    var centerY = rect.height / 2;
    var coord = klineChart.convertFromPixel({ x: centerX, y: centerY }, { paneId: 'candle_pane' });
    if (!coord || coord.dataIndex == null || coord.value == null) return null;
    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
    var rawValue = (_isLog && coord.value != null) ? Math.exp(coord.value) : coord.value;
    return { dataIndex: coord.dataIndex, value: rawValue };
}

/**
 * Get the center point of a drawing's points (average timestamp & value).
 */
function _getDrawingCenter(points) {
    if (!points || points.length === 0) return null;
    var sumTs = 0, sumVal = 0, countTs = 0;
    for (var i = 0; i < points.length; i++) {
        if (points[i].timestamp) { sumTs += points[i].timestamp; countTs++; }
        if (points[i].value != null) sumVal += points[i].value;
    }
    return {
        timestamp: countTs > 0 ? sumTs / countTs : 0,
        value: sumVal / points.length
    };
}

/**
 * Strip side-effect metadata from a cloned drawing's extendData.
 */
function _stripSideEffects(extendData) {
    if (!extendData) return {};
    var clean = Object.assign({}, extendData);
    delete clean._alertId;
    delete clean._journalId;
    delete clean.watch;
    delete clean._locked;
    delete clean._hoverTick;
    return clean;
}

/**
 * Paste drawing from clipboard at the center of the visible chart.
 */
function pasteDrawing() {
    if (!_drawingClipboard || !klineChart || !klineChartSymbol) return false;
    if (_drawingClipboard.symbol !== klineChartSymbol) {
        showToast('Clipboard is for ' + _drawingClipboard.symbol);
        return false;
    }
    var src = _drawingClipboard.drawing;
    if (!src || !src.points || src.points.length === 0) return false;

    var center = _getVisibleCenter();
    if (!center) return false;

    // Compute drawing center and offset
    var drawCenter = _getDrawingCenter(src.points);
    if (!drawCenter) return false;

    // Resolve drawing center timestamp → dataIndex for offset calculation
    var data = klineChart.getDataList();
    var drawCenterIdx = 0;
    if (drawCenter.timestamp && data && data.length) {
        drawCenterIdx = timestampToDataIndex(data, drawCenter.timestamp);
    }
    var idxOffset = center.dataIndex - drawCenterIdx;
    var valOffset = center.value - drawCenter.value;

    // Clone and offset points
    var newPoints = src.points.map(function(p) {
        var np = Object.assign({}, p);
        if (np.timestamp && data && data.length) {
            var origIdx = timestampToDataIndex(data, np.timestamp);
            var newIdx = Math.max(0, Math.min(origIdx + idxOffset, data.length - 1));
            np.timestamp = data[newIdx].timestamp;
        }
        if (np.value != null) np.value = np.value + valOffset;
        return np;
    });

    // Build new drawing record (no id — KLineChart generates one)
    var newDrawing = {
        name: src.name,
        points: newPoints,
        color: src.color,
        width: src.width,
        extendData: _stripSideEffects(src.extendData)
    };
    // Shift the position box's right extent with the points (see duplicate path).
    if (newDrawing.extendData && typeof newDrawing.extendData.rightTs === 'number' && data && data.length) {
        var rIdxP = timestampToDataIndex(data, newDrawing.extendData.rightTs);
        var rNewP = Math.max(0, Math.min(rIdxP + idxOffset, data.length - 1));
        newDrawing.extendData.rightTs = data[rNewP].timestamp;
    }
    if (src.opacity !== undefined) newDrawing.opacity = src.opacity;
    if (src.label) newDrawing.label = src.label;
    if (src.paneId) newDrawing.paneId = src.paneId;

    var newId = createOverlayFromDrawing(newDrawing);
    if (!newId) return false;

    newDrawing.id = newId;
    saveKlineDrawing(newDrawing);

    // Select the pasted overlay
    klineSelectedOverlayId = newId;
    try { klineChart.overrideOverlay({ id: newId, mode: 'normal' }); } catch(e) {}

    showToast('Pasted ' + _getDrawingLabel(src.name));
    return true;
}

/**
 * Duplicate the selected drawing (or specific overlay) with a small offset.
 * Does NOT touch the clipboard.
 *
 * opts (optional):
 *   { barOffset: 0, valOffset: 0, silent: true }  — in-place clone, no toast.
 *   Default (no opts): barOffset=5, valOffset=1.5%, toast shown.
 *
 * Returns the new overlay id on success (truthy), or false on failure.
 * Existing callers that treat the return as truthy (context menu, keyboard shortcuts)
 * are unaffected — a non-empty string is truthy.
 */
function duplicateSelectedDrawing(overlayId, opts) {
    var id = overlayId || klineSelectedOverlayId;
    if (!id || !klineChart || !klineChartSymbol) return false;
    var drawing = getKlineDrawingById(id);
    if (!drawing || !drawing.name) return false;
    if (!_isDrawingCopyable(drawing.name)) {
        if (!opts || !opts.silent) showToast('Cannot duplicate ' + _getDrawingLabel(drawing.name));
        return false;
    }

    var src = JSON.parse(JSON.stringify(drawing));
    var data = klineChart.getDataList();

    // Resolve offsets from opts or apply legacy defaults
    var barOffset = (opts && opts.barOffset != null) ? opts.barOffset : 5;
    var valOffset;
    if (opts && opts.valOffset != null) {
        valOffset = opts.valOffset;
    } else {
        // 1.5% price offset (upward) — legacy default
        var sampleVal = (src.points[0] && src.points[0].value) || 1;
        valOffset = Math.abs(sampleVal) * 0.015;
    }
    var silent = opts && opts.silent;

    var newPoints = src.points.map(function(p) {
        var np = Object.assign({}, p);
        if (np.timestamp && data && data.length) {
            var origIdx = timestampToDataIndex(data, np.timestamp);
            var newIdx = Math.min(origIdx + barOffset, data.length - 1);
            np.timestamp = data[Math.max(0, newIdx)].timestamp;
        }
        if (np.value != null) np.value = np.value + valOffset;
        return np;
    });

    var newDrawing = {
        name: src.name,
        points: newPoints,
        color: src.color,
        width: src.width,
        extendData: _stripSideEffects(src.extendData)
    };
    // Shift the position box's right extent with the points, or a narrow box
    // duplicates inverted (entry landing right of the old rightTs).
    // When barOffset is 0 (in-place clone), preserve rightTs exactly.
    if (newDrawing.extendData && typeof newDrawing.extendData.rightTs === 'number' && data && data.length) {
        if (barOffset === 0) {
            // Preserve exactly — no shift
        } else {
            var rIdxDup = timestampToDataIndex(data, newDrawing.extendData.rightTs);
            var rNewDup = Math.min(rIdxDup + barOffset, data.length - 1);
            newDrawing.extendData.rightTs = data[Math.max(0, rNewDup)].timestamp;
        }
    }
    if (src.opacity !== undefined) newDrawing.opacity = src.opacity;
    if (src.label) newDrawing.label = src.label;
    if (src.paneId) newDrawing.paneId = src.paneId;

    var newId = createOverlayFromDrawing(newDrawing);
    if (!newId) return false;

    newDrawing.id = newId;
    saveKlineDrawing(newDrawing);

    klineSelectedOverlayId = newId;
    try { klineChart.overrideOverlay({ id: newId, mode: 'normal' }); } catch(e) {}

    if (!silent) showToast('Duplicated ' + _getDrawingLabel(src.name));
    return newId;
}

// ── Ctrl+drag clone — custom drag ─────────────────────────────
// The engine cannot drag a just-created clone (it starts drags from its own
// hover state, which references the original), so the clone is moved by a
// custom drag state like positions/channel-corners: convertFromPixel deltas
// in {bar, chart-space value}, applied via overrideOverlay each mousemove.

var _ctrlCloneDrag = null; // { origId, cloneId, paneId, startDataIndex, startValue, origPoints, moved }

/**
 * Clone the drawing under the cursor in place and arm the custom clone drag.
 * Returns true when armed (caller stops the event), false to fall through.
 */
function _armCtrlCloneDrag(srcOverlayId, localX, localY, container, e) {
    var srcDrawing = getKlineDrawingById(srcOverlayId);
    if (!srcDrawing || !_isDrawingCopyable(srcDrawing.name)) return false;
    var sA = null;
    try { var sArr = klineChart.getOverlayById(srcOverlayId); sA = Array.isArray(sArr) ? sArr[0] : sArr; } catch(err) {}
    if (sA && sA.lock === true) return false;

    var paneId = (sA && sA.paneId) || srcDrawing.paneId || 'candle_pane';
    var anchor;
    try { anchor = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: paneId }); } catch(err) {}
    if (!anchor || anchor.dataIndex == null || anchor.value == null) return false;

    var cloneId = duplicateSelectedDrawing(srcOverlayId, { barOffset: 0, valOffset: 0, silent: true });
    if (!cloneId) return false;

    var cA = null;
    try { var cArr = klineChart.getOverlayById(cloneId); cA = Array.isArray(cArr) ? cArr[0] : cArr; } catch(err) {}
    if (!cA || !cA.points || !cA.points.length || cA.points.some(function(p) { return p.dataIndex == null; })) {
        // Clone overlay didn't materialize with usable geometry — undo and bail
        try { klineChart.removeOverlay(cloneId); } catch(err) {}
        removeKlineDrawingFromStorage(cloneId);
        klineSelectedOverlayId = srcOverlayId;
        return false;
    }

    e.stopPropagation();
    e.preventDefault();
    klineSelectedOverlayId = cloneId;
    _ctrlCloneDrag = {
        origId: srcOverlayId,
        cloneId: cloneId,
        paneId: paneId,
        startDataIndex: anchor.dataIndex,
        startValue: anchor.value,
        // live points: chart-space values + resolved dataIndex
        origPoints: cA.points.map(function(p) { return { dataIndex: p.dataIndex, value: p.value }; }),
        moved: false,
        downX: e.clientX,
        downY: e.clientY
    };
    _forceCanvasCursor(container, 'grabbing');
    return true;
}

function _handleCtrlCloneDrag(clientX, clientY, container) {
    if (!_ctrlCloneDrag || !klineChart) return;
    var rect = container.getBoundingClientRect();
    var localX = clientX - rect.left;
    var localY = clientY - rect.top;
    var cur;
    try { cur = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: _ctrlCloneDrag.paneId }); } catch(e) { return; }
    if (!cur || cur.dataIndex == null || cur.value == null) return;

    // 3px screen-distance threshold: do NOT apply any translation until the cursor
    // has moved >3px from the mousedown point. This prevents real mice (1-2px jitter
    // on click) from marking `moved=true` and saving an invisible in-place clone.
    var _ccsdx = clientX - _ctrlCloneDrag.downX;
    var _ccsdy = clientY - _ctrlCloneDrag.downY;
    if (Math.sqrt(_ccsdx * _ccsdx + _ccsdy * _ccsdy) <= 3) return;

    var deltaBars = cur.dataIndex - _ctrlCloneDrag.startDataIndex;
    var deltaVal = cur.value - _ctrlCloneDrag.startValue;
    var dataList = klineChart.getDataList();

    // Abort this move if any point would leave the data range (no distortion)
    var op = _ctrlCloneDrag.origPoints;
    for (var i = 0; i < op.length; i++) {
        var nd = op[i].dataIndex + deltaBars;
        if (nd < 0 || nd >= dataList.length) return;
    }
    var newPoints = op.map(function(p) {
        var nd = p.dataIndex + deltaBars;
        // HARD RULE: write BOTH dataIndex and timestamp
        return { dataIndex: nd, timestamp: dataList[nd].timestamp, value: p.value + deltaVal };
    });
    try { klineChart.overrideOverlay({ id: _ctrlCloneDrag.cloneId, points: newPoints }); } catch(e) {}
    _ctrlCloneDrag.moved = true;
}

/**
 * Finalize on mouseup: persist a moved clone through the standard save path;
 * remove a never-moved clone (Ctrl+click without drag) — it sits invisibly on
 * the original, is storage/sync garbage, and would collide with Ctrl+click
 * multi-select.
 */
function _finalizeCtrlCloneDrag() {
    if (!_ctrlCloneDrag) return;
    var state = _ctrlCloneDrag;
    _ctrlCloneDrag = null;
    try {
        if (!state.moved) {
            try { klineChart.removeOverlay(state.cloneId); } catch(e) {}
            removeKlineDrawingFromStorage(state.cloneId);
            if (klineSelectedOverlayId === state.cloneId) klineSelectedOverlayId = state.origId;
            // Ctrl+click without drag → toggle origId into multi-select set
            _msToggle(state.origId);
            return;
        }
        var cA = klineChart.getOverlayById(state.cloneId);
        var c = Array.isArray(cA) ? cA[0] : cA;
        var drawing = getKlineDrawingById(state.cloneId);
        if (c && c.points && drawing) {
            drawing.points = resolvePointTimestamps(c.points);
            saveKlineDrawing(drawing);
        }
    } catch(e) {}
}

// ── Arrow-key nudge of the selected drawing ───────────────────

/**
 * Nudge the currently selected drawing by arrow key.
 * Exported on window context for testability via ctx._nudgeSelectedDrawing(key, shift, target).
 *
 * @param {string}  key    — 'ArrowLeft' | 'ArrowRight' | 'ArrowUp' | 'ArrowDown'
 * @param {boolean} shift  — true = ×10 multiplier
 * @param {object}  [target] — optional { tagName } to simulate a different focus target
 */
var _nudgeDebounceTimer = null;

function _nudgeSelectedDrawing(key, shift, target) {
    if (!klineChart) return;

    // Guard: bail if focus is in a text-entry element — check BOTH the event
    // target and the focused element (they can diverge; either one typing wins)
    var _guards = [target, (typeof document !== 'undefined' ? document.activeElement : null)];
    for (var _gi = 0; _gi < _guards.length; _gi++) {
        var _tgt = _guards[_gi];
        if (!_tgt) continue;
        var _tag = (_tgt.tagName || '').toUpperCase();
        if (_tag === 'INPUT' || _tag === 'TEXTAREA' || _tag === 'SELECT') return;
        if (_tgt.isContentEditable) return;
    }

    // when multi-select set is non-empty, nudge ALL members together
    if (klineMultiSelectIds.length > 1) {
        _nudgeMultiSelect(key, shift);
        return;
    }

    if (!klineSelectedOverlayId) return;

    var id = klineSelectedOverlayId;
    var ovArr;
    try { ovArr = klineChart.getOverlayById(id); } catch(e) {}
    var ov = Array.isArray(ovArr) ? ovArr[0] : ovArr;
    if (!ov) return;
    // Bail if locked
    if (ov.lock === true) return;
    if (!ov.points || ov.points.length === 0) return;

    var data = klineChart.getDataList();
    if (!data || !data.length) return;

    var n = shift ? 10 : 1;
    var newPoints;

    if (key === 'ArrowLeft' || key === 'ArrowRight') {
        // Horizontal nudge: ±N bars
        var dir = (key === 'ArrowRight') ? 1 : -1;
        var delta = dir * n;

        // Check all points first — abort if ANY would clamp at array edge
        for (var i = 0; i < ov.points.length; i++) {
            var di = ov.points[i].dataIndex;
            if (di == null) return; // live points without dataIndex — can't nudge
            var newDi = di + delta;
            if (newDi < 0 || newDi >= data.length) return; // would clamp — abort whole nudge
        }

        newPoints = ov.points.map(function(p) {
            var newDi = p.dataIndex + delta;
            return {
                dataIndex: newDi,
                timestamp: data[newDi].timestamp,
                value: p.value
            };
        });

        // Build override object
        var overrideObj = { id: id, points: newPoints };

        // For positions: also shift rightTs by the same bar count
        var ext = ov.extendData || {};
        if (typeof ext.rightTs === 'number' && isFinite(ext.rightTs)) {
            var rIdxN = timestampToDataIndex(data, ext.rightTs);
            var rNewN = rIdxN + delta;
            // Clamp rightTs within data bounds
            rNewN = Math.max(0, Math.min(rNewN, data.length - 1));
            overrideObj.extendData = Object.assign({}, ext, { rightTs: data[rNewN].timestamp });
        }

        try { klineChart.overrideOverlay(overrideObj); } catch(e) {}

    } else if (key === 'ArrowUp' || key === 'ArrowDown') {
        // Vertical nudge: ±delta value (1 chart-pixel equivalent per step)
        // Compute the pixel delta using the first point as the reference
        var paneId = ov.paneId || 'candle_pane';
        var refPt = ov.points[0];
        var refPx;
        try { refPx = klineChart.convertToPixel({ dataIndex: refPt.dataIndex, value: refPt.value }, { paneId: paneId }); } catch(e) {}
        if (!refPx) return;

        // Up arrow = decrease y pixel = increase value
        var pyDir = (key === 'ArrowUp') ? -1 : 1;
        var newPy = refPx.y + pyDir * n;
        var newCoord;
        try { newCoord = klineChart.convertFromPixel({ x: refPx.x, y: newPy }, { paneId: paneId }); } catch(e) {}
        if (!newCoord || newCoord.value == null) return;

        var valueDelta = newCoord.value - refPt.value;
        if (valueDelta === 0) return;

        newPoints = ov.points.map(function(p) {
            return {
                dataIndex: p.dataIndex,
                timestamp: p.timestamp,
                value: p.value + valueDelta
            };
        });

        try { klineChart.overrideOverlay({ id: id, points: newPoints }); } catch(e) {}

    } else {
        return; // Not an arrow key we handle
    }

    // Debounced persist: save the final position after the last arrow press
    if (_nudgeDebounceTimer) {
        try { clearTimeout(_nudgeDebounceTimer); } catch(e) {}
    }
    _nudgeDebounceTimer = setTimeout(function() {
        _nudgeDebounceTimer = null;
        try {
            var ovs2 = klineChart.getOverlayById(id);
            var ov2 = Array.isArray(ovs2) ? ovs2[0] : ovs2;
            if (ov2) {
                var drawing = getKlineDrawingById(id);
                if (drawing) {
                    drawing.points = resolvePointTimestamps(ov2.points);
                    // Persist rightTs if updated (positions)
                    if (ov2.extendData && typeof ov2.extendData.rightTs === 'number') {
                        drawing.extendData = Object.assign(drawing.extendData || {}, { rightTs: ov2.extendData.rightTs });
                    }
                    saveKlineDrawing(drawing);
                }
            }
        } catch(e) {}
    }, 500);
}

/**
 * Nudge all members of the multi-select set together.
 * Abort ALL if any member would clamp at the data boundary.
 */
function _nudgeMultiSelect(key, shift) {
    if (!klineChart || !klineMultiSelectIds.length) return;
    var data = klineChart.getDataList();
    if (!data || !data.length) return;
    var n = shift ? 10 : 1;
    var ids = klineMultiSelectIds.slice();

    // Fetch live overlays for all members
    var snapshots = [];
    for (var _mi = 0; _mi < ids.length; _mi++) {
        var _ovArr;
        try { _ovArr = klineChart.getOverlayById(ids[_mi]); } catch(e) {}
        var _ov = Array.isArray(_ovArr) ? _ovArr[0] : _ovArr;
        if (!_ov || _ov.lock === true || !_ov.points || !_ov.points.length) continue;
        snapshots.push({ id: ids[_mi], ov: _ov });
    }
    if (!snapshots.length) return;

    if (key === 'ArrowLeft' || key === 'ArrowRight') {
        var dir = (key === 'ArrowRight') ? 1 : -1;
        var delta = dir * n;
        // Pre-check: abort ALL if ANY point of ANY member would go out of bounds
        for (var _ci = 0; _ci < snapshots.length; _ci++) {
            var _sov = snapshots[_ci].ov;
            for (var _pi = 0; _pi < _sov.points.length; _pi++) {
                var _di = _sov.points[_pi].dataIndex;
                if (_di == null) return;
                if (_di + delta < 0 || _di + delta >= data.length) return;
            }
        }
        // Apply to all
        for (var _ai = 0; _ai < snapshots.length; _ai++) {
            var _asov = snapshots[_ai].ov;
            var _aid = snapshots[_ai].id;
            var _newPts = _asov.points.map(function(p) {
                var _ndi = p.dataIndex + delta;
                return { dataIndex: _ndi, timestamp: data[_ndi].timestamp, value: p.value };
            });
            var _aOvr = { id: _aid, points: _newPts };
            var _aExt = _asov.extendData || {};
            if (typeof _aExt.rightTs === 'number' && isFinite(_aExt.rightTs)) {
                var _rIdx = timestampToDataIndex(data, _aExt.rightTs);
                var _rNew = Math.max(0, Math.min(_rIdx + delta, data.length - 1));
                _aOvr.extendData = Object.assign({}, _aExt, { rightTs: data[_rNew].timestamp });
            }
            try { klineChart.overrideOverlay(_aOvr); } catch(e) {}
        }
    } else if (key === 'ArrowUp' || key === 'ArrowDown') {
        // Use first member's first point as vertical reference
        var _refSov = snapshots[0].ov;
        var _paneId = _refSov.paneId || 'candle_pane';
        var _refPt = _refSov.points[0];
        var _refPx;
        try { _refPx = klineChart.convertToPixel({ dataIndex: _refPt.dataIndex, value: _refPt.value }, { paneId: _paneId }); } catch(e) {}
        if (!_refPx) return;
        var _pyDir = (key === 'ArrowUp') ? -1 : 1;
        var _newPy = _refPx.y + _pyDir * n;
        var _newCoord;
        try { _newCoord = klineChart.convertFromPixel({ x: _refPx.x, y: _newPy }, { paneId: _paneId }); } catch(e) {}
        if (!_newCoord || _newCoord.value == null) return;
        var _valueDelta = _newCoord.value - _refPt.value;
        if (_valueDelta === 0) return;
        for (var _vi = 0; _vi < snapshots.length; _vi++) {
            var _vsov = snapshots[_vi].ov;
            var _vid = snapshots[_vi].id;
            var _vPts = _vsov.points.map(function(p) {
                return { dataIndex: p.dataIndex, timestamp: p.timestamp, value: p.value + _valueDelta };
            });
            try { klineChart.overrideOverlay({ id: _vid, points: _vPts }); } catch(e) {}
        }
    } else {
        return;
    }

    // Debounced persist for all members
    if (_nudgeDebounceTimer) {
        try { clearTimeout(_nudgeDebounceTimer); } catch(e) {}
    }
    var _persistIds = ids.slice();
    _nudgeDebounceTimer = setTimeout(function() {
        _nudgeDebounceTimer = null;
        for (var _si = 0; _si < _persistIds.length; _si++) {
            var _pid = _persistIds[_si];
            try {
                var _povArr = klineChart.getOverlayById(_pid);
                var _pov = Array.isArray(_povArr) ? _povArr[0] : _povArr;
                if (_pov) {
                    var _pdrw = getKlineDrawingById(_pid);
                    if (_pdrw) {
                        _pdrw.points = resolvePointTimestamps(_pov.points);
                        if (_pov.extendData && typeof _pov.extendData.rightTs === 'number') {
                            _pdrw.extendData = Object.assign(_pdrw.extendData || {}, { rightTs: _pov.extendData.rightTs });
                        }
                        saveKlineDrawing(_pdrw);
                    }
                }
            } catch(e) {}
        }
    }, 500);
}

// Register ONE document-level keydown listener for arrow nudge + multi-select keys.
// Runs at module init time (when _initOverlayHoverDetection is called or module is loaded).
(function() {
    if (typeof document === 'undefined') return;
    var _nudgeKeyDown = function(e) {
        var key = e.key;
        // Guard: bail if focus is in a text-entry element — check BOTH the
        // event target and the focused element (either one typing wins)
        var _gList = [e.target, document.activeElement];
        for (var _gi = 0; _gi < _gList.length; _gi++) {
            var _tgt = _gList[_gi];
            if (!_tgt) continue;
            var _tag = (_tgt.tagName || '').toUpperCase();
            if (_tag === 'INPUT' || _tag === 'TEXTAREA' || _tag === 'SELECT') return;
            if (_tgt.isContentEditable) return;
        }

        // Escape clears the multi-select set
        if (key === 'Escape' && klineMultiSelectIds.length) {
            _msClear();
            return;
        }

        // Delete/Backspace with multi-select set → group delete
        if ((key === 'Delete' || key === 'Backspace') && klineMultiSelectIds.length > 1) {
            var _delIds = klineMultiSelectIds.slice();
            _msClear();
            for (var _di = 0; _di < _delIds.length; _di++) {
                try { handleContextMenuAction('delete', _delIds[_di]); } catch(e) {}
            }
            e.preventDefault();
            return;
        }

        // Arrow nudge (single or multi-select)
        if (key !== 'ArrowLeft' && key !== 'ArrowRight' && key !== 'ArrowUp' && key !== 'ArrowDown') return;
        if (!klineSelectedOverlayId && !klineMultiSelectIds.length) return;
        e.preventDefault();
        _nudgeSelectedDrawing(key, e.shiftKey, e.target);
    };
    _nudgeKeyDown._isNudgeHandler = true;
    document.addEventListener('keydown', _nudgeKeyDown);
})();

/**
 * Show a chart-level context menu (right-click on empty area) with Paste option.
 */
function showChartContextMenu(x, y) {
    if (klineContextMenu) klineContextMenu.remove();
    klineContextMenu = document.createElement('div');
    klineContextMenu.className = 'drawing-context-menu';
    klineContextMenu.innerHTML =
        '<div class="ctx-menu-item" data-action="paste">' + _ico.paste + ' Paste</div>';
    document.body.appendChild(klineContextMenu);
    klineContextMenu.addEventListener('click', function(e) {
        var item = e.target.closest('[data-action]');
        if (!item) return;
        if (item.getAttribute('data-action') === 'paste') pasteDrawing();
        hideDrawingContextMenu();
    });
    klineContextMenuOverlayId = null;
    var menuW = 140, menuH = 40;
    klineContextMenu.style.left = Math.min(x, window.innerWidth - menuW - 10) + 'px';
    klineContextMenu.style.top = Math.min(y, window.innerHeight - menuH - 10) + 'px';
    klineContextMenu.classList.add('visible');
}

// Load drawings from localStorage
// Migrate old per-timeframe drawing keys to asset-only keys (one-time)
function migrateKlineDrawingsToAssetKeys() {
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var migrated = {};
        var needsMigration = false;
        var knownTfs = ['1h', '4h', '1D', '3D', '1W', '1M'];

        for (var key in all) {
            var lastUs = key.lastIndexOf('_');
            if (lastUs > 0) {
                var possibleTf = key.substring(lastUs + 1);
                if (knownTfs.indexOf(possibleTf) !== -1) {
                    // Old format: BTC_1D or BTC_SPX_4h → merge into asset key
                    var asset = key.substring(0, lastUs);
                    if (!migrated[asset]) migrated[asset] = [];
                    (all[key] || []).forEach(function(d) {
                        // Avoid duplicates by checking id
                        var exists = migrated[asset].some(function(x) { return x.id === d.id; });
                        if (!exists) migrated[asset].push(d);
                    });
                    needsMigration = true;
                    continue;
                }
            }
            // Already asset-only key — keep as-is
            if (!migrated[key]) migrated[key] = [];
            (all[key] || []).forEach(function(d) {
                var exists = migrated[key].some(function(x) { return x.id === d.id; });
                if (!exists) migrated[key].push(d);
            });
        }

        if (needsMigration) {
            localStorage.setItem('klineDrawings', JSON.stringify(migrated));
        }
    } catch (e) {
    }
}

// ── Unified drawing helpers ──────────────────────────────────────────

// Migrate legacy drawing names to current overlay names (mutates in place)
function migrateDrawingName(d) {
    if (!d || !d.name) return d;
    // Old parallel channel variants → boundedChannel
    if (d.name === 'parallelStraightLine' || d.name === 'parallelChannel') {
        d.name = 'boundedChannel';
    }
    // Old separate long/short position → unified position with swapped point order
    // Old order: [Entry, TP, SL] → New order: [Entry, SL, TP]
    // The one-click tools REUSE the longPosition/shortPosition names
    // but already save the new order, so the name alone can't identify a legacy
    // drawing. Only swap when the point order contradicts the name's direction
    // (long: SL must sit below entry; short: above) — otherwise every freshly
    // drawn position got its SL/TP flipped on the next drawings reload and
    // rendered as the opposite side.
    if ((d.name === 'longPosition' || d.name === 'shortPosition') && d.points && d.points.length >= 3) {
        var _entryV = d.points[0] ? d.points[0].value : null;
        var _slotV = d.points[1] ? d.points[1].value : null;
        var _legacyOrder = _entryV != null && _slotV != null &&
            (d.name === 'longPosition' ? _slotV > _entryV : _slotV < _entryV);
        if (_legacyOrder) {
            var tmp = d.points[1];
            d.points[1] = d.points[2];
            d.points[2] = tmp;
            d.name = 'position';
        }
    }
    return d;
}

// Create a KLineChart overlay from a localStorage-format drawing object.
// Returns the overlay ID on success, or null if skipped.
// d: { name, id, points, color, width, opacity, extendData, paneId }
// paneIdOverride: force a specific pane (used by loadKlineDrawingsForPane)
function createOverlayFromDrawing(d, paneIdOverride) {
    if (!klineChart || !d || !d.name || !d.points || d.points.length === 0) return null;
    // Freehand is rendered by its own native draw callback, not as an overlay
    if (d.name === 'freehand') return null;

    var color = d.color || '#3fb950';
    var width = d.width || 1;
    var opacity = d.opacity !== undefined ? d.opacity : 1.0;
    var lineColor = opacity < 1.0 ? hexToRgba(color, opacity) : color;

    var overlayOpts = {
        name: d.name,
        id: d.id,
        points: snapPointsToData(d.points),
        styles: {
            line: { color: lineColor, size: width },
            point: _invisiblePointStyle
        }
    };

    // Restore extendData
    if (d.extendData) {
        overlayOpts.extendData = d.extendData;
        // Transform intermediate TPs from raw to log space for chart rendering
        var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
        if (_isLog && d.extendData.tps && d.extendData.tps.length) {
            overlayOpts.extendData = Object.assign({}, d.extendData);
            overlayOpts.extendData.tps = d.extendData.tps.map(function(v) { return (v > 0) ? Math.log(v) : v; });
        }
    }

    // Ensure _color/_width in extendData for custom overlay renderers that read it
    if (!overlayOpts.extendData) overlayOpts.extendData = {};
    if (!overlayOpts.extendData._color) overlayOpts.extendData._color = lineColor;
    if (!overlayOpts.extendData._width) overlayOpts.extendData._width = width;

    // Lock: spot stays always-locked (transient buy marker). Positions are
    // editable on creation and lock ON DEMAND via right-click (persisted as
    // _locked) — do NOT force-lock them here, or every candle-close reload
    // re-freezes a position the instant the user draws it.
    if (d.name === 'spot') {
        overlayOpts.lock = true;
    }
    if (d.extendData && d.extendData._locked) {
        overlayOpts.lock = true;
    }

    // Route to correct pane
    var targetPane = paneIdOverride || d.paneId || 'candle_pane';
    if (!paneIdOverride) {
        // Only check active panes when not explicitly overridden
        var currentPanes = getActivePaneIds();
        if (targetPane !== 'candle_pane' && currentPanes.indexOf(targetPane) === -1) {
            return null; // Target pane not active
        }
    }

    try {
        var _createdId = klineChart.createOverlay(Object.assign(overlayOpts, getOverlayCallbacks()), targetPane);
        // Master hide-all is view-only state — overlays (re)created while it's
        // on (symbol switch, reload, server sync) must come up hidden.
        if (_createdId && _allDrawingsHidden) {
            try { klineChart.overrideOverlay({ id: d.id, visible: false }); } catch(e2) {}
        }
        return _createdId;
    } catch (e) {
        return null;
    }
}

// ── Drawing loaders ──────────────────────────────────────────────────

function loadKlineDrawings() {
    if (!klineChart || !klineChartSymbol) return;
    // symbol switch clears the multi-select set
    _msClear();

    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        var drawings = allDrawings[key];

        var needsResave = false;
        if (drawings && drawings.length > 0) {
            // Read z_order from metadata into drawing.zOrder for hit-test use
            drawings.forEach(function(d) {
                if (d.zOrder == null && d.extendData && d.extendData.z_order != null) {
                    d.zOrder = d.extendData.z_order;
                }
            });
            // Sort ascending by zOrder (missing → 0) so KLineChart creates overlays
            // in that order — creation order = paint order = lowest zOrder painted first
            // (underneath), highest painted last (on top). Stable sort: array order as tiebreak.
            drawings = drawings.slice().sort(function(a, b) {
                var az = (a.zOrder != null) ? a.zOrder : 0;
                var bz = (b.zOrder != null) ? b.zOrder : 0;
                return az - bz;
            });
            drawings.forEach(function(d) {
                if (!d.name || !d.points || d.points.length === 0) return;
                // hidden drawings stay in storage but are not rendered
                if (d.extendData && d.extendData._hidden) return;
                migrateDrawingName(d);
                // Migrate value-only points: re-save with timestamps so fix is permanent
                var hadMissingTs = d.points.some(function(p) { return !p.timestamp && p.dataIndex === undefined; });
                if (hadMissingTs) {
                    var snapped = snapPointsToData(d.points);
                    if (snapped.some(function(p) { return !!p.timestamp; })) {
                        d.points = snapped;
                        needsResave = true;
                    }
                }
                createOverlayFromDrawing(d);
            });
            if (needsResave) {
                allDrawings[key] = drawings;
                localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
            }
        }
    } catch (e) {
    }
    // update objects panel summary + re-render if tab is open
    if (typeof _refreshObjectsPanelSummary === 'function') _refreshObjectsPanelSummary();
    if (typeof document !== 'undefined' && typeof document.getElementById === 'function') {
        var _objSecL = document.getElementById('drawer-section-objects');
        if (_objSecL && _objSecL.style && _objSecL.style.display !== 'none' && typeof renderDrawingsObjectsPanel === 'function') {
            renderDrawingsObjectsPanel();
        }
    }
}

// Load only drawings belonging to a specific pane (called when indicator toggled on)
function loadKlineDrawingsForPane(targetPaneId) {
    if (!klineChart || !klineChartSymbol || !targetPaneId) return;
    try {
        var allDrawings = _readDrawingsStore();
        var drawings = allDrawings[klineChartSymbol] || [];
        var loaded = 0;
        drawings.forEach(function(d) {
            if (!d.paneId || d.paneId !== targetPaneId) return;
            if (!d.name || !d.points || d.points.length === 0) return;
            if (createOverlayFromDrawing(d, targetPaneId)) loaded++;
        });
    } catch (e) {
    }
}

// Track which symbol's drawings were last fetched (skip redundant fetches on TF switch)
var _drawingsLoadedForSymbol = null;

// Load drawings from server for cross-device sync
function loadDrawingsFromServer(forceRefresh) {
    if (!klineChart || !klineChartSymbol) return;
    // Drawings are symbol-scoped — skip fetch if same symbol (TF switch doesn't change drawings)
    if (!forceRefresh && _drawingsLoadedForSymbol === klineChartSymbol) return;
    _drawingsLoadedForSymbol = klineChartSymbol;
    // T4.5: route through dedupFetch so two callers within 2 s (e.g. a forced
    // render-path refresh racing another trigger) share ONE GET /api/drawings.
    // URL includes the asset → per-asset dedup key, never mixes symbols. The
    // _drawingsLoadedForSymbol guard above still gates non-forced re-fetches.
    var _drawUrl = '/api/drawings?asset=' + encodeURIComponent(klineChartSymbol);
    var _drawFetch = (typeof dedupFetch === 'function')
        ? dedupFetch(_drawUrl, { credentials: 'same-origin' }, 2000)
        : fetch(_drawUrl, { credentials: 'same-origin' });
    _drawFetch
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.success || !data.drawings || data.drawings.length === 0) return;
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var key = klineChartSymbol;
        var localDrawings = allDrawings[key] || [];
        var localIds = {};
        localDrawings.forEach(function(d) { if (d.id) localIds[d.id] = true; });

        var added = 0;
        data.drawings.forEach(function(serverDraw) {
            var meta = serverDraw.metadata || {};
            var localId = meta.local_id;
            // Skip if already in localStorage
            if (localId && localIds[localId]) return;
            // Skip if just deleted in this session (race guard)
            if (localId && window.DrawingTools && window.DrawingTools.isRecentlyDeleted && window.DrawingTools.isRecentlyDeleted(localId)) return;
            var points = meta.points;
            if (!meta.kline_name || !points || points.length === 0) return;
            var drawObj = {
                name: meta.kline_name,
                id: localId || ('srv_' + serverDraw.id),
                points: points,
                color: meta.color || serverDraw.color || '#3fb950',
                width: meta.width || serverDraw.line_width || 1
            };
            if (meta.extend_data && Object.keys(meta.extend_data).length > 0) {
                drawObj.extendData = meta.extend_data;
            }
            if (meta.pane_id && meta.pane_id !== 'candle_pane') {
                drawObj.paneId = meta.pane_id;
            }
            if (meta.opacity !== undefined) {
                drawObj.opacity = meta.opacity;
            }
            // read z_order from metadata (stored there to avoid DB migration)
            if (meta.z_order != null) {
                drawObj.zOrder = meta.z_order;
            } else if (drawObj.extendData && drawObj.extendData.z_order != null) {
                drawObj.zOrder = drawObj.extendData.z_order;
            }
            migrateDrawingName(drawObj);
            localDrawings.push(drawObj);
            localIds[drawObj.id] = true;
            // Render on chart (createOverlayFromDrawing handles pane check internally)
            createOverlayFromDrawing(drawObj);
            added++;
        });
        if (added > 0) {
            allDrawings[key] = localDrawings;
            // Save to localStorage (no hook — sync is per-operation now)
            window._suppressDrawingSync = true;
            localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
            setTimeout(function() { window._suppressDrawingSync = false; }, 300);
        }
    })
    // SILENCE-OK: this merges server drawings pulled in the cross-device
    // GET-sync path into localStorage — a failure here just means this round
    // of resurrection didn't happen; the same GET-sync re-runs on the next
    // asset load / tab return and will pick the drawings up then.
    .catch(function(e) {  });
}

// Add KLineChart drawings to small popup chart (LightweightCharts)
function addKlineDrawingsToPopupChart(symbol, candleSeries, tf) {
    if (!candleSeries) return;

    try {
        var allDrawings = _readDrawingsStore();

        // Drawings are stored per asset (shared across all timeframes)
        var drawings = allDrawings[symbol] || [];

        // Filter horizontal lines for popup chart price lines
        var horizontalLines = drawings.filter(function(d) {
            return d.name === 'horizontalStraightLine' && d.points && d.points.length > 0;
        });

        // Add horizontal lines to the popup chart
        horizontalLines.forEach(function(drawing) {
            if (drawing.points && drawing.points.length > 0) {
                var price = drawing.points[0].value;
                candleSeries.createPriceLine({
                    price: price,
                    color: drawing.color || '#3fb950',
                    lineWidth: drawing.width || 1,
                    lineStyle: 0, // Solid line
                    axisLabelVisible: true,
                    title: ''
                });
            }
        });

        if (horizontalLines.length > 0) {
        }
    } catch (e) {
    }
}

// =============================================================================
// Drawing Toolbar Toggle
// =============================================================================
function updateOhlcOffset() {
    // Multi-chart: per-pane offsets (incl. bottom-row panes the toolbar doesn't
    // overlap) are owned by mcUpdateChromeInsets — delegate and skip the
    // single-chart math, which would wrongly push a bottom-row pane's legend down.
    if (typeof mcIsMultiChart === 'function' && mcIsMultiChart() &&
        typeof window.mcUpdateChromeInsets === 'function') {
        window.mcUpdateChromeInsets();
        return;
    }
    if (!klineChart) return;
    var toolbar = document.querySelector('.adv-chart-toolbar');
    var offset = toolbar ? (toolbar.offsetHeight + 4) : 32;
    klineChart.setStyles({ candle: { tooltip: { offsetTop: offset } } });
}

// Watch toolbar height changes (flex-wrap at narrow viewports)
(function() {
    if (typeof ResizeObserver === 'undefined') return;
    var scheduled = false;
    function onToolbarResize() {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(function() {
            scheduled = false;
            updateOhlcOffset();
            if (typeof _repositionConsensioTable === 'function') _repositionConsensioTable();
        });
    }
    var obs = new MutationObserver(function() {
        var tb = document.querySelector('.adv-chart-toolbar');
        if (tb) {
            obs.disconnect();
            new ResizeObserver(onToolbarResize).observe(tb);
        }
    });
    obs.observe(document.body, { childList: true, subtree: true });
})();

function toggleDrawingToolbar() {
    // On mobile touch devices, show a hint instead of drawing toolbar
    var _touch = (typeof isTouchDevice === 'function') ? isTouchDevice() : ('ontouchstart' in window || navigator.maxTouchPoints > 0);
    if (_touch && window.innerWidth <= 768) {
        if (typeof enterMobileDrawMode === 'function') { enterMobileDrawMode(); return; }
        if (window.showSwipeHint) window.showSwipeHint('Drawing tools coming soon');
        return;
    }
    var content = document.getElementById('adv-chart-content');
    if (!content) return;
    content.classList.toggle('drawing-toolbar-visible');
    var btn = document.getElementById('draw-toggle-btn');
    if (btn) btn.classList.toggle('active', content.classList.contains('drawing-toolbar-visible'));
    updateOhlcOffset();
    // Reposition consensio table after toolbar toggle
    setTimeout(function() { if (typeof _repositionConsensioTable === 'function') _repositionConsensioTable(); }, 50);
}

function showDrawingToolbar() {
    var content = document.getElementById('adv-chart-content');
    if (content && !content.classList.contains('drawing-toolbar-visible')) {
        content.classList.add('drawing-toolbar-visible');
        var btn = document.getElementById('draw-toggle-btn');
        if (btn) btn.classList.add('active');
        updateOhlcOffset();
    }
}

function hideDrawingToolbar() {
    var content = document.getElementById('adv-chart-content');
    if (content && content.classList.contains('drawing-toolbar-visible')) {
        content.classList.remove('drawing-toolbar-visible');
        var btn = document.getElementById('draw-toggle-btn');
        if (btn) btn.classList.remove('active');
        updateOhlcOffset();
    }
}

// ── Expose handle functions for mobile touch handler ──
window._mobileHandleApi = {
    getRectHit: function(x, y) { return (typeof _getRectHitAtPoint === 'function') ? _getRectHitAtPoint(x, y) : null; },
    getChannelHit: function(x, y) { return (typeof _getChannelCornerHit === 'function') ? _getChannelCornerHit(x, y) : null; },
    getPositionHit: function(x, y) { return (typeof _getPositionHitAtPoint === 'function') ? _getPositionHitAtPoint(x, y) : null; },
    getFrvpHit: function(x, y) { return (typeof _getFrvpEdgeHit === 'function') ? _getFrvpEdgeHit(x, y) : null; },
    hitTest: function(x, y) { return (typeof hitTestAtPoint === 'function') ? hitTestAtPoint(x, y) : null; },
    computeATR: function(dataList, period) { return _computeATR(dataList, period); },
    handleRectResize: function(cx, cy, el) { if (typeof _handleRectResize === 'function') _handleRectResize(cx, cy, el, false); },
    handleChannelDrag: function(cx, cy, el) { if (typeof _handleChannelCornerDrag === 'function') _handleChannelCornerDrag(cx, cy, el, false); },
    handlePositionDrag: function(cx, cy, el) { if (typeof _handlePositionLevelDrag === 'function') _handlePositionLevelDrag(cx, cy, el, false); },
    handleFrvpDrag: function(cx, cy, el) { if (typeof _handleFrvpEdgeDrag === 'function') _handleFrvpEdgeDrag(cx, cy, el); },
    finalizeRect: function() { if (typeof _finalizeRectResize === 'function') _finalizeRectResize(); },
    finalizeChannel: function() { if (typeof _finalizeChannelCornerDrag === 'function') _finalizeChannelCornerDrag(); },
    finalizePosition: function() { if (typeof _finalizePositionLevelDrag === 'function') _finalizePositionLevelDrag(); },
    finalizeFrvp: function() { if (typeof _finalizeFrvpEdgeDrag === 'function') _finalizeFrvpEdgeDrag(); },
    hasActiveDrag: function() { return !!(_rectResizeState || _channelCornerDrag || _frvpEdgeDrag || _positionLevelDrag); },
    startRectDrag: function(hit) {
        var d = hit.drawing || (typeof getKlineDrawingById === 'function' ? getKlineDrawingById(hit.overlayId) : null);
        if (!d || !d.points || d.points.length < 2) return false;
        var p0 = d.points[0], p1 = d.points[1];
        _rectResizeState = {
            overlayId: hit.overlayId, handle: hit.handle, paneId: 'candle_pane',
            origPoints: [
                { timestamp: Math.min(p0.timestamp, p1.timestamp), value: Math.max(p0.value, p1.value) },
                { timestamp: Math.max(p0.timestamp, p1.timestamp), value: Math.min(p0.value, p1.value) }
            ],
            currentPoints: null
        };
        return true;
    },
    startChannelDrag: function(hit) {
        var chState = _armChannelDragState(hit);
        if (!chState) return false;
        _channelCornerDrag = chState;
        try { klineChart.overrideOverlay({ id: hit.overlayId, lock: true }); } catch(e) {}
        return true;
    },
    startFrvpDrag: function(hit) {
        if (!hit) return false;
        // Extend Right: reverts to manual only once a real drag starts —
        // deferred to _handleFrvpEdgeDrag (>=3px), same as the desktop arm
        // path, so a tap doesn't silently disarm. startX null = first move
        // sets the baseline.
        var _tfD = (typeof getKlineDrawingById === 'function') ? getKlineDrawingById(hit.overlayId) : null;
        var _tfPending = !!(_tfD && _tfD.extendData && _tfD.extendData.extendRight);
        _frvpEdgeDrag = { overlayId: hit.overlayId, edge: hit.edge, paneId: hit.paneId || 'candle_pane',
            disarmHit: _tfPending ? hit : null, startX: null };
        try { klineChart.overrideOverlay({ id: hit.overlayId, lock: true }); } catch(e) {}
        return true;
    },
    startPositionDrag: function(hit, localX, localY) {
        // Populate initPixelXs and clickOffset to match desktop arm shape,
        // so edge drags and body drags work correctly on touch.
        if (!hit) return false;
        var d = hit.drawing || (typeof getKlineDrawingById === 'function' ? getKlineDrawingById(hit.overlayId) : null);
        if (!d) return false;
        var _paneId = d.paneId || 'candle_pane';
        // Compute click offset (value-based, same as desktop arm :1308-1312)
        var _clickOffset = 0;
        var _hitValue = hit.level === 'body' ? d.points[0].value : hit.value;
        var _isLogScale = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
        var _hitValueChart = (_isLogScale && _hitValue > 0) ? Math.log(_hitValue) : _hitValue;
        try {
            var _clickPt = klineChart.convertFromPixel({ x: localX, y: localY }, { paneId: _paneId });
            if (_clickPt && _clickPt.value != null) _clickOffset = _clickPt.value - _hitValueChart;
        } catch(e) {}
        // Populate initPixelXs for edge/body drag (same as desktop arm :1321-1330)
        var _initPixelXs = null;
        var _initRightX = null;
        var _initRightTs = undefined;
        if (hit.level === 'right-edge' || hit.level === 'left-edge' || hit.level === 'body') {
            var _pts = d.points || [];
            _initPixelXs = _pts.map(function(p) {
                try {
                    var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                    var val = (_isLog && p.value > 0) ? Math.log(p.value) : p.value;
                    var px = klineChart.convertToPixel({ timestamp: p.timestamp, value: val }, { paneId: _paneId });
                    return px ? px.x : null;
                } catch(e) { return null; }
            });
            var _dExt = d.extendData || {};
            if (typeof _dExt.rightTs === 'number' && isFinite(_dExt.rightTs)) _initRightTs = _dExt.rightTs;
            if (hit.level === 'right-edge') {
                try {
                    var _xsA = _initPixelXs.filter(function(x) { return x !== null; });
                    var _xLA = _xsA.length ? Math.min.apply(null, _xsA) : 0;
                    var _xPM = _xsA.length ? Math.max.apply(null, _xsA) : 0;
                    _initRightX = _positionXRight(_xLA, _xPM, _dExt, _paneId, _pts[0] ? _pts[0].value : 0);
                } catch(e) {}
            }
        }
        var _origEntryTsMob = (d.points && d.points[0]) ? d.points[0].timestamp : null;
        _positionLevelDrag = {
            overlayId: hit.overlayId, level: hit.level, paneId: _paneId,
            origValue: _hitValue, clickOffset: _clickOffset, currentValue: null,
            startX: localX, initPixelXs: _initPixelXs,
            initRightX: _initRightX, initRightTs: _initRightTs,
            origEntryTs: _origEntryTsMob
        };
        // Full-width placement guidelines while dragging (read by the painter in chart-position-overlays.js)
        window._positionGuidelinesFor = _positionLevelDrag.overlayId;
        return true;
    }
};

// ── Drawings Object Tree (panel data model + actions) ─────

/**
 * Local price formatter (_fmtPriceSmart in app-core.js is not in this module's scope).
 * ≥1000 → thousands-separated, 0 decimals; ≥1 → 2 decimals; <1 → 4 sig figs.
 */
function _fmtObjPrice(p) {
    if (p == null || isNaN(p)) return String(p);
    var abs = Math.abs(p);
    var s;
    if (abs >= 1000) {
        s = Math.round(p).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    } else if (abs >= 1) {
        s = p.toFixed(2);
    } else if (abs >= 0.0001) {
        // 4 significant figures for small numbers
        s = parseFloat(p.toPrecision(4)).toString();
    } else {
        s = p.toPrecision(4);
    }
    return s;
}

/**
 * Map a hex color to a human-readable name via HSL buckets.
 * Pure function, exported for tests.
 */
function _colorName(hex) {
    if (!hex) return '';
    // Normalise: strip # and expand shorthand
    var h = (hex.charAt(0) === '#') ? hex.slice(1) : hex;
    if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    if (h.length !== 6) return '';
    var ri = parseInt(h.substring(0,2), 16);
    var gi = parseInt(h.substring(2,4), 16);
    var bi = parseInt(h.substring(4,6), 16);
    if (isNaN(ri) || isNaN(gi) || isNaN(bi)) return '';
    var r = ri / 255, g = gi / 255, b = bi / 255;
    var max = Math.max(r, g, b), min = Math.min(r, g, b);
    var l = (max + min) / 2;
    var s = 0;
    if (max !== min) {
        var d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    }
    // Achromatic: low saturation or very light/dark — use tighter threshold
    if (s < 0.20) {
        if (l >= 0.80) return 'white';
        if (l <= 0.20) return 'black';
        return 'gray';
    }
    // Chromatic hue (degrees 0-360)
    var hh = 0;
    if (max !== min) {
        if (max === r) hh = ((g - b) / (max - min) % 6) * 60;
        else if (max === g) hh = ((b - r) / (max - min) + 2) * 60;
        else hh = ((r - g) / (max - min) + 4) * 60;
    }
    if (hh < 0) hh += 360;
    if (hh < 20)  return 'red';
    if (hh < 45)  return 'orange';
    if (hh < 70)  return 'yellow';
    if (hh < 150) return 'green';
    if (hh < 190) return 'teal';
    if (hh < 255) return 'blue';
    if (hh < 320) return 'purple';
    if (hh < 345) return 'pink';
    return 'red';
}

// In-memory empty groups per symbol (created via “+ Group”, vanish on reload if no members).
// Shape: { symbol: { groupName: true } }
var _ephemeralGroups = {};

// Collapsed group state is stored in localStorage as UI-only, not in extendData.
var _GROUPS_COLLAPSED_KEY = 'carracObjGroupsCollapsed';

function _getGroupsCollapsed() {
    try {
        return JSON.parse(localStorage.getItem(_GROUPS_COLLAPSED_KEY) || '{}');
    } catch(e) { return {}; }
}

function _setGroupCollapsed(symbol, groupName, collapsed) {
    try {
        var c = _getGroupsCollapsed();
        if (!c[symbol]) c[symbol] = {};
        if (collapsed) {
            c[symbol][groupName] = true;
        } else {
            delete c[symbol][groupName];
        }
        localStorage.setItem(_GROUPS_COLLAPSED_KEY, JSON.stringify(c));
    } catch(e) {}
}

function _isGroupCollapsed(symbol, groupName) {
    try {
        var c = _getGroupsCollapsed();
        return !!(c[symbol] && c[symbol][groupName]);
    } catch(e) { return false; }
}

/**
 * Idempotent setter for _hidden — does not toggle.
 * Refactored from _toggleDrawingVisibility so group eye can set mixed states correctly.
 */
function _setDrawingHidden(overlayId, hidden) {
    try {
        var drawing = getKlineDrawingById(overlayId);
        if (!drawing) return;
        var isHidden = !!(drawing.extendData && drawing.extendData._hidden);
        if (isHidden === hidden) return; // already in desired state

        updateKlineDrawingExtendData(overlayId, { _hidden: hidden });

        if (hidden) {
            // Remove the live overlay, keep storage record
            if (klineChart) { try { klineChart.removeOverlay(overlayId); } catch(e) {} }
            if (klineSelectedOverlayId === overlayId) klineSelectedOverlayId = null;
        } else {
            // Recreate overlay from storage
            var updated = getKlineDrawingById(overlayId);
            if (updated) createOverlayFromDrawing(updated);
        }
    } catch(e) {}
}

/**
 * Master visibility toggle for ALL of the current asset's drawings (H key /
 * toolbar eye button). PURE VIEW STATE: flips klinecharts' native overlay
 * `visible` flag + one persisted boolean — it must NEVER touch the stored
 * drawings or sync to the server. (Mutating the per-drawing _hidden flag
 * would fire an upsert per drawing; upserts on system rows — liquidation
 * zones — create duplicate server rows and the sync poll then fights the
 * flags into a stuck-hidden state.)
 */
var _allDrawingsHidden = false;
(function() {
    try { _allDrawingsHidden = localStorage.getItem('klineAllDrawingsHidden') === '1'; } catch(e) {}
})();

function _applyAllDrawingsVisibility() {
    if (!klineChart) return;
    var drawings = [];
    try { drawings = _readDrawingsStore()[klineChartSymbol] || []; } catch(e) {}
    for (var i = 0; i < drawings.length; i++) {
        var d = drawings[i];
        if (!d.id || d.name === 'freehand') continue; // freehand = native renderer, no overlay
        if (d.extendData && d.extendData._hidden) continue; // individually hidden: no live overlay
        try { klineChart.overrideOverlay({ id: d.id, visible: !_allDrawingsHidden }); } catch(e) {}
    }
    // Freehand strokes render via the FREEHAND_NATIVE indicator — poke it so
    // the flag change repaints without waiting for a mouse move.
    try { klineChart.overrideIndicator({ name: 'FREEHAND_NATIVE' }); } catch(e) {}
    var eyeBtn = document.getElementById('drawings-eye-btn');
    if (eyeBtn) {
        eyeBtn.classList.toggle('drawings-hidden', _allDrawingsHidden);
        eyeBtn.title = _allDrawingsHidden ? 'Show all drawings (H)' : 'Hide all drawings (H)';
    }
}

function toggleAllDrawingsVisibility() {
    _allDrawingsHidden = !_allDrawingsHidden;
    try { localStorage.setItem('klineAllDrawingsHidden', _allDrawingsHidden ? '1' : '0'); } catch(e) {}
    _applyAllDrawingsVisibility();
    showToast(_allDrawingsHidden ? 'Drawings hidden — press H to show' : 'Drawings visible');
}

/**
 * Build the data model for the Drawings Object Tree panel.
 * Returns rows sorted zOrder DESC (topmost first, TV-style).
 * Includes hidden drawings (hidden flag set, no overlay).
 *
 * Row shape: { id, name, zOrder, hidden, locked, label, description, group }
 * Also returns groupMap: { groupName: [row, ...] } (members sorted zOrder DESC)
 * and ungrouped: [row, ...]
 */
function _buildObjectsPanelModel() {
    var rows = [];
    try {
        var all = _readDrawingsStore();
        var drawings = all[klineChartSymbol] || [];
        for (var i = 0; i < drawings.length; i++) {
            var d = drawings[i];
            var ext = d.extendData || {};
            var zOrder = (d.zOrder != null) ? d.zOrder : 0;
            var hidden = ext._hidden === true;
            var locked = ext._locked === true;
            var group = (ext._group && typeof ext._group === 'string') ? ext._group : null;

            // Richer labels
            var description = _buildDrawingLabel(d);

            rows.push({
                id: d.id,
                name: d.name,
                zOrder: zOrder,
                hidden: hidden,
                locked: locked,
                label: description,
                description: description,
                group: group,
                color: d.color || null
            });
        }
        // Sort DESC by zOrder (topmost first)
        rows.sort(function(a, b) {
            return (b.zOrder || 0) - (a.zOrder || 0);
        });
    } catch (e) {}
    return rows;
}

/**
 * Build the rich descriptive label for a drawing record.
 * Exported at module level for tests.
 */
function _buildDrawingLabel(d) {
    if (!d) return '';
    var ext = d.extendData || {};
    var points = d.points || [];
    var baseLabel = _getDrawingLabel(d.name);
    var colorLabel = d.color ? _colorName(d.color) : '';
    var colorPrefix = colorLabel ? (colorLabel.charAt(0).toUpperCase() + colorLabel.slice(1) + ' ') : '';

    if (d.name === 'spot') {
        var spotVal = points.length > 0 && points[0].value != null ? points[0].value : null;
        return 'Spot' + (spotVal != null ? ' ' + _fmtObjPrice(spotVal) : '');
    }

    if (d.name === 'longPosition' || d.name === 'shortPosition' || d.name === 'position') {
        // Determine side: name wins; fallback: SL < entry → long
        var side;
        if (d.name === 'longPosition') side = 'Long';
        else if (d.name === 'shortPosition') side = 'Short';
        else {
            // generic 'position': infer from SL vs entry
            var entryV = points.length > 0 && points[0].value != null ? points[0].value : null;
            var slV = points.length > 1 && points[1].value != null ? points[1].value : null;
            if (entryV != null && slV != null) {
                side = (slV < entryV) ? 'Long' : 'Short';
            } else {
                side = 'Position';
            }
        }
        var entry = points.length > 0 && points[0].value != null ? points[0].value : null;
        var sl    = points.length > 1 && points[1].value != null ? points[1].value : null;
        var tp    = points.length > 2 && points[2].value != null ? points[2].value : null;
        var parts = [side];
        if (entry != null) parts.push(_fmtObjPrice(entry));
        var extras = [];
        if (tp != null) extras.push('TP ' + _fmtObjPrice(tp));
        if (sl != null) extras.push('SL ' + _fmtObjPrice(sl));
        if (extras.length > 0) parts.push('→ ' + extras.join(' / '));
        return parts.join(' ');
    }

    if (d.name === 'rect') {
        var rectParts = [colorPrefix + 'rect'];
        if (ext.rectText) rectParts.push('— ' + ext.rectText);
        return rectParts.join(' ');
    }

    if (d.name === 'callout') {
        var calloutParts = [colorPrefix + 'callout'];
        if (ext.text) calloutParts.push('— ' + ext.text);
        return calloutParts.join(' ');
    }

    if (d.name === 'horizontalStraightLine' || d.name === 'horizontalRayLine' || d.name === 'horizontalSegment' || d.name === 'priceLine') {
        var hVal = points.length > 0 && points[0].value != null ? points[0].value : null;
        var hShort = d.name === 'horizontalRayLine' ? 'H-ray' : 'H-line';
        if (hVal != null) return colorPrefix + hShort + ' ' + _fmtObjPrice(hVal);
        return colorPrefix + hShort;
    }

    if (d.name === 'boundedChannel' || d.name === 'parallelStraightLine' || d.name === 'parallelChannel' || d.name === 'priceChannelLine') {
        return colorPrefix + 'channel';
    }

    if (d.name === 'freehand') return colorPrefix + 'freehand';

    if (d.name === 'fibonacciRetrace' || d.name === 'fibonacciInfinite' || d.name === 'fibonacciLine' ||
        d.name === 'fibonacciSegment' || d.name === 'fibonacciCircle' || d.name === 'fibonacciExtension') {
        return colorPrefix + 'fibonacci';
    }

    // Generic: colorPrefix + base label + first value
    var firstValue = points.length > 0 && points[0].value != null ? points[0].value : null;
    if (firstValue != null) return colorPrefix + baseLabel + ' ' + _fmtObjPrice(firstValue);
    return colorPrefix + baseLabel;
}

/**
 * Toggle drawing visibility (hide/show).
 * Delegates to _setDrawingHidden (idempotent) — toggles by reading current state.
 */
function _toggleDrawingVisibility(overlayId) {
    try {
        var drawing = getKlineDrawingById(overlayId);
        if (!drawing) return;
        var isHidden = !!(drawing.extendData && drawing.extendData._hidden);
        _setDrawingHidden(overlayId, !isHidden);
        _refreshObjectsPanelSummary();
    } catch (e) {}
}

/**
 * Toggle lock state for a drawing.
 * Shared between context-menu toggle-lock and the Objects panel lock button.
 * Calls klineChart.overrideOverlay({ id, lock }) + persists _locked via updateKlineDrawingExtendData.
 */
function toggleDrawingLock(overlayId) {
    if (!klineChart || !overlayId) return;
    var isLocked = false;
    var ov = null;
    try {
        var ovs = klineChart.getOverlayById(overlayId);
        ov = Array.isArray(ovs) ? ovs[0] : ovs;
    } catch(e) {}
    if (ov) {
        isLocked = ov.lock === true;
    } else {
        // Hidden drawing: no live overlay — read the persisted state instead
        var d = getKlineDrawingById(overlayId);
        isLocked = !!(d && d.extendData && d.extendData._locked);
    }
    if (ov) { try { klineChart.overrideOverlay({ id: overlayId, lock: !isLocked }); } catch(e) {} }
    updateKlineDrawingExtendData(overlayId, { _locked: !isLocked });
    _refreshObjectsPanelSummary();
}

/**
 * Reorder drawings: given an ordered array of ids (top → bottom, i.e. highest zOrder first),
 * assign descending zOrder values (ids[0] gets the highest), persist changed drawings,
 * then wipe+reload overlays to apply the new paint order.
 */
function _reorderDrawings(orderedIds) {
    if (!klineChartSymbol || !orderedIds || orderedIds.length === 0) return;
    try {
        var all = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var drawings = all[klineChartSymbol] || [];
        var n = orderedIds.length;
        // Assign zOrder: first id = n (highest), last = 1
        var changed = false;
        for (var i = 0; i < n; i++) {
            var newZ = n - i; // top row → n, bottom → 1
            for (var j = 0; j < drawings.length; j++) {
                if (drawings[j].id === orderedIds[i]) {
                    if ((drawings[j].zOrder || 0) !== newZ) {
                        drawings[j].zOrder = newZ;
                        changed = true;
                    }
                    break;
                }
            }
        }
        if (changed) {
            all[klineChartSymbol] = drawings;
            localStorage.setItem('klineDrawings', JSON.stringify(all));
            // Sync changed drawings to server
            for (var k = 0; k < n; k++) {
                var d = drawings.find ? drawings.find(function(x) { return x.id === orderedIds[k]; })
                    : (function(ids, dk) { for (var m = 0; m < drawings.length; m++) { if (drawings[m].id === dk) return drawings[m]; } return null; })(orderedIds, orderedIds[k]);
                if (d && typeof DrawingTools !== 'undefined' && DrawingTools.syncDrawingToServer) {
                    DrawingTools.syncDrawingToServer(d, klineChartSymbol);
                }
            }
        }
        // Wipe+reload to apply new paint order (same as Send to Back)
        _reloadAllDrawingOverlays();
    } catch (e) {}
}

/**
 * Update the tab count summary span (#drawer-objects-summary).
 * Safe to call from any context; guards with typeof document check.
 */
function _refreshObjectsPanelSummary() {
    if (typeof document === 'undefined') return;
    try {
        var all = _readDrawingsStore();
        var drawings = (all && klineChartSymbol) ? (all[klineChartSymbol] || []) : [];
        var count = drawings.length;
        var el = document.getElementById('drawer-objects-summary');
        if (el) el.textContent = count + (count === 1 ? ' Drawing' : ' Drawings');
    } catch (e) {}
}

// SVG icon helpers (inline, no DOM dependency)
var _ICON_EYE_ON  = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
var _ICON_EYE_OFF = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
var _ICON_LOCK    = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
var _ICON_UNLOCK  = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>';
var _ICON_TRASH   = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
var _ICON_PENCIL  = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';

function _buildRowHtml(row) {
    var hiddenCls = row.hidden ? ' objects-row-hidden' : '';
    var lockedCls = row.locked ? ' objects-row-locked' : '';
    var eyeIcon   = row.hidden ? _ICON_EYE_OFF : _ICON_EYE_ON;
    var lockIcon  = row.locked ? _ICON_LOCK : _ICON_UNLOCK;
    return '<li class="objects-panel-row' + hiddenCls + lockedCls + '" data-drawing-id="' + row.id + '" draggable="true">' +
        '<span class="objects-row-drag" aria-hidden="true">&#9776;</span>' +
        '<span class="objects-row-label" title="' + escapeHtml(row.description) + '">' + escapeHtml(row.label) + '</span>' +
        '<button class="objects-row-btn objects-row-lock" data-action="lock" data-id="' + row.id + '" title="' + (row.locked ? 'Unlock' : 'Lock') + '" aria-label="' + (row.locked ? 'Unlock' : 'Lock') + '">' + lockIcon + '</button>' +
        '<button class="objects-row-btn objects-row-eye" data-action="eye" data-id="' + row.id + '" title="' + (row.hidden ? 'Show' : 'Hide') + '" aria-label="' + (row.hidden ? 'Show' : 'Hide') + '">' + eyeIcon + '</button>' +
        '<button class="objects-row-btn objects-row-delete" data-action="delete" data-id="' + row.id + '" title="Delete" aria-label="Delete">' + _ICON_TRASH + '</button>' +
    '</li>';
}

/**
 * Render the Drawings Object Tree panel into #drawer-section-objects.
 * Called by openDrawer('objects') hook, and after mutations when tab is open.
 */
function renderDrawingsObjectsPanel() {
    if (typeof document === 'undefined') return;
    var section = document.getElementById('drawer-section-objects');
    if (!section) return;

    _refreshObjectsPanelSummary();

    var model = _buildObjectsPanelModel();
    if (model.length === 0) {
        section.innerHTML =
            '<div class="objects-panel-toolbar" id="objects-panel-toolbar">' +
                '<button class="objects-toolbar-btn" data-action="add-group" title="New group">+ Group</button>' +
            '</div>' +
            '<div class="objects-panel-empty">No drawings on this chart.</div>';
        _wireObjectsPanelEvents(section);
        return;
    }

    // Determine whether any groups exist (persisted or ephemeral)
    var sym = klineChartSymbol || '';
    var hasGroups = false;
    for (var mi = 0; mi < model.length; mi++) {
        if (model[mi].group) { hasGroups = true; break; }
    }
    if (!hasGroups && _ephemeralGroups[sym]) {
        var epKeys = Object.keys(_ephemeralGroups[sym]);
        if (epKeys.length > 0) hasGroups = true;
    }

    // Build the list HTML
    var html = '<div class="objects-panel-toolbar" id="objects-panel-toolbar">' +
        '<button class="objects-toolbar-btn" data-action="add-group" title="New group">+ Group</button>' +
    '</div>';

    html += '<ul class="objects-panel-list" id="objects-panel-list">';

    if (!hasGroups) {
        // Flat list (original layout)
        for (var i = 0; i < model.length; i++) {
            html += _buildRowHtml(model[i]);
        }
    } else {
        // Grouped layout: collect groups
        var groupMap = {};  // groupName -> [row]
        var ungrouped = [];
        for (var gi = 0; gi < model.length; gi++) {
            var r = model[gi];
            if (r.group) {
                if (!groupMap[r.group]) groupMap[r.group] = [];
                groupMap[r.group].push(r);
            } else {
                ungrouped.push(r);
            }
        }

        // Add ephemeral empty groups (rows will be empty)
        if (_ephemeralGroups[sym]) {
            var eKeys = Object.keys(_ephemeralGroups[sym]);
            for (var ei = 0; ei < eKeys.length; ei++) {
                if (!groupMap[eKeys[ei]]) groupMap[eKeys[ei]] = [];
            }
        }

        var groupNames = Object.keys(groupMap).sort();

        for (var gni = 0; gni < groupNames.length; gni++) {
            var gName = groupNames[gni];
            var members = groupMap[gName]; // already sorted zOrder DESC by _buildObjectsPanelModel
            var collapsed = _isGroupCollapsed(sym, gName);
            var anyVisible = false;
            for (var vi = 0; vi < members.length; vi++) {
                if (!members[vi].hidden) { anyVisible = true; break; }
            }
            var caretIcon = collapsed ? '&#9658;' : '&#9660;';
            var groupEyeIcon = anyVisible ? _ICON_EYE_ON : _ICON_EYE_OFF;
            html += '<li class="objects-group-header" data-group-name="' + escapeHtml(gName) + '" data-drop-target="group">' +
                '<span class="objects-group-caret" data-action="collapse-group" data-group="' + escapeHtml(gName) + '">' + caretIcon + '</span>' +
                '<span class="objects-group-name" data-group="' + escapeHtml(gName) + '">' + escapeHtml(gName) + '</span>' +
                '<span class="objects-group-count">' + members.length + '</span>' +
                '<button class="objects-row-btn objects-group-eye" data-action="group-eye" data-group="' + escapeHtml(gName) + '" title="Toggle group visibility">' + groupEyeIcon + '</button>' +
                '<button class="objects-row-btn objects-group-rename" data-action="group-rename" data-group="' + escapeHtml(gName) + '" title="Rename group">' + _ICON_PENCIL + '</button>' +
            '</li>';
            if (!collapsed) {
                if (members.length === 0) {
                    html += '<li class="objects-group-empty-hint">(drag drawings here)</li>';
                } else {
                    for (var mj = 0; mj < members.length; mj++) {
                        html += _buildRowHtml(members[mj]);
                    }
                }
            }
        }

        // Ungrouped section header (only when at least one group exists)
        html += '<li class="objects-group-header objects-group-ungrouped" data-group-name="" data-drop-target="ungrouped">' +
            '<span class="objects-group-name">Ungrouped</span>' +
            '<span class="objects-group-count">' + ungrouped.length + '</span>' +
        '</li>';
        for (var ui = 0; ui < ungrouped.length; ui++) {
            html += _buildRowHtml(ungrouped[ui]);
        }
    }

    html += '</ul>';
    section.innerHTML = html;

    // Wire interactions
    _wireObjectsPanelEvents(section);
}

/**
 * Wire click (eye/lock), hover-highlight, click-to-select, and drag-to-reorder events.
 */
function _wireObjectsPanelEvents(section) {
    if (!section) return;

    // Section-level listeners survive innerHTML re-renders (delegation via
    // closest) — wire them ONCE or every re-render stacks another set and a
    // single eye click toggles N times.
    if (!section._objectsWired) {
        section._objectsWired = true;
        _wireObjectsSectionEvents(section);
    }
    _wireObjectsDragEvents(section);
}

// Flash timer for click-to-select highlight (cleared by next flash)
var _objPanelFlashTimer = null;

function _flashDrawing(id, name) {
    if (!id) return;
    if (_objPanelFlashTimer) { clearTimeout(_objPanelFlashTimer); _objPanelFlashTimer = null; }
    // Apply hover highlight
    if (name === 'rect') {
        _setRectHovered(id);
    } else if (name === 'position' || name === 'longPosition' || name === 'shortPosition') {
        _setPositionHovered(id);
    } else if (name === 'boundedChannel') {
        _setChannelHovered(id);
    } else {
        if (klineChart) {
            try {
                var ovs = klineChart.getOverlayById(id);
                var ov = Array.isArray(ovs) ? ovs[0] : ovs;
                if (ov) {
                    var origW = (ov.styles && ov.styles.line && ov.styles.line.size) || 1;
                    _nativeHoverOrigWidth[id] = origW;
                    klineChart.overrideOverlay({ id: id, styles: { line: { size: origW + 2 } } });
                }
            } catch(ef) {}
        }
    }
    // Clear after 1200ms
    _objPanelFlashTimer = setTimeout(function() {
        _setRectHovered(null);
        _setPositionHovered(null);
        _setChannelHovered(null);
        if (_nativeHoverOrigWidth[id] != null && klineChart) {
            try { klineChart.overrideOverlay({ id: id, styles: { line: { size: _nativeHoverOrigWidth[id] } } }); } catch(e) {}
            delete _nativeHoverOrigWidth[id];
        }
        _objPanelFlashTimer = null;
    }, 1200);
}

function _scrollChartToDrawing(drawing) {
    if (!drawing || !klineChart) return;
    try {
        var points = drawing.points || [];
        if (points.length === 0) return;
        // Try scrollToDataIndex first (confirmed present in bundled klinecharts)
        if (typeof klineChart.scrollToDataIndex === 'function') {
            // Use mean dataIndex from live overlay if available, else from timestamps
            var idx = null;
            try {
                var ovRaw = klineChart.getOverlayById(drawing.id);
                var ov0 = Array.isArray(ovRaw) ? ovRaw[0] : ovRaw;
                if (ov0 && ov0.points && ov0.points.length > 0) {
                    var sum = 0; var cnt = 0;
                    for (var pi = 0; pi < ov0.points.length; pi++) {
                        if (ov0.points[pi].dataIndex != null) { sum += ov0.points[pi].dataIndex; cnt++; }
                    }
                    if (cnt > 0) idx = Math.round(sum / cnt);
                }
            } catch(eg) {}
            if (idx == null && klineChart.getDataList) {
                // Resolve first point's timestamp to dataIndex
                var ts0 = points[0].timestamp;
                if (ts0) {
                    var dl = klineChart.getDataList();
                    for (var di = 0; di < dl.length; di++) {
                        if (dl[di].timestamp >= ts0) { idx = di; break; }
                    }
                    if (idx == null && dl.length > 0) idx = dl.length - 1;
                }
            }
            if (idx != null) { try { klineChart.scrollToDataIndex(idx); } catch(e) {} }
        } else if (typeof klineChart.scrollToTimestamp === 'function' && points[0].timestamp) {
            try { klineChart.scrollToTimestamp(points[0].timestamp); } catch(e) {}
        }
    } catch(e) {}
}

function _wireObjectsSectionEvents(section) {
    // ── Button clicks: eye, lock, delete, group actions, toolbar ──────
    section.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-action]');
        if (!btn) return;
        var action = btn.getAttribute('data-action');
        var id = btn.getAttribute('data-id');

        if (action === 'eye') {
            _toggleDrawingVisibility(id);
            renderDrawingsObjectsPanel();
            e.stopPropagation();
            return;
        }
        if (action === 'lock') {
            toggleDrawingLock(id);
            renderDrawingsObjectsPanel();
            e.stopPropagation();
            return;
        }
        if (action === 'delete') {
            handleContextMenuAction('delete', id);
            renderDrawingsObjectsPanel();
            _refreshObjectsPanelSummary();
            e.stopPropagation();
            return;
        }

        // ── Group actions ──────────────────────────────────────────────
        var gName = btn.getAttribute('data-group');

        if (action === 'collapse-group') {
            var sym = klineChartSymbol || '';
            var wasCollapsed = _isGroupCollapsed(sym, gName);
            _setGroupCollapsed(sym, gName, !wasCollapsed);
            renderDrawingsObjectsPanel();
            e.stopPropagation();
            return;
        }

        if (action === 'group-eye') {
            // Toggle: any visible → hide all; none visible → show all
            try {
                var allDrawings = _readDrawingsStore();
                var symDrawings = allDrawings[klineChartSymbol] || [];
                var members = symDrawings.filter(function(d) { return d.extendData && d.extendData._group === gName; });
                var anyVisible = members.some(function(d) { return !d.extendData._hidden; });
                for (var gi = 0; gi < members.length; gi++) {
                    _setDrawingHidden(members[gi].id, anyVisible); // anyVisible → hide all; else show all
                }
            } catch(eg) {}
            renderDrawingsObjectsPanel();
            e.stopPropagation();
            return;
        }

        if (action === 'group-rename') {
            // Inline rename: replace the group name span with an input
            var header = btn.closest('.objects-group-header');
            if (!header) { e.stopPropagation(); return; }
            var nameSpan = header.querySelector('.objects-group-name');
            if (!nameSpan || nameSpan._renaming) { e.stopPropagation(); return; }
            nameSpan._renaming = true;
            var oldName = gName;
            var inp = typeof document !== 'undefined' ? document.createElement('input') : null;
            if (!inp) { e.stopPropagation(); return; }
            inp.type = 'text';
            inp.value = oldName;
            inp.className = 'objects-group-rename-input';
            inp.style.cssText = 'background:#1e222d;border:1px solid #58a6ff;color:#d1d4dc;font-size:11px;padding:1px 4px;border-radius:2px;width:90px;outline:none;';
            nameSpan.replaceWith(inp);
            inp.focus();
            inp.select();

            // Re-render detaches the input → blur fires → without a guard,
            // Escape would COMMIT via blur instead of canceling, and Enter
            // would commit twice.
            var _renameDone = false;
            function commitRename() {
                if (_renameDone) return;
                _renameDone = true;
                var newName = inp.value.trim();
                if (!newName || newName === oldName) {
                    // Cancel — just re-render
                    renderDrawingsObjectsPanel();
                    return;
                }
                // Rename all members
                try {
                    var allD = _readDrawingsStore();
                    var sDrawings = allD[klineChartSymbol] || [];
                    for (var ri = 0; ri < sDrawings.length; ri++) {
                        if (sDrawings[ri].extendData && sDrawings[ri].extendData._group === oldName) {
                            updateKlineDrawingExtendData(sDrawings[ri].id, { _group: newName });
                        }
                    }
                    // Move ephemeral group key if applicable
                    if (_ephemeralGroups[klineChartSymbol] && _ephemeralGroups[klineChartSymbol][oldName]) {
                        delete _ephemeralGroups[klineChartSymbol][oldName];
                        _ephemeralGroups[klineChartSymbol][newName] = true;
                    }
                    // Move collapsed state
                    var wasCol = _isGroupCollapsed(klineChartSymbol, oldName);
                    _setGroupCollapsed(klineChartSymbol, oldName, false);
                    if (wasCol) _setGroupCollapsed(klineChartSymbol, newName, true);
                } catch(er) {}
                renderDrawingsObjectsPanel();
            }

            inp.addEventListener('keydown', function(ke) {
                if (ke.key === 'Enter') { ke.preventDefault(); commitRename(); }
                if (ke.key === 'Escape') { _renameDone = true; renderDrawingsObjectsPanel(); }
            });
            inp.addEventListener('blur', commitRename);
            e.stopPropagation();
            return;
        }

        if (action === 'add-group') {
            // Show inline input for new group name in the toolbar
            var toolbar = section.querySelector('#objects-panel-toolbar');
            if (!toolbar) { e.stopPropagation(); return; }
            var existing = toolbar.querySelector('.objects-group-name-input');
            if (existing) { e.stopPropagation(); return; }
            var groupInput = typeof document !== 'undefined' ? document.createElement('input') : null;
            if (!groupInput) { e.stopPropagation(); return; }
            groupInput.type = 'text';
            groupInput.placeholder = 'Group name…';
            groupInput.className = 'objects-group-name-input';
            groupInput.style.cssText = 'background:#1e222d;border:1px solid #58a6ff;color:#d1d4dc;font-size:11px;padding:1px 4px;border-radius:2px;width:90px;margin-left:6px;outline:none;';
            toolbar.appendChild(groupInput);
            groupInput.focus();

            // Same detach-blur guard as the rename input: Escape must cancel.
            var _newGroupDone = false;
            function confirmNewGroup() {
                if (_newGroupDone) return;
                _newGroupDone = true;
                var gNewName = groupInput.value.trim();
                if (gNewName) {
                    var symKey = klineChartSymbol || '';
                    if (!_ephemeralGroups[symKey]) _ephemeralGroups[symKey] = {};
                    _ephemeralGroups[symKey][gNewName] = true;
                }
                renderDrawingsObjectsPanel();
            }

            groupInput.addEventListener('keydown', function(ke) {
                if (ke.key === 'Enter') { ke.preventDefault(); confirmNewGroup(); }
                if (ke.key === 'Escape') { _newGroupDone = true; renderDrawingsObjectsPanel(); }
            });
            groupInput.addEventListener('blur', confirmNewGroup);
            e.stopPropagation();
            return;
        }

        e.stopPropagation();
    });

    // ── Row click → select + scroll + flash ───────────────────────────
    section.addEventListener('click', function(e) {
        if (e.target.closest('[data-action]')) return;
        var row = e.target.closest('.objects-panel-row');
        if (!row) return;
        var id = row.getAttribute('data-drawing-id');
        if (!id) return;
        klineSelectedOverlayId = id;
        // Poke overlay for selection visuals
        try {
            var ov = klineChart && klineChart.getOverlayById ? klineChart.getOverlayById(id) : null;
            var ov0 = Array.isArray(ov) ? ov[0] : ov;
            if (ov0) {
                var ext = Object.assign({}, ov0.extendData || {});
                ext._hoverTick = Date.now();
                klineChart.overrideOverlay({ id: id, extendData: ext });
            }
        } catch(e2) {}
        // Scroll chart to drawing (if not hidden)
        try {
            var allD = _readDrawingsStore();
            var symD = (allD && klineChartSymbol) ? (allD[klineChartSymbol] || []) : [];
            var drw = null;
            for (var si = 0; si < symD.length; si++) { if (symD[si].id === id) { drw = symD[si]; break; } }
            if (drw && !(drw.extendData && drw.extendData._hidden)) {
                _scrollChartToDrawing(drw);
                _flashDrawing(id, drw.name);
            }
        } catch(e3) {}
    });

    // ── Row hover → chart highlight ───────────────────────────────────
    section.addEventListener('mouseover', function(e) {
        var row = e.target.closest('.objects-panel-row');
        if (!row) return;
        var id = row.getAttribute('data-drawing-id');
        if (!id) return;
        // Check if hidden
        var all = _readDrawingsStore();
        var drawings = (all && klineChartSymbol) ? (all[klineChartSymbol] || []) : [];
        var d = null;
        for (var i = 0; i < drawings.length; i++) { if (drawings[i].id === id) { d = drawings[i]; break; } }
        if (!d || (d.extendData && d.extendData._hidden)) return;
        var name = d.name;
        if (name === 'rect') {
            _setRectHovered(id);
        } else if (name === 'position' || name === 'longPosition' || name === 'shortPosition') {
            _setPositionHovered(id);
        } else if (name === 'boundedChannel') {
            _setChannelHovered(id);
        } else {
            if (klineChart) {
                try {
                    var ovs = klineChart.getOverlayById(id);
                    var ov = Array.isArray(ovs) ? ovs[0] : ovs;
                    if (ov) {
                        var origW = (ov.styles && ov.styles.line && ov.styles.line.size) || 1;
                        _nativeHoverOrigWidth[id] = origW;
                        klineChart.overrideOverlay({ id: id, styles: { line: { size: origW + 1 } } });
                    }
                } catch(e3) {}
            }
        }
    });

    section.addEventListener('mouseout', function(e) {
        var row = e.target.closest('.objects-panel-row');
        if (!row) return;
        var id = row.getAttribute('data-drawing-id');
        if (!id) return;
        _setRectHovered(null);
        _setPositionHovered(null);
        _setChannelHovered(null);
        if (_nativeHoverOrigWidth[id] != null && klineChart) {
            try {
                klineChart.overrideOverlay({ id: id, styles: { line: { size: _nativeHoverOrigWidth[id] } } });
            } catch(e4) {}
            delete _nativeHoverOrigWidth[id];
        }
    });
}

// The list element is REPLACED on every render — drag wiring runs per render.
function _wireObjectsDragEvents(section) {
    var _dragSrcRow = null;
    var list = section.querySelector('#objects-panel-list');
    if (!list) return;

    list.addEventListener('dragstart', function(e) {
        _dragSrcRow = e.target.closest('.objects-panel-row');
        if (!_dragSrcRow) return;
        e.dataTransfer.effectAllowed = 'move';
        _dragSrcRow.classList.add('objects-row-dragging');
    });
    list.addEventListener('dragend', function(e) {
        if (_dragSrcRow) _dragSrcRow.classList.remove('objects-row-dragging');
        _dragSrcRow = null;
        list.querySelectorAll('.objects-row-dragover, .objects-group-header.objects-row-dragover').forEach(function(r) {
            r.classList.remove('objects-row-dragover');
        });
    });
    list.addEventListener('dragover', function(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        // Highlight group headers or regular rows
        var groupTarget = e.target.closest('.objects-group-header');
        var rowTarget   = e.target.closest('.objects-panel-row');
        list.querySelectorAll('.objects-row-dragover').forEach(function(r) { r.classList.remove('objects-row-dragover'); });
        if (groupTarget) {
            groupTarget.classList.add('objects-row-dragover');
        } else if (rowTarget && rowTarget !== _dragSrcRow) {
            rowTarget.classList.add('objects-row-dragover');
        }
    });
    list.addEventListener('drop', function(e) {
        e.preventDefault();
        list.querySelectorAll('.objects-row-dragover').forEach(function(r) { r.classList.remove('objects-row-dragover'); });

        if (!_dragSrcRow) return;
        var srcId = _dragSrcRow.getAttribute('data-drawing-id');
        if (!srcId) return;

        // Check if dropped on a group header
        var groupHeader = e.target.closest('.objects-group-header');
        if (groupHeader) {
            var dropTarget = groupHeader.getAttribute('data-drop-target');
            if (dropTarget === 'ungrouped') {
                // Remove from group
                updateKlineDrawingExtendData(srcId, { _group: null });
                // Remove from ephemeral if it was there
            } else if (dropTarget === 'group') {
                var gName = groupHeader.getAttribute('data-group-name');
                if (gName) {
                    updateKlineDrawingExtendData(srcId, { _group: gName });
                    // Promote ephemeral to persisted (it now has a member)
                    var sym = klineChartSymbol || '';
                    if (_ephemeralGroups[sym] && _ephemeralGroups[sym][gName]) {
                        delete _ephemeralGroups[sym][gName];
                    }
                }
            }
            renderDrawingsObjectsPanel();
            return;
        }

        // Regular row-to-row reorder (existing z-order logic — unchanged)
        var target = e.target.closest('.objects-panel-row');
        if (!target || target === _dragSrcRow) return;
        var rows = Array.from(list.querySelectorAll('.objects-panel-row'));
        var srcIdx = rows.indexOf(_dragSrcRow);
        var tgtIdx = rows.indexOf(target);
        if (srcIdx < tgtIdx) {
            list.insertBefore(_dragSrcRow, target.nextSibling);
        } else {
            list.insertBefore(_dragSrcRow, target);
        }
        target.classList.remove('objects-row-dragover');
        // Collect new order (top row = index 0 = highest zOrder)
        var newOrder = Array.from(list.querySelectorAll('.objects-panel-row')).map(function(r) {
            return r.getAttribute('data-drawing-id');
        });
        _reorderDrawings(newOrder);
        renderDrawingsObjectsPanel();
    });
}
