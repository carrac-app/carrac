/**
 * Portfolio Page — hero tiles, book-value curve range, allocation charts,
 * sortable position table, and the excluded-positions banner.
 *
 * The excluded banner is the point of the whole page: a position the
 * backend could not value (no size / no entry / no price) is reported by
 * reason rather than folded into the totals as zero. If this banner is
 * empty, wrong, or hidden, the hero numbers above it look confidently
 * complete when they are not.
 *
 * Pure helpers (formatUsd, sortRows, buildExcludedMessage, tableRowsFrom,
 * findUnpromotedSpots) are kept free of DOM/window access so they run
 * under a bare `vm` sandbox in tests/js/portfolio-controller.test.cjs.
 *
 * The excluded/missing-data banner has THREE independent slots: positions
 * the backend has but couldn't value (summary), a truncated result set
 * (summary or history), and — the newest — spot holdings that exist only
 * as a chart drawing on this device and never made it into the ledger at
 * all, so the backend never even knew to ask about them.
 */
(function() {
'use strict';

// ── Pure helpers ──────────────────────────────────────────────────────

function formatUsd(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    var neg = n < 0;
    var abs = Math.abs(n);
    var fixed = abs.toFixed(2);
    var parts = fixed.split('.');
    var intPart = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (neg ? '-' : '') + '$' + intPart + '.' + parts[1];
}

var REASON_LABELS = {
    no_size: 'no size recorded',
    no_price: 'no price available',
    no_entry: 'no entry price',
};

// Groups excluded rows by reason and names the assets in each group — the
// count alone tells the reader the totals are short, but the asset names
// are what let them actually go fix the trade (log a size, an entry, etc).
// States the total count first so the headline lands before the detail.
function buildExcludedMessage(excluded) {
    excluded = excluded || [];
    if (!excluded.length) return '';

    var assetsByLabel = {};
    var order = [];
    excluded.forEach(function(row) {
        var label = REASON_LABELS[row.reason] || row.reason;
        if (!assetsByLabel.hasOwnProperty(label)) {
            assetsByLabel[label] = [];
            order.push(label);
        }
        assetsByLabel[label].push(row.asset);
    });

    var reasonParts = order.map(function(label) {
        return assetsByLabel[label].join(', ') + ' (' + label + ')';
    });

    var n = excluded.length;
    return n + ' position' + (n === 1 ? '' : 's') +
        ' excluded from the totals above — ' + reasonParts.join('; ') + '.';
}

// carrac/portfolio/history.py reports `excluded_count` on EVERY point of
// the curve — a day where at least one open position had no usable close
// and was left out of that day's book/cost totals rather than valued at
// zero. Before this, nothing in static/js/ ever read the field (`rg
// excluded_count static/js/` found nothing) — the backend computed it and
// the frontend threw it away, so a dip or a flat stretch on the curve
// could just as easily be a coverage gap as a real move, with no way to
// tell which from the page. This is a coarse day-level flag, not a
// per-point marker on the chart itself — enough to warn the reader the
// curve may be short on those days, without the extra render-path work
// a per-point chart annotation would need.
function buildCurveExclusionMessage(curve) {
    var anyExcluded = (curve || []).some(function(d) {
        return d && d.excluded_count > 0;
    });
    if (!anyExcluded) return '';
    return 'Some days in this range could not price every open position — ' +
        'the curve may understate the book on those days.';
}

// Nulls/undefined always sort last regardless of direction — a missing
// value is not "smallest", it's unorderable, and burying it at the top
// of a descending sort would look like the biggest position in the book.
function sortRows(rows, key, ascending) {
    var copy = (rows || []).slice();
    copy.sort(function(a, b) {
        var av = a[key];
        var bv = b[key];
        var aNull = (av === null || av === undefined);
        var bNull = (bv === null || bv === undefined);
        if (aNull && bNull) return 0;
        if (aNull) return 1;
        if (bNull) return -1;

        var cmp;
        if (typeof av === 'string' || typeof bv === 'string') {
            cmp = String(av).toLowerCase().localeCompare(String(bv).toLowerCase());
        } else {
            cmp = av < bv ? -1 : (av > bv ? 1 : 0);
        }
        return ascending ? cmp : -cmp;
    });
    return copy;
}

// A spot buy that never made it into trade_journal (its promotion POST
// failed, or it predates Task 10 and the one-time backfill hasn't run for
// this user yet) is invisible to /api/portfolio/summary entirely — it
// isn't merely unvalued, it doesn't exist as far as the backend is
// concerned, so it can never show up in `excluded`. The only place left
// that still knows about it is the chart drawing itself, in this
// browser's localStorage — `extendData._journalDrawingId` is the exact
// stamp Task 10's live path and the backfill script both write on
// success, so its absence is the definitive "not yet promoted" signal
// (see scripts/backfill_spot_journal.py's primary gate, which this
// mirrors: name === 'spot', unstamped, not sold, positive quantity).
function findUnpromotedSpots(drawingsStore) {
    var count = 0;
    var assets = [];
    var seenAsset = {};

    if (!drawingsStore || typeof drawingsStore !== 'object') {
        return {count: count, assets: assets};
    }

    Object.keys(drawingsStore).forEach(function(symbol) {
        var list = drawingsStore[symbol];
        if (!Array.isArray(list)) return;

        list.forEach(function(d) {
            if (!d || d.name !== 'spot') return;
            var ext = d.extendData || {};
            if (ext._journalDrawingId !== null && ext._journalDrawingId !== undefined) return;
            if (ext.sold) return;
            if (!(ext.quantity > 0)) return;

            count += 1;
            if (!seenAsset.hasOwnProperty(symbol)) {
                seenAsset[symbol] = true;
                assets.push(symbol);
            }
        });
    });

    return {count: count, assets: assets};
}

// Merges summary.positions with the allocation.by_asset weights so each
// table row carries its own weight_pct. An asset absent from the
// allocation block (e.g. it was excluded, or rounds to nothing) gets 0,
// never undefined — undefined would sort inconsistently and render blank.
function tableRowsFrom(summary) {
    summary = summary || {};
    var positions = summary.positions || [];
    var byAsset = (summary.allocation && summary.allocation.by_asset) || [];

    var weightByAsset = {};
    byAsset.forEach(function(a) {
        weightByAsset[a.asset] = a.weight_pct;
    });

    return positions.map(function(p) {
        var row = {};
        Object.keys(p).forEach(function(k) { row[k] = p[k]; });
        row.weight_pct = weightByAsset.hasOwnProperty(p.asset) ? weightByAsset[p.asset] : 0;
        return row;
    });
}

// ── DOM-facing controller (guarded — inert if loaded off-page) ─────────


function formatPctSuffix(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) return '';
    var sign = pct >= 0 ? '+' : '';
    return ' (' + sign + pct.toFixed(2) + '%)';
}

function formatPct(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) return '—';
    return pct.toFixed(2) + '%';
}

function formatSize(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return String(n);
}

function setText(id, text) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
}

function getJson(url) {
    return fetch(url, {credentials: 'same-origin'}).then(function(r) {
        // Check r.ok before touching the body — a non-2xx response is a
        // failure regardless of whether its body happens to parse as JSON.
        if (!r.ok) {
            throw new Error('Request to ' + url + ' failed with status ' + r.status);
        }
        return r.json();
    });
}

function formatOpenedAt(s) {
    if (!s) return '—';
    var d = new Date(s);
    if (isNaN(d.getTime())) return String(s);
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return months[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
}

// Matches the app's existing chart-open contract: main.js reads
// `?chart=SYMBOL` off the URL on the dashboard's DOMContentLoaded (and
// falls back to sessionStorage/localStorage) to auto-open that asset's
// chart, in both the fullscreen-mobile and split-desktop paths. This is
// the only place in the codebase that currently *produces* that link —
// journal-page.js does not link out to a chart at all, so this reuses
// the real consumer in main.js instead of inventing a new URL scheme.
function navigateToChart(asset) {
    if (typeof window === 'undefined' || !window.location || !asset) return;
    window.location.href = '/?chart=' + encodeURIComponent(asset);
}

// Defensive on every axis: localStorage can be unavailable (SSR-ish sandbox,
// privacy mode), the key can be absent, and the value — five other files
// write this same key — can in principle be malformed. Any of those must
// fall back to "no drawings", never take the page down.
function readDrawingsStore() {
    if (typeof localStorage === 'undefined' || !localStorage) return {};
    var raw = null;
    try {
        raw = localStorage.getItem('klineDrawings');
    } catch (e) {
        return {};
    }
    try {
        var parsed = JSON.parse(raw || '{}');
        return (parsed && typeof parsed === 'object') ? parsed : {};
    } catch (e) {
        return {};
    }
}

// Deliberately says "on this device" rather than implying a check across
// every device the user has ever drawn on — chart drawings live in this
// browser's localStorage, so that is the honest limit of what this can see.
//
// Deliberately hedged ("may not be counted"), not asserted as fact. This
// check only reads THIS browser's localStorage, which a server-side
// backfill (scripts/backfill_spot_journal.py) never touches — right after
// a backfill runs, the exact scenario this banner exists for, the server
// has already linked these holdings but the local stamp hasn't synced
// down yet. Stating "not counted" as a certainty in that window is
// actively wrong, not just imprecise: it tells the user their holdings
// are missing when they aren't. It self-heals on the next chart visit for
// that asset (the stamp syncs down then), which is why the wording points
// at that instead of implying a permanent gap.
function buildUnpromotedMessage(result) {
    if (!result || !result.count) return '';
    var n = result.count;
    var assets = result.assets || [];
    var assetList = assets.length ? ' (' + assets.join(', ') + ')' : '';
    var verb = n === 1 ? 'is' : 'are';
    var pronoun = n === 1 ? 'it' : 'they';
    return n + ' spot holding' + (n === 1 ? '' : 's') + ' on this device' + assetList +
        ' ' + verb + ' not linked to your ledger here yet, so ' + pronoun +
        ' may not be counted in the totals above. Open the chart once to ' +
        'refresh this check — it clears itself if the holding is already ' +
        'recorded server-side (e.g. right after a backfill).';
}

var _rows = [];             // last tableRowsFrom() result
var _sortField = 'book_contribution';
var _sortAsc = false;       // biggest position first by default
var _summaryBannerMsg = '';
var _historyBannerMsg = '';
var _drawingsBannerMsg = '';

// A failed fetch must never look like an empty portfolio: the hero tiles
// would otherwise sit at the template's placeholder em-dashes forever,
// which is indistinguishable from "still loading" or "genuinely zero
// positions" (that state renders $0.00, not em-dashes, but a permanently
// stuck page gives the user no way to tell "broken" from "not done yet").
// These messages reuse the same #pf-excluded banner and overwrite
// whichever slot failed — if the summary call is down there is no
// position data to report an exclusion count for anyway.
var SUMMARY_ERROR_MSG = 'Could not load portfolio positions — try refreshing the page.';
var HISTORY_ERROR_MSG = 'Could not load portfolio history — try refreshing the page.';

function updateBanner() {
    var el = document.getElementById('pf-excluded');
    if (!el) return;
    // Three independent slots — summary (excluded/truncated/error), history
    // (truncated/error), and drawings (unpromoted spots) — each written by
    // its own loader and joined here. None of them resets another, so
    // whichever arrives first, second, or third, the other two survive.
    var parts = [];
    if (_summaryBannerMsg) parts.push(_summaryBannerMsg);
    if (_historyBannerMsg) parts.push(_historyBannerMsg);
    if (_drawingsBannerMsg) parts.push(_drawingsBannerMsg);
    if (parts.length) {
        el.textContent = parts.join(' ');
        el.style.display = '';
    } else {
        el.textContent = '';
        el.style.display = 'none';
    }
}

// Synchronous (localStorage, not a fetch) — reads this device's chart
// drawings and flags any spot buy that never made it into the ledger.
// This is the only banner slot that can fire even when both API calls
// succeed and report nothing wrong: the missing holding was never in the
// backend's data at all, so /api/portfolio/summary has no way to know
// about it.
function checkUnpromotedSpots() {
    var result = findUnpromotedSpots(readDrawingsStore());
    _drawingsBannerMsg = buildUnpromotedMessage(result);
    updateBanner();
}

function renderHero(summary) {
    setText('pf-book-value', formatUsd(summary.book_value));
    var spotCount = summary.spot_count != null ? summary.spot_count : 0;
    var futuresCount = summary.futures_count != null ? summary.futures_count : 0;
    setText('pf-book-split', spotCount + ' spot · ' + futuresCount + ' futures');
    setText('pf-day-change', formatUsd(summary.day_change_usd) + formatPctSuffix(summary.day_change_pct));
    setText('pf-unrealized', formatUsd(summary.unrealized_pnl_usd) + formatPctSuffix(summary.unrealized_pnl_pct));
    setText('pf-cost-basis', 'Cost basis ' + formatUsd(summary.cost_basis));
    setText('pf-position-count', summary.position_count != null ? String(summary.position_count) : '—');
}

// Every new account has zero positions on day one — without this, they see
// three widgets (curve/allocation/table) rendered against nothing, which
// reads as broken rather than "you haven't logged anything yet". Swaps in
// one honest message instead. #pf-sections/#pf-empty are optional (guarded)
// so this is a no-op on any older page markup that doesn't have them.
function updateEmptyState(summary) {
    var isEmpty = summary.position_count === 0;
    var emptyEl = document.getElementById('pf-empty');
    var sectionsEl = document.getElementById('pf-sections');
    if (emptyEl) emptyEl.style.display = isEmpty ? '' : 'none';
    if (sectionsEl) sectionsEl.style.display = isEmpty ? 'none' : '';
}

function buildRowHtml(row) {
    return '<tr data-asset="' + escapeHtml(row.asset) + '" tabindex="0" style="cursor:pointer">' +
        '<td>' + escapeHtml(row.asset) + '</td>' +
        '<td>' + escapeHtml(row.kind) + '</td>' +
        '<td>' + escapeHtml(row.direction) + '</td>' +
        '<td>' + escapeHtml(formatSize(row.size)) + '</td>' +
        '<td>' + escapeHtml(formatUsd(row.entry_price)) + '</td>' +
        '<td>' + escapeHtml(formatUsd(row.current_price)) + '</td>' +
        '<td>' + escapeHtml(formatUsd(row.book_contribution)) + '</td>' +
        '<td>' + escapeHtml(formatUsd(row.unrealized_pnl_usd)) + '</td>' +
        '<td>' + escapeHtml(formatPct(row.weight_pct)) + '</td>' +
        '<td>' + escapeHtml(formatOpenedAt(row.opened_at)) + '</td>' +
        '</tr>';
}

function renderTable() {
    var tbody = document.getElementById('pf-table-body');
    if (!tbody) return;
    var sorted = sortRows(_rows, _sortField, _sortAsc);
    tbody.innerHTML = sorted.map(buildRowHtml).join('');
}

function loadSummary() {
    return getJson('/api/portfolio/summary').then(function(summary) {
        summary = summary || {};
        renderHero(summary);
        updateEmptyState(summary);

        _summaryBannerMsg = buildExcludedMessage(summary.excluded || []);
        if (summary.truncated) {
            _summaryBannerMsg = (_summaryBannerMsg ? _summaryBannerMsg + ' ' : '') +
                'Position results hit a cap — totals above may be incomplete.';
        }
        updateBanner();

        // The positions table is the data the user actually came for — it
        // must render before the chart calls below, and survive even if
        // they throw. A blocked CDN or an SRI mismatch on the ECharts
        // <script> (templates/portfolio.html) leaves `window.echarts`
        // undefined; renderAllocationDonut/renderCompositionBars would
        // throw synchronously the first time they touch it. If charts rendered
        // ahead of the table, that throw would abort this whole .then()
        // callback before `_rows`/`renderTable()` ever ran, leaving the table
        // empty while the generic fetch-catch banner below overwrote the
        // message — even though the summary fetch succeeded. Table first, charts wrapped in their own try/catch, so a
        // chart-library failure costs the charts, never the table.
        _rows = tableRowsFrom(summary);
        renderTable();

        try {
            var allocation = summary.allocation || {};
            if (typeof window.renderAllocationDonut === 'function') {
                window.renderAllocationDonut('pf-donut', allocation.by_asset || []);
            }
            if (typeof window.renderCompositionBars === 'function') {
                window.renderCompositionBars('pf-split', allocation.by_kind || {}, allocation.by_direction || {});
            }
        } catch (e) {
            if (typeof window.carracLog === 'function') {
                window.carracLog('portfolio', 'allocation chart render failed', {err: String(e)});
            }
        }
    }).catch(function() {
        _summaryBannerMsg = SUMMARY_ERROR_MSG;
        updateBanner();
    });
}

function loadHistory() {
    var rangeSel = document.getElementById('pf-curve-range');
    var days = (rangeSel && rangeSel.value) ? rangeSel.value : '90';
    return getJson('/api/portfolio/history?days=' + encodeURIComponent(days)).then(function(data) {
        data = data || {};
        var curve = data.curve || [];

        // Banner text first, independent of whether the chart itself
        // renders — same reasoning as loadSummary's table-before-charts
        // ordering (I5): a chart-library throw must not cost the page the
        // truncated/excluded-coverage messages it already has the data for.
        var parts = [];
        if (data.truncated) {
            parts.push('History range hit a cap — the curve may not cover the full period.');
        }
        var exclusionMsg = buildCurveExclusionMessage(curve);
        if (exclusionMsg) parts.push(exclusionMsg);
        _historyBannerMsg = parts.join(' ');
        updateBanner();

        try {
            if (typeof window.renderBookCurve === 'function') {
                window.renderBookCurve('pf-curve', curve);
            }
        } catch (e) {
            if (typeof window.carracLog === 'function') {
                window.carracLog('portfolio', 'book curve chart render failed', {err: String(e)});
            }
        }
    }).catch(function() {
        _historyBannerMsg = HISTORY_ERROR_MSG;
        updateBanner();
    });
}

function bindSortHeaders() {
    var headers = document.querySelectorAll('.pf-table thead [data-sort]');
    headers.forEach(function(th) {
        th.addEventListener('click', function() {
            var field = th.getAttribute('data-sort');
            if (_sortField === field) _sortAsc = !_sortAsc;
            else { _sortField = field; _sortAsc = true; }
            renderTable();
        });
    });
}

// A single delegated `click` listener covers both mouse and touch — a
// tap on a real device dispatches `click` the same as a mouse click, so
// no separate touchend handler is needed. What matters is that
// activation lives on the row's click handler itself, not behind a
// hover-revealed affordance that a touchscreen can never trigger.
//
// A <tr> is not natively interactive, so giving it tabindex="0" without
// binding keydown would make it a tab stop that does nothing on Enter or
// Space — worse than not being focusable, because it promises a keyboard
// interaction that isn't there. Enter/Space are translated to the same
// navigateToChart() call as the click handler; Space is prevented so it
// doesn't also scroll the page.
function bindRowActivation() {
    var tbody = document.getElementById('pf-table-body');
    if (!tbody) return;
    tbody.addEventListener('click', function(e) {
        var tr = e.target && e.target.closest ? e.target.closest('tr[data-asset]') : null;
        if (!tr) return;
        navigateToChart(tr.getAttribute('data-asset'));
    });
    tbody.addEventListener('keydown', function(e) {
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
        var tr = e.target && e.target.closest ? e.target.closest('tr[data-asset]') : null;
        if (!tr) return;
        if (e.key === ' ' || e.key === 'Spacebar') e.preventDefault();
        navigateToChart(tr.getAttribute('data-asset'));
    });
}

function init() {
    var rangeSel = document.getElementById('pf-curve-range');
    if (rangeSel) {
        rangeSel.addEventListener('change', loadHistory);
    }
    bindSortHeaders();
    bindRowActivation();
    loadSummary();
    loadHistory();
    checkUnpromotedSpots();
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('DOMContentLoaded', init);
}

if (typeof window !== 'undefined') {
    window.Portfolio = {
        formatUsd: formatUsd,
        sortRows: sortRows,
        buildExcludedMessage: buildExcludedMessage,
        buildCurveExclusionMessage: buildCurveExclusionMessage,
        tableRowsFrom: tableRowsFrom,
        findUnpromotedSpots: findUnpromotedSpots,
        // String-level, DOM-free — exists so an XSS hole (e.g.
        // a dropped escapeHtml() call) fails a test instead of shipping quietly.
        // Tests that drive loadSummary()/renderTable() feed empty position
        // arrays, so only a direct buildRowHtml call exercises the markup.
        buildRowHtml: buildRowHtml,
        // Exposed beyond the pure helpers above so the fetch-failure paths
        // (a rejected fetch, a non-2xx response) and the unpromoted-spots
        // banner are directly testable without a real DOM — see
        // portfolio-controller.test.cjs.
        loadSummary: loadSummary,
        loadHistory: loadHistory,
        checkUnpromotedSpots: checkUnpromotedSpots,
        // Exposed so the panel-view controller (split-layout.js:setViewMode)
        // Can drive the exact same first-load binding the
        // standalone page's own DOMContentLoaded handler runs — see
        // ensurePortfolioPanel() in app-core.js for why that handler is a no-op
        // there (the script loads after DOMContentLoaded has already fired).
        init: init,
    };
}

})();
