// ============= Asset Search Popup =============
// Thin caller — shared ranking + server-fallback logic lives in
// asset-search-core.js (window.assetSearchCore). This file owns the
// chart-popup UI (★ favourites, ÷ ratios, in-place DOM popup) and the
// selectAssetFromSearch callback that flips the chart symbol.

var assetSearchSelectedIndex = 0;
var assetSearchResults = [];
var _chartSearchTimer = null;
var _chartSearchAbort = null;
var _assetSearchOutsideHandler = null;

function showAssetSearchPopup() {
    var popup = document.getElementById('asset-search-popup');
    var input = document.getElementById('asset-search-input');
    if (!popup || !input) return;
    // On mobile, .adv-chart-content has transform which traps position:fixed.
    // Move popup to body so it escapes the stacking context.
    if (popup.parentElement && popup.parentElement !== document.body) {
        var parent = popup.closest('.adv-chart-content');
        if (parent && getComputedStyle(parent).transform !== 'none') {
            document.body.appendChild(popup);
        }
    }
    input.value = '';
    assetSearchSelectedIndex = 0;
    popup.classList.add('visible');
    // Use setTimeout for iOS PWA — direct focus() can be blocked
    setTimeout(function() { input.focus(); }, 50);
    populateAssetSearchResults('');
    // Dismiss when tapping/clicking outside the popup. Deferred + capture so the
    // opening tap doesn't immediately re-close it.
    if (!_assetSearchOutsideHandler) {
        _assetSearchOutsideHandler = function(e) {
            var p = document.getElementById('asset-search-popup');
            if (p && p.classList.contains('visible') && !p.contains(e.target)) {
                hideAssetSearchPopup();
            }
        };
        setTimeout(function() {
            document.addEventListener('pointerdown', _assetSearchOutsideHandler, true);
        }, 0);
    }
}

function hideAssetSearchPopup() {
    var popup = document.getElementById('asset-search-popup');
    if (popup) popup.classList.remove('visible');
    if (_assetSearchOutsideHandler) {
        document.removeEventListener('pointerdown', _assetSearchOutsideHandler, true);
        _assetSearchOutsideHandler = null;
    }
}

// Row extras as two GRID CELLS (badges column + price column) so every row
// aligns under the sticky header and long content can never push the price
// out of the popup. `meta` fields are optional.
function _searchRowExtras(sym, meta) {
    meta = meta || {};
    var badges = '';
    if (meta.type) badges += '<span class="asp-badge">' + escapeHtml(String(meta.type).toUpperCase()) + '</span>';
    if (meta.exchange) badges += '<span class="asp-badge asp-badge-ex">' + escapeHtml(meta.exchange) + '</span>';
    var region = (window.assetSearchCore && assetSearchCore.suffixRegion) ? assetSearchCore.suffixRegion(sym) : null;
    if (region) badges += '<span class="asp-badge asp-badge-region">' + escapeHtml(region.market + ' · ' + region.currency) + '</span>';
    var qHtml = _searchQuoteHtml(meta.price, meta.change_pct);
    // data-quote-sym marks the row for async /api/quote enrichment when the
    // price isn't known yet (dynamic results only).
    return '<span class="asp-badges">' + badges + '</span>' +
        '<span class="asp-quote"' + (qHtml ? '' : ' data-quote-sym="' + escapeHtml(sym) + '"') + '>' + qHtml + '</span>';
}

// Sticky column header for the results list — same grid as the rows.
function _searchHeadHtml() {
    return '<div class="asp-head" aria-hidden="true">' +
        '<span>Symbol</span><span>Name</span>' +
        '<span class="asp-head-mkt">Market</span>' +
        '<span class="asp-head-px">Last / 24h</span></div>';
}

function _searchQuoteHtml(price, changePct) {
    if (price == null || isNaN(price)) return '';
    var fp = (typeof formatPriceSimple === 'function') ? formatPriceSimple(Number(price)) : String(price);
    var html = fp;
    if (changePct != null && isFinite(changePct)) {
        var ch = Number(changePct);
        html += ' <span class="' + (ch >= 0 ? 'asp-up' : 'asp-down') + '">' + (ch >= 0 ? '+' : '') + ch.toFixed(2) + '%</span>';
    }
    return html;
}

function _curatedSearchMeta(sym) {
    var a = allAssets[sym] || {};
    var rt = a.realtime || {};
    return {
        type: a.type,
        price: (rt.price != null) ? rt.price : a.latest_price,
        change_pct: (rt.change_pct != null) ? rt.change_pct : a.change_24h
    };
}

// Async price enrichment for dynamic rows (max 5 per search to respect the
// 30/min limit on /api/quote). Results land in window._dynamicQuoteCache
// (screener-table.js) so watchlist/table code reuses them.
function _enrichDynamicQuotes(container) {
    var pending = container.querySelectorAll('.asp-quote[data-quote-sym]');
    var n = Math.min(pending.length, 5);
    for (var i = 0; i < n; i++) {
        (function(el) {
            var sym = el.getAttribute('data-quote-sym');
            el.removeAttribute('data-quote-sym');
            var cached = (typeof _dynamicQuoteCache !== 'undefined') ? _dynamicQuoteCache[sym] : null;
            if (cached && cached.price != null) {
                el.innerHTML = _searchQuoteHtml(cached.price, cached.change_pct);
                return;
            }
            fetch('/api/quote/' + encodeURIComponent(sym))
                .then(function(r) { return r.ok ? r.json() : null; })
                .then(function(q) {
                    if (!q || q.price == null) return;
                    if (typeof _dynamicQuoteCache !== 'undefined') {
                        _dynamicQuoteCache[sym] = { name: q.name, price: q.price, change_pct: q.change_pct };
                    }
                    if (el.isConnected) el.innerHTML = _searchQuoteHtml(q.price, q.change_pct);
                })
                .catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/quote/ failed — rendered as empty', String((e && e.message) || e)); });
        })(pending[i]);
    }
}

function populateAssetSearchResults(query) {
    var container = document.getElementById('asset-search-results');
    if (!container) return;
    var q = String(query || '').toLowerCase().trim();
    var core = window.assetSearchCore;

    var ranked = core.filterAndRank(q);
    var filteredFavs = ranked.favs;
    var filteredOthers = ranked.others;
    var ratioMatch = core.detectRatio(q);

    assetSearchResults = ranked.all;
    assetSearchSelectedIndex = Math.min(assetSearchSelectedIndex, Math.max(0, assetSearchResults.length - 1));

    var html = _searchHeadHtml();
    if (ratioMatch) {
        assetSearchResults.unshift(ratioMatch.symbol);
        assetSearchSelectedIndex = 0;
        html += '<div class="asset-search-header">Custom Ratio</div>';
        html += '<div class="asset-search-item highlighted" data-symbol="' + ratioMatch.symbol + '" data-idx="0">' +
            '<span class="asset-switcher-sym">\u00F7 ' + escapeHtml(ratioMatch.symbol) + '</span>' +
            '<span class="asset-switcher-name">' + escapeHtml(ratioMatch.name) + '</span>' +
            '<span class="asp-badges"></span><span class="asp-quote"></span></div>';
    }
    if (filteredFavs.length > 0) {
        html += '<div class="asset-search-header">Favorites</div>';
    }
    var idxOffset = ratioMatch ? 1 : 0;
    filteredFavs.forEach(function(s, i) {
        var a = allAssets[s] || {};
        var idx = idxOffset + i;
        html += '<div class="asset-search-item' + (idx === assetSearchSelectedIndex ? ' highlighted' : '') + '" data-symbol="' + s + '" data-idx="' + idx + '">' +
            '<span class="asset-switcher-sym">\u2605 ' + escapeHtml(s) + '</span>' +
            '<span class="asset-switcher-name">' + escapeHtml(a.name || '') + '</span>' +
            _searchRowExtras(s, _curatedSearchMeta(s)) + '</div>';
    });
    if (filteredFavs.length > 0 && filteredOthers.length > 0) {
        html += '<div class="asset-search-divider"></div>';
    }
    filteredOthers.forEach(function(s, i) {
        var a = allAssets[s] || {};
        var idx = idxOffset + filteredFavs.length + i;
        html += '<div class="asset-search-item' + (idx === assetSearchSelectedIndex ? ' highlighted' : '') + '" data-symbol="' + s + '" data-idx="' + idx + '">' +
            '<span class="asset-switcher-sym">' + escapeHtml(s) + '</span>' +
            '<span class="asset-switcher-name">' + escapeHtml(a.name || '') + '</span>' +
            _searchRowExtras(s, _curatedSearchMeta(s)) + '</div>';
    });
    if (assetSearchResults.length === 0 && !ratioMatch) {
        html = '<div class="asset-search-empty">' + (q ? 'No local matches' : 'No matches') + '</div>';
    }
    container.innerHTML = html;

    container.querySelectorAll('.asset-search-item').forEach(function(item) {
        item.addEventListener('click', function() {
            selectAssetFromSearch(this.getAttribute('data-symbol'));
        });
    });

    // Server-side fallback for sparse local results.
    var localCount = assetSearchResults.length;
    if (_chartSearchAbort) { _chartSearchAbort.abort(); _chartSearchAbort = null; }
    clearTimeout(_chartSearchTimer);
    if (q.length >= 2 && localCount < 3) {
        if (localCount === 0) {
            container.innerHTML = '<div class="asset-search-empty" style="color:#8b949e">Searching\u2026</div>';
        } else {
            container.insertAdjacentHTML('beforeend', '<div class="asset-search-header" style="color:#8b949e">Searching\u2026</div>');
        }
        var seen = {};
        assetSearchResults.forEach(function(s) { seen[s] = true; });
        _chartSearchTimer = setTimeout(function() {
            var ac = new AbortController();
            _chartSearchAbort = ac;
            core.fetchDynamicSymbols(q, { seen: seen, signal: ac.signal })
                .then(function(dynamic) {
                    _chartSearchAbort = null;
                    var hint = container.querySelector('.asset-search-header:last-child');
                    if (hint && hint.textContent === 'Searching\u2026') hint.remove();
                    if (dynamic.length === 0) {
                        if (assetSearchResults.length === 0) {
                            container.innerHTML = '<div class="asset-search-empty">No matches</div>';
                        }
                        return;
                    }
                    container.insertAdjacentHTML('beforeend', '<div class="asset-search-header">Dynamic</div>');
                    dynamic.forEach(function(d) {
                        var idx = assetSearchResults.length;
                        assetSearchResults.push(d.symbol);
                        var itemHtml = '<div class="asset-search-item' + (idx === assetSearchSelectedIndex ? ' highlighted' : '') + '" data-symbol="' + escapeHtml(d.symbol) + '" data-idx="' + idx + '">' +
                            '<span class="asset-switcher-sym">' + escapeHtml(d.symbol) + '</span>' +
                            '<span class="asset-switcher-name">' + escapeHtml(d.name || '') + '</span>' +
                            _searchRowExtras(d.symbol, { type: d.type, exchange: d.exchange }) + '</div>';
                        container.insertAdjacentHTML('beforeend', itemHtml);
                    });
                    _enrichDynamicQuotes(container);
                    if (localCount === 0 && assetSearchResults.length > 0) {
                        assetSearchSelectedIndex = 0;
                        var first = container.querySelector('.asset-search-item');
                        if (first) first.classList.add('highlighted');
                    }
                    container.querySelectorAll('.asset-search-item:not([data-bound])').forEach(function(item) {
                        item.setAttribute('data-bound', '1');
                        item.addEventListener('click', function() {
                            selectAssetFromSearch(this.getAttribute('data-symbol'));
                        });
                    });
                })
                .catch(function() { _chartSearchAbort = null; });
        }, 300);
    }
}

function selectAssetFromSearch(symbol) {
    hideAssetSearchPopup();
    if (symbol && symbol !== klineChartSymbol) {
        klineChartSymbol = symbol;
        try { sessionStorage.setItem('klineOpenSymbol', symbol); } catch(e) {}
        var a = allAssets[symbol] || {};
        var symEl = document.getElementById('adv-chart-symbol');
        var nameEl = document.getElementById('adv-chart-name');
        if (symEl) symEl.textContent = symbol;
        if (nameEl) nameEl.textContent = a.name || '';
        if (typeof _updateWatchlistStar === 'function') _updateWatchlistStar(symbol);
        // Reset TF if sub-hourly on non-crypto (1H/4H are valid for all via Yahoo)
        if (!isCryptoAsset(symbol) && ['1m','3m','5m','15m','30m'].indexOf(klineChartTimeframe) >= 0) {
            klineChartTimeframe = '1D';
        }
        // Free-tier: reset to 4H if on a blocked TF
        if (!window._isPremium && ['1m','3m','5m','15m','30m','1h'].indexOf(klineChartTimeframe) >= 0) {
            klineChartTimeframe = '4h';
        }
        // Rebuild TF buttons for new asset
        var tfContainer = document.getElementById('tf-buttons-container');
        if (tfContainer) {
            tfContainer.innerHTML = buildTfButtons(symbol, klineChartTimeframe);
            tfContainer.querySelectorAll('.tf-selector').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var tf = this.getAttribute('data-tf');
                    tfContainer.querySelectorAll('.tf-selector').forEach(function(b) { b.classList.remove('active'); });
                    this.classList.add('active');
                    klineChartTimeframe = tf;
                    loadKlineChart(klineChartSymbol, tf);
                });
            });
        }
        // In spread mode, reload all panes with new asset; otherwise just the active pane
        if (typeof mcSpreadOnAssetChange === 'function' && mcSpreadOnAssetChange(symbol)) {
            // Spread mode handled loading all panes
        } else {
            loadKlineChart(symbol, klineChartTimeframe);
        }
        // Drawer + alert-overlay refresh is handled by the chart render path that
        // follows: loadKlineChart → fastSwapChartData's rAF (cache-hit) or
        // renderKlineChart (fetch-miss) BOTH call updateDrawerAll() +
        // renderAlertOverlays() AFTER the data swap settles. Calling them eagerly
        // here fired a duplicate GET /api/alerts + /api/scenarios per switch AND
        // rebuilt the drawer DOM twice. Do not call updateDrawerAll() or
        // renderAlertOverlays() eagerly here.
        if (typeof cancelEditAlert === 'function') cancelEditAlert();
    }
}

function getOrderedAssetList() {
    // Return favourites (watchlist items) for cycling; fallback to all assets
    // Accept both regular assets AND ratio symbols (e.g., GOLD_QQQ)
    var favs = getAllWatchlistSymbols().filter(function(s) {
        if (allAssets[s]) return true;
        if (s.indexOf('_') > 0) {
            var p = s.split('_');
            return p.length === 2 && allAssets[p[0]] && allAssets[p[1]];
        }
        return false;
    });
    if (favs.length > 0) return favs;
    return Object.keys(allAssets).sort();
}

function switchAsset(direction) {
    var list = getOrderedAssetList();
    if (list.length === 0) return;
    var idx = list.indexOf(klineChartSymbol);
    if (idx < 0) idx = 0;
    idx += direction;
    if (idx < 0) idx = list.length - 1;
    if (idx >= list.length) idx = 0;
    var newSymbol = list[idx];
    if (newSymbol === klineChartSymbol) return;
    klineChartSymbol = newSymbol;
    try { sessionStorage.setItem('klineOpenSymbol', newSymbol); } catch(e) {}
    var a = allAssets[newSymbol] || {};
    var symEl = document.getElementById('adv-chart-symbol');
    var nameEl = document.getElementById('adv-chart-name');
    if (symEl) symEl.textContent = newSymbol;
    if (nameEl) nameEl.textContent = a.name || '';
    if (typeof _updateWatchlistStar === 'function') _updateWatchlistStar(newSymbol);
    // Reset TF if sub-hourly on non-crypto (1H/4H are valid for all via Yahoo)
    if (!isCryptoAsset(newSymbol) && ['1m','3m','5m','15m','30m'].indexOf(klineChartTimeframe) >= 0) {
        klineChartTimeframe = '1D';
    }
    var tfContainer = document.getElementById('tf-buttons-container');
    if (tfContainer) {
        tfContainer.innerHTML = buildTfButtons(newSymbol, klineChartTimeframe);
        tfContainer.querySelectorAll('.tf-selector').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var tf = this.getAttribute('data-tf');
                tfContainer.querySelectorAll('.tf-selector').forEach(function(b) { b.classList.remove('active'); });
                this.classList.add('active');
                klineChartTimeframe = tf;
                loadKlineChart(klineChartSymbol, tf);
            });
        });
    }
    // In spread mode, reload all panes with new asset; otherwise just the active pane
    if (typeof mcSpreadOnAssetChange === 'function' && mcSpreadOnAssetChange(newSymbol)) {
        // Spread mode handled loading all panes
    } else {
        loadKlineChart(newSymbol, klineChartTimeframe);
    }
    // Drawer + alert-overlay refresh is handled by the chart render path that
    // follows (fastSwapChartData rAF on cache-hit, renderKlineChart on fetch-miss);
    // both call updateDrawerAll() + renderAlertOverlays() after the data swap.
    // Eager calls here would duplicate GET /api/alerts + /api/scenarios per switch
    // and rebuild the drawer DOM twice.
    if (typeof cancelEditAlert === 'function') cancelEditAlert();
}

function switchTimeframe(tf) {
    klineChartTimeframe = tf;
    try { sessionStorage.setItem('klineOpenTf', tf); localStorage.setItem('lastViewedTf', tf); } catch(e) {}
    var tfButtons = klineModal ? klineModal.querySelectorAll('.tf-selector') : [];
    tfButtons.forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-tf') === tf); });
    // Keep the mobile header TF dropdown label in sync (e.g. when changed elsewhere).
    var _mtfLbl = document.getElementById('mob-tf-current');
    if (_mtfLbl) _mtfLbl.textContent = tf;
    loadKlineChart(klineChartSymbol, tf);

    // Preload all relevant timeframes in background (staggered)
    preloadAllTimeframes(klineChartSymbol, tf);
}
