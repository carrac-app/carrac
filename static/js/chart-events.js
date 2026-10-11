// Chart modal wiring: setupKlineChartEvents() attaches every toolbar,
// menu, drawing-tool and chart-surface handler each time a chart opens.
// Needs app-core.js and chart-modal.js loaded first.

function setupKlineChartEvents() {
    // Timeframe buttons
    var tfButtons = klineModal.querySelectorAll('.tf-selector');
    tfButtons.forEach(function(btn) {
        btn.addEventListener('click', function() {
            var tf = this.getAttribute('data-tf');
            tfButtons.forEach(function(b) { b.classList.remove('active'); });
            this.classList.add('active');
            klineChartTimeframe = tf;
            // Remember the TF so a reopen (this session or next) resumes on it.
            try { sessionStorage.setItem('klineOpenTf', tf); localStorage.setItem('lastViewedTf', tf); } catch(e) {}
            loadKlineChart(klineChartSymbol, tf);
        });
    });

    // Asset switcher dropdown
    var assetSwitcherBtn = document.getElementById('asset-switcher-btn');
    var assetSwitcherMenu = document.getElementById('asset-switcher-menu');
    if (assetSwitcherBtn && assetSwitcherMenu) {
        assetSwitcherBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChartMenus('asset-switcher-menu');
            if (assetSwitcherMenu.classList.contains('visible')) {
                assetSwitcherMenu.classList.remove('visible');
                return;
            }
            // Build asset list: watchlist first, then all others
            var wlSet = {};
            getAllWatchlistSymbols().forEach(function(s) { wlSet[s] = true; });
            var html = '';
            var favs = Object.keys(wlSet).sort();
            var others = Object.keys(allAssets).filter(function(s) { return !wlSet[s]; }).sort();
            if (favs.length > 0) {
                html += '<div class="asset-switcher-header">Favorites</div>';
                favs.forEach(function(s) {
                    var a = allAssets[s] || {};
                    html += '<div class="asset-switcher-item' + (s === klineChartSymbol ? ' active' : '') + '" data-symbol="' + s + '">' +
                        '<span class="asset-switcher-sym">★ ' + s + '</span>' +
                        '<span class="asset-switcher-name">' + escapeHtml(a.name || '') + '</span></div>';
                });
                html += '<div class="asset-switcher-divider"></div>';
            }
            others.forEach(function(s) {
                var a = allAssets[s] || {};
                html += '<div class="asset-switcher-item' + (s === klineChartSymbol ? ' active' : '') + '" data-symbol="' + s + '">' +
                    '<span class="asset-switcher-sym">' + s + '</span>' +
                    '<span class="asset-switcher-name">' + escapeHtml(a.name || '') + '</span></div>';
            });
            assetSwitcherMenu.innerHTML = html;
            assetSwitcherMenu.classList.add('visible');
        });

        assetSwitcherMenu.addEventListener('click', function(e) {
            var item = e.target.closest('[data-symbol]');
            if (!item) return;
            var newSymbol = item.getAttribute('data-symbol');
            assetSwitcherMenu.classList.remove('visible');
            if (newSymbol && newSymbol !== klineChartSymbol) {
                klineChartSymbol = newSymbol;
                try { sessionStorage.setItem('klineOpenSymbol', newSymbol); localStorage.setItem('lastViewedAsset', newSymbol); } catch(e) {}
                if (window.PreferenceSync) PreferenceSync.syncPreference('chart_prefs', { lastViewedAsset: newSymbol });
                var a = allAssets[newSymbol] || {};
                var symEl = document.getElementById('adv-chart-symbol');
                var nameEl = document.getElementById('adv-chart-name');
                if (symEl) symEl.textContent = newSymbol;
                if (nameEl) nameEl.textContent = a.name || '';
                // Rebuild TF buttons for new asset — only reset sub-hourly on non-crypto (1H/4H are valid for all)
                var subHourlyOnly = ['1m','3m','5m','15m','30m'];
                if (!isCryptoAsset(newSymbol) && subHourlyOnly.indexOf(klineChartTimeframe) >= 0) {
                    klineChartTimeframe = '1D';
                }
                var tfContainer = document.getElementById('tf-buttons-container');
                if (tfContainer) {
                    tfContainer.innerHTML = buildTfButtons(newSymbol, klineChartTimeframe);
                    _addTfEditZone(tfContainer);
                    // Re-attach TF click handlers
                    tfContainer.querySelectorAll('.tf-selector').forEach(function(btn) {
                        btn.addEventListener('click', function() {
                            var tf = this.getAttribute('data-tf');
                            tfContainer.querySelectorAll('.tf-selector').forEach(function(b) { b.classList.remove('active'); });
                            this.classList.add('active');
                            klineChartTimeframe = tf;
                            try { sessionStorage.setItem('klineOpenTf', tf); localStorage.setItem('lastViewedTf', tf); } catch(e) {}
                            loadKlineChart(klineChartSymbol, tf);
                        });
                    });
                }
                loadKlineChart(newSymbol, klineChartTimeframe);
            }
        });
    }

    // Draw toggle button
    var drawToggleBtn = document.getElementById('draw-toggle-btn');
    if (drawToggleBtn) {
        drawToggleBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleDrawingToolbar();
        });
    }

    // Drawing toolbar pin — persisted; pinned = toolbar auto-opens on every chart build.
    // NOTE: showDrawingToolbar/updateOhlcOffset live in LAZILY-loaded kline-drawings.js
    // and are usually not defined yet at panel-build time — open the bar with the same
    // class toggle inline instead of calling them (guarded OHLC reposition if present).
    var drawPinBtn = document.getElementById('draw-pin-btn');
    if (drawPinBtn) {
        var _pinned = false;
        try { _pinned = localStorage.getItem('klineDrawToolbarPinned') === '1'; } catch(e) {}
        drawPinBtn.classList.toggle('active', _pinned);
        if (_pinned) {
            var _pinContent = document.getElementById('adv-chart-content');
            if (_pinContent) _pinContent.classList.add('drawing-toolbar-visible');
            if (drawToggleBtn) drawToggleBtn.classList.add('active');
            if (typeof updateOhlcOffset === 'function') { try { updateOhlcOffset(); } catch(e) {} }
        }
        drawPinBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var nowPinned = !this.classList.contains('active');
            this.classList.toggle('active', nowPinned);
            try { localStorage.setItem('klineDrawToolbarPinned', nowPinned ? '1' : '0'); } catch(e) {}
            if (window.showSwipeHint) window.showSwipeHint(nowPinned ? 'Toolbar pinned — stays open' : 'Toolbar unpinned');
        });
    }

    // Master drawings visibility (eye) — logic lives in lazily-loaded kline-drawings.js
    var drawingsEyeBtn = document.getElementById('drawings-eye-btn');
    if (drawingsEyeBtn) {
        try {
            var _dhAll = localStorage.getItem('klineAllDrawingsHidden') === '1';
            drawingsEyeBtn.classList.toggle('drawings-hidden', _dhAll);
            if (_dhAll) drawingsEyeBtn.title = 'Show all drawings (H)';
        } catch(e) {}
        drawingsEyeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            ensureKLineDrawings().then(function() {
                if (typeof toggleAllDrawingsVisibility === 'function') toggleAllDrawingsVisibility();
            });
        });
    }

    // Container-width responsive toolbar + compact TF dropdown
    _initToolbarResizeObserver();
    _initTfCompactDropdown();

    // Log scale toggle
    var logBtn = document.getElementById('log-scale-btn');
    if (logBtn) {
        logBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleLogScale();
        });
    }

    // Shortcuts info button
    var shortcutsBtn = document.getElementById('shortcuts-btn');
    if (shortcutsBtn) {
        shortcutsBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            showShortcutsModal();
        });
    }

    // In-toolbar search button — opens the same asset search popup the Space
    // key opens (desktop-only affordance; hidden on mobile via CSS, which
    // already has its own #mob-top-search icon).
    var chartSearchBtn = document.getElementById('chart-search-btn');
    if (chartSearchBtn) {
        chartSearchBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (typeof showAssetSearchPopup === 'function') showAssetSearchPopup();
        });
    }

    var screenshotBtn = document.getElementById('screenshot-export-btn');
    if (screenshotBtn) {
        screenshotBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (typeof klineChart === 'undefined' || !klineChart) return;
            try {
                // Use KlineChart's built-in screenshot API
                var dataUrl = klineChart.getConvertPictureUrl(true, 'png', '#131722');
                if (dataUrl) {
                    // Convert data URL to blob for download
                    var byteString = atob(dataUrl.split(',')[1]);
                    var mimeString = dataUrl.split(',')[0].split(':')[1].split(';')[0];
                    var ab = new ArrayBuffer(byteString.length);
                    var ia = new Uint8Array(ab);
                    for (var i = 0; i < byteString.length; i++) ia[i] = byteString.charCodeAt(i);
                    var blob = new Blob([ab], { type: mimeString });
                    var filename = (klineChartSymbol || 'chart') + '_' + (klineChartTimeframe || '1D') + '.png';
                    var url = URL.createObjectURL(blob);
                    var a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    setTimeout(function() { URL.revokeObjectURL(url); }, 5000);
                    showSwipeHint('Chart saved as ' + filename);
                }
            } catch (ex) {
                console.warn('Screenshot export failed:', ex);
                showSwipeHint('Screenshot export failed');
            }
        });
    }

    var chartShareBtn = document.getElementById('chart-share-btn');
    if (chartShareBtn) {
        chartShareBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!window.ChartShare) { showSwipeHint('Share unavailable'); return; }
            var symbol = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || 'BTC';
            var timeframe = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
            // Option C: capture the chart EXACTLY as rendered (overlays=true →
            // includes drawings + indicators) and let the server composite it
            // into the brand frame — full fidelity, no re-render. If capture
            // fails, the server falls back to a candle render from {symbol, tf}.
            var image = null;
            try {
                if (typeof klineChart !== 'undefined' && klineChart) {
                    image = klineChart.getConvertPictureUrl(true, 'png', '#131722');
                }
            } catch (ex) { image = null; }
            // Gate the capture by decoded size: the pure
            // decision lives in share-modal.js's decideCaptureSize (unit-tested
            // in tests/js/chart-capture-gate.test.cjs) so this handler and the test
            // suite share one threshold. A dropped capture TELLS the user via ChartShare's toast —
            // the share still succeeds as a server-rendered chart, it just
            // isn't the pixel-perfect capture. openChart() also retries once
            // without the image if nginx itself rejects an oversize body
            // (413 / network failure), so the raised gate stays safe even if
            // the nginx client_max_body_size bump (Chunk 1 step 1.0) lags.
            if (image) {
                var sizeDecision = (typeof decideCaptureSize === 'function')
                    ? decideCaptureSize(image.length) : { keep: true };
                // a PNG capture that's still oversize gets
                // ONE retry as JPEG before we give up on the pixel-perfect
                // capture. klinecharts' getConvertPictureUrl forwards its
                // 'type' arg straight to canvas.toDataURL('image/' + type), so
                // 'jpeg' is a supported, much-smaller-body format — same API,
                // no offscreen-canvas re-encode needed. The server already
                // accepts JPEG capture data URLs (Chunk 1.4 decode_chart_capture).
                if (!sizeDecision.keep && sizeDecision.reason === 'oversize') {
                    var jpegImage = null;
                    try {
                        if (typeof klineChart !== 'undefined' && klineChart) {
                            jpegImage = klineChart.getConvertPictureUrl(true, 'jpeg', '#131722');
                        }
                    } catch (ex3) { jpegImage = null; }
                    if (jpegImage) {
                        var jpegDecision = (typeof decideCaptureSize === 'function')
                            ? decideCaptureSize(jpegImage.length) : { keep: true };
                        if (jpegDecision.keep) {
                            image = jpegImage;
                            sizeDecision = jpegDecision;
                        }
                    }
                }
                if (!sizeDecision.keep) {
                    image = null;
                    if (window.ChartShare && typeof window.ChartShare.toastCaptureDropped === 'function') {
                        window.ChartShare.toastCaptureDropped();
                    }
                }
            }
            // Interactive frozen share: capture the VISIBLE
            // state alongside the raster so the public page can re-render a
            // real chart. WYSIWYG — hidden indicators/drawings stay out.
            var extras = {};
            try {
                var indicatorsHidden = (typeof klineIndicatorsHidden !== 'undefined') && klineIndicatorsHidden;
                if (!indicatorsHidden && typeof klineActiveIndicators !== 'undefined' && klineActiveIndicators) {
                    var paramOverrides = {};
                    try { paramOverrides = JSON.parse(localStorage.getItem('indicatorParamOverrides')) || {}; } catch (exO) {}
                    var frozenInds = [];
                    Object.keys(klineActiveIndicators).forEach(function(k) {
                        if (!klineActiveIndicators[k] || frozenInds.length >= 40) return;
                        var entry = { key: k };
                        if (paramOverrides[k]) entry.params = paramOverrides[k];
                        frozenInds.push(entry);
                    });
                    if (frozenInds.length) extras.indicators = frozenInds;
                }
            } catch (exInd) {}
            try {
                if (typeof _readDrawingsStore === 'function' &&
                    typeof computeVisibleDrawingIds === 'function') {
                    var allHidden = false;
                    try { allHidden = localStorage.getItem('klineAllDrawingsHidden') === '1'; } catch (exH) {}
                    var dIds = computeVisibleDrawingIds(_readDrawingsStore()[symbol] || [], allHidden);
                    if (dIds.length) extras.drawing_ids = dIds;
                }
            } catch (exDr) {}
            try {
                if (typeof getThemeId === 'function') extras.theme = getThemeId();
            } catch (exTh) {}
            try {
                if (typeof klineChart !== 'undefined' && klineChart &&
                    typeof klineChart.getVisibleRange === 'function' &&
                    typeof klineChart.getDataList === 'function') {
                    var dl = klineChart.getDataList();
                    var vr = klineChart.getVisibleRange();
                    if (dl && dl.length && vr) {
                        var fromIdx = Math.max(0, Math.floor(vr.realFrom != null ? vr.realFrom : (vr.from || 0)));
                        var toEx = (vr.realTo != null ? vr.realTo : (vr.to != null ? vr.to : dl.length));
                        var toIdx = Math.min(dl.length - 1, Math.max(fromIdx, Math.ceil(toEx) - 1));
                        if (dl[fromIdx] && dl[toIdx]) {
                            extras.viewport = {
                                from_ts: Math.floor(dl[fromIdx].timestamp / 1000),
                                to_ts: Math.floor(dl[toIdx].timestamp / 1000)
                            };
                        }
                    }
                }
            } catch (exVp) {}
            ChartShare.open(Object.assign({ symbol: symbol, timeframe: timeframe, image: image }, extras));
        });
    }

    // Flat '.tool-btn[data-tool]' buttons — the 24 draw tools no
    // longer live at this level (they're '.tool-menu-item[data-tool]' inside
    // the group flyouts, wired separately below), so this now only matches
    // Magnet/Undo/Redo/Clear. Runs before rebuildFavToolsBar() populates
    // '#fav-tools-bar', so its (also '.tool-btn[data-tool]') favourite chips
    // aren't double-bound here either — same ordering as before this task.
    var toolButtons = klineModal.querySelectorAll('.tool-btn[data-tool]');
    toolButtons.forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            var tool = this.getAttribute('data-tool');
            if (tool) {
                handleKlineToolClick(tool, this, toolButtons, e);
                // Auto-show drawing toolbar when a tool is activated
                showDrawingToolbar();
            }
        });
    });

    // Indicator toggle dropdown
    // Chart type dropdown
    var chartTypeToggle = document.getElementById('chart-type-toggle');
    var chartTypeMenu = document.getElementById('chart-type-menu');
    if (chartTypeToggle && chartTypeMenu) {
        chartTypeToggle.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChartMenus('chart-type-menu');
            chartTypeMenu.classList.toggle('visible');
        });
        chartTypeMenu.querySelectorAll('.indicator-menu-item').forEach(function(item) {
            item.addEventListener('click', function(e) {
                e.stopPropagation();
                var type = this.getAttribute('data-chart-type');
                if (!klineChart || !type) return;
                var prevType = 'candle_solid';
                try { prevType = localStorage.getItem('klineChartType') || 'candle_solid'; } catch(e) {}
                // Heikin Ashi: transform data; other types: just set candle style
                // setStyles AFTER applyNewData — applyNewData resets styles to init defaults
                if (type === 'heikin_ashi') {
                    _applyHeikinAshi(true);
                    klineChart.setStyles({ candle: { type: 'candle_solid' } });
                } else {
                    if (prevType === 'heikin_ashi') _applyHeikinAshi(false);
                    klineChart.setStyles({ candle: { type: type } });
                }
                // Update active state
                chartTypeMenu.querySelectorAll('.indicator-menu-item').forEach(function(el) {
                    el.classList.toggle('active', el.getAttribute('data-chart-type') === type);
                });
                chartTypeMenu.classList.remove('visible');
                // Save preference
                try { localStorage.setItem('klineChartType', type); } catch(e) {}
                // Update the trigger button's text label
                var chartTypeLabelEl = document.getElementById('chart-type-label');
                if (chartTypeLabelEl) {
                    chartTypeLabelEl.textContent = (window.ChartToolbar && typeof ChartToolbar.chartTypeLabel === 'function') ? ChartToolbar.chartTypeLabel(type) : type;
                }
            });
        });
        // Close on outside click
        document.addEventListener('click', function() { chartTypeMenu.classList.remove('visible'); });
    }

    var indToggle = document.getElementById('indicator-toggle');
    var indMenu = document.getElementById('indicator-menu');
    if (indToggle && indMenu) {
        indToggle.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChartMenus('indicator-menu');
            indMenu.classList.toggle('visible');
            // Show/hide "Save as group" based on active indicators
            var _sgBtn = document.getElementById('ind-save-group-btn');
            if (_sgBtn) {
                var hasActive = Object.keys(klineActiveIndicators).some(function(k) { return klineActiveIndicators[k]; });
                _sgBtn.style.display = hasActive ? '' : 'none';
            }
        });

        // Indicator menu items — add star icons + click handlers
        var indItems = indMenu.querySelectorAll('.indicator-menu-item');
        indItems.forEach(function(item) {
            var ind = item.getAttribute('data-ind');
            // Add star icon at end
            var star = document.createElement('span');
            star.className = 'ind-fav-star' + (klineFavouriteIndicators.indexOf(ind) >= 0 ? ' active' : '');
            star.setAttribute('data-ind', ind);
            star.innerHTML = '&#9733;';
            star.addEventListener('click', function(e) {
                e.stopPropagation();
                toggleFavouriteIndicator(this.getAttribute('data-ind'));
            });
            item.style.position = 'relative';
            item.appendChild(star);
            // Gear icon click handler (settings modal)
            var gear = item.querySelector('.ind-settings-gear');
            if (gear) {
                gear.addEventListener('click', function(e) {
                    e.stopPropagation();
                    var settingsKey = this.getAttribute('data-settings');
                    if (typeof IndicatorSettings !== 'undefined' && settingsKey) {
                        IndicatorSettings.open(settingsKey);
                    }
                });
            }
            // Info icon — tooltip + prevent click from toggling
            var info = item.querySelector('.ind-info-icon');
            if (info) {
                info.addEventListener('click', function(e) { e.stopPropagation(); });
                info.addEventListener('mouseenter', function() {
                    var desc = this.getAttribute('data-desc');
                    if (!desc) return;
                    var tip = document.createElement('div');
                    tip.className = 'ind-info-tooltip';
                    tip.textContent = desc;
                    tip.id = 'ind-info-tooltip-active';
                    document.body.appendChild(tip);
                    var rect = this.getBoundingClientRect();
                    var tipW = 260, tipH = tip.offsetHeight;
                    // Position to the right of the icon; flip left if it would overflow viewport
                    var left = rect.right + 6;
                    if (left + tipW > window.innerWidth - 4) left = rect.left - tipW - 6;
                    var top = rect.top + rect.height / 2 - tipH / 2;
                    if (top < 4) top = 4;
                    if (top + tipH > window.innerHeight - 4) top = window.innerHeight - tipH - 4;
                    tip.style.left = left + 'px';
                    tip.style.top = top + 'px';
                });
                info.addEventListener('mouseleave', function() {
                    var tip = document.getElementById('ind-info-tooltip-active');
                    if (tip) tip.remove();
                });
            }
            // Indicator toggle on item click
            item.addEventListener('click', function(e) {
                e.stopPropagation();
                var ind = this.getAttribute('data-ind');
                var _premiumInds = {CVD_USDT:1, CVD_COIN:1, OI:1, DERIV_COMBINED:1};
                if (_premiumInds[ind] && !window._isPremium) {
                    showToast('Derivatives indicators require Premium. Upgrade in Settings.');
                    return;
                }
                // Macro & Quant indicators require premium/tester/admin
                var _indMeta = IndicatorRegistry.getIndicator(ind);
                if (_indMeta && _indMeta.premiumOnly && !window._hasPremiumFeatures) {
                    if (typeof showUpgradeModal === 'function')
                        showUpgradeModal('Macro & Quant indicators are available with a Premium subscription.');
                    return;
                }
                toggleKlineIndicator(ind);
            });
        });
        // Render favourite pills in toolbar
        renderFavouritePills();

        // Master toggle
        var masterToggle = document.getElementById('indicator-master-toggle');
        if (masterToggle) {
            masterToggle.addEventListener('click', function(e) {
                e.stopPropagation();
                toggleAllIndicators();
            });
        }

        // Save active indicators as group
        var saveGroupBtn = document.getElementById('ind-save-group-btn');
        if (saveGroupBtn) {
            saveGroupBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                var activeKeys = Object.keys(klineActiveIndicators).filter(function(k) { return klineActiveIndicators[k]; });
                if (!activeKeys.length) return;
                var n = klineIndicatorGroups.length + 1;
                var group = { id: 'grp_' + Date.now(), name: 'Group ' + n, indicators: activeKeys, active: true };
                klineIndicatorGroups.push(group);
                saveIndicatorGroups();
                renderFavouritePills();
                indMenu.classList.remove('visible');
                setTimeout(function() { showGroupEditor(group.id, true); }, 20);
            });
        }
    }

    // Group chips (Lines, Chan, Fib, Shape, Pos, Mark, Tools)
    // regroup. Each chip is a split button: main zone fires the group's
    // remembered tool, drop zone opens the full flyout. tool->group + the
    // group's ordered tool list are derived from the live DOM (the markup
    // chart-toolbar.js just rendered) rather than a second hand-authored
    // table, so they can never drift from it.
    var allToolGroupMenus = [];
    klineToolGroupOf = {};
    _klineGroupToolList = {};

    KLINE_TOOL_GROUP_IDS.forEach(function(group) {
        var btn = document.getElementById(group + '-group-btn');
        var menu = document.getElementById(group + '-group-menu');
        if (!btn || !menu) return;
        allToolGroupMenus.push({ btn: btn, menu: menu });
        // a11y: aria-pressed must be present from wiring time, not only once
        // _klineSyncToolActiveState() first toggles .active (it's diff-gated
        // and only writes the attribute on a change).
        if (!btn.hasAttribute('aria-pressed')) btn.setAttribute('aria-pressed', 'false');

        var menuItems = menu.querySelectorAll('.tool-menu-item[data-tool]');
        var groupTools = [];
        menuItems.forEach(function(item) {
            var t = item.getAttribute('data-tool');
            groupTools.push(t);
            klineToolGroupOf[t] = group;
        });
        _klineGroupToolList[group] = groupTools;

        // Validate/repair the remembered tool for this group against the
        // current markup — unknown/missing values fall back to the group
        // default (first menu item, per chart-toolbar.js's ordering).
        if (groupTools.length) {
            var remembered = klineToolGroupMemory[group];
            if (!remembered || groupTools.indexOf(remembered) === -1) {
                klineToolGroupMemory[group] = groupTools[0];
                _klineSaveToolGroupMemory();
            }
        }
        _klineUpdateChipDisplay(group);

        // Split-button chip: the drop zone (▾) opens the flyout; anywhere
        // else on the chip fires the group's remembered tool.
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (e.target.closest('.tool-chip-drop')) {
                var willOpen = !menu.classList.contains('visible');
                closeAllChartMenus(willOpen ? menu.id : undefined);
                _klineResetChipAriaExpanded(willOpen ? group : null);
                menu.classList.toggle('visible', willOpen);
                btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
                return;
            }
            closeAllChartMenus();
            _klineResetChipAriaExpanded(null);
            var rememberedTool = klineToolGroupMemory[group];
            if (rememberedTool) _klineFireTool(rememberedTool, btn, e);
        });

        // Tool menu items — add star + click handler
        menuItems.forEach(function(item) {
            var tool = item.getAttribute('data-tool');
            // Add favourite star
            var star = document.createElement('span');
            star.className = 'tool-fav-star' + (klineFavouriteTools.indexOf(tool) >= 0 ? ' active' : '');
            star.textContent = klineFavouriteTools.indexOf(tool) >= 0 ? '★' : '☆';
            star.title = 'Toggle favourite';
            star.addEventListener('click', function(ev) {
                ev.stopPropagation();
                toggleFavouriteTool(tool, this);
            });
            item.appendChild(star);
            // Drawing click — fires the tool AND becomes the group's new
            // remembered tool.
            item.addEventListener('click', function(e) {
                if (e.target.classList.contains('tool-fav-star')) return; // handled above
                e.stopPropagation();
                klineToolGroupMemory[group] = tool;
                _klineSaveToolGroupMemory();
                _klineUpdateChipDisplay(group);
                menu.classList.remove('visible');
                btn.setAttribute('aria-expanded', 'false');
                _klineFireTool(tool, this, e);
            });
        });
    });

    // Floating favourites rail (docked by default; drag out via #fav-home-grip)
    _favRailInit();

    // Build favourite tools bar
    rebuildFavToolsBar();

    // Keep .tool-chip / .fav-tool-btn active-state in sync with whichever
    // .tool-menu-item is actually engaged — covers our own fire paths AND
    // kline-drawings.js's internal onDrawEnd auto-deactivate (which clears
    // .active on the resolved element but doesn't know about the new chip
    // markup). Diffs before mutating so it can't self-trigger a loop.
    _klineInitToolActiveObserver();
    _klineSyncToolActiveState();

    // Asset search popup events
    var searchInput = document.getElementById('asset-search-input');
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            assetSearchSelectedIndex = 0;
            populateAssetSearchResults(this.value);
        });
        searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                e.stopPropagation();
                hideAssetSearchPopup();
                return;
            }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                assetSearchSelectedIndex = Math.min(assetSearchSelectedIndex + 1, assetSearchResults.length - 1);
                populateAssetSearchResults(this.value);
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                assetSearchSelectedIndex = Math.max(assetSearchSelectedIndex - 1, 0);
                populateAssetSearchResults(this.value);
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                if (assetSearchResults.length > 0) {
                    selectAssetFromSearch(assetSearchResults[assetSearchSelectedIndex]);
                }
                return;
            }
        });
    }

    // Close menus when clicking outside
    document.addEventListener('click', function(e) {
        if (indMenu && !indMenu.contains(e.target) && e.target !== indToggle) {
            indMenu.classList.remove('visible');
            var tip = document.getElementById('ind-info-tooltip-active');
            if (tip) tip.remove();
        }
        // Close all tool group menus
        allToolGroupMenus.forEach(function(m) {
            if (!m.menu.contains(e.target) && e.target !== m.btn) {
                m.menu.classList.remove('visible');
                m.btn.setAttribute('aria-expanded', 'false');
            }
        });
        // Close asset switcher
        var asSwitcher = document.getElementById('asset-switcher-menu');
        var asSwitcherBtn = document.getElementById('asset-switcher-btn');
        if (asSwitcher && !asSwitcher.contains(e.target) && (!asSwitcherBtn || !asSwitcherBtn.contains(e.target))) {
            asSwitcher.classList.remove('visible');
        }
        // Close drawing context menu. klineContextMenu is declared in the
        // LAZILY-loaded kline-drawings.js (only fetched on first chart open via
        // ensureKLineDrawings()). This handler is registered once per chart-open
        // but then fires on EVERY future click anywhere on the page — so if
        // kline-drawings.js comes back as a synthesized service-worker 503 or
        // genuinely fails to load, this bare reference threw a ReferenceError on
        // the very next tap, killing every statement after it in this handler
        // (the search-popup close below included). Guard existence first.
        if (typeof klineContextMenu !== 'undefined' && klineContextMenu && !klineContextMenu.contains(e.target)) {
            hideDrawingContextMenu();
        }
        // Close overlay pickers (color, fill, opacity, width)
        var overlayPicker = document.getElementById('overlay-color-picker');
        if (overlayPicker && !overlayPicker.contains(e.target)) {
            overlayPicker.classList.remove('visible');
        }
        ['fill-color-picker', 'opacity-picker', 'width-picker'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el && !el.contains(e.target)) el.remove();
        });
        // Close asset search popup
        var searchPopup = document.getElementById('asset-search-popup');
        if (searchPopup && searchPopup.classList.contains('visible') && !searchPopup.contains(e.target)) {
            hideAssetSearchPopup();
        }
    });

    // Resize handle
    setupKlineResize();

    // Keyboard shortcuts - remove old handler first to prevent duplicates
    if (klineKeydownHandler) {
        document.removeEventListener('keydown', klineKeydownHandler, true);
    }
    klineKeydownHandler = handleKlineKeydown;
    document.addEventListener('keydown', klineKeydownHandler, true);

    // Keyup for arrow key navigation release
    if (klineKeyupHandler) {
        document.removeEventListener('keyup', klineKeyupHandler, true);
    }
    klineKeyupHandler = handleKlineKeyup;
    document.addEventListener('keyup', klineKeyupHandler, true);

    // AI panel initialized lazily by setViewMode('ai') in split-layout.js
}
