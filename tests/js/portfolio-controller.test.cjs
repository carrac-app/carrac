const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// portfolio.js relies on the shared helpers (escapeHtml, showToast) from
// static/js/shared-utils.js, which the page loads before portfolio.js.
const SHARED_UTILS = path.join(__dirname, '../../static/js/shared-utils.js');
function runPortfolio(sandbox, src) {
  vm.runInContext(fs.readFileSync(SHARED_UTILS, 'utf8'), sandbox);
  vm.runInContext(src, sandbox);
}

function load() {
  const src = fs.readFileSync(
    path.join(__dirname, '../../static/js/portfolio.js'), 'utf8');
  const sandbox = { window: {}, document: { getElementById: () => null,
                    addEventListener: () => {}, querySelectorAll: () => [] },
                    fetch: () => Promise.resolve({ json: () => ({}) }),
                    setTimeout, clearTimeout, console };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  runPortfolio(sandbox, src);
  return sandbox.window.Portfolio;
}

test('formats dollars compactly and signed', () => {
  const P = load();
  assert.strictEqual(P.formatUsd(1234.5), '$1,234.50');
  assert.strictEqual(P.formatUsd(-1234.5), '-$1,234.50');
  assert.strictEqual(P.formatUsd(0), '$0.00');
  assert.strictEqual(P.formatUsd(null), '—');
});

test('excluded message names the reason and is silent when there is nothing to say', () => {
  const P = load();
  assert.strictEqual(P.buildExcludedMessage([]), '');
  const msg = P.buildExcludedMessage([
    { id: 1, asset: 'BTC', reason: 'no_size' },
    { id: 2, asset: 'ETH', reason: 'no_size' },
    { id: 3, asset: 'SOL', reason: 'no_price' },
  ]);
  assert.ok(msg.indexOf('3') !== -1, 'states the count');
  assert.ok(msg.indexOf('no size recorded') !== -1);
  assert.ok(msg.indexOf('no price') !== -1);
});

test('sorting is stable and handles nulls last', () => {
  const P = load();
  const rows = [
    { asset: 'ETH', size: 2 }, { asset: 'BTC', size: null }, { asset: 'SOL', size: 5 },
  ];
  const desc = P.sortRows(rows, 'size', false);
  assert.deepStrictEqual(desc.map(function(r) { return r.asset; }),
                         ['SOL', 'ETH', 'BTC']);
  const asc = P.sortRows(rows, 'size', true);
  assert.deepStrictEqual(asc.map(function(r) { return r.asset; }),
                         ['ETH', 'SOL', 'BTC']);
});

test('sorts strings case-insensitively', () => {
  const P = load();
  const rows = [{ asset: 'eth' }, { asset: 'BTC' }];
  assert.deepStrictEqual(
    P.sortRows(rows, 'asset', true).map(function(r) { return r.asset; }),
    ['BTC', 'eth']);
});

test('table rows carry the weight from the allocation block', () => {
  const P = load();
  const rows = P.tableRowsFrom({
    positions: [
      { id: 1, asset: 'BTC', kind: 'spot', book_contribution: 30000 },
      { id: 2, asset: 'ETH', kind: 'futures', book_contribution: 400 },
    ],
    allocation: { by_asset: [
      { asset: 'BTC', value: 30000, weight_pct: 98.68 },
      { asset: 'ETH', value: 400, weight_pct: 1.32 },
    ] },
  });
  assert.strictEqual(rows[0].weight_pct, 98.68);
  assert.strictEqual(rows[1].weight_pct, 1.32);
});

test('an asset missing from allocation gets zero weight, not undefined', () => {
  const P = load();
  const rows = P.tableRowsFrom({
    positions: [{ id: 1, asset: 'BTC', kind: 'spot', book_contribution: 0 }],
    allocation: { by_asset: [] },
  });
  assert.strictEqual(rows[0].weight_pct, 0);
});

// ── I7: buildRowHtml / esc() ────────────────────────────────────────────
//
// Every test that drives loadSummary()/renderTable() through this file's
// fetch/DOM harness only ever hands it EMPTY position arrays — so
// buildRowHtml() never actually executed anywhere in the suite before
// this. Deleting the esc() calls inside it is an XSS regression the whole
// 175-test suite would wave through. This is a string-level test: call
// buildRowHtml directly with a hostile asset symbol and assert the raw
// HTML markup never contains an un-escaped `<`/`>`/quote from the input.

test('buildRowHtml escapes a hostile asset symbol — an XSS regression must fail this test', () => {
  const P = load();
  const row = {
    asset: '<script>alert(1)</script>', kind: 'spot', direction: 'long',
    size: 1, entry_price: 100, current_price: 100,
    book_contribution: 100, unrealized_pnl_usd: 0, weight_pct: 0,
    opened_at: null,
  };
  const html = P.buildRowHtml(row);
  assert.ok(html.indexOf('<script>') === -1, 'raw <script> tag must never reach the DOM string');
  assert.ok(html.indexOf('&lt;script&gt;') !== -1, 'the hostile symbol must come back HTML-escaped');
});

test('buildRowHtml escapes a hostile symbol used to break out of the data-asset attribute', () => {
  const P = load();
  const row = {
    asset: '"><img src=x onerror=alert(1)>', kind: 'spot', direction: 'long',
    size: 1, entry_price: 100, current_price: 100,
    book_contribution: 100, unrealized_pnl_usd: 0, weight_pct: 0,
    opened_at: null,
  };
  const html = P.buildRowHtml(row);
  assert.ok(html.indexOf('"><img') === -1, 'must not be able to break out of the data-asset attribute');
  assert.ok(html.indexOf('&quot;&gt;&lt;img') !== -1, 'the attribute-breakout payload must be fully escaped');
});

// ── I4: curve excluded_count surfaced in the banner ─────────────────────
//
// carrac/portfolio/history.py:86 puts `excluded_count` on EVERY point of
// the returned curve — a day where at least one open position had no
// usable close and was left out of that day's totals rather than valued
// at zero. Before this, nothing in static/js/ ever read the field: built
// on the backend, never connected on the frontend, so a dip on the curve
// read identically whether it was a real move or a coverage gap.

test('buildCurveExclusionMessage is silent when no day excluded anything', () => {
  const P = load();
  const curve = [
    { date: '2026-01-01', book_value: 100, cost_basis: 100, excluded_count: 0 },
    { date: '2026-01-02', book_value: 110, cost_basis: 100, excluded_count: 0 },
  ];
  assert.strictEqual(P.buildCurveExclusionMessage(curve), '');
  assert.strictEqual(P.buildCurveExclusionMessage([]), '');
  assert.strictEqual(P.buildCurveExclusionMessage(null), '');
});

test('buildCurveExclusionMessage warns when any day in the range excluded a position', () => {
  const P = load();
  const curve = [
    { date: '2026-01-01', book_value: 100, cost_basis: 100, excluded_count: 0 },
    { date: '2026-01-02', book_value: 0, cost_basis: 0, excluded_count: 1 },
  ];
  const msg = P.buildCurveExclusionMessage(curve);
  assert.notStrictEqual(msg, '');
  assert.ok(msg.toLowerCase().indexOf('understate') !== -1 || msg.toLowerCase().indexOf('could not price') !== -1);
});

// ── Fetch-failure paths ──
//
// A failed fetch must never render identically to an empty portfolio: the
// #pf-excluded banner is the one element built to say "something is
// missing", so a broken load reuses it rather than leaving the page silent.
// This harness stubs a real, mutable `document` (unlike `load()` above,
// whose getElementById always returns null) so the banner's actual
// textContent/display can be asserted on.

function loadWithFetch(fetchImpl, localStorageImpl) {
  const src = fs.readFileSync(
    path.join(__dirname, '../../static/js/portfolio.js'), 'utf8');
  const els = {};
  function elFor(id) {
    if (!els[id]) {
      els[id] = {
        textContent: '', style: {}, value: undefined,
        addEventListener: () => {}, getAttribute: () => null,
      };
    }
    return els[id];
  }
  const sandbox = {
    window: {},
    document: {
      getElementById: (id) => elFor(id),
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    fetch: fetchImpl,
    localStorage: localStorageImpl || { getItem: () => null, setItem: () => {} },
    setTimeout, clearTimeout, console,
  };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  runPortfolio(sandbox, src);
  // `window` exposed alongside Portfolio so a test can stub the chart
  // globals (renderAllocationDonut etc.) INSIDE this sandbox's realm —
  // there is no real `window` in a plain node --test process, so a test
  // cannot reach the identifier portfolio.js itself resolves any other way.
  return { Portfolio: sandbox.window.Portfolio, window: sandbox.window, els };
}

// A fake `localStorage` backed by a single in-memory `klineDrawings` blob —
// mirrors the real shape (symbol -> array of drawings) that five other
// files in static/js/ read and write against this same key.
function storageWithDrawings(drawingsBySymbol) {
  const raw = JSON.stringify(drawingsBySymbol);
  return {
    getItem: (k) => (k === 'klineDrawings' ? raw : null),
    setItem: () => {},
  };
}

// ── The real entry point ──
//
// Every test above calls checkUnpromotedSpots()/loadSummary()/loadHistory()
// directly. That proves the logic is correct but NOT that init() actually
// wires it — if a future edit deleted `checkUnpromotedSpots();` from
// init(), every test above would keep passing while the banner never
// fired. This
// harness captures the real `document.addEventListener('DOMContentLoaded',
// init)` registration portfolio.js performs at module load, and exposes a
// `dispatch()` to actually fire it — driving the same code path the
// browser drives on page load, not a stand-in for it.
function loadAndDispatchInit(fetchImpl, localStorageImpl) {
  const src = fs.readFileSync(
    path.join(__dirname, '../../static/js/portfolio.js'), 'utf8');
  const els = {};
  function elFor(id) {
    if (!els[id]) {
      els[id] = {
        textContent: '', style: {}, value: undefined,
        addEventListener: () => {}, getAttribute: () => null,
      };
    }
    return els[id];
  }
  let domContentLoadedHandler = null;
  const sandbox = {
    window: {},
    document: {
      getElementById: (id) => elFor(id),
      querySelectorAll: () => [],
      addEventListener: (evt, handler) => {
        if (evt === 'DOMContentLoaded') domContentLoadedHandler = handler;
      },
    },
    fetch: fetchImpl,
    localStorage: localStorageImpl || { getItem: () => null, setItem: () => {} },
    setTimeout, clearTimeout, console,
  };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  runPortfolio(sandbox, src);
  return {
    els,
    dispatch: () => {
      assert.strictEqual(typeof domContentLoadedHandler, 'function',
        'portfolio.js must have registered a DOMContentLoaded handler');
      domContentLoadedHandler();
    },
  };
}

// Flushes pending microtasks (the fetch -> r.ok check -> r.json() -> render
// chain each loader runs) — a macrotask boundary via setTimeout guarantees
// every already-queued microtask has drained first.
function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

test('a rejected fetch produces an honest error banner, not silence', async () => {
  const { Portfolio, els } = loadWithFetch(() => Promise.reject(new Error('network down')));
  await Portfolio.loadSummary();
  assert.notStrictEqual(els['pf-excluded'].textContent, '');
  assert.ok(els['pf-excluded'].textContent.indexOf('Could not load') !== -1);
  assert.strictEqual(els['pf-excluded'].style.display, '');
});

test('a non-2xx response is treated as failure without trusting its body, and produces an honest banner', async () => {
  let jsonCalled = false;
  const { Portfolio, els } = loadWithFetch(() => Promise.resolve({
    ok: false,
    status: 500,
    json: () => { jsonCalled = true; return Promise.resolve({}); },
  }));
  await Portfolio.loadHistory();
  assert.strictEqual(jsonCalled, false, 'getJson must check r.ok before calling .json()');
  assert.notStrictEqual(els['pf-excluded'].textContent, '');
  assert.ok(els['pf-excluded'].textContent.indexOf('Could not load') !== -1);
});

test('a successful load after a prior failure clears the error banner', async () => {
  let callCount = 0;
  const { Portfolio, els } = loadWithFetch(() => {
    callCount += 1;
    if (callCount === 1) return Promise.reject(new Error('down'));
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ positions: [], excluded: [], allocation: {} }),
    });
  });

  await Portfolio.loadSummary();
  assert.notStrictEqual(els['pf-excluded'].textContent, '');

  // Second call on the SAME module instance, endpoint now healthy and
  // reporting nothing excluded — the banner must go back to fully
  // hidden, not linger from the earlier failure.
  await Portfolio.loadSummary();
  assert.strictEqual(els['pf-excluded'].textContent, '');
  assert.strictEqual(els['pf-excluded'].style.display, 'none');
});

// ── Unpromoted spot holdings ──
//
// A spot buy whose promotion POST failed never becomes a trade_journal
// row at all — it isn't "unvalued", it's invisible to the backend, so it
// can never appear in `excluded`. findUnpromotedSpots reads the same
// klineDrawings shape the rest of the chart code writes (symbol -> array
// of drawings) and applies the exact gate scripts/backfill_spot_journal.py
// uses server-side: name === 'spot', unstamped
// (extendData._journalDrawingId missing/null), not sold, positive
// quantity.

// NOTE: results come back from a vm-sandboxed realm, so a plain object/array
// created inside portfolio.js is NOT the same Object/Array constructor as
// this test file's — `assert.deepStrictEqual` treats that as a mismatch
// even when the contents are identical. Asserting on primitive fields
// (count, .length, Array.from(...) values) sidesteps that entirely, and
// is what every assertion below does instead of comparing the raw object.

function assertNoUnpromoted(result) {
  assert.strictEqual(result.count, 0);
  assert.strictEqual(result.assets.length, 0);
}

test('findUnpromotedSpots: empty store', () => {
  const P = load();
  assertNoUnpromoted(P.findUnpromotedSpots({}));
});

test('findUnpromotedSpots: fully promoted spots are not flagged', () => {
  const P = load();
  const store = {
    BTC: [{name: 'spot', extendData: {_journalDrawingId: 12345, quantity: 0.5}}],
  };
  assertNoUnpromoted(P.findUnpromotedSpots(store));
});

test('findUnpromotedSpots: a sold-but-unpromoted spot is excluded', () => {
  const P = load();
  const store = {SOL: [{name: 'spot', extendData: {sold: true, quantity: 3}}]};
  assertNoUnpromoted(P.findUnpromotedSpots(store));
});

test('findUnpromotedSpots: a zero-quantity spot is excluded', () => {
  const P = load();
  const store = {SOL: [{name: 'spot', extendData: {quantity: 0}}]};
  assertNoUnpromoted(P.findUnpromotedSpots(store));
});

test('findUnpromotedSpots: a non-spot drawing (e.g. longPosition) is ignored', () => {
  const P = load();
  const store = {SOL: [{name: 'longPosition', extendData: {quantity: 3}}]};
  assertNoUnpromoted(P.findUnpromotedSpots(store));
});

test('findUnpromotedSpots: a mix counts only the unpromoted, live holdings and dedupes assets', () => {
  const P = load();
  const store = {
    BTC: [
      {name: 'spot', extendData: {quantity: 0.5}},                          // unpromoted -> counted
      {name: 'spot', extendData: {_journalDrawingId: 999, quantity: 1}},     // promoted -> ignored
    ],
    ETH: [
      {name: 'spot', extendData: {quantity: 2}},                            // unpromoted -> counted
      {name: 'spot', extendData: {sold: true, quantity: 1}},                // sold -> ignored
      {name: 'spot', extendData: {quantity: 0}},                            // zero qty -> ignored
      {name: 'longPosition', extendData: {quantity: 5}},                    // not spot -> ignored
    ],
  };
  const result = P.findUnpromotedSpots(store);
  assert.strictEqual(result.count, 2);
  assert.deepStrictEqual(Array.from(result.assets).sort(), ['BTC', 'ETH']);
});

test('findUnpromotedSpots: malformed input never throws and reports nothing', () => {
  const P = load();
  assertNoUnpromoted(P.findUnpromotedSpots(null));
  assertNoUnpromoted(P.findUnpromotedSpots(undefined));
  assertNoUnpromoted(P.findUnpromotedSpots('not an object'));
  assertNoUnpromoted(P.findUnpromotedSpots({BTC: 'not-an-array'}));
  assertNoUnpromoted(P.findUnpromotedSpots({BTC: [null, 42, {name: 'spot'}]}));
});

// ── Third banner slot: wiring + non-clobbering + the sabotage guard ──

test('checkUnpromotedSpots writes a visible, honest banner naming the assets', () => {
  const store = {
    BTC: [{name: 'spot', extendData: {quantity: 0.5}}],
    ETH: [{name: 'spot', extendData: {quantity: 2}}],
  };
  const {Portfolio, els} = loadWithFetch(
    () => Promise.resolve({ok: true, json: () => Promise.resolve({})}),
    storageWithDrawings(store));

  Portfolio.checkUnpromotedSpots();

  const text = els['pf-excluded'].textContent;
  assert.notStrictEqual(text, '');
  assert.strictEqual(els['pf-excluded'].style.display, '');
  assert.ok(text.indexOf('2 spot holdings') !== -1, 'states the count');
  assert.ok(text.indexOf('BTC') !== -1);
  assert.ok(text.indexOf('ETH') !== -1);
  assert.ok(text.indexOf('on this device') !== -1, 'honest about the device scope');
  assert.ok(text.toLowerCase().indexOf('every device') === -1, 'must not claim it checked every device');
});

test('the drawings slot does not clobber a prior summary message (drawings arrives second)', async () => {
  const store = {BTC: [{name: 'spot', extendData: {quantity: 1}}]};
  const {Portfolio, els} = loadWithFetch(() => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      positions: [], excluded: [{id: 1, asset: 'DOGE', reason: 'no_price'}], allocation: {},
    }),
  }), storageWithDrawings(store));

  await Portfolio.loadSummary();
  assert.ok(els['pf-excluded'].textContent.indexOf('DOGE') !== -1);

  Portfolio.checkUnpromotedSpots();
  assert.ok(els['pf-excluded'].textContent.indexOf('DOGE') !== -1, 'summary message must survive');
  assert.ok(els['pf-excluded'].textContent.indexOf('BTC') !== -1, 'drawings message must appear');
});

test('the drawings slot does not get clobbered by a later summary message (drawings arrives first)', async () => {
  const store = {BTC: [{name: 'spot', extendData: {quantity: 1}}]};
  const {Portfolio, els} = loadWithFetch(() => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      positions: [], excluded: [{id: 1, asset: 'DOGE', reason: 'no_price'}], allocation: {},
    }),
  }), storageWithDrawings(store));

  Portfolio.checkUnpromotedSpots();
  assert.ok(els['pf-excluded'].textContent.indexOf('BTC') !== -1);

  await Portfolio.loadSummary();
  assert.ok(els['pf-excluded'].textContent.indexOf('BTC') !== -1, 'drawings message must survive');
  assert.ok(els['pf-excluded'].textContent.indexOf('DOGE') !== -1, 'summary message must appear');
});

test('the banner stays hidden when all three slots are empty', async () => {
  const {Portfolio, els} = loadWithFetch(() => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({positions: [], excluded: [], allocation: {}}),
  }), storageWithDrawings({}));

  await Portfolio.loadSummary();
  await Portfolio.loadHistory();
  Portfolio.checkUnpromotedSpots();

  assert.strictEqual(els['pf-excluded'].textContent, '');
  assert.strictEqual(els['pf-excluded'].style.display, 'none');
});

test('loadHistory() surfaces excluded_count from the real fetch response into the banner', async () => {
  const {Portfolio, els} = loadWithFetch(() => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      curve: [
        { date: '2026-01-01', book_value: 100, cost_basis: 100, excluded_count: 0 },
        { date: '2026-01-02', book_value: 0, cost_basis: 0, excluded_count: 2 },
      ],
      truncated: false,
    }),
  }));

  await Portfolio.loadHistory();
  assert.notStrictEqual(els['pf-excluded'].textContent, '', 'a coverage gap in the curve must be visible');
  assert.strictEqual(els['pf-excluded'].style.display, '');
});

// ── I5: a chart-library failure must not empty the page ─────────────────
//
// static/js/portfolio.js sets `_rows = tableRowsFrom(summary); renderTable();`
// BEFORE it builds the ECharts calls. A blocked CDN or an SRI mismatch on the
// pinned echarts <script> (templates/portfolio.html) leaves `window.echarts`
// undefined and the first real chart call throws; that must not abort the
// .then() callback or skip the table, since the summary fetch itself succeeded. These sandboxes never define
// `window.renderAllocationDonut`/`renderCompositionBars`/`renderBookCurve`
// as functions, and the code guards with `typeof ... === 'function'` —
// which does not reproduce the real failure. Simulate it directly by
// making those globals throw when called, the way a missing echarts
// global actually would.

test('a throwing allocation-chart render still leaves the table and banner populated', async () => {
  const {Portfolio, window: sandboxWindow, els} = loadWithFetch(() => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      positions: [{ id: 1, asset: 'BTC', kind: 'spot', direction: 'long', size: 1,
                    entry_price: 100, current_price: 110, book_contribution: 110,
                    unrealized_pnl_usd: 10, opened_at: null }],
      excluded: [],
      allocation: { by_asset: [{ asset: 'BTC', value: 110, weight_pct: 100 }] },
    }),
  }));

  sandboxWindow.renderAllocationDonut = () => { throw new Error('echarts is not defined'); };
  sandboxWindow.renderCompositionBars = () => { throw new Error('echarts is not defined'); };
  await Portfolio.loadSummary();

  assert.ok(els['pf-table-body'].innerHTML.indexOf('BTC') !== -1,
    'the positions table must render even though the chart calls threw');
  assert.strictEqual(els['pf-excluded'].textContent, '',
    'a successful summary load with nothing excluded must not show the generic fetch-error banner');
});

// This is the guard the sabotage step in Task 12 found missing: a spot
// drawing that never made it into the ledger must produce a VISIBLE
// banner, not a silently-empty one. Broken and restored by hand (see the
// task-9-report.md fix log) to confirm this test actually fails without
// the wiring — a guard never seen to fail is not a guard.
test('sabotage guard: an unpromoted spot on this device produces a visible banner', () => {
  const store = {BTC: [{name: 'spot', extendData: {quantity: 0.25}}]};
  const {Portfolio, els} = loadWithFetch(
    () => Promise.resolve({ok: true, json: () => Promise.resolve({})}),
    storageWithDrawings(store));

  Portfolio.checkUnpromotedSpots();

  assert.notStrictEqual(els['pf-excluded'].textContent, '', 'the missing holding must be visible, not silent');
  assert.strictEqual(els['pf-excluded'].style.display, '');
});

// This is the entry-point gap the review found: every test above calls
// checkUnpromotedSpots()/loadSummary()/loadHistory() directly, so none of
// them would notice if init() itself stopped calling one of them. This
// test dispatches the real DOMContentLoaded handler — the same thing the
// browser fires on page load — against a store containing an unpromoted
// spot, and covers all three loaders since the fetch stubbing needed to
// do that cheaply here was no more work than covering just the one.
test('init() (dispatched via the real DOMContentLoaded registration) wires all three loaders', async () => {
  const store = {BTC: [{name: 'spot', extendData: {quantity: 1}}]};
  const calledUrls = [];
  const fetchImpl = (url) => {
    calledUrls.push(url);
    if (url.indexOf('/api/portfolio/summary') !== -1) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          book_value: 42, position_count: 1, positions: [], excluded: [], allocation: {},
        }),
      });
    }
    return Promise.resolve({ok: true, json: () => Promise.resolve({curve: [], truncated: false})});
  };

  const {els, dispatch} = loadAndDispatchInit(fetchImpl, storageWithDrawings(store));
  dispatch();

  // checkUnpromotedSpots() is synchronous — its effect is already visible
  // the instant dispatch() returns, with no flush needed.
  assert.ok(els['pf-excluded'].textContent.indexOf('BTC') !== -1,
    'init() must actually call checkUnpromotedSpots() — this is the exact wiring Task 12 found missing');
  assert.strictEqual(els['pf-excluded'].style.display, '');

  // loadSummary()/loadHistory() are fetch-driven; let their promise chains
  // settle before checking their effects.
  await flushMicrotasks();

  assert.ok(calledUrls.some((u) => u.indexOf('/api/portfolio/summary') !== -1),
    'init() must actually call loadSummary()');
  assert.strictEqual(els['pf-book-value'].textContent, '$42.00',
    'loadSummary() reached via init() must actually populate the hero tile');
  assert.ok(calledUrls.some((u) => u.indexOf('/api/portfolio/history') !== -1),
    'init() must actually call loadHistory()');

  // The unpromoted-spots message must still be there after the async
  // loaders resolve — proving the three slots really are independent
  // through the real entry point, not just when called directly.
  assert.ok(els['pf-excluded'].textContent.indexOf('BTC') !== -1);
});
