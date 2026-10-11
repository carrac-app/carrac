// static/js/desk-grid.js — Trader's Desk grid. Column registry = the ONE place
// to add/change a column (label, tooltip, format, sort). Conditions-only:
// no scores, counts, or verdict words.
//
// A column picker exposes the FULL screener column set (see createRow() and
// getSortValue() in screener-table.js + the templates/index.html header
// titles) as opt-in entries, grouped Core / Indicators / Extended. The 9
// default-on columns are the ones listed first.
//
// The chg/ribbon/changed/structure columns reuse the dashboard table's own
// createRow() markup and classes verbatim (via the guarded window.* helpers
// price-formatting.js provides on desk.html). Rows are grouped into
// watchlist-first collapsible sections mirroring renderFilteredTable()'s
// 'All' tab, with a delegated star toggle and a quick filter.
(function () {
  'use strict';
  var RIBBON = { bull: 'Above', bear: 'Below', neutral: 'Inside' };

  // price-formatting.js's formatStructure()/formatCPRLevel() and
  // signal-formatting.js's formatSignals() all call a bare global
  // Pages load shared-utils.js first; plain Node (the check script) requires it.
  var escapeHtml = typeof globalThis.escapeHtml === 'function' ? globalThis.escapeHtml : require('./shared-utils.js').escapeHtml;

  function px(a) {
    var p = a.latest_price;
    if (p == null) return '--';
    return (typeof window !== 'undefined' && window.formatPrice) ? window.formatPrice(p, a.symbol) : String(p);
  }

  // Absolute 24h change value — mirrors _assetChangeValue() in
  // screener-table.js (not loaded on desk.html), used as the fallback when
  // window._assetChangeValue isn't present; reused verbatim if it is.
  function _localChangeValue(a) {
    var rt = a && a.realtime;
    if (!rt) return null;
    if (rt.change_value !== null && rt.change_value !== undefined && !isNaN(rt.change_value)) return rt.change_value;
    var pct = rt.change_pct;
    var price = (rt.price !== undefined && rt.price !== null) ? rt.price : a.latest_price;
    if (pct === null || pct === undefined || isNaN(pct) || !price) return null;
    var denom = 1 + pct / 100;
    if (!denom) return null;
    return price - price / denom;
  }
  function _changeValue(a) {
    return (typeof window !== 'undefined' && window._assetChangeValue) ? window._assetChangeValue(a) : _localChangeValue(a);
  }

  // a.consensio['1D'|'3D'|'1W'].signal, null-safe.
  function _consensioSignal(a, tf) {
    var c = a.consensio && a.consensio[tf];
    return (c && typeof c.signal === 'number') ? c.signal : null;
  }

  function _ext(a) { return (a && a.cockpit_extended) || {}; }

  var CPR_ORDER = { 'Below S3': 1, 'S3-S2': 2, 'S2-S1': 3, 'S1-BC': 4, 'BC-CP': 5,
    'CP-TC': 6, 'TC-R1': 7, 'R1-R2': 8, 'R2-R3': 9, 'Above R3': 10 };

  var COLUMNS = [
    // ── Core (default-ON, unchanged from pre-picker desk-grid) ──────────
    { key: 'symbol', label: 'Symbol', title: 'Asset ticker', group: 'core',
      format: function (a) {
        return '<a class="desk-sym-link" href="/chart/' + a.symbol + '/1D">' +
               a.symbol + '</a>' + (a.data_stale
          ? ' <span class="stale-chip" title="Price feed unavailable — showing last stored data">⚠</span>' : '');
      },
      sortVal: function (a) { return a.symbol; } },
    { key: 'price', label: 'Price', title: 'Last price', num: true, group: 'core',
      format: px, sortVal: function (a) { return a.latest_price || 0; } },
    // Chg% — mirrors createRow() in screener-table.js: a
    // <span class="price-change price-change-up|down"> wrapping a signed,
    // locale-formatted percentage. cellClass 'change-col' is the same <td>
    // class createRow() uses; main.css (already loaded on desk.html) styles
    // it with right alignment and a right-border divider.
    { key: 'chg', label: 'Chg%', title: 'Intraday change', num: true, group: 'core',
      cellClass: function () { return 'change-col'; },
      format: function (a) {
        var c = a.realtime && a.realtime.change_pct;
        if (c === null || c === undefined) return '--';
        var cls = c >= 0 ? 'price-change-up' : 'price-change-down';
        var sign = c >= 0 ? '+' : '';
        var n = (typeof window !== 'undefined' && window._fmtN) ? window._fmtN(c, 2) : c.toFixed(2);
        return '<span class="price-change ' + cls + '">' + sign + n + '%</span>';
      },
      sortVal: function (a) { return (a.realtime && a.realtime.change_pct) || 0; } },
    // Ribbon — mirrors price-formatting.js formatEmaRibbon() (createRow()
    // in screener-table.js calls it; the cell is a dot + label span pair,
    // cellClass 'ema-ribbon ema-ribbon-col'). Local RIBBON
    // map is the Node-safe fallback only (no window.formatEmaRibbon there).
    { key: 'ribbon', label: 'Ribbon', title: 'EMA 30/60 trend — price Above / Below / Inside the ribbon', group: 'core',
      cellClass: function () { return 'ema-ribbon ema-ribbon-col'; },
      format: function (a) {
        if (typeof window !== 'undefined' && window.formatEmaRibbon) {
          return window.formatEmaRibbon(a.ema_ribbon).html;
        }
        return RIBBON[a.ema_ribbon && a.ema_ribbon.trend] || '--';
      },
      sortVal: function (a) { return (a.ema_ribbon && a.ema_ribbon.trend) || ''; } },
    // Changed — mirrors createRow() in screener-table.js: prefers
    // "<N>d" (days since flip) over the bare EMA-ribbon-flip date, with a
    // titled tooltip, and swaps in the 'fresh_flip' gradient-highlight +
    // data-tooltip branch when trend_analysis.patterns includes 'fresh_flip'.
    // Deliberately reproduces the dashboard's literal display verbatim — INCLUDING
    // showing "999d" for "never flipped" (999 is ema_ribbon.py's actual
    // sentinel; createRow() has no special-case for it).
    // cellClass 'changed-col ema-ribbon-date' is createRow()'s own <td> class
    // in screener-table.js; neither carries alignment in main.css (Changed is only
    // centered there via a positional nth-child(13) rule tied to the dashboard's fixed
    // column layout, which can't transfer to the Desk's reorderable
    // columns) — desk.css adds an explicit non-positional center rule instead.
    { key: 'changed', label: 'Changed', title: 'Days since the EMA Ribbon trend last flipped', group: 'core',
      cellClass: function () { return 'changed-col ema-ribbon-date'; },
      format: function (a) {
        var ribbonDate = (typeof window !== 'undefined' && window.formatEmaRibbon)
          ? window.formatEmaRibbon(a.ema_ribbon).date
          : ((a.ema_ribbon && a.ema_ribbon.last_change) || '--');
        var daysSinceFlip = (a.ema_ribbon && typeof a.ema_ribbon.days_since_flip === 'number')
          ? a.ema_ribbon.days_since_flip : null;
        var display = (daysSinceFlip !== null) ? (daysSinceFlip + 'd') : ribbonDate;
        var title = 'EMA Ribbon last flipped ' + ribbonDate +
          (daysSinceFlip !== null ? ' (' + daysSinceFlip + ' days ago)' : '');
        var patterns = (a.trend_analysis && a.trend_analysis.patterns) || [];
        if (patterns.indexOf('fresh_flip') !== -1 && a.ema_ribbon) {
          var trend = a.ema_ribbon.trend || 'neutral';
          var gradientClass = trend === 'bull' ? 'highlight-flip-to-bull'
            : trend === 'bear' ? 'highlight-flip-to-bear' : 'highlight-flip-to-neutral';
          return '<span class="signal-highlight ' + gradientClass + '" data-tooltip="Fresh EMA Ribbon Flip" title="' +
            escapeHtml(title) + '">' + display + '</span>';
        }
        return '<span title="' + escapeHtml(title) + '">' + display + '</span>';
      },
      sortVal: function (a) {
        var d = a.ema_ribbon && a.ema_ribbon.days_since_flip;
        return (typeof d === 'number') ? d : 1e9;
      } },
    // Structure — mirrors price-formatting.js formatStructure(), same as
    // createRow() in screener-table.js. cellClass 'structure-col
    // structure-cell' is createRow()'s own <td> class pair — structure-col
    // centers the text, structure-cell adds a left border divider (both in
    // main.css).
    { key: 'structure', label: 'Structure', title: 'Daily market structure (HH/HL = Uptrend, LH/LL = Downtrend)', group: 'core',
      cellClass: function () { return 'structure-col structure-cell'; },
      format: function (a) {
        if (typeof window !== 'undefined' && window.formatStructure) return window.formatStructure(a.structure);
        var s = a.structure || {};
        return (s.last_high_type && s.last_low_type)
          ? s.last_high_type + '·' + s.last_low_type : '--';
      },
      sortVal: function (a) { return ((a.structure || {}).structure_trend) || ''; } },
    { key: 'squeeze', label: 'BB Width', title: 'Bollinger Band width vs 60-day average', num: true, group: 'core',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatBB) ? window.formatBB(a.bb) : ((a.bb && Math.round(a.bb.squeeze_pct) + '%') || '--');
      },
      sortVal: function (a) { return (a.bb && a.bb.squeeze_pct) || 0; } },
    { key: 'cpr', label: 'CPR', title: 'Position vs the Monthly Central Pivot Range', group: 'core',
      format: function (a) { return (typeof window !== 'undefined' && window.formatCPRLevel) ? window.formatCPRLevel(a.cpr) : ((a.cpr && a.cpr.price_level) || '--'); },
      sortVal: function (a) { return CPR_ORDER[a.cpr && a.cpr.price_level] || 0; } },
    { key: 'activity', label: 'Activity', title: 'Stream events for this asset today', num: true, group: 'core',
      format: function (a, opts) {
        var n = opts && opts.activityBySymbol && opts.activityBySymbol[a.symbol];
        return n ? String(n) : '';
      },
      sortVal: function (a, opts) {
        return (opts && opts.activityBySymbol && opts.activityBySymbol[a.symbol]) || 0;
      } },

    // ── Indicators (default-OFF; legacy screener columns) ───────────────
    { key: 'name', label: 'Name', title: 'Asset name', group: 'indicators',
      format: function (a) { return a.name || '--'; },
      sortVal: function (a) { return a.name || ''; } },
    { key: 'chg_dollar', label: 'Chg $', title: 'Absolute price change (today)', num: true, group: 'indicators',
      format: function (a) {
        var v = _changeValue(a);
        if (v === null || v === undefined || isNaN(v)) return '--';
        return (typeof window !== 'undefined' && window.formatChangeValue) ? window.formatChangeValue(v, a.symbol, a.type) : '--';
      },
      sortVal: function (a) {
        var v = _changeValue(a);
        return (v === null || v === undefined || isNaN(v)) ? 0 : v;
      } },
    // Consensio's "6/9" text is the legacy alignment display, not a scanner score.
    { key: 'consensio', label: 'Consensio', title: 'Consensio MA alignment — 0-9 positions across 1D/3D/1W (9/9 = full bull, 0/9 = full bear)', group: 'indicators',
      format: function (a) {
        return (a.positions != null && a.max_positions != null) ? (a.positions + '/' + a.max_positions) : '--';
      },
      sortVal: function (a) { return a.positions || 0; } },
    { key: 'signal_1d', label: '1D', title: 'Per-timeframe Consensio signal (-3 to +3)', num: true, group: 'indicators',
      format: function (a) {
        var s = _consensioSignal(a, '1D');
        return (s !== null && typeof window !== 'undefined' && window.formatSignal) ? window.formatSignal(s) : '--';
      },
      sortVal: function (a) { return _consensioSignal(a, '1D') || 0; } },
    { key: 'signal_3d', label: '3D', title: 'Per-timeframe Consensio signal (-3 to +3)', num: true, group: 'indicators',
      format: function (a) {
        var s = _consensioSignal(a, '3D');
        return (s !== null && typeof window !== 'undefined' && window.formatSignal) ? window.formatSignal(s) : '--';
      },
      sortVal: function (a) { return _consensioSignal(a, '3D') || 0; } },
    { key: 'signal_1w', label: '1W', title: 'Per-timeframe Consensio signal (-3 to +3)', num: true, group: 'indicators',
      format: function (a) {
        var s = _consensioSignal(a, '1W');
        return (s !== null && typeof window !== 'undefined' && window.formatSignal) ? window.formatSignal(s) : '--';
      },
      sortVal: function (a) { return _consensioSignal(a, '1W') || 0; } },
    { key: 'rsi', label: 'W-RSI', title: 'Weekly RSI (14)', num: true, group: 'indicators',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatRsi) ? window.formatRsi(a.weekly_rsi) : '--';
      },
      sortVal: function (a) { return a.weekly_rsi || 0; } },
    { key: 'macd', label: 'MACD', title: 'MACD histogram direction', group: 'indicators',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatMacd) ? window.formatMacd(a.macd) : '--';
      },
      sortVal: function (a) { return (a.macd && a.macd.histogram) || 0; } },
    { key: 'adx', label: 'ADX', title: 'ADX trend strength', num: true, group: 'indicators',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatAdx) ? window.formatAdx(a.adx) : '--';
      },
      sortVal: function (a) { return (a.adx && a.adx.adx) || 0; } },
    { key: 'atr', label: 'ATR%', title: 'ATR as percentage of price', num: true, group: 'indicators',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatAtrPct) ? window.formatAtrPct(a.adx) : '--';
      },
      sortVal: function (a) { return (a.adx && a.adx.atr_pct) || 0; } },
    { key: 'vol', label: 'Vol', title: 'Volume vs 20-bar average', num: true, group: 'indicators',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatRelVolume) ? window.formatRelVolume(a.volume_metrics) : '--';
      },
      sortVal: function (a) { return (a.volume_metrics && a.volume_metrics.ratio) || 0; } },
    { key: 'signals', label: 'Signals', title: 'Signal badges — hover a badge for its meaning', group: 'indicators',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatSignals) ? window.formatSignals(a) : '--';
      },
      sortVal: function (a) { return (typeof window !== 'undefined' && window.getSignalCount) ? window.getSignalCount(a) : 0; } },

    // ── Extended (default-OFF; cockpit_extended indicator snapshots) ────
    { key: 'ichi', label: 'Ichi', title: 'Ichimoku cloud position + TK cross', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatIchimoku) ? window.formatIchimoku(_ext(a).ichimoku) : '--';
      },
      sortVal: function (a) {
        var pos = (_ext(a).ichimoku && _ext(a).ichimoku.cloud_position) || '';
        return pos.indexOf('above') !== -1 ? 2 : pos.indexOf('below') !== -1 ? 0 : 1;
      } },
    { key: 'strend', label: 'STrend', title: 'SuperTrend direction (* = flip)', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatSuperTrend) ? window.formatSuperTrend(_ext(a).supertrend) : '--';
      },
      sortVal: function (a) { return (_ext(a).supertrend && _ext(a).supertrend.direction === 'long') ? 1 : 0; } },
    { key: 'psar', label: 'PSAR', title: 'Parabolic SAR direction (* = flip)', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatPsar) ? window.formatPsar(_ext(a).psar) : '--';
      },
      sortVal: function (a) { return (_ext(a).psar && _ext(a).psar.direction === 'long') ? 1 : 0; } },
    { key: 'emax', label: 'EMA-X', title: 'EMA 9/21 cross state + bars', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatEmaCross) ? window.formatEmaCross(_ext(a).ema_cross) : '--';
      },
      sortVal: function (a) { return (_ext(a).ema_cross && _ext(a).ema_cross.bars_since_cross) || 0; } },
    { key: 'allig', label: 'Allig', title: 'Bill Williams Alligator state', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatAlligator) ? window.formatAlligator(_ext(a).alligator) : '--';
      },
      sortVal: function (a) { return (_ext(a).alligator && _ext(a).alligator.state) || ''; } },
    { key: 'strsi', label: 'StRSI', title: 'Stochastic RSI K/D', num: true, group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatStochRsi) ? window.formatStochRsi(_ext(a).stoch_rsi) : '--';
      },
      sortVal: function (a) { return (_ext(a).stoch_rsi && _ext(a).stoch_rsi.k) || 0; } },
    { key: 'sqz', label: 'Sqz', title: 'TTM Squeeze active + momentum', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatTtmSqueeze) ? window.formatTtmSqueeze(_ext(a).ttm_squeeze) : '--';
      },
      sortVal: function (a) { var t = _ext(a).ttm_squeeze; return (t && t.active) ? (t.bars || 0) : 0; } },
    { key: 'donch', label: 'Donch', title: 'Donchian Channel breakout', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatDonchian) ? window.formatDonchian(_ext(a).donchian) : '--';
      },
      sortVal: function (a) { return (_ext(a).donchian && _ext(a).donchian.breakout) || ''; } },
    { key: 'nr7', label: 'NR7', title: 'Narrowest Range 7 bars', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatNr7) ? window.formatNr7(_ext(a).nr7) : '--';
      },
      sortVal: function (a) { return (_ext(a).nr7 && _ext(a).nr7.active) ? 1 : 0; } },
    { key: 'bbwpct', label: 'BBW%', title: 'Bollinger Width percentile rank', num: true, group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatBbwPct) ? window.formatBbwPct(_ext(a).bbw_pct) : '--';
      },
      sortVal: function (a) { return (_ext(a).bbw_pct && _ext(a).bbw_pct.percentile) || 0; } },
    { key: 'atrpctrank', label: 'ATR%R', title: 'ATR percentile rank', num: true, group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatAtrPctRank) ? window.formatAtrPctRank(_ext(a).atr_pct) : '--';
      },
      sortVal: function (a) { return (_ext(a).atr_pct && _ext(a).atr_pct.percentile) || 0; } },
    { key: 'obv', label: 'OBV', title: 'On-Balance Volume trend + divergence', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatObv) ? window.formatObv(_ext(a).obv) : '--';
      },
      sortVal: function (a) {
        var t = _ext(a).obv && _ext(a).obv.trend;
        return t === 'rising' ? 1 : t === 'falling' ? -1 : 0;
      } },
    { key: 'fvg', label: 'FVG', title: 'Fair Value Gaps (bullish/bearish)', num: true, group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatFvg) ? window.formatFvg(_ext(a).fvg) : '--';
      },
      sortVal: function (a) {
        var f = _ext(a).fvg || {};
        return (f.bullish_count || 0) + (f.bearish_count || 0);
      } },
    { key: 'vp', label: 'VP', title: 'Volume Profile POC level', num: true, group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatVpLevels) ? window.formatVpLevels(a.vp_levels) : '--';
      },
      sortVal: function (a) { return (a.vp_levels && a.vp_levels.poc) || 0; } },
    { key: 'rsidiv', label: 'RSI-D', title: 'RSI Divergence (daily/weekly)', group: 'extended',
      format: function (a) {
        return (typeof window !== 'undefined' && window.formatRsiDiv) ? window.formatRsiDiv(a.rsi_divergence) : '--';
      },
      sortVal: function (a) {
        var d = a.rsi_divergence || {};
        var day = typeof d.daily === 'object' ? (d.daily || {}).type : d.daily;
        var wk = typeof d.weekly === 'object' ? (d.weekly || {}).type : d.weekly;
        return ((day && day !== 'none') ? 1 : 0) + ((wk && wk !== 'none') ? 1 : 0);
      } },
  ];

  var DEFAULT_KEYS = ['symbol', 'price', 'chg', 'ribbon', 'changed', 'structure', 'squeeze', 'cpr', 'activity'];
  var STORAGE_KEY = 'desk_columns_v1';
  var GROUP_LABELS = { core: 'Core', indicators: 'Indicators', extended: 'Extended' };

  // ── Watchlist-first sections ──────────────────────────
  // Type buckets + labels + display order mirror sectionLabels/sectionOrder in
  // static/js/score-formatting.js — that file is NOT loaded on
  // desk.html (it also carries scanner-score vocabulary out of scope for the
  // Desk's conditions-only surface), so the small type->label/order map is
  // duplicated here rather than pulling the whole file in.
  var SECTION_TYPE_ORDER = ['crypto_metric', 'crypto', 'index', 'forex', 'stock', 'etf', 'commodity'];
  var SECTION_TYPE_LABELS = {
    crypto: 'Cryptocurrencies', crypto_metric: 'Crypto Metrics', stock: 'Stocks',
    etf: 'ETFs', index: 'Indices', forex: 'Forex', commodity: 'Commodities',
  };
  var COLLAPSE_STORAGE_KEY = 'desk_collapsedSections';

  // Substring match on symbol OR name — mirrors _passesQuickFilter() in
  // screener-table.js; `q` is expected pre-uppercased by the caller.
  function _matchesQuickFilter(a, q) {
    if (!q) return true;
    var sym = String((a && a.symbol) || '').toUpperCase();
    if (sym.indexOf(q) !== -1) return true;
    var name = (a && a.name) ? String(a.name).toUpperCase() : '';
    return name.indexOf(q) !== -1;
  }

  // Pure grouping core (Node-testable — exported below): watchlist section
  // FIRST, then the same type-based sections the dashboard uses, in its order.
  // Mirrors renderFilteredTable()'s 'All'-tab layout in screener-table.js:
  // a watchlisted asset appears BOTH in the pinned Watchlist section (in the
  // user's watchlist order, unsorted — the dashboard never re-sorts that pinned
  // block by the active column either) AND again in its own type section
  // below. That duplication is the dashboard's intentional pinned-favorites
  // behavior (quick access + still browsable in natural order) — kept here
  // deliberately rather than deduping. Empty sections (post-filter) are
  // omitted.
  function buildSections(assets, opts) {
    opts = opts || {};
    var q = (opts.quickFilter || '').toUpperCase();
    var wlOrder = opts.watchlistSymbols || [];
    var bySymbol = {};
    (assets || []).forEach(function (a) { if (a && a.symbol) bySymbol[a.symbol] = a; });

    var sections = [];
    var wlRows = wlOrder
      .map(function (s) { return bySymbol[s]; })
      .filter(function (a) { return a && _matchesQuickFilter(a, q); });
    if (wlRows.length) {
      sections.push({ key: 'watchlist', label: 'Watchlist', star: true, count: wlRows.length, rows: wlRows });
    }

    var byType = {};
    (assets || []).forEach(function (a) {
      if (!a || !_matchesQuickFilter(a, q)) return;
      var t = a.type || 'stock';
      if (!byType[t]) byType[t] = [];
      byType[t].push(a);
    });
    SECTION_TYPE_ORDER.forEach(function (t) {
      if (byType[t] && byType[t].length) {
        sections.push({ key: t, label: SECTION_TYPE_LABELS[t] || t, star: false, count: byType[t].length, rows: byType[t] });
      }
    });
    // Defensive: an asset type outside the known 7 (none exist today) still
    // gets a section instead of silently vanishing — the dashboard's
    // equivalent loop in renderFilteredTable() creates but never renders such a group.
    Object.keys(byType).sort().forEach(function (t) {
      if (SECTION_TYPE_ORDER.indexOf(t) !== -1 || !byType[t].length) return;
      sections.push({ key: t, label: SECTION_TYPE_LABELS[t] || t, star: false, count: byType[t].length, rows: byType[t] });
    });
    return sections;
  }

  function _loadCollapsedSections() {
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        var raw = window.localStorage.getItem(COLLAPSE_STORAGE_KEY);
        if (raw) {
          var obj = JSON.parse(raw);
          if (obj && typeof obj === 'object') return obj;
        }
      }
    } catch (e) { /* corrupt/blocked storage — fall through */ }
    return {};
  }
  function _saveCollapsedSections(obj) {
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        window.localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(obj));
      }
    } catch (e) { /* storage full/blocked — collapse still works this session */ }
  }

  var _sortKey = null, _sortDir = -1;
  var _enabledKeys = null; // lazy: loaded from localStorage on first render()
  var _collapsedSections = null; // lazy: loaded from localStorage on first render()
  var _quickFilter = ''; // uppercased; session-only, mirrors the dashboard's panelQuickFilter (not persisted)
  var _lastAssets = [], _lastOpts = {};

  function _knownKeys() {
    var known = {};
    COLUMNS.forEach(function (c) { known[c.key] = true; });
    return known;
  }

  function loadEnabledKeys() {
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        var raw = window.localStorage.getItem(STORAGE_KEY);
        if (raw) {
          var arr = JSON.parse(raw);
          if (Array.isArray(arr) && arr.length) {
            var known = _knownKeys();
            var filtered = arr.filter(function (k) { return known[k]; });
            if (filtered.length) return filtered;
          }
        }
      }
    } catch (e) { /* corrupt/blocked storage — fall through to defaults */ }
    return DEFAULT_KEYS.slice();
  }

  function saveEnabledKeys(keys) {
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(keys));
      }
    } catch (e) { /* storage full/blocked — picker still works this session */ }
  }

  function activeColumns() {
    if (_enabledKeys === null) _enabledKeys = loadEnabledKeys();
    var set = {};
    _enabledKeys.forEach(function (k) { set[k] = true; });
    return COLUMNS.filter(function (c) { return set[c.key]; });
  }

  function toggleColumn(key, on) {
    if (_enabledKeys === null) _enabledKeys = loadEnabledKeys();
    var set = {};
    _enabledKeys.forEach(function (k) { set[k] = true; });
    if (on) set[key] = true; else delete set[key];
    // Keep registry order so column order is stable regardless of toggle order.
    _enabledKeys = COLUMNS.filter(function (c) { return set[c.key]; }).map(function (c) { return c.key; });
    saveEnabledKeys(_enabledKeys);
    render(_lastAssets, _lastOpts);
  }

  function resetColumns() {
    _enabledKeys = DEFAULT_KEYS.slice();
    saveEnabledKeys(_enabledKeys);
    render(_lastAssets, _lastOpts);
  }

  function syncPickerState(panel) {
    if (_enabledKeys === null) _enabledKeys = loadEnabledKeys();
    var set = {};
    _enabledKeys.forEach(function (k) { set[k] = true; });
    panel.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
      cb.checked = !!set[cb.dataset.colKey];
    });
  }

  // Built once; lives as a sibling of #desk-grid inside #desk-grid-wrap so
  // render()'s mount.innerHTML rewrite never wipes it. The toolbar holds
  // the quick-filter input (mirrors the dashboard's #panel-quick-filter) and
  // the Columns picker button/panel.
  function ensureToolbar() {
    var wrap = document.getElementById('desk-grid-wrap');
    var mount = document.getElementById('desk-grid');
    if (!wrap || !mount) return;
    var existing = document.getElementById('desk-col-picker-panel');
    if (existing) { syncPickerState(existing); return; }

    var holder = document.createElement('div');
    holder.className = 'desk-col-picker';

    // Quick filter — substring match on symbol/name; shares the dashboard's
    // panel-quick-filter styling, 150ms debounce and Esc-to-clear behavior.
    var filterInput = document.createElement('input');
    filterInput.type = 'search';
    filterInput.id = 'desk-quick-filter';
    filterInput.className = 'panel-quick-filter';
    filterInput.placeholder = 'Filter…';
    filterInput.autocomplete = 'off';
    filterInput.spellcheck = false;
    filterInput.setAttribute('aria-label', 'Filter assets by symbol or name');
    var _qfTimer = null;
    filterInput.addEventListener('input', function () {
      if (_qfTimer) clearTimeout(_qfTimer);
      var v = filterInput.value;
      _qfTimer = setTimeout(function () {
        _quickFilter = (v || '').trim().toUpperCase();
        render(_lastAssets, _lastOpts);
      }, 150);
    });
    filterInput.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        filterInput.value = '';
        _quickFilter = '';
        render(_lastAssets, _lastOpts);
        filterInput.blur();
      }
    });

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'desk-col-picker-btn';
    btn.textContent = 'Columns';

    var panel = document.createElement('div');
    panel.id = 'desk-col-picker-panel';
    panel.className = 'desk-col-picker-panel';
    panel.hidden = true;

    ['core', 'indicators', 'extended'].forEach(function (gk) {
      var cols = COLUMNS.filter(function (c) { return c.group === gk; });
      if (!cols.length) return;
      var section = document.createElement('div');
      section.className = 'desk-col-group';
      var h = document.createElement('div');
      h.className = 'desk-col-group-title';
      h.textContent = GROUP_LABELS[gk] || gk;
      section.appendChild(h);
      cols.forEach(function (c) {
        var label = document.createElement('label');
        label.className = 'desk-col-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.dataset.colKey = c.key;
        cb.addEventListener('change', function () { toggleColumn(c.key, cb.checked); });
        var span = document.createElement('span');
        span.textContent = c.label;
        span.title = c.title;
        label.appendChild(cb);
        label.appendChild(span);
        section.appendChild(label);
      });
      panel.appendChild(section);
    });

    var resetLink = document.createElement('a');
    resetLink.href = '#';
    resetLink.className = 'desk-col-reset';
    resetLink.textContent = 'Reset to defaults';
    resetLink.addEventListener('click', function (e) {
      e.preventDefault();
      resetColumns();
    });
    panel.appendChild(resetLink);

    btn.addEventListener('click', function () { panel.hidden = !panel.hidden; });

    holder.appendChild(filterInput);
    holder.appendChild(btn);
    holder.appendChild(panel);
    wrap.insertBefore(holder, mount);
    syncPickerState(panel);
  }

  // Watchlist star — mirrors createRow()'s inline star span in
  // screener-table.js (starred = filled '&#9733;'/.star.active,
  // unstarred = outline '&#9734;'/.star). Appended after the symbol
  // column's own cell content (the symbol column's format() is untouched)
  // so it lands in the same cell the dashboard puts it in. Renders only once watchlist state has
  // loaded (opts.watchlistSet present) to avoid a flash of "all unstarred".
  function _starHtml(a, opts) {
    var wlSet = opts && opts.watchlistSet;
    if (!wlSet) return '';
    var starred = !!wlSet[String(a.symbol).toUpperCase()];
    var icon = starred ? '&#9733;' : '&#9734;';
    var cls = starred ? 'star active' : 'star';
    return ' <span class="' + cls + '" data-symbol="' + escapeHtml(a.symbol) + '" data-action="toggle-watchlist">' + icon + '</span>';
  }

  function _rowHtml(a, cols, opts, hidden) {
    var cells = cols.map(function (c) {
      var classes = [];
      if (c.num) classes.push('num');
      if (c.cellClass) {
        var cc = c.cellClass(a, opts);
        if (cc) classes.push(cc);
      }
      var content = c.format(a, opts);
      if (c.key === 'symbol') content += _starHtml(a, opts);
      return '<td' + (classes.length ? ' class="' + classes.join(' ') + '"' : '') + '>' + content + '</td>';
    }).join('');
    return '<tr data-symbol="' + a.symbol + '"' + (hidden ? ' class="hidden"' : '') + '>' + cells + '</tr>';
  }

  // Section header row — mirrors createSectionHeader() and the inline
  // watchlist header in screener-table.js: toggle-icon + label +
  // count, collapsible via data-action="toggle-section". colspan is dynamic
  // (cols.length) since the Desk's column set is user-configurable, unlike
  // the dashboard's fixed-column table.
  function _sectionHeaderHtml(sec, colCount, collapsed) {
    var label = (sec.star ? '★ ' : '') + sec.label;
    return '<tr class="section-header' + (collapsed ? ' collapsed' : '') + '" data-type="' + escapeHtml(sec.key) + '" data-action="toggle-section">' +
      '<td colspan="' + colCount + '"><span class="toggle-icon">▼</span>' + escapeHtml(label) +
      '<span class="count">(' + sec.count + ')</span></td></tr>';
  }

  // Single delegated listener (bound once — mount's innerHTML is rewritten
  // every render(), but the mount node itself is stable) for the two new
  // interactive row/section affordances. Mirrors the dashboard's delegated
  // click dispatch (the data-action switch on `document` and the row-click
  // handler bound once on #table-body) — the Desk has neither of those
  // generic systems, so this is the Desk-scoped equivalent.
  function _bindDelegatedActions(mount) {
    if (mount._deskActionsBound) return;
    mount._deskActionsBound = true;
    mount.addEventListener('click', function (event) {
      var starEl = event.target.closest && event.target.closest('[data-action="toggle-watchlist"]');
      if (starEl) {
        var sym = starEl.dataset.symbol;
        if (sym && _lastOpts.onWatchlistToggle) _lastOpts.onWatchlistToggle(sym);
        return;
      }
      var headerEl = event.target.closest && event.target.closest('[data-action="toggle-section"]');
      if (headerEl) {
        var key = headerEl.dataset.type;
        if (key) {
          if (_collapsedSections === null) _collapsedSections = _loadCollapsedSections();
          _collapsedSections[key] = !_collapsedSections[key];
          _saveCollapsedSections(_collapsedSections);
          render(_lastAssets, _lastOpts);
        }
      }
    });
  }

  function render(assets, opts) {
    opts = opts || {};
    _lastAssets = assets || [];
    _lastOpts = opts;
    var mount = document.getElementById('desk-grid');
    if (!mount) return;
    ensureToolbar();
    if (_collapsedSections === null) _collapsedSections = _loadCollapsedSections();

    var cols = activeColumns();
    var sortCol = _sortKey ? cols.filter(function (c) { return c.key === _sortKey; })[0] : null;
    function sortedCopy(rows) {
      if (!sortCol) return rows;
      var copy = rows.slice();
      copy.sort(function (x, y) {
        var a = sortCol.sortVal(x, opts), b = sortCol.sortVal(y, opts);
        return (a < b ? -1 : a > b ? 1 : 0) * _sortDir;
      });
      return copy;
    }

    // watchlistSet: O(1) star-state lookup, derived once per render from
    // the ordered array desk.js passes in (that order is also what
    // buildSections uses for the pinned Watchlist section).
    var watchlistSet = null;
    if (opts.watchlistSymbols) {
      watchlistSet = {};
      opts.watchlistSymbols.forEach(function (s) { watchlistSet[String(s).toUpperCase()] = true; });
    }
    var rowOpts = watchlistSet ? Object.assign({}, opts, { watchlistSet: watchlistSet }) : opts;

    var sections = buildSections(_lastAssets, { watchlistSymbols: opts.watchlistSymbols || [], quickFilter: _quickFilter });

    var bodyHtml;
    if (!sections.length) {
      bodyHtml = '<tr><td colspan="' + cols.length + '" class="desk-empty-row">No assets match your filter.</td></tr>';
    } else {
      bodyHtml = sections.map(function (sec) {
        var collapsed = !!_collapsedSections[sec.key];
        var rows = sec.key === 'watchlist' ? sec.rows : sortedCopy(sec.rows);
        return _sectionHeaderHtml(sec, cols.length, collapsed) +
          rows.map(function (a) { return _rowHtml(a, cols, rowOpts, collapsed); }).join('');
      }).join('');
    }

    var html = '<table class="desk-table"><thead><tr>' + cols.map(function (c) {
      return '<th data-key="' + c.key + '" title="' + c.title + '"' +
             (c.num ? ' class="num"' : '') + '>' + c.label + '</th>';
    }).join('') + '</tr></thead><tbody>' + bodyHtml + '</tbody></table>';
    mount.innerHTML = html;
    mount.querySelectorAll('th').forEach(function (th) {
      th.onclick = function () {
        var k = th.dataset.key;
        _sortDir = (_sortKey === k) ? -_sortDir : -1;
        _sortKey = k;
        render(_lastAssets, _lastOpts);
      };
    });
    if (opts.onRowClick) {
      // Only real data rows carry data-symbol (section headers don't), and
      // the star click must not also open the drawer — same guard as the
      // dashboard's row-click handler.
      mount.querySelectorAll('tbody tr[data-symbol]').forEach(function (tr) {
        tr.onclick = function (event) {
          if (event.target.closest && event.target.closest('[data-action="toggle-watchlist"]')) return;
          opts.onRowClick(tr.dataset.symbol);
        };
      });
    }
    mount.querySelectorAll('.desk-sym-link').forEach(function (link) {
      link.onclick = function (event) {
        event.stopPropagation();
        if (typeof window !== 'undefined' && window.posthog) {
          var sym = link.textContent;
          posthog.capture('desk_chart_out', { symbol: sym });
        }
      };
    });
    _bindDelegatedActions(mount);
  }

  var api = { COLUMNS: COLUMNS, DEFAULT_KEYS: DEFAULT_KEYS, render: render, buildSections: buildSections };
  if (typeof window !== 'undefined') window.DeskGrid = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
