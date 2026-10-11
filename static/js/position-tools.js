// Position and spot tools on the chart: the take-profit readout, the
// click-to-add intermediate TP mode, and the spot buy/sell dialogs that
// write through to the journal. Needs app-core.js loaded first.

// TP-zone readout — singleton dashed line + stats label. Used by the
// selected-position hover (kline-drawings.js hover pass) AND T-mode.
var _tpReadoutEls = null;
function _showTpReadout(x, y, text) {
    var container = document.getElementById('kline-chart-container');
    if (!container) return;
    if (!_tpReadoutEls) {
        var line = document.createElement('div');
        line.id = 'tp-zone-readout-line';
        line.style.cssText = 'position:absolute;left:0;right:0;height:0;border-top:1px dashed rgba(255,215,0,0.6);z-index:10000;pointer-events:none';
        var tag = document.createElement('div');
        tag.id = 'tp-zone-readout-tag';
        tag.style.cssText = 'position:absolute;transform:translateY(4px);color:#FFD700;font:11px JetBrains Mono,monospace;background:rgba(58,50,10,0.9);border:1px solid rgba(255,215,0,0.35);border-radius:3px;padding:2px 6px;z-index:10001;pointer-events:none;white-space:nowrap';
        container.appendChild(line); container.appendChild(tag);
        _tpReadoutEls = { line: line, tag: tag };
    }
    _tpReadoutEls.line.style.display = '';
    _tpReadoutEls.tag.style.display = '';
    _tpReadoutEls.line.style.top = y + 'px';
    _tpReadoutEls.tag.style.top = y + 'px';
    var containerW = container.clientWidth || 800;
    _tpReadoutEls.tag.style.left = Math.min(Math.max(x + 12, 8), containerW - 190) + 'px';
    _tpReadoutEls.tag.textContent = text;
}
function _hideTpReadout() {
    if (_tpReadoutEls) { _tpReadoutEls.line.style.display = 'none'; _tpReadoutEls.tag.style.display = 'none'; }
}
window._showTpReadout = _showTpReadout;
window._hideTpReadout = _hideTpReadout;

// Add intermediate TP by clicking in the TP zone of a position overlay
var _addTPMode = null; // { overlayId, entryPrice, tpPrice, cleanup }
function enterAddTPMode(overlay) {
    if (_addTPMode) exitAddTPMode();
    var entryP = overlay.points[0].value;
    var slP = overlay.points[1].value;
    var tpP = overlay.points[2].value;
    var isLong = slP < entryP;
    var container = document.getElementById('kline-chart-container');
    if (!container) return;

    // Show hint banner
    var hint = document.createElement('div');
    hint.id = 'add-tp-hint';
    hint.style.cssText = 'position:absolute;top:36px;left:50%;transform:translateX(-50%);z-index:10001;background:rgba(58,50,10,0.9);border:1px solid #FFD700;color:#FFD700;padding:4px 14px;border-radius:4px;font:12px JetBrains Mono,monospace;pointer-events:none;white-space:nowrap';
    hint.textContent = 'Click in TP zone to add target \u2022 Esc to cancel';
    var chartContent = document.getElementById('adv-chart-content');
    if (chartContent) chartContent.appendChild(hint);

    // Crosshair line follows mouse in TP zone
    var previewLine = document.createElement('div');
    previewLine.id = 'add-tp-preview';
    previewLine.style.cssText = 'position:absolute;left:0;right:0;height:1px;border-top:1px dashed rgba(255,215,0,0.6);z-index:10000;pointer-events:none;display:none';
    container.appendChild(previewLine);

    function onMouseMove(e) {
        var rect = container.getBoundingClientRect();
        var y = e.clientY - rect.top;
        try {
            var coord = klineChart.convertFromPixel({ x: 0, y: y }, { paneId: 'candle_pane' });
            if (coord && coord.value !== undefined) {
                var price = coord.value;
                var inZone = isLong ? (price > entryP && price < tpP) : (price < entryP && price > tpP);
                if (inZone) {
                    previewLine.style.display = 'block';
                    previewLine.style.top = y + 'px';
                    container.style.cursor = 'crosshair';
                    // Live stats label (price · % · R) — raw prices, log-safe
                    var _isLogT = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                    var _rawP = _isLogT ? Math.exp(price) : price;
                    var _rawE = _isLogT ? Math.exp(entryP) : entryP;
                    var _rawS = _isLogT ? Math.exp(slP) : slP;
                    if (typeof window.carracPositionStats === 'function') {
                        var _st = window.carracPositionStats(_rawP, _rawE, _rawS);
                        _showTpReadout(e.clientX - rect.left, y,
                            _fmtPriceSmart(_rawP) + ' · ' + (_st.pct >= 0 ? '+' : '') + _st.pct.toFixed(2) + '%' + (_st.rLabel ? ' · ' + _st.rLabel : ''));
                        previewLine.style.display = 'none';
                    }
                } else {
                    previewLine.style.display = 'none';
                    container.style.cursor = '';
                    _hideTpReadout();
                }
            }
        } catch(ex) {}
    }

    function onClick(e) {
        var rect = container.getBoundingClientRect();
        var y = e.clientY - rect.top;
        try {
            var coord = klineChart.convertFromPixel({ x: 0, y: y }, { paneId: 'candle_pane' });
            if (coord && coord.value !== undefined) {
                var price = coord.value;
                var inZone = isLong ? (price > entryP && price < tpP) : (price < entryP && price > tpP);
                if (inZone) {
                    e.preventDefault();
                    e.stopPropagation();
                    // Add TP to overlay
                    var ovs = klineChart.getOverlayById(overlay.id);
                    var ov = Array.isArray(ovs) ? ovs[0] : ovs;
                    if (ov) {
                        var ext = ov.extendData || {};
                        if (!ext.tps) ext.tps = [];
                        ext.tps.push(price);
                        ext.tps.sort(function(a, b) {
                            return Math.abs(a - entryP) - Math.abs(b - entryP);
                        });
                        klineChart.overrideOverlay({ id: ov.id, extendData: ext });
                        var drawRec = getKlineDrawingById(ov.id);
                        if (drawRec) {
                            drawRec.extendData = Object.assign({}, ext);
                            // Convert TPs from chart space (log) back to raw for storage
                            var _isLog = (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled);
                            if (_isLog && drawRec.extendData.tps) {
                                drawRec.extendData.tps = drawRec.extendData.tps.map(function(v) { return Math.exp(v); });
                            }
                            saveKlineDrawing(drawRec);
                        }
                    }
                    exitAddTPMode();
                }
            }
        } catch(ex) {}
    }

    function onKeyDown(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            exitAddTPMode();
        }
    }

    container.addEventListener('mousemove', onMouseMove);
    container.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKeyDown, true);

    window._addTPModeActive = true;
    _addTPMode = {
        overlayId: overlay.id,
        cleanup: function() {
            container.removeEventListener('mousemove', onMouseMove);
            container.removeEventListener('click', onClick, true);
            document.removeEventListener('keydown', onKeyDown, true);
            container.style.cursor = '';
            if (previewLine.parentNode) previewLine.remove();
            if (hint.parentNode) hint.remove();
            _hideTpReadout();
            window._addTPModeActive = false;
            _addTPMode = null;
        }
    };
}

function exitAddTPMode() {
    if (_addTPMode && _addTPMode.cleanup) _addTPMode.cleanup();
}

function showSpotBuyDialog(entryPrice, callback, editData) {
    var existing = document.getElementById('spot-buy-dialog');
    if (existing) existing.remove();
    var ed = editData || {};
    function fmtP(p) { return Math.abs(p) >= 1 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6)); }
    var sym = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : '';
    var isEdit = !!editData;
    var dialog = document.createElement('div');
    dialog.id = 'spot-buy-dialog';
    dialog.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:300px;background:#1e222d;border:1px solid #4db6ac;border-radius:8px;z-index:26003;display:flex;flex-direction:column;padding:16px;box-shadow:0 0 20px rgba(77,182,172,0.15),0 8px 32px rgba(0,0,0,0.6);font-family:JetBrains Mono,monospace';
    dialog.innerHTML =
        '<div style="color:#4db6ac;font-size:13px;font-weight:600;margin-bottom:12px">' + (isEdit ? 'Edit Spot Buy' : 'Spot Buy') + '</div>' +
        '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">' +
            '<div style="display:flex;align-items:center;gap:8px">' +
                '<label style="color:#787b86;font-size:11px;width:50px">Price</label>' +
                '<input type="text" id="spot-price-input" value="' + fmtP(entryPrice) + '" autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other" role="presentation" style="flex:1;background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-size:13px;outline:none;font-family:inherit">' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
                '<label style="color:#787b86;font-size:11px;width:50px">USD $</label>' +
                '<input type="text" id="spot-usd-input" value="' + (ed.usdAmount ? ed.usdAmount : '') + '" placeholder="Amount spent" autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other" role="presentation" style="flex:1;background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-size:13px;outline:none;font-family:inherit">' +
            '</div>' +
            '<div id="spot-qty-display" style="color:#787b86;font-size:11px;padding-left:58px"></div>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
                '<label style="color:#787b86;font-size:11px;width:50px">Label</label>' +
                '<input type="text" id="spot-label-input" value="' + escapeHtml(ed.label || '') + '" placeholder="e.g. DCA #3" autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other" role="presentation" style="flex:1;background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-size:13px;outline:none;font-family:inherit">' +
            '</div>' +
        '</div>' +
        '<div style="display:flex;gap:8px;justify-content:flex-end">' +
            '<button id="spot-cancel-btn" style="background:#2a2e39;border:1px solid #363a45;border-radius:4px;padding:6px 14px;color:#787b86;cursor:pointer;font-size:12px;font-family:inherit">Cancel</button>' +
            '<button id="spot-ok-btn" style="background:#4db6ac;border:none;border-radius:4px;padding:6px 14px;color:#131722;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit">' + (isEdit ? 'Save' : 'OK') + '</button>' +
        '</div>';
    document.body.appendChild(dialog);
    var usdInput = document.getElementById('spot-usd-input');
    var priceInput = document.getElementById('spot-price-input');
    var qtyDisplay = document.getElementById('spot-qty-display');
    if (isEdit) { document.getElementById('spot-label-input').focus(); } else { usdInput.focus(); }
    function updateQty() {
        var price = parseFloat(priceInput.value);
        var usd = parseFloat(usdInput.value);
        if (price > 0 && usd > 0) {
            var qty = usd / price;
            qtyDisplay.textContent = '= ' + (qty >= 1 ? qty.toFixed(4) : qty.toPrecision(4)) + ' ' + sym;
        } else {
            qtyDisplay.textContent = '';
        }
    }
    usdInput.addEventListener('input', updateQty);
    priceInput.addEventListener('input', updateQty);
    updateQty();
    function submit() {
        var price = parseFloat(priceInput.value);
        var usd = parseFloat(usdInput.value);
        var label = document.getElementById('spot-label-input').value.trim();
        var qty = (price > 0 && usd > 0) ? usd / price : 0;
        dialog.remove();
        callback(price || entryPrice, usd || 0, qty, label);
    }
    document.getElementById('spot-ok-btn').addEventListener('click', submit);
    document.getElementById('spot-cancel-btn').addEventListener('click', function() { dialog.remove(); callback(null); });
    dialog.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); dialog.remove(); callback(null); }
        e.stopPropagation();
    });
}

// ── Spot Sell Picker (toolbar "Sell" button) ─────────────────────────────
function showSpotSellPicker() {
    var sym = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : '';
    if (!sym) return;

    // Get open (unsold) spots for this asset
    var spots = [];
    try {
        var store = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        (store[sym] || []).forEach(function(d) {
            if (d.name === 'spot' && d.points && d.points.length >= 1 && !(d.extendData && d.extendData.sold)) {
                spots.push(d);
            }
        });
    } catch(e) {}

    if (spots.length === 0) {
        if (typeof showToast === 'function') showToast('No open spot positions for ' + sym);
        return;
    }

    // Single spot: open sell dialog directly
    if (spots.length === 1) {
        showSpotSellDialog(spots[0].id, spots[0]);
        return;
    }

    // Multiple spots: show picker
    var existing = document.getElementById('spot-sell-picker');
    if (existing) existing.remove();

    function fmtP(p) { return Math.abs(p) >= 1 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6)); }

    var picker = document.createElement('div');
    picker.id = 'spot-sell-picker';
    picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:340px;background:#1e222d;border:1px solid #ef5350;border-radius:8px;z-index:26003;display:flex;flex-direction:column;padding:16px;box-shadow:0 0 20px rgba(239,83,80,0.15),0 8px 32px rgba(0,0,0,0.6);font-family:JetBrains Mono,monospace;max-height:60vh;overflow-y:auto';

    var html = '<div style="color:#ef5350;font-size:13px;font-weight:600;margin-bottom:12px">Sell Spot — ' + escapeHtml(sym) + ' (' + spots.length + ' positions)</div>';
    spots.forEach(function(s, i) {
        var ext = s.extendData || {};
        var entry = s.points[0].value;
        if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && entry > 0) entry = Math.exp(entry);
        var qty = ext.quantity || 0;
        var usd = ext.usdAmount || 0;
        var label = ext.label || '';
        var dateStr = '';
        if (s.points[0].timestamp) {
            var dt = new Date(s.points[0].timestamp);
            dateStr = dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        }
        html += '<div class="spot-pick-item" data-idx="' + i + '" style="display:flex;align-items:center;gap:10px;padding:8px 10px;cursor:pointer;border:1px solid #2a2e39;border-radius:4px;margin-bottom:4px;transition:background 0.15s">' +
            '<span style="color:#787b86;font-size:11px;width:50px">' + dateStr + '</span>' +
            '<span style="color:#d1d4dc;font-size:12px;flex:1">$' + fmtP(entry) + '</span>' +
            '<span style="color:#787b86;font-size:11px">' + (usd > 0 ? '$' + usd.toFixed(0) : '') + '</span>' +
            '<span style="color:#4db6ac;font-size:11px">' + escapeHtml(label) + '</span>' +
        '</div>';
    });
    html += '<button id="spot-pick-cancel" style="margin-top:8px;background:#2a2e39;border:1px solid #363a45;border-radius:4px;padding:6px 14px;color:#787b86;cursor:pointer;font-size:12px;font-family:inherit;align-self:flex-end">Cancel</button>';
    picker.innerHTML = html;
    document.body.appendChild(picker);

    picker.querySelectorAll('.spot-pick-item').forEach(function(el) {
        el.addEventListener('mouseenter', function() { el.style.background = '#2a2e39'; });
        el.addEventListener('mouseleave', function() { el.style.background = ''; });
        el.addEventListener('click', function() {
            var idx = parseInt(el.getAttribute('data-idx'));
            picker.remove();
            showSpotSellDialog(spots[idx].id, spots[idx]);
        });
    });
    document.getElementById('spot-pick-cancel').addEventListener('click', function() { picker.remove(); });
    picker.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') { picker.remove(); }
        e.stopPropagation();
    });
}

// ── Spot Sell Dialog ─────────────────────────────────────────────────────
function showSpotSellDialog(overlayId, spotData, onComplete) {
    var existing = document.getElementById('spot-sell-dialog');
    if (existing) existing.remove();

    var ext = spotData.extendData || {};
    var entryPrice = (spotData.points && spotData.points[0]) ? spotData.points[0].value : 0;
    // Log scale: entry price stored as log(price)
    if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && entryPrice > 0) {
        entryPrice = Math.exp(entryPrice);
    }
    var totalQty = ext.quantity || 0;
    var costUsd = ext.usdAmount || 0;
    var sym = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : '';

    // Get current price for pre-fill
    var currentPrice = 0;
    try {
        var dl = klineChart.getDataList();
        if (dl && dl.length > 0) {
            var last = dl[dl.length - 1];
            currentPrice = last.close || 0;
            if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && currentPrice > 0) {
                currentPrice = Math.exp(currentPrice);
            }
        }
    } catch(e) {}

    function fmtP(p) { return Math.abs(p) >= 1 ? p.toFixed(2) : (Math.abs(p) >= 0.01 ? p.toFixed(4) : p.toFixed(6)); }
    function fmtQ(q) { return q >= 1 ? q.toFixed(4) : q.toPrecision(4); }

    var dialog = document.createElement('div');
    dialog.id = 'spot-sell-dialog';
    dialog.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:320px;background:#1e222d;border:1px solid #ef5350;border-radius:8px;z-index:26003;display:flex;flex-direction:column;padding:16px;box-shadow:0 0 20px rgba(239,83,80,0.15),0 8px 32px rgba(0,0,0,0.6);font-family:JetBrains Mono,monospace';
    dialog.innerHTML =
        '<div style="color:#ef5350;font-size:13px;font-weight:600;margin-bottom:12px">Sell Spot — ' + escapeHtml(sym) + '</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;margin-bottom:10px">' +
            '<div style="display:flex;justify-content:space-between;color:#787b86;font-size:11px"><span>Entry</span><span style="color:#d1d4dc">$' + fmtP(entryPrice) + '</span></div>' +
            '<div style="display:flex;justify-content:space-between;color:#787b86;font-size:11px"><span>Cost</span><span style="color:#d1d4dc">$' + (costUsd ? costUsd.toFixed(2) : '—') + '</span></div>' +
            '<div style="display:flex;justify-content:space-between;color:#787b86;font-size:11px"><span>Total Qty</span><span style="color:#d1d4dc">' + fmtQ(totalQty) + ' ' + escapeHtml(sym) + '</span></div>' +
        '</div>' +
        '<div style="height:1px;background:#2a2e39;margin-bottom:10px"></div>' +
        '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px">' +
            '<div style="display:flex;align-items:center;gap:8px">' +
                '<label style="color:#787b86;font-size:11px;width:65px">Sell Price</label>' +
                '<input type="text" id="spot-sell-price" value="' + fmtP(currentPrice) + '" autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other" style="flex:1;background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-size:13px;outline:none;font-family:inherit">' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
                '<label style="color:#787b86;font-size:11px;width:65px">Sell Qty</label>' +
                '<input type="text" id="spot-sell-qty" value="' + fmtQ(totalQty) + '" autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other" style="flex:1;background:#131722;border:1px solid #2a2e39;border-radius:4px;padding:6px 8px;color:#d1d4dc;font-size:13px;outline:none;font-family:inherit">' +
                '<button id="spot-sell-all-btn" style="background:#2a2e39;border:1px solid #363a45;border-radius:4px;padding:4px 8px;color:#787b86;cursor:pointer;font-size:10px;font-family:inherit">All</button>' +
            '</div>' +
        '</div>' +
        '<div id="spot-sell-pnl" style="text-align:center;font-size:13px;font-weight:600;padding:8px;background:#131722;border-radius:4px;margin-bottom:12px"></div>' +
        '<div style="display:flex;gap:8px;justify-content:flex-end">' +
            '<button id="spot-sell-cancel" style="background:#2a2e39;border:1px solid #363a45;border-radius:4px;padding:6px 14px;color:#787b86;cursor:pointer;font-size:12px;font-family:inherit">Cancel</button>' +
            '<button id="spot-sell-confirm" style="background:#ef5350;border:none;border-radius:4px;padding:6px 14px;color:#fff;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit">Sell</button>' +
        '</div>';
    document.body.appendChild(dialog);

    var sellPriceInput = document.getElementById('spot-sell-price');
    var sellQtyInput = document.getElementById('spot-sell-qty');
    var pnlDisplay = document.getElementById('spot-sell-pnl');

    function updatePnl() {
        var sp = parseFloat(sellPriceInput.value);
        var sq = parseFloat(sellQtyInput.value);
        if (!sp || !sq || sp <= 0 || sq <= 0 || entryPrice <= 0) {
            pnlDisplay.textContent = '—';
            pnlDisplay.style.color = '#787b86';
            return;
        }
        var sellValue = sp * sq;
        var costBasis = entryPrice * sq;
        var pnlUsd = sellValue - costBasis;
        var pnlPct = ((sp - entryPrice) / entryPrice) * 100;
        var color = pnlUsd >= 0 ? '#26a69a' : '#ef5350';
        var sign = pnlUsd >= 0 ? '+' : '';
        pnlDisplay.innerHTML = '<span style="color:' + color + '">' + sign + '$' + pnlUsd.toFixed(2) + ' (' + sign + pnlPct.toFixed(2) + '%)</span>';
    }
    sellPriceInput.addEventListener('input', updatePnl);
    sellQtyInput.addEventListener('input', updatePnl);
    updatePnl();
    sellPriceInput.focus();
    sellPriceInput.select();

    document.getElementById('spot-sell-all-btn').addEventListener('click', function() {
        sellQtyInput.value = fmtQ(totalQty);
        updatePnl();
    });

    function submit() {
        var sp = parseFloat(sellPriceInput.value);
        var sq = parseFloat(sellQtyInput.value);
        if (!sp || sp <= 0) return;
        if (!sq || sq <= 0) sq = totalQty;
        if (sq > totalQty) sq = totalQty;
        dialog.remove();
        closeSpotWithJournal(overlayId, spotData, { sellPrice: sp, sellQty: sq }, onComplete);
    }
    document.getElementById('spot-sell-confirm').addEventListener('click', submit);
    document.getElementById('spot-sell-cancel').addEventListener('click', function() { dialog.remove(); });
    dialog.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); dialog.remove(); }
        e.stopPropagation();
    });
}

function closeSpotWithJournal(overlayId, spotData, sellData, onComplete) {
    var ext = spotData.extendData || {};
    var entryPrice = (spotData.points && spotData.points[0]) ? spotData.points[0].value : 0;
    if (typeof _logScaleEnabled !== 'undefined' && _logScaleEnabled && entryPrice > 0) {
        entryPrice = Math.exp(entryPrice);
    }
    var totalQty = ext.quantity || 0;
    var costUsd = ext.usdAmount || 0;
    var sellPrice = sellData.sellPrice;
    var sellQty = sellData.sellQty;
    var isPartial = sellQty < totalQty * 0.999; // float tolerance
    var pnlCurrency = (sellPrice - entryPrice) * sellQty;
    var sym = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : '';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';

    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    // Journal integration: create entry + close it
    function journalClose(journalId) {
        if (!journalId) { finalize(); return; }
        fetch('/api/journal/trades/' + journalId + '/close', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            credentials: 'same-origin',
            body: JSON.stringify({
                exit_price: sellPrice,
                exit_reason: 'manual',
                size: sellQty,
                pnl_currency: pnlCurrency
            })
        }).then(function() {
            if (typeof refreshJournal === 'function') refreshJournal();
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal close (spot sell) failed — position finalized locally, journal entry not closed', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Journal entry could not be closed — check your connection and try again');
        });
        finalize(journalId);
    }

    // Fallback for spots with no known promoted trade: pre-feature drawings,
    // or a promotion that failed at buy time. Creates a fresh entry then
    // immediately closes it (the old, pre-promotion behaviour).
    function openThenClose() {
        if (typeof autoOpenJournalEntry === 'function') {
            autoOpenJournalEntry({
                asset: sym,
                timeframe: tf,
                direction: 'long',
                entry_price: entryPrice,
                size: isPartial ? sellQty : totalQty,
            }).then(function(journalId) {
                journalClose(journalId);
            }).catch(function() { finalize(); });
        } else {
            finalize();
        }
    }

    // Close the trade this buy was promoted into — never log a fresh,
    // unrelated entry for an already-promoted buy, or the promoted trade
    // stays open forever and the book double-counts.
    var existingJournalId = ext._journalId;
    if (existingJournalId) {
        journalClose(existingJournalId);
    } else if (ext._journalDrawingId && typeof _findPromotedTradeId === 'function') {
        _findPromotedTradeId(ext._journalDrawingId, function(promotedId) {
            if (promotedId) {
                journalClose(promotedId);
            } else {
                openThenClose();
            }
        });
    } else {
        openThenClose();
    }

    function finalize(journalId) {
        // Mark sold in localStorage
        try {
            var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
            var drawings = allDrawings[sym] || [];
            var drawing = drawings.find(function(d) { return d.id === overlayId; });
            if (drawing) {
                if (!drawing.extendData) drawing.extendData = {};
                drawing.extendData.sold = true;
                drawing.extendData.soldPrice = sellPrice;
                drawing.extendData.soldQty = sellQty;
                drawing.extendData.soldAt = Date.now();
                if (journalId) drawing.extendData._journalId = journalId;

                // Partial sell: create remainder spot
                if (isPartial) {
                    var remainQty = totalQty - sellQty;
                    var remainUsd = entryPrice * remainQty;
                    var remainId = 'spot_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6);
                    var remainSpot = {
                        id: remainId,
                        name: 'spot',
                        points: JSON.parse(JSON.stringify(drawing.points)),
                        color: '#4db6ac',
                        extendData: { usdAmount: remainUsd, quantity: remainQty, label: (ext.label || '') + ' (remainder)' }
                    };
                    drawings.push(remainSpot);
                }

                allDrawings[sym] = drawings;
                localStorage.setItem('klineDrawings', JSON.stringify(allDrawings));
            }
        } catch(e) {}

        // Remove sold overlay from chart
        if (klineChart) {
            try { klineChart.removeOverlay(overlayId); } catch(e) {}
            // If partial, reload to show remainder
            if (isPartial) {
                try { loadKlineDrawings(sym); } catch(e) {}
            }
        }

        if (typeof updateDrawerPosition === 'function') updateDrawerPosition();
        if (typeof scheduleSyncToBackend === 'function') scheduleSyncToBackend();
        if (onComplete) onComplete();
    }
}
