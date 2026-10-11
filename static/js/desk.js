// static/js/desk.js — Trader's Desk bootstrap
(function () {
  'use strict';
  if (document.body.dataset.context !== 'desk') return;
  var _assets = [];
  // Flat, uppercased symbol array — same shape GET /api/watchlist (default
  // format=flat) returns. Kept here (not in
  // desk-grid.js) because it's app/API-owned state, same role desk.js already
  // plays for `_assets` — desk-grid.js only renders whatever it's handed.
  var _watchlist = [];

  function showError(msg) {
    var el = document.getElementById('desk-error');
    el.hidden = false;
    el.innerHTML = msg + ' <button id="desk-retry" class="btn">Retry</button>';
    document.getElementById('desk-retry').onclick = refresh;
  }

  function selectAsset(sym, source) {
    if (window.posthog) posthog.capture('desk_drawer_open', { symbol: sym, source: source || 'row' });
    if (window.openDetailsDrawer) window.openDetailsDrawer(sym);
  }

  // Star toggle — same two endpoints watchlist.js uses
  // (addToWatchlist()/removeFromWatchlist()):
  // POST /api/watchlist/add|remove with {symbol}, session-cookie auth + CSRF
  // (carrac/fastapi_app/routers/watchlist.py). Deliberately does NOT
  // load the rest of watchlist.js (grouped-watchlist drag/drop + pagehide
  // flush machinery the Desk doesn't use). Optimistic
  // local update + re-render, reverted on failure.
  function toggleWatchlist(symbol) {
    symbol = String(symbol).toUpperCase();
    var idx = _watchlist.indexOf(symbol);
    var wasIn = idx !== -1;
    if (wasIn) _watchlist.splice(idx, 1); else _watchlist.push(symbol);
    draw();
    var url = wasIn ? '/api/watchlist/remove' : '/api/watchlist/add';
    var csrf = window.getCSRFToken ? window.getCSRFToken() : '';
    fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify({ symbol: symbol }),
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (data) {
      // Server is authoritative — sync to its returned list rather than
      // trust the optimistic splice/push above.
      if (data && Array.isArray(data.symbols)) { _watchlist = data.symbols; draw(); }
      if (window.posthog) posthog.capture('desk_watchlist_toggle', { symbol: symbol, action: wasIn ? 'remove' : 'add' });
    }).catch(function (e) {
      var i2 = _watchlist.indexOf(symbol);
      if (wasIn && i2 === -1) _watchlist.push(symbol);
      else if (!wasIn && i2 !== -1) _watchlist.splice(i2, 1);
      draw();
      if (window.carracLog) window.carracLog('desk', 'watchlist toggle failed', { symbol: symbol, error: String(e) });
    });
  }

  function draw() {
    var activity = (window.DeskStream && window.DeskStream.activityBySymbol()) || {};
    if (window.DeskGrid) {
      window.DeskGrid.render(_assets, {
        onRowClick: selectAsset,
        activityBySymbol: activity,
        watchlistSymbols: _watchlist,
        onWatchlistToggle: toggleWatchlist,
      });
    }
  }

  function refresh() {
    document.getElementById('desk-error').hidden = true;
    fetch('/api/assets?mode=htf').then(function (r) {
      if (r.status === 202) {
        return r.json().then(function (j) {
          setTimeout(refresh, ((j && j.retry_after) || 5) * 1000);
          throw new Error('warming');
        });
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (data) {
      _assets = data.assets || [];
      window.allAssets = {};
      _assets.forEach(function (a) { window.allAssets[a.symbol] = a; });
      draw();
    }).catch(function (e) {
      if (e.message !== 'warming') {
        showError('Market data failed to load (' + e.message + ').');
      }
    });
  }

  // Initial watchlist load — GET /api/watchlist (flat format, the default).
  // Independent of refresh()'s asset-warming retry loop; whichever of the two
  // resolves first paints, the other's draw() call fills in star state.
  function fetchWatchlist() {
    fetch('/api/watchlist', { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (arr) {
      _watchlist = Array.isArray(arr) ? arr : [];
      draw();
    }).catch(function (e) {
      if (window.carracLog) window.carracLog('desk', 'watchlist fetch failed', String(e));
      /* non-fatal — grid still works, just without star state */
    });
  }

  window.DeskApp = { refresh: refresh, selectAsset: selectAsset, redraw: draw };
  document.addEventListener('DOMContentLoaded', function () {
    if (window.posthog) posthog.capture('desk_opened');
    // desk.html doesn't load the chart app, so nothing else calls CarracDetails.init() —
    // do it here (idempotent via details-drawer.js's _initDone guard) so the
    // drawer's DOM refs exist before the first row/stream click opens it.
    if (window.CarracDetails && window.CarracDetails.init) window.CarracDetails.init();
    refresh();
    fetchWatchlist();
    setInterval(function () { if (!document.hidden) refresh(); }, 120000);
  });
})();
