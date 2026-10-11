// static/js/desk-stream.js — event stream rail. Renders /api/stream data,
// flattened into row_type-tagged rows (zone/signal/transition/liquidation/
// position/event) the same way static/js/scenarios.js's _computeStreamRows
// does for the main Filter drawer — the raw /api/stream payload has NO
// row_type field of its own (it's per-symbol aggregates: entries[].zones/
// signals/transitions/liquidation + top-level positions[]/stream_event).
// NO verdict/score wording (conditions-only).
//
// Type tabs (All/Signals/Transitions/Zones/Liq/Events) with counts + a
// Stream/By-asset view toggle. Mirrors the Radar (Filter) UI's vocabulary in
// scenarios.js's _computeStreamRows and radar.js's lens chips and assetView
// grouping.
(function () {
  'use strict';
  var TAGS = { transition: 'SIGNAL', signal: 'SIGNAL', zone: 'ZONE',
               liquidation: 'LIQ', event: 'CAL', position: 'POS' };
  var _entries = [];

  // Type tabs. 'Signals'/'Transitions' are split (scenarios.js's Filter
  // drawer and radar.js's lens chips both treat transition/signal as
  // distinct types); 'Events' merges the MACRO calendar row with open
  // positions since neither is a chart condition.
  var TAB_ORDER = ['all', 'signals', 'transitions', 'zones', 'liq', 'events'];
  var TAB_LABELS = { all: 'All', signals: 'Signals', transitions: 'Transitions',
                      zones: 'Zones', liq: 'Liq', events: 'Events' };
  // 'all' shows EVENTS (things that happened) — zones are reference levels,
  // not events, so they live only in their own tab and never flood the feed.
  var TAB_TYPES = { all: ['signal', 'transition', 'liquidation', 'event', 'position'],
                     signals: ['signal'], transitions: ['transition'],
                     zones: ['zone'], liq: ['liquidation'], events: ['event', 'position'] };
  var TAB_STORAGE_KEY = 'desk_stream_tab';

  // 'Most significant' rank for by-asset group headers — transition > signal
  // > zone mirrors scenarios.js's stream sort
  // (its typeOrder map: transition 0, signal 1, zone 2); liquidation/event/
  // position aren't ranked there, so they're placed after zone here (a
  // forced-liquidation beat is still notable, cal/position rows are more
  // contextual than a live condition).
  var TYPE_PRIORITY = { transition: 0, signal: 1, liquidation: 2, zone: 3, event: 4, position: 5 };

  var _tab = loadTab();
  var _view = 'stream'; // 'stream' | 'asset'
  var _expanded = {};   // by-asset group expand state, symbol -> bool (in-memory only, not persisted)

  // Pages load shared-utils.js first; plain Node (the check script) requires it.
  var escapeHtml = typeof globalThis.escapeHtml === 'function' ? globalThis.escapeHtml : require('./shared-utils.js').escapeHtml;

  function loadTab() {
    try {
      if (typeof localStorage === 'undefined') return 'all';
      var v = localStorage.getItem('desk_stream_tab');
      return (v && Object.prototype.hasOwnProperty.call(TAB_TYPES, v)) ? v : 'all';
    } catch (e) { return 'all'; }
  }

  function saveTab(tab) {
    try {
      if (typeof localStorage !== 'undefined') localStorage.setItem(TAB_STORAGE_KEY, tab);
    } catch (e) { /* private mode / quota — tab still works in-memory */ }
  }

  // Pure: does a flattened row belong to the given tab? No DOM/module-state
  // dependency, so it's directly Node-testable (tests/js/desk-stream.check.mjs).
  // Zone member vocabulary — mirrors Radar's confluence label map in scenarios.js
  var CONF_LABELS = {
    'S1': 'S1', 'S2': 'S2', 'S3': 'S3', 'R1': 'R1', 'R2': 'R2', 'R3': 'R3',
    'PIVOT': 'Pivot', 'TC': 'CPR Top', 'BC': 'CPR Bottom',
    'PWH': 'Prev Week High', 'PWL': 'Prev Week Low',
    'PMH': 'Prev Month High', 'PML': 'Prev Month Low',
    'PDH': 'Prev Day High', 'PDL': 'Prev Day Low',
    'SW_H': 'Swing High', 'SW_L': 'Swing Low',
    'SW_PH': 'Prev Swing High', 'SW_PL': 'Prev Swing Low',
    'EMA21': 'EMA 21', 'EMA30': 'EMA 30', 'EMA50': 'EMA 50', 'EMA60': 'EMA 60',
    'EMA200': 'EMA 200', 'SMA200': 'SMA 200',
    'BB_U': 'BB Upper', 'BB_M': 'BB Middle', 'BB_L': 'BB Lower',
    'POC': 'POC', 'VAH': 'Value Area High', 'VAL': 'Value Area Low',
    'MH': 'Monthly High', 'ML': 'Monthly Low'
  };
  function expandZoneLabel(label) {
    if (!label) return '';
    return String(label).split('+').map(function (t) {
      return CONF_LABELS[t] || t;
    }).join(' · ');
  }
  function prettyStateToken(t) {
    // Level codes shout ('bc_s1' -> 'BC–S1'); word states stay words
    // ('above_pivot' -> 'above pivot', 'overbought' -> 'overbought').
    var str = String(t);
    var parts = str.split('_');
    var isCode = parts.every(function (p) { return p.length <= 3 || /\d/.test(p); });
    if (isCode && parts.length > 1) {
      return parts.map(function (p) { return p.toUpperCase(); }).join('–');
    }
    return str.replace(/_/g, ' ');
  }

  function matchesTab(row, tab) {
    var types = Object.prototype.hasOwnProperty.call(TAB_TYPES, tab) ? TAB_TYPES[tab] : null;
    if (!types) return true;
    return types.indexOf(row && row.row_type) >= 0;
  }

  // Title-case an indicator/signal-type key ('macd_cross' -> 'Macd Cross').
  // Only used where the API doesn't already ship a friendly label (signals —
  // transitions carry their own 'label' straight from TRACKED_STATES, see
  // carrac/indicator_state.py detect_transitions()).
  function titleCase(s) {
    return String(s || '').replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  // Mirrors scenarios.js's _fmtLiqUsd — "$4.2M" / "$350K".
  function fmtLiqUsd(n) {
    var abs = Math.abs(n || 0);
    if (abs >= 1000000) return '$' + (n / 1000000).toFixed(1) + 'M';
    if (abs >= 1000) return '$' + Math.round(n / 1000) + 'K';
    return '$' + Math.round(n || 0);
  }

  // Relative time from an ISO 'ts' string (only transition rows carry one —
  // see carrac/indicator_state.py detect_transitions(): 'ts': now.isoformat()).
  function relTime(ts) {
    if (!ts) return '';
    try {
      var s = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
      if (!(s >= 0)) return '';
      return s < 60 ? s + 's' : s < 3600 ? Math.floor(s / 60) + 'm' : Math.floor(s / 3600) + 'h';
    } catch (e) { return ''; }
  }

  // Flatten the raw /api/stream payload into row_type-tagged rows. Field
  // names follow the entry shape in carrac/scanner/routes.py and
  // scenarios.js's _computeStreamRows, which does the same flattening for the
  // main Filter drawer.
  function flatten(data) {
    var entries = data.entries || [];
    var rows = [];
    entries.forEach(function (entry) {
      var sym = entry.symbol;
      var livePrice = entry.price;
      // Existence gate: an entry with no valid price can't measure zone
      // proximity at all, so it contributes zero zone rows (scenarios.js
      // _computeStreamRows mirrors this with `if (!livePrice || ...) continue;`).
      var hasPrice = typeof livePrice === 'number' && livePrice > 0;
      (entry.zones || []).forEach(function (zone) {
        if (!hasPrice) return;
        var band = zone.band;
        var dist;
        if (band && band.lower != null && band.upper != null) {
          // Band zone (user-drawn rectangle): proximity to the NEAREST edge,
          // same as scenarios.js (price can be above/inside/below).
          if (livePrice > band.upper) dist = (livePrice - band.upper) / livePrice;
          else if (livePrice < band.lower) dist = (band.lower - livePrice) / livePrice;
          else dist = 0;
        } else {
          // Point zone: distance to zone.price. scenarios.js computes
          // the same ratio (there in percent form, `* 100` vs `> 5`).
          var zp = zone.price;
          if (!(typeof zp === 'number' && zp > 0)) return;
          dist = Math.abs(zp - livePrice) / livePrice;
        }
        if (dist > 0.05) return;
        rows.push({ symbol: sym, row_type: 'zone', label: expandZoneLabel(zone.label || '') });
      });
      (entry.signals || []).forEach(function (sig) {
        var dir = sig.direction === 'bullish' ? 'Bull' : 'Bear';
        rows.push({ symbol: sym, row_type: 'signal', label: dir + ' ' + titleCase(sig.type) });
      });
      (entry.transitions || []).forEach(function (t) {
        rows.push({
          symbol: sym, row_type: 'transition',
          label: (t.label || titleCase(t.indicator)) + ': ' + prettyStateToken(t.from) + ' → ' + prettyStateToken(t.to),
          ts: t.ts,
        });
      });
      if (entry.liquidation) {
        var liq = entry.liquidation;
        var dominant = liq.dominant === 'short' ? 'short' : 'long';
        var dominantUsd = dominant === 'long' ? liq.m15_long_usd : liq.m15_short_usd;
        rows.push({ symbol: sym, row_type: 'liquidation', label: fmtLiqUsd(dominantUsd) + ' ' + dominant + 's liquidated' });
      }
    });
    (data.positions || []).forEach(function (p) {
      var dir = p.direction === 'short' ? 'SHORT' : 'LONG';
      var entryTxt = (p.entry_price != null)
        ? ' from ' + ((typeof window !== 'undefined' && window.formatPrice)
            ? window.formatPrice(p.entry_price, p.symbol) : p.entry_price)
        : '';
      rows.push({ symbol: p.symbol, row_type: 'position', label: dir + entryTxt });
    });
    if (data.stream_event && data.stream_event.title) {
      rows.push({ symbol: 'MACRO', row_type: 'event', label: data.stream_event.title });
    }
    return rows;
  }

  function activityBySymbol() {
    var m = {};
    _entries.forEach(function (e) {
      if (e.symbol) m[e.symbol] = (m[e.symbol] || 0) + 1;
    });
    return m;
  }

  // Pure: group already-flattened/filtered rows by symbol, in first-
  // appearance order (stable — no re-sort by name/count). MACRO (the
  // event-row placeholder symbol, see flatten() above) falls out of this
  // naturally as its own group since only 'event' rows carry it.
  // topTag picks the TAGS[] label of the highest-priority row_type present
  // (TYPE_PRIORITY above), not just the first row's type — verified by
  // tests/js/desk-stream.check.mjs with an out-of-priority-order fixture.
  function groupBySymbol(rows) {
    var order = [];
    var map = {};
    (rows || []).forEach(function (r) {
      var sym = (r && r.symbol) || '?';
      if (!map[sym]) { map[sym] = []; order.push(sym); }
      map[sym].push(r);
    });
    return order.map(function (sym) {
      var grpRows = map[sym];
      var bestP = Infinity, topTag = '';
      grpRows.forEach(function (r) {
        var p = Object.prototype.hasOwnProperty.call(TYPE_PRIORITY, r.row_type) ? TYPE_PRIORITY[r.row_type] : 99;
        if (p < bestP) { bestP = p; topTag = TAGS[r.row_type] || r.row_type; }
      });
      return { symbol: sym, rows: grpRows, topTag: topTag };
    });
  }

  function tabCounts() {
    var c = {};
    TAB_ORDER.forEach(function (k) { c[k] = 0; });
    _entries.forEach(function (r) {
      TAB_ORDER.forEach(function (k) { if (matchesTab(r, k)) c[k]++; });
    });
    return c;
  }

  function renderStreamView(rows) {
    if (!rows.length) return '<div class="dstream-empty">No stream activity</div>';
    return rows.map(function (e) {
      return '<div class="dstream-row" data-symbol="' + escapeHtml(e.symbol || '') + '" data-rt="' + escapeHtml(e.row_type) + '">' +
        '<span class="dstream-row-sym">' + escapeHtml(e.symbol || '—') + '</span>' +
        '<span class="dstream-row-tag">' + escapeHtml(TAGS[e.row_type] || e.row_type) + '</span>' +
        (e.ts ? '<span class="dstream-row-time">' + escapeHtml(relTime(e.ts)) + '</span>' : '') +
        '<div class="dstream-row-msg">' + escapeHtml(e.label || '') + '</div></div>';
    }).join('');
  }

  function renderAssetView(rows) {
    var groups = groupBySymbol(rows);
    if (!groups.length) return '<div class="dstream-empty">No stream activity</div>';
    return groups.map(function (g) {
      var isMacro = g.symbol === 'MACRO';
      var expanded = !!_expanded[g.symbol];
      return '<div class="dstream-group' + (expanded ? ' open' : '') + '" data-sym="' + escapeHtml(g.symbol) + '">' +
        '<div class="dstream-group-head" data-toggle="' + escapeHtml(g.symbol) + '">' +
          '<span class="dstream-group-chev">' + (expanded ? '▾' : '▸') + '</span>' +
          '<span class="dstream-group-sym' + (isMacro ? ' nolink' : '') + '">' + escapeHtml(g.symbol) + '</span>' +
          '<span class="dstream-group-count">' + g.rows.length + '</span>' +
          (g.topTag ? '<span class="dstream-group-tag">' + escapeHtml(g.topTag) + '</span>' : '') +
        '</div>' +
        '<div class="dstream-group-rows">' + g.rows.map(function (e) {
          return '<div class="dstream-subrow" data-symbol="' + escapeHtml(e.symbol || '') + '" data-rt="' + escapeHtml(e.row_type) + '">' +
            '<span class="dstream-row-tag">' + escapeHtml(TAGS[e.row_type] || e.row_type) + '</span>' +
            (e.ts ? '<span class="dstream-row-time">' + escapeHtml(relTime(e.ts)) + '</span>' : '') +
            '<div class="dstream-row-msg">' + escapeHtml(e.label || '') + '</div></div>';
        }).join('') + '</div>' +
      '</div>';
    }).join('');
  }

  function draw() {
    var mount = document.getElementById('desk-stream');
    if (!mount) return;
    var rows = _entries.filter(function (r) { return matchesTab(r, _tab); });
    var counts = tabCounts();

    var tabsHtml = '<div class="dstream-tabs">' + TAB_ORDER.map(function (k) {
      return '<button class="dstream-tab' + (k === _tab ? ' on' : '') + '" data-tab="' + k + '">' +
        escapeHtml(TAB_LABELS[k]) + ' <span class="dstream-tab-count">' + counts[k] + '</span></button>';
    }).join('') + '</div>';

    var viewHtml = '<div class="dstream-viewtoggle">' +
      '<button class="dstream-viewbtn' + (_view === 'stream' ? ' on' : '') + '" data-view="stream">Stream</button>' +
      '<button class="dstream-viewbtn' + (_view === 'asset' ? ' on' : '') + '" data-view="asset">By asset</button>' +
      '</div>';

    var bodyHtml = '<div class="dstream-body">' + (_view === 'asset' ? renderAssetView(rows) : renderStreamView(rows)) + '</div>';

    mount.innerHTML = tabsHtml + viewHtml + bodyHtml;

    mount.querySelectorAll('.dstream-tab').forEach(function (btn) {
      btn.onclick = function () {
        _tab = btn.dataset.tab;
        saveTab(_tab);
        draw();
      };
    });

    mount.querySelectorAll('.dstream-viewbtn').forEach(function (btn) {
      btn.onclick = function () {
        _view = btn.dataset.view;
        draw();
      };
    });

    if (_view === 'asset') {
      mount.querySelectorAll('.dstream-group-head[data-toggle]').forEach(function (h) {
        var sym = h.dataset.toggle;
        h.onclick = function (ev) {
          // Clicking the symbol text selects the asset (unless the MACRO
          // pseudo-group — it carries only 'event' rows, which never select
          // an asset — see flatten()'s MACRO placeholder comment above).
          // Clicking elsewhere on the header toggles expand/collapse.
          if (ev.target.closest('.dstream-group-sym') && sym !== 'MACRO') {
            if (window.DeskApp) window.DeskApp.selectAsset(sym, 'stream');
            return;
          }
          _expanded[sym] = !_expanded[sym];
          draw();
        };
      });
      mount.querySelectorAll('.dstream-subrow').forEach(function (row) {
        row.onclick = function () {
          // 'event' rows carry the MACRO placeholder symbol — never select.
          if (row.dataset.rt === 'event') return;
          var sym = row.dataset.symbol;
          if (!sym) return;
          if (window.DeskApp) window.DeskApp.selectAsset(sym, 'stream');
        };
      });
    } else {
      mount.querySelectorAll('.dstream-row').forEach(function (row) {
        row.onclick = function () {
          // 'event' rows carry the MACRO placeholder symbol (no real asset —
          // see carrac/scanner/routes.py get_imminent_event / scenarios.js
          // comment "by-asset view and jumpChart both special-case row_type
          // 'event'"), so they never select an asset.
          if (row.dataset.rt === 'event') return;
          var sym = row.dataset.symbol;
          if (!sym) return;
          // Single drawer-open event lives in desk.js's selectAsset (desk_drawer_open) —
          // capturing here too would double-count opens sourced from the stream.
          if (window.DeskApp) window.DeskApp.selectAsset(sym, 'stream');
        };
      });
    }
  }

  function refresh() {
    fetch('/api/stream').then(function (r) { return r.json(); }).then(function (d) {
      _entries = flatten(d);
      draw();
      if (window.DeskApp) window.DeskApp.redraw();
    }).catch(function (e) {
      if (window.carracLog) window.carracLog('desk', 'stream refresh failed', String(e));
      /* stream is auxiliary — grid keeps working */
    });
  }

  if (typeof window !== 'undefined') window.DeskStream = { refresh: refresh, activityBySymbol: activityBySymbol };
  if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', function () {
      if (document.body.dataset.context !== 'desk') return;
      refresh();
      setInterval(function () { if (!document.hidden) refresh(); }, 60000);
    });
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      flatten: flatten, refresh: refresh, activityBySymbol: activityBySymbol,
      groupBySymbol: groupBySymbol, matchesTab: matchesTab,
    };
  }
})();
