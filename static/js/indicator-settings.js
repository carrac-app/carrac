/**
 * Indicator Settings Modal
 * TradingView-style parameter editing with live preview.
 * Must load after indicator-registry.js and carrac-indicator-pack.js, before the chart-app files.
 */
(function() {
    'use strict';

    // === Defaults snapshot (taken before any overrides are applied) ===
    var _packDefaults = {};
    var _builtinDefaults = {};
    var _overrides = {};

    function _deepClone(obj) {
        return JSON.parse(JSON.stringify(obj));
    }

    function _getNestedValue(obj, path) {
        var parts = path.split('.');
        var cur = obj;
        for (var i = 0; i < parts.length; i++) {
            if (cur == null) return undefined;
            cur = cur[parts[i]];
        }
        return cur;
    }

    function _setNestedValue(obj, path, value) {
        var parts = path.split('.');
        var cur = obj;
        for (var i = 0; i < parts.length - 1; i++) {
            if (cur[parts[i]] == null) cur[parts[i]] = {};
            cur = cur[parts[i]];
        }
        cur[parts[parts.length - 1]] = value;
    }

    // === Initialization ===

    function _init() {
        // Snapshot pack config defaults
        if (window.indicatorPackConfig) {
            _packDefaults = _deepClone(window.indicatorPackConfig);
        }
        // Snapshot built-in calcParams defaults
        var indicators = IndicatorRegistry.INDICATORS;
        for (var k in indicators) {
            if (indicators[k].calcParams) {
                _builtinDefaults[k] = indicators[k].calcParams.slice();
            }
        }
        // Initialize calcParams for built-in indicators that don't have them yet
        var builtinInits = { MACD: [12, 26, 9], MA: [20], EMA: [21], BOLL: [20, 2, 0] };
        for (var bk in builtinInits) {
            if (indicators[bk] && !indicators[bk].calcParams) {
                indicators[bk].calcParams = builtinInits[bk].slice();
                _builtinDefaults[bk] = builtinInits[bk].slice();
            }
        }

        // Load overrides from localStorage
        try {
            _overrides = JSON.parse(localStorage.getItem('indicatorParamOverrides')) || {};
        } catch(e) {
            _overrides = {};
        }

        // Apply overrides immediately (before first chart render)
        _applyOverrides(_overrides);

        // Set up modal event listeners
        _bindEvents();
    }

    function _applyOverrides(overrides) {
        if (!overrides) return;
        for (var key in overrides) {
            var diff = overrides[key];
            // Built-in indicator: overrides have calcParams array
            var ind = IndicatorRegistry.INDICATORS[key];
            if (ind && diff.calcParams) {
                ind.calcParams = diff.calcParams.slice();
                continue;
            }
            // Pack indicator: apply to indicatorPackConfig
            var config = window.indicatorPackConfig && window.indicatorPackConfig[key];
            if (config) {
                for (var paramKey in diff) {
                    _setNestedValue(config, paramKey, diff[paramKey]);
                }
            }
        }
    }

    // === Modal state ===
    var _currentKey = null;       // INDICATORS key (e.g. 'SUPER_TREND')
    var _currentConfigKey = null; // Pack configKey (e.g. 'superTrend') or null
    var _currentSchema = null;
    var _isBuiltin = false;
    var _debounceTimer = null;

    // === Open / Close ===

    function open(indicatorKey) {
        // Re-snapshot pack defaults if they were empty when this module first loaded
        // (happens when carrac-indicator-pack.js is lazy-loaded)
        if (Object.keys(_packDefaults).length === 0 && window.indicatorPackConfig) {
            _packDefaults = _deepClone(window.indicatorPackConfig);
        }
        var ind = IndicatorRegistry.getIndicator(indicatorKey);
        if (!ind) return;
        var schema = IndicatorRegistry.getParamSchema(indicatorKey);
        if (!schema || schema.length === 0) return;

        _currentKey = indicatorKey;
        _currentConfigKey = ind.configKey || null;
        _currentSchema = schema;
        _isBuiltin = !_currentConfigKey;

        var modal = document.getElementById('indicator-settings-modal');
        var title = document.getElementById('is-title');
        var tabs = document.getElementById('is-tabs');
        if (!modal) return;

        title.textContent = ind.label + ' Settings';

        // Determine which tabs exist
        var hasInputs = schema.some(function(p) { return p.tab === 'inputs'; });
        var hasStyle = schema.some(function(p) { return p.tab === 'style'; });

        if (hasInputs && hasStyle) {
            tabs.style.display = '';
            _setActiveTab('inputs');
        } else {
            tabs.style.display = 'none';
        }

        _buildForm();
        modal.classList.add('active');
        if (window.a11y) a11y.trapFocus(modal.querySelector('[role="dialog"]'));
    }

    function close() {
        var modal = document.getElementById('indicator-settings-modal');
        if (modal) {
            if (window.a11y) a11y.releaseFocus(modal.querySelector('[role="dialog"]'));
            modal.classList.remove('active');
        }
        _computeAndSaveOverrides();
        _currentKey = null;
        _currentConfigKey = null;
        _currentSchema = null;
        // Reopen indicator dropdown — delayed so the click event finishes bubbling
        // (otherwise the document click-outside handler immediately closes it)
        setTimeout(function() {
            var indMenu = document.getElementById('indicator-menu');
            if (indMenu) indMenu.classList.add('visible');
        }, 0);
    }

    // === Tab switching ===

    function _setActiveTab(tab) {
        var tabs = document.getElementById('is-tabs');
        var form = document.getElementById('is-form');
        if (!tabs || !form) return;
        tabs.querySelectorAll('.is-tab').forEach(function(btn) {
            btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
        });
        form.querySelectorAll('.is-row').forEach(function(row) {
            row.style.display = row.getAttribute('data-tab') === tab ? '' : 'none';
        });
    }

    // === Current / default value accessors ===

    function _getCurrentValue(param) {
        if (_isBuiltin) {
            var ind = IndicatorRegistry.INDICATORS[_currentKey];
            if (ind && ind.calcParams && param.calcParamIndex !== undefined) {
                return ind.calcParams[param.calcParamIndex];
            }
            return null;
        }
        var config = window.indicatorPackConfig[_currentConfigKey];
        if (!config) return null;
        return _getNestedValue(config, param.key);
    }

    function _getDefaultValue(param) {
        if (_isBuiltin) {
            var defaults = _builtinDefaults[_currentKey];
            if (defaults && param.calcParamIndex !== undefined) {
                return defaults[param.calcParamIndex];
            }
            return null;
        }
        var defaultConfig = _packDefaults[_currentConfigKey];
        if (!defaultConfig) return null;
        return _getNestedValue(defaultConfig, param.key);
    }

    // === Form building ===

    function _buildForm() {
        var form = document.getElementById('is-form');
        var tabs = document.getElementById('is-tabs');
        if (!form || !_currentSchema) return;
        form.innerHTML = '';

        // Determine active tab
        var activeTabBtn = tabs && tabs.style.display !== 'none' ? tabs.querySelector('.is-tab.active') : null;
        var activeTab = activeTabBtn ? activeTabBtn.getAttribute('data-tab') : _currentSchema[0].tab;

        _currentSchema.forEach(function(param) {
            var row = document.createElement('div');
            row.className = 'is-row';
            row.setAttribute('data-tab', param.tab);
            row.style.display = param.tab === activeTab ? '' : 'none';

            var label = document.createElement('label');
            label.textContent = param.label;
            row.appendChild(label);

            var value = _getCurrentValue(param);

            if (param.type === 'bool') {
                var toggle = document.createElement('label');
                toggle.className = 'is-toggle';
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = !!value;
                var slider = document.createElement('span');
                slider.className = 'is-toggle-slider';
                toggle.appendChild(cb);
                toggle.appendChild(slider);
                row.appendChild(toggle);
                cb.addEventListener('change', function() {
                    _applyParamValue(param, cb.checked);
                    _livePreview();
                });
            } else if (param.type === 'color') {
                var colorInput = document.createElement('input');
                colorInput.type = 'color';
                colorInput.value = _toHex(value || '#ffffff');
                row.appendChild(colorInput);
                colorInput.addEventListener('input', function() {
                    _applyParamValue(param, colorInput.value);
                    _livePreview();
                });
            } else if (param.type === 'select') {
                var sel = document.createElement('select');
                (param.options || []).forEach(function(opt) {
                    var option = document.createElement('option');
                    option.value = opt.value;
                    option.textContent = opt.label;
                    if (opt.value === value) option.selected = true;
                    sel.appendChild(option);
                });
                row.appendChild(sel);
                sel.addEventListener('change', function() {
                    _applyParamValue(param, sel.value);
                    _livePreview();
                });
            } else {
                // int or float
                var numInput = document.createElement('input');
                numInput.type = 'number';
                numInput.min = param.min;
                numInput.max = param.max;
                numInput.step = param.type === 'float' ? (param.step || 0.1) : 1;
                numInput.value = value != null ? value : '';
                row.appendChild(numInput);
                numInput.addEventListener('input', (function(p, inp) {
                    return function() {
                        var v = p.type === 'int' ? parseInt(inp.value) : parseFloat(inp.value);
                        if (isNaN(v) || v < p.min || v > p.max) return;
                        _applyParamValue(p, v);
                        clearTimeout(_debounceTimer);
                        _debounceTimer = setTimeout(_livePreview, 150);
                    };
                })(param, numInput));
            }

            form.appendChild(row);
        });
    }

    // === Value application ===

    function _applyParamValue(param, value) {
        if (_isBuiltin) {
            var ind = IndicatorRegistry.INDICATORS[_currentKey];
            if (ind && ind.calcParams && param.calcParamIndex !== undefined) {
                ind.calcParams[param.calcParamIndex] = value;
            }
        } else {
            var config = window.indicatorPackConfig[_currentConfigKey];
            if (config) {
                _setNestedValue(config, param.key, value);
            }
        }
    }

    // === Live preview ===

    function _livePreview() {
        if (!_currentKey) return;
        if (typeof klineActiveIndicators === 'undefined' || !klineActiveIndicators[_currentKey]) return;
        if (typeof klineChart === 'undefined' || !klineChart) return;

        if (_isBuiltin) {
            _previewBuiltin();
        } else {
            _previewPack();
        }
    }

    function _previewPack() {
        if (!_currentConfigKey || typeof drawSingleIndicator !== 'function') return;
        var cacheKey = (typeof klineChartSymbol !== 'undefined' ? klineChartSymbol : '') + '_' +
                       (typeof klineChartTimeframe !== 'undefined' ? klineChartTimeframe : '');
        var cached = typeof klineDataCache !== 'undefined' ? klineDataCache[cacheKey] : null;
        if (!cached) return;
        var kd = typeof convertToKlineData === 'function' ? convertToKlineData(cached.data) : null;
        if (!kd) return;
        try {
            drawSingleIndicator(klineChart, kd, cached.data, _currentConfigKey);
        } catch (e) {
            console.error('[indicator-settings] preview error:', e);
        }
    }

    function _previewBuiltin() {
        var ind = IndicatorRegistry.INDICATORS[_currentKey];
        if (!ind) return;
        var builtInPanes = IndicatorRegistry.getBuiltInPanes();
        var builtInOverlays = IndicatorRegistry.getBuiltInOverlays();

        if (_currentKey in builtInPanes) {
            var paneId = builtInPanes[_currentKey];
            try { klineChart.removeIndicator(paneId); } catch(e) {}
            var paneOpts = { id: paneId, height: ind.paneHeight || 90, minHeight: 40 };
            klineChart.createIndicator({ name: _currentKey, calcParams: ind.calcParams.slice() }, false, paneOpts);
            var gapOpts = _currentKey === 'VOL' ? { top: 0.02, bottom: 0 } : { top: 0.05, bottom: 0.05 };
            klineChart.setPaneOptions({ id: paneId, gap: gapOpts });
        } else if (_currentKey in builtInOverlays) {
            try { klineChart.removeIndicator('candle_pane', _currentKey); } catch(e) {}
            klineChart.createIndicator({ name: _currentKey, calcParams: ind.calcParams.slice() }, true, { id: 'candle_pane' });
        }
    }

    // === Persistence ===

    function _computeAndSaveOverrides() {
        if (!_currentKey || !_currentSchema) return;
        var key = _currentConfigKey || _currentKey;
        var diff = {};
        var hasDiff = false;

        _currentSchema.forEach(function(param) {
            var current = _getCurrentValue(param);
            var def = _getDefaultValue(param);
            if (current !== def) {
                if (_isBuiltin && param.calcParamIndex !== undefined) {
                    if (!diff.calcParams) diff.calcParams = _builtinDefaults[_currentKey].slice();
                    diff.calcParams[param.calcParamIndex] = current;
                } else {
                    diff[param.key] = current;
                }
                hasDiff = true;
            }
        });

        if (hasDiff) {
            _overrides[key] = diff;
        } else {
            delete _overrides[key];
        }
        _persistOverrides();
    }

    function _persistOverrides() {
        try {
            localStorage.setItem('indicatorParamOverrides', JSON.stringify(_overrides));
        } catch(e) {}
        // Fire-and-forget server sync
        try {
            fetch('/api/indicator-prefs', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': typeof getCSRFToken === 'function' ? getCSRFToken() : '',
                    'X-Sync-Session': (window.DrawingTools && DrawingTools.SYNC_SESSION_ID) || ''
                },
                credentials: 'same-origin',
                body: JSON.stringify({ params: _overrides })
            });
        } catch(e) {}
    }

    // === Reset to defaults ===

    function _resetToDefaults() {
        if (!_currentKey || !_currentSchema) return;

        if (_isBuiltin) {
            var defaults = _builtinDefaults[_currentKey];
            if (defaults) {
                IndicatorRegistry.INDICATORS[_currentKey].calcParams = defaults.slice();
            }
        } else {
            var defaultConfig = _packDefaults[_currentConfigKey];
            if (defaultConfig) {
                _currentSchema.forEach(function(param) {
                    var defVal = _getNestedValue(defaultConfig, param.key);
                    if (defVal !== undefined) {
                        _setNestedValue(window.indicatorPackConfig[_currentConfigKey], param.key, defVal);
                    }
                });
            }
        }

        // Remove overrides for this indicator
        var key = _currentConfigKey || _currentKey;
        delete _overrides[key];

        // Rebuild form + live preview
        _buildForm();
        _livePreview();
    }

    // === Utility ===

    function _toHex(color) {
        if (!color) return '#ffffff';
        if (color.charAt(0) === '#') return color.length > 7 ? color.substring(0, 7) : color;
        var m = color.match(/(\d+)/g);
        if (m && m.length >= 3) {
            return '#' + ((1 << 24) + (parseInt(m[0]) << 16) + (parseInt(m[1]) << 8) + parseInt(m[2])).toString(16).slice(1);
        }
        return '#ffffff';
    }

    // === Event binding ===

    function _bindEvents() {
        // Wait for DOM
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _bindEventsImpl);
        } else {
            _bindEventsImpl();
        }
    }

    function _bindEventsImpl() {
        var modal = document.getElementById('indicator-settings-modal');
        if (!modal) return;

        // Close on overlay click
        modal.addEventListener('click', function(e) {
            if (e.target === modal) close();
        });

        // Close button
        var closeBtn = modal.querySelector('.modal-close');
        if (closeBtn) closeBtn.addEventListener('click', close);

        // Done button
        var doneBtn = document.getElementById('is-done');
        if (doneBtn) doneBtn.addEventListener('click', close);

        // Reset button
        var resetBtn = document.getElementById('is-reset');
        if (resetBtn) resetBtn.addEventListener('click', _resetToDefaults);

        // Tab switching
        var tabs = document.getElementById('is-tabs');
        if (tabs) {
            tabs.addEventListener('click', function(e) {
                var tab = e.target.getAttribute('data-tab');
                if (tab) _setActiveTab(tab);
            });
        }

        // Escape key
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && modal.classList.contains('active')) {
                close();
            }
        });
    }

    // === Public API ===

    window.IndicatorSettings = {
        open: open,
        close: close,
        applyOverrides: function(overrides) {
            if (overrides) {
                _overrides = overrides;
                try { localStorage.setItem('indicatorParamOverrides', JSON.stringify(_overrides)); } catch(e) {}
            }
            _applyOverrides(_overrides);
        }
    };

    // Initialize immediately
    _init();
})();
