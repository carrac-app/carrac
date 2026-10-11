// ============================================================
// Main Page Asset Search (Space key)
// ============================================================
// Thin caller — shared ranking + server-fallback logic lives in
// asset-search-core.js. This file owns the main-page overlay UI (no star,
// no ratios, clicks open the chart modal via openAdvancedChartModal).

var mainSearchSelectedIndex = 0;
var mainSearchResults = [];
var _mainSearchTimer = null;
var _mainSearchAbort = null;

function showMainAssetSearch() {
    var overlay = document.getElementById('main-asset-search-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'main-asset-search-overlay';
        overlay.className = 'asset-search-overlay';
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) hideMainAssetSearch();
        });
        var popup = document.createElement('div');
        popup.id = 'main-asset-search-popup';
        popup.className = 'asset-search-popup';
        popup.innerHTML =
            '<input class="asset-search-input" id="main-asset-search-input" placeholder="Search assets... (type to filter)" autocomplete="off">' +
            '<div class="asset-search-results" id="main-asset-search-results"></div>';
        overlay.appendChild(popup);
        document.body.appendChild(overlay);

        var input = document.getElementById('main-asset-search-input');
        input.addEventListener('input', function() {
            mainSearchSelectedIndex = 0;
            populateMainSearchResults(this.value);
        });
        input.addEventListener('keydown', function(e) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                mainSearchSelectedIndex = Math.min(mainSearchSelectedIndex + 1, mainSearchResults.length - 1);
                populateMainSearchResults(this.value);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                mainSearchSelectedIndex = Math.max(mainSearchSelectedIndex - 1, 0);
                populateMainSearchResults(this.value);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (mainSearchResults[mainSearchSelectedIndex]) {
                    hideMainAssetSearch();
                    klineIsFullscreen = true;
                    openAdvancedChartModal(mainSearchResults[mainSearchSelectedIndex]);
                }
            } else if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                hideMainAssetSearch();
            }
        });
    }
    mainSearchSelectedIndex = 0;
    overlay.classList.add('visible');
    var popup = document.getElementById('main-asset-search-popup');
    popup.classList.add('visible');
    var input = document.getElementById('main-asset-search-input');
    input.value = '';
    input.focus();
    populateMainSearchResults('');
}

function hideMainAssetSearch() {
    var overlay = document.getElementById('main-asset-search-overlay');
    if (overlay) overlay.classList.remove('visible');
    var popup = document.getElementById('main-asset-search-popup');
    if (popup) popup.classList.remove('visible');
}

function populateMainSearchResults(query) {
    var container = document.getElementById('main-asset-search-results');
    if (!container) return;
    var q = String(query || '').toLowerCase().trim();
    var core = window.assetSearchCore;

    var ranked = core.filterAndRank(q);
    var filteredFavs = ranked.favs;
    var filteredOthers = ranked.others;
    mainSearchResults = ranked.all;
    mainSearchSelectedIndex = Math.min(mainSearchSelectedIndex, Math.max(0, mainSearchResults.length - 1));

    var html = '';
    var idx = 0;
    function renderRow(s) {
        var a = allAssets[s] || {};
        html += '<div class="asset-search-item' + (idx === mainSearchSelectedIndex ? ' highlighted' : '') + '" data-symbol="' + escapeHtml(s) + '">' +
            '<strong>' + escapeHtml(s) + '</strong> <span style="color:#8b949e;font-size:12px">' + escapeHtml(a.name || '') + '</span></div>';
        idx++;
    }
    if (filteredFavs.length > 0) {
        html += '<div class="asset-search-header">Favorites</div>';
        filteredFavs.forEach(renderRow);
        html += '<div class="asset-search-divider"></div>';
    }
    if (filteredOthers.length > 0) {
        filteredOthers.forEach(renderRow);
    }
    if (mainSearchResults.length === 0 && !q) {
        html = '<div class="asset-search-empty">No matches</div>';
    }
    container.innerHTML = html;

    container.querySelectorAll('.asset-search-item').forEach(function(item) {
        item.addEventListener('click', function() {
            var sym = this.getAttribute('data-symbol');
            hideMainAssetSearch();
            klineIsFullscreen = true;
            openAdvancedChartModal(sym);
        });
    });

    // Scroll highlighted into view
    var highlighted = container.querySelector('.highlighted');
    if (highlighted) highlighted.scrollIntoView({ block: 'nearest' });

    // Server-side fallback for sparse local results.
    var localCount = mainSearchResults.length;
    if (_mainSearchAbort) { _mainSearchAbort.abort(); _mainSearchAbort = null; }
    clearTimeout(_mainSearchTimer);
    if (q.length >= 2 && localCount < 3) {
        if (localCount === 0) {
            container.innerHTML = '<div class="asset-search-empty" style="color:#8b949e">Searching\u2026</div>';
        } else {
            container.insertAdjacentHTML('beforeend', '<div class="asset-search-header" style="color:#8b949e">Searching\u2026</div>');
        }
        var seen = {};
        mainSearchResults.forEach(function(s) { seen[s] = true; });
        _mainSearchTimer = setTimeout(function() {
            var ac = new AbortController();
            _mainSearchAbort = ac;
            core.fetchDynamicSymbols(q, { seen: seen, signal: ac.signal })
                .then(function(dynamic) {
                    _mainSearchAbort = null;
                    var hint = container.querySelector('.asset-search-header:last-child');
                    if (hint && hint.textContent === 'Searching\u2026') hint.remove();
                    if (dynamic.length === 0) {
                        if (mainSearchResults.length === 0) {
                            container.innerHTML = '<div class="asset-search-empty">No matches</div>';
                        }
                        return;
                    }
                    container.insertAdjacentHTML('beforeend', '<div class="asset-search-header">Dynamic</div>');
                    var baseIdx = mainSearchResults.length;
                    dynamic.forEach(function(d, i) {
                        var idx = baseIdx + i;
                        mainSearchResults.push(d.symbol);
                        var badge = d.exchange ? ' <span style="color:#58a6ff;font-size:10px;background:#1c2333;padding:1px 5px;border-radius:3px">' + escapeHtml(d.exchange) + '</span>' : '';
                        var itemHtml = '<div class="asset-search-item' + (idx === mainSearchSelectedIndex ? ' highlighted' : '') + '" data-symbol="' + escapeHtml(d.symbol) + '">' +
                            '<strong>' + escapeHtml(d.symbol) + '</strong> <span style="color:#8b949e;font-size:12px">' + escapeHtml(d.name || '') + badge + '</span></div>';
                        container.insertAdjacentHTML('beforeend', itemHtml);
                    });
                    if (localCount === 0 && mainSearchResults.length > 0) {
                        mainSearchSelectedIndex = 0;
                        var first = container.querySelector('.asset-search-item');
                        if (first) first.classList.add('highlighted');
                    }
                    container.querySelectorAll('.asset-search-item:not([data-bound])').forEach(function(item) {
                        item.setAttribute('data-bound', '1');
                        item.addEventListener('click', function() {
                            var sym = this.getAttribute('data-symbol');
                            hideMainAssetSearch();
                            klineIsFullscreen = true;
                            openAdvancedChartModal(sym);
                        });
                    });
                })
                .catch(function() { _mainSearchAbort = null; });
        }, 300);
    }
}
