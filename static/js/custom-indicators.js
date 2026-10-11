/* Custom Formula Indicators — editor modal, save/load, live preview, rendering.
 * Bridges FormulaParser + TALib (pure modules) with KLineChart + DOM.
 * Must load after: indicator-registry.js, ta-lib.js, formula-parser.js, before the chart-app files */
(function() {
    'use strict';

    var COLORS = ['#2196F3', '#FF9800', '#4CAF50', '#E91E63', '#9C27B0', '#00BCD4'];
    var MAX_FORMULAS_FREE = 3;
    var MAX_FORMULAS_PAID = 10;
    var MAX_FORMULAS_ADMIN = 20;
    var MAX_ROWS = 6;
    var _previewTimer = null;
    var _editingId = null; // null = new, string = editing existing
    var _previewRowIndex = 0;

    // ── Storage (localStorage + server sync) ───────────────────────────

    function getFormulas() {
        try {
            var raw = localStorage.getItem('customFormulas');
            return raw ? JSON.parse(raw) : [];
        } catch (e) { return []; }
    }

    function saveFormulas(formulas) {
        try { localStorage.setItem('customFormulas', JSON.stringify(formulas)); } catch (e) {}
    }

    // Server sync helpers (fire-and-forget with one retry)
    function _getCSRF() {
        return (typeof window !== 'undefined' && window.getCSRFToken) ? window.getCSRFToken() : '';
    }

    function _handleLimitHit(res, source) {
        // Translate server 403 limit_type responses into the shared
        // upgrade modal; only transient network/server errors should retry.
        return res.json().then(function(d) {
            if (res.status === 403 && typeof handleLimitHitResponse === 'function' && handleLimitHitResponse(d, source)) {
                return true; // handled — don't retry
            }
            return false;
        }).catch(function() { return false; });
    }

    function _serverSave(formula, retried) {
        fetch('/api/custom-formulas', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _getCSRF() },
            credentials: 'same-origin',
            body: JSON.stringify(formula)
        }).then(function(r) {
            if (r.status === 403) { return _handleLimitHit(r, 'custom_formula_limit'); }
        }).catch(function() {
            if (!retried) setTimeout(function() { _serverSave(formula, true); }, 3000);
        });
    }

    function _serverUpdate(formula, retried) {
        fetch('/api/custom-formulas/' + formula.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _getCSRF() },
            credentials: 'same-origin',
            body: JSON.stringify(formula)
        }).then(function(r) {
            if (r.status === 403) { return _handleLimitHit(r, 'custom_formula_limit'); }
        }).catch(function() {
            if (!retried) setTimeout(function() { _serverUpdate(formula, true); }, 3000);
        });
    }

    function _serverDelete(formulaId, retried) {
        fetch('/api/custom-formulas/' + formulaId, {
            method: 'DELETE',
            headers: { 'X-CSRFToken': _getCSRF() },
            credentials: 'same-origin'
        }).catch(function() {
            if (!retried) setTimeout(function() { _serverDelete(formulaId, true); }, 3000);
        });
    }

    function syncFromServer() {
        fetch('/api/custom-formulas', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                if (!data || !data.formulas) return;
                var serverFormulas = data.formulas.map(function(f) {
                    return {
                        id: f.id, name: f.name, formula: f.formula,
                        style: f.style, color: f.color, pane: f.pane,
                        lineWidth: f.line_width || 1,
                        created_at: f.created_at, updated_at: f.updated_at
                    };
                });
                // Server wins on conflict by updated_at
                var local = getFormulas();
                var merged = _mergeFormulas(local, serverFormulas);
                saveFormulas(merged);
                registerCustomIndicators();
                rebuildIndicatorMenu();
            }).catch(function() { /* offline — localStorage works standalone */ });
    }

    function _mergeFormulas(local, server) {
        var map = {};
        local.forEach(function(f) { map[f.id] = f; });
        server.forEach(function(f) {
            var existing = map[f.id];
            if (!existing || (f.updated_at && existing.updated_at && f.updated_at > existing.updated_at)) {
                map[f.id] = f;
            }
        });
        var result = [];
        for (var id in map) result.push(map[id]);
        return result.sort(function(a, b) { return (a.created_at || '') > (b.created_at || '') ? 1 : -1; });
    }

    function generateId() {
        return 'f' + Math.random().toString(36).substring(2, 8);
    }

    function getNextColor() {
        var formulas = getFormulas();
        return COLORS[formulas.length % COLORS.length];
    }

    // ── Registry Integration ────────────────────────────────────────────

    function registerCustomIndicators() {
        var formulas = getFormulas();
        var reg = window.IndicatorRegistry;
        if (!reg) return;

        // 'custom' category is defined in CATEGORIES at the top of indicator-registry.js

        // Register each formula as an indicator
        formulas.forEach(function(f) {
            var key = 'CUSTOM_' + f.id;
            reg.INDICATORS[key] = {
                label: f.name,
                shortLabel: f.name,
                category: 'custom',
                type: 'user_formula',
                formulaId: f.id,
                groupId: 'ind_custom_' + f.id
            };
            // Update the cached labels map so favourite pills show the name
            if (typeof klineIndicatorLabels !== 'undefined') {
                klineIndicatorLabels[key] = f.name;
            }
        });

        // Update CATEGORY_KEYS
        if (!window.IndicatorRegistry._CATEGORY_KEYS) {
            window.IndicatorRegistry._CATEGORY_KEYS = {};
        }
    }

    function getCustomCategoryKeys() {
        return getFormulas().map(function(f) { return 'CUSTOM_' + f.id; });
    }

    // ── Editor Modal ────────────────────────────────────────────────────

    function _isMobile() {
        return window.innerWidth <= 768 || /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
    }

    // ── PostHog Events ──────────────────────────────────────────────────

    function _track(event, props) {
        if (typeof posthog !== 'undefined' && posthog.capture) {
            posthog.capture(event, props || {});
        }
    }

    // ── Duplicate Detection ─────────────────────────────────────────────

    var _BUILTIN_PATTERNS = {
        'RSI': /^\s*RSI\s*\(\s*close\s*,\s*14\s*\)\s*$/i,
        'MACD': /^\s*MACD_(LINE|SIGNAL|HIST)\s*\(\s*(close\s*,\s*)?12\s*,\s*26\s*,\s*9\s*\)\s*$/i,
        'ATR': /^\s*ATR\s*\(\s*14\s*\)\s*$/i,
        'ADX': /^\s*ADX\s*\(\s*14\s*\)\s*$/i,
        'OBV': /^\s*OBV\s*\(\s*\)\s*$/i,
        'Bollinger Bands': /^\s*BB_(UPPER|LOWER|MID)\s*\(\s*(close\s*,\s*)?20\s*,?\s*2?\s*\)\s*$/i,
        'Stochastic': /^\s*STOCH_[KD]\s*\(\s*14\s*\)\s*$/i,
        'CCI': /^\s*CCI\s*\(\s*20\s*\)\s*$/i
    };

    function _checkDuplicateBuiltin(formulaText) {
        for (var name in _BUILTIN_PATTERNS) {
            if (_BUILTIN_PATTERNS[name].test(formulaText)) {
                return name;
            }
        }
        return null;
    }

    // ── Compound Formula Helpers ──────────────────────────────────────

    // Normalize a formula object to an array of row objects
    function _normalizeFormula(f) {
        var text = f.formula;
        if (text && text.charAt(0) === '{') {
            try {
                var parsed = JSON.parse(text);
                if (parsed.rows && Array.isArray(parsed.rows)) return parsed.rows;
            } catch (e) {}
        }
        // Legacy single-formula
        var pane = f.pane === 'new' ? 'own' : (f.pane || 'own');
        return [{ formula: text || '', style: f.style || 'line', color: f.color || '#2196F3', pane: pane, lineWidth: f.lineWidth || 1 }];
    }

    var _STYLE_OPTIONS_HTML =
        '<option value="line">Line</option>' +
        '<option value="stepline">Step Line</option>' +
        '<option value="area">Area</option>' +
        '<option value="histogram">Histogram</option>' +
        '<option value="dots">Dots</option>' +
        '<option value="circles">Circles</option>' +
        '<option value="hline">Ref. Line</option>' +
        '<optgroup label="Band">' +
        '<option value="band_upper">Band Upper</option>' +
        '<option value="band_lower">Band Lower</option>' +
        '</optgroup>' +
        '<optgroup label="Signals">' +
        '<option value="triangle_up">&#9650; Triangle Up</option>' +
        '<option value="triangle_down">&#9660; Triangle Down</option>' +
        '<option value="arrow_up">&#8593; Arrow Up</option>' +
        '<option value="arrow_down">&#8595; Arrow Down</option>' +
        '<option value="diamond">&#9670; Diamond</option>' +
        '<option value="cross">&#10005; Cross</option>' +
        '<option value="square">&#9632; Square</option>' +
        '</optgroup>';

    var _PANE_OPTIONS_HTML =
        '<option value="overlay">Overlay</option>' +
        '<option value="shared">Shared pane</option>' +
        '<option value="own">Own pane</option>';

    function _addEditorRow(index, rowData) {
        var container = document.getElementById('fe-rows');
        if (!container) return;

        var row = document.createElement('div');
        row.className = 'fe-row';
        row.setAttribute('data-row', index);

        var ta = document.createElement('textarea');
        ta.className = 'fe-row-formula';
        ta.rows = 1;
        ta.placeholder = 'SMA(close, 20)';
        ta.value = rowData ? rowData.formula : '';

        var controls = document.createElement('div');
        controls.className = 'fe-row-controls';

        var styleSelect = document.createElement('select');
        styleSelect.className = 'fe-row-style';
        styleSelect.innerHTML = _STYLE_OPTIONS_HTML;
        if (rowData) styleSelect.value = rowData.style || 'line';

        var colorPicker = document.createElement('input');
        colorPicker.type = 'color';
        colorPicker.className = 'fe-row-color';
        colorPicker.value = rowData ? (rowData.color || COLORS[index % COLORS.length]) : COLORS[index % COLORS.length];

        var paneSelect = document.createElement('select');
        paneSelect.className = 'fe-row-pane';
        paneSelect.innerHTML = _PANE_OPTIONS_HTML;
        if (rowData) paneSelect.value = rowData.pane || 'own';

        // Signal styles force overlay
        if (rowData && _SIGNAL_STYLES[rowData.style]) {
            paneSelect.value = 'overlay';
            paneSelect.disabled = true;
        }

        var removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'fe-row-remove';
        removeBtn.innerHTML = '&times;';
        removeBtn.title = 'Remove row';

        controls.appendChild(styleSelect);
        controls.appendChild(colorPicker);
        controls.appendChild(paneSelect);
        controls.appendChild(removeBtn);
        row.appendChild(ta);
        row.appendChild(controls);
        container.appendChild(row);

        _updateRowButtons();
        return row;
    }

    function _updateRowButtons() {
        var rows = document.querySelectorAll('#fe-rows .fe-row');
        var removes = document.querySelectorAll('#fe-rows .fe-row-remove');
        removes.forEach(function(btn) {
            btn.style.display = rows.length > 1 ? '' : 'none';
        });
        var addBtn = document.getElementById('fe-add-row');
        if (addBtn) addBtn.style.display = rows.length >= MAX_ROWS ? 'none' : '';
    }

    function _reindexRows() {
        var rows = document.querySelectorAll('#fe-rows .fe-row');
        rows.forEach(function(r, i) { r.setAttribute('data-row', i); });
    }

    // ── Editor Modal ────────────────────────────────────────────────────

    function openFormulaEditor(formulaId) {
        if (_isMobile()) {
            // The editor doesn't fit on a phone. Instead of silently
            // returning, tell the user where to open it so they know the
            // feature exists.
            var msg = 'Custom formulas need the desktop editor. Open Carrac on a laptop to build and preview them.';
            if (typeof showToast === 'function') showToast(msg);
            else if (typeof alert === 'function') alert(msg);
            return;
        }
        if (!window._hasPremiumFeatures) {
            if (typeof showUpgradeModal === 'function')
                showUpgradeModal('Custom indicators and formulas are available with a Premium subscription.');
            return;
        }
        var modal = document.getElementById('formula-editor-modal');
        if (!modal) return;

        _editingId = formulaId || null;
        _previewRowIndex = 0;
        var formula = null;
        if (_editingId) {
            var all = getFormulas();
            for (var i = 0; i < all.length; i++) {
                if (all[i].id === _editingId) { formula = all[i]; break; }
            }
        }

        document.getElementById('fe-name').value = formula ? formula.name : '';

        // Build rows
        var container = document.getElementById('fe-rows');
        if (container) container.innerHTML = '';

        var rows = formula ? _normalizeFormula(formula) : [{ formula: '', style: 'line', color: getNextColor(), pane: 'overlay' }];
        for (var i = 0; i < rows.length; i++) {
            _addEditorRow(i, rows[i]);
        }

        var deleteBtn = document.getElementById('fe-delete-btn');
        deleteBtn.style.display = _editingId ? '' : 'none';

        modal.classList.add('active');
        document.body.style.overflow = 'hidden';
        if (window.a11y) a11y.trapFocus(modal.querySelector('[role="dialog"]'));

        clearPreview();
        if (formula) runPreview();

        setTimeout(function() { document.getElementById('fe-name').focus(); }, 100);
    }

    function closeFormulaEditor() {
        var modal = document.getElementById('formula-editor-modal');
        if (!modal) return;
        if (window.a11y) a11y.releaseFocus(modal.querySelector('[role="dialog"]'));
        modal.classList.remove('active');
        document.body.style.overflow = '';
        _editingId = null;
        if (_previewTimer) { clearTimeout(_previewTimer); _previewTimer = null; }
    }

    function saveFormula() {
        var name = document.getElementById('fe-name').value.trim();
        if (!name) { showPreviewError('Name is required'); return; }
        if (name.length > 30) { showPreviewError('Name must be 30 characters or less'); return; }

        // Collect rows from editor
        var rowEls = document.querySelectorAll('#fe-rows .fe-row');
        if (!rowEls.length) { showPreviewError('At least one formula row is required'); return; }

        var rows = [];
        for (var ri = 0; ri < rowEls.length; ri++) {
            var formulaText = rowEls[ri].querySelector('.fe-row-formula').value.trim();
            if (!formulaText) { showPreviewError('Row ' + (ri + 1) + ': Formula is required'); return; }

            var parseResult = window.FormulaParser.parse(formulaText);
            if (parseResult.error) {
                showPreviewError('Row ' + (ri + 1) + ': ' + parseResult.error);
                _track('custom_indicator_error', { error_type: 'parse_error' });
                return;
            }

            rows.push({
                formula: formulaText,
                style: rowEls[ri].querySelector('.fe-row-style').value,
                color: rowEls[ri].querySelector('.fe-row-color').value,
                pane: rowEls[ri].querySelector('.fe-row-pane').value
            });
        }

        // Build formula field: single row → legacy format, multi-row → compound JSON
        var formulaField, style, color, pane, lineWidth;
        if (rows.length === 1) {
            formulaField = rows[0].formula;
            style = rows[0].style;
            color = rows[0].color;
            pane = rows[0].pane === 'own' ? 'new' : rows[0].pane;
            lineWidth = 1;
        } else {
            formulaField = JSON.stringify({ rows: rows });
            style = rows[0].style;
            color = rows[0].color;
            pane = rows[0].pane === 'own' ? 'new' : rows[0].pane;
            lineWidth = 1;
        }

        // Duplicate detection for single-formula
        if (rows.length === 1) {
            var dupName = _checkDuplicateBuiltin(formulaField);
            if (dupName) {
                showPreviewError('Note: This is similar to the built-in ' + dupName + ' indicator. Saved anyway.');
            }
        }

        var formulas = getFormulas();
        var maxFormulas = window._isAdmin ? MAX_FORMULAS_ADMIN :
                          window._isPremium ? MAX_FORMULAS_PAID : MAX_FORMULAS_FREE;

        if (_editingId) {
            for (var i = 0; i < formulas.length; i++) {
                if (formulas[i].id === _editingId) {
                    formulas[i].name = name;
                    formulas[i].formula = formulaField;
                    formulas[i].style = style;
                    formulas[i].color = color;
                    formulas[i].pane = pane;
                    formulas[i].lineWidth = lineWidth;
                    formulas[i].updated_at = new Date().toISOString();
                    break;
                }
            }
        } else {
            if (formulas.length >= maxFormulas) {
                showPreviewError('Maximum ' + maxFormulas + ' custom indicators');
                return;
            }
            var newFormula = {
                id: generateId(),
                name: name,
                formula: formulaField,
                style: style,
                color: color,
                pane: pane,
                lineWidth: lineWidth,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString()
            };
            formulas.push(newFormula);
            if (typeof klineActiveIndicators !== 'undefined') {
                klineActiveIndicators['CUSTOM_' + newFormula.id] = true;
            }
        }

        saveFormulas(formulas);

        if (_editingId) {
            var updated = formulas.filter(function(f) { return f.id === _editingId; })[0];
            if (updated) _serverUpdate(updated);
        } else {
            _serverSave(newFormula);
            _track('custom_indicator_created', { name: name, style: style, pane: pane, rows: rows.length });
        }

        registerCustomIndicators();
        rebuildIndicatorMenu();
        closeFormulaEditor();

        if (typeof klineChart !== 'undefined' && klineChart && klineChart.getDataList) {
            var dl = klineChart.getDataList();
            if (dl && dl.length) {
                drawCustomIndicators(klineChart, dl);
            }
        }
    }

    function deleteFormula() {
        if (!_editingId) return;
        var deletedId = _editingId;
        var key = 'CUSTOM_' + deletedId;
        var formulas = getFormulas().filter(function(f) { return f.id !== deletedId; });
        saveFormulas(formulas);

        // Sync delete to server
        _serverDelete(deletedId);
        _track('custom_indicator_deleted');

        // Remove from active indicators
        if (typeof klineActiveIndicators !== 'undefined') {
            delete klineActiveIndicators[key];
            try { localStorage.setItem('klineActiveIndicators', JSON.stringify(klineActiveIndicators)); } catch (e) {}
        }

        // Remove from favourites
        if (typeof klineFavouriteIndicators !== 'undefined') {
            var favIdx = klineFavouriteIndicators.indexOf(key);
            if (favIdx >= 0) {
                klineFavouriteIndicators.splice(favIdx, 1);
                try { localStorage.setItem('klineFavouriteIndicators', JSON.stringify(klineFavouriteIndicators)); } catch (e) {}
            }
        }

        // Remove from indicator groups
        if (typeof klineIndicatorGroups !== 'undefined') {
            klineIndicatorGroups.forEach(function(g) {
                var idx = g.indicators.indexOf(key);
                if (idx >= 0) g.indicators.splice(idx, 1);
            });
            try { localStorage.setItem('klineIndicatorGroups', JSON.stringify(klineIndicatorGroups)); } catch (e) {}
        }

        // Remove from registry
        var reg = window.IndicatorRegistry;
        if (reg) delete reg.INDICATORS[key];

        // Remove panes from chart (both legacy and row-indexed)
        if (typeof klineChart !== 'undefined' && klineChart) {
            try { klineChart.removeIndicator('formula_pane_' + deletedId); } catch (e) {}
            try { klineChart.removeIndicator('candle_pane', 'CVX_F_' + deletedId); } catch (e) {}
            for (var r = 0; r < MAX_ROWS; r++) {
                try { klineChart.removeIndicator('formula_pane_' + deletedId + '_' + r); } catch (e) {}
                try { klineChart.removeIndicator('candle_pane', 'CVX_F_' + deletedId + '_' + r); } catch (e) {}
            }
        }

        // Clean up formula results (both legacy and row-indexed)
        if (window._formulaResults) {
            delete window._formulaResults[deletedId];
            for (var r = 0; r < MAX_ROWS; r++) {
                delete window._formulaResults[deletedId + '_' + r];
            }
        }

        registerCustomIndicators();
        rebuildIndicatorMenu();

        // Remove favourite pill from DOM
        var pill = document.querySelector('.ind-fav-pill[data-ind="' + key + '"]');
        if (pill) pill.remove();

        closeFormulaEditor();
    }

    // ── Indicator Menu Rebuild ──────────────────────────────────────────

    function rebuildIndicatorMenu() {
        var menu = document.getElementById('indicator-menu');
        if (!menu) return;
        menu.innerHTML = window.IndicatorRegistry.buildIndicatorMenuHTML();

        // Re-attach event listeners + star icons (same pattern as the indicator menu handlers in chart-events.js)
        var _premiumInds = { CVD_USDT:1, CVD_COIN:1, OI:1, DERIV_COMBINED:1 };
        var items = menu.querySelectorAll('.indicator-menu-item');
        items.forEach(function(item) {
            var ind = item.getAttribute('data-ind');
            if (!ind) return;

            // Star icon for favourites
            var star = document.createElement('span');
            star.className = 'ind-fav-star' + (typeof klineFavouriteIndicators !== 'undefined' && klineFavouriteIndicators.indexOf(ind) >= 0 ? ' active' : '');
            star.setAttribute('data-ind', ind);
            star.innerHTML = '&#9733;';
            star.addEventListener('click', function(e) {
                e.stopPropagation();
                if (typeof toggleFavouriteIndicator === 'function') toggleFavouriteIndicator(this.getAttribute('data-ind'));
            });
            item.style.position = 'relative';
            item.appendChild(star);

            // Indicator toggle on click
            item.addEventListener('click', function(e) {
                e.stopPropagation();
                if (_premiumInds[ind] && !window._isPremium) {
                    if (typeof showToast === 'function') showToast('Derivatives indicators require Premium.');
                    return;
                }
                if (typeof toggleKlineIndicator === 'function') toggleKlineIndicator(ind);
            });

            // Right-click to edit custom indicators
            if (ind.indexOf('CUSTOM_') === 0) {
                item.addEventListener('contextmenu', function(e) {
                    e.preventDefault();
                    openFormulaEditor(ind.replace('CUSTOM_', ''));
                });
            }
        });

        // "New Custom Indicator" button
        _attachNewButtons();

        // Re-render favourite pills + lucide icons
        if (typeof renderFavouritePills === 'function') renderFavouritePills();
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }

    // ── Live Preview ────────────────────────────────────────────────────

    function runPreview() {
        var rowEls = document.querySelectorAll('#fe-rows .fe-row');
        if (!rowEls.length) { clearPreview(); return; }

        var rowIdx = Math.min(_previewRowIndex, rowEls.length - 1);
        var row = rowEls[rowIdx];
        var formulaText = row.querySelector('.fe-row-formula').value.trim();
        var previewColor = row.querySelector('.fe-row-color').value;

        var canvas = document.getElementById('fe-preview-canvas');
        var status = document.getElementById('fe-preview-status');
        if (!formulaText) { clearPreview(); return; }

        var ohlcv = extractOHLCVForFormula();
        if (!ohlcv) {
            status.textContent = 'Load a chart first';
            status.className = 'fe-preview-status fe-error';
            return;
        }

        var result = window.FormulaParser.evaluate(formulaText, ohlcv);
        if (result.error) {
            showPreviewError(result.error);
            clearCanvas(canvas);
            return;
        }

        drawSparkline(canvas, result.values, previewColor);

        var min = Infinity, max = -Infinity, allBool = true;
        for (var i = 0; i < result.values.length; i++) {
            var v = result.values[i];
            if (isNaN(v)) continue;
            if (v < min) min = v;
            if (v > max) max = v;
            if (v !== 0 && v !== 1 && !(v >= 0 && v <= 5 && v === Math.floor(v))) allBool = false;
        }

        var rowLabel = rowEls.length > 1 ? 'Row ' + (rowIdx + 1) + ' — ' : '';
        var rangeText = rowLabel + 'Range: ' + (min === Infinity ? '—' : min.toFixed(2)) +
                        ' to ' + (max === -Infinity ? '—' : max.toFixed(2));
        status.textContent = rangeText;
        status.className = 'fe-preview-status fe-ok';

        // Boolean auto-detection: suggest signal style
        if (allBool && min !== Infinity) {
            var styleSelect = row.querySelector('.fe-row-style');
            if (styleSelect && styleSelect.value === 'line') {
                status.textContent = rangeText + ' — Signal output detected, consider ▲ Up or ▼ Down style';
            }
        }
    }

    function showPreviewError(msg) {
        var status = document.getElementById('fe-preview-status');
        if (status) {
            status.textContent = msg;
            status.className = 'fe-preview-status fe-error';
        }
    }

    function clearPreview() {
        var canvas = document.getElementById('fe-preview-canvas');
        var status = document.getElementById('fe-preview-status');
        if (canvas) clearCanvas(canvas);
        if (status) { status.textContent = ''; status.className = 'fe-preview-status'; }
    }

    function clearCanvas(canvas) {
        var ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
    }

    function drawSparkline(canvas, values, color) {
        var ctx = canvas.getContext('2d');
        var w = canvas.width, h = canvas.height;
        ctx.clearRect(0, 0, w, h);

        // Filter valid values
        var valid = [];
        for (var i = 0; i < values.length; i++) {
            if (!isNaN(values[i])) valid.push({ i: i, v: values[i] });
        }
        if (valid.length < 2) return;

        var min = Infinity, max = -Infinity;
        for (var i = 0; i < valid.length; i++) {
            if (valid[i].v < min) min = valid[i].v;
            if (valid[i].v > max) max = valid[i].v;
        }
        var range = max - min || 1;

        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        var started = false;
        for (var i = 0; i < valid.length; i++) {
            var x = (valid[i].i / (values.length - 1)) * w;
            var y = h - 4 - ((valid[i].v - min) / range) * (h - 8);
            if (!started) { ctx.moveTo(x, y); started = true; }
            else ctx.lineTo(x, y);
        }
        ctx.stroke();
    }

    // ── OHLCV Extraction ────────────────────────────────────────────────

    function extractOHLCVFromData(dataList) {
        if (!dataList || !dataList.length) return null;
        var realLen = dataList._realLength || dataList.length;
        var o = [], h = [], l = [], c = [], v = [];
        for (var i = 0; i < realLen; i++) {
            var bar = dataList[i];
            if (!bar) continue;
            // Use _orig* fields when available (log scale stores real prices there)
            o.push(bar._origOpen || bar.open);
            h.push(bar._origHigh || bar.high);
            l.push(bar._origLow || bar.low);
            c.push(bar._origClose || bar.close);
            v.push(bar.volume || 0);
        }
        if (o.length === 0) return null;
        return { open: o, high: h, low: l, close: c, volume: v };
    }

    // Fallback: get data from chart instance
    function extractOHLCVForFormula() {
        if (typeof klineChart !== 'undefined' && klineChart && klineChart.getDataList) {
            return extractOHLCVFromData(klineChart.getDataList());
        }
        return null;
    }

    // ── Quant Data Cache & Fetch ───────────────────────────────────────
    // Caches quant series (M2, FEAR_GREED, etc.) fetched from the bridge API.
    // Key: series name (uppercase). Value: aligned number[] matching OHLCV length.
    // Cleared on symbol/TF change.

    var _quantCache = {};
    var _quantFetching = {};  // in-flight requests

    function clearQuantCache() {
        _quantCache = {};
        _quantFetching = {};
    }

    // Collect all quant deps from all enabled formulas
    function _getAllQuantDeps(formulas) {
        var deps = {};
        var FP = window.FormulaParser;
        if (!FP || !FP.getQuantDeps) return [];
        formulas.forEach(function(f) {
            var rows = _normalizeFormula(f);
            rows.forEach(function(row) {
                var d = FP.getQuantDeps(row.formula);
                d.forEach(function(s) { deps[s] = true; });
            });
        });
        return Object.keys(deps);
    }

    // Merge cached quant data into ohlcv context (lowercase keys for parser)
    function _mergeQuantIntoOHLCV(ohlcv) {
        var QV = window.FormulaParser.QUANT_VARS;
        for (var ident in QV) {
            var series = QV[ident];
            if (_quantCache[series]) {
                ohlcv[ident] = _quantCache[series];
            }
        }
        return ohlcv;
    }

    // Fetch quant series from bridge API and trigger re-draw on completion
    function _fetchQuantData(deps, timestamps, chart, chartData) {
        // Filter to series not already cached or in-flight
        var needed = deps.filter(function(s) { return !_quantCache[s] && !_quantFetching[s]; });
        if (!needed.length) return;

        needed.forEach(function(s) { _quantFetching[s] = true; });

        var url = '/api/quant/chart-series?series=' + encodeURIComponent(needed.join(',')) +
                  '&timestamps=' + encodeURIComponent(JSON.stringify(timestamps));

        fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.series) {
                    for (var name in data.series) {
                        var vals = data.series[name].values;
                        // Convert null to NaN for formula compatibility
                        _quantCache[name] = vals.map(function(v) { return v === null ? NaN : v; });
                    }
                }
                needed.forEach(function(s) { delete _quantFetching[s]; });
                // Re-draw custom indicators with quant data now available
                if (chart && chartData) {
                    drawCustomIndicators(chart, chartData);
                }
            })
            .catch(function(err) {
                console.warn('[custom-ind] Quant fetch failed:', err);
                needed.forEach(function(s) { delete _quantFetching[s]; });
            });
    }

    // ── Chart Rendering ─────────────────────────────────────────────────
    // Uses KLineChart's native indicator system instead of overlays.
    // Each formula registers a KLineChart indicator whose calc() returns
    // the pre-computed values. KLineChart handles Y-axis, drawing, crosshair.

    window._formulaResults = {};
    var _registeredInds = {};
    var _SIGNAL_STYLES = { triangle_up: 1, triangle_down: 1, arrow_up: 1, arrow_down: 1, diamond: 1, cross: 1, square: 1 };

    // ── Custom Draw Helpers ──────────────────────────────────────────

    // Get X pixel coords for visible bar range (2 convertToPixel calls, rest interpolated)
    function _getVisibleXCoords(from, to) {
        if (typeof klineChart === 'undefined' || !klineChart) return null;
        var f = klineChart.convertToPixel({ dataIndex: from, value: 0 }, { paneId: 'candle_pane' });
        var l = klineChart.convertToPixel({ dataIndex: Math.max(from, to - 1), value: 0 }, { paneId: 'candle_pane' });
        if (!f || !l || isNaN(f.x) || isNaN(l.x)) return null;
        return { start: f.x, step: (to - from > 1) ? (l.x - f.x) / (to - from - 1) : 0, from: from };
    }

    function _drawAreaFill(params, fid, color) {
        var ctx = params.ctx;
        var bounding = params.bounding;
        var yAxis = params.yAxis;
        var visibleRange = params.visibleRange;
        var dataList = params.kLineDataList || params.dataList;
        if (!ctx || !bounding || !yAxis || !visibleRange || !dataList) return;

        var store = (window._formulaResults || {})[fid];
        if (!store) return;

        var from = visibleRange.from != null ? visibleRange.from : 0;
        var to = Math.min(visibleRange.to != null ? visibleRange.to : dataList.length, dataList.length);
        var xc = _getVisibleXCoords(from, to);
        if (!xc) return;

        ctx.save();
        ctx.globalAlpha = 0.15;
        ctx.fillStyle = color;
        ctx.beginPath();

        var started = false;
        var lastX = 0;
        for (var i = from; i < to; i++) {
            var bar = dataList[i];
            if (!bar) continue;
            var val = store[bar.timestamp];
            if (val === undefined || isNaN(val)) continue;
            var x = xc.start + (i - xc.from) * xc.step;
            var y = yAxis.convertToPixel(val);
            if (!isFinite(y)) continue;
            if (!started) {
                ctx.moveTo(x, bounding.height);
                ctx.lineTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, y);
            }
            lastX = x;
        }
        if (started) {
            ctx.lineTo(lastX, bounding.height);
            ctx.closePath();
            ctx.fill();
        }
        ctx.restore();
    }

    function _drawStepLine(params, fid, color, lw) {
        var ctx = params.ctx;
        var yAxis = params.yAxis;
        var visibleRange = params.visibleRange;
        var dataList = params.kLineDataList || params.dataList;
        if (!ctx || !yAxis || !visibleRange || !dataList) return;

        var store = (window._formulaResults || {})[fid];
        if (!store) return;

        var from = visibleRange.from != null ? visibleRange.from : 0;
        var to = Math.min(visibleRange.to != null ? visibleRange.to : dataList.length, dataList.length);
        var xc = _getVisibleXCoords(from, to);
        if (!xc) return;

        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = lw || 1;
        ctx.beginPath();

        var started = false;
        var prevY = 0;
        for (var i = from; i < to; i++) {
            var bar = dataList[i];
            if (!bar) continue;
            var val = store[bar.timestamp];
            if (val === undefined || isNaN(val)) continue;
            var x = xc.start + (i - xc.from) * xc.step;
            var y = yAxis.convertToPixel(val);
            if (!isFinite(y)) continue;
            if (!started) {
                ctx.moveTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, prevY);
                ctx.lineTo(x, y);
            }
            prevY = y;
        }
        ctx.stroke();
        ctx.restore();
    }

    // Candle-relative signals: Y at candle high/low + pixel offset
    var _CANDLE_SIGNALS = { triangle_up:1, triangle_down:1, arrow_up:1, arrow_down:1 };

    function _drawSignals(params, fid, color, signalType) {
        var ctx = params.ctx;
        var yAxis = params.yAxis;
        var visibleRange = params.visibleRange;
        var dataList = params.kLineDataList || params.dataList;
        if (!ctx || !yAxis || !visibleRange || !dataList) return;

        var store = (window._formulaResults || {})[fid];
        if (!store) return;

        var from = visibleRange.from != null ? visibleRange.from : 0;
        var to = Math.min(visibleRange.to != null ? visibleRange.to : dataList.length, dataList.length);
        var xc = _getVisibleXCoords(from, to);
        if (!xc) return;

        var size = 5;
        var offset = 8;
        var isCandlePos = !!_CANDLE_SIGNALS[signalType];
        var isUp = signalType.indexOf('_up') > 0;

        ctx.save();
        ctx.fillStyle = color;
        ctx.strokeStyle = color;

        for (var i = from; i < to; i++) {
            var bar = dataList[i];
            if (!bar) continue;
            var val = store[bar.timestamp];
            if (val === undefined || isNaN(val) || val === 0) continue;

            var x = xc.start + (i - xc.from) * xc.step;
            var py;

            if (isCandlePos) {
                var price = isUp ? (bar._origLow || bar.low) : (bar._origHigh || bar.high);
                py = yAxis.convertToPixel(price);
                if (!isFinite(py)) continue;
                py = isUp ? py + offset : py - offset;
            } else {
                py = yAxis.convertToPixel(val);
                if (!isFinite(py)) continue;
            }

            ctx.beginPath();
            switch (signalType) {
                case 'triangle_up':
                    ctx.moveTo(x, py);
                    ctx.lineTo(x - size, py + size * 1.7);
                    ctx.lineTo(x + size, py + size * 1.7);
                    ctx.closePath();
                    ctx.fill();
                    break;
                case 'triangle_down':
                    ctx.moveTo(x, py);
                    ctx.lineTo(x - size, py - size * 1.7);
                    ctx.lineTo(x + size, py - size * 1.7);
                    ctx.closePath();
                    ctx.fill();
                    break;
                case 'arrow_up':
                    ctx.moveTo(x, py);
                    ctx.lineTo(x - size * 0.7, py + size);
                    ctx.lineTo(x + size * 0.7, py + size);
                    ctx.closePath();
                    ctx.fill();
                    ctx.beginPath();
                    ctx.moveTo(x, py + size);
                    ctx.lineTo(x, py + size * 3);
                    ctx.lineWidth = 1.5;
                    ctx.stroke();
                    break;
                case 'arrow_down':
                    ctx.moveTo(x, py);
                    ctx.lineTo(x - size * 0.7, py - size);
                    ctx.lineTo(x + size * 0.7, py - size);
                    ctx.closePath();
                    ctx.fill();
                    ctx.beginPath();
                    ctx.moveTo(x, py - size);
                    ctx.lineTo(x, py - size * 3);
                    ctx.lineWidth = 1.5;
                    ctx.stroke();
                    break;
                case 'diamond':
                    ctx.moveTo(x, py - size);
                    ctx.lineTo(x + size, py);
                    ctx.lineTo(x, py + size);
                    ctx.lineTo(x - size, py);
                    ctx.closePath();
                    ctx.fill();
                    break;
                case 'cross':
                    ctx.moveTo(x - size, py - size);
                    ctx.lineTo(x + size, py + size);
                    ctx.moveTo(x + size, py - size);
                    ctx.lineTo(x - size, py + size);
                    ctx.lineWidth = 2;
                    ctx.stroke();
                    break;
                case 'square':
                    ctx.fillRect(x - size * 0.7, py - size * 0.7, size * 1.4, size * 1.4);
                    break;
            }
        }

        ctx.restore();
    }

    function _drawBandFill(params, upperFid, lowerFid, color) {
        var ctx = params.ctx;
        var bounding = params.bounding;
        var yAxis = params.yAxis;
        var visibleRange = params.visibleRange;
        var dataList = params.kLineDataList || params.dataList;
        if (!ctx || !bounding || !yAxis || !visibleRange || !dataList) return;

        var upperStore = (window._formulaResults || {})[upperFid];
        var lowerStore = (window._formulaResults || {})[lowerFid];
        if (!upperStore || !lowerStore) return;

        var from = visibleRange.from != null ? visibleRange.from : 0;
        var to = Math.min(visibleRange.to != null ? visibleRange.to : dataList.length, dataList.length);
        var xc = _getVisibleXCoords(from, to);
        if (!xc) return;

        var upperPts = [];
        var lowerPts = [];
        for (var i = from; i < to; i++) {
            var bar = dataList[i];
            if (!bar) continue;
            var uv = upperStore[bar.timestamp];
            var lv = lowerStore[bar.timestamp];
            if (uv === undefined || isNaN(uv) || lv === undefined || isNaN(lv)) continue;
            var x = xc.start + (i - xc.from) * xc.step;
            var uy = yAxis.convertToPixel(uv);
            var ly = yAxis.convertToPixel(lv);
            if (!isFinite(uy) || !isFinite(ly)) continue;
            upperPts.push({ x: x, y: uy });
            lowerPts.push({ x: x, y: ly });
        }

        if (upperPts.length < 2) return;

        ctx.save();
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(upperPts[0].x, upperPts[0].y);
        for (var i = 1; i < upperPts.length; i++) {
            ctx.lineTo(upperPts[i].x, upperPts[i].y);
        }
        for (var i = lowerPts.length - 1; i >= 0; i--) {
            ctx.lineTo(lowerPts[i].x, lowerPts[i].y);
        }
        ctx.closePath();
        ctx.fill();
        ctx.restore();
    }

    // ── Indicator Registration (style-aware) ─────────────────────────

    function _registerFormulaIndicator(fid, fname, color, lw, style, bandPairFid) {
        if (typeof klinecharts === 'undefined') return null;
        var indName = 'CVX_F_' + fid;

        var figures = [];
        var drawFn = null;

        if (style === 'band_upper') {
            figures = [{
                key: 'val', title: '', type: 'line',
                styles: function() { return { style: 'solid', size: lw || 1, color: color }; }
            }];
            if (bandPairFid) {
                drawFn = function(params) { _drawBandFill(params, fid, bandPairFid, color); };
            }
        } else if (style === 'band_lower') {
            figures = [{
                key: 'val', title: '', type: 'line',
                styles: function() { return { style: 'solid', size: lw || 1, color: color }; }
            }];
        } else if (_SIGNAL_STYLES[style]) {
            var _signalType = style;
            drawFn = function(params) { _drawSignals(params, fid, color, _signalType); };
        } else if (style === 'histogram') {
            figures = [{
                key: 'val', title: '', type: 'bar', baseValue: 0,
                styles: function(data) {
                    return { style: 'fill', color: (data.current && data.current.val >= 0) ? color : '#EF5350' };
                }
            }];
        } else if (style === 'area') {
            figures = [{
                key: 'val', title: '', type: 'line',
                styles: function() { return { style: 'solid', size: lw || 1, color: color }; }
            }];
            drawFn = function(params) { _drawAreaFill(params, fid, color); };
        } else if (style === 'stepline') {
            figures = [{
                key: 'val', title: '', type: 'line',
                styles: function() { return { style: 'solid', size: 0 }; }
            }];
            drawFn = function(params) { _drawStepLine(params, fid, color, lw); };
        } else if (style === 'circles') {
            figures = [{
                key: 'val', title: '', type: 'circle',
                styles: function() { return { style: 'stroke', color: color, size: 5, borderColor: color, borderSize: lw || 1.5 }; }
            }];
        } else if (style === 'hline') {
            figures = [{
                key: 'val', title: '', type: 'line',
                styles: function() { return { style: 'dashed', size: lw || 1, color: color, dashedValue: [6, 4] }; }
            }];
        } else {
            // 'line' and 'dots'
            var figType = (style === 'dots') ? 'circle' : 'line';
            figures = [{
                key: 'val', title: '', type: figType,
                styles: function() { return { style: 'solid', size: lw || 1, color: color }; }
            }];
        }

        try {
            var indConfig = {
                name: indName,
                shortName: fname || 'Custom',
                precision: 4,
                calc: function(dataList) {
                    var store = (window._formulaResults || {})[fid];
                    if (!store) return dataList.map(function() { return {}; });
                    return dataList.map(function(bar) {
                        var v = store[bar.timestamp];
                        return v !== undefined ? { val: v } : {};
                    });
                },
                figures: figures
            };
            if (drawFn) indConfig.draw = drawFn;
            klinecharts.registerIndicator(indConfig);
            _registeredInds[indName] = true;
        } catch (e) {
            console.warn('[custom-ind] registerIndicator error:', e);
        }
        return indName;
    }

    function _cleanupFormulaIndicators(chart, fid) {
        // Remove legacy single-formula indicator
        try { chart.removeIndicator('formula_pane_' + fid); } catch (e) {}
        try { chart.removeIndicator('candle_pane', 'CVX_F_' + fid); } catch (e) {}
        // Remove row-indexed indicators
        for (var r = 0; r < MAX_ROWS; r++) {
            try { chart.removeIndicator('formula_pane_' + fid + '_' + r); } catch (e) {}
            try { chart.removeIndicator('candle_pane', 'CVX_F_' + fid + '_' + r); } catch (e) {}
        }
    }

    function drawCustomIndicators(chart, chartData) {
        var formulas = getFormulas();
        if (!formulas.length) return;
        if (typeof klineActiveIndicators === 'undefined') return;

        // Remove panes for toggled-off custom indicators
        formulas.forEach(function(f) {
            var key = 'CUSTOM_' + f.id;
            if (klineActiveIndicators[key] === false) {
                _cleanupFormulaIndicators(chart, f.id);
            }
        });

        var ohlcv = extractOHLCVFromData(chartData);
        if (!ohlcv) return;
        var realLen = chartData._realLength || chartData.length;

        // Check for quant variable dependencies and fetch if needed
        var enabledFormulas = formulas.filter(function(f) {
            return klineActiveIndicators['CUSTOM_' + f.id] !== false;
        });
        var quantDeps = _getAllQuantDeps(enabledFormulas);
        if (quantDeps.length > 0) {
            // Extract timestamps for alignment
            var timestamps = [];
            for (var ti = 0; ti < realLen; ti++) {
                timestamps.push(chartData[ti].timestamp / 1000);  // KLineChart uses ms, bridge expects seconds
            }
            // Trigger fetch for any uncached series (re-draws on completion)
            _fetchQuantData(quantDeps, timestamps, chart, chartData);
            // Merge any already-cached quant data into ohlcv
            _mergeQuantIntoOHLCV(ohlcv);
        }

        formulas.forEach(function(f) {
            var key = 'CUSTOM_' + f.id;
            if (!(key in klineActiveIndicators)) {
                klineActiveIndicators[key] = true;
                try { localStorage.setItem('klineActiveIndicators', JSON.stringify(klineActiveIndicators)); } catch (e) {}
            }
            if (!klineActiveIndicators[key]) return;

            var rows = _normalizeFormula(f);

            // Clean up all stale placements before re-creating
            _cleanupFormulaIndicators(chart, f.id);

            // Find band_lower index for pairing with band_upper
            var bandLowerIdx = -1;
            for (var bi = 0; bi < rows.length; bi++) {
                if (rows[bi].style === 'band_lower') { bandLowerIdx = bi; break; }
            }

            rows.forEach(function(row, rowIdx) {
                // Server-side eval path for HTF / per-asset derivatives vars.
                // Fires async preview request then re-triggers the draw via setData.
                if (window.FormulaParser.needsServerEval && window.FormulaParser.needsServerEval(row.formula)) {
                    var sym = (window._currentSymbol || '').toUpperCase();
                    var tf = window._currentTimeframe || '1D';
                    if (!sym) return;
                    // Cache key guards against redundant parallel fetches for the same row.
                    var pendKey = 'CUSTOM_PREVIEW_' + f.id + '_' + rowIdx + '_' + sym + '_' + tf + '_' + row.formula;
                    if (window._pendingFormulaPreviews && window._pendingFormulaPreviews[pendKey]) return;
                    window._pendingFormulaPreviews = window._pendingFormulaPreviews || {};
                    window._pendingFormulaPreviews[pendKey] = true;
                    window.FormulaParser.evaluateServerSide(row.formula, sym, tf).then(function(res) {
                        delete window._pendingFormulaPreviews[pendKey];
                        if (res.error || !res.values) {
                            console.warn('[custom-ind] Server eval error:', f.name, 'row', rowIdx, res.error);
                            return;
                        }
                        var lookup2 = {};
                        for (var i = 0; i < realLen; i++) {
                            var v = res.values[i];
                            if (v !== null && !isNaN(v)) {
                                lookup2[chartData[i].timestamp] = v;
                            }
                        }
                        var fid2 = f.id + '_' + rowIdx;
                        window._formulaResults[fid2] = lookup2;
                        // Ask chart to redraw indicators by overriding data
                        try {
                            if (chart && chart.overrideIndicator) {
                                chart.overrideIndicator({ name: 'custom_' + fid2 }, 'candle_pane');
                                chart.overrideIndicator({ name: 'custom_' + fid2 }, 'formula_pane_' + f.id);
                                chart.overrideIndicator({ name: 'custom_' + fid2 }, 'formula_pane_' + f.id + '_' + rowIdx);
                            }
                        } catch (e) { /* best-effort redraw */ }
                    });
                    // Still register the indicator shell so panes appear; values populate on next tick.
                }
                var result = window.FormulaParser.evaluate(row.formula, ohlcv);
                if (result.error) {
                    // If the error is due to server-only features, swallow and let async path fill in.
                    if (window.FormulaParser.needsServerEval && window.FormulaParser.needsServerEval(row.formula)) {
                        // Create empty lookup so pane still registers
                        result = { values: [], error: null };
                    } else {
                        console.warn('[custom-ind] Formula error:', f.name, 'row', rowIdx, result.error);
                        return;
                    }
                }

                var lookup = {};
                for (var i = 0; i < realLen; i++) {
                    if (!isNaN(result.values[i])) {
                        lookup[chartData[i].timestamp] = result.values[i];
                    }
                }

                var fid = f.id + '_' + rowIdx;
                window._formulaResults[fid] = lookup;

                var color = row.color || '#2196F3';
                var lw = row.lineWidth || f.lineWidth || 1;
                var bandPairFid = (row.style === 'band_upper' && bandLowerIdx >= 0) ? f.id + '_' + bandLowerIdx : null;
                var indName = _registerFormulaIndicator(fid, f.name, color, lw, row.style, bandPairFid);
                if (!indName) return;

                var pane = row.pane || 'own';
                if (_SIGNAL_STYLES[row.style] || pane === 'overlay') {
                    chart.createIndicator({ name: indName }, true, { id: 'candle_pane' });
                } else if (pane === 'shared') {
                    chart.createIndicator(
                        { name: indName },
                        false,
                        { id: 'formula_pane_' + f.id, height: 70, minHeight: 40 }
                    );
                } else {
                    // 'own' — each row gets its own pane
                    chart.createIndicator(
                        { name: indName },
                        false,
                        { id: 'formula_pane_' + f.id + '_' + rowIdx, height: 70, minHeight: 40 }
                    );
                }
            });
        });
    }

    // ── Event Handlers ──────────────────────────────────────────────────

    function initEditorEvents() {
        var container = document.getElementById('fe-rows');
        if (!container) return;

        // Delegated input: formula textareas trigger preview
        container.addEventListener('input', function(e) {
            if (e.target.classList.contains('fe-row-formula') || e.target.classList.contains('fe-row-color')) {
                var row = e.target.closest('.fe-row');
                if (row) _previewRowIndex = parseInt(row.getAttribute('data-row')) || 0;
                if (_previewTimer) clearTimeout(_previewTimer);
                _previewTimer = setTimeout(runPreview, 500);
            }
        });

        // Delegated keydown: Tab inserts spaces, Ctrl+Enter saves
        container.addEventListener('keydown', function(e) {
            if (!e.target.classList.contains('fe-row-formula')) return;
            if (e.key === 'Tab') {
                e.preventDefault();
                var el = e.target;
                var start = el.selectionStart, end = el.selectionEnd;
                el.value = el.value.substring(0, start) + '  ' + el.value.substring(end);
                el.selectionStart = el.selectionEnd = start + 2;
            }
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                saveFormula();
            }
        });

        // Delegated change: style + pane + color
        container.addEventListener('change', function(e) {
            var row = e.target.closest('.fe-row');
            if (!row) return;

            if (e.target.classList.contains('fe-row-style')) {
                var style = e.target.value;
                var paneSelect = row.querySelector('.fe-row-pane');
                if (_SIGNAL_STYLES[style]) {
                    paneSelect.value = 'overlay';
                    paneSelect.disabled = true;
                } else {
                    paneSelect.disabled = false;
                }
            }

            _previewRowIndex = parseInt(row.getAttribute('data-row')) || 0;
            if (_previewTimer) clearTimeout(_previewTimer);
            _previewTimer = setTimeout(runPreview, 300);
        });

        // Delegated click: remove row
        container.addEventListener('click', function(e) {
            if (e.target.classList.contains('fe-row-remove') || e.target.closest('.fe-row-remove')) {
                var row = e.target.closest('.fe-row');
                if (row) {
                    row.remove();
                    _reindexRows();
                    _updateRowButtons();
                    _previewRowIndex = 0;
                    runPreview();
                }
            }
        });

        // Add row button
        var addBtn = document.getElementById('fe-add-row');
        if (addBtn) {
            addBtn.addEventListener('click', function() {
                var rows = container.querySelectorAll('.fe-row');
                if (rows.length >= MAX_ROWS) return;
                var newRow = _addEditorRow(rows.length, null);
                if (newRow) {
                    var ta = newRow.querySelector('.fe-row-formula');
                    if (ta) ta.focus();
                }
            });
        }
    }

    // ── Initialization ──────────────────────────────────────────────────

    function _attachNewButtons() {
        document.querySelectorAll('.fe-new-btn').forEach(function(btn) {
            if (btn._feAttached) return;
            btn._feAttached = true;
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                openFormulaEditor();
            });
        });
    }

    function init() {
        registerCustomIndicators();
        initEditorEvents();
        // Attach "New" button handlers (menu may already be built by chart-toolbar.js)
        _attachNewButtons();
        // Also re-attach after a delay (menu may build later)
        setTimeout(_attachNewButtons, 2000);

        // Document-level delegation for edit pencil clicks (bulletproof — works
        // regardless of when menu is built or rebuilt, no handler conflicts)
        document.addEventListener('click', function(e) {
            var el = e.target.closest ? e.target.closest('[data-edit-formula]') : null;
            if (el) {
                e.stopPropagation();
                e.preventDefault();
                openFormulaEditor(el.getAttribute('data-edit-formula'));
            }
        }, true); // capture phase — fires before item toggle handler

        // Sync from server on page load (if logged in)
        syncFromServer();
    }

    // Run init after DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // ── Public API ──────────────────────────────────────────────────────

    // Promise-returning quant-series fetcher for other modules (backtester etc).
    // Resolves to map of seriesName → values[] (aligned to given timestamps, with NaN for null).
    // Populates _quantCache as a side effect so existing callers benefit.
    function fetchQuantSeries(seriesNames, timestamps) {
        var needed = (seriesNames || []).filter(function(s) { return s && !_quantCache[s]; });
        var cached = {};
        (seriesNames || []).forEach(function(s) { if (_quantCache[s]) cached[s] = _quantCache[s]; });
        if (!needed.length) return Promise.resolve(cached);
        var url = '/api/quant/chart-series?series=' + encodeURIComponent(needed.join(',')) +
                  '&timestamps=' + encodeURIComponent(JSON.stringify(timestamps));
        return fetch(url, { credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var out = Object.assign({}, cached);
                if (data && data.series) {
                    for (var name in data.series) {
                        var vals = (data.series[name].values || []).map(function(v) { return v === null ? NaN : v; });
                        _quantCache[name] = vals;
                        out[name] = vals;
                    }
                }
                return out;
            });
    }

    window.CustomIndicators = {
        openEditor: openFormulaEditor,
        closeEditor: closeFormulaEditor,
        saveFormula: saveFormula,
        deleteFormula: deleteFormula,
        drawAll: drawCustomIndicators,
        getFormulas: getFormulas,
        getCustomCategoryKeys: getCustomCategoryKeys,
        rebuildMenu: rebuildIndicatorMenu,
        extractOHLCV: extractOHLCVForFormula,
        clearQuantCache: clearQuantCache,
        getQuantCache: function() { return _quantCache; },
        fetchQuantSeries: fetchQuantSeries
    };
})();
