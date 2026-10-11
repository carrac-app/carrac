// Share modal (generalized) — mint / manage the public share link for
// a Play or a closed journal Trade.
//
// Self-contained: injects its own scoped styles (ps-*). Kind-parameterized
// (endpoint, update HTTP verb, toggle set + labels, share text, optional
// staleness hint) so plays and trades reuse one modal. Writes go to the
// FastAPI-live share endpoints (POST = get-or-create, PUT/PATCH = toggles /
// refresh-snapshot, DELETE = revoke). FastAPI errors arrive flat (no
// {detail} wrapper for dict details) — _api unwraps the rare string-detail.
//
// Exposes:
//   window.ShareModal.open(kind, id, subject)
//   window.PlayShare.open(playId, play)     — back-compat (plays-panel.js)
//   window.TradeShare.open(tradeId, trade)  — journal close-flow + Share btn

/**
 * Pure decision core for the chart-capture size gate —
 * no DOM, no globals; unit-tested via tests/js/chart-capture-gate.test.cjs.
 * Mirrors the chart-live-tick.js decision-core convention (pure fn + dual
 * window/module.exports export at the end of the file).
 *
 * MAX_CAPTURE_CHARS headroom: decode_chart_capture's decoded cap
 * (carrac/plays/shares.py _MAX_CAPTURE_BYTES) is 4 MiB, and base64 expands
 * bytes by ~4/3 → 4 MiB * 4/3 ≈ 5.59M chars. 5.5M leaves slack before the
 * server's own pre-decode length check would 400.
 * @param {number} lengthChars data-URL string length (image.length)
 * @returns {{keep: boolean, reason: string}}
 */
function decideCaptureSize(lengthChars) {
    var MAX_CAPTURE_CHARS = 5500000;
    if (typeof lengthChars !== 'number' || !isFinite(lengthChars) || lengthChars <= 0) {
        return { keep: false, reason: 'empty' };
    }
    if (lengthChars > MAX_CAPTURE_CHARS) {
        return { keep: false, reason: 'oversize' };
    }
    return { keep: true, reason: 'ok' };
}

/**
 * Pure WYSIWYG filter for the chart-share drawing set — no DOM, no globals;
 * unit-tested via tests/js/share-capture-state.test.cjs.
 * Keeps only drawings that are actually visible (master hide-all off, no
 * per-drawing _hidden flag) and that carry a numeric server id the freezer
 * (_freeze_drawings) can look up; local-only string ids are dropped.
 * @param {Array} drawings the symbol's entries from the klineDrawings store
 * @param {boolean} allHidden the klineAllDrawingsHidden master flag
 * @returns {number[]} at most 100 ids (ChartShareRequest.drawing_ids cap)
 */
function computeVisibleDrawingIds(drawings, allHidden) {
    if (allHidden || !Array.isArray(drawings)) return [];
    var ids = [];
    for (var i = 0; i < drawings.length && ids.length < 100; i++) {
        var d = drawings[i];
        if (!d) continue;
        if (d.extendData && d.extendData._hidden) continue;
        var n = parseInt(d.id, 10);
        if (!isNaN(n) && String(n) === String(d.id)) ids.push(n);
    }
    return ids;
}

(function() {
    'use strict';

    var _share = null;     // last payload from the API
    var _id = null;        // play_id or trade_id
    var _subject = null;   // play / trade object (for labels + staleness)
    var _cfg = null;       // active kind config

    // ── Per-kind configuration ──
    var KINDS = {
        play: {
            title: 'Share play',
            endpoint: function(id) { return '/api/plays/' + id + '/share'; },
            updateMethod: 'PUT',
            toggles: [
                { key: 'include_thesis', label: 'Thesis' },
                { key: 'include_conditions', label: 'Conditions' },
                { key: 'include_plan', label: 'Plan & levels' },
                { key: 'include_outcome', label: 'Outcome (result, once completed)' }
            ],
            shareText: function(s, subj) {
                var title = s.snapshot_title || (subj && subj.title) || 'My market scenario';
                return title + ' — shared via Carrac.cc';
            },
            // Plays can drift from their frozen snapshot (status changes).
            stale: function(s, subj) {
                return subj && s.snapshot_status && subj.status &&
                    s.snapshot_status !== subj.status;
            },
            staleNote: function(subj) {
                return 'Snapshot predates this play’s “' + subj.status + '” status.';
            }
        },
        trade: {
            title: 'Share trade result',
            endpoint: function(id) { return '/api/journal/trades/' + id + '/share'; },
            updateMethod: 'PATCH',
            toggles: [
                { key: 'include_levels', label: 'Entry / exit prices' },
                { key: 'include_note', label: 'Close note' }
            ],
            shareText: function(s, subj) {
                var asset = s.snapshot_asset || (subj && subj.asset) || 'My trade';
                var pnl = s.snapshot_pnl_pct;
                var pct = (typeof pnl === 'number')
                    ? ' ' + (pnl > 0 ? '+' : '') + pnl.toFixed(1) + '%' : '';
                return asset + pct + ' — trade result via Carrac.cc';
            },
            stale: function() { return false; },  // closed trades don't drift
            staleNote: function() { return ''; }
        },
        chart: {
            title: 'Share chart',
            // Chart shares are a frozen MOMENT: no toggles, no re-freeze, and
            // revocation is by slug (no subject id). Minted via openChart().
            toggles: [],
            noRefresh: true,
            revokeUrl: function(s) { return '/api/chart/share/' + s.slug; },
            shareText: function(s) {
                var sym = s.snapshot_symbol || 'Chart';
                var tf = s.snapshot_timeframe ? ' ' + s.snapshot_timeframe : '';
                return sym + tf + ' chart — via Carrac.cc';
            },
            stale: function() { return false; },
            staleNote: function() { return ''; }
        }
    };

    function _csrf() {
        return (typeof window !== 'undefined' && window.getCSRFToken) ? window.getCSRFToken() : '';
    }

    function _toast(msg) {
        if (typeof showToast === 'function') showToast(msg);
        else if (typeof toast === 'function') toast(msg);
    }

    function _api(method, body, url) {
        return fetch(url || _cfg.endpoint(_id), {
            method: method,
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrf() },
            body: body === undefined ? undefined : JSON.stringify(body)
        }).then(function(r) {
            return r.json().catch(function() { return {}; }).then(function(data) {
                return { status: r.status, data: data.detail !== undefined ? data.detail : data };
            });
        });
    }

    function _ensureStyles() {
        if (document.getElementById('ps-styles')) return;
        var css = '' +
        '.ps-overlay{position:fixed;inset:0;background:rgba(1,4,9,.72);z-index:30000;display:flex;align-items:center;justify-content:center;padding:16px}' +
        '.ps-modal{background:#161b22;border:1px solid #30363d;border-radius:12px;width:100%;max-width:430px;padding:18px;color:#c9d1d9;font-size:.85rem;max-height:90vh;overflow-y:auto}' +
        '.ps-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}' +
        '.ps-title{font-weight:700;color:#e6edf3;font-size:.95rem}' +
        '.ps-close{background:none;border:none;color:#8b949e;font-size:1.2rem;cursor:pointer;padding:2px 6px}' +
        '.ps-linkrow{display:flex;gap:7px;margin-bottom:6px}' +
        '.ps-link{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:7px;color:#58a6ff;padding:8px 10px;font-size:.78rem;font-family:inherit;min-width:0}' +
        '.ps-btn{background:#1c2128;border:1px solid #30363d;border-radius:7px;color:#c9d1d9;padding:7px 12px;cursor:pointer;font-size:.78rem;white-space:nowrap}' +
        '.ps-btn:hover{border-color:#8b949e}' +
        '.ps-btn.primary{background:#238636;border-color:#238636;color:#fff;font-weight:600}' +
        '.ps-btn.danger{color:#ef5350;border-color:#52262b}' +
        '.ps-meta{color:#8b949e;font-size:.72rem;margin-bottom:12px}' +
        '.ps-stale{background:#1c2128;border:1px solid #9e6a03;border-radius:7px;color:#d29922;padding:7px 10px;font-size:.74rem;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:8px}' +
        '.ps-sec{border-top:1px solid #30363d;padding-top:10px;margin-top:4px}' +
        '.ps-sec-h{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:#8b949e;margin-bottom:7px;font-weight:700}' +
        '.ps-toggle{display:flex;align-items:center;gap:9px;padding:4px 0;cursor:pointer}' +
        '.ps-toggle input{accent-color:#3fb950}' +
        '.ps-intents{display:flex;gap:8px;margin:12px 0 4px;flex-wrap:wrap}' +
        '.ps-foot{display:flex;justify-content:space-between;align-items:center;margin-top:12px}';
        var el = document.createElement('style');
        el.id = 'ps-styles';
        el.textContent = css;
        document.head.appendChild(el);
    }

    function _overlay() {
        return document.getElementById('ps-overlay');
    }

    function _close() {
        var ov = _overlay();
        if (ov) ov.remove();
        _share = null; _id = null; _subject = null; _cfg = null;
    }

    function _shareText() {
        return _cfg.shareText(_share || {}, _subject);
    }

    function _render() {
        _ensureStyles();
        var ov = _overlay();
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'ps-overlay';
            ov.className = 'ps-overlay';
            ov.addEventListener('click', function(e) { if (e.target === ov) _close(); });
            document.body.appendChild(ov);
        }

        var s = _share;
        var stale = _cfg.stale(s, _subject);
        var xUrl = 'https://twitter.com/intent/tweet?url=' + encodeURIComponent(s.url) +
            '&text=' + encodeURIComponent(_shareText());
        var tgUrl = 'https://t.me/share/url?url=' + encodeURIComponent(s.url) +
            '&text=' + encodeURIComponent(_shareText());

        var html = '<div class="ps-modal" role="dialog" aria-label="' + _cfg.title + '">';
        html += '<div class="ps-head"><span class="ps-title">' + _cfg.title + '</span>' +
            '<button class="ps-close" id="ps-close" aria-label="Close">×</button></div>';

        html += '<div class="ps-linkrow">' +
            '<input class="ps-link" id="ps-link" readonly value="' + s.url + '">' +
            '<button class="ps-btn primary" id="ps-copy">Copy</button></div>';
        html += '<div class="ps-meta">Public, no account needed · ' +
            (s.view_count || 0) + ' view' + ((s.view_count || 0) === 1 ? '' : 's') +
            ' · frozen snapshot — edits never leak</div>';

        if (stale) {
            html += '<div class="ps-stale"><span>' + _cfg.staleNote(_subject) + '</span>' +
                '<button class="ps-btn" id="ps-refresh">Refresh</button></div>';
        }

        if (_cfg.toggles && _cfg.toggles.length) {
            html += '<div class="ps-sec"><div class="ps-sec-h">Visible on the public page</div>';
            _cfg.toggles.forEach(function(t) {
                html += '<label class="ps-toggle"><input type="checkbox" data-toggle="' + t.key + '"' +
                    (s[t.key] ? ' checked' : '') + '> ' + t.label + '</label>';
            });
            html += '</div>';
        }

        html += '<div class="ps-intents">' +
            '<a class="ps-btn" href="' + xUrl + '" target="_blank" rel="noopener">Post on X</a>' +
            '<a class="ps-btn" href="' + tgUrl + '" target="_blank" rel="noopener">Telegram</a>' +
            '<a class="ps-btn" href="' + s.url + '" target="_blank" rel="noopener">Preview ↗</a>' +
            '</div>';

        var showRefresh = !_cfg.noRefresh && !stale;
        html += '<div class="ps-foot">' +
            (showRefresh ? '<button class="ps-btn" id="ps-refresh">Refresh snapshot</button>' : '<span></span>') +
            '<button class="ps-btn danger" id="ps-revoke">Revoke link</button></div>';
        html += '</div>';

        ov.innerHTML = html;

        ov.querySelector('#ps-close').addEventListener('click', _close);
        ov.querySelector('#ps-copy').addEventListener('click', function() {
            var input = ov.querySelector('#ps-link');
            input.select();
            var done = function() { _toast('Link copied'); };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(s.url).then(done, function() {
                    try { document.execCommand('copy'); done(); } catch (e) {}
                });
            } else {
                try { document.execCommand('copy'); done(); } catch (e) {}
            }
        });
        var refreshBtn = ov.querySelector('#ps-refresh');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', function() {
                _api(_cfg.updateMethod, {}).then(function(res) {
                    if (res.status === 200) {
                        _share = res.data;
                        _toast('Snapshot refreshed');
                        _render();
                    } else {
                        _toast(res.data.error || 'Refresh failed');
                    }
                });
            });
        }
        ov.querySelectorAll('input[data-toggle]').forEach(function(cb) {
            cb.addEventListener('change', function() {
                var body = {};
                body[cb.getAttribute('data-toggle')] = cb.checked;
                _api(_cfg.updateMethod, body).then(function(res) {
                    if (res.status === 200) {
                        _share = res.data;
                        _render();
                    } else {
                        cb.checked = !cb.checked;
                        _toast(res.data.error || 'Update failed');
                    }
                });
            });
        });
        ov.querySelector('#ps-revoke').addEventListener('click', function() {
            if (!window.confirm('Revoke this share link? The public page will show 410 Gone.')) return;
            var revokeUrl = _cfg.revokeUrl ? _cfg.revokeUrl(s) : undefined;
            _api('DELETE', undefined, revokeUrl).then(function(res) {
                if (res.status === 200) {
                    _toast('Share revoked');
                    _close();
                    if (typeof refreshPlays === 'function') refreshPlays();
                    if (typeof loadTrades === 'function') loadTrades();
                } else {
                    _toast(res.data.error || 'Revoke failed');
                }
            });
        });
    }

    function open(kind, id, subject) {
        _cfg = KINDS[kind];
        if (!_cfg) { _toast('Unknown share type'); return; }
        _id = id;
        _subject = subject || null;
        _api('POST', {}).then(function(res) {
            if (res.status === 201 || res.status === 200) {
                _share = res.data;
                _render();
                if (typeof refreshPlays === 'function') refreshPlays();
            } else if (res.status === 402) {
                if (!(typeof handleLimitHitResponse === 'function' &&
                      handleLimitHitResponse(res.data, 'share_limit'))) {
                    _toast(res.data.error || 'Share limit reached — upgrade for more.');
                }
            } else if (res.status === 404) {
                _toast(kind === 'trade' ? 'Trade not found' : 'Play not found');
            } else if (res.status === 400) {
                _toast(res.data.error || 'Only closed trades can be shared');
            } else {
                _toast(res.data.error || 'Could not create share link');
            }
        }).catch(function() {
            _toast('Could not create share link');
        });
    }

    // Chart shares mint from a DESCRIPTOR (symbol/timeframe/…), not a subject
    // id, and ALWAYS create a new frozen moment — so they get their own open.
    function openChart(descriptor) {
        _cfg = KINDS.chart;
        _id = null;
        _subject = null;
        _mintChart(descriptor || {}, true);
    }

    // retryOnOversize: true on the first attempt, false on the fallback retry
    // (one retry only). If the capture body is rejected
    // as too large — HTTP 413, or a network-level failure (some proxies just
    // drop the connection instead of returning a clean 413) — retry ONCE
    // without the image so the share still mints as a server-rendered chart
    // instead of failing outright. This keeps the raised client-side capture
    // gate (decideCaptureSize, above) safe even if nginx's
    // client_max_body_size bump (a deploy-time step) hasn't landed.
    function _mintChart(descriptor, retryOnOversize) {
        _api('POST', descriptor, '/api/chart/share').then(function(res) {
            if (res.status === 201 || res.status === 200) {
                _share = res.data;
                _render();
            } else if (res.status === 413 && retryOnOversize && descriptor.image) {
                _retryChartWithoutImage(descriptor);
            } else if (res.status === 402) {
                if (!(typeof handleLimitHitResponse === 'function' &&
                      handleLimitHitResponse(res.data, 'share_limit'))) {
                    _toast(res.data.error || 'Share limit reached — upgrade for more.');
                }
            } else if (res.status === 404) {
                _toast(res.data.error || 'No chart data to share');
            } else {
                _toast(res.data.error || 'Could not create share link');
            }
        }).catch(function() {
            if (retryOnOversize && descriptor.image) {
                _retryChartWithoutImage(descriptor);
                return;
            }
            _toast('Could not create share link');
        });
    }

    function _retryChartWithoutImage(descriptor) {
        var stripped = Object.assign({}, descriptor);
        delete stripped.image;
        _toast('Capture too large to upload — shared a simpler chart');
        _mintChart(stripped, false);
    }

    // Guarded: this whole IIFE is required as-is by the Node test for
    // decideCaptureSize, and there's no `window` there.
    if (typeof window !== 'undefined') {
        window.ShareModal = { open: open, openChart: openChart, close: _close };
        window.PlayShare = {
            open: function(playId, play) { return open('play', playId, play); },
            close: _close
        };
        window.TradeShare = {
            open: function(tradeId, trade) { return open('trade', tradeId, trade); },
            close: _close
        };
        window.ChartShare = {
            open: function(descriptor) { return openChart(descriptor); },
            close: _close,
            // Called by the share-button handler in chart-events.js when
            // decideCaptureSize() drops an oversize capture client-side, so the
            // user is told instead of silently getting a degraded share.
            toastCaptureDropped: function() {
                _toast('Capture too large — sharing a simpler chart instead.');
            }
        };
    }
})();

// Explicit export, same convention as window.applyChartTick (chart-live-tick.js).
if (typeof window !== 'undefined') {
    window.decideCaptureSize = decideCaptureSize;
    window.computeVisibleDrawingIds = computeVisibleDrawingIds;
}

// Node test hook (tests/js/chart-capture-gate.test.cjs,
// tests/js/share-capture-state.test.cjs); inert in the browser.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        decideCaptureSize: decideCaptureSize,
        computeVisibleDrawingIds: computeVisibleDrawingIds
    };
}
