/* ============================================================================
   CARRAC — RADAR  (Stream view)
   Ported from the Claude Design export; wired to live /api/stream row data.
   Exposes window.Radar = { render(section, rows, meta), teardown() }
   ============================================================================ */
(function () {
  "use strict";

  /* ---- localStorage keys ------------------------------------------------- */
  var LS = {
    lens:    "carrac_radar_lens",
    mode:    "carrac_radar_mode",
  };

  /* ---- icon set (verbatim from export) ------------------------------------ */
  var IC = {
    bull:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 6 19 16 5 16Z"/></svg>',
    bear:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 18 5 8 19 8Z"/></svg>',
    sig:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.5 13.5H11l-1 8.5L19.5 10H13Z"/></svg>',
    trn:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h14l-3.5-3.5M21 16H7l3.5 3.5"/></svg>',
    zone:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="0.6" fill="currentColor"/></svg>',
    pos:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M3 12.5h18"/></svg>',
    liq:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3c4 5 7 8.7 7 12a7 7 0 0 1-14 0c0-3.3 3-7 7-12Z"/></svg>',
    event:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>',
    all:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5 6 5-13L17 12h4"/></svg>',
    star:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="m12 2.5 2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 18.4 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9Z"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    chev:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>',
    caret:  '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 15 6 9h12Z"/></svg>',
    cross:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>',
    lock:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    spark:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/></svg>',
    pause:  '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
    play:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7Z"/></svg>',
    stream: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
    grid:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    empty:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    // Log-to-journal / Alert-at-level row actions
    log:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5V5a2 2 0 0 1 2-2h9l5 5v11.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/><path d="M14 3v5h5"/><path d="M8 13h8M8 17h5"/></svg>',
    bell:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
  };

  /* ---- module-level state ------------------------------------------------- */
  var _section  = null;   // live DOM element passed to render()
  var _rows     = [];     // last rows array from caller
  var _meta     = {};     // last meta from caller
  var _ticker   = null;   // our own setInterval handle

  // keyed row cache for the stream (flat table) view, so a
  // plain data refresh (scenarios.js calling render() every 15/30s) can diff
  // against what's already on screen instead of a full innerHTML rebuild.
  // `ready` is only true right after a full render painted a non-empty stream
  // table for the section/lens below; any other render shape (By-Asset,
  // upsell, empty state) invalidates it until the next such render.
  var _streamDiffState = { ready: false, section: null, lens: null, rowMap: {}, order: [] };

  /* ---- UI state (persisted) ---------------------------------------------- */
  function _ls(key, def) {
    try { var v = localStorage.getItem(key); return v !== null ? v : def; } catch (e) { return def; }
  }
  function _lsSet(key, val) { try { localStorage.setItem(key, String(val)); } catch (e) {} }

  var state = {
    lens:      _ls(LS.lens,    "all"),
    mode:      _ls(LS.mode,    "stream"),
    search:    "",
    dir:       "all",
    tf:        "all",
    squeeze:   "all",
    side:      "all",
    signif:    "all",
    paused:    false,
    collapsed: {},
  };

  function _saveState() {
    _lsSet(LS.lens,    state.lens);
    _lsSet(LS.mode,    state.mode);
  }

  /* ---- helpers ------------------------------------------------------------ */

  // Live data uses bb_squeeze_pct (0-100 percentile). Export used raw BBW (55-320).
  // Mapping: tight = <30%, mid = 30-60%, wide = >60%  (mirrors stream-squeeze-tight/mid class thresholds)
  function sqzCls(pct) {
    if (pct == null) return "";
    return pct < 30 ? "tight" : pct < 60 ? "mid" : "wide";
  }

  // Format "ago" time from seconds elapsed
  function fmtWhen(s) {
    if (s == null || isNaN(s)) return "";
    return s < 8 ? "now" : s < 60 ? s + "s" : s < 3600 ? Math.floor(s / 60) + "m" : Math.floor(s / 3600) + "h";
  }
  function whenCls(s) { return s < 120 ? "fresh" : s < 900 ? "mid" : ""; }

  // Derive "when" (seconds ago) from a transition's ts ISO string
  function tsToAgo(ts) {
    if (!ts) return null;
    try {
      var d = new Date(ts);
      return Math.floor((Date.now() - d.getTime()) / 1000);
    } catch (e) { return null; }
  }

  // Signal strength bucket: live signals carry strength = volume_ratio (float, may be 0)
  function sigWeight(r) {
    var vr = r.strength || 0;
    return vr >= 1.5 ? 3 : vr >= 1.2 ? 2 : 1;
  }

  // Derive direction for filtering purposes from a live row
  function rowDir(r) {
    if (r.row_type === "signal") {
      return r.signal_direction === "bullish" ? "bull" : r.signal_direction === "bearish" ? "bear" : "neu";
    }
    if (r.row_type === "transition") {
      // No explicit meaning field — infer from label text (contains "→ <state>")
      var lbl = (r.label || "").toLowerCase();
      if (/bull|long|above|overbought|buy/.test(lbl)) return "bull";
      if (/bear|short|below|oversold|sell/.test(lbl)) return "bear";
      return "neu";
    }
    if (r.row_type === "position") {
      return r.direction === "long" ? "bull" : r.direction === "short" ? "bear" : "neu";
    }
    if (r.row_type === "liquidation") {
      return r.direction === "bullish" ? "bull" : r.direction === "bearish" ? "bear" : "neu";
    }
    if (r.row_type === "event") {
      return "neu";
    }
    // zone
    return r.side === "support" ? "bull" : r.side === "resistance" ? "bear" : "neu";
  }

  // Zone proximity class (for "when" coloring we use proximity for zones)
  function zoneWhenCls(prox) {
    return prox <= 0.5 ? "fresh" : prox <= 2 ? "mid" : "";
  }

  /* ---- meter / stars / strength bar -------------------------------------- */
  function meterHTML(n) {
    var s = '<span class="meter">';
    for (var i = 1; i <= 3; i++) s += '<i class="' + (i <= n ? "f" : "") + '"></i>';
    return s + "</span>";
  }
  function starsHTML(n) {
    var s = '<span class="stars" title="' + n + '★ significance">';
    for (var i = 1; i <= 3; i++) s += '<span class="' + (i <= n ? "" : "off") + '">' + IC.star + "</span>";
    return s + "</span>";
  }
  function typeGlyph(t) {
    return t === "signal" ? IC.sig : t === "transition" ? IC.trn : t === "position" ? IC.pos :
      t === "liquidation" ? IC.liq : t === "event" ? IC.event : IC.zone;
  }

  /* ---- per-type cell builders -------------------------------------------- */
  function eventCell(r) {
    if (r.row_type === "signal") {
      var bc  = r.signal_direction === "bullish" ? "bull" : "bear";
      var ico = r.signal_direction === "bullish" ? IC.bull : IC.bear;
      return '<div class="ev"><span class="ev-h ' + bc + '"><span class="dirico">' + ico + '</span>' + escapeHtml(r.label) + '</span>' +
        '<span class="ev-s">' + escapeHtml(r.detail || "") + '</span></div>';
    }
    if (r.row_type === "transition") {
      // label is already "Indicator: from → to" — split on first ": "
      var parts  = (r.label || "").split(/:\s*/);
      var indic  = parts[0] || r.label;
      var change = parts.slice(1).join(": ");
      // Highlight the "to" state after the arrow
      var arrow  = change.indexOf(" → ");
      var from   = arrow >= 0 ? change.slice(0, arrow) : change;
      var to     = arrow >= 0 ? change.slice(arrow + 3) : "";
      var dirCls = rowDir(r) === "bull" ? "to-bull" : rowDir(r) === "bear" ? "to-bear" : "to-neu";
      return '<div class="ev"><span class="ev-h">' + escapeHtml(indic) + '</span>' +
        '<span class="ev-s">' + escapeHtml(from) + ' <span class="arrow">→</span> <span class="' + dirCls + '">' + escapeHtml(to) + '</span></span></div>';
    }
    if (r.row_type === "position") {
      var pbc  = r.direction === "long" ? "bull" : "bear";
      var pico = r.direction === "long" ? IC.bull : IC.bear;
      return '<div class="ev"><span class="ev-h ' + pbc + '"><span class="dirico">' + pico + '</span>' + escapeHtml(r.label) + '</span>' +
        '<span class="ev-s">' + escapeHtml(r.detail || "") + '</span></div>';
    }
    if (r.row_type === "liquidation") {
      var lbc  = r.direction === "bullish" ? "bull" : "bear";
      var lico = r.direction === "bullish" ? IC.bull : IC.bear;
      return '<div class="ev"><span class="ev-h ' + lbc + '"><span class="dirico">' + lico + '</span>' + escapeHtml(r.label) + '</span>' +
        '<span class="ev-s">' + escapeHtml(r.detail || "") + '</span></div>';
    }
    if (r.row_type === "event") {
      // Neutral styling — a macro event has no bull/bear lean of its own.
      return '<div class="ev"><span class="ev-h">' + escapeHtml(r.label) + '</span>' +
        '<span class="ev-s">' + escapeHtml(r.detail || "") + '</span></div>';
    }
    // zone
    var st   = r.status;
    var side = r.side;
    var srcMore = (r.sources && r.sources.length > 1)
      ? ' <span style="color:var(--fg4)">+' + (r.sources.length - 1) + '</span>' : "";
    var sideTxt = side === "support" ? "Support" : side === "inside" ? "Inside" : "Resistance";
    // User-drawn bands can contain price — "% away" is meaningless inside.
    var distHtml = st === "inside"
      ? 'price in your zone '
      : '<span class="num">' + (r.proximity != null ? r.proximity.toFixed(1) : "?") + '%</span> away ';
    return '<div class="ev"><span class="ev-h"><span class="lvl-pill">' + escapeHtml(r.label) + srcMore + '</span> <span class="side ' + side + '">' +
      sideTxt + '</span></span>' +
      '<span class="ev-s">' + distHtml +
      '<span class="status" data-st="' + st + '"><span class="pips"><i></i><i></i><i></i></span>' + st + '</span></span></div>';
  }

  function magCell(r, locked) {
    if (locked) return '<span class="lockico">' + IC.lock + 'Member</span>';
    if (r.row_type === "signal") {
      return '<span class="mag">' + meterHTML(sigWeight(r)) + '</span>';
    }
    if (r.row_type === "transition") {
      return '<span class="mag">' + starsHTML(r.significance || 1) + '</span>';
    }
    if (r.row_type === "position") {
      // strength = |current_rr| (or a pnl-derived proxy) — 3R fills the bar.
      var rval = r.strength || 0;
      var rpct = Math.min(100, rval / 3 * 100);
      return '<span class="mag"><span class="strbar"><i style="width:' + rpct + '%"></i></span>' +
        '<span class="mv">' + rval.toFixed(1) + '<span class="u">R</span></span></span>';
    }
    if (r.row_type === "liquidation") {
      // strength is pre-scaled 0-3 (total_usd / $1M, capped) — same bar shape
      // as the position R-bar, value shown as a compact $ amount instead.
      var lval = r.strength || 0;
      var lpct = Math.min(100, lval / 3 * 100);
      var lusd = (typeof _fmtLiqUsd === "function") ? _fmtLiqUsd(r.total_usd || 0) : "$" + Math.round(r.total_usd || 0);
      return '<span class="mag"><span class="strbar strbar-liq"><i style="width:' + lpct + '%"></i></span>' +
        '<span class="mv">' + lusd + '</span></span>';
    }
    if (r.row_type === "event") {
      // No magnitude concept for a macro event — neutral placeholder.
      return '<span class="mag mag-event">—</span>';
    }
    // zone — live code uses strength / 5 * 100 for the bar width
    var pct = Math.min(100, (r.strength || 0) / 5 * 100);
    return '<span class="mag"><span class="strbar"><i style="width:' + pct + '%"></i></span>' +
      '<span class="mv">' + (r.strength != null ? r.strength.toFixed(1) : "0.0") + '</span></span>';
  }

  function whenCell(r) {
    // For signals: use status ('current' = "⚡ Just fired", 'previous' = "Last bar")
    // For transitions: derive from ts ISO string
    // For zones: use proximity as freshness proxy (there is no timestamp)
    if (r.row_type === "signal") {
      var cls = r.status === "current" ? "fresh" : "";
      var txt = r.status === "current" ? "⚡ fired" : "last bar";
      return '<span class="when ' + cls + '"><span class="fdot"></span>' + txt + '</span>';
    }
    if (r.row_type === "transition") {
      var ago = tsToAgo(r.ts);
      if (ago != null) {
        return '<span class="when ' + whenCls(ago) + '"><span class="fdot"></span>' + fmtWhen(ago) + '</span>';
      }
      return '<span class="when"><span class="fdot"></span>—</span>';
    }
    if (r.row_type === "position") {
      var pcls = r.risk_status === "profitable" ? "fresh" : "";
      return '<span class="when ' + pcls + '"><span class="fdot"></span>' + escapeHtml(r.age || "open") + '</span>';
    }
    if (r.row_type === "liquidation") {
      return '<span class="when fresh"><span class="fdot"></span>live</span>';
    }
    if (r.row_type === "event") {
      var hrs = r.hours_until;
      var ecls = (hrs <= 0) ? "fresh" : "";
      var etxt = (hrs <= 0) ? "today" : "in ~" + Math.round(hrs) + "h";
      return '<span class="when ' + ecls + '"><span class="fdot"></span>' + etxt + '</span>';
    }
    // zone
    if (r.status === "inside") {
      return '<span class="when fresh"><span class="fdot"></span>inside zone</span>';
    }
    var wc = zoneWhenCls(r.proximity || 999);
    return '<span class="when ' + wc + '"><span class="fdot"></span>' +
      (r.proximity != null ? r.proximity.toFixed(1) + "% away" : "?") + '</span>';
  }

  function bbwCell(r) {
    if (r.row_type === "position" || r.row_type === "liquidation" || r.row_type === "event") {
      // None of these carry BB squeeze data — always the neutral placeholder.
      return '<span class="squeeze"><span class="gauge"><i></i><i></i><i></i></span><span class="bv">—</span></span>';
    }
    // Live data: bb_squeeze_pct is 0-100 percentile. We display it as-is.
    var pct = r.bb_squeeze_pct;
    if (pct == null) return '<span class="squeeze"><span class="gauge"><i></i><i></i><i></i></span><span class="bv">—</span></span>';
    var c = sqzCls(pct);
    return '<span class="squeeze ' + c + '"><span class="gauge"><i></i><i></i><i></i></span><span class="bv">' + Math.round(pct) + '%</span>' +
      (c === "tight" ? '<span class="tighttag">tight</span>' : "") + '</span>';
  }

  /* ---- row_type whitelists ------------------------------------------------ */
  // By-asset (assetView) accepts anything with a real per-symbol identity.
  function _isAssetRowType(rt) {
    return rt === "signal" || rt === "transition" || rt === "zone" || rt === "position" || rt === "liquidation";
  }
  // Flat stream table accepts everything asset-view does, PLUS the single
  // global 'event' row — it has no real symbol (MACRO
  // placeholder) so it is deliberately excluded from _isAssetRowType/assetView.
  function _isStreamRowType(rt) {
    return _isAssetRowType(rt) || rt === "event";
  }

  /* ---- filtering / sorting ----------------------------------------------- */
  function passes(r) {
    if (state.search && (r.symbol || "").toLowerCase().indexOf(state.search.toLowerCase()) < 0) return false;
    if (state.tf !== "all" && r.tf !== state.tf) return false;
    if (state.dir !== "all") {
      if (rowDir(r) !== state.dir) return false;
    }
    if (state.squeeze !== "all") {
      if (sqzCls(r.bb_squeeze_pct) !== state.squeeze) return false;
    }
    if (state.side !== "all" && r.row_type === "zone" && r.side !== state.side) return false;
    if (state.signif !== "all" && r.row_type === "transition" && (r.significance || 1) < +state.signif) return false;
    return true;
  }

  function lensFilter(list, lens) {
    if (lens === "all") return list;
    return list.filter(function (r) { return r.row_type === lens; });
  }

  function sortFor(list, lens) {
    var l = list.slice();
    if (lens === "zone") {
      // sort by proximity ascending
      l.sort(function (a, b) { return (a.proximity || 999) - (b.proximity || 999); });
    } else if (lens === "transition") {
      // newest first by ts
      l.sort(function (a, b) { return (b.ts || "").localeCompare(a.ts || ""); });
    } else {
      // signals: current before previous; mixed (all): default order (already sorted by _computeStreamRows)
      // keep original order from the caller which already applies the global sort
    }
    return l;
  }

  /* ---- counts ------------------------------------------------------------ */
  function counts(rows) {
    var c = { all: 0, signal: 0, transition: 0, zone: 0, position: 0, liquidation: 0 };
    for (var i = 0; i < rows.length; i++) {
      var rt = rows[i].row_type;
      if (rt === "signal" || rt === "transition" || rt === "zone" || rt === "position" || rt === "liquidation") {
        c[rt]++;
        c.all++;
      } else if (rt === "event") {
        // No lens chip (and no per-type bucket) for the single global event
        // row — it still belongs in the "All" total, so bump c.all only.
        c.all++;
      }
    }
    return c;
  }

  /* ---- TF option list ---------------------------------------------------- */
  var TFS = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1D", "3D", "1W", "1M",
             "15m", "1H", "4H", "D", "3D", "W", "M"];
  function buildTfOpts(rows) {
    // Collect unique TFs from live data
    var seen = {};
    var opts = [["all", "All"]];
    for (var i = 0; i < rows.length; i++) {
      var tf = rows[i].tf;
      if (tf && !seen[tf]) { seen[tf] = 1; opts.push([tf, tf]); }
    }
    return opts;
  }

  /* ---- chip / filter helpers --------------------------------------------- */
  function lensChip(key, label, glyph, cnt, tcls) {
    return '<button class="lens-chip ' + (tcls || "") + (state.lens === key ? " on" : "") + '" data-lens="' + key + '">' +
      '<span class="lg">' + glyph + '</span>' + label + '<span class="lc">' + cnt + '</span></button>';
  }

  function fchip(id, label, val, opts) {
    var o = opts.map(function (op) {
      return '<option value="' + op[0] + '"' + (val === op[0] ? " selected" : "") + '>' + op[1] + '</option>';
    }).join("");
    return '<span class="fchip ' + (val !== "all" ? "set" : "") + '"><label>' + label + '</label>' +
      '<select data-filter="' + id + '">' + o + '</select><span class="cv">' + IC.caret + '</span></span>';
  }

  /* ---- contextual filters ------------------------------------------------ */
  function contextualFilters(rows) {
    var lens = state.lens;
    var F = "";
    var tfOpts = buildTfOpts(rows);
    if (lens === "transition") {
      F += fchip("signif", "Min ★", state.signif, [["all", "Any"], ["1", "★"], ["2", "★★"], ["3", "★★★"]]);
    } else if (lens === "zone") {
      F += fchip("tf", "TF", state.tf, tfOpts);
      F += '<div class="seg" data-seg="side">' +
        '<button data-v="all"        class="' + (state.side === "all"        ? "on" : "") + '">Both</button>' +
        '<button data-v="support"    class="' + (state.side === "support"    ? "on bull" : "") + '">Support</button>' +
        '<button data-v="resistance" class="' + (state.side === "resistance" ? "on bear" : "") + '">Resist</button>' +
        '</div>';
      F += fchip("squeeze", "Squeeze", state.squeeze, [["all", "All"], ["tight", "Tight"], ["mid", "Mid"], ["wide", "Wide"]]);
    } else {
      // all / signal
      F += fchip("tf", "TF", state.tf, tfOpts);
      F += '<div class="seg" data-seg="dir">' +
        '<button data-v="all"  class="' + (state.dir === "all"  ? "on" : "") + '">All</button>' +
        '<button data-v="bull" class="' + (state.dir === "bull" ? "on bull" : "") + '">Bull</button>' +
        '<button data-v="bear" class="' + (state.dir === "bear" ? "on bear" : "") + '">Bear</button>' +
        '</div>';
      F += fchip("squeeze", "Squeeze", state.squeeze, [["all", "All"], ["tight", "Tight"], ["mid", "Mid"], ["wide", "Wide"]]);
    }
    return F;
  }

  /* ---- live ring ---------------------------------------------------------- */
  function liveCluster() {
    var meta = _meta || {};
    var nextRefreshTs  = meta.nextRefreshTs  || 0;
    var lastFetchTs    = meta.lastFetchTs    || 0;
    var now            = Date.now();

    // Countdown: seconds until next refresh
    var countdown = Math.max(0, Math.round((nextRefreshTs - now) / 1000));
    // Cycle length in seconds (infer from gap; floor at 10 to avoid div-zero)
    var cycle = Math.max(10, Math.round((nextRefreshTs - lastFetchTs) / 1000));
    var R     = 11;
    var C     = 2 * Math.PI * R;
    var off   = C * (1 - countdown / cycle);

    // "updated N ago"
    var ago   = lastFetchTs ? Math.round((now - lastFetchTs) / 1000) : null;
    var agoTxt = ago != null ? fmtWhen(ago) : "—";

    return '<div class="live' + (state.paused ? " paused" : "") + '">' +
      '<div class="live-ring"><svg viewBox="0 0 26 26">' +
      '<circle class="track" cx="13" cy="13" r="' + R + '" fill="none" stroke-width="2.5"></circle>' +
      '<circle class="prog" cx="13" cy="13" r="' + R + '" fill="none" stroke-width="2.5" ' +
        'stroke-dasharray="' + C.toFixed(1) + '" stroke-dashoffset="' + off.toFixed(1) + '"></circle>' +
      '</svg><span class="n">' + (state.paused ? "∥" : countdown) + '</span></div>' +
      '<div class="live-txt"><b>updated ' + agoTxt + '</b><br>' + (state.paused ? "paused" : "next refresh") + '</div>' +
      '<button class="pausebtn" id="radarPauseBtn" title="' + (state.paused ? "Resume" : "Pause") + ' live updates">' +
      (state.paused ? IC.play : IC.pause) + '</button></div>';
  }

  /* ---- head render ------------------------------------------------------- */
  function renderHead(rows) {
    var c = counts(rows);
    var H = "";

    H += '<div class="rhead" id="rhead">';

    H += '<div class="lens">' +
      lensChip("all",        "All",         IC.all,  c.all,        "t-all")  +
      lensChip("signal",     "Signals",     IC.sig,  c.signal,     "t-sig")  +
      lensChip("transition", "Transitions", IC.trn,  c.transition, "t-trn")  +
      lensChip("zone",       "Zones",       IC.zone, c.zone,       "t-zone") +
      '</div>';

    H += '<div class="rh-div"></div>';

    H += '<div class="modeseg">' +
      '<button data-mode="stream" class="' + (state.mode === "stream" ? "on" : "") + '">' + IC.stream + 'Stream</button>' +
      '<button data-mode="asset"  class="' + (state.mode === "asset"  ? "on" : "") + '">' + IC.grid   + 'By asset</button>' +
      '</div>';

    H += '<div class="rsearch">' + IC.search + '<input id="radarSearch" type="text" placeholder="Search…" value="' + escapeHtml(state.search) + '"></div>';

    H += '<div class="rfilters">' + contextualFilters(rows) + '</div>';

    H += '<div class="rh-spacer"></div>';

    H += liveCluster();

    H += '</div>'; // .rhead

    return H;
  }

  /* ---- table head -------------------------------------------------------- */
  function thead(lens) {
    var firedLabel = lens === "zone" ? "DIST" : "FIRED";
    return '<thead><tr>' +
      '<th class="c-type"></th>' +
      '<th class="c-asset">Asset</th>' +
      '<th class="c-tf">TF</th>' +
      '<th class="c-event">Event</th>' +
      '<th class="c-mag">Strength</th>' +
      '<th class="c-when">' + firedLabel + '</th>' +
      '<th class="c-bbw">BBW</th>' +
      '<th class="c-go"></th>' +
      '</tr></thead>';
  }

  /* ---- row HTML ---------------------------------------------------------- */
  function rowHTML(r) {
    // locked = transition rows when preview/free tier
    var locked = r.row_type === "transition" && _meta && _meta.preview;
    var d = rowDir(r);
    var cls = "erow " + r.row_type +
      (r.row_type === "signal" ? " " + d : "") +
      (locked ? " locked" : "");
    // Display symbol (live uses r.symbol)
    var sym = r.symbol || "?";
    // Asset class from allAssets if available, fallback blank
    var assetCls = "";
    if (window.allAssets && window.allAssets[sym]) {
      var a = window.allAssets[sym];
      assetCls = a.type || a.asset_class || "";
    }

    // Hover row actions — carried as data attributes so click
    // handlers never rely on array-index lookups into module state that can
    // drift between render and click (see _logRowToJournal/_alertRowLevel).
    var rowPrice = (r.row_type === "zone" && typeof r.price === "number" && isFinite(r.price)) ? r.price : "";
    var rowDirAttr = d === "bull" ? "long" : d === "bear" ? "short" : "";

    // data-key mirrors r.key — the stable identity the
    // stream-view diff (see _applyRowDiff) keys added/removed/changed rows on.
    return '<tr class="' + cls + '" data-sym="' + escapeHtml(sym) + '" data-tf="' + escapeHtml(r.tf || "") + '"' +
      ' data-rtype="' + escapeHtml(r.row_type) + '" data-dir="' + rowDirAttr + '"' +
      ' data-price="' + escapeHtml(String(rowPrice)) + '" data-label="' + escapeHtml(r.label || "") + '"' +
      ' data-key="' + escapeHtml(r.key || "") + '">' +
      '<td class="c-type"><span class="tglyph">' + typeGlyph(r.row_type) + '</span></td>' +
      '<td class="c-asset"><span class="asset"><span class="sym">' + escapeHtml(sym) + '</span>' +
        (assetCls ? '<span class="cls">' + escapeHtml(assetCls) + '</span>' : '') + '</span></td>' +
      '<td class="c-tf"><span class="tf">' + escapeHtml(r.tf || "—") + '</span></td>' +
      '<td class="c-event">' + eventCell(r) + '</td>' +
      '<td class="c-mag">' + magCell(r, locked) + '</td>' +
      '<td class="c-when">' + whenCell(r) + '</td>' +
      '<td class="c-bbw">' + bbwCell(r) + '</td>' +
      '<td class="c-go">' + rowActionsHTML(r, sym) + '</td>' +
      '</tr>';
  }

  // Hover action buttons for the .c-go cell — STREAM rows only (subrowHTML /
  // By-Asset view is untouched). Position rows already ARE a journal trade;
  // event rows have no real symbol (MACRO placeholder) — neither gets the
  // extra Log/Alert buttons (goico stays for both, unchanged from before).
  function rowActionsHTML(r, sym) {
    var html = '<span class="rowactions">' +
      '<button class="goico" title="Open ' + escapeHtml(sym) + ' chart">' + IC.cross + '</button>';
    if (r.row_type !== "position" && r.row_type !== "event") {
      html += '<button class="logico" title="Log to journal">' + IC.log + '</button>';
      if (r.row_type === "zone" && typeof r.price === "number" && isFinite(r.price)) {
        html += '<button class="alerico" title="Alert at this level">' + IC.bell + '</button>';
      }
    }
    html += '</span>';
    return html;
  }

  /* ---- stream table ------------------------------------------------------ */
  // `rowSigsOut`, when passed, is filled with {key, html} per row in render
  // order — the exact strings _captureStreamDiffState primes its cache with,
  // so a later tick's diff compares like-for-like.
  function streamTable(list, lens, totalZones, rowSigsOut) {
    if (!list.length) return emptyState();
    var hint;
    if (lens === "zone") {
      hint = '<div class="feedhint">Showing the <b>' + list.length + '</b> nearest of <b>' + (totalZones || list.length) + '</b> live zones · sorted by distance</div>';
    } else {
      hint = '<div class="feedhint"><b>' + list.length + '</b> live ' + (lens === "all" ? "events" : lens + "s") + ' · auto-refreshing</div>';
    }
    var bodyHtml = "";
    for (var i = 0; i < list.length; i++) {
      var rHtml = rowHTML(list[i]);
      bodyHtml += rHtml;
      if (rowSigsOut) rowSigsOut.push({ key: list[i].key, html: rHtml });
    }
    return '<table class="rtable">' + thead(lens) + '<tbody>' + bodyHtml + '</tbody></table>' + hint;
  }

  /* ---- upsell card ------------------------------------------------------- */
  function upsell() {
    var count = 0;
    for (var i = 0; i < _rows.length; i++) {
      if (_rows[i].row_type === "transition") count++;
    }
    return '<div class="upsell">' +
      '<div class="uc">' + IC.trn + '</div>' +
      '<h3>Transitions is a Member feature</h3>' +
      '<p>See the moment an indicator flips state — RSI crossing, MACD turning, the Alligator waking — across all assets, the instant it happens.</p>' +
      '<div class="uprev">' +
      '<div class="pr"><span class="a">stoch_rsi</span><span class="b">neutral → overbought</span></div>' +
      '<div class="pr"><span class="a">MACD</span><span class="b">bearish → bullish</span></div>' +
      '<div class="pr"><span class="a">CPR Position</span><span class="b">s2_s3 → s1_s2</span></div>' +
      '</div>' +
      '<span class="upnote">This needs a higher plan. An admin can change plans under Admin → Users.</span>' +
      (count ? '<span class="upnote">' + count + ' transitions firing right now</span>' : '') +
      '</div>';
  }

  /* ---- by-asset confluence view ------------------------------------------ */

  // Derive a numeric "when" (seconds) from a live row for freshness comparisons.
  // Signals: current = 0, previous = 120. Transitions: tsToAgo. Zones: proximity * 60 (proxy).
  function rowWhen(r) {
    if (r.row_type === "signal") {
      return r.status === "current" ? 0 : 120;
    }
    if (r.row_type === "transition") {
      var ago = tsToAgo(r.ts);
      return ago != null ? ago : 9999;
    }
    // zone: use proximity as freshness proxy (closer = fresher)
    return r.proximity != null ? Math.round(r.proximity * 60) : 9999;
  }

  // Client-side bias tally — mirrors biasOf from the export, adapted to live row shape.
  function _biasOfRows(rows) {
    var sc = 0;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r.row_type === "signal") {
        var dir = r.signal_direction === "bullish" ? 1 : r.signal_direction === "bearish" ? -1 : 0;
        sc += dir * sigWeight(r);
      } else if (r.row_type === "transition") {
        var d = rowDir(r);
        var meaning = d === "bull" ? 1 : d === "bear" ? -1 : 0;
        sc += meaning * (r.significance || 1);
      } else {
        // zone: support = positive, resistance = negative; weight by proximity (closer = stronger)
        var side = r.side === "support" ? 1 : r.side === "resistance" ? -1 : 0;
        sc += side * Math.min(2, (r.strength || 0) / 5);
      }
    }
    return sc > 1.5 ? "bull" : sc < -1.5 ? "bear" : "mixed";
  }

  // Heat = activity intensity 1-4. Port of heatOf: event count + recency weighting.
  function _heatOf(rows, whenArr) {
    var recent = 0;
    for (var i = 0; i < whenArr.length; i++) {
      if (whenArr[i] < 300) recent++;
    }
    return Math.max(1, Math.min(4, Math.round(rows.length / 2 + recent / 2)));
  }

  // Render a single subrow within an asset group (port of subrowHTML from export).
  function subrowHTML(r) {
    var locked = r.row_type === "transition" && _meta && _meta.preview;
    var sym = r.symbol || "?";
    return '<div class="subrow ' + r.row_type + (locked ? " locked" : "") + '" data-sym="' + escapeHtml(sym) + '" data-tf="' + escapeHtml(r.tf || "") + '">' +
      '<span class="sgl">' + typeGlyph(r.row_type) + '</span>' +
      '<span class="c-tf"><span class="tf">' + escapeHtml(r.tf || "—") + '</span></span>' +
      '<span class="c-event">' + eventCell(r) + '</span>' +
      '<span class="c-mag">' + magCell(r, locked) + '</span>' +
      '<span class="c-when">' + whenCell(r) + '</span>' +
      '<span class="c-bbw">' + bbwCell(r) + '</span>' +
      '</div>';
  }

  // Main By-Asset view renderer. `rows` = same lens+filter-passed rows as the stream view.
  function assetView(rows) {
    if (!rows.length) return emptyState();

    // Group by symbol
    var map = {};
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var sym = r.symbol || "?";
      if (!map[sym]) map[sym] = [];
      map[sym].push(r);
    }

    // Build group objects
    var groups = [];
    var syms = Object.keys(map);
    for (var s = 0; s < syms.length; s++) {
      var sym = syms[s];
      var grpRows = map[sym];

      // Count by type
      var t = { signal: 0, transition: 0, zone: 0, position: 0, liquidation: 0 };
      for (var j = 0; j < grpRows.length; j++) { t[grpRows[j].row_type]++; }

      // Nearest zone
      var nearestProx = null;
      for (var j = 0; j < grpRows.length; j++) {
        if (grpRows[j].row_type === "zone" && grpRows[j].proximity != null) {
          if (nearestProx === null || grpRows[j].proximity < nearestProx) {
            nearestProx = grpRows[j].proximity;
          }
        }
      }

      // When array (seconds, for freshness + heat)
      var whenArr = [];
      for (var j = 0; j < grpRows.length; j++) { whenArr.push(rowWhen(grpRows[j])); }
      var freshness = whenArr.length ? Math.min.apply(null, whenArr) : 9999;

      // Heat
      var heat = _heatOf(grpRows, whenArr);

      // Bias: prefer backend composite from _meta.biasBySymbol; fall back to client tally
      var biasKey = "mixed";
      var biasBySymbol = _meta && _meta.biasBySymbol;
      if (biasBySymbol && biasBySymbol[sym] && biasBySymbol[sym].bias != null) {
        var raw = biasBySymbol[sym].bias;
        biasKey = raw === "long" ? "bull" : raw === "short" ? "bear" : "mixed";
      } else {
        biasKey = _biasOfRows(grpRows);
      }

      // Asset class from window.allAssets
      var assetCls = "";
      if (window.allAssets && window.allAssets[sym]) {
        var a = window.allAssets[sym];
        assetCls = a.type || a.asset_class || "";
      }

      groups.push({
        sym: sym,
        cls: assetCls,
        rows: grpRows,
        t: t,
        bias: biasKey,
        heat: heat,
        near: nearestProx,
        fresh: freshness,
      });
    }

    // Sort: heat desc, then freshness asc
    groups.sort(function (a, b) { return b.heat - a.heat || a.fresh - b.fresh; });

    if (!groups.length) return emptyState();

    // Render groups
    var html = "";
    for (var g = 0; g < groups.length; g++) {
      var grp = groups[g];
      var collapsed = state.collapsed[grp.sym];

      // Heat bars (4 total, N filled)
      var heatBars = "";
      for (var h = 1; h <= 4; h++) {
        heatBars += '<i class="' + (h <= grp.heat ? "f" : "") + '"></i>';
      }

      // Bias icon + label (non-advice language)
      var biasIco = grp.bias === "bull" ? IC.bull : grp.bias === "bear" ? IC.bear : "";
      var biasLbl = grp.bias === "bull" ? "Bull lean" : grp.bias === "bear" ? "Bear lean" : "Mixed";

      // Type chips — omit zero-count types
      var types = "";
      if (grp.t.signal)     types += '<span class="atype sig">'  + IC.sig  + '<b>' + grp.t.signal     + '</b> sig</span>';
      if (grp.t.transition) types += '<span class="atype trn">'  + IC.trn  + '<b>' + grp.t.transition + '</b> flip</span>';
      if (grp.t.zone)       types += '<span class="atype zone">' + IC.zone + '<b>' + grp.t.zone       + '</b> zone</span>';
      if (grp.t.position)   types += '<span class="atype pos">'  + IC.pos  + '<b>' + grp.t.position   + '</b> pos</span>';
      if (grp.t.liquidation) types += '<span class="atype liq">' + IC.liq + '<b>' + grp.t.liquidation + '</b> liq</span>';

      // Subrows: sort by freshness asc, then render
      var subsSorted = grp.rows.slice().sort(function (a, b) { return rowWhen(a) - rowWhen(b); });
      var subs = "";
      for (var sr = 0; sr < subsSorted.length; sr++) { subs += subrowHTML(subsSorted[sr]); }

      html += '<div class="agroup' + (collapsed ? " collapsed" : "") + '" data-sym="' + escapeHtml(grp.sym) + '">' +
        '<div class="ahead" data-toggle="' + escapeHtml(grp.sym) + '">' +
        '<span class="achev">' + IC.chev + '</span>' +
        '<span class="asym">' + escapeHtml(grp.sym) + (grp.cls ? ' <span class="acls">' + escapeHtml(grp.cls) + '</span>' : '') + '</span>' +
        '<span class="abias ' + grp.bias + '">' + biasIco + biasLbl + '</span>' +
        '<span class="aheat"><span class="bars">' + heatBars + '</span><span class="ht">' + grp.rows.length + ' live</span></span>' +
        (grp.near != null ? '<span class="anear">nearest zone ' + grp.near.toFixed(1) + '%</span>' : '') +
        '<span class="atypes">' + types + '</span>' +
        '</div>' +
        '<div class="sublist">' + subs + '</div>' +
        '</div>';
    }

    return html;
  }

  /* ---- empty state ------------------------------------------------------- */
  function emptyState() {
    return '<div class="empty"><div class="ic">' + IC.empty + '</div>' +
      '<h3>Nothing on the radar</h3>' +
      '<p>No live events match the current filters. Widen a filter or clear the search to see the full stream.</p>' +
      '<button id="radarClearAll">Clear filters</button></div>';
  }

  /* ---- full render ------------------------------------------------------- */
  function _render() {
    if (!_section) return;
    var rows = _rows || [];

    var html = "";

    // Control strip (rendered inside section, above the feed)
    html += renderHead(rows);

    // Feed area
    html += '<div class="rfeed" id="radarFeed">';

    // Populated only for the plain stream table below — primes the keyed
    // row cache a later data-only tick can diff against.
    var rowSigs = null;

    if (state.mode === "asset") {
      var assetFiltered = lensFilter(rows.filter(function (r) { return _isAssetRowType(r.row_type); }), state.lens);
      assetFiltered = assetFiltered.filter(passes);
      html += assetView(assetFiltered);
    } else if (state.lens === "transition" && _meta && _meta.preview) {
      html += upsell();
    } else {
      var filtered = lensFilter(rows.filter(function (r) { return _isStreamRowType(r.row_type); }), state.lens);
      filtered = filtered.filter(passes);
      var sorted  = sortFor(filtered, state.lens);
      // Total zone count for hint (all zone rows before filter, for context)
      var allZones = 0;
      for (var i = 0; i < rows.length; i++) { if (rows[i].row_type === "zone") allZones++; }
      rowSigs = [];
      html += streamTable(sorted, state.lens, state.lens === "zone" ? allZones : null, rowSigs);
    }

    html += '</div>'; // .rfeed

    // Preserve scroll
    var feed = _section.querySelector("#radarFeed");
    var scrollTop = feed ? feed.scrollTop : 0;

    _section.innerHTML = html;

    // Restore scroll
    var newFeed = _section.querySelector("#radarFeed");
    if (newFeed && scrollTop) newFeed.scrollTop = scrollTop;

    _bindAll(_section);

    // a plain (non-empty) stream table was just painted:
    // capture its keyed rows so the next data-only render() call (the 15/30s
    // tick from scenarios.js) can diff instead of rebuilding. Any other shape
    // (By-Asset, upsell, empty state, or no rows at all) invalidates the
    // cache — the next tick simply falls back to this same full rebuild.
    if (rowSigs && rowSigs.length) {
      _captureStreamDiffState(rowSigs);
    } else {
      _streamDiffState.ready = false;
    }
  }

  /* ---- keyed row diff -------------------------------- */
  // Snapshot the <tr> nodes _render() just painted for the plain stream table,
  // keyed by r.key, alongside the EXACT html string each was built from (the
  // same rowHTML(r) output a later tick recomputes) — so future comparisons
  // are string-vs-string, never a raw-string-vs-browser-normalized-outerHTML
  // mismatch that would make every row look "changed" forever.
  function _captureStreamDiffState(rowSigs) {
    _streamDiffState.ready = false;
    var tbody = _section && _section.querySelector("#radarFeed .rtable tbody");
    if (!tbody) return;
    var trs = tbody.children;
    if (trs.length !== rowSigs.length) return; // shouldn't happen — skip, next tick just falls back
    var map = {};
    var order = [];
    for (var i = 0; i < rowSigs.length; i++) {
      var key = rowSigs[i].key;
      if (!key) continue; // every live row carries r.key; skip defensively if one doesn't
      map[key] = { sig: rowSigs[i].html, node: trs[i] };
      order.push(key);
    }
    if (!order.length) return;
    _streamDiffState.section = _section;
    _streamDiffState.lens = state.lens;
    _streamDiffState.rowMap = map;
    _streamDiffState.order = order;
    _streamDiffState.ready = true;
  }

  // Parse one rowHTML() string into a detached <tr> node. A bare <tr> only
  // parses correctly with a table-section context node — assigning innerHTML
  // on a real <tbody> gives the HTML parser that context.
  function _htmlToRowNode(html) {
    var tmp = document.createElement("tbody");
    tmp.innerHTML = html;
    return tmp.firstElementChild;
  }

  // Try to satisfy a render() data refresh by patching the already-painted
  // stream table in place instead of rebuilding it. Returns false (caller
  // must fall back to a full _render()) whenever the current view isn't a
  // plain, non-empty stream table matching what the cache was captured
  // against — By-Asset mode, the Transitions upsell card, a lens/lens switch,
  // a torn-down section, or the filtered set going empty all bail out here
  // and let _render() draw the right thing from scratch.
  function _tryDiffStreamUpdate() {
    if (!_section) return false;
    if (state.mode === "asset") return false;
    if (state.lens === "transition" && _meta && _meta.preview) return false;
    if (!_streamDiffState.ready || _streamDiffState.section !== _section || _streamDiffState.lens !== state.lens) return false;

    var tbody = _section.querySelector("#radarFeed .rtable tbody");
    if (!tbody) return false;

    var rows = _rows || [];
    var filtered = lensFilter(rows.filter(function (r) { return _isStreamRowType(r.row_type); }), state.lens);
    filtered = filtered.filter(passes);
    var sorted = sortFor(filtered, state.lens);
    if (!sorted.length) return false; // let the empty state render normally

    _applyRowDiff(tbody, sorted);

    var allZones = 0;
    for (var i = 0; i < rows.length; i++) { if (rows[i].row_type === "zone") allZones++; }
    _updateFeedHint(sorted, state.lens, allZones);
    _updateHeadCounts(rows);
    _updateTfOptions(rows);

    return true;
  }

  // Reconcile `tbody`'s children against `sorted` by r.key: unchanged rows
  // are left completely untouched (no rebuild, no rebind); changed rows are
  // rebuilt + rebound; added rows are built + bound + inserted; removed rows
  // are dropped. Final pass reorders nodes to match `sorted` when ranking
  // changed (cheap — a no-op check per node when the order didn't change).
  function _applyRowDiff(tbody, sorted) {
    var prevMap = _streamDiffState.rowMap;
    var newMap = {};
    var order = [];
    var i, r, key, newHtml, entry, node;

    for (i = 0; i < sorted.length; i++) {
      r = sorted[i];
      key = r.key;
      // Every live row carries a stable r.key; the fallback below only
      // guards against a future row type that forgets to set one.
      if (!key) key = "__nokey_" + i;
      order.push(key);
      newHtml = rowHTML(r);
      entry = prevMap[key];
      if (!entry) {
        node = _htmlToRowNode(newHtml);
        _bindRow(node);
      } else if (entry.sig !== newHtml) {
        node = _htmlToRowNode(newHtml);
        _bindRow(node);
      } else {
        node = entry.node;
      }
      newMap[key] = { sig: newHtml, node: node };
    }

    // Drop rows for keys no longer present.
    for (key in prevMap) {
      if (!newMap.hasOwnProperty(key)) {
        var oldNode = prevMap[key].node;
        if (oldNode && oldNode.parentNode) oldNode.parentNode.removeChild(oldNode);
      }
    }

    // Reorder to match `order` — walks the desired sequence against what's
    // actually in the DOM; a node already in place costs one reference
    // comparison, only out-of-place nodes get an insertBefore (which also
    // covers brand-new nodes, which have no parent yet).
    var ref = tbody.firstElementChild;
    for (i = 0; i < order.length; i++) {
      node = newMap[order[i]].node;
      if (node !== ref) {
        tbody.insertBefore(node, ref);
      } else {
        ref = ref.nextElementSibling;
      }
    }
    // Anything left past `ref` is stale (should already be gone via the
    // removal pass above) — guard against stragglers.
    while (ref) {
      var next = ref.nextElementSibling;
      tbody.removeChild(ref);
      ref = next;
    }

    _streamDiffState.rowMap = newMap;
    _streamDiffState.order = order;
  }

  // Lightweight head/feed refreshes that accompany a diffed tick — cheap
  // (a handful of nodes, not the row list) so they run every tick without
  // reintroducing the cost the row diff exists to avoid.
  function _updateFeedHint(list, lens, allZones) {
    if (!_section) return;
    var hintEl = _section.querySelector("#radarFeed .feedhint");
    if (!hintEl) return;
    if (lens === "zone") {
      hintEl.innerHTML = "Showing the <b>" + list.length + "</b> nearest of <b>" + (allZones || list.length) + "</b> live zones · sorted by distance";
    } else {
      hintEl.innerHTML = "<b>" + list.length + "</b> live " + (lens === "all" ? "events" : lens + "s") + " · auto-refreshing";
    }
  }

  function _updateHeadCounts(rows) {
    if (!_section) return;
    var c = counts(rows);
    var countMap = { all: c.all, signal: c.signal, transition: c.transition, zone: c.zone };
    var chips = _section.querySelectorAll("#rhead .lens-chip");
    for (var i = 0; i < chips.length; i++) {
      var key = chips[i].getAttribute("data-lens");
      if (countMap[key] == null) continue;
      var lc = chips[i].querySelector(".lc");
      if (lc) lc.textContent = countMap[key];
    }
  }

  function _updateTfOptions(rows) {
    if (!_section) return;
    var sel = _section.querySelector('#rhead select[data-filter="tf"]');
    if (!sel) return;
    var opts = buildTfOpts(rows);
    var same = opts.length === sel.options.length;
    if (same) {
      for (var i = 0; i < opts.length; i++) {
        if (sel.options[i].value !== opts[i][0]) { same = false; break; }
      }
    }
    if (same) return;
    var cur = sel.value;
    sel.innerHTML = opts.map(function (op) {
      return '<option value="' + op[0] + '"' + (cur === op[0] ? " selected" : "") + '>' + op[1] + '</option>';
    }).join("");
    sel.value = cur; // re-apply — innerHTML replacement can otherwise reset to index 0
  }

  /* ---- tick (own interval) ----------------------------------------------- */
  function _tick() {
    if (document.hidden) return;
    if (!_section) return;
    // Only re-render the live ring portion; avoid full re-render on each tick
    var liveEl = _section.querySelector(".live");
    if (liveEl) {
      var tmp = document.createElement("div");
      tmp.innerHTML = liveCluster();
      var newLive = tmp.firstElementChild;
      if (newLive) liveEl.parentNode.replaceChild(newLive, liveEl);
      // Re-bind pause button
      var pb = _section.querySelector("#radarPauseBtn");
      if (pb) pb.onclick = _togglePause;
    }
  }

  function _togglePause() {
    state.paused = !state.paused;
    window.__radarPaused = state.paused;
    _render();
  }

  /* ---- chart navigation -------------------------------------------------- */
  // Row tf values → chart timeframe strings. Zones/HTF signals carry
  // 'D'/'W'/'M'/'3D'; LTF signals already use '1h'/'4h'; transitions carry ''.
  var TF_TO_CHART = { "D": "1D", "W": "1W", "M": "1M", "3D": "3D", "1h": "1h", "4h": "4h" };

  function jumpChart(sym, tf) {
    // Land on the row's timeframe, not just the asset.
    var chartTf = TF_TO_CHART[tf] || null;
    if (chartTf) {
      // Same clamps every other TF-selection path applies (asset-search.js):
      // switchTimeframe() does NOT clamp, so the caller must.
      if (typeof isCryptoAsset === "function" && !isCryptoAsset(sym) &&
          ["1m", "3m", "5m", "15m", "30m"].indexOf(chartTf) >= 0) chartTf = "1D";
      if (!window._isPremium && ["1m", "3m", "5m", "15m", "30m", "1h"].indexOf(chartTf) >= 0) chartTf = "4h";
    }
    if (window.klineChartSymbol === sym) {
      // selectAssetFromSearch no-ops on the already-open symbol — switch the
      // timeframe directly instead of doing nothing.
      if (chartTf && chartTf !== window.klineChartTimeframe &&
          typeof switchTimeframe === "function") {
        switchTimeframe(chartTf);
      }
      return;
    }
    // Seed the target TF before the asset switch: selectAssetFromSearch keeps
    // whatever klineChartTimeframe already is (it never reads sessionStorage),
    // so this lands asset + TF in a single chart load.
    if (chartTf) window.klineChartTimeframe = chartTf;
    if (typeof selectAssetFromSearch === "function") {
      selectAssetFromSearch(sym);
    } else if (typeof selectAssetInPanel === "function") {
      selectAssetInPanel(sym);
    }
  }

  /* ---- journal / alert row actions ------------------------- */
  // Log-to-journal — the same entry point the drawer's log-setup action used
  // (closeDrawer -> setViewMode('journal') -> _journalShowLogFormPrefilled);
  // that scenario-era caller is gone, this is the live one. The delay before injecting is load-bearing:
  // the journal form container only exists after the panel inits, and
  // setViewMode('journal') itself lazy-loads ai-panel.js via ensureAIPanel()
  // (app-core.js) when it isn't loaded yet.
  function _logRowToJournal(row) {
    var sym = row.dataset.sym;
    if (!sym) return;
    var chartTf = TF_TO_CHART[row.dataset.tf] || row.dataset.tf || "";
    var priceRaw = row.dataset.price;
    var price = priceRaw ? parseFloat(priceRaw) : NaN;
    var data = { asset: sym, timeframe: chartTf, scenarioName: row.dataset.label || "" };
    if (row.dataset.dir) data.direction = row.dataset.dir;
    if (row.dataset.rtype === "zone" && isFinite(price)) data.entry = price;

    if (typeof closeDrawer === "function") closeDrawer();
    if (typeof setViewMode === "function") setViewMode("journal");
    // Inject-and-reassert: on the panel's FIRST open, initJournalPanel's async
    // trade-list fetch re-renders #journal-open-trades AFTER the 100ms inject
    // and wipes the form. Re-inject on a short backoff until it sticks
    // (the prefill fn removes any existing form first, so repeats are safe).
    var attempts = [100, 700, 1600];
    attempts.forEach(function (delay, i) {
      setTimeout(function () {
        var present = document.querySelector("#journal-open-trades .journal-log-form");
        if (present && i > 0) return; // stuck from an earlier attempt
        if (typeof _journalShowLogFormPrefilled === "function") {
          _journalShowLogFormPrefilled(data);
        } else if (i === attempts.length - 1 && typeof showToast === "function") {
          showToast("Journal panel not ready — try again in a moment");
        }
      }, delay);
    });
  }

  // Alert-at-level — zone rows only (the only rows carrying a real price).
  // Plain observation wording only, no directional recommendation.
  function _alertRowLevel(row) {
    var sym = row.dataset.sym;
    var priceRaw = row.dataset.price;
    var price = priceRaw ? parseFloat(priceRaw) : NaN;
    if (!sym || !isFinite(price)) return;
    var note = "Radar zone: " + (row.dataset.label || "level");
    if (typeof createPriceAlert === "function") createPriceAlert(sym, price, note);
  }

  // Bind one stream <tr>'s interactions — row click (open chart, skipped for
  // locked/event rows) plus its Go/Log/Alert action buttons. Single source of
  // truth for _bindAll (every row, on a full render) AND the keyed diff's
  // _applyRowDiff (only the rows it adds/replaces). Keeping one copy avoids
  // two-places-to-update drift between _bindAll and _renderFeed.
  function _bindRow(tr) {
    if (!tr) return;
    if (!(tr.classList.contains("locked") || tr.classList.contains("event"))) {
      tr.onclick = function (ev) {
        if (ev.target.closest(".goico") || ev.target.closest(".logico") || ev.target.closest(".alerico")) return;
        jumpChart(tr.dataset.sym, tr.dataset.tf);
      };
    }
    var g = tr.querySelector(".goico");
    if (g) {
      g.onclick = function (ev) {
        ev.stopPropagation();
        if (!tr.classList.contains("event")) jumpChart(tr.dataset.sym, tr.dataset.tf);
      };
    }
    var lg = tr.querySelector(".logico");
    if (lg) {
      lg.onclick = function (ev) {
        ev.stopPropagation();
        _logRowToJournal(tr);
      };
    }
    var al = tr.querySelector(".alerico");
    if (al) {
      al.onclick = function (ev) {
        ev.stopPropagation();
        _alertRowLevel(tr);
      };
    }
  }

  /* ---- bind interactions ------------------------------------------------- */
  function _bindAll(section) {
    // Lens chips
    section.querySelectorAll(".lens-chip").forEach(function (b) {
      b.onclick = function () { state.lens = b.dataset.lens; _saveState(); _render(); };
    });

    // Mode segment (Stream / By Asset)
    section.querySelectorAll(".modeseg button").forEach(function (b) {
      b.onclick = function () { state.mode = b.dataset.mode; _saveState(); _render(); };
    });

    // Search
    var si = section.querySelector("#radarSearch");
    if (si) {
      si.oninput = function () { state.search = si.value; _renderFeed(section); };
      // Don't let search input bubble to chart keyboard shortcuts
      si.addEventListener("keydown", function (e) { e.stopPropagation(); });
    }

    // Filter selects (TF, squeeze, signif)
    section.querySelectorAll("select[data-filter]").forEach(function (s) {
      s.onchange = function () { state[s.dataset.filter] = s.value; _render(); };
    });

    // Seg buttons (dir, side)
    section.querySelectorAll(".seg[data-seg] button").forEach(function (b) {
      b.onclick = function () {
        var seg = b.parentElement.dataset.seg;
        state[seg] = b.dataset.v;
        _render();
      };
    });

    // Pause button
    var pb = section.querySelector("#radarPauseBtn");
    if (pb) pb.onclick = _togglePause;

    // Row clicks (open chart) + Go/Log/Alert action buttons — stream rows.
    // _bindRow also covers the .goico/.logico/.alerico buttons
    // (all three only ever occur inside .erow — see
    // rowActionsHTML); one loop over the rows is equivalent and is also what
    // the keyed diff's _applyRowDiff calls per added/replaced row.
    section.querySelectorAll(".erow").forEach(_bindRow);

    // Asset group collapse toggle
    section.querySelectorAll(".ahead[data-toggle]").forEach(function (h) {
      h.onclick = function () {
        var sym = h.dataset.toggle;
        state.collapsed[sym] = !state.collapsed[sym];
        _renderFeed(section);
      };
    });

    // Subrow clicks (open chart) — by-asset rows
    section.querySelectorAll(".subrow").forEach(function (r) {
      if (r.classList.contains("locked")) return;
      r.onclick = function (ev) {
        if (ev.target.closest(".goico")) return;
        jumpChart(r.dataset.sym, r.dataset.tf);
      };
    });

    // Clear filters
    var ca = section.querySelector("#radarClearAll");
    if (ca) {
      ca.onclick = function () {
        state.search = ""; state.dir = "all"; state.tf = "all";
        state.squeeze = "all"; state.side = "all"; state.signif = "all";
        _render();
      };
    }
  }

  /* ---- partial re-render for search (avoids resetting scroll fully) ------- */
  function _renderFeed(section) {
    var feed = section.querySelector("#radarFeed");
    if (!feed) { _render(); return; }
    var scrollTop = feed.scrollTop;

    // This repaint (search input / asset-group collapse) bypasses the keyed
    // row cache — it doesn't prime rowSigs the way
    // _render() does. Invalidate so the next data tick's render() call falls
    // back to one full _render() (which recaptures a fresh cache) instead of
    // diffing against the nodes we're about to replace below. Acceptable per
    // design: this path isn't the hot 15/30s tick the diff targets.
    _streamDiffState.ready = false;

    var rows = _rows || [];
    var html = "";

    if (state.mode === "asset") {
      var assetFiltered = lensFilter(rows.filter(function (r) { return _isAssetRowType(r.row_type); }), state.lens);
      assetFiltered = assetFiltered.filter(passes);
      html = assetView(assetFiltered);
    } else if (state.lens === "transition" && _meta && _meta.preview) {
      html = upsell();
    } else {
      var filtered = lensFilter(rows.filter(function (r) { return _isStreamRowType(r.row_type); }), state.lens);
      filtered = filtered.filter(passes);
      var sorted = sortFor(filtered, state.lens);
      var allZones = 0;
      for (var i = 0; i < rows.length; i++) { if (rows[i].row_type === "zone") allZones++; }
      html = streamTable(sorted, state.lens, state.lens === "zone" ? allZones : null);
    }

    feed.innerHTML = html;
    feed.scrollTop = scrollTop;

    // Re-bind feed interactions only — stream rows + their Go/Log/Alert
    // buttons, via the same _bindRow used by _bindAll's full render and the
    // keyed diff.
    section.querySelectorAll(".erow").forEach(_bindRow);
    // Asset group collapse toggle
    section.querySelectorAll(".ahead[data-toggle]").forEach(function (h) {
      h.onclick = function () {
        var sym = h.dataset.toggle;
        state.collapsed[sym] = !state.collapsed[sym];
        _renderFeed(section);
      };
    });
    // Subrow clicks — by-asset rows
    section.querySelectorAll(".subrow").forEach(function (r) {
      if (r.classList.contains("locked")) return;
      r.onclick = function (ev) {
        if (ev.target.closest(".goico")) return;
        jumpChart(r.dataset.sym, r.dataset.tf);
      };
    });
    var ca = section.querySelector("#radarClearAll");
    if (ca) {
      ca.onclick = function () {
        state.search = ""; state.dir = "all"; state.tf = "all";
        state.squeeze = "all"; state.side = "all"; state.signif = "all";
        _render();
      };
    }
  }

  /* ====================================================================== */
  /*  PUBLIC API                                                             */
  /* ====================================================================== */
  window.Radar = {
    /**
     * (Re)render Radar into `section` using `rows` and `meta`.
     * Called by scenarios.js whenever the stream data updates.
     *
     * @param {Element} section  - #drawer-section-stream DOM element
     * @param {Array}   rows     - unified event rows from _computeStreamRows()
     * @param {Object}  meta     - { computedAt, nextRefreshTs, lastFetchTs, preview, tier }
     */
    render: function (section, rows, meta) {
      // Same section already mounted → this is a plain data refresh (the
      // 15/30s tick from scenarios.js's _computeAndRenderStream), not a user
      // interaction — those go through _render()/_renderFeed() directly from
      // this module's own click handlers, never through this public API.
      var sameSection = (_section === section);

      _section = section;
      _rows    = rows  || [];
      _meta    = meta  || {};

      // Sync pause state with scenarios.js loop flag
      if (state.paused) window.__radarPaused = true;

      // Clear any previous ticker before starting a new one
      if (_ticker) { clearInterval(_ticker); _ticker = null; }

      // try a keyed diff of the already-painted stream
      // table first; only fall back to the full rebuild when the diff can't
      // apply (first mount, By-Asset mode, upsell card, empty state, or a
      // structural mismatch — see _tryDiffStreamUpdate).
      if (!sameSection || !_tryDiffStreamUpdate()) {
        _render();
      }

      // Own ticker: updates the countdown ring + "updated N ago" every second
      _ticker = setInterval(_tick, 1000);
    },

    /**
     * Clear Radar's own ticker + any listeners. Called when the drawer section
     * is torn down or the tab is switched away.
     */
    teardown: function () {
      if (_ticker) { clearInterval(_ticker); _ticker = null; }
      _section = null;
      window.__radarPaused = false;
      // Drop references to the (about-to-be-detached) row nodes; the next
      // render() call will always be a fresh mount (_section is null, so
      // sameSection is false) and rebuild the diff cache from scratch anyway.
      _streamDiffState = { ready: false, section: null, lens: null, rowMap: {}, order: [] };
    },
  };

})();
