/* Plays — backtest evidence panel.
 *
 * Split out of plays-panel.js, which is a flat classic script with no IIFE
 * (unlike journal-page.js), so everything here is global scope and every
 * shared name below is late-bound at call time. The file has no top-level
 * executable statements, so script order does not matter beyond both files
 * being present.
 *
 * Reads from plays-panel.js: _playsData, _playsExpandedId.
 * _playBacktestCache moved WITH this section — its only reference outside
 * the region was its own declaration.
 * Provides: _canBacktestPlay, _renderPlayBacktestBadge,
 * _renderPlayBacktestDetail, _loadPlayBacktest, _refreshPlayBacktestCards,
 * _bindPlayBacktestOpenBtn, _openInBacktester.
 */

// Per-play backtest result cache (persists across re-renders within session).
// key = play.id, value = { loading: bool, result: {}, error: str }
var _playBacktestCache = {};

// ── Play backtest (Hold-N evidence) ──

function _canBacktestPlay(play) {
    if (!play || !play.primary_asset) return false;
    var rules = (play.conditions || {}).rules || [];
    // Must have at least one backtestable rule type
    var supported = ['indicator_value', 'indicator_crosses', 'price_level',
                     'derivatives_metric', 'formula_expression'];
    return rules.some(function(r) { return supported.indexOf(r.type) !== -1; });
}

function _renderPlayBacktestBadge(play) {
    if (!_canBacktestPlay(play)) return '';
    var cached = _playBacktestCache[play.id];
    if (!cached) {
        return '<span class="play-bt-badge loading" data-play-id="' + play.id +
            '" title="Hold-N historical backtest: loading...">\u2026 bt</span>';
    }
    if (cached.error) {
        return '<span class="play-bt-badge err" data-play-id="' + play.id +
            '" title="Backtest: ' + escapeHtml(cached.error) + '">\u2014 bt</span>';
    }
    var r = cached.result;
    if (!r || r.trades === 0) {
        return '<span class="play-bt-badge empty" data-play-id="' + play.id +
            '" title="No signal flips in 365-bar window">0 bt</span>';
    }
    var hit = (r.hit_rate != null) ? Math.round(r.hit_rate * 100) : null;
    var mean = (r.mean_return != null) ? (r.mean_return * 100) : null;
    var cls = 'pos';
    if (hit != null && hit < 50) cls = 'neg';
    if (hit == null) cls = '';
    var title = 'Hold-' + (r.hold_bars || 5) + ' backtest (last ' + (r.lookback_bars || 365) + ' bars):\n' +
        r.trades + ' flips · ' + (hit != null ? hit + '% hit' : 'no hit rate') +
        (mean != null ? ' · ' + (mean >= 0 ? '+' : '') + mean.toFixed(1) + '% mean' : '');
    return '<span class="play-bt-badge ' + cls + '" data-play-id="' + play.id +
        '" title="' + escapeHtml(title) + '">' +
        (hit != null ? hit + '%' : '?') + ' bt · ' + r.trades + '</span>';
}

function _renderPlayBacktestDetail(play) {
    if (!_canBacktestPlay(play)) {
        var rules = (play.conditions || {}).rules || [];
        if (rules.length === 0) return '';
        return '<div class="play-bt-detail" style="margin-top:8px;padding:6px 8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:4px;font-size:11px;color:#8b949e;">' +
            'No backtestable conditions. Only manual / scanner_verdict rules can\'t be tested.' +
            '</div>';
    }
    var cached = _playBacktestCache[play.id];
    var body;
    if (!cached || cached.loading) {
        body = '<span style="color:#8b949e">Loading historical backtest\u2026</span>';
    } else if (cached.error) {
        body = '<span style="color:#ef5350">\u26A0 ' + escapeHtml(cached.error) + '</span>';
    } else if (!cached.result || cached.result.trades === 0) {
        body = '<span style="color:#8b949e">No signal flips in last ' +
            (cached.result && cached.result.lookback_bars ? cached.result.lookback_bars : 365) +
            ' bars. Try wider lookback in the full backtester.</span>';
    } else {
        var r = cached.result;
        var hit = r.hit_rate != null ? (r.hit_rate * 100).toFixed(1) : '\u2014';
        var mean = r.mean_return != null ? (r.mean_return * 100) : 0;
        var median = r.median_return != null ? (r.median_return * 100) : 0;
        var sharpe = r.sharpe_like != null ? r.sharpe_like.toFixed(2) : '\u2014';
        var mdd = r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(1) + '%' : '\u2014';
        var skipped = r.conditions_skipped && r.conditions_skipped.length > 0
            ? ' <span style="color:#8b949e">(' + r.conditions_skipped.length + ' conditions not backtestable)</span>'
            : '';
        body =
            '<div style="display:flex;gap:14px;flex-wrap:wrap;font-size:11px;">' +
                '<span><strong>' + r.trades + '</strong> flips</span>' +
                '<span><strong>' + hit + '%</strong> hit</span>' +
                '<span>' + (mean >= 0 ? '+' : '') + mean.toFixed(2) + '% mean</span>' +
                '<span>' + (median >= 0 ? '+' : '') + median.toFixed(2) + '% med</span>' +
                '<span>Sharpe-like <strong>' + sharpe + '</strong></span>' +
                '<span>Max DD <strong>' + mdd + '</strong></span>' +
            '</div>' + skipped;
    }
    var asset = play.primary_asset || '';
    var tf = play.timeframe || '';
    return '<div class="play-bt-detail" style="margin-top:8px;padding:6px 8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:4px;">' +
        '<div style="font-size:11px;color:#8b949e;margin-bottom:4px;">' +
            'Hold-N historical evidence <span title="Flip-signal backtest: fires when any backtestable rule flips True, holds 5 bars, measures forward return. Distinct from the full UI backtester which models entries, exits, fees, and sizing.">\u24D8</span>' +
        '</div>' +
        body +
        '<div style="margin-top:6px;">' +
            '<button class="play-bt-open-btn" data-play-id="' + play.id +
                '" data-asset="' + escapeHtml(asset) + '" data-tf="' + escapeHtml(tf) + '"' +
                ' style="font-size:11px;padding:3px 8px;background:#1f6feb;color:#fff;border:none;border-radius:3px;cursor:pointer;">Open in Backtester</button>' +
        '</div>' +
    '</div>';
}

function _loadPlayBacktest(playId) {
    // Skip if already cached with a non-error result or currently loading
    var existing = _playBacktestCache[playId];
    if (existing && (existing.loading || existing.result)) return;
    _playBacktestCache[playId] = { loading: true };
    fetch('/api/plays/' + playId + '/backtest', { credentials: 'same-origin' })
        .then(function(r) { return r.json().then(function(body) { return { ok: r.ok, body: body }; }); })
        .then(function(resp) {
            if (!resp.ok) {
                _playBacktestCache[playId] = { loading: false, error: resp.body.error || 'Backtest failed' };
            } else {
                _playBacktestCache[playId] = { loading: false, result: resp.body };
            }
            _refreshPlayBacktestCards(playId);
        })
        .catch(function() {
            _playBacktestCache[playId] = { loading: false, error: 'Network error' };
            _refreshPlayBacktestCards(playId);
        });
}

function _refreshPlayBacktestCards(playId) {
    // Update badge in-place
    document.querySelectorAll('.play-bt-badge[data-play-id="' + playId + '"]').forEach(function(el) {
        var play = _playsData.find(function(p) { return p.id === playId; });
        if (play) {
            el.outerHTML = _renderPlayBacktestBadge(play);
        }
    });
    // Re-render expanded detail if this play is the expanded one
    if (_playsExpandedId === playId) {
        var details = document.querySelectorAll('.play-card[data-play-id="' + playId + '"] .play-bt-detail');
        if (details.length > 0) {
            var play = _playsData.find(function(p) { return p.id === playId; });
            if (play) {
                details[0].outerHTML = _renderPlayBacktestDetail(play);
                // Rebind the open button
                var btn = document.querySelector('.play-bt-open-btn[data-play-id="' + playId + '"]');
                if (btn) _bindPlayBacktestOpenBtn(btn);
            }
        }
    }
}

function _bindPlayBacktestOpenBtn(btn) {
    btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var playId = parseInt(btn.getAttribute('data-play-id'));
        var asset = btn.getAttribute('data-asset') || '';
        var tf = btn.getAttribute('data-tf') || '';
        var cached = _playBacktestCache[playId];
        var expr = cached && cached.result && cached.result.expression ? cached.result.expression : '';
        if (!expr) {
            if (typeof showToast === 'function') showToast('No synthesized formula available');
            return;
        }
        _openInBacktester({ expression: expr, asset: asset, timeframe: tf, playId: playId });
    });
}

function _openInBacktester(opts) {
    // Switch chart asset/tf if needed, open backtester drawer, pre-fill entry formula.
    var expr = opts.expression;
    var asset = (opts.asset || '').toUpperCase();
    var tf = opts.timeframe || '';
    // Play timeframes that aren't real bar TFs fall back to 1D (matches server synth logic)
    var realTf = (tf && tf !== 'swing' && tf !== 'position' && tf !== 'until_event' && tf !== 'open_ended') ? tf : '1D';

    try {
        var curSym = (typeof klineChartSymbol !== 'undefined') ? klineChartSymbol : '';
        var curTf = (typeof klineChartTimeframe !== 'undefined') ? klineChartTimeframe : '';
        if (typeof loadKlineChart === 'function' && (curSym !== asset || curTf !== realTf)) {
            loadKlineChart(asset || curSym, realTf);
        }
    } catch (e) { /* best effort */ }

    // Open the backtester drawer (openDrawer also calls BacktestUI.render())
    try {
        if (typeof openDrawer === 'function') openDrawer('backtest');
    } catch (e) {}

    // Wait for chart + panel render, then pre-fill
    setTimeout(function() {
        var entryEl = document.getElementById('bt-entry');
        var exitEl = document.getElementById('bt-exit');
        if (entryEl) {
            entryEl.value = expr;
            entryEl.dispatchEvent(new Event('input', { bubbles: true }));
        }
        // Simple fallback exit: invalidate when entry is no longer true
        if (exitEl && !exitEl.value) {
            exitEl.value = 'NOT (' + expr + ')';
            exitEl.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (typeof showToast === 'function') showToast('Pre-filled from play — review exit formula before running');
    }, 150);
}
