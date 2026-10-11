/**
 * Screening Matrix — 10-column indicator heatmap for all assets.
 *
 * Part of the Indicator Coherence work.
 */

/* global loadChart */

var _mxData = [];
var _mxSort = 'symbol';
var _mxDir = 'asc';
var _mxTab = 'watchlist';
var _mxLoading = false;
var _mxPreview = null; // { total_available, reason } when server returns preview_mode

// Column definitions
var MX_COLUMNS = [
    { key: 'symbol', label: 'Asset', sortKey: 'symbol', align: 'left', width: '70px' },
    { key: 'price', label: 'Price', sortKey: 'price', align: 'right', width: '72px' },
    { key: 'change', label: 'Chg%', sortKey: 'change', align: 'right', width: '52px' },
    { key: 'ema', label: 'EMA', sortKey: 'ema', align: 'center', width: '56px', title: 'EMA Ribbon trend' },
    { key: 'rsi', label: 'RSI', sortKey: 'rsi', align: 'right', width: '44px', title: 'Weekly RSI' },
    { key: 'bb', label: 'BB', sortKey: 'bb', align: 'center', width: '44px', title: 'Bollinger squeeze' },
    { key: 'consensio', label: 'Cons', sortKey: 'consensio', align: 'center', width: '44px', title: 'Consensio signal' },
    { key: 'structure', label: 'Struct', sortKey: 'structure', align: 'center', width: '52px', title: 'Market structure' },
    { key: 'macd', label: 'MACD', sortKey: 'macd', align: 'center', width: '44px', title: 'MACD direction' },
    { key: 'adx', label: 'ADX', sortKey: 'adx', align: 'right', width: '44px', title: 'ADX value' },
    { key: 'cpr', label: 'CPR', sortKey: 'cpr', align: 'center', width: '48px', title: 'CPR position' },
    { key: 'atr', label: 'ATR%', sortKey: 'atr', align: 'right', width: '48px', title: 'ATR as % of price' },
    { key: 'volume', label: 'Vol', sortKey: 'volume', align: 'right', width: '44px', title: 'Relative volume (vs 20-bar avg)' },
];


function initScreeningMatrix() {
    _buildHeader();
    _bindEvents();
}

function _buildHeader() {
    var thead = document.getElementById('mx-thead');
    if (!thead) return;
    var tr = document.createElement('tr');
    MX_COLUMNS.forEach(function(col) {
        var th = document.createElement('th');
        th.textContent = col.label;
        th.className = 'mx-th';
        th.style.textAlign = col.align;
        if (col.width) th.style.width = col.width;
        if (col.title) th.title = col.title;
        th.dataset.sort = col.sortKey;
        th.addEventListener('click', function() { _toggleSort(col.sortKey); });
        tr.appendChild(th);
    });
    thead.innerHTML = '';
    thead.appendChild(tr);
}

function _bindEvents() {
    // Tab buttons
    var tabs = document.querySelectorAll('[data-mx-tab]');
    tabs.forEach(function(btn) {
        btn.addEventListener('click', function() {
            _mxTab = this.dataset.mxTab;
            tabs.forEach(function(b) { b.classList.toggle('active', b.dataset.mxTab === _mxTab); });
            refreshMatrix();
        });
    });
    // Refresh button
    var refreshBtn = document.getElementById('mx-refresh-btn');
    if (refreshBtn) refreshBtn.addEventListener('click', function() { refreshMatrix(); });
    // Back button (cockpit → matrix)
    var backBtn = document.getElementById('mx-back-btn');
    if (backBtn) backBtn.addEventListener('click', _closeCockpit);
}

function _toggleSort(key) {
    if (_mxSort === key) {
        _mxDir = _mxDir === 'asc' ? 'desc' : 'asc';
    } else {
        _mxSort = key;
        _mxDir = key === 'symbol' ? 'asc' : 'desc';
    }
    _updateSortIndicators();
    refreshMatrix();
}

function _updateSortIndicators() {
    var ths = document.querySelectorAll('#mx-thead .mx-th');
    ths.forEach(function(th) {
        th.classList.remove('mx-sort-asc', 'mx-sort-desc');
        if (th.dataset.sort === _mxSort) {
            th.classList.add(_mxDir === 'asc' ? 'mx-sort-asc' : 'mx-sort-desc');
        }
    });
}

function refreshMatrix() {
    if (_mxLoading) return;
    _mxLoading = true;

    var url = '/api/details/matrix?tab=' + encodeURIComponent(_mxTab) +
        '&sort=' + encodeURIComponent(_mxSort) +
        '&dir=' + encodeURIComponent(_mxDir);

    fetch(url, { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            _mxLoading = false;
            if (!data) { _renderMatrixError(); return; }
            _mxData = data.rows || [];
            _mxPreview = data.preview_mode ? {
                total_available: data.total_available || 0,
                reason: data.preview_reason || ''
            } : null;
            _renderTable();
            _updateSortIndicators();
        })
        .catch(function() {
            _mxLoading = false;
            _renderMatrixError();
        });
}

// Render an inline error state into the matrix body instead of leaving
// stale/blank content on a failed refresh (same pattern as the cockpit
// error state below).
function _renderMatrixError() {
    var tbody = document.getElementById('mx-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="' + MX_COLUMNS.length + '" class="mx-empty">Failed to load — check your connection and try again.</td></tr>';
}


// ─── Cell Renderers ───────────────────────────────────────

function _cls(base, variant) { return 'mx-cell ' + base + (variant ? ' mx-' + variant : ''); }

function _renderCell(col, row) {
    var td = document.createElement('td');
    td.style.textAlign = col.align;

    switch (col.key) {
    case 'symbol':
        td.textContent = row.symbol;
        td.className = 'mx-cell mx-symbol';
        break;
    case 'price':
        td.textContent = _fmtPrice(row.price);
        td.className = 'mx-cell mx-price';
        break;
    case 'change': {
        var chg = row.change_pct;
        td.textContent = chg != null ? (chg > 0 ? '+' : '') + chg.toFixed(1) + '%' : '—';
        td.className = _cls('mx-change', chg > 0 ? 'bull' : chg < 0 ? 'bear' : '');
        break;
    }
    case 'ema': {
        var e = row.ema_ribbon || {};
        var t = e.trend || 'neutral';
        var label = t === 'bullish' ? '▲' : t === 'bearish' ? '▼' : '—';
        if (e.days) label += ' ' + e.days + 'd';
        td.textContent = label;
        td.className = _cls('mx-ema', t === 'bullish' ? 'bull' : t === 'bearish' ? 'bear' : '');
        td.title = 'EMA Ribbon: ' + t + (e.days ? ' (' + e.days + ' days)' : '');
        break;
    }
    case 'rsi': {
        var r = row.rsi || {};
        var v = r.value;
        td.textContent = v != null ? v.toFixed(0) : '—';
        td.className = _cls('mx-rsi', r.zone === 'overbought' ? 'bear' : r.zone === 'oversold' ? 'bull' : '');
        break;
    }
    case 'bb': {
        var bb = row.bb_squeeze || {};
        if (bb.squeezed) {
            td.textContent = '⬤';
            td.className = _cls('mx-bb', 'squeeze');
            td.title = 'BB Squeeze active — width ' + (bb.width_pct || '?') + '%';
        } else {
            td.textContent = bb.width_pct != null ? bb.width_pct.toFixed(0) + '%' : '—';
            td.className = 'mx-cell mx-bb';
            td.title = 'BB width ' + (bb.width_pct || '?') + '%';
        }
        break;
    }
    case 'consensio': {
        var c = row.consensio || {};
        var sig = c.signal || 0;
        td.textContent = sig > 0 ? '+' + sig : String(sig);
        td.className = _cls('mx-cons', sig > 0 ? 'bull' : sig < 0 ? 'bear' : '');
        td.title = 'Consensio: ' + (c.positions || 0) + '/' + (c.max_positions || 6) + ' positions';
        break;
    }
    case 'structure': {
        var s = row.structure || {};
        td.textContent = s.label || '—';
        td.className = _cls('mx-struct', s.trend === 'uptrend' ? 'bull' : s.trend === 'downtrend' ? 'bear' : '');
        break;
    }
    case 'macd': {
        var m = row.macd || {};
        var dir = m.direction || 'flat';
        td.textContent = dir === 'bullish' ? '▲' : dir === 'bearish' ? '▼' : '—';
        td.className = _cls('mx-macd', dir === 'bullish' ? 'bull' : dir === 'bearish' ? 'bear' : '');
        break;
    }
    case 'adx': {
        var a = row.adx || {};
        td.textContent = a.value != null ? a.value.toFixed(0) : '—';
        td.className = _cls('mx-adx', a.regime === 'strong_trend' ? 'strong' : a.regime === 'ranging' ? 'weak' : '');
        td.title = 'ADX: ' + (a.regime || '') + (a.direction ? ', ' + a.direction : '');
        break;
    }
    case 'cpr': {
        var cp = row.cpr || {};
        var pos = cp.position;
        td.textContent = pos === 'above' ? '▲' : pos === 'below' ? '▼' : pos === 'inside' ? '◆' : '—';
        td.className = _cls('mx-cpr', pos === 'above' ? 'bull' : pos === 'below' ? 'bear' : pos === 'inside' ? 'neutral-hl' : '');
        td.title = pos ? 'CPR: price ' + pos : 'CPR unavailable';
        break;
    }
    case 'atr': {
        var at = row.atr_pct || {};
        td.textContent = at.value != null ? at.value.toFixed(1) + '%' : '—';
        td.className = _cls('mx-atr', at.value > 5 ? 'high' : at.value < 1 ? 'low' : '');
        break;
    }
    case 'volume': {
        var vl = row.rel_volume || {};
        var rv = vl.ratio;
        td.textContent = rv != null ? rv.toFixed(1) + 'x' : '—';
        td.className = _cls('mx-vol', rv > 1.5 ? 'high' : rv < 0.5 ? 'low' : '');
        break;
    }
    default:
        td.textContent = '—';
    }

    return td;
}

function _fmtPrice(p) {
    if (p == null) return '—';
    if (p >= 10000) return p.toFixed(0);
    if (p >= 100) return p.toFixed(1);
    if (p >= 1) return p.toFixed(2);
    if (p >= 0.01) return p.toFixed(4);
    return p.toFixed(6);
}


// ─── Table Rendering ──────────────────────────────────────

function _renderTable() {
    var tbody = document.getElementById('mx-tbody');
    if (!tbody) return;

    if (_mxData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="' + MX_COLUMNS.length + '" class="mx-empty">No data available</td></tr>';
        return;
    }

    var fragment = document.createDocumentFragment();
    _mxData.forEach(function(row) {
        var tr = document.createElement('tr');
        tr.className = 'mx-row';
        tr.dataset.symbol = row.symbol;
        tr.addEventListener('click', function() { _onRowClick(row.symbol); });
        MX_COLUMNS.forEach(function(col) {
            tr.appendChild(_renderCell(col, row));
        });
        fragment.appendChild(tr);
    });

    // Preview-mode upsell row (free tier sees the first N rows, then a locked prompt)
    if (_mxPreview) {
        var upsellTr = document.createElement('tr');
        upsellTr.className = 'mx-upsell-row';
        var td = document.createElement('td');
        td.colSpan = MX_COLUMNS.length;
        td.className = 'mx-upsell-cell';
        var extraCount = Math.max(0, (_mxPreview.total_available || 0) - _mxData.length);
        var lockMsg = extraCount > 0
            ? (extraCount + ' more assets available with Member.')
            : (_mxPreview.reason || 'This needs a higher plan. An admin can change plans under Admin → Users.');
        td.innerHTML =
            '<div class="mx-upsell"><span class="mx-upsell-lock">\uD83D\uDD12</span>' +
            '<span class="mx-upsell-msg">' + lockMsg + '</span>' +
            '</div>';
        upsellTr.appendChild(td);
        fragment.appendChild(upsellTr);
    }

    tbody.innerHTML = '';
    tbody.appendChild(fragment);
}

function _onRowClick(symbol) {
    // Load chart for the clicked asset
    if (typeof loadChart === 'function') {
        loadChart(symbol, null);  // null = keep current TF
    }
    // Highlight selected row
    document.querySelectorAll('.mx-row').forEach(function(r) {
        r.classList.toggle('mx-row-active', r.dataset.symbol === symbol);
    });
}


// ─── Asset Cockpit ────────────────────────────────────────

function _closeCockpit() {
    var scroll = document.getElementById('mx-scroll');
    var cockpit = document.getElementById('mx-cockpit');
    if (scroll) scroll.style.display = '';
    if (cockpit) cockpit.style.display = 'none';
}

